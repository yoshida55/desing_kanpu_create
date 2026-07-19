"""レスポンシブ自動監査（カンプHTML → 3画面幅の実測レポート）。

ねらい：「画面を小さくすると崩れる」を保存前に機械検出する。
スマホ375px／タブレット768px／PC1440pxの3幅でPlaywrightで開き、
  ① 横スクロール発生（ページ全体が画面幅より広い）
  ② 画面右へはみ出している要素（犯人の特定つき）
  ③ 文字同士の重なり（読めない事故）
  ④ 小さすぎる文字（10px未満）
を実測して1枚のHTMLレポートに出す。AIは使わない（無料・一瞬・ブレない）。
レポートには「AIに直させる指示文」も自動で用意する＝✍自分で指示にコピペで直せる。

spec.py（仕様書）と同じ流儀：file://で開く→下までスクロールしてlazyload・アニメ発火→
保険スクリプトの強制表示を待ってから測る。
"""

from __future__ import annotations

import base64
import html as _html
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from . import config
from .utils import get_logger

log = get_logger("respcheck")

# レポートの置き場（camps直下に置くと履歴一覧に混ざるので分ける・specsと同じ考え方）
CHECK_DIR = config.CAMP_DIR / "checks"

# 検査する画面幅（幅, 高さ, 表示名）
WIDTHS = [(375, 812, "スマホ"), (768, 1024, "タブレット"), (1440, 900, "PC")]

# アニメ発火と保険スクリプト(2.2〜2.5秒)を待つ時間
_SETTLE_MS = 2_800

# 1幅ぶんの検査JS。座標は全部 getBoundingClientRect の実測。
_CHECK_JS = r"""
() => {
  const vw = window.innerWidth;
  const doc = document.documentElement;
  const trim = (t, n) => (t || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const selOf = (el) => {
    let s = el.tagName.toLowerCase();
    let cls = el.className;
    if (cls && cls.baseVal !== undefined) cls = cls.baseVal;   // SVG対策
    const cs = String(cls || '').trim().split(/\s+/)
      .filter(c => c && !/^(fxa|__ce|reveal|in$|show$)/.test(c)).slice(0, 2);
    if (el.id) s += '#' + el.id; else if (cs.length) s += '.' + cs.join('.');
    return s;
  };
  // 所属セクション（トップレベル<section>の順番。1始まりで表示に合わせる）
  const secs = [...document.querySelectorAll('section')].filter(s => !s.parentElement.closest('section'));
  const secOf = (el) => {
    let s = el.closest('section');
    if (!s) return el.closest('header') ? 'ヘッダー' : (el.closest('footer') ? 'フッター' : 'ページ直下');
    while (s.parentElement.closest('section')) s = s.parentElement.closest('section');
    const i = secs.indexOf(s);
    return i >= 0 ? ('セクション' + (i + 1)) : 'セクション';
  };
  const visible = (el, s, r) => {
    if (r.width < 8 || r.height < 8) return false;
    if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) < 0.05) return false;
    return true;
  };

  const all = [...document.querySelectorAll('body *')].filter(el => !el.closest('#__ce') && !el.closest('#__ce_cm'));

  // ① 横スクロール量
  const overflowPx = Math.max(0, doc.scrollWidth - vw);

  // ② 右はみ出し要素（親も犯人なら親だけ報告＝一番外側の犯人に絞る）
  const offSet = new Set();
  const offenders = [];
  for (const el of all) {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    if (!visible(el, s, r)) continue;
    if (s.position === 'fixed' || s.position === 'sticky') continue;
    if (r.right > vw + 4 || r.left < -4) offSet.add(el);
  }
  for (const el of offSet) {
    if (el.parentElement && offSet.has(el.parentElement)) continue;  // 外側だけ
    const r = el.getBoundingClientRect();
    offenders.push({ sel: selOf(el), sec: secOf(el), text: trim(el.textContent, 20),
      over: Math.round(Math.max(r.right - vw, -r.left)), w: Math.round(r.width) });
    if (offenders.length >= 12) break;
  }

  // ④ 小さすぎる文字（直に文字を持つ要素で10px未満）
  const tiny = [];
  for (const el of all) {
    const hasText = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length >= 4);
    if (!hasText) continue;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (!visible(el, s, r)) continue;
    const fs = parseFloat(s.fontSize);
    if (fs && fs < 10) {
      tiny.push({ sel: selOf(el), sec: secOf(el), text: trim(el.textContent, 20), fs: Math.round(fs * 10) / 10 });
      if (tiny.length >= 10) break;
    }
  }

  // ③ 文字同士の重なり（読めない事故）。誤検知を抑えるため：
  //    直に文字を持つ要素同士／親子関係でない／fixed系でない／小さい方の45%以上が重なる、だけ報告
  const texts = [];
  for (const el of all) {
    const hasText = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length >= 4);
    if (!hasText) continue;
    const s = getComputedStyle(el);
    if (s.position === 'fixed' || s.position === 'sticky') continue;
    const r = el.getBoundingClientRect();
    if (!visible(el, s, r)) continue;
    texts.push({ el, r });
    if (texts.length >= 400) break;   // 巨大ページの計算量ガード
  }
  const overlaps = [];
  outer:
  for (let i = 0; i < texts.length; i++) {
    for (let j = i + 1; j < texts.length; j++) {
      const A = texts[i], B = texts[j];
      if (A.el.contains(B.el) || B.el.contains(A.el)) continue;
      const x = Math.min(A.r.right, B.r.right) - Math.max(A.r.left, B.r.left);
      const y = Math.min(A.r.bottom, B.r.bottom) - Math.max(A.r.top, B.r.top);
      if (x <= 0 || y <= 0) continue;
      const inter = x * y;
      const small = Math.min(A.r.width * A.r.height, B.r.width * B.r.height);
      if (small <= 0 || inter / small < 0.45) continue;
      overlaps.push({ a: selOf(A.el) + '「' + trim(A.el.textContent, 12) + '」',
                      b: selOf(B.el) + '「' + trim(B.el.textContent, 12) + '」',
                      sec: secOf(A.el) });
      if (overlaps.length >= 8) break outer;
    }
  }

  return { vw, scrollW: doc.scrollWidth, overflowPx, offenders, tiny, overlaps };
}
"""


def _esc(t) -> str:
    return _html.escape(str(t or ""))


def _issues_of(d: dict) -> list[str]:
    """1幅ぶんの検査結果を人間の言葉の1行issueリストにする。"""
    out = []
    if d["overflowPx"] > 4:
        out.append(f"ページ全体が画面幅より {d['overflowPx']}px 広い（横スクロールが発生）")
    for o in d["offenders"]:
        t = f"「{o['text']}」" if o.get("text") else ""
        out.append(f"{o['sec']}: {o['sel']}{t} が画面の外へ {o['over']}px はみ出し（幅{o['w']}px）")
    for o in d["overlaps"]:
        out.append(f"{o['sec']}: 文字の重なり {o['a']} × {o['b']}")
    for o in d["tiny"]:
        out.append(f"{o['sec']}: {o['sel']}「{o['text']}」の文字が {o['fs']}px と小さすぎる")
    return out


def _ai_instruction(results: list[dict]) -> str:
    """全幅の結果から「AIに直させる指示文」を組み立てる（✍自分で指示へコピペ用）。"""
    lines = []
    for (w, _h, label), d in zip(WIDTHS, results):
        iss = _issues_of(d)
        if not iss:
            continue
        lines.append(f"【{label}（画面幅{w}px）で崩れている箇所の修正】")
        lines += [f"- {s}" for s in iss]
    if not lines:
        return ""
    lines.append(
        "上記を、見た目のデザイン（配色・フォント・PC幅でのレイアウト）は変えずに直してください。"
        "固定pxの幅はmax-width:100%やclamp()に、複数カラムは狭い幅で1〜2列に落とす"
        "（@media (max-width:768px) 等を追加してよい）。文章・画像は1つも消さないこと。"
    )
    return "\n".join(lines)


def run_check(filename: str) -> dict:
    """カンプを3幅で実測してレポートHTMLを作る。戻り値: {file, issues, ok_widths}。"""
    src = config.CAMP_DIR / filename
    if not src.exists():
        raise FileNotFoundError(f"カンプが見つかりません: {filename}")
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    cfg = config.CONFIG.capture

    results: list[dict] = []
    shots: list[str] = []
    log.info("レスポンシブ監査を開始: %s", filename)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for w, h, label in WIDTHS:
                context = browser.new_context(viewport={"width": w, "height": h})
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
                    page.wait_for_timeout(_SETTLE_MS)  # 保険スクリプトの強制表示を待つ
                    page.evaluate("() => window.scrollTo(0, 0)")
                    data = page.evaluate(_CHECK_JS)
                    shot = page.screenshot(full_page=True, type="jpeg", quality=55)
                finally:
                    context.close()
                results.append(data)
                shots.append(base64.b64encode(shot).decode("ascii"))
                log.info("  %spx(%s): 問題%d件", w, label, len(_issues_of(data)))
        finally:
            browser.close()

    report = _render_report(filename, results, shots)
    out = CHECK_DIR / (src.stem + "_resp.html")
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text(report, encoding="utf-8")
    tmp.replace(out)
    total = sum(len(_issues_of(d)) for d in results)
    ok_widths = sum(1 for d in results if not _issues_of(d))
    log.info("レスポンシブ監査レポート保存: %s（問題%d件）", out.name, total)
    return {"file": out.name, "issues": total, "ok_widths": ok_widths}


def _render_report(camp_file: str, results: list[dict], shots: list[str]) -> str:
    """自己完結のレポートHTML（スクショはbase64で焼き込み＝単体で開ける）。"""
    total = sum(len(_issues_of(d)) for d in results)
    head_note = ("✅ 3つの画面幅すべてで問題は見つかりませんでした" if total == 0
                 else f"⚠ 合計 {total} 件の崩れ候補が見つかりました")
    cols = []
    for (w, _h, label), d, b64 in zip(WIDTHS, results, shots):
        iss = _issues_of(d)
        badge = ("<span class='ok'>問題なし ✅</span>" if not iss
                 else f"<span class='ng'>問題 {len(iss)}件</span>")
        li = "".join(f"<li>{_esc(s)}</li>" for s in iss) or "<li class='dim'>検出なし</li>"
        cols.append(
            f"<div class='col'><h2>{w}px <small>{_esc(label)}</small> {badge}</h2>"
            f"<ul>{li}</ul>"
            f"<details {'open' if iss else ''}><summary>この幅のスクショを見る</summary>"
            f"<img src='data:image/jpeg;base64,{b64}' alt='{w}px'></details></div>"
        )
    ins = _ai_instruction(results)
    ins_block = ""
    if ins:
        ins_block = (
            "<div class='insbox'><h2>🔧 AIに直させる指示文（コピーして、カンプ画面の「✍自分で指示」へ貼る）</h2>"
            f"<textarea id='ins' readonly>{_esc(ins)}</textarea>"
            "<button onclick=\"var t=document.getElementById('ins');t.select();document.execCommand('copy');this.textContent='コピーしました ✅';\">📋 指示文をコピー</button>"
            "<p class='dim'>※文字の重なりは「候補」です。バッジを写真に重ねる等のデザイン意図の場合は、その行を消してから使ってください。</p></div>"
        )
    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<title>レスポンシブ検査: {_esc(camp_file)}</title>
<style>
body{{font-family:'Hiragino Sans','Yu Gothic',sans-serif;margin:0;padding:24px;background:#f4f6f8;color:#223}}
h1{{font-size:20px;margin:0 0 4px}}
.note{{margin:0 0 20px;font-size:14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;align-items:start}}
.col{{background:#fff;border-radius:10px;padding:16px;box-shadow:0 2px 10px rgba(20,40,80,.08)}}
.col h2{{font-size:16px;margin:0 0 10px}}
.col small{{color:#889;font-weight:400}}
.ok{{color:#0a7d44;font-size:13px}} .ng{{color:#c22;font-size:13px}}
ul{{margin:0 0 12px;padding-left:18px;font-size:13px;line-height:1.8}}
.dim{{color:#99a}}
details summary{{cursor:pointer;font-size:13px;color:#367;margin-bottom:8px}}
details img{{width:100%;border:1px solid #dde;border-radius:6px}}
.insbox{{background:#fff;border-radius:10px;padding:16px;margin-top:20px;box-shadow:0 2px 10px rgba(20,40,80,.08)}}
.insbox h2{{font-size:15px;margin:0 0 10px}}
.insbox textarea{{width:100%;min-height:140px;box-sizing:border-box;font-size:12.5px;line-height:1.7;padding:10px;border:1px solid #cdd;border-radius:6px}}
.insbox button{{margin-top:8px;padding:8px 18px;border:0;border-radius:6px;background:#0b6bcb;color:#fff;font-size:13px;cursor:pointer}}
.insbox p{{font-size:12px;margin:8px 0 0}}
</style></head><body>
<h1>📱 レスポンシブ検査レポート</h1>
<p class="note">対象: {_esc(camp_file)} ／ {_esc(head_note)}（数値はすべてブラウザ実測・AI不使用）</p>
<div class="grid">{''.join(cols)}</div>
{ins_block}
</body></html>"""
