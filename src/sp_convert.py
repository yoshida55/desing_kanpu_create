"""スマホ版のおおよそ自動変換（カンプHTML → SP用 @media を注入した1ファイル）。

ねらい：PC幅で作ったカンプを、スマホ375pxで「おおよそ崩れない」叩き台に一発変換する。
AIは使わない（無料・一瞬・ブレない）。完璧なSPデザインではなく、コーダー/自分が仕上げる前の
たたき台を作るのが目的（CLAUDE.md「SPはコーダー裁量／叩き台」の方針どおり）。

やり方：
  ① Playwrightで375px幅で開いて実測（respcheck.py と同じ流儀）。
  ② 崩れの犯人を機械検出して、その要素だけに効く一意セレクタ＋直しルールを作る：
     - 横並び（flex row / 複数列grid で幅広コンテナ）→ 縦積みに落とす
     - 画面外へはみ出し／画面幅より広い固定幅 → 幅を100%に収める
     - 特大フォント（40px超）→ スマホで収まるサイズへ縮小
     - 横paddingが厚い（40px超）→ 16pxへ詰める
  ③ 生成した `@media (max-width:767px){…!important…}` を、元カンプの </head> 直前に
     <style data-ce-sp> として差し込んだHTMLを sp/<元名>_sp.html に書き出す。
     元ファイルは一切いじらない（＝いつでも元に戻れる）。CSSを足すだけなので
     保険スクリプトの焼き込み等も起きない。viewport metaが無ければ一緒に補う。
"""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from . import config
from .utils import get_logger

log = get_logger("sp_convert")

# 書き出し先（camps直下に置くと履歴一覧に混ざるので分ける・specs/checks と同じ考え方）
SP_DIR = config.CAMP_DIR / "sp"

# スマホの実測幅。@media のしきい値もこの下限に合わせる
SP_WIDTH, SP_HEIGHT = 375, 812
SP_BREAKPOINT = 767  # @media (max-width:767px)

# アニメ発火と保険スクリプト(2.2〜2.5秒)を待つ時間（respcheck と同じ）
_SETTLE_MS = 2_800

# スマホで許すフォント上限（これを超える見出しは縮める）
_MAX_FONT = 40

# 375px幅でDOMを走査し、崩れの犯人ごとに {sel, kinds} を返すJS。
# sel は nth-of-type チェーンの一意パス（保険UIが body末尾に増えても既存要素はズレない）。
_SCAN_JS = r"""
() => {
  const vw = window.innerWidth;
  const out = [];
  const seen = new Map();

  // 一意セレクタ（先頭からの nth-of-type チェーン）
  const uniq = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && el.tagName !== 'HTML' && el.tagName !== 'BODY') {
      const p = el.parentElement;
      if (!p) break;
      const tag = el.tagName.toLowerCase();
      const same = [...p.children].filter(c => c.tagName === el.tagName);
      parts.unshift(tag + ':nth-of-type(' + (same.indexOf(el) + 1) + ')');
      el = p;
    }
    return 'body ' + parts.join('>');
  };
  const vis = (el, s, r) => {
    if (r.width < 6 || r.height < 6) return false;
    if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) < 0.05) return false;
    return true;
  };
  const add = (el, kind) => {
    // ツールのUIや保険要素は対象外
    if (el.closest('#__ce') || el.closest('#__ce_cm') || (el.id || '').startsWith('__ce')) return;
    const sel = uniq(el);
    if (!seen.has(sel)) { seen.set(sel, new Set()); out.push({ sel, el }); }
    seen.get(sel).add(kind);
  };

  const all = [...document.querySelectorAll('body *')];
  for (const el of all) {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (!vis(el, s, r)) continue;
    if (s.position === 'fixed' || s.position === 'sticky') continue;

    // ① 横並び（幅広コンテナのみ・小さなボタン群やナビは触らない）
    const kids = [...el.children].filter(c => {
      const cs = getComputedStyle(c), cr = c.getBoundingClientRect();
      return vis(c, cs, cr);
    });
    const wide = r.width > vw * 0.55;
    if (wide && kids.length >= 2) {
      if (s.display === 'flex' && (s.flexDirection || '').startsWith('row')) {
        // 子が実際に横に並んでいる（先頭2つの縦位置が近い）
        const a = kids[0].getBoundingClientRect(), b = kids[1].getBoundingClientRect();
        if (Math.abs(a.top - b.top) < Math.min(a.height, b.height) * 0.8) add(el, 'stackFlex');
      } else if (s.display === 'grid') {
        const cols = (s.gridTemplateColumns || '').trim().split(/\s+/).filter(Boolean);
        if (cols.length >= 2) add(el, 'stackGrid');
      } else if (s.display.includes('inline') && kids.length >= 3) {
        // inline-block 横並びカードなど
        const a = kids[0].getBoundingClientRect(), b = kids[1].getBoundingClientRect();
        if (Math.abs(a.top - b.top) < Math.min(a.height, b.height) * 0.8) add(el, 'stackInline');
      }
    }

    // ② 画面外へはみ出し／画面幅より広い（横スクロールの犯人）
    //    親も犯人なら子は後で親優先に潰れるが、まず全部拾う
    if (r.right > vw + 4 || r.width > vw + 4 || r.left < -4) {
      // ページ全体を覆う背景(section等)で幅=vw+わずかは無視
      if (r.width > vw + 4) add(el, 'overWide');
      else add(el, 'overRight');
    }

    // ③ 特大フォント（直に文字を持つ要素だけ）
    const hasText = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length >= 2);
    if (hasText) {
      const fs = parseFloat(s.fontSize);
      if (fs && fs > __MAXF__) add(el, 'bigFont|' + Math.round(fs));
    }

    // ④ 横paddingが厚い
    const pl = parseFloat(s.paddingLeft) || 0, pr = parseFloat(s.paddingRight) || 0;
    if (pl > 40 || pr > 40) add(el, 'pad');
  }

  // el は返せないので kinds を配列化して返す
  return out.map(o => ({ sel: o.sel, kinds: [...seen.get(o.sel)] }));
}
""".replace("__MAXF__", str(_MAX_FONT))


def _rules_for(kinds: list[str]) -> list[str]:
    """検出した崩れ種別 → CSS宣言（!importantは呼び出し側で付ける）。"""
    decls: list[str] = []
    kset = set(k.split("|")[0] for k in kinds)

    if "stackFlex" in kset:
        decls += ["flex-direction:column", "align-items:stretch"]
    if "stackGrid" in kset:
        decls.append("grid-template-columns:1fr")
    if "stackInline" in kset:
        decls.append("display:block")
    if "overWide" in kset or "overRight" in kset:
        # 幅を画面内へ収める。max-widthとwidth両方を緩める
        decls += ["max-width:100%", "width:auto", "overflow-wrap:break-word"]
    if "pad" in kset:
        decls += ["padding-left:16px", "padding-right:16px"]

    # 特大フォントは実測値から縮小率を決める（40→大きいほど強く縮める・下限22px）
    for k in kinds:
        if k.startswith("bigFont|"):
            try:
                fs = int(k.split("|")[1])
            except (ValueError, IndexError):
                continue
            target = max(22, min(_MAX_FONT, round(fs * 0.62)))
            # clampで画面幅にも少し追従（最小 target、推奨 7vw、最大 元の8割）
            decls.append(f"font-size:clamp({target}px,7vw,{round(fs*0.8)}px)")
            decls.append("line-height:1.35")
            break
    return decls


def _build_css(items: list[dict]) -> tuple[str, int]:
    """検出結果 → @media ブロック文字列と、実際に直した箇所数。"""
    # 子孫が同じ overWide 犯人のとき、親だけ直せば足りることが多いが、
    # セレクタ単位で素直に全部当てる（!importantなので副作用は限定的）。
    body_rules = []
    for it in items:
        decls = _rules_for(it["kinds"])
        if not decls:
            continue
        css = ";".join(d + " !important" for d in decls)
        body_rules.append(f"{it['sel']}{{{css}}}")

    fixes = len(body_rules)
    head = (
        "/* スマホ版おおよそ変換（AI不使用・自動生成）。"
        "元カンプは無傷。ここを消せば元に戻ります */\n"
        f"@media (max-width:{SP_BREAKPOINT}px){{\n"
        "  html,body{overflow-x:hidden !important;max-width:100vw !important}\n"
        "  img,picture,video{max-width:100% !important;height:auto !important}\n"
    )
    block = head + "  " + "\n  ".join(body_rules) + "\n}\n"
    return block, fixes


_VIEWPORT_RE = re.compile(r"<meta[^>]+name=[\"']viewport[\"']", re.I)
_HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.I)
_BODY_OPEN_RE = re.compile(r"<body\b", re.I)


def _inject(src_html: str, css_block: str) -> str:
    """元HTMLの </head> 直前に <style data-ce-sp> を差し込む。viewport metaが無ければ補う。"""
    inject = ""
    if not _VIEWPORT_RE.search(src_html):
        inject += '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    inject += '<style data-ce-sp>\n' + css_block + "</style>\n"

    m = _HEAD_CLOSE_RE.search(src_html)
    if m:
        return src_html[: m.start()] + inject + src_html[m.start() :]
    m = _BODY_OPEN_RE.search(src_html)
    if m:
        return src_html[: m.start()] + "<head>" + inject + "</head>\n" + src_html[m.start() :]
    return inject + src_html


def run_convert(filename: str) -> dict:
    """カンプを375px幅で実測してSP用CSSを注入した新ファイルを作る。

    戻り値: {file, fixes}（file=書き出したSP版のファイル名, fixes=直した箇所数）。
    """
    src = config.CAMP_DIR / filename
    if not src.exists():
        raise FileNotFoundError(f"カンプが見つかりません: {filename}")
    SP_DIR.mkdir(parents=True, exist_ok=True)
    cfg = config.CONFIG.capture

    log.info("スマホ版変換を開始: %s", filename)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": SP_WIDTH, "height": SP_HEIGHT})
            page = context.new_page()
            page.set_default_navigation_timeout(cfg.nav_timeout_ms)
            try:
                page.goto(src.resolve().as_uri(), wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=cfg.networkidle_timeout_ms)
                except PWTimeout:
                    pass  # ダミー写真が遅くても進める
                page.evaluate(
                    """async () => {
                        const h = document.documentElement.scrollHeight;
                        for (let y = 0; y < h; y += 700) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 60)); }
                    }"""
                )
                page.wait_for_timeout(_SETTLE_MS)  # 保険スクリプトの強制表示・lazyloadを待つ
                page.evaluate("() => window.scrollTo(0, 0)")
                items = page.evaluate(_SCAN_JS)
            finally:
                context.close()
        finally:
            browser.close()

    css_block, fixes = _build_css(items or [])
    out_html = _inject(src.read_text(encoding="utf-8"), css_block)
    out = SP_DIR / (src.stem + "_sp.html")
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text(out_html, encoding="utf-8")
    tmp.replace(out)
    log.info("スマホ版を書き出し: %s（%d箇所を調整）", out.name, fixes)
    return {"file": out.name, "fixes": fixes}
