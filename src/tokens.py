"""
デザイントークン抽出（仕様 Phase 3 / カンプ生成の"効き"を強くする）。

ねらい：登録サイトの「ふわっとした雰囲気」ではなく、**実際の数値**
（配色・フォント・余白・角丸・影・レイアウト構造）を抜き出す。
これをカンプ生成でClaudeに具体的に渡すと、出力に"そのサイトらしさ"が乗る。

抽出はライブページ（Playwright）から行う＝計算済みCSSなので色やフォントが正確。
"""

from __future__ import annotations

import json
from typing import Optional

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from . import config, db
from .utils import get_logger, normalize_url, url_to_id

log = get_logger("tokens")

# ライブページから計算済みスタイルを集計するJS
_EXTRACT_JS = r"""
() => {
  const els = [...document.querySelectorAll('body *')].slice(0, 4000);
  const tally = {};
  const bump = (m, k, w) => { if (k) m[k] = (m[k] || 0) + (w || 1); };
  const skip = (c) => !c || c === 'transparent' || c.indexOf('rgba(0, 0, 0, 0)') >= 0;

  const bg = {}, fg = {}, accent = {}, radius = {}, shadow = {};
  let maxW = 0;
  const rowGrids = []; // 横並びの要素数を集める（AIっぽい3カラム検出用）

  for (const el of els) {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const area = r.width * r.height;
    if (!skip(s.backgroundColor) && area > 1500) bump(bg, s.backgroundColor, Math.round(area / 1000));
    if (!skip(s.color)) bump(fg, s.color);
    if (el.matches('a,button,[class*="btn"],[class*="button"],[class*="cta"]')) {
      if (!skip(s.backgroundColor)) bump(accent, s.backgroundColor, 3);
    }
    if (s.borderRadius && s.borderRadius !== '0px') bump(radius, s.borderRadius);
    if (s.boxShadow && s.boxShadow !== 'none') bump(shadow, s.boxShadow);
    // コンテナ幅の最大値（中央寄せ枠の目安）
    if (s.maxWidth && s.maxWidth.endsWith('px')) {
      const w = parseFloat(s.maxWidth);
      if (w > maxW && w < 2000) maxW = w;
    }
    // flex/grid で横に複数並ぶ親 → 子の数を記録
    if ((s.display === 'flex' && s.flexDirection.startsWith('row')) || s.display === 'grid') {
      const n = el.children.length;
      if (n >= 2 && n <= 6 && r.width > 500) rowGrids.push(n);
    }
  }

  const top = (m, n) => Object.entries(m).sort((a, b) => b[1] - a[1]).slice(0, n).map(x => x[0]);
  const bodyFont = getComputedStyle(document.body).fontFamily;
  const hEl = document.querySelector('h1, h2');
  const headFont = hEl ? getComputedStyle(hEl).fontFamily : bodyFont;

  return {
    bg: top(bg, 5),
    text: top(fg, 3),
    accent: top(accent, 4),
    radius: top(radius, 3),
    shadow: top(shadow, 2),
    head_font: headFont,
    body_font: bodyFont,
    container_max: maxW ? Math.round(maxW) + 'px' : '',
    row_counts: rowGrids,   // 例: [3,3,2] → 3カラムが多い
  };
}
"""


def extract_tokens(url: str) -> Optional[dict]:
    """ライブページからデザイントークンを抽出して返す（DB保存はしない）。"""
    cfg = config.CONFIG.capture
    norm_url = normalize_url(url)
    log.info("トークン抽出: %s", norm_url)
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
            tokens = page.evaluate(_EXTRACT_JS)
        except Exception as exc:  # noqa: BLE001
            log.error("トークン抽出に失敗: %s (%s)", norm_url, exc)
            tokens = None
        finally:
            context.close()
            browser.close()
    return tokens


def extract_and_store(url: str) -> Optional[dict]:
    """抽出してDBに保存する（site が登録済みであること）。"""
    site_id = url_to_id(normalize_url(url))
    tokens = extract_tokens(url)
    if tokens is None:
        return None
    with db.connect() as conn:
        db.update_tokens(conn, site_id, json.dumps(tokens, ensure_ascii=False))
    log.info("トークン保存: %s", url)
    return tokens


def tokens_to_prompt(tokens: dict) -> str:
    """抽出トークンを、Claudeに渡す簡潔な指定文に整える。"""
    if not tokens:
        return ""
    rows = tokens.get("row_counts") or []
    threes = rows.count(3)
    lines = [
        f"- 背景色: {', '.join(tokens.get('bg', [])[:4])}",
        f"- 文字色: {', '.join(tokens.get('text', [])[:2])}",
        f"- アクセント色: {', '.join(tokens.get('accent', [])[:3])}",
        f"- 見出しフォント: {tokens.get('head_font', '')}",
        f"- 本文フォント: {tokens.get('body_font', '')}",
        f"- 角丸: {', '.join(tokens.get('radius', [])[:2])}",
        f"- 影: {', '.join(tokens.get('shadow', [])[:1]) or 'ほぼ無し'}",
        f"- 中央寄せ枠の幅: {tokens.get('container_max', '') or '不明'}",
    ]
    return "\n".join(lines)
