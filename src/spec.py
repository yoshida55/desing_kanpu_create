"""
コーディング仕様書の生成（カンプHTML → 寸法・色・フォント・動き入りの1枚HTML）。

ねらい：生成カンプを「コーディングする人にそのまま渡せるデザインカンプ」にする。
数値は想像ではなく Playwright の実測（getBoundingClientRect / getComputedStyle）＝正確。
AIは使わない（無料・一瞬・ブレない）。

仕様書HTML自体も編集できる：ツール経由（/spec/…）で開けば、表のセルを
クリックして数値やメモを書き換え→💾保存でファイルに焼き込まれる
（カンプ編集バーと同じ「ブラウザのDOMを丸ごと保存」方式）。
"""

from __future__ import annotations

import base64
import html as _html
import json
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from . import config
from .utils import get_logger

log = get_logger("spec")

# 仕様書の置き場（data/camps/specs/）。camps直下に置くと履歴一覧に混ざるので分ける
SPEC_DIR = config.CAMP_DIR / "specs"

# 仕様書内でスクショを表示する幅（px）。計測幅1440の半分＝座標変換が単純
_SHOT_W = 720

# 出現アニメ等を確実に発火・完了させるための待ち時間（保険スクリプトが2.2〜2.5秒後に
# 強制表示するので、それより長く待つ）
_SETTLE_MS = 2_800

# ページ内の全要素を実測して、セクションごとの構造データを返すJS。
# 動きの判定はクラス名（fxa_pre等）とdata-cedelay＝このツールが焼き込む印を機械で読む。
_MEASURE_JS = r"""
() => {
  const hex = (c) => {
    const m = c && c.match(/rgba?\(([^)]+)\)/);
    if (!m) return '';
    const p = m[1].split(',').map(x => parseFloat(x));
    if (p.length > 3 && p[3] === 0) return '';  // 完全透明は「なし」
    return '#' + p.slice(0, 3).map(v => Math.round(v).toString(16).padStart(2, '0')).join('');
  };
  const px = (v) => Math.round(parseFloat(v) || 0);
  const trim = (t, n) => (t || '').replace(/\s+/g, ' ').trim().slice(0, n);

  // 動きの説明を機械判定（ツールが焼き込む印を読む）
  const MOTION_MAP = {
    reveal: 'スクロールで出現',
    fxa_pre: 'スクロールで出現',
    fxa_ch: '文字が1文字ずつ出る',
    fxa_hl: 'マーカー線が左から伸びる',
    __ceax_left: '左からスライドイン',
    __ceax_right: '右からスライドイン',
    __ceax_zoom: 'ズームしながら出現',
    __ceax_pulse: 'ゆっくり明滅（ループ）',
    __ceax_float: 'ふわふわ浮遊（ループ）',
    __ceax_bounce: '跳ねる（ループ）',
  };
  const motionOf = (el, s) => {
    const parts = [];
    for (const k in MOTION_MAP) if (el.classList.contains(k)) parts.push(MOTION_MAP[k]);
    const d = el.getAttribute('data-cedelay');
    if (d) parts.push(d + 'ms遅れて再生');
    // 独自の@keyframesアニメ。実測前にアニメを止めるので、止める直前に
    // data-specanim に退避した値（名前|ループ有無）から読む
    const a = el.getAttribute('data-specanim');
    if (a && !parts.length) {
      const [nm, inf] = a.split('|');
      parts.push('CSSアニメ ' + nm + (inf ? '（ループ）' : ''));
    }
    return parts.join('／');
  };

  // 文字系の値を1つの文字列にまとめる（表のセル用）
  const fontOf = (s) => {
    const fs = px(s.fontSize);
    const lh = s.lineHeight === 'normal' ? '-' : (Math.round(parseFloat(s.lineHeight) / fs * 100) / 100);
    const ls = (s.letterSpacing === 'normal' || !s.letterSpacing) ? '' : ' / 字間' + s.letterSpacing;
    return fs + 'px / ' + s.fontWeight + ' / 行間' + lh + ls;
  };
  const boxOf = (s) => {
    const f = (v) => px(v);
    const pad = [f(s.paddingTop), f(s.paddingRight), f(s.paddingBottom), f(s.paddingLeft)];
    const mar = [f(s.marginTop), f(s.marginRight), f(s.marginBottom), f(s.marginLeft)];
    const p = pad.some(v => v) ? 'pad ' + pad.join(' ') : '';
    const m = mar.some(v => v) ? 'mar ' + mar.join(' ') : '';
    return [p, m].filter(Boolean).join(' / ') || '-';
  };
  const vis = (r) => r.width >= 10 && r.height >= 8;

  // ---- セクション（入れ子は作らない設計なので、非入れ子sectionとheader/footerを拾う）----
  let secs = [...document.querySelectorAll('section')].filter(s => !s.parentElement.closest('section'));
  for (const t of ['header', 'footer']) {
    const el = document.querySelector('body ' + t);
    if (el && !el.closest('section')) secs.push(el);
  }
  secs = secs
    .map(el => ({ el, r: el.getBoundingClientRect() }))
    .filter(o => o.r.height > 40)
    .sort((a, b) => (a.r.top - b.r.top));

  const sections = [];
  let counter = 0;
  for (const { el, r } of secs) {
    const s = getComputedStyle(el);
    const secTop = r.top + window.scrollY;
    const h = el.querySelector('h1,h2,h3');
    const name = trim(h && h.textContent, 24) || el.id || trim(String(el.className || ''), 24) || el.tagName.toLowerCase();

    // 主要要素を種類ごとに集める（多すぎると表が読めないので上限あり）
    const picked = [];
    const seen = new Set();
    const add = (cand, kind, cap) => {
      let n = 0;
      for (const c of cand) {
        if (n >= cap) break;
        if (seen.has(c)) continue;
        const cr = c.getBoundingClientRect();
        if (!vis(cr)) continue;
        seen.add(c); picked.push({ el: c, kind }); n++;
      }
    };
    add(el.querySelectorAll('h1,h2,h3,h4'), 'head', 6);
    // ボタンらしい a/button（padding があり背景か枠を持つもの）
    add([...el.querySelectorAll('a,button')].filter(c => {
      const cs = getComputedStyle(c);
      return px(cs.paddingLeft) >= 8 && (hex(cs.backgroundColor) || cs.borderStyle !== 'none');
    }), 'btn', 4);
    add(el.querySelectorAll('img'), 'img', 4);
    // 段落は長い順に3つ（短い飾り文字より本文を優先）
    add([...el.querySelectorAll('p')].sort((a, b) => b.textContent.length - a.textContent.length), 'text', 3);
    // p/見出し以外の文字（数字・ラベル・キャプション等のdiv/span）。
    // 全部載せると溢れるので「書式（サイズ×太さ×色）が違うもの」を代表1つずつ拾う
    const sigOf = (cs) => cs.fontSize + '|' + cs.fontWeight + '|' + cs.color;
    const sigSeen = new Set();
    for (const { el: pc, kind: pk } of picked) {
      if (pk === 'head' || pk === 'text' || pk === 'btn') sigSeen.add(sigOf(getComputedStyle(pc)));
    }
    let tn = 0;
    for (const c of el.querySelectorAll('div,span,li,dt,dd,th,td,figcaption,small,strong,em,time,blockquote,cite,label')) {
      if (tn >= 6) break;
      if (seen.has(c)) continue;
      if (trim(c.textContent, 4).length < 2) continue;
      // 子にブロック要素を持たない「文字のかたまり」だけ（入れ物は除く）
      if ([...c.children].some(ch => getComputedStyle(ch).display.indexOf('inline') !== 0)) continue;
      const r2 = c.getBoundingClientRect();
      if (!vis(r2)) continue;
      const sig = sigOf(getComputedStyle(c));
      if (sigSeen.has(sig)) continue;
      sigSeen.add(sig); seen.add(c); picked.push({ el: c, kind: 'text' }); tn++;
    }
    // 横並びの親（flex/grid）＝列数とgapを知りたい
    add([...el.querySelectorAll('*')].filter(c => {
      const cs = getComputedStyle(c);
      const row = (cs.display === 'flex' && cs.flexDirection.indexOf('row') === 0) || cs.display === 'grid';
      return row && c.children.length >= 2 && c.getBoundingClientRect().width > 400;
    }), 'layout', 3);
    // 面になっている箱（白カード等：背景色/影を持つ大きめの塊。全幅の帯は除く）
    const byArea = (a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return rb.width * rb.height - ra.width * ra.height;
    };
    add([...el.querySelectorAll('div,article,figure,aside')].filter(c => {
      const cs = getComputedStyle(c);
      const r2 = c.getBoundingClientRect();
      const face = (hex(cs.backgroundColor) && hex(cs.backgroundColor) !== hex(s.backgroundColor)) || cs.boxShadow !== 'none';
      return face && r2.width * r2.height > 60000 && r2.width < r.width - 20;
    }).sort(byArea), 'box', 3);
    // 装飾（svg・背景画像・absoluteの飾り）。大きい順に5つ・入れ子は親だけ代表で載せる
    const decoPicked = [];
    const decoCand = [...el.querySelectorAll('*')].filter(c => {
      if (c.ownerSVGElement) return false;  // svgの中の部品は親svgで代表
      const cs = getComputedStyle(c);
      const r2 = c.getBoundingClientRect();
      if (!vis(r2) || r2.width * r2.height < 400) return false;
      if (r2.width * r2.height > r.width * r.height * 0.9) return false;  // 背景そのものは除く
      const art = c.tagName.toLowerCase() === 'svg'
        || (cs.backgroundImage && cs.backgroundImage !== 'none' && c.tagName.toLowerCase() !== 'img');
      const abs = cs.position === 'absolute' && trim(c.textContent, 3) === '';
      return art || abs;
    }).sort(byArea);
    for (const c of decoCand) {
      if (decoPicked.length >= 5) break;
      if (seen.has(c)) continue;
      if (decoPicked.some(p => p.contains(c) || c.contains(p))) continue;
      seen.add(c); picked.push({ el: c, kind: 'deco' }); decoPicked.push(c);
    }

    const withEl = [];
    for (const { el: c, kind } of picked) {
      const cr = c.getBoundingClientRect();
      const cs = getComputedStyle(c);
      counter++;
      const it = {
        n: counter, kind, tag: c.tagName.toLowerCase(),
        cls: trim(String(c.className || ''), 40),
        text: kind === 'img' ? trim(c.getAttribute('alt'), 20) : trim(c.textContent, 26),
        x: Math.round(cr.left), y: Math.round(cr.top + window.scrollY - secTop),
        w: Math.round(cr.width), h: Math.round(cr.height),
        font: ['img', 'layout', 'box', 'deco'].indexOf(kind) >= 0 ? '' : fontOf(cs),
        color: hex(cs.color), bg: hex(cs.backgroundColor),
        radius: cs.borderRadius !== '0px' ? cs.borderRadius : '',
        box: boxOf(cs),
        motion: motionOf(c, cs),
      };
      if (kind === 'deco') {
        // クラス名だけでは伝わらないので、見た目から日本語の説明を組み立てる
        const bits = [];
        const round = parseFloat(cs.borderRadius) || 0;
        if (Math.abs(cr.width - cr.height) < 6 && (String(cs.borderRadius).indexOf('50%') >= 0 || round >= cr.width / 2)) bits.push('円形');
        else if (round > 0) bits.push('角丸' + cs.borderRadius);
        if (cs.filter && cs.filter.indexOf('blur') >= 0) bits.push('ぼかし');
        if (cs.backgroundImage.indexOf('gradient') >= 0) bits.push('グラデーション');
        else if (cs.backgroundImage.indexOf('url(') >= 0) bits.push('画像入り');
        const op = parseFloat(cs.opacity);
        if (op < 0.95) bits.push('半透明' + Math.round(op * 100) + '%');
        if (c.tagName.toLowerCase() === 'svg') bits.push('SVGイラスト');
        it.text = (bits.length ? bits.join('・') + 'の飾り' : '飾り') + (it.cls ? '（' + it.cls + '）' : '');
      }
      if (kind === 'layout') {
        const ch = c.children[0];
        const chw = ch ? Math.round(ch.getBoundingClientRect().width) : 0;
        it.text = c.children.length + '列並び' + (chw ? '（子1個の幅 ' + chw + 'px）' : '');
        it.box = 'gap ' + (cs.gap && cs.gap !== 'normal' ? cs.gap : '0px');
      }
      withEl.push({ it, el: c });
    }
    // 「どの並びの中の要素か」を紐づける（gapは親の並び行に載るので、子から辿れるように）
    const lays = withEl.filter(o => o.it.kind === 'layout')
      .sort((a, b) => {
        const ra = a.el.getBoundingClientRect(), rb = b.el.getBoundingClientRect();
        return ra.width * ra.height - rb.width * rb.height;  // 小さい順＝一番内側の並びを優先
      });
    for (const o of withEl) {
      if (o.it.kind === 'layout') continue;
      const hit = lays.find(L => L.el !== o.el && L.el.contains(o.el));
      if (hit) o.it.parent = hit.it.n;
    }
    const items = withEl.map(o => o.it);
    items.sort((a, b) => a.y - b.y || a.x - b.x);

    sections.push({
      name, tag: el.tagName.toLowerCase(),
      y: Math.round(secTop), w: Math.round(r.width), h: Math.round(r.height),
      bg: hex(s.backgroundColor),
      pad: px(s.paddingTop) + 'px / ' + px(s.paddingBottom) + 'px',
      items,
    });
  }

  // ---- ページ全体の情報 ----
  let containerMax = 0;
  const bgTally = {};
  for (const el of [...document.querySelectorAll('body *')].slice(0, 4000)) {
    const cs = getComputedStyle(el);
    if (cs.maxWidth && cs.maxWidth.endsWith('px')) {
      const w = parseFloat(cs.maxWidth);
      // ページ幅と同じ値は「全幅」なのでコンテナ扱いしない（中央寄せ枠だけ拾う）
      if (w > containerMax && w >= 480 && w < document.documentElement.clientWidth - 8) containerMax = w;
    }
    const b = hex(cs.backgroundColor);
    const r2 = el.getBoundingClientRect();
    if (b && r2.width * r2.height > 5000) bgTally[b] = (bgTally[b] || 0) + Math.round(r2.width * r2.height / 1000);
  }
  const palette = Object.entries(bgTally).sort((a, b) => b[1] - a[1]).slice(0, 8).map(x => x[0]);
  const hEl = document.querySelector('h1,h2');
  return {
    page_w: Math.round(document.documentElement.getBoundingClientRect().width),
    page_h: Math.round(document.documentElement.scrollHeight),
    container: containerMax ? Math.round(containerMax) : 0,
    body_font: getComputedStyle(document.body).fontFamily.split(',')[0].replace(/["']/g, ''),
    head_font: (hEl ? getComputedStyle(hEl).fontFamily : '').split(',')[0].replace(/["']/g, ''),
    palette,
    sections,
  };
}
"""

# スクショ前に全要素を「最終の見た目」で止めるJS（出現待ちの透明・ループの途中を無くす）
_FREEZE_JS = r"""
() => {
  // 止める前に、動いているアニメの名前を data-specanim に退避（実測JSが動き列に使う）
  for (const el of document.querySelectorAll('body *')) {
    const s = getComputedStyle(el);
    if (s.animationName && s.animationName !== 'none') {
      const inf = s.animationIterationCount.indexOf('infinite') >= 0 ? '1' : '';
      el.setAttribute('data-specanim', s.animationName.split(',')[0] + '|' + inf);
    }
  }
  const st = document.createElement('style');
  st.textContent = '*{animation:none !important;transition:none !important}'
    + '.fxa_hl{--hlw:100 !important}';
  document.head.appendChild(st);
  // 透明のまま残った要素（出現アニメ不発など）を強制表示
  for (const el of document.querySelectorAll('body *')) {
    const s = getComputedStyle(el);
    if (parseFloat(s.opacity) < 0.05) {
      el.style.setProperty('opacity', '1', 'important');
      el.style.setProperty('transform', 'none', 'important');
      el.style.setProperty('visibility', 'visible', 'important');
    }
  }
}
"""


def build_spec(filename: str) -> dict:
    """カンプHTMLを実測して仕様書HTMLを生成する。戻り値: {file, sections, items}。"""
    src = config.CAMP_DIR / filename
    if not src.exists():
        raise FileNotFoundError(f"カンプが見つかりません: {filename}")
    SPEC_DIR.mkdir(parents=True, exist_ok=True)
    cfg = config.CONFIG.capture

    log.info("仕様書の実測を開始: %s", filename)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": cfg.viewport_w, "height": cfg.viewport_h})
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.nav_timeout_ms)
        try:
            page.goto(src.resolve().as_uri(), wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=cfg.networkidle_timeout_ms)
            except PWTimeout:
                pass  # ダミー写真(外部URL)が遅くても実測は進める
            # 下までゆっくりスクロール＝lazyloadと出現アニメを発火させる
            page.evaluate(
                """async () => {
                    const h = document.documentElement.scrollHeight;
                    for (let y = 0; y < h; y += 700) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 90)); }
                }"""
            )
            page.wait_for_timeout(_SETTLE_MS)  # 保険スクリプト(2.2〜2.5秒)の強制表示を待つ
            page.evaluate("() => window.scrollTo(0, 0)")
            page.evaluate(_FREEZE_JS)
            page.wait_for_timeout(300)
            data = page.evaluate(_MEASURE_JS)
            shot = page.screenshot(full_page=True, type="jpeg", quality=80)
        finally:
            context.close()
            browser.close()

    title = _read_title(src) or filename
    spec_name = src.stem + "_spec.html"
    html = _render_spec(filename, title, data, base64.b64encode(shot).decode("ascii"))
    # 一時ファイル→置き換え（書き込み途中で落ちても壊れない）
    out = SPEC_DIR / spec_name
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(out)
    items = sum(len(s["items"]) for s in data["sections"])
    log.info("仕様書を保存: %s（%dセクション・%d要素）", spec_name, len(data["sections"]), items)
    return {"file": spec_name, "sections": len(data["sections"]), "items": items}


def _read_title(path: Path) -> str:
    """カンプの<title>を拾う（仕様書の見出し用）。"""
    import re
    head = path.read_text(encoding="utf-8", errors="ignore")[:2000]
    m = re.search(r"<title>(.*?)</title>", head, flags=re.IGNORECASE | re.DOTALL)
    return " ".join(m.group(1).split()).strip()[:60] if m else ""


def _esc(t: str) -> str:
    return _html.escape(str(t or ""))


def _chip(color: str) -> str:
    """色の値＋見本チップ（空なら「-」）。"""
    if not color:
        return "-"
    return f'<span class="spec_chip" style="background:{_esc(color)}"></span>{_esc(color)}'


def _render_spec(camp_file: str, title: str, data: dict, shot_b64: str) -> str:
    """実測データから仕様書HTML（1枚・単体で開ける）を組み立てる。"""
    scale = _SHOT_W / max(data["page_w"], 1)
    kind_label = {"head": "見出し", "text": "本文", "btn": "ボタン", "img": "画像",
                  "layout": "並び", "box": "箱", "deco": "装飾"}

    sec_blocks = []
    for i, sec in enumerate(data["sections"], 1):
        pins = []
        rows = []
        for it in sec["items"]:
            px_, py_ = round(it["x"] * scale), round(it["y"] * scale)
            pins.append(
                f'<a class="spec_pin" href="#row{it["n"]}" style="left:{px_}px;top:{py_}px" title="{_esc(it["text"])}">{it["n"]}</a>'
            )
            label = f'{kind_label.get(it["kind"], it["kind"])} {_esc(it["tag"])}'
            name = _esc(it["text"]) or _esc(it["cls"])
            if it.get("parent"):
                name += f'<br>↳ <a href="#row{it["parent"]}">#{it["parent"]}</a> の並びの中'
            rows.append(
                f'<tr id="row{it["n"]}">'
                f'<td class="spec_num">{it["n"]}</td>'
                f'<td>{label}<br><span class="spec_name">{name}</span></td>'
                f'<td contenteditable>{it["w"]}×{it["h"]}</td>'
                f'<td contenteditable>{_esc(it["font"]) or "-"}</td>'
                # 文字を持たない行（画像・並び・箱・装飾）は文字色を出さない（背景色だけ）
                f'<td>{(_chip(it["color"]) + "<br>" + (_chip(it["bg"]) if it["bg"] else "")) if it["font"] else _chip(it["bg"])}</td>'
                f'<td contenteditable>{_esc(it["box"])}{("<br>角丸 " + _esc(it["radius"])) if it["radius"] else ""}</td>'
                f'<td contenteditable>{_esc(it["motion"]) or "なし"}</td>'
                f'<td contenteditable class="spec_memo"></td>'
                f"</tr>"
            )
        crop_h = max(round(sec["h"] * scale), 40)
        pos_y = round(sec["y"] * scale)
        sec_blocks.append(f"""
<section class="spec_section">
  <h2 class="spec_section_title">{i:02d} {_esc(sec['name'])}
    <span class="spec_size">{sec['w']}×{sec['h']}px ／ 背景 {_esc(sec['bg']) or 'なし'} ／ 上下pad {_esc(sec['pad'])}</span>
  </h2>
  <div class="spec_row">
    <div class="spec_shot" style="height:{crop_h}px;background-position:0 -{pos_y}px">{''.join(pins)}</div>
    <div class="spec_detail">
      <table class="spec_table">
        <thead><tr><th>#</th><th>要素</th><th>サイズ(px)</th><th>文字</th><th>色/背景</th><th>余白・角丸</th><th>動き</th><th>メモ</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      <button class="spec_addrow" type="button">＋ 行を追加（自由に書けます）</button>
    </div>
  </div>
</section>""")

    palette = "".join(
        f'<span class="spec_swatch" title="{_esc(c)}" style="background:{_esc(c)}"></span><code>{_esc(c)}</code> '
        for c in data["palette"]
    )
    made = time.strftime("%Y-%m-%d %H:%M")
    file_json = json.dumps(camp_file.replace(".html", "_spec.html"))

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>仕様書 - {_esc(title)}</title>
<style>
body{{margin:0;background:#f4f5f7;color:#1d1d1f;font-family:"Hiragino Kaku Gothic ProN","Yu Gothic",Meiryo,sans-serif;font-size:13px;line-height:1.7}}
code{{font-family:Consolas,monospace}}
.spec_header{{position:sticky;top:0;z-index:10;background:#1d1d1f;color:#fff;padding:14px 24px;display:flex;align-items:center;gap:16px}}
.spec_title{{margin:0;font-size:18px}}
.spec_title span{{font-weight:400;color:#ffce8a;margin-left:10px}}
.spec_meta{{color:#aaa;font-size:12px;flex:1}}
.spec_save{{background:#1a7f37;color:#fff;border:none;border-radius:10px;padding:10px 18px;font-size:14px;font-weight:700;cursor:pointer}}
.spec_summary{{background:#fff;margin:20px 24px;padding:18px 22px;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.06)}}
.spec_summary h2{{margin:0 0 10px;font-size:15px}}
.spec_basic td{{padding:3px 16px 3px 0;vertical-align:top}}
.spec_swatch{{display:inline-block;width:16px;height:16px;border-radius:4px;border:1px solid #ccc;vertical-align:-3px;margin:0 4px 0 10px}}
.spec_note{{margin-top:10px;min-height:44px;border:1px dashed #bbb;border-radius:8px;padding:8px 12px;background:#fffdf3}}
.spec_note:empty::before{{content:"（クリックして全体メモを書けます：ブレイクポイント、共通ルールなど）";color:#999}}
.spec_section{{background:#fff;margin:20px 24px;padding:18px 22px;border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,.06)}}
.spec_section_title{{margin:0 0 12px;font-size:16px;border-left:5px solid #0b6bcb;padding-left:10px}}
.spec_size{{font-size:12px;font-weight:400;color:#666;margin-left:12px}}
.spec_row{{display:flex;gap:18px;align-items:flex-start}} /* 左スクショ・右表の2カラム */
.spec_shot{{position:relative;flex:none;width:{_SHOT_W}px;background-image:url(data:image/jpeg;base64,{shot_b64});background-size:{_SHOT_W}px auto;background-repeat:no-repeat;border:1px solid #ddd;border-radius:6px;overflow:hidden}}
.spec_pin{{position:absolute;width:20px;height:20px;line-height:20px;text-align:center;background:#e5484d;color:#fff;font-size:11px;font-weight:700;border-radius:50%;box-shadow:0 2px 6px rgba(0,0,0,.35);text-decoration:none;transform:translate(-4px,-4px)}}
.spec_pin:hover{{background:#0b6bcb}}
.spec_detail{{flex:1;min-width:0;overflow-x:auto}}
.spec_table{{border-collapse:collapse;width:100%;font-size:12px}}
.spec_table th{{background:#eef2f7;border:1px solid #d8dee8;padding:6px 8px;white-space:nowrap}}
.spec_table td{{border:1px solid #d8dee8;padding:6px 8px;vertical-align:top}}
.spec_table td[contenteditable]{{cursor:text}}
.spec_table td[contenteditable]:hover{{background:#f5faff}}
.spec_table td[contenteditable]:focus{{outline:2px solid #0b6bcb;background:#fff}}
.spec_table tr:target{{background:#fff3d6}} /* ピンをクリックした行を光らせる */
.spec_num{{font-weight:700;color:#e5484d;text-align:center}}
.spec_name{{color:#666}}
.spec_memo{{min-width:110px;background:#fffdf3}}
.spec_chip{{display:inline-block;width:12px;height:12px;border-radius:3px;border:1px solid #ccc;vertical-align:-1px;margin-right:5px}}
.spec_addrow{{margin-top:8px;background:#eef2f7;color:#1d1d1f;border:1px dashed #9db3cc;border-radius:8px;padding:6px 14px;font-size:12px;cursor:pointer}}
.spec_addrow:hover{{background:#e0eaf6}}
.spec_delrow{{color:#e5484d;cursor:pointer;font-weight:700;padding:0 4px}}
.spec_delrow:hover{{background:#fde2e4;border-radius:4px}}
@media print{{.spec_save{{display:none}}.spec_header{{position:static}}.spec_section,.spec_summary{{box-shadow:none;margin:10px 0}}}}
</style>
</head>
<body>
<header class="spec_header">
  <h1 class="spec_title">📐 コーディング仕様書<span>{_esc(title)}</span></h1>
  <div class="spec_meta">元カンプ: {_esc(camp_file)} ／ 計測幅 {data['page_w']}px ／ 作成 {made}（数値はブラウザ実測）</div>
  <button class="spec_save" id="spec_save">💾 変更を保存</button>
</header>

<section class="spec_summary">
  <h2>🧾 基本情報</h2>
  <table class="spec_basic">
    <tr><td>ページ幅（計測時）</td><td contenteditable>{data['page_w']}px（全高 {data['page_h']}px）</td></tr>
    <tr><td>コンテナ幅（中央寄せ枠）</td><td contenteditable>{(str(data['container']) + 'px') if data['container'] else '不明（実測できず）'}</td></tr>
    <tr><td>見出しフォント</td><td contenteditable>{_esc(data['head_font'])}</td></tr>
    <tr><td>本文フォント</td><td contenteditable>{_esc(data['body_font'])}</td></tr>
    <tr><td>主な背景色</td><td>{palette}</td></tr>
  </table>
  <div class="spec_note" contenteditable></div>
</section>
{''.join(sec_blocks)}
<script>
(function(){{
  var FILE={file_json};
  // ＋行を追加（手書きの補足用）／追加した行は#欄の×で消せる。保存でファイルに残る
  document.addEventListener('click',function(e){{
    var add=e.target.closest('.spec_addrow');
    if(add){{
      var tbody=add.closest('.spec_detail').querySelector('tbody');
      var tr=document.createElement('tr');
      var tds='<td class="spec_num"><span class="spec_delrow" title="この行を削除">×</span></td>';
      for(var i=0;i<7;i++) tds+='<td contenteditable'+(i===6?' class="spec_memo"':'')+'></td>';
      tr.innerHTML=tds;
      tbody.appendChild(tr);
      tr.cells[1].focus();
      return;
    }}
    var del=e.target.closest('.spec_delrow');
    if(del && confirm('この行を削除しますか？')) del.closest('tr').remove();
  }});
  var btn=document.getElementById('spec_save');
  btn.addEventListener('click',function(){{
    if(location.protocol==='file:'){{
      alert('保存はツール経由で開いたときに使えます。\\n起動.bat でツールを立ち上げ、カンプの編集バーから仕様書を開き直してください。');
      return;
    }}
    btn.disabled=true;btn.textContent='保存中…';
    var htmlText='<!doctype html>\\n'+document.documentElement.outerHTML;
    fetch('/api/save_spec_html',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{file:FILE,html:htmlText}})}})
      .then(function(r){{return r.json();}})
      .then(function(d){{btn.disabled=false;btn.textContent=d.ok?'✅ 保存しました':'保存失敗: '+(d.message||'');setTimeout(function(){{btn.textContent='💾 変更を保存';}},2500);}})
      .catch(function(){{btn.disabled=false;btn.textContent='通信エラー';}});
  }});
}})();
</script>
</body>
</html>
"""
