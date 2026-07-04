"""実サイトの忠実クローン（DOMスナップショット方式）。

AIに描かせず、実ページの DOM＋CSS＋画像をそのまま吸い出して
カンプHTML（+ 画像フォルダ）に固める。忠実度が最優先の機能。

- CSSアニメ（@keyframes/transition）はCSSごと保存されるのでそのまま動く
- JSは捨てる（動かない・重い・危ない）代わりに、
  汎用のスクロール出現アニメを注入する（完璧じゃなくていい方針）
- 画像・フォントはダウンロードして `<カンプ名>_files/` に置き、相対パスに書き換える
  → data/camps に入るので、既存の編集バー・履歴・ダブルクリック単体表示がそのまま使える
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from . import assets, config, ingest
from .utils import get_logger, normalize_url, url_to_id, url_to_slug

log = get_logger("clone")

# ダウンロードの上限（暴走防止）
_MAX_ASSETS = 300
_MAX_BYTES = 15 * 1024 * 1024  # 1ファイル15MBまで

# content-type → 拡張子（不明はURLの拡張子を使う）
_CT_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg", "image/avif": ".avif",
    "image/x-icon": ".ico", "font/woff2": ".woff2", "font/woff": ".woff",
    "font/ttf": ".ttf", "application/font-woff2": ".woff2",
    "application/font-woff": ".woff", "video/mp4": ".mp4", "video/webm": ".webm",
}

_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)")
_IMPORT_RE = re.compile(r"@import\s+(?:url\(\s*)?['\"]?([^'\")\s;]+)['\"]?\s*\)?[^;]*;")


def _rebase_css(css: str, base_url: str) -> str:
    """CSS中の url(...) を絶対URLに直す（data:等はそのまま）。"""
    def rep(m: re.Match) -> str:
        u = m.group(2).strip()
        if u.startswith(("data:", "blob:", "#")):
            return m.group(0)
        return f'url("{urljoin(base_url, u)}")'
    return _URL_RE.sub(rep, css)


def _fetch_css(context, url: str, depth: int = 0) -> str:
    """外部CSSを取得して @import も展開（2段まで）。失敗は空文字で流す。"""
    if depth > 2:
        return ""
    try:
        res = context.request.get(url, timeout=20_000)
        if res.status != 200:
            return ""
        css = res.text()
    except Exception as exc:  # noqa: BLE001
        log.debug("CSS取得失敗（続行）: %s (%s)", url, exc)
        return ""
    css = _rebase_css(css, url)
    return _expand_imports(context, css, url, depth)


def _expand_imports(context, css: str, base_url: str, depth: int = 0) -> str:
    """@import をインライン展開する。"""
    def rep(m: re.Match) -> str:
        target = urljoin(base_url, m.group(1))
        return _fetch_css(context, target, depth + 1)
    return _IMPORT_RE.sub(rep, css)


# ── ページ内で実行するJS ──────────────────────────────

# document.styleSheets からCSSを回収（クロスオリジンで読めない物は href だけ返す）
_JS_COLLECT_SHEETS = """
() => {
  const out = [];
  for (const sheet of document.styleSheets) {
    const href = sheet.href || null;
    try {
      const css = Array.from(sheet.cssRules).map((r) => r.cssText).join("\\n");
      out.push({ href, css, ok: true });
    } catch (e) {
      out.push({ href, css: "", ok: false });
    }
  }
  return out;
}
"""

# 画像srcを確定（srcset→現物1枚）＋ 収集すべきURL一覧を返す
_JS_PREP_AND_LIST_ASSETS = """
() => {
  document.querySelectorAll("img").forEach((img) => {
    const src = img.currentSrc || img.src;
    if (src) img.setAttribute("src", src);
    img.removeAttribute("srcset");
    img.removeAttribute("sizes");
    img.removeAttribute("loading");
  });
  document.querySelectorAll("picture source").forEach((s) => s.remove());
  document.querySelectorAll("video").forEach((v) => {
    if (v.poster) v.setAttribute("poster", v.poster);
    if (v.getAttribute("src")) v.setAttribute("src", v.src);
  });
  const urls = new Set();
  document.querySelectorAll("img[src]").forEach((i) => urls.add(i.getAttribute("src")));
  document.querySelectorAll("video[poster]").forEach((v) => urls.add(v.getAttribute("poster")));
  document.querySelectorAll("video[src]").forEach((v) => urls.add(v.getAttribute("src")));
  document.querySelectorAll("[style]").forEach((el) => {
    const s = el.getAttribute("style") || "";
    (s.match(/url\\(([^)]+)\\)/g) || []).forEach((x) => {
      urls.add(x.replace(/^url\\(\\s*['"]?/, "").replace(/['"]?\\s*\\)$/, ""));
    });
  });
  return Array.from(urls);
}
"""

# 元CSS・CSP等を取り除く（CSSは回収済みの物を後で1本にして入れ直す）。
# keep_js=false のときは <script> も全部捨てる／true のときは残して src を絶対URL化。
_JS_STRIP = """
(keepJs) => {
  const sel = 'style, base, noscript, link[rel~="stylesheet"],' +
    ' link[rel="preload"], link[rel="modulepreload"], link[rel="prefetch"],' +
    ' meta[http-equiv="Content-Security-Policy"]';
  document.querySelectorAll(sel).forEach((el) => el.remove());
  // 右クリック禁止・選択禁止（属性方式）を外す＝編集バーの右クリックメニューが確実に開けるように
  document.querySelectorAll('[oncontextmenu],[onselectstart],[ondragstart]').forEach((el) => {
    el.removeAttribute('oncontextmenu'); el.removeAttribute('onselectstart'); el.removeAttribute('ondragstart');
  });
  if (keepJs) {
    document.querySelectorAll("script[src]").forEach((s) => s.setAttribute("src", s.src));
  } else {
    document.querySelectorAll("script").forEach((el) => el.remove());
  }
}
"""

# 注入するスクロール出現アニメ。IntersectionObserverではなく
# 250msの位置チェック（この環境で一番確実だった手法に合わせる）。
# ・方向4種（下から/左から/右から/ズーム）をセクションごとに順繰りに
# ・セクション内の直下の子（カード等）は時間差（スタッガー）で出す
# ★保存(cleanHtml)後の再表示でも壊れないよう、毎回クラスを掃除してやり直す。
_REVEAL_SCRIPT = """
<script id="__clone_reveal">
(function(){
  document.querySelectorAll(".__cl_pre,.__cl_kid").forEach(function(el){
    el.classList.remove("__cl_pre","__cl_in","__cl_kid");
    el.style.transitionDelay = "";
  });
  var st = document.createElement("style");
  st.textContent = [
    /* 動きは大きめ・1秒・最後スッと止まるイージング（派手め設定） */
    ".__cl_pre{opacity:0;transition:opacity 1s cubic-bezier(.16,1,.3,1),transform 1s cubic-bezier(.16,1,.3,1)}",
    '.__cl_pre[data-clv="up"]{transform:translateY(60px)}',
    '.__cl_pre[data-clv="left"]{transform:translateX(-80px)}',
    '.__cl_pre[data-clv="right"]{transform:translateX(80px)}',
    '.__cl_pre[data-clv="zoom"]{transform:scale(.88)}',
    ".__cl_pre.__cl_in{opacity:1;transform:none}",
    ".__cl_kid{opacity:0;transform:translateY(34px);transition:opacity .8s cubic-bezier(.16,1,.3,1),transform .8s cubic-bezier(.16,1,.3,1)}",
    ".__cl_in .__cl_kid{opacity:1;transform:none}"
  ].join("");
  document.head.appendChild(st);
  var VARIANTS = ["up", "left", "zoom", "right"];
  function prep(e, i){
    e.classList.add("__cl_pre");
    e.dataset.clv = VARIANTS[i % VARIANTS.length];
    // 子が2〜10個あれば時間差で出す（カード群がパラパラ現れる）
    var kids = Array.prototype.filter.call(e.children, function(k){ return k.offsetHeight > 20; });
    if (kids.length >= 2 && kids.length <= 10) {
      kids.forEach(function(k, j){
        k.classList.add("__cl_kid");
        k.style.transitionDelay = (0.15 * j).toFixed(2) + "s";
      });
    }
  }
  function init(){
    var vh = window.innerHeight;
    var els = Array.prototype.slice.call(document.querySelectorAll("section,footer,article"));
    if (els.length < 3) {
      els = Array.prototype.slice.call(document.querySelectorAll("body>*,body>div>*"))
        .filter(function(e){ return e.offsetHeight > 80; });
    }
    els = els.filter(function(e){ return !els.some(function(o){ return o !== e && o.contains(e); }); });
    // 最初の画面に見えている分＝開いた瞬間に時間差でフワッと出す
    var heroes = els.filter(function(e){ return e.getBoundingClientRect().top <= vh * 0.85; });
    els = els.filter(function(e){ return e.getBoundingClientRect().top > vh * 0.85; });
    heroes.forEach(function(e, i){
      prep(e, 0); /* 最初の画面は全部「下からフワッ」で統一 */
      setTimeout(function(){ e.classList.add("__cl_in"); }, 120 + 160 * i);
    });
    els.forEach(prep);
    var timer = setInterval(function(){
      var h = window.innerHeight, left = 0;
      els.forEach(function(e){
        if (e.classList.contains("__cl_in")) return;
        if (e.getBoundingClientRect().top < h * 0.88) { e.classList.add("__cl_in"); } else { left++; }
      });
      if (!left) clearInterval(timer);
    }, 250);
    setTimeout(function(){
      heroes.concat(els).forEach(function(e){ e.classList.add("__cl_in"); });
      clearInterval(timer);
    }, 30000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
</script>
"""

# keep_js（元サイトのJSを残す）モード用の保険：
# 出現アニメのopacity:0が発火せず残っても、4秒後に強制表示して真っ白を防ぐ。
_KEEPJS_SAFETY = """
<script id="__clone_safety">
(function(){
  /* 押した時だけ出す隠しメニュー/オーバーレイ(fixed/absoluteで隠されている)は
     本来ずっと隠れているものなので、保険で強制表示しない(MENUが開いた状態で残るのを防ぐ)。 */
  function inHiddenOverlay(e){
    var n=e;
    while(n && n!==document.body){
      var s=getComputedStyle(n);
      if((s.position==='fixed'||s.position==='absolute') && (parseFloat(s.opacity)===0 || s.visibility==='hidden')) return true;
      n=n.parentElement;
    }
    return false;
  }
  function sweep(){
    var all = document.querySelectorAll("body *");
    for (var i = 0; i < all.length; i++) {
      var e = all[i];
      var cs = getComputedStyle(e);
      var hidden = (parseFloat(cs.opacity) === 0) || (cs.visibility === "hidden");
      if (hidden && inHiddenOverlay(e)) continue;
      if (parseFloat(cs.opacity) === 0) {
        e.style.setProperty("opacity", "1", "important");
        e.style.transform = "none";
      }
      if (cs.visibility === "hidden") e.style.setProperty("visibility", "visible", "important");
    }
  }
  setTimeout(sweep, 4000);
})();
</script>
"""


def _ext_for(url: str, content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CT_EXT:
        return _CT_EXT[ct]
    m = re.search(r"\.([a-zA-Z0-9]{2,5})(?:[?#]|$)", url)
    return f".{m.group(1).lower()}" if m else ".bin"


def _download_assets(context, urls: list[str], assets_dir, dirname: str,
                     progress: Optional[Callable[[str], None]] = None,
                     preloaded: Optional[dict[str, Path]] = None) -> dict[str, str]:
    """URL群をダウンロードして {絶対URL: 相対パス} を返す。失敗は黙って飛ばす。

    preloaded に該当URLがあれば、ネット取得せず既存ファイルをそのままコピーする
    （「抽出済み画像で再現」モード＝二重取得を避け、既に手元にある画像を使う）。
    """
    mapping: dict[str, str] = {}
    total = min(len(urls), _MAX_ASSETS)
    for i, url in enumerate(urls[:_MAX_ASSETS]):
        if progress and i % 10 == 0:
            progress(f"素材をダウンロード中 {i}/{total}")
        pre = preloaded.get(url) if preloaded else None
        if pre is not None:
            try:
                body = pre.read_bytes()
                name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12] + pre.suffix
                (assets_dir / name).write_bytes(body)
                mapping[url] = f"{dirname}/{name}"
                continue
            except Exception as exc:  # noqa: BLE001
                log.debug("抽出済み画像の読込に失敗、通常DLへ切替: %s (%s)", url, exc)
        try:
            res = context.request.get(url, timeout=20_000)
            if res.status != 200:
                continue
            body = res.body()
            if not body or len(body) > _MAX_BYTES:
                continue
            ext = _ext_for(url, res.headers.get("content-type", ""))
            name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12] + ext
            (assets_dir / name).write_bytes(body)
            mapping[url] = f"{dirname}/{name}"
        except Exception as exc:  # noqa: BLE001
            log.debug("素材DL失敗（続行）: %s (%s)", url, exc)
    return mapping


def clone_site(url: str, keep_js: bool = False, use_extracted: bool = False,
               progress: Optional[Callable[[str], None]] = None) -> dict:
    """実サイトを忠実クローンして data/camps に保存する。

    keep_js=True なら元サイトの <script> を残す＝本物のアニメが動く可能性がある。
    ただし壊れる（真っ白・エラー）サイトもあるので保険スクリプト付き。
    use_extracted=True なら、事前に「🖼画像を抜き出す」で保存済みの画像を
    ネット再取得せずそのまま使う（一致しないURLは通常どおりその場でDLする）。
    返り値: {"file": ファイル名, "assets": 保存した素材数}
    """
    def say(msg: str) -> None:
        log.info("clone: %s", msg)
        if progress:
            progress(msg)

    cfg = config.CONFIG.capture
    norm_url = normalize_url(url)
    slug = url_to_slug(norm_url)

    preloaded: dict[str, Path] = {}
    if use_extracted:
        site_id = url_to_id(norm_url)
        manifest = assets.load_manifest(site_id)
        adir = assets.assets_dir(site_id)
        for au, fname in manifest.items():
            p = adir / fname
            if p.exists():
                preloaded[au] = p
        say(f"抽出済み画像 {len(preloaded)} 件を再利用します")

    ts = time.strftime("%Y%m%d_%H%M%S")
    suffix = "_js" if keep_js else ""
    out_name = f"clone_{slug}_{ts}{suffix}.html"
    files_dirname = f"clone_{slug}_{ts}{suffix}_files"
    config.CAMP_DIR.mkdir(parents=True, exist_ok=True)
    assets_dir = config.CAMP_DIR / files_dirname
    assets_dir.mkdir(parents=True, exist_ok=True)

    say("ページを開いています…")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.headless)
        context = browser.new_context(
            viewport={"width": cfg.viewport_w, "height": cfg.viewport_h},
            user_agent=cfg.user_agent,
        )
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.nav_timeout_ms)
        page.goto(norm_url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=cfg.networkidle_timeout_ms)
        except Exception:  # noqa: BLE001
            pass

        # 最後までスクロール＝lazy画像とJSアニメの「表示後の姿」を確定させる
        say("ページ全体を読み込み中…")
        ingest._trigger_lazy_load(page)  # noqa: SLF001（ingestの内部関数を意図的に再利用）

        # ① CSSを回収（読めた分＋クロスオリジンはfetchで取り直し）
        say("CSSを吸い出し中…")
        sheets = page.evaluate(_JS_COLLECT_SHEETS)
        css_parts: list[str] = []
        for sh in sheets:
            base = sh["href"] or norm_url
            css = sh["css"] if sh["ok"] and sh["css"] else ""
            if not css and sh["href"]:
                css = _fetch_css(context, sh["href"])
            if css:
                css = _rebase_css(css, base)
                css = _expand_imports(context, css, base)
                css_parts.append(f"/* == {sh['href'] or 'inline'} == */\n{css}")
        combined_css = "\n\n".join(css_parts)

        # ② DOM側の素材URLを確定・回収リストを作る
        dom_urls = page.evaluate(_JS_PREP_AND_LIST_ASSETS)

        # ③ 元CSS等（keep_jsでなければJSも）を除去してDOMを取り出す
        page.evaluate(_JS_STRIP, keep_js)
        html = page.evaluate("() => document.documentElement.outerHTML")

        # ④ 素材ダウンロード（DOM分＋CSS内のurl()分）
        asset_urls: list[str] = []
        seen: set[str] = set()
        for u in dom_urls:
            au = urljoin(norm_url, u)
            if au.startswith(("http://", "https://")) and au not in seen:
                seen.add(au)
                asset_urls.append(au)
        for m in _URL_RE.finditer(combined_css):
            au = m.group(2).strip()
            if au.startswith(("http://", "https://")) and au not in seen:
                seen.add(au)
                asset_urls.append(au)
        say(f"素材をダウンロード中（{min(len(asset_urls), _MAX_ASSETS)}件）…")
        mapping = _download_assets(context, asset_urls, assets_dir, files_dirname, progress, preloaded)

        context.close()
        browser.close()

    # ⑤ URL置換（長いURLから先に。HTML側は &amp; エスケープ版も置換）
    say("組み立て中…")
    for au in sorted(mapping, key=len, reverse=True):
        local = mapping[au]
        combined_css = combined_css.replace(au, local)
        html = html.replace(au, local)
        esc = au.replace("&", "&amp;")
        if esc != au:
            html = html.replace(esc, local)

    # 相対パス(url)がDOM内に残っていたら絶対URLのまま残す（読み込み切れないよりマシ）
    style_tag = f"<style>\n{combined_css}\n</style>"
    low = html.lower()
    if "</head>" in low:
        i = low.rfind("</head>")
        html = html[:i] + style_tag + html[i:]
    else:
        html = style_tag + html
    # keep_js＝本物のJSに任せて保険だけ／通常＝スクロール出現アニメを注入
    inject = _KEEPJS_SAFETY if keep_js else _REVEAL_SCRIPT
    low = html.lower()
    if "</body>" in low:
        i = low.rfind("</body>")
        html = html[:i] + inject + html[i:]
    else:
        html = html + inject

    if not html.lstrip().lower().startswith("<!doctype"):
        html = "<!DOCTYPE html>\n" + html

    out_path = config.CAMP_DIR / out_name
    out_path.write_text(html, encoding="utf-8")
    say("完了")
    log.info("クローン保存: %s（素材 %d 件）", out_path, len(mapping))
    return {"file": out_name, "assets": len(mapping)}
