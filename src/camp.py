"""
カンプ生成（仕様 4.7 / Phase 4 ＝ 最終目標）。

デザイン版RAGの「生成」層：
1. 作りたいものの説明（ブリーフ）でストックを検索し、雰囲気の近い参考を引く
2. 参考の「スクショ＋雰囲気文＋アニメのライブラリ名」を文脈としてClaudeに渡す
3. Claudeが **参照しつつ新しい** カンプのHTML下書きを生成する（複製ではない）

方針（仕様より）：
- 1サイトのクローンではなく「参照 → 作り直し」を徹底する
- 形式は「スクショ→HTML」（SVGは実装に進めないので使わない）
- アニメは「こういう演出」と言葉で足して再現させる
"""

from __future__ import annotations

import base64
import io
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config, db, search, tokens as tokens_mod
from .model import DesignEmbedder
from .utils import get_logger

log = get_logger("camp")

_SYSTEM = (
    "あなたは一流のWebデザイナー兼フロントエンド実装者です。"
    "参考サイトの『雰囲気』を取り入れつつ、それらを丸写しせず、"
    "依頼内容に合った新しいランディングページのデザインカンプ(HTML下書き)を作ります。"
)


def _ref_image_block(image_path: Path, max_w: int = 900, max_h: int = 900) -> Optional[dict]:
    """画像をJPEG(base64)のメッセージブロックにする。

    fullpage（縦長）も渡せるよう、幅と高さの上限を別々に指定できる。
    """
    from PIL import Image

    try:
        img = Image.open(image_path).convert("RGB")
    except Exception:
        return None
    img.thumbnail((max_w, max_h))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
    }


# 出現アニメを“確実に動かす”監視をこちら側で注入する。
# （Pythonで生成HTMLの末尾に足す＝トークン上限の途中切れと無縁。必ず動く）
# - html に 'js' を付与（CSSが html.js でアニメ初期状態を効かせている場合に対応）
# - reveal系要素を IntersectionObserver で監視し、画面に入ったら表示クラスを付ける
# - 画面内(ヒーロー等)は読み込み時に表示、下方はスクロールで順次表示＝ちゃんとアニメする
# - JSが万一動かなくても、html.js が付かない＝中身は見えるまま（白画面にならない）
_REVIEW_FALLBACK = """
<script>
(function(){
  var html=document.documentElement;
  html.classList.add('js');
  var SHOW=['in','show','is-visible','active','visible','in-view','inview','animated','revealed','aos-animate','is-inview','is-show','reveal-show','show-up','on','enter'];
  var SEL='[class*="reveal"],[class*="fade"],[class*="animate"],[class*="inview"],[class*="in-view"],[data-reveal]';
  function show(el){ for(var i=0;i<SHOW.length;i++) el.classList.add(SHOW[i]); }
  function run(){
    var els=document.querySelectorAll(SEL);
    if(!els.length) return;
    if(!('IntersectionObserver' in window)){ els.forEach(show); return; }
    var io=new IntersectionObserver(function(es){
      es.forEach(function(e){ if(e.isIntersecting){ show(e.target); io.unobserve(e.target);} });
    }, {threshold:0.12, rootMargin:'0px 0px -8% 0px'});
    els.forEach(function(el){ io.observe(el); });
    // 最終保険：3.5秒後、まだ隠れている要素は opacity を直接1にして必ず表示する。
    // （クラス名がClaude側と一致しなくても確実に見える＝「下のセクションが無い」を根絶）
    setTimeout(function(){
      els.forEach(function(e){
        if(parseFloat(getComputedStyle(e).opacity)===0){
          show(e); e.style.opacity='1'; e.style.transform='none';
        }
      });
    }, 3500);
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', run);
  else run();
})();
</script>
"""


def _finalize_html(html: str) -> str:
    """生成HTMLを安全に仕上げる。

    - トークン上限で末尾の <script> が途中で切れていたら、その壊れたスクリプトを捨てる
      （壊れたJSがあるとページ全体のJSが止まり、出現アニメで真っ白になるため）
    - 必ず全体が見える保険スクリプトを入れる
    - </body></html> が無ければ補う
    """
    # 閉じていない <script> が末尾にあれば切り捨てる
    if html.count("<script") > html.count("</script>"):
        cut = html.rfind("<script")
        html = html[:cut].rstrip()

    low = html.lower()
    if "</body>" in low:
        idx = low.rfind("</body>")
        html = html[:idx] + _REVIEW_FALLBACK + html[idx:]
    else:
        html = html.rstrip() + _REVIEW_FALLBACK + "\n</body>\n</html>\n"
    return html


def _strip_html(text: str) -> str:
    """Claudeの返答から HTML 本体だけ取り出す（```html フェンス等を除去）。"""
    m = re.search(r"```(?:html)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # フェンスが無ければ <!doctype/<html 以降を採用
    m = re.search(r"(<!doctype html.*|<html.*)", text, flags=re.DOTALL | re.IGNORECASE)
    return (m.group(1).strip() if m else text).strip()


def _to_openai_content(content: list) -> list:
    """Anthropic形式の content を OpenAI(chat) 形式に変換する。"""
    out = []
    for block in content:
        if block.get("type") == "text":
            out.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "image":
            src = block["source"]
            data_url = f"data:{src['media_type']};base64,{src['data']}"
            out.append({"type": "image_url", "image_url": {"url": data_url}})
    return out


def _call_anthropic(system: str, content: list) -> str:
    from anthropic import Anthropic

    vcfg = config.CONFIG.vibe
    client = Anthropic(api_key=vcfg.api_key)
    msg = client.messages.create(
        model=vcfg.model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    return msg.content[0].text


def _call_openai(system: str, content: list) -> str:
    from openai import OpenAI

    hcfg = config.CONFIG.htmlgen
    client = OpenAI(api_key=hcfg.openai_api_key)
    resp = client.chat.completions.create(
        model=hcfg.openai_model,
        max_completion_tokens=16000,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": _to_openai_content(content)},
        ],
    )
    return resp.choices[0].message.content or ""


def _call_llm(system: str, content: list) -> tuple[str, str]:
    """設定のプロバイダでHTMLを生成。返り値は (本文, 使ったモデル表示)。"""
    hcfg = config.CONFIG.htmlgen
    if hcfg.provider == "openai":
        if not hcfg.openai_enabled:
            raise RuntimeError("OPENAI_API_KEY が未設定です（.env を確認）")
        return _call_openai(system, content), f"openai:{hcfg.openai_model}"
    if not config.CONFIG.vibe.enabled:
        raise RuntimeError("ANTHROPIC_API_KEY が未設定です（.env を確認）")
    return _call_anthropic(system, content), f"anthropic:{config.CONFIG.vibe.model}"


def _pick_refs_no_model(brief: str, n: int) -> list[str]:
    """モデルを使わずに参考サイトを選ぶ（メモリ制約PC用のフォールバック）。

    雰囲気文がある（言語化済み）サイトを優先し、ブリーフとの文字の重なりで簡易採点。
    言語化済みが無ければ、最近撮ったサイトを使う。
    """
    with db.connect() as conn:
        rows = db.iter_sites_with_embedding(conn)
    described = [r for r in rows if r["vibe_description"]]
    pool = described or sorted(rows, key=lambda r: r["captured_at"], reverse=True)

    # ブリーフ内の2文字以上のかたまりが雰囲気文に何回出るかで素朴に採点
    grams = {brief[i : i + 2] for i in range(len(brief) - 1)}

    def score(r) -> int:
        text = (r["vibe_description"] or "") + " " + (r["animation_libs"] or "")
        return sum(1 for g in grams if g in text)

    ranked = sorted(pool, key=score, reverse=True)
    return [r["id"] for r in ranked[:n]]


def generate_camp(
    brief: str,
    n_refs: int = 3,
    embedder: Optional[DesignEmbedder] = None,
    use_model: bool = True,
    base_site_id: Optional[str] = None,
) -> dict:
    """ブリーフからカンプHTMLを生成して保存。生成結果のメタ情報を返す。

    use_model=True: SigLIPで雰囲気の近い参考を検索（本来の精度）。
    use_model=False: モデルを読まずに参考を選ぶ（メモリが厳しいPC向け）。
    base_site_id: 指定すると、そのサイトを"主役の参考"として先頭に置く
                  （配色・字組みをそのサイトに強く寄せたいとき）。
    """
    brief = brief.strip()
    if not brief:
        raise ValueError("作りたいサイトの説明（ブリーフ）が空です")

    # ① ストックから参考を引く（デザインRAGの"検索"）
    if use_model:
        embedder = embedder or DesignEmbedder()
        ref_ids = [h.site_id for h in search.search_by_text(brief, top_n=n_refs, embedder=embedder)]
    else:
        ref_ids = _pick_refs_no_model(brief, n_refs)
    # 主役サイトを先頭に固定（重複は除く）
    if base_site_id:
        ref_ids = [base_site_id] + [r for r in ref_ids if r != base_site_id]
        ref_ids = ref_ids[: max(1, n_refs)]
    log.info("カンプ生成: ブリーフ='%s' / 参考 %d 件 (use_model=%s, base=%s)",
             brief, len(ref_ids), use_model, base_site_id)

    # ② 参考の文脈を組み立てる。
    #   1件目（base指定時）は「レイアウトの手本」として fullpage(全体) を渡し、
    #   構造・構図・密度まで踏襲させる。他は firstview(ヒーロー) で雰囲気の補助。
    content: list = []
    refs_meta = []
    for i, site_id in enumerate(ref_ids, start=1):
        with db.connect() as conn:
            row = db.get_site(conn, site_id)
        if not row:
            continue
        vibe_txt = row["vibe_description"] or "(雰囲気文なし)"
        libs = row["animation_libs"] or "(検出なし)"
        is_layout_source = (i == 1)
        # デザイントークン（実数値）があれば具体的に渡す＝"らしさ"が乗る肝
        token_txt = ""
        if row["design_tokens"]:
            try:
                token_txt = "\n実際のデザイントークン:\n" + tokens_mod.tokens_to_prompt(
                    json.loads(row["design_tokens"])
                )
            except Exception:
                token_txt = ""

        if is_layout_source:
            # 全体スクショ（縦長）を渡してレイアウトの骨格を踏襲させる
            img_block = _ref_image_block(
                config.PROJECT_ROOT / row["fullpage_path"], max_w=820, max_h=4200
            )
            head = (
                f"# 参考{i}【★レイアウトの手本：この全体構成を忠実に踏襲する】: {row['url']}\n"
                f"↓これはこのサイトの『全体スクショ』。セクションの並び・各セクションの構図"
                f"（左右配置/グリッド列数/余白のリズム/情報の密度・強弱）を、できるだけ忠実に再現する。\n"
                f"雰囲気: {vibe_txt}\nアニメのライブラリ: {libs}{token_txt}"
            )
        else:
            img_block = _ref_image_block(config.PROJECT_ROOT / row["firstview_path"])
            head = f"# 参考{i}（雰囲気の補助）: {row['url']}\n雰囲気: {vibe_txt}{token_txt}"

        content.append({"type": "text", "text": head})
        if img_block:
            content.append(img_block)
        refs_meta.append({"url": row["url"], "libs": libs})

    # ③ 生成指示（参照→作り直し・1ファイルHTML・AIっぽさ回避）
    content.append(
        {
            "type": "text",
            "text": (
                f"# 依頼\n参考1（レイアウトの手本）の**構成をそのまま踏襲**して、次のサイトの"
                f"デザインカンプ(ランディングページ)を作ってください。\n\n"
                f"【作りたいもの】\n{brief}\n\n"
                "【レイアウトの踏襲（最重要）】\n"
                "- 参考1の全体スクショを見て、**セクションの種類・順番・各セクションの構図を忠実に再現**する\n"
                "  （例：ヒーローが左テキスト＋右画像なら同じに。次が横スクロールの商品列なら同じに。\n"
                "   グリッドの列数・余白の広さ・情報の密度・文字の大小のメリハリも合わせる）\n"
                "- レイアウトを自分で勝手に発明しない。汎用テンプレに逃げない\n"
                "- ただし**中身は全部差し替える**：コピー文・画像・ロゴ・イラストは依頼内容で作り直す（著作物を流用しない）\n\n"
                "【配色・字組み】\n"
                "- 参考の『実際のデザイントークン』の配色・フォント系統・角丸・余白に**忠実に**従う\n"
                "  （勝手に無難な色に変えない。手本サイトの配色をそのまま基調にする）\n\n"
                "【AIっぽさを避ける】\n"
                "- ❌ 手本に無いのに『横3カラム均等カード』を足さない（最もAIっぽい）\n"
                "- ✅ 手本の非対称・メリハリ・余白のリズムをそのまま活かす\n\n"
                "【画像（重要・ヘタな絵を描かない）】\n"
                "- ❌ 人物・風景・モノを CSSの図形やSVGで手描きしない（稚拙になるので絶対禁止）\n"
                "- ✅ 写真が入る所は **LoremFlickr のダミー写真**を使う：\n"
                "     https://loremflickr.com/{幅}/{高さ}/{英語キーワード}?lock={固有の数字}\n"
                "     キーワードはサイトのテーマに合わせる（例 cafe,coffee / factory,industry / books,reading）。\n"
                "     lock は画像ごとに違う数字にして固定（毎回変わらないように）。\n"
                "- <img> には width/height か aspect-ratio と object-fit:cover を必ず指定\n"
                "- 画像が読めない時に備え、画像の親に**ブランド色のbackground**を敷いておく\n"
                "- アイコンは絵文字か、シンプルな線のインラインSVGのみ（イラストは描かない）\n\n"
                "【アニメーション（本物っぽさの肝・しっかり入れる）】\n"
                "- スクロールで各セクションが**ふわっと出現**（fade＋少し下から上へ。複数要素は少しずつ時間差=stagger）\n"
                "- ヒーローは**読み込み時に**見出し・画像が動いて入る（軽いズーム/スライド/フェード）\n"
                "- ボタン・カードに**ホバーの微動**（少し浮く/影が増す/色が変わる、transitionで滑らかに）\n"
                "- 参考がGSAP/Lenis/Swiper等を使っていれば、その**動きの種類**をCSS/素のJSで控えめに再現\n"
                "- ★壊れても見えるように（重要）：先頭で `document.documentElement.classList.add('js')` を実行し、\n"
                "  出現アニメの初期非表示(opacity:0)は **`html.js` が付いている時だけ** 効かせる\n"
                "  （JSが無効/失敗でも中身は必ず見える）。IntersectionObserverで画面入り時に表示クラスを付ける\n\n"
                "【技術条件】\n"
                "- 1ファイルで完結するHTML（CSSは<style>に内包、JSは<script>に内包）。レスポンシブ対応\n"
                "- 日本語のダミーテキストで、実在しそうな具体的な内容にする\n"
                "- 返答は **HTMLコードだけ**。説明文やマークダウンの前置きは書かない\n"
            ),
        }
    )

    raw, used_model = _call_llm(_SYSTEM, content)
    html = _finalize_html(_strip_html(raw))

    config.CAMP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = config.CAMP_DIR / f"camp_{ts}.html"
    out.write_text(html, encoding="utf-8")
    log.info("カンプを保存: %s (%s)", out.name, used_model)

    return {
        "file": out.name,
        "brief": brief,
        "refs": refs_meta,
        "bytes": out.stat().st_size,
        "model": used_model,
    }
