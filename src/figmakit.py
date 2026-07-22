"""🎨 Figma取り込み用の書き出し（AIなし＝無料・一瞬）。

カンプを「Figmaの html.to.design プラグインにそのまま取り込める状態」に整える。
実地テストで分かった2つのつまずきを機械で潰すのが仕事：
  ① アニメの初期状態が opacity:0 の要素は、Figmaに **透明のまま** 取り込まれて消える
     → 完成状態（全部表示）に固定する（_FLATTEN）
  ② 画像がツールのサーバー配信（/uploads/…）頼みだと、ファイル単体では画像が出ない
     → data:URI で本文に埋め込み、**単体で完結**させる（持ち運び・他人に渡せる）

2つの出口を用意する：
  A. キャプチャ用ページ（/camp_figma/<file>・軽い）… ツール起動中に開いて拡張でCapture
     ＝画像はサーバー配信のまま＝軽い。実地で成功した経路そのまま。
  B. figma_ready.html（単体完結・画像埋め込み）… サーバーが無い人に渡す用

★Figmaには「動き」は付かない（静止画のスナップショットになる）。動きは同梱の
  アニメ実装キット（animkit）でコーダーに渡す＝このツールの強みはそちらに残る。
"""
from __future__ import annotations

import base64
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import animkit, config, export_split, prodkit
from .utils import get_logger

log = get_logger("figmakit")

FIGMA_DIR = config.CAMP_DIR / "figma"

# アニメの初期非表示（opacity:0 / translate / clip-path 等）を「完成状態」に固定する保険。
# これが無いと、スクロールで出てくる要素がFigmaに透明・ズレた状態で取り込まれる。
# transform/clip-path はアニメ用クラスにだけ効かせる（飾りの意図した回転等は壊さない）。
_FLATTEN = """<style id="figma-flatten">
/* Figma取り込み用（figmakit）：スクロール出現の初期非表示だけを「完成状態」に固定する。
   ★ visibility は全体では触らない＝「隠れているべき覆い」（ローディング/モーダル等）を勝手に表示しない。
     実害(2026-07-22)：position:fixedのローディング `.loader.is-hidden{visibility:hidden}` を
     表示に戻してしまい、緑の覆い(z-index:9999)が画面を覆ったまま止まった。 */
*{animation:none!important;transition:none!important}
*{opacity:1!important}
/* スクロール出現系だけは visibility まで含めて確実に「表示・完成状態」へ */
.reveal,[class*="reveal"],.fxa_pre,.fxa_in,[class*="fxa_"],[data-aos],.wow,.animate__animated,
[class*="inview"],[class*="in-view"],[class*="is-visible"],[class*="is-show"],[class*="is-inview"]{
  opacity:1!important;visibility:visible!important;transform:none!important;filter:none!important;clip-path:none!important}
[class*="fxa_hl"]{--hlw:100!important}
/* ローディング/オープニングの覆いは「消す」（表示に固定すると画面を覆ったまま止まる）。
   ※ coverImg/coverPc/coverSp/coverPortal 等＝中身のカバー写真は消さない。loader/preload/splash/spinnerだけ */
[class*="loader"],[class*="preload"],[class*="splash"],[class*="spinner"],[class*="page-load"],
[id*="loader"],[id*="preload"],[id*="splash"]{display:none!important}
</style>
</body>"""

_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".avif": "image/avif", ".ico": "image/x-icon",
}


def flatten(html: str) -> str:
    """アニメの初期非表示を潰すスタイルを </body> 直前に足す（見た目＝完成状態に固定）。"""
    if "</body>" in html:
        return html.replace("</body>", _FLATTEN, 1)
    return html + _FLATTEN


def capture_ready(html: str) -> str:
    """キャプチャ用：掃除＋アニメ潰し。画像URLはそのまま（サーバー配信で表示）＝軽い。"""
    return flatten(prodkit._clean(html))


def _mime_for(url: str, ct: str) -> str:
    ct = (ct or "").split(";")[0].strip().lower()
    if ct.startswith(("image/", "video/")):
        return ct
    ext = os.path.splitext(url.split("?")[0].split("#")[0])[1].lower()
    return _MIME.get(ext, "image/png")


def embed_images(html: str, camp_dir: Path) -> tuple[str, int, int]:
    """画像を data:URI で本文に埋め込み、単体で完結させる。戻り (html, 埋め込み数, 欠け数)。

    URL解決は分割エクスポートの実績ロジックを再利用（/uploads・外部URL・クローン素材・data:）。
    """
    urls = export_split._collect_urls(html, "")
    repl: dict[str, str] = {}
    missing = 0
    for u in urls:
        if u.startswith("data:"):
            continue
        data, ct = export_split._fetch_bytes(u, camp_dir)
        if not data:
            missing += 1
            continue
        repl[u] = "data:%s;base64,%s" % (_mime_for(u, ct), base64.b64encode(data).decode())
    # 長いURLから置換（短いURLが長いURLの一部に化ける事故を防ぐ）
    for u in sorted(repl, key=len, reverse=True):
        html = html.replace(u, repl[u])
        esc = u.replace("&", "&amp;")
        if esc != u:
            html = html.replace(esc, repl[u])
    # srcset は埋め込んでいない別解像度を指すことがある＝ブラウザがそちらを読むと画像が欠ける。属性ごと外す
    html = re.sub(r'\s+srcset=(["\']).*?\1', "", html, flags=re.IGNORECASE)
    return html, len(repl), missing


def build_figmakit(filename: str) -> dict:
    """Figma取り込み用フォルダを1本ぶん書き出す。

    出力（data/camps/figma/<カンプ名>/）：
      figma_ready.html            … 単体で完結（掃除＋画像埋め込み＋アニメ潰し）＝配布・持ち運び用
      動きの引き継ぎ_animkit.html  … 動きをコードで渡す（Figmaでは動きが消えるため）
      Figmaへの取り込み方.md       … 手順

    戻り値 {dir, portable, capture_url, rows, embedded, missing, size_mb}
    """
    src = config.CAMP_DIR / filename
    if not src.exists():
        raise FileNotFoundError(f"カンプが見つかりません: {filename}")
    html = src.read_text(encoding="utf-8")

    out = FIGMA_DIR / src.stem
    out.mkdir(parents=True, exist_ok=True)

    # ① 単体完結HTML（掃除 → 画像埋め込み → アニメ潰し）
    cleaned = prodkit._clean(html)
    embedded_html, emb, miss = embed_images(cleaned, src.parent)
    portable = flatten(embedded_html)
    (out / "figma_ready.html").write_text(portable, encoding="utf-8")
    size_mb = round(len(portable.encode("utf-8")) / 1024 / 1024, 1)

    # ② 動きの引き継ぎ（Figmaでは動きが付かないので、コーダー用に別で渡す）
    rows = 0
    try:
        r = animkit.build_kit(filename)
        shutil.copy(animkit.KIT_DIR / r["file"], out / "動きの引き継ぎ_animkit.html")
        rows = r.get("rows", 0)
    except Exception as exc:  # noqa: BLE001  同梱失敗でも本体は必ず残す
        log.warning("アニメキットの同梱に失敗: %s", exc)

    # ③ 手順書
    miss_note = ("" if not miss else
                 f"\n- ⚠ 画像 {miss}枚 が取得できず埋め込めていません（外部URL切れ等）。"
                 "Figmaでは空欄になります。差し替え素材を確認してください。")
    md = (_README
          .replace("%DATE%", datetime.now().strftime("%Y-%m-%d"))
          .replace("%FILE%", filename)
          .replace("%CAPTURE%", f"http://127.0.0.1:5000/camp_figma/{filename}")
          .replace("%EMB%", str(emb)).replace("%MISS%", str(miss))
          .replace("%ROWS%", str(rows)).replace("%SIZE%", str(size_mb))
          .replace("%MISSNOTE%", miss_note))
    (out / "Figmaへの取り込み方.md").write_text(md, encoding="utf-8")

    log.info("Figma書き出し: %s（画像埋め込み%d・欠け%d・アニメ%d件・%sMB）",
             out, emb, miss, rows, size_mb)
    return {"dir": str(out), "portable": "figma_ready.html",
            "capture_url": f"/camp_figma/{filename}", "rows": rows,
            "embedded": emb, "missing": miss, "size_mb": size_mb}


_README = """# 🎨 Figmaへの取り込み方（%DATE% 生成）

元カンプ: %FILE%

## このフォルダの中身
- **figma_ready.html** … 単体で完結（画像%EMB%枚を埋め込み済み・アニメは完成状態で固定）。約%SIZE%MB
- **動きの引き継ぎ_animkit.html** … アニメ%ROWS%件をコード化。Figmaには動きが付かないので、動きはコーダーにこれで渡す
- **Figmaへの取り込み方.md** … これ

## 取り込み手順（おすすめ＝ツール経由・一番ラク）
1. ツールを起動（起動.bat）した状態で、カンプ編集画面の **「🎨 Figma用に書き出す」** を押すと
   キャプチャ用ページが自動で開きます:
   %CAPTURE%
2. Chrome右上の **html.to.design 拡張アイコン → Capture Current Page**
   （Viewport は **1440px** 推奨・他はチェックを外すと取り込み回数の節約になる）
3. **Send to Figma** → Figmaで html.to.design プラグインを起動 → **Extension タブ** → Import

## サーバーが無い人に渡す場合
- **figma_ready.html** をそのまま渡す → Figmaの html.to.design プラグイン
  「Import via browser extension」の枠に **ドラッグ&ドロップ**
  （または拡張でファイルを開いてCapture。file:// を使うには chrome://extensions で
  「ファイルの URL へのアクセスを許可」を ON）

## 大事な注意
- **Figmaには動き（アニメーション）は付きません**。入るのは「止まった完成状態」です。
  動きは **動きの引き継ぎ_animkit.html** でコーダーに渡してください（ツールの強みはそちらに残る）。
- 取り込んだ中身は「位置指定」寄りです。オートレイアウトへの組み直しはデザイナー作業になります
  （それでも白紙から作るより速いたたき台になります）。%MISSNOTE%
"""
