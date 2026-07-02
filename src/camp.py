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
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import anim as anim_mod, config, db, search, tokens as tokens_mod
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
# 保険スクリプトは、差し替え時に「古いのを消して新しいのを入れ直す」ため
# コメントで挟んで一意にマークする（_finalize_html が古い版を剥がして最新を入れる）。
_SAFE_START = "<!--__CE_SAFE_START__-->"
_SAFE_END = "<!--__CE_SAFE_END__-->"
_REVIEW_FALLBACK = _SAFE_START + """
<script>
(function(){
  var html=document.documentElement;
  html.classList.add('js');
  var SHOW=['in','show','is-visible','active','visible','in-view','inview','animated','revealed','aos-animate','is-inview','is-show','reveal-show','show-up','on','enter'];
  var SEL='[class*="reveal"],[class*="fade"],[class*="animate"],[class*="inview"],[class*="in-view"],[class*="stagger"],[class*="slide"],[class*="appear"],[data-reveal]';
  function show(el){ for(var i=0;i<SHOW.length;i++) el.classList.add(SHOW[i]); }
  function run(){
    var els=document.querySelectorAll(SEL);
    if(els.length && ('IntersectionObserver' in window)){
      var io=new IntersectionObserver(function(es){
        es.forEach(function(e){ if(e.isIntersecting){ show(e.target); io.unobserve(e.target);} });
      }, {threshold:0.12, rootMargin:'0px 0px -8% 0px'});
      els.forEach(function(el){ io.observe(el); });
    } else if(els.length){ els.forEach(show); }
    // ★最終保険：2.5秒後、クラス名に関係なく「透明・非表示のままの要素」を
    // すべて強制表示する。これで独自クラスの出現アニメ(stagger等)が
    // トリガー不発でも"真っ黒に消える"ことを根絶する（カンプは全部見えるのが正）。
    setTimeout(function(){
      var all=document.querySelectorAll('body *');
      for(var i=0;i<all.length;i++){
        var e=all[i];
        if(e.closest&&(e.closest('.fxa_pre')||e.closest('.fxa_wrap'))) continue;  // ★fxaの手付けアニメ(文字span含む)は監視が担当＝強制表示しない（タイプライター等が固定表示になるのを防ぐ）
        var cs=getComputedStyle(e);
        if(parseFloat(cs.opacity)===0){ e.style.setProperty('opacity','1','important'); e.style.transform='none'; e.style.animation='none'; }
        if(cs.visibility==='hidden'){ e.style.visibility='visible'; }
      }
    }, 2500);
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', run);
  else run();
})();
</script>
""" + _SAFE_END


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

    # 既に入っている保険スクリプト（前回の版）を剥がす→最新版を入れ直す。
    # （何度も編集しても重複せず、常に最新の"全部見える保険"が効くようにする）
    html = re.sub(
        re.escape(_SAFE_START) + r".*?" + re.escape(_SAFE_END),
        "", html, flags=re.DOTALL,
    )

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


def _anthropic_text(msg) -> str:
    """Claudeの返答から本文テキストだけを結合して返す。

    Sonnet 5 / Opus 4.8 等は「考える過程(ThinkingBlock)」も content に混ぜて返すため、
    先頭ブロックを決め打ちで .text すると落ちる。type=='text' のブロックだけ拾う。
    """
    parts = []
    for b in msg.content:
        if getattr(b, "type", None) == "text":
            parts.append(getattr(b, "text", ""))
    if parts:
        return "".join(parts)
    # 保険：type が付いていない実装でも text 属性があれば拾う
    return "".join(getattr(b, "text", "") for b in msg.content)


def _call_anthropic(system: str, content: list) -> str:
    from anthropic import Anthropic

    vcfg = config.CONFIG.vibe
    # 制限時間を付ける（固まっても180秒で諦めてエラー→「永遠に続く」を防ぐ）
    client = Anthropic(api_key=vcfg.api_key, timeout=180.0, max_retries=1)
    msg = client.messages.create(
        model=vcfg.model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    return _anthropic_text(msg)


def _call_openai(system: str, content: list) -> str:
    from openai import OpenAI

    hcfg = config.CONFIG.htmlgen
    client = OpenAI(api_key=hcfg.openai_api_key, timeout=180.0, max_retries=1)
    resp = client.chat.completions.create(
        model=hcfg.openai_model,
        max_completion_tokens=16000,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": _to_openai_content(content)},
        ],
    )
    return resp.choices[0].message.content or ""


def _call_gemini(system: str, content: list) -> str:
    """Gemini（無料枠が使える）でHTMLを生成（REST直叩き・SDK不要）。

    Anthropic形式の content（text/image ブロック）を Gemini の parts に変換して送る。
    """
    import urllib.request

    gcfg = config.CONFIG.gemini
    parts: list = []
    for b in content:
        if b.get("type") == "text":
            parts.append({"text": b["text"]})
        elif b.get("type") == "image":
            src = b["source"]
            parts.append({"inline_data": {
                "mime_type": src.get("media_type", "image/jpeg"),
                "data": src["data"],
            }})
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        # HTML全文はトークンが要るので上限を大きく取る
        "generationConfig": {"maxOutputTokens": 32768, "temperature": 0.7},
    }
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{gcfg.model}:generateContent?key={gcfg.api_key}"
    )
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=150) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    cand = data["candidates"][0]
    return "".join(p.get("text", "") for p in cand["content"]["parts"])


def _call_deepseek(system: str, content: list) -> str:
    """DeepSeek（OpenAI互換・激安）でHTMLを生成。

    DeepSeekは画像入力に非対応の想定なので、テキストのブロックだけ送る。
    （修正はテキストのみ＝影響なし。生成の画像手本は渡せない点は割り切り）
    """
    from openai import OpenAI

    dcfg = config.CONFIG.deepseek
    client = OpenAI(
        api_key=dcfg.api_key, base_url=dcfg.base_url, timeout=180.0, max_retries=1
    )
    text = "\n\n".join(b["text"] for b in content if b.get("type") == "text")
    resp = client.chat.completions.create(
        model=dcfg.model,
        max_tokens=8000,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
    )
    return resp.choices[0].message.content or ""


def _call_llm(system: str, content: list, provider: str | None = None) -> tuple[str, str]:
    """指定プロバイダでHTMLを生成。返り値は (本文, 使ったモデル表示)。

    provider を渡さなければ「生成用エンジン」(htmlgen.provider)を使う。
    修正時は provider=htmlgen.edit_provider を渡して別エンジンにできる。
    """
    hcfg = config.CONFIG.htmlgen
    provider = provider or hcfg.provider
    if provider == "openai":
        if not hcfg.openai_enabled:
            raise RuntimeError("OPENAI_API_KEY が未設定です（.env を確認）")
        return _call_openai(system, content), f"openai:{hcfg.openai_model}"
    if provider == "gemini":
        if not config.CONFIG.gemini.enabled:
            raise RuntimeError("GEMINI_API_KEY が未設定です（.env を確認）")
        return _call_gemini(system, content), f"gemini:{config.CONFIG.gemini.model}"
    if provider == "deepseek":
        if not config.CONFIG.deepseek.enabled:
            raise RuntimeError("DEEPSEEK_API_KEY が未設定です（.env を確認）")
        return _call_deepseek(system, content), f"deepseek:{config.CONFIG.deepseek.model}"
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


def _anim_ref_block(anim_ref_id: str) -> Optional[dict]:
    """アニメ参照B：そのサイトの抜き出したアニメ素材を、生成プロンプト用の指示にする。

    mix & match の核。Bの"動きの種類"を（丸写しではなく）雰囲気として寄せさせる。
    """
    with db.connect() as conn:
        row = db.get_site(conn, anim_ref_id)
    if not row or not row["animation_snippets"]:
        return None
    try:
        snippets = json.loads(row["animation_snippets"])
    except Exception:  # noqa: BLE001
        return None
    anim_txt = anim_mod.anim_to_prompt(snippets)
    libs = row["animation_libs"] or "(検出なし)"
    if not anim_txt and libs == "(検出なし)":
        return None
    return {
        "type": "text",
        "text": (
            f"# アニメ参照B【動きの種類はこのサイトに寄せる】: {row['url']}\n"
            f"下は参照Bから実際に抜き出したアニメ素材と使用ライブラリ。"
            f"この“動きの傾向”をCSS/素のJSで**控えめに再現**する"
            f"（コードの丸写しではなく、種類・速さ・向きの雰囲気を寄せる）。\n"
            f"使用ライブラリ: {libs}\n{anim_txt}"
        ),
    }


def generate_camp(
    brief: str,
    n_refs: int = 3,
    embedder: Optional[DesignEmbedder] = None,
    use_model: bool = True,
    base_site_id: Optional[str] = None,
    anim_ref_id: Optional[str] = None,
) -> dict:
    """ブリーフからカンプHTMLを生成して保存。生成結果のメタ情報を返す。

    use_model=True: SigLIPで雰囲気の近い参考を検索（本来の精度）。
    use_model=False: モデルを読まずに参考を選ぶ（メモリが厳しいPC向け）。
    base_site_id: 指定すると、そのサイトを"主役の参考"として先頭に置く
                  （配色・字組みをそのサイトに強く寄せたいとき）。
    anim_ref_id: 指定すると、そのサイトの抜き出したアニメの"動きの種類"を寄せる
                  （mix & match：Aの見た目にBの動き）。
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

    # ②' アニメ参照B（mix & match：Aの見た目にBの動き）を文脈に足す
    anim_ref_url = None
    if anim_ref_id:
        blk = _anim_ref_block(anim_ref_id)
        if blk:
            content.append(blk)
            with db.connect() as conn:
                brow = db.get_site(conn, anim_ref_id)
            anim_ref_url = brow["url"] if brow else None
            log.info("アニメ参照B を使用: %s", anim_ref_url)

    # ②'' ユーザー提供の実画像があれば、それを優先的に使わせる
    up_block = uploads_prompt_block()
    if up_block:
        content.append({"type": "text", "text": up_block})

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
                "【画像（重要・確実に表示させる）】\n"
                "- ❌ 人物・風景・モノを CSSの図形やSVGで手描きしない（稚拙になるので絶対禁止）\n"
                "- ✅ 上に『使える実画像（ユーザー提供）』があれば、内容の合う場所に**それを最優先で使う**"
                "（<img src> に一覧のURLをそのまま入れる。合う画像が無い所だけ次のpicsum）\n"
                "- ✅ 実画像で足りない所は **picsum.photos** を使う（安定・毎回同じ画像）：\n"
                "     https://picsum.photos/seed/{英語の固有シード}/{幅}/{高さ}\n"
                "     seed は画像ごとに違う語にする（例 team1, office2, hands3）。\n"
                "- <img> には width/height か aspect-ratio と object-fit:cover を必ず指定\n"
                "- ★画像が空に見えないよう二重の保険を必ず入れる：\n"
                "   1) 画像の**親要素に必ずグラデーション/ブランド色の background** を敷く（読込中や失敗でも色で埋まる）\n"
                "   2) <img> に onerror=\"this.style.display='none'\" を付ける（失敗時にalt文字を画面に出さない）\n"
                "- アイコンは絵文字か、シンプルな線のインラインSVGのみ（イラストは描かない）\n\n"
                "【アニメーション（本物っぽさの肝・しっかり入れる）】\n"
                "- スクロールで各セクションが**ふわっと出現**（fade＋少し下から上へ。複数要素は少しずつ時間差=stagger）\n"
                "- ヒーローは**読み込み時に**見出し・画像が動いて入る（軽いズーム/スライド/フェード）\n"
                "- ボタン・カードに**ホバーの微動**（少し浮く/影が増す/色が変わる、transitionで滑らかに）\n"
                "- 参考がGSAP/Lenis/Swiper等を使っていれば、その**動きの種類**をCSS/素のJSで控えめに再現\n"
                "- 上に『アニメ参照B』があれば、その**動きの種類・速さ・向き**を優先的に反映する（Aの見た目にBの動き）\n"
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
    # 同時生成でも衝突しないよう microsecond まで入れて一意にする
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out = config.CAMP_DIR / f"camp_{ts}.html"
    out.write_text(html, encoding="utf-8")
    log.info("カンプを保存: %s (%s)", out.name, used_model)

    return {
        "file": out.name,
        "brief": brief,
        "refs": refs_meta,
        "anim_ref": anim_ref_url,
        "bytes": out.stat().st_size,
        "model": used_model,
    }


# ── 反復編集：セクション単位で速く直す＋AIが改善案を提案する ──────────────
_SEC_RE = re.compile(r"<section\b[^>]*>.*?</section>", re.DOTALL | re.IGNORECASE)

_SUGGEST_SYSTEM = (
    "あなたは一流のWebデザイナー。既存のランディングページを見て、"
    "クライアント提案に使える『こう直せます』という改善案を、具体的に複数出します。"
)
_EDIT_SYSTEM = (
    "あなたは一流のフロントエンド実装者。既存LPの指定部分だけを依頼どおりに直します。"
    "ページ全体のトーン・配色・フォントは保ちます。"
)


def _strip_fragment(text: str) -> str:
    """AI返答から HTML 断片（セクション）を取り出す。

    ```html フェンスを外し、さらに前後の説明文（Geminiが付けがち）を落とす：
    最初の '<' から最後の '>' までを HTML 本体とみなす。
    """
    m = re.search(r"```(?:html)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if m:
        text = m.group(1)
    text = text.strip()
    lt, gt = text.find("<"), text.rfind(">")
    if lt != -1 and gt != -1 and gt > lt:
        text = text[lt:gt + 1]
    return text.strip()


def _extract_json(text: str):
    """AI返答から JSON 配列を取り出して読み込む。"""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    raw = m.group(1) if m else text
    m2 = re.search(r"\[.*\]", raw, flags=re.DOTALL)
    if m2:
        raw = m2.group(0)
    return json.loads(raw)


def list_camp_sections(html: str) -> list[dict]:
    """カンプHTMLの <section> を列挙し、見出しでラベル付けする（編集の指定用）。"""
    out = []
    for i, m in enumerate(_SEC_RE.finditer(html)):
        block = m.group(0)
        hm = re.search(r"<h[1-3][^>]*>(.*?)</h[1-3]>", block, flags=re.DOTALL | re.IGNORECASE)
        label = re.sub(r"<[^>]+>", "", hm.group(1)).strip() if hm else ""
        out.append({"index": i, "label": (label[:24] or f"セクション{i + 1}")})
    return out


def suggest_edits(filename: str, n: int = 10, section: int = -1) -> list[dict]:
    """カンプを見て、1クリックで試せる改善案を n 個提案する（種類を散らす）。

    section>=0 なら、そのセクションだけを見て「そのセクション向けの具体案」を多めに出す。
    section<0 なら、ページ全体から散らして出す。
    """
    html = (config.CAMP_DIR / filename).read_text(encoding="utf-8")
    secs = list_camp_sections(html)
    matches = list(_SEC_RE.finditer(html))

    if 0 <= section < len(matches):
        label = secs[section]["label"] if section < len(secs) else f"セクション{section + 1}"
        target = matches[section].group(0)
        prompt = (
            f"次はランディングページの『{label}』セクションのHTMLです。"
            f"このセクションだけをより良くする具体的な改善案を{n}個、たくさん出してください。\n"
            "実際のこのHTMLの中身を見て、そこにある要素に対する具体案にする（一般論でなく）。\n"
            "種類を散らす：画像／配色／レイアウト／余白／アニメ・動き／コピー文／装飾・あしらい／文字組み。\n\n"
            f"# このセクションのHTML\n{target}\n\n"
            "# 出力（JSON配列だけ・前置き無し）\n"
            f'[{{"label":"20字以内のボタン文言","section":{section},"instruction":"具体的な修正指示を1文で"}}]'
        )
    else:
        sec_list = "\n".join(f"{s['index']}: {s['label']}" for s in secs) or "(セクション未検出)"
        prompt = (
            f"次のランディングページHTMLを見て、具体的な改善案を{n}個、たくさん出してください。\n"
            "各案は1クリックで適用できる粒度（1つの狙いに絞る）にする。\n\n"
            f"# セクション一覧（index で指定する）\n{sec_list}\n\n"
            f"# HTML\n{html}\n\n"
            "# 出力（JSON配列だけ・前置き無し）\n"
            '[{"label":"20字以内のボタン文言","section":該当セクションのindex(整数。全体なら-1),"instruction":"具体的な修正指示を1文で"}]\n'
            "・label例：ヒーロー画像を実写に／料金表にホバーで浮く動き／CTAを黄色で目立たせる／余白を広げて上品に\n"
            "・種類を散らす（画像・配色・レイアウト・アニメ・コピーなど）"
        )
    raw, _ = _call_llm(
        _SUGGEST_SYSTEM, [{"type": "text", "text": prompt}],
        provider=config.CONFIG.htmlgen.edit_provider,
    )
    try:
        items = _extract_json(raw)
    except Exception:  # noqa: BLE001
        log.warning("改善案JSONの解析に失敗")
        items = []
    out = []
    for it in items[:n]:
        if isinstance(it, dict) and it.get("label") and it.get("instruction"):
            try:
                sec = int(it.get("section", -1))
            except Exception:  # noqa: BLE001
                sec = -1
            out.append({
                "label": str(it["label"])[:40],
                "section": sec,
                "instruction": str(it["instruction"])[:300],
            })
    log.info("改善案 %d 件を提案: %s", len(out), filename)
    return out


# ── ユーザー自前画像：アップロード → AIが内容を説明 → 生成/編集で優先使用 ──────
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_UPLOAD_BASE = "http://127.0.0.1:5000/uploads/"  # このツールの配信URL（ローカル固定）


def _uploads_meta_path() -> Path:
    return config.UPLOAD_DIR / "_captions.json"


def load_uploads_meta() -> dict:
    p = _uploads_meta_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_uploads_meta(meta: dict) -> None:
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _uploads_meta_path().write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


# ── カンプの「名前を付けて保存（お気に入り）」メタ ──────────────
# ファイル名 → {"name": 表示名, "fav": True} を JSON で持つ。
# HTMLファイル自体は汚さない（メタは別ファイル）。
def _camp_names_path() -> Path:
    return config.CAMP_DIR / "_names.json"


def load_camp_names() -> dict:
    p = _camp_names_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_camp_names(meta: dict) -> None:
    config.CAMP_DIR.mkdir(parents=True, exist_ok=True)
    _camp_names_path().write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def set_camp_name(filename: str, name: str, fav: bool = True) -> dict:
    """カンプに表示名を付ける（お気に入り登録）。名前が空なら登録を外す。"""
    meta = load_camp_names()
    name = (name or "").strip()
    if not name:
        meta.pop(filename, None)
    else:
        meta[filename] = {"name": name[:60], "fav": bool(fav)}
    save_camp_names(meta)
    return meta.get(filename, {})


def save_favorite(html: str, name: str) -> dict:
    """現在の完成形HTML（見た目＋焼き込んだ動き）を『お気に入り』として
    新ファイル fav_<時刻>.html にスナップショット保存する。

    元カンプは汚さず複製で残すので、あとで一覧から選べば丸ごと再現できる
    （動き・配色・画像差し替え・スクロール発火アニメも全部そのまま）。
    """
    if not html or len(html) < 200 or "</html>" not in html.lower():
        raise ValueError("HTMLが空か壊れています（保存中止）")
    config.CAMP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fn = f"fav_{ts}.html"
    p = config.CAMP_DIR / fn
    i = 1
    while p.exists():  # 同じ秒に連続保存しても衝突しないように連番を足す
        fn = f"fav_{ts}_{i}.html"
        p = config.CAMP_DIR / fn
        i += 1
    p.write_text(html, encoding="utf-8")
    meta = load_camp_names()
    meta[fn] = {"name": (name or "").strip()[:60] or "お気に入り", "fav": True, "snap": True}
    save_camp_names(meta)
    return {"file": fn, "name": meta[fn]["name"]}


def list_uploads() -> list[dict]:
    """アップロード済み画像の一覧（file, url, caption）。"""
    meta = load_uploads_meta()
    out = []
    if not config.UPLOAD_DIR.exists():
        return out
    for p in sorted(config.UPLOAD_DIR.iterdir()):
        if p.name.startswith("_") or not p.is_file() or p.suffix.lower() not in _IMG_EXTS:
            continue
        out.append({"file": p.name, "url": _UPLOAD_BASE + p.name, "caption": meta.get(p.name, "")})
    return out


_CAPTION_PROMPT = (
    "この画像をWebサイトの写真素材として使う前提で、日本語1行(30字以内)で説明して。"
    "人物/風景/建物/商品などの内容と雰囲気を端的に。説明文だけ返す。"
)


def _caption_gemini(path: Path) -> str:
    """Gemini（無料枠が使える）で画像に日本語1行キャプションを付ける。REST直叩き。"""
    import urllib.request

    from PIL import Image

    gcfg = config.CONFIG.gemini
    img = Image.open(path).convert("RGB")
    img.thumbnail((760, 760))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    b64 = base64.b64encode(buf.getvalue()).decode()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{gcfg.model}:generateContent?key={gcfg.api_key}"
    )
    body = {"contents": [{"parts": [
        {"text": _CAPTION_PROMPT},
        {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
    ]}]}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return text.strip().replace("\n", " ")[:60]


def caption_image(path: Path) -> str:
    """画像を見て、Web素材向けの日本語1行キャプションを付ける。

    Gemini（無料枠・安い）が設定されていれば優先。無ければ生成用LLM（Claude/GPT）で代替。
    """
    if config.CONFIG.gemini.enabled:
        try:
            cap = _caption_gemini(path)
            if cap:
                return cap
        except Exception:  # noqa: BLE001
            log.exception("Geminiキャプション失敗 → 生成用LLMで代替")

    blk = _ref_image_block(path, max_w=760, max_h=760)
    if not blk:
        return ""
    content = [blk, {"type": "text", "text": _CAPTION_PROMPT}]
    try:
        raw, _ = _call_llm("あなたは画像に短いキャプションを付けるアシスタントです。", content)
        return raw.strip().replace("\n", " ")[:60]
    except Exception:  # noqa: BLE001
        log.exception("画像キャプション生成に失敗")
        return ""


def uploads_prompt_block() -> str:
    """アップロード画像の一覧を、生成/編集プロンプト用の指示文にする。"""
    ups = list_uploads()
    if not ups:
        return ""
    lines = [
        "# 使える実画像（ユーザー提供・picsumより優先して使う）",
        "内容の合う場所には、下の実画像を <img src> に**そのままのURLで**使うこと（合う画像が無い所だけpicsum）。",
    ]
    for u in ups:
        lines.append(f"- {u['url']} … {u['caption'] or '(説明なし)'}")
    return "\n".join(lines)


_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def swap_image(filename: str, index: int, new_src: str) -> dict:
    """カンプ内の index 番目の <img> の src を new_src に差し替える（AI不使用・一瞬）。

    生成物の画像だけを、アップロード画像に手で置き換える用。別ファイルで保存。
    """
    new_src = (new_src or "").strip()
    if not new_src:
        raise ValueError("差し替える画像が指定されていません")
    html = (config.CAMP_DIR / filename).read_text(encoding="utf-8")
    tags = list(_IMG_TAG_RE.finditer(html))
    if index < 0 or index >= len(tags):
        raise ValueError("対象の画像が見つかりません")
    m = tags[index]
    tag = m.group(0)
    if re.search(r"""src\s*=\s*["']""", tag, flags=re.IGNORECASE):
        # 既存の src="..." / src='...' を置換（onerror等は残す）
        new_tag = re.sub(
            r"""(src\s*=\s*)(["']).*?\2""",
            lambda mm: mm.group(1) + '"' + new_src + '"',
            tag, count=1, flags=re.IGNORECASE,
        )
    else:
        new_tag = tag[:-1].rstrip() + f' src="{new_src}">'
    new_html = html[:m.start()] + new_tag + html[m.end():]

    config.CAMP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out = config.CAMP_DIR / f"camp_{ts}.html"
    out.write_text(new_html, encoding="utf-8")
    log.info("画像差し替え(AI不使用): %s img[%d] → %s", filename, index, new_src)
    return {"file": out.name, "swapped_index": index}


# 一括改善（Before→After営業デモ用）の共通方針。全セクションに同じ方向性を与えて統一感を出す
_IMPROVE_INSTRUCTION = (
    "このセクションの見た目を、現代的で洗練された印象に**上書きで**ブラッシュアップしてください。\n"
    "- HTMLは原則そのまま返す（文章・リンク・画像URL・構造・class名を維持）。"
    "変えていいのはルート<section>に目印class「imp」を足すことだけ\n"
    "- 見た目の変更は、セクション内に追加する <style> の**上書きCSSだけ**で行う\n"
    "- 上書きの方向：余白を広めに、行間ゆったり、モダンなタイポグラフィ、"
    "角丸＋ソフトシャドウ、上品な配色（彩度を抑えたアクセント1色）、ボタンは今風のフラット＋ホバー\n"
    "- 古臭い要素（ベタ塗りの原色・小さい文字・詰まった行間・立体ボタン・濃い枠線）を重点的に直す\n"
    "- ★重要：元サイトの巨大CSSが生きているので、セレクタは「section.imp .元class名」の形で"
    "**必ず勝つ具体度**で書き、効かない恐れがある所は !important を付ける"
)

# スクショが渡せるAI（Claude/GPT）用の大胆モード：見た目が見えているので構造ごと任せる
_IMPROVE_BOLD_INSTRUCTION = (
    "添付スクショが現在の見た目。これを**プロがフルリニューアルしたレベル**で作り直してください。\n"
    "- 文章・リンク・画像URLは**そのまま全部使う**（増やさない・減らさない・要約しない）\n"
    "- HTML構造は自由に組み直してよい。ただし**元のclass名は使わず**、新しいclass名（接頭辞 imp- ）で書く\n"
    "  （元サイトの巨大CSSが生きているため。imp- なら影響を受けない）\n"
    "- CSSは <style> でセクション内に完結。レイアウトはグリッド/カードなど現代的に\n"
    "- **アニメーションを必ず入れる（誰が見ても動いてると分かる強さで）**：\n"
    "  - 出現演出：見出しは1文字ずつ順に出す(spanに分割) or 60px以上のスライドイン。"
    "画像は scale(1.15)→1 のズームしながらフェードイン。要素ごとに0.15秒ずつの時間差\n"
    "  - 常時アニメ：背景グラデーションがゆっくり流れる(background-position移動) や、"
    "大きめの装飾図形が30px以上の振れ幅でゆっくり浮遊するなど、**画面のどこかが常に動いている**こと\n"
    "  - ホバー演出：カードは浮き上がり(translateY(-8px)+影拡大)、ボタンは色とサイズが変化\n"
    "  - 数字があればカウントアップ(<script>で0から実数まで)\n"
    "  - ❌ 2〜3pxの微妙な浮遊・0.2秒で終わる控えめフェードだけ＝不合格。"
    "営業デモで「おっ、動いてる」と声が出るレベルにする\n"
    "  - 出現はIntersectionObserverの小さな<script>をセクション内に入れてよい"
    "（クラス付与でopacity/transformをtransitionさせる方式）\n"
    "  - ★出現アニメには必ず「3秒後に強制表示」の保険を入れる（真っ白事故防止）\n"
    "  - ★★見出しを1文字ずつに分割する時は【分割済みの<span>を最初からHTMLに直接書き出す】こと。空白は<span>&nbsp;</span>と直接書く。\n"
    "    ❌ JSで見出しの innerHTML を組み立て直すのは**全面禁止**（`el.innerHTML=...` や `el.innerHTML=''`＋appendも含む）。\n"
    "       理由：そのJSは毎回リロードで再実行され、(1)&nbsp;が「&nbsp;」という文字に化ける (2)後から足した別のアニメを消す、という事故を起こす。\n"
    "    出現の発火(opacityやtransformのtransition開始)だけをJS(IntersectionObserver)でやるのはOK。ただし中身(innerHTML)は絶対に作り直さない。\n"
    "- Before→Afterの営業デモ用なので、**一目で「別物に良くなった」と分かる**変化量を出すこと"
)

# 1セクションがこれより大きい場合はAIに送らずスキップ（コスト暴発・上限超え防止）
_IMPROVE_MAX_SECTION = 60_000
# ページ全体CSSは参考として先頭だけ渡す（クローンのCSSは1MB級になるため）
_IMPROVE_MAX_STYLE = 12_000


def _shoot_sections(filename: str, indices: list[int]) -> dict[int, Path]:
    """カンプの指定セクションのスクショを撮る（改善AIに「現在の見た目」を見せる用）。

    出現アニメで透明のままだと真っ白に写るので、強制表示してから撮る。
    失敗したセクションは黙って飛ばす（テキストのみで改善を続行できる）。
    """
    from playwright.sync_api import sync_playwright

    path = config.CAMP_DIR / filename
    shots_dir = config.CAMP_DIR / "_improve_shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    out: dict[int, Path] = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(path.resolve().as_uri())
            page.wait_for_timeout(1200)
            page.evaluate(
                """() => {
                  document.querySelectorAll('body *').forEach((e) => {
                    const cs = getComputedStyle(e);
                    if (parseFloat(cs.opacity) === 0) { e.style.setProperty('opacity','1','important'); e.style.transform='none'; }
                    if (cs.visibility === 'hidden') e.style.setProperty('visibility','visible','important');
                  });
                }"""
            )
            page.wait_for_timeout(300)
            secs = page.query_selector_all("section")
            # 入れ子は外側だけ数える（_SEC_RE の数え方に合わせる近似）
            tops = [s for s in secs if not s.evaluate("(el) => !!el.parentElement.closest('section')")]
            for i in indices:
                if i >= len(tops):
                    continue
                try:
                    tops[i].scroll_into_view_if_needed()
                    page.wait_for_timeout(200)
                    shot = shots_dir / f"sec_{i}.png"
                    tops[i].screenshot(path=str(shot))
                    out[i] = shot
                except Exception as exc:  # noqa: BLE001
                    log.debug("improve: セクション%dのスクショ失敗（テキストのみで続行）: %s", i, exc)
            browser.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("improve: スクショ撮影に失敗（全てテキストのみで続行）: %s", exc)
    return out


def improve_all(filename: str, limit: int = 0, targets: list[int] | None = None,
                hint: str = "", ref_id: str = "", progress=None) -> dict:
    """セクションを順に「今風」へ一括改善し、別ファイル（After版）に保存する。

    limit > 0 なら最初の limit セクションだけ。targets（0始まりの番号リスト）を渡すと
    その番号だけ改善（例 [1,4]＝2番目と5番目）。両方無指定なら全部。
    hint はデザインの方向性（例「高級ホテルのように」「ポップで元気に」）。指示に追記される。
    ref_id を渡すと、その登録サイトを「手本」として画像＋トークンでAIに見せる（Claude/GPT時のみ）。
    ＝言葉より画像の手本が効く（カンプ生成で実証済みの決定打）。
    Claude/GPTのときは各セクションのスクショを撮って見せる＝見た目の判断が具体的になる。
    失敗したセクションは元のまま残して続行する（全滅しない）。
    返り値: {"file": 新ファイル名, "total": 対象数, "improved": 成功数, "skipped": スキップ数}
    """
    def say(msg: str) -> None:
        log.info("improve: %s", msg)
        if progress:
            progress(msg)

    html = (config.CAMP_DIR / filename).read_text(encoding="utf-8")
    real_total = len(list(_SEC_RE.finditer(html)))
    if real_total == 0:
        raise ValueError("<section> が見つかりません（このページは一括改善に対応できません）")
    if targets:
        indices = sorted({i for i in targets if 0 <= i < real_total})
    else:
        n = min(real_total, limit) if limit and limit > 0 else real_total
        indices = list(range(n))
    if not indices:
        raise ValueError("対象セクションがありません（番号を確認してください）")
    total = len(indices)

    # Claude/GPTは画像が見える → 現在の見た目を撮って渡す（DeepSeek/Geminiはテキストのみ）
    provider = config.CONFIG.htmlgen.edit_provider
    shots: dict[int, Path] = {}
    if provider in ("anthropic", "openai"):
        say("セクションのスクショを撮影中…（AIに見た目を見せる）")
        shots = _shoot_sections(filename, indices)

    # 手本サイト（目指す雰囲気）：画像＋トークンで見せる＝言葉より効く
    ref_blocks: list = []
    ref_note = ""
    if ref_id and provider in ("anthropic", "openai"):
        with db.connect() as conn:
            ref_row = db.get_site(conn, ref_id)
        if ref_row and ref_row["firstview_path"]:
            token_txt = ""
            if ref_row["design_tokens"]:
                try:
                    token_txt = "\n実際のデザイントークン:\n" + tokens_mod.tokens_to_prompt(
                        json.loads(ref_row["design_tokens"]))
                except Exception:  # noqa: BLE001
                    token_txt = ""
            ref_img = _ref_image_block(config.PROJECT_ROOT / ref_row["firstview_path"])
            if ref_img:
                ref_blocks = [
                    {"type": "text", "text": (
                        f"# 手本サイト（目指す雰囲気）: {ref_row['url']}\n"
                        "↓次の画像がその手本。配色・余白・質感・タイポ・装飾の雰囲気を、この手本に強く寄せること。\n"
                        f"雰囲気: {ref_row['vibe_description'] or '(雰囲気文なし)'}{token_txt}\n"
                        "※手本の文章・ロゴ・写真はコピーしない（見た目の方向だけ参照）"
                    )},
                    ref_img,
                ]
                ref_note = "※画像は複数ある：先の画像＝手本サイト、最後の画像＝このセクションの現在の見た目。\n\n"
                say(f"手本サイトを添付: {ref_row['url']}")

    config.CAMP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = config.CAMP_DIR / f"camp_{ts}_improved.html"
    out.write_text(_finalize_html(html), encoding="utf-8")  # まず元の複製（途中経過も見られる）

    improved = 0
    skipped = 0
    for step, i in enumerate(indices):
        # 差し替えのたびに位置がズレるので、毎回探し直す
        matches = list(_SEC_RE.finditer(html))
        if i >= len(matches):
            break
        m = matches[i]
        section_html = m.group(0)
        labels = list_camp_sections(html)
        label = labels[i]["label"] if i < len(labels) else f"セクション{i + 1}"
        say(f"{step + 1}/{total}: {label}")
        if len(section_html) > _IMPROVE_MAX_SECTION:
            log.info("improve: セクション%dは大きすぎるのでスキップ（%d文字）", i, len(section_html))
            skipped += 1
            continue
        hint_txt = f"\n\n# デザインの方向性（ユーザー指定・最優先で反映）\n{hint}" if hint else ""
        content: list = []
        if i in shots:
            img_block = _ref_image_block(shots[i], max_w=1000, max_h=1400)
            if img_block:
                content.append(img_block)
        if content:  # スクショあり＝大胆モード（構造ごと組み直し＋アニメ盛り込み）
            if ref_blocks:
                content = list(ref_blocks) + content
            body = (
                f"指定セクションをフルリニューアルしてください。\n{ref_note}\n"
                f"# 直す対象セクション（この中身＝文章・画像を全部使う）\n{section_html}\n\n"
                f"# 依頼\n{_IMPROVE_BOLD_INSTRUCTION}{hint_txt}\n\n"
                "# 出力ルール\n"
                "- 新しいセクションHTML一式だけ返す（<section class=\"imp-...\">…</section>）\n"
                "- CSSと、アニメ用の小さな<script>はセクション内に入れて完結させる\n"
                "- 返答は HTML だけ。説明やマークダウンの前置きは書かない"
            )
        else:  # テキストのみ（DeepSeek/Gemini）＝見た目が見えないので安全な上書きCSS方式
            style_m = re.search(r"<style\b[^>]*>.*?</style>", html, flags=re.DOTALL | re.IGNORECASE)
            style_ctx = style_m.group(0)[:_IMPROVE_MAX_STYLE] if style_m else "(なし)"
            body = (
                "指定セクションだけを依頼どおり直してください。ページ全体との統一感を保つ。\n\n"
                f"# ページ全体のCSS（参考・長い場合は先頭のみ）\n{style_ctx}\n\n"
                f"# 直す対象セクション（このHTMLだけを直す）\n{section_html}\n\n"
                f"# 依頼\n{_IMPROVE_INSTRUCTION}{hint_txt}\n\n"
                "# 出力ルール\n"
                "- 元のセクションHTMLをそのまま返し、ルートに class「imp」を足し、"
                "先頭か末尾に上書き用の <style> を1つ入れる（<section>…</section> 一式）\n"
                "- 中身のHTML構造・class名・文章・画像URLは変えない（見た目はCSSだけで変える）\n"
                "- 返答は HTML だけ。説明やマークダウンの前置きは書かない"
            )
        content.append({"type": "text", "text": body})
        try:
            raw, _used = _call_llm(_EDIT_SYSTEM, content, provider=config.CONFIG.htmlgen.edit_provider)
            new_section = _strip_fragment(raw)
            if "<" not in new_section:  # 返事がHTMLじゃない＝失敗扱い
                raise ValueError("HTMLが返ってきませんでした")
            html = html[: m.start()] + new_section + html[m.end():]
            improved += 1
            out.write_text(_finalize_html(html), encoding="utf-8")  # 1つ終わるごとに保存
        except Exception as exc:  # noqa: BLE001
            log.warning("improve: セクション%dの改善に失敗（元のまま続行）: %s", i, exc)
            skipped += 1

    out.write_text(_finalize_html(html), encoding="utf-8")
    for pth in shots.values():  # スクショの後片付け
        try:
            pth.unlink()
        except Exception:  # noqa: BLE001
            pass
    log.info("一括改善 完了: %s → %s（成功%d/スキップ%d/全%d）", filename, out.name, improved, skipped, total)
    return {"file": out.name, "total": total, "improved": improved, "skipped": skipped}


def edit_camp_section(filename: str, section_index: int, instruction: str) -> dict:
    """指定セクションだけを依頼どおり直す（速い）。section_index<0 は全体編集（遅い）。

    新しいHTMLは別ファイルに保存（元は残す＝いつでも戻れる）。
    """
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("修正指示が空です")
    html = (config.CAMP_DIR / filename).read_text(encoding="utf-8")
    matches = list(_SEC_RE.finditer(html))
    whole = section_index is None or section_index < 0 or not matches or section_index >= len(matches)
    up_block = uploads_prompt_block()  # ユーザー提供画像があれば編集でも使う
    up_txt = ("\n\n" + up_block) if up_block else ""

    if whole:
        # 全体編集：HTML全部を渡して直す（確実だが遅い）
        content = [{"type": "text", "text": (
            "次のLP全体を、依頼どおり**最小限だけ**直してHTML全体を返してください。"
            "指示に無い所は変えない。トーン・配色・フォントは維持。\n\n"
            f"# 依頼\n{instruction}{up_txt}\n\n# 現在のHTML\n{html}\n\n返答はHTMLだけ。"
        )}]
        raw, used = _call_llm(_EDIT_SYSTEM, content, provider=config.CONFIG.htmlgen.edit_provider)
        new_html = _finalize_html(_strip_html(raw))
    else:
        m = matches[section_index]
        section_html = m.group(0)
        style_m = re.search(r"<style\b[^>]*>.*?</style>", html, flags=re.DOTALL | re.IGNORECASE)
        style_ctx = style_m.group(0) if style_m else "(なし)"
        content = [{"type": "text", "text": (
            "指定セクションだけを依頼どおり直してください。全体のトーン・配色・フォントは保つ。\n\n"
            f"# ページ全体のCSS（参考・むやみに変えない）\n{style_ctx}\n\n"
            f"# 直す対象セクション（このHTMLだけを直す）\n{section_html}\n\n"
            f"# 依頼\n{instruction}{up_txt}\n\n"
            "# 出力ルール\n"
            "- このセクションの**新しいHTMLだけ**返す（<section>…</section> 一式）\n"
            "- 見た目の変更でCSSが要る場合は、返すセクションの中に <style> を入れて完結させる（既存クラスの上書きも可）\n"
            "- 写真は、上の『使える実画像』があればURLをそのまま優先使用。無ければ picsum.photos（https://picsum.photos/seed/{英語シード}/{幅}/{高さ}）。\n"
            "  親要素にグラデ/色backgroundを敷き、<img>に onerror=\"this.style.display='none'\" を付けて空表示を防ぐ（絵は手描きしない）\n"
            "- 返答は HTML だけ。説明やマークダウンの前置きは書かない"
        )}]
        raw, used = _call_llm(_EDIT_SYSTEM, content, provider=config.CONFIG.htmlgen.edit_provider)
        new_section = _strip_fragment(raw)
        new_html = html[:m.start()] + new_section + html[m.end():]
        # 差し替え後も必ず"全部見える保険"を入れ直す（出現アニメで真っ黒に消えるのを防ぐ）
        new_html = _finalize_html(new_html)

    config.CAMP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out = config.CAMP_DIR / f"camp_{ts}.html"
    out.write_text(new_html, encoding="utf-8")
    log.info("カンプを部分編集: %s → %s (section=%s)", filename, out.name, section_index)
    return {"file": out.name, "model": used, "edited_section": -1 if whole else section_index}
