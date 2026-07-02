# -*- coding: utf-8 -*-
from playwright.sync_api import sync_playwright
BASE="http://127.0.0.1:5057"; FILE="fav_20260703_041324.html"; SEL='h2[aria-label="「るあん」のご紹介"]'
def op(page):
    return page.evaluate("""(s)=>{var h=document.querySelector(s); if(!h)return null;
      var c=h.querySelectorAll('.fxa_ch'); var o=[].slice.call(c).slice(0,4).map(function(x){return Math.round(parseFloat(getComputedStyle(x).opacity)*100)/100;});
      return {has_in:h.classList.contains('fxa_in'), first4:o, top:Math.round(h.getBoundingClientRect().top)};}""", SEL)
with sync_playwright() as p:
    b=p.chromium.launch(headless=True); pg=b.new_page(viewport={"width":1440,"height":900})
    pg.goto(f"{BASE}/camp/{FILE}", wait_until="load")
    pg.wait_for_timeout(2500)  # 時間トリガーがあれば発火する猶予
    print("① 読込2.5秒後・スクロール前（時間で出てはダメ→隠れてるのが正解）:", op(pg))
    pg.eval_on_selector(SEL, "e=>e.scrollIntoView({block:'center'})"); pg.wait_for_timeout(80)
    print("② 見出しへスクロール直後:", op(pg))
    pg.wait_for_timeout(1100)
    print("③ +1.1秒（再生完了）:", op(pg))
    b.close()
