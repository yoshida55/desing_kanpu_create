"""検証：移動を個別プロパティ translate で当て、アニメは transform で当てる → 両立するか？
   さらに fxa_in の transform:none!important が translate(移動)を消さないか？"""
import json
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:5000/camp/camp_20260704_124945_improved.html"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.goto(URL, wait_until="networkidle")
    pg.wait_for_timeout(1000)

    res = pg.evaluate(
        """(sel)=>{
      const el=document.querySelector(sel); if(!el) return {err:'no el'};
      const info=(e)=>{const r=e.getBoundingClientRect();const cs=getComputedStyle(e);
        return {x:Math.round(r.x),y:Math.round(r.y),op:cs.opacity,tf:cs.transform.slice(0,50),tr:cs.translate};};
      // fxaの肝ルール（transformを触る）を注入＋fxa-on
      const st=document.createElement('style'); st.textContent=
        'html.fxa-on .fxa_pre{opacity:0;transition:opacity .8s,transform .8s}'+
        'html.fxa-on .fxa_pre.fxa_xl{transform:translateX(-48px)}'+
        'html.fxa-on .fxa_pre.fxa_in{opacity:1!important;transform:none!important}';
      document.head.appendChild(st); document.documentElement.classList.add('fxa-on');
      const out={before:info(el)};
      // ① 移動を個別プロパティ translate で当てる（transformは使わない）
      el.style.setProperty('translate','40px 40px','important');
      out.afterMove=info(el);   // x が +40 になっていれば移動OK
      // ② 出現アニメ(transform)を当てる：付与直後(fxa_pre.fxa_xl 状態=transformあり)
      el.classList.add('fxa_pre','fxa_xl');
      out.entering=info(el);    // transformに translateX(-48) が入りつつ、translateの移動も生きているか
      // ③ 出現完了(fxa_in=transform:none!important)
      el.classList.add('fxa_in');
      out.done=info(el);        // transform:none でも translate(移動)が残っていれば成功
      return out;
    }""",
        ".person",
    )
    print(json.dumps(res, ensure_ascii=False, indent=1))
    b.close()
