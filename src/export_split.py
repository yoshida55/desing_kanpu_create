"""納品用の「分割エクスポート」（HTML / CSS / JS を別ファイルに切り出し＋画像ローカル化）。

ねらい：ツールで作った1枚カンプを「そのまま渡せる納品物」に近づける。
- <style> の中身 → css/style.css に切り出し、<link> に置換
- インライン <script>（srcなし）→ js/script.js に切り出し、<script src> に置換
  （CDN等の外部 <script src> はそのまま残す）
- 画像を images/ に落として相対パスに書き換える（localhost/uploads・外部URL・クローン素材・data:）
- 上記を zip にまとめて data/exports/ に保存し、そのパスを返す

★これは「後処理」＝既存のカンプ生成/編集の仕組みには一切手を出さない。壊れようがない設計。
★命名規約に沿ったクラス名リネーム等は"仕上げ工程"（Claude Code等）に任せる。ここでは機械的な分割だけ。
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import config
from .utils import get_logger, url_to_slug

log = get_logger("export_split")

EXPORT_DIR = config.DATA_DIR / "exports"

_MAX_BYTES = 20 * 1024 * 1024  # 1画像20MBまで
_CT_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg", "image/avif": ".avif",
    "image/x-icon": ".ico", "video/mp4": ".mp4", "video/webm": ".webm",
}

# 編集ツール由来（保存済みファイルには通常無いが、念のため）を除去する
_CE_STYLE_RE = re.compile(r"<style[^>]*>(?:(?!</style>).)*?#__ce.*?</style>", re.DOTALL | re.IGNORECASE)
_CE_SCRIPT_RE = re.compile(r"<script[^>]*>(?:(?!</script>).)*?__ce.*?</script>", re.DOTALL | re.IGNORECASE)

_STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.DOTALL | re.IGNORECASE)
# src が無い <script> だけ（インラインJS）。src付き（CDN等）は残す。
_SCRIPT_INLINE_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_URL_IN_CSS_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.IGNORECASE)


def _ext_for(url: str, content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CT_EXT:
        return _CT_EXT[ct]
    m = re.search(r"\.([a-zA-Z0-9]{2,5})(?:[?#]|$)", url)
    return f".{m.group(1).lower()}" if m else ".img"


def _fetch_bytes(url: str, camp_dir: Path) -> tuple[bytes | None, str]:
    """画像URLの実体を取得。返り値 (bytes|None, content_type)。

    - data:URI → その場でデコード
    - /uploads/... や http://127.0.0.1:.../uploads/... → ローカルファイルを直接読む
    - 相対パス（clone_xxx_files/... 等）→ camp_dir 基準で読む
    - http(s) 外部 → ダウンロード
    """
    try:
        if url.startswith("data:"):
            head, b64 = url.split(",", 1)
            data = base64.b64decode(b64 + "===")
            ct = head[5:].split(";")[0]
            return data, ct
        parts = urlsplit(url)
        # localhost / uploads はローカルファイルから
        if "/uploads/" in parts.path:
            name = parts.path.split("/uploads/", 1)[1]
            p = config.UPLOAD_DIR / name
            if p.exists():
                return p.read_bytes(), ""
        # 相対パス（スキーム無し・先頭スラッシュ無し）→ カンプ隣の素材フォルダ
        if not parts.scheme and not parts.path.startswith("/"):
            p = camp_dir / url.split("?")[0].split("#")[0]
            if p.exists():
                return p.read_bytes(), ""
        # localhost の img 等その他ローカル配信も、まずファイルを試す
        if parts.hostname in ("127.0.0.1", "localhost"):
            # /uploads 以外（/img/... 等）は http で取りに行く（サーバー稼働前提）
            pass
        # http(s) をダウンロード
        if parts.scheme in ("http", "https"):
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (design-stock export)"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read(_MAX_BYTES + 1)
                if len(data) > _MAX_BYTES:
                    return None, ""
                return data, resp.headers.get("Content-Type", "")
    except Exception as exc:  # noqa: BLE001
        log.debug("画像取得失敗（続行）: %s (%s)", url, exc)
    return None, ""


def _collect_urls(html: str, css: str) -> list[str]:
    """HTML と CSS から画像/メディアのURLを集める（重複除去・順序保持）。"""
    seen: set[str] = set()
    out: list[str] = []

    def add(u: str) -> None:
        u = (u or "").strip()
        if not u or u.startswith("#"):
            return
        if u not in seen:
            seen.add(u)
            out.append(u)

    # <img src> / <img currentSrc は無い前提> / poster / <source src>
    for m in re.finditer(r'<img[^>]*\bsrc=(["\'])(.*?)\1', html, re.IGNORECASE):
        add(m.group(2))
    for m in re.finditer(r'\bposter=(["\'])(.*?)\1', html, re.IGNORECASE):
        add(m.group(2))
    for m in re.finditer(r'<source[^>]*\bsrc=(["\'])(.*?)\1', html, re.IGNORECASE):
        add(m.group(2))
    # インラインstyle と CSS の url()
    for m in _URL_IN_CSS_RE.finditer(html):
        add(m.group(2))
    for m in _URL_IN_CSS_RE.finditer(css):
        add(m.group(2))
    return out


def export_split(camp_filename: str) -> dict:
    """カンプHTMLを HTML/CSS/JS＋images に分割し、zip にまとめて返す。"""
    src_path = config.CAMP_DIR / camp_filename
    if not src_path.exists() or src_path.suffix != ".html":
        return {"ok": False, "message": "カンプが見つかりません"}

    html = src_path.read_text(encoding="utf-8", errors="ignore")
    camp_dir = src_path.parent

    # ① 編集ツール由来を念のため除去
    html = _CE_STYLE_RE.sub("", html)
    html = _CE_SCRIPT_RE.sub("", html)

    # ② <style> を集めて css へ、タグは除去
    css_parts = _STYLE_RE.findall(html)
    html = _STYLE_RE.sub("", html)
    css = "\n\n".join(p.strip() for p in css_parts if p.strip())

    # ③ インライン <script> を集めて js へ、タグは除去（src付きは残す）
    js_parts = _SCRIPT_INLINE_RE.findall(html)
    html = _SCRIPT_INLINE_RE.sub("", html)
    js = "\n\n".join(p.strip() for p in js_parts if p.strip())

    # ④ 画像を集めて images/ に落とす（URL→相対パスのマップを作る）
    urls = _collect_urls(html, css)
    mapping: dict[str, str] = {}
    images: dict[str, bytes] = {}
    warnings: list[str] = []
    for u in urls:
        data, ct = _fetch_bytes(u, camp_dir)
        if not data:
            warnings.append(u)
            continue
        ext = _ext_for(u, ct)
        name = "img_" + hashlib.sha1(u.encode("utf-8")).hexdigest()[:12] + ext
        images[name] = data
        mapping[u] = "images/" + name

    # ⑤ URL を相対パスに置換（長いURLから先に。&amp; エスケープ版も）
    for u in sorted(mapping, key=len, reverse=True):
        rel = mapping[u]
        html = html.replace(u, rel)
        css = css.replace(u, rel)
        esc = u.replace("&", "&amp;")
        if esc != u:
            html = html.replace(esc, rel)
    # srcset は現物1枚に統一済みでないので、壊れないよう属性ごと削除
    html = re.sub(r'\s+srcset=(["\']).*?\1', "", html, flags=re.IGNORECASE)

    # ⑥ <head> に css リンク、</body> 直前に js を差し込む
    link_tag = '<link rel="stylesheet" href="css/style.css">'
    low = html.lower()
    if "</head>" in low:
        i = low.rfind("</head>")
        html = html[:i] + "  " + link_tag + "\n" + html[i:]
    else:
        html = link_tag + "\n" + html
    if js:
        script_tag = '<script src="js/script.js"></script>'
        low = html.lower()
        if "</body>" in low:
            i = low.rfind("</body>")
            html = html[:i] + "  " + script_tag + "\n" + html[i:]
        else:
            html = html + "\n" + script_tag

    # ⑦ zip にまとめる
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    slug = url_to_slug(camp_filename.replace(".html", "")) or "site"
    ts = time.strftime("%Y%m%d_%H%M%S")
    zip_name = f"{slug}_{ts}.zip"
    zip_path = EXPORT_DIR / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html)
        if css:
            zf.writestr("css/style.css", css)
        if js:
            zf.writestr("js/script.js", js)
        for name, data in images.items():
            zf.writestr("images/" + name, data)

    log.info("分割エクスポート: %s（画像 %d 枚・欠け %d）", zip_name, len(images), len(warnings))
    return {
        "ok": True,
        "zip": zip_name,
        "images": len(images),
        "missing": len(warnings),
        "has_css": bool(css),
        "has_js": bool(js),
    }
