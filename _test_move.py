# -*- coding: utf-8 -*-
import sys, shutil
from pathlib import Path
from playwright.sync_api import sync_playwright
BASE="http://127.0.0.1:5057"; C=Path("data/camps")
SRC=C/"camp_20260703_024614_improved.html"; DST=C/"camp_zztest_move.html"; IMG="img.imp-photo"
def rec(page, tag):
    r=page.evaluate("""(s)=>{var el=document.querySelector(s); if(!el)return null;
      var comp=getComputedStyle(el).transform;  // 実際に効いてる変形（matrix）
      var hasMove = comp.indexOf('131')>=0 || (el.style.transform||'').indexOf('-131')>=0;
      return {inline_tf:(el.style.transform||'(なし)').slice(0,45), computed:comp.slice(0,60),
              keeps_move: hasMove, cls:(el.className||'').split(' ').filter(function(c){return c.indexOf('fxa')>=0;}).join(' '),
              wrapped:(el.parentElement&&el.parentElement.classList.contains('fxa_wrap'))};}""", IMG)
    print(f"  [{tag}]", r); return r
def apply(page, ak):
    page.evaluate("""(s)=>{var el=document.querySelector(s); el.scrollIntoView({block:'center'});
      var r=el.getBoundingClientRect();
      el.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}));}""", IMG)
    page.wait_for_selector("#__ce_cm",timeout=5000)
    page.click(f'#__fx_grid button[data-ak="{ak}"]'); page.wait_for_selector("#__fx_apply",timeout=5000)
    page.click("#__fx_apply"); page.wait_for_timeout(300)
    page.evaluate("()=>{var m=document.getElementById('__ce_cm'); if(m)m.remove();}")
shutil.copyfile(SRC,DST)
with sync_playwright() as p:
    b=p.chromium.launch(headless=True); pg=b.new_page(viewport={"width":1440,"height":900})
    pg.goto(f"{BASE}/camp/camp_zztest_move.html",wait_until="load"); pg.wait_for_selector("#__ce",timeout=8000); pg.wait_for_timeout(700)
    rec(pg,"適用前(移動済み translate -131)")
    apply(pg,"pulse")  # ループアニメ(脈打つ)を移動済み画像に
    pg.click("#__ce_save"); pg.wait_for_timeout(2600); pg.wait_for_selector("#__ce",timeout=8000)
    pg.evaluate("(s)=>{var e=document.querySelector(s); if(e)e.scrollIntoView({block:'center'});}", IMG); pg.wait_for_timeout(600)
    a=rec(pg,"脈打つ付与→保存→リロード後")
print("\n移動が保たれた?:", "✅" if (a and a['keeps_move']) else "❌ 移動が消えて元位置で脈打ってる(再現!)")
