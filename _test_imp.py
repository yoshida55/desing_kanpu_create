# -*- coding: utf-8 -*-
"""一括改善ファイル(imp-char等の自前アニメあり)で、複数アニメが保存→リロード後も残るか検証。"""
import sys, shutil
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5057"
CAMPS = Path("data/camps")
SRC = CAMPS / "camp_20260703_024614_improved.html"
DST = CAMPS / "camp_zztest_imp.html"

def apply_anim(page, sel, ak):
    el = page.query_selector(sel)
    if not el:
        return f"要素が見つからない: {sel}"
    el.scroll_into_view_if_needed()
    page.wait_for_timeout(150)
    page.click(sel, button="right")
    page.wait_for_selector("#__ce_cm", timeout=5000)
    btn = f'#__fx_grid button[data-ak="{ak}"]'
    page.click(btn)
    page.wait_for_selector("#__fx_apply", timeout=5000)
    page.click("#__fx_apply")
    page.wait_for_timeout(300)
    page.evaluate("() => { var m=document.getElementById('__ce_cm'); if(m) m.remove(); }")
    page.wait_for_timeout(150)
    return None

def check(page, sel, want_cls, want_ch=False):
    el = page.query_selector(sel)
    if el:
        el.scroll_into_view_if_needed()
        page.wait_for_timeout(400)
    return page.evaluate(
        """(a) => {
          var el = document.querySelector(a.sel);
          if(!el) return {ok:false, why:'要素なし'};
          var cl = el.className || '';
          var has_dir = cl.indexOf(a.want_cls) >= 0;
          var op = parseFloat(getComputedStyle(el).opacity);
          var revealed = el.classList.contains('fxa_in') || op >= 0.99;
          var nch = el.querySelectorAll('.fxa_ch').length;
          var nimp = el.querySelectorAll('.imp-char').length;
          var ch_ok = a.want_ch ? nch > 0 : true;
          // 文字化けチェック：literalな &nbsp; が出ていないか
          var lit = (el.textContent||'').indexOf('&nbsp;') >= 0;
          return {ok: has_dir && revealed && ch_ok && !lit,
                  dir_ok: has_dir, revealed: revealed, opacity: op, fxa_ch: nch, imp_char: nimp, garbled: lit};
        }""",
        {"sel": sel, "want_cls": want_cls, "want_ch": want_ch},
    )

def run():
    shutil.copyfile(SRC, DST)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errs = []
        page.on("pageerror", lambda e: errs.append("PAGEERROR: " + str(e)))
        page.goto(f"{BASE}/camp/camp_zztest_imp.html", wait_until="load")
        page.wait_for_selector("#__ce", timeout=8000)
        page.wait_for_timeout(800)

        targets = [
            ('h2[aria-label="「るあん」のご紹介"]', "fxa_tw", "typewriter", True),
            ("img.imp-photo", "fxa_bl", "blur", False),
            ("p.imp-body", "fxa_y", "fadeup", False),
        ]
        applied = []
        for sel, cls, ak, ch in targets:
            err = apply_anim(page, sel, ak)
            applied.append((sel, cls, ch, err))

        page.click("#__ce_save")
        page.wait_for_timeout(2500)
        page.wait_for_selector("#__ce", timeout=8000)
        page.wait_for_timeout(1500)

        results = []
        for sel, cls, ch, err in applied:
            if err:
                results.append((sel, {"ok": False, "why": err}))
            else:
                results.append((sel, check(page, sel, cls, ch)))
        browser.close()

    print("=== 検証結果（一括改善ファイル / リロード後）===")
    all_ok = True
    for sel, r in results:
        ok = r.get("ok")
        all_ok = all_ok and bool(ok)
        print(("  ✅ " if ok else "  ❌ ") + sel[:40] + " -> " + str(r))
    if errs:
        print("JSエラー:", errs[:5])
    print("ALL_OK" if all_ok else "SOME_FAILED")
    return all_ok

if __name__ == "__main__":
    sys.exit(0 if run() else 1)
