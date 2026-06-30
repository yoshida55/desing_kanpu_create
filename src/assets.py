"""
画像の抜き出し（Feature：登録サイトから実画像を集める）。

ねらい：叩き台に「Aサイトの実画像（アイコン・写真・イラスト）」を仮置きして、
あとで Codex 等で差し替えやすくする。模擬・参考用（商用利用はしない前提）。

ライブページから集める対象：
- <img> の src / currentSrc（lazy対策でスクロールしてから）
- <picture><source srcset> の最大候補
- 背景画像 background-image: url(...)
- data: URI 画像
重複・極小（トラッカー）・SVGデータURIは除く。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Optional

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from . import config
from .utils import get_logger, normalize_url, url_to_id

log = get_logger("assets")

# ページ内の画像URLを集めるJS（絶対URLに直して返す）
_COLLECT_JS = r"""
() => {
  const out = new Set();
  const abs = (u) => { try { return new URL(u, location.href).href; } catch (e) { return null; } };
  // <img>（極小は除く）
  document.querySelectorAll('img').forEach((img) => {
    const r = img.getBoundingClientRect();
    if (r.width * r.height > 0 && r.width * r.height < 256) return;
    const u = img.currentSrc || img.src;
    const a = abs(u);
    if (a) out.add(a);
  });
  // <source srcset>（picture）
  document.querySelectorAll('source[srcset]').forEach((s) => {
    const first = (s.srcset || '').split(',')[0].trim().split(' ')[0];
    const a = abs(first);
    if (a) out.add(a);
  });
  // 背景画像
  document.querySelectorAll('*').forEach((el) => {
    const bg = getComputedStyle(el).backgroundImage;
    if (!bg || bg === 'none') return;
    const matches = bg.match(/url\((['"]?)(.*?)\1\)/g) || [];
    matches.forEach((m) => {
      const u = m.replace(/url\((['"]?)/, '').replace(/(['"]?)\)$/, '');
      const a = abs(u);
      if (a) out.add(a);
    });
  });
  return [...out];
}
"""

# 画像っぽい拡張子（クエリは無視して判定）
_IMG_EXT = {"png", "jpg", "jpeg", "gif", "webp", "svg", "avif", "ico", "bmp"}


def _ext_from(content_type: str, url: str) -> str:
    ct = (content_type or "").lower()
    table = {
        "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
        "image/webp": "webp", "image/svg+xml": "svg", "image/avif": "avif",
        "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico", "image/bmp": "bmp",
    }
    if ct.split(";")[0].strip() in table:
        return table[ct.split(";")[0].strip()]
    m = re.search(r"\.([a-zA-Z0-9]{2,4})(?:\?|#|$)", url)
    if m and m.group(1).lower() in _IMG_EXT:
        return m.group(1).lower()
    return "img"


def assets_dir(site_id: str) -> Path:
    return config.ASSET_DIR / site_id


def list_assets(site_id: str) -> list[str]:
    """保存済みの抜き出し画像の相対パス一覧（新しい順でなく名前順）。"""
    d = assets_dir(site_id)
    if not d.exists():
        return []
    files = sorted(
        f for f in d.iterdir()
        if f.is_file() and f.suffix.lstrip(".").lower() in _IMG_EXT
    )
    return [str(f.relative_to(config.PROJECT_ROOT)) for f in files]


def extract_images(url: str, max_count: int = 60) -> dict:
    """ライブページから画像を集めてDLし、data/assets/<id>/ に保存。"""
    cfg = config.CONFIG.capture
    norm_url = normalize_url(url)
    site_id = url_to_id(norm_url)
    out_dir = assets_dir(site_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("画像の抜き出し開始: %s", norm_url)
    urls: list[str] = []
    saved = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.headless)
        context = browser.new_context(
            viewport={"width": cfg.viewport_w, "height": cfg.viewport_h},
            user_agent=cfg.user_agent,
        )
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.nav_timeout_ms)
        try:
            page.goto(norm_url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=cfg.networkidle_timeout_ms)
            except PWTimeout:
                pass
            page.wait_for_timeout(cfg.settle_after_load_ms)
            # lazy画像を発火させるため最下部までスクロール
            try:
                page.evaluate(
                    "async()=>{await new Promise(r=>{let y=0;const t=setInterval(()=>{window.scrollBy(0,window.innerHeight);y+=window.innerHeight;if(y>=document.body.scrollHeight){clearInterval(t);r();}},120);});}"
                )
                page.wait_for_timeout(600)
            except Exception:
                pass
            urls = page.evaluate(_COLLECT_JS) or []
        except Exception as exc:  # noqa: BLE001
            log.error("ページを開けず: %s (%s)", norm_url, exc)

        # 集めたURLを1つずつDL（ブラウザのセッションで取得＝保護画像も取れる）
        seen_hash = set()
        for u in urls:
            if saved >= max_count:
                break
            try:
                if u.startswith("data:image/"):
                    head, b64 = u.split(",", 1)
                    if "svg" in head:
                        continue
                    data = base64.b64decode(b64 + "===")
                    ext = _ext_from(head, "")
                else:
                    resp = context.request.get(u, timeout=15000)
                    if not resp.ok:
                        continue
                    data = resp.body()
                    ext = _ext_from(resp.headers.get("content-type", ""), u)
                if len(data) < 1024:  # 1KB未満はトラッカー等とみなして捨てる
                    continue
                h = hashlib.sha1(data).hexdigest()
                if h in seen_hash:  # 中身が同じ画像は1枚に
                    continue
                seen_hash.add(h)
                (out_dir / f"img_{saved:03d}.{ext}").write_bytes(data)
                saved += 1
            except Exception:  # noqa: BLE001
                continue

        context.close()
        browser.close()

    log.info("画像の抜き出し完了: %d 枚保存 / 候補 %d", saved, len(urls))
    return {"saved": saved, "candidates": len(urls)}
