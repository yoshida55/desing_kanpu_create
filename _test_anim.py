# -*- coding: utf-8 -*-
"""複数アニメの焼き込みをブラウザで自動検証する（右クリック→選ぶ→付ける→保存→リロード→残ってるか）。"""
import sys, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5057"

def apply_anim(page, sel, ak):
    """要素selを右クリック→アニメak選択→この動きを付ける。"""
    page.click(sel, button="right")
    page.wait_for_selector("#__ce_cm", timeout=5000)
    btn = f'#__fx_grid button[data-ak="{ak}"]'
    page.wait_for_selector(btn, timeout=5000)
    page.click(btn)
    page.wait_for_selector("#__fx_apply", timeout=5000)
    page.click("#__fx_apply")
    page.wait_for_timeout(300)
    # メニューを閉じる（次の要素と重ならないように）
    page.evaluate("() => { var m=document.getElementById('__ce_cm'); if(m) m.remove(); }")
    page.wait_for_timeout(150)

def check(page, sel, want_cls, want_ch=False):
    return page.evaluate(
        """(a) => {
          var el = document.querySelector(a.sel);
          if(!el) return {ok:false, why:'要素なし'};
          var cl = el.className || '';
          var has_pre = /\\bfxa_pre\\b/.test(cl) || /fxa_lp_/.test(cl) || /\\bfxa_wave\\b/.test(cl);
          var has_dir = cl.indexOf(a.want_cls) >= 0;
          var op = parseFloat(getComputedStyle(el).opacity);
          var revealed = el.classList.contains('fxa_in') || op === 1;
          var nch = el.querySelectorAll('.fxa_ch').length;
          var ch_ok = a.want_ch ? nch > 0 : true;
          return {ok: has_pre && has_dir && revealed && ch_ok,
                  cls: cl, dir: a.want_cls, has_dir: has_dir, revealed: revealed, opacity: op, nch: nch};
        }""",
        {"sel": sel, "want_cls": want_cls, "want_ch": want_ch},
    )

def run():
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append("PAGEERROR: " + str(e)))

        page.goto(f"{BASE}/camp/camp_zztest_clean.html", wait_until="load")
        page.wait_for_selector("#__ce", timeout=8000)
        page.wait_for_timeout(500)

        # 3要素にアニメを付ける：見出し=一文字ずつ / 段落=ふわっと出現 / 画像=ズームイン
        apply_anim(page, "#t-head", "stagger")
        apply_anim(page, "#t-para", "fadeup")
        apply_anim(page, "#t-img", "zoom")

        # 保存（saveLayout→約600msでlocation.reload）
        page.click("#__ce_save")
        page.wait_for_timeout(2500)
        page.wait_for_selector("#__ce", timeout=8000)  # リロード後、編集バー再注入を待つ
        page.wait_for_timeout(1500)                     # fxaShowが走る猶予

        results.append(("見出し 一文字ずつ", check(page, "#t-head", "fxa_cpre", want_ch=True)))
        results.append(("段落 ふわっと出現", check(page, "#t-para", "fxa_y")))
        results.append(("画像 ズームイン", check(page, "#t-img", "fxa_s")))

        browser.close()

    print("=== 検証結果（クリーンなカンプ / リロード後）===")
    all_ok = True
    for name, r in results:
        ok = r.get("ok")
        all_ok = all_ok and ok
        print(("  ✅ " if ok else "  ❌ ") + name + " -> " + str(r))
    print("ALL_OK" if all_ok else "SOME_FAILED")
    return all_ok

if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
