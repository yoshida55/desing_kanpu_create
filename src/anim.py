"""
アニメーションの抜き出し（Feature：登録サイトから再利用できるアニメ素材を集める）。

ねらい（仕様 Phase 4 / mix & match）：
登録HPを"部品"として、Aの見た目にBのアニメを混ぜて別業種の叩き台を作りたい。
その「Bのアニメ材料」を、あとでカンプ生成プロンプトに渡せる形で集めておく。

ライブページ（Playwright）から集める対象：
- CSS `@keyframes` ルール（名前＋CSS本文）… コード片としてそのまま再利用できる
- `transition` の指定（実際に使われている値をユニークに集める）
- 実際に使われている `animation`（どの keyframes を何秒で回しているか）
- Lottie JSON（<lottie-player src> や bodymovin の読込先）→ 実ファイルをDLして保存

割り切り（CLAUDE.md 9章どおり）：
- GSAP 等の JS 系アニメは抜けない → 録画動画＋「GSAP使用」タグ＋言葉で参照する
  （このモジュールは CSS と Lottie だけを対象にする）。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from . import config
from .utils import get_logger, normalize_url, url_to_id

log = get_logger("anim")

# ページ内の CSS アニメ（keyframes / transition / animation / Lottie）を集めるJS。
# document.styleSheets を舐めて @keyframes を取り出す（同一オリジンなら cssRules が読める）。
_COLLECT_JS = r"""
() => {
  const abs = (u) => { try { return new URL(u, location.href).href; } catch (e) { return null; } };
  const keyframes = [];   // {name, css}
  const seenKf = new Set();

  // 1) スタイルシートから @keyframes を丸ごと取り出す
  for (const sheet of document.styleSheets) {
    let rules;
    try { rules = sheet.cssRules; } catch (e) { continue; } // CORSで読めないシートは飛ばす
    if (!rules) continue;
    for (const rule of rules) {
      // CSSKeyframesRule（type 7）
      if (rule.type === 7 || (rule.name && rule.cssText && rule.cssText.indexOf('@keyframes') === 0)) {
        const name = rule.name;
        if (!name || seenKf.has(name)) continue;
        seenKf.add(name);
        keyframes.push({ name, css: rule.cssText });
        if (keyframes.length >= 60) break;
      }
    }
    if (keyframes.length >= 60) break;
  }

  // 2) 実際に使われている transition / animation を要素から集める（ユニーク化）
  const transitions = new Set();
  const animations = new Set();  // "name dur ..." の形
  const els = [...document.querySelectorAll('body *')].slice(0, 4000);
  for (const el of els) {
    const s = getComputedStyle(el);
    const tp = s.transitionProperty;
    if (tp && tp !== 'all' && tp !== 'none') {
      const t = `${s.transitionProperty} ${s.transitionDuration} ${s.transitionTimingFunction}`.trim();
      if (s.transitionDuration && s.transitionDuration !== '0s') transitions.add(t);
    }
    const an = s.animationName;
    if (an && an !== 'none') {
      const a = `${s.animationName} ${s.animationDuration} ${s.animationTimingFunction} ${s.animationIterationCount}`.trim();
      animations.add(a);
    }
  }

  // 3) Lottie の読込先（<lottie-player src> / data-src / bodymovin の path）
  const lottie = new Set();
  document.querySelectorAll('lottie-player, dotlottie-player, [data-animation-path], [data-lottie]').forEach((el) => {
    const cand = el.getAttribute('src') || el.getAttribute('data-src')
      || el.getAttribute('data-animation-path') || el.getAttribute('data-lottie');
    const a = abs(cand);
    if (a) lottie.add(a);
  });
  // ページのHTML中に出てくる *.json（lottie/animation を含むパス）も拾う
  const html = document.documentElement.outerHTML;
  const re = /["'(]([^"'()\s]+\.json)(?:\?[^"')]*)?["')]/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const low = m[1].toLowerCase();
    if (low.includes('lottie') || low.includes('animation') || low.includes('anim')) {
      const a = abs(m[1]);
      if (a) lottie.add(a);
    }
  }

  return {
    keyframes,
    transitions: [...transitions].slice(0, 40),
    animations: [...animations].slice(0, 40),
    lottie: [...lottie].slice(0, 12),
  };
}
"""


def anim_dir(site_id: str) -> Path:
    return config.ANIM_DIR / site_id


def list_lottie(site_id: str) -> list[str]:
    """保存済みの Lottie JSON の相対パス一覧（名前順）。"""
    d = anim_dir(site_id)
    if not d.exists():
        return []
    files = sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() == ".json")
    return [str(f.relative_to(config.PROJECT_ROOT)) for f in files]


def extract_animations(url: str) -> dict:
    """ライブページから CSS アニメ素材と Lottie JSON を抜き出す。

    返り値の dict（DBの animation_snippets にそのまま入れる想定）:
      { keyframes: [{name, css}], transitions: [...], animations: [...],
        lottie_saved: [相対パス...], lottie_urls: [元URL...] }
    """
    cfg = config.CONFIG.capture
    norm_url = normalize_url(url)
    site_id = url_to_id(norm_url)
    out_dir = anim_dir(site_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("アニメの抜き出し開始: %s", norm_url)
    collected: dict = {"keyframes": [], "transitions": [], "animations": [], "lottie": []}
    lottie_saved: list[str] = []
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
            # スクロールで遅延読み込みのCSS/アニメ要素も発火させる
            try:
                page.evaluate(
                    "async()=>{await new Promise(r=>{let y=0;const t=setInterval(()=>{window.scrollBy(0,window.innerHeight);y+=window.innerHeight;if(y>=document.body.scrollHeight){clearInterval(t);r();}},120);});}"
                )
                page.wait_for_timeout(500)
            except Exception:  # noqa: BLE001
                pass
            collected = page.evaluate(_COLLECT_JS) or collected
        except Exception as exc:  # noqa: BLE001
            log.error("ページを開けず: %s (%s)", norm_url, exc)

        # Lottie JSON をDL（ブラウザのセッションで取る＝保護されていても取れることがある）
        seen_hash = set()
        for u in (collected.get("lottie") or []):
            if len(lottie_saved) >= 12:
                break
            try:
                resp = context.request.get(u, timeout=15000)
                if not resp.ok:
                    continue
                data = resp.body()
                if len(data) < 200:  # 空・エラーページは捨てる
                    continue
                # 本当にLottie JSONっぽいか軽く検査（v と layers を持つ）
                try:
                    obj = json.loads(data.decode("utf-8", "ignore"))
                    if not (isinstance(obj, dict) and ("layers" in obj or "v" in obj)):
                        continue
                except Exception:  # noqa: BLE001
                    continue
                h = hashlib.sha1(data).hexdigest()
                if h in seen_hash:
                    continue
                seen_hash.add(h)
                idx = len(lottie_saved)
                (out_dir / f"lottie_{idx:02d}.json").write_bytes(data)
                lottie_saved.append(str((out_dir / f"lottie_{idx:02d}.json").relative_to(config.PROJECT_ROOT)))
            except Exception:  # noqa: BLE001
                continue

        context.close()
        browser.close()

    result = {
        "keyframes": collected.get("keyframes", []),
        "transitions": collected.get("transitions", []),
        "animations": collected.get("animations", []),
        "lottie_saved": lottie_saved,
        "lottie_urls": collected.get("lottie", []),
    }
    log.info(
        "アニメの抜き出し完了: keyframes %d / transition %d / animation %d / lottie %d",
        len(result["keyframes"]), len(result["transitions"]),
        len(result["animations"]), len(lottie_saved),
    )
    return result


def anim_to_prompt(snippets: dict) -> str:
    """抜き出したアニメ素材を、カンプ生成でAIに渡す簡潔な指定文に整える。"""
    if not snippets:
        return ""
    lines: list[str] = []
    kf = snippets.get("keyframes") or []
    if kf:
        names = ", ".join(k.get("name", "") for k in kf[:8] if k.get("name"))
        lines.append(f"- 使える @keyframes: {names}")
        # 代表を数個そのまま渡す（AIがコピーして使えるように）
        for k in kf[:4]:
            css = (k.get("css") or "").replace("\n", " ")
            lines.append(f"  {css[:400]}")
    if snippets.get("transitions"):
        lines.append(f"- よく使う transition: {'; '.join(snippets['transitions'][:6])}")
    if snippets.get("animations"):
        lines.append(f"- 実際の animation 指定: {'; '.join(snippets['animations'][:6])}")
    if snippets.get("lottie_saved"):
        lines.append(f"- Lottie 素材: {len(snippets['lottie_saved'])} 個あり（差し込み用）")
    return "\n".join(lines)
