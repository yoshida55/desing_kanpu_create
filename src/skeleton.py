"""セクションの「骨格」を実測して型（レイアウトの種類）に正規化する。

なぜ実測（Playwright）なのか
---------------------------
クローンのDOMは他社マークアップ＋WordPress＋ツールの継ぎ足しなので、
**HTMLの入れ子構造と見た目の構造が一致しない**（CLAUDE.md「レイヤーツリーを作らないと決めた」の理由と同じ）。
実測（ノード1024個の77%がクラス名なし）。だから静的パース（BeautifulSoup等）で
「左に画像・右にテキスト」を判定するのは不可能で、必ず座標を測る必要がある。

出す骨格
--------
imgpos : 画像の位置（none / bg / left / right / top / bottom / grid）
cols   : 列数（1 / 2 / 3＝3列以上）
txt    : 文字量（none / sm / md / lg）
band   : 高さの帯（band=帯 / half=半画面 / full=1画面 / tall=それ以上）

型ID = "imgpos|colN|txtX" （bandは並べ替え用に持つが型IDには入れない＝細かく割れすぎるため）
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from . import config

# 撮影条件はツール全体で固定（CLAUDE.md「撮影条件は固定」）＝骨格の判定もブレさせない
VIEWPORT_W = 1440
VIEWPORT_H = 900
SETTLE_MS = 1800          # 保険スクリプトの強制表示を待つ
NAV_TIMEOUT_MS = 25000

OUT_DIR = config.DATA_DIR / "skeleton"
THUMB_DIR = OUT_DIR / "thumbs"
INDEX_PATH = OUT_DIR / "index.json"

# ── セクションの数え方（サーバ側とブラウザ側で必ず同じものを使う）──────────
# ★ここが2箇所に分かれて書かれていると、片方だけ直した時に番号が1個ずれて
#   「選んだのと違うセクションが入れ替わる」という直しにくいバグになる。
#   viewer.py は %SECS_JS% としてこの文字列をそのまま編集バーに埋め込む。
SECS_JS = r"""
window.__ceSkelUtil = (function(){
  const rectOf = (el) => {
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.left + window.scrollX), y: Math.round(r.top + window.scrollY),
             w: Math.round(r.width), h: Math.round(r.height) };
  };
  const vis = (el) => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    if (parseFloat(s.opacity || '1') < 0.05) return false;
    return true;
  };
  // 透明な親を遡って「実際に見えている地の色」を出す
  const bgOf = (el) => {
    let n = el;
    for (let i = 0; i < 12 && n; i++) {
      const c = getComputedStyle(n).backgroundColor;
      if (c && c !== 'transparent' && !/rgba\(\s*0,\s*0,\s*0,\s*0\s*\)/.test(c)) return c;
      n = n.parentElement;
    }
    return 'rgb(255,255,255)';
  };
  const bgImgUrl = (el) => {
    const b = getComputedStyle(el).backgroundImage || '';
    const m = b.match(/url\((['"]?)(.*?)\1\)/);
    return m ? m[2] : '';
  };
  return { rectOf, vis, bgOf, bgImgUrl };
})();

// セクション（＝1つの塊）の一覧を、上から順に返す。番号はこの並び順。
window.__ceSkelSecs = function(){
  const U = window.__ceSkelUtil;
  const VW = document.documentElement.clientWidth;
  // まずは意味タグ。少なすぎるクローン（section が0個）は「画面幅級の塊」で拾い直す。
  let secs = Array.from(document.querySelectorAll('section, header, footer, article'))
    .filter(el => U.vis(el) && !el.querySelector('section'));
  secs = secs.filter(el => { const r = U.rectOf(el); return r.w >= VW * 0.6 && r.h >= 120; });

  if (secs.length < 3) {
    const cand = [];
    for (const el of document.body.querySelectorAll('*')) {
      if (!U.vis(el)) continue;
      const r = U.rectOf(el);
      if (r.w < VW * 0.8 || r.h < 200) continue;
      if (el.closest('[id^="__ce"]')) continue;   // ツールのUIは除く
      cand.push({ el, r });
    }
    // 外側優先で、既に採った塊に含まれるものは捨てる（＝重複した入れ子を1つに）
    cand.sort((a, b) => (b.r.w * b.r.h) - (a.r.w * a.r.h));
    const picked = [];
    for (const c of cand) {
      if (picked.some(p => p.el.contains(c.el) || c.el.contains(p.el))) continue;
      picked.push(c);
    }
    secs = picked.sort((a, b) => a.r.y - b.r.y).map(c => c.el);
  }
  return secs.filter(el => !el.closest('[id^="__ce"]'));
};

// 要素から「それが入っているセクションの番号」を出す（画面側が使う）
window.__ceSkelIndexOf = function(el){
  const secs = window.__ceSkelSecs();
  for (let i = 0; i < secs.length; i++) {
    if (secs[i] === el || secs[i].contains(el)) return i;
  }
  return -1;
};
"""

# ── 骨格を測るJS（1回のevaluateで全部返す＝往復を減らす） ────────────────
_MEASURE_JS = "() => {\n" + SECS_JS + r"""
  const U = window.__ceSkelUtil;
  const rectOf = U.rectOf, vis = U.vis, bgOf = U.bgOf, bgImgUrl = U.bgImgUrl;
  const VW = document.documentElement.clientWidth;
  const secs = window.__ceSkelSecs();

  // ── セクションごとに中身を測る ──────────────────────
  const out = [];
  for (let i = 0; i < secs.length && i < 40; i++) {
    const sec = secs[i];
    const R = rectOf(sec);
    if (R.h < 80 || R.w < VW * 0.4) continue;

    const imgs = [], texts = [];
    const walk = sec.querySelectorAll('*');
    let nodes = 0;

    for (const el of walk) {
      if (nodes++ > 2500) break;                 // 巨大セクションの保険
      if (el.closest('[id^="__ce"]')) continue;
      if (!vis(el)) continue;
      const r = rectOf(el);
      if (r.w < 8 || r.h < 8) continue;

      // 画像（<img> と 背景画像の両方を同じ土俵で見る＝素人目に区別がつかないため）
      if (el.tagName === 'IMG') {
        if (r.w >= 40 && r.h >= 40) imgs.push({ ...r, kind: 'img', src: el.getAttribute('src') || '' });
        continue;
      }
      const bu = bgImgUrl(el);
      if (bu && r.w >= 80 && r.h >= 80) {
        imgs.push({ ...r, kind: 'bg', src: bu });
      }

      // 文字（自分が直接持っているテキストだけ数える＝親で二重に数えない）
      let own = '';
      for (const n of el.childNodes) {
        if (n.nodeType === 3) own += n.nodeValue;
      }
      own = own.replace(/\s+/g, ' ').trim();
      if (own.length >= 2) {
        const s = getComputedStyle(el);
        texts.push({ ...r, len: own.length,
                     fs: Math.round(parseFloat(s.fontSize) || 16),
                     color: s.color,
                     head: /^H[1-6]$/.test(el.tagName) });
      }
    }

    out.push({
      idx: i,
      tag: sec.tagName.toLowerCase(),
      cls: (sec.getAttribute('class') || '').slice(0, 120),
      rect: R,
      bg: bgOf(sec),
      imgs: imgs.slice(0, 60),
      texts: texts.slice(0, 200),
      vw: VW,
    });
  }
  return { vw: VW, docH: document.documentElement.scrollHeight, sections: out };
}
"""

# 出現アニメの取りこぼしで「測ったら全部透明だった」を防ぐ（CLAUDE.md ㉝ の型）
_FORCE_VISIBLE_CSS = """
html.op-wait *, .fxa_pre, .reveal, [data-aos] {
  opacity: 1 !important; visibility: visible !important;
  transform: none !important; translate: none !important; filter: none !important;
  animation: none !important; transition: none !important;
}
#__op_screen, [id^="__op_"] { display: none !important; }
"""


def _num(color: str) -> tuple[int, int, int]:
    """'rgb(a) 表記' → (r,g,b)。読めなければ白扱い。"""
    m = re.findall(r"[\d.]+", color or "")
    if len(m) >= 3:
        return (int(float(m[0])), int(float(m[1])), int(float(m[2])))
    return (255, 255, 255)


def _classify(sec: dict) -> dict:
    """測った生データ → 骨格（型）に正規化する。"""
    R = sec["rect"]
    W, H = max(R["w"], 1), max(R["h"], 1)
    vw = sec.get("vw") or VIEWPORT_W
    area = W * H

    imgs = sec["imgs"]
    texts = sec["texts"]

    # ── 画像の位置 ────────────────────────────────
    # 「一番大きい画像」がその区画の性格を決める（小物の飾りは無視）
    big = sorted(imgs, key=lambda a: a["w"] * a["h"], reverse=True)
    main = big[0] if big else None
    imgpos = "none"
    img_ratio = 0.0
    if main:
        img_ratio = round((main["w"] * main["h"]) / area, 3)
        cx = (main["x"] + main["w"] / 2 - R["x"]) / W
        cy = (main["y"] + main["h"] / 2 - R["y"]) / H
        wide = main["w"] >= W * 0.8

        # 面積の7割以上を覆う＝背景として敷かれている（文字はその上に乗る）
        if img_ratio >= 0.7:
            imgpos = "bg"
        elif len([a for a in big if a["w"] * a["h"] >= area * 0.02]) >= 3:
            imgpos = "grid"          # 写真が3枚以上並ぶ＝一覧・ギャラリー型
        elif wide:
            imgpos = "top" if cy < 0.5 else "bottom"
        else:
            imgpos = "left" if cx < 0.5 else "right"

    # ── 列数（中身のx中心をクラスタリング）──────────────
    items = [a for a in imgs if a["w"] * a["h"] >= area * 0.01]
    items += [t for t in texts if t["len"] >= 6]
    centers = sorted(((t["x"] + t["w"] / 2 - R["x"]) / W) for t in items) if items else []
    cols = 1
    if centers:
        groups = [[centers[0]]]
        for c in centers[1:]:
            if c - groups[-1][-1] > 0.18:      # 18%以上離れたら別の列
                groups.append([c])
            else:
                groups[-1].append(c)
        # 中身が1個しか無い列はノイズ（飾り・キャプション）として数えない
        cols = max(1, len([g for g in groups if len(g) >= 2]) or len(groups))
    cols = min(cols, 3)

    # ── 文字量 ────────────────────────────────
    chars = sum(t["len"] for t in texts)
    txt = "none" if chars < 10 else "sm" if chars < 80 else "md" if chars < 400 else "lg"

    # ── 高さの帯 ───────────────────────────────
    hr = H / VIEWPORT_H
    band = "band" if hr < 0.45 else "half" if hr < 0.8 else "full" if hr < 1.35 else "tall"

    # ── 色（並べ替え用）────────────────────────────
    bg = _num(sec["bg"])
    heads = [t for t in texts if t["head"]] or texts
    fg = _num(heads[0]["color"]) if heads else (17, 17, 17)

    type_id = f"{imgpos}|col{cols}|txt{txt}"
    return {
        "type_id": type_id,
        "imgpos": imgpos, "cols": cols, "txt": txt, "band": band,
        "img_ratio": img_ratio, "chars": chars,
        "n_img": len(imgs), "n_text": len(texts),
        "bg_rgb": list(bg), "fg_rgb": list(fg),
        "w": W, "h": H, "vw": vw,
    }


def extract_file(path: Path, shots: bool = False, page=None) -> list[dict]:
    """HTML1本からセクションの骨格一覧を出す。page を渡せばブラウザを使い回す。"""
    own = page is None
    pw = browser = None
    if own:
        pw = sync_playwright().start()
        browser = pw.chromium.launch()
        page = browser.new_context(viewport={"width": VIEWPORT_W, "height": VIEWPORT_H}).new_page()
    try:
        page.goto(path.resolve().as_uri(), wait_until="load", timeout=NAV_TIMEOUT_MS)
    except PWTimeout:
        print(f"  [warn] load timeout: {path.name}")
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] load failed: {path.name} / {e}")
        return []

    page.wait_for_timeout(SETTLE_MS)
    try:
        page.add_style_tag(content=_FORCE_VISIBLE_CSS)
        page.wait_for_timeout(250)
    except Exception:  # noqa: BLE001
        pass

    try:
        data = page.evaluate(_MEASURE_JS)
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] measure failed: {path.name} / {e}")
        return []

    shot_map = _shots_all(page, data, path) if shots else {}

    out = []
    for sec in data.get("sections", []):
        feat = _classify(sec)
        feat.update({
            "file": path.name,
            "idx": sec["idx"],
            "tag": sec["tag"],
            "cls": sec["cls"],
            "y": sec["rect"]["y"],
        })
        sh = shot_map.get(sec["idx"]) or {}
        feat["thumb"] = sh.get("thumb", "")
        if sh.get("bg_rgb"):
            feat["bg_rgb"] = sh["bg_rgb"]      # 画素から取れたほうを正とする
            feat["pal"] = sh.get("pal") or []
        out.append(feat)

    _mark_roles(out)

    if own:
        browser.close()
        pw.stop()
    return out


def _mark_roles(rows: list[dict]) -> None:
    """役割（hero / nav / footer / content）を位置から決める。

    ★形（型ID）だけでは足りない：ヒーローの候補をフッターの差し替え先に出したら使えない。
      「見た目の形」と「ページ上の役目」は別の軸なので分けて持つ。
    """
    if not rows:
        return
    rows.sort(key=lambda r: r["y"])
    for i, r in enumerate(rows):
        role = "content"
        if r["tag"] == "header" and r["h"] < 220:
            role = "nav"
        elif r["y"] < 140 and r["h"] >= 480:
            role = "hero"          # 一番上にある大きい塊＝ファーストビュー
        elif r["tag"] == "footer" or i == len(rows) - 1:
            role = "footer"
        r["role"] = role
        # 地の色が暗いか（明るい候補と暗い候補が混ざると並べた時に見づらいので持つ）
        rr, gg, bb = r["bg_rgb"]
        r["dark"] = (0.299 * rr + 0.587 * gg + 0.114 * bb) < 128


def _shots_all(page, data: dict, path: Path) -> dict:
    """ページ全体を1枚だけ撮って、セクションごとに切り出す（候補パネルのサムネ用）。

    ★セクションごとに page.screenshot(clip=...) を呼ぶと、1枚ずつフルページを描き直すので
      とても遅い。1枚撮ってPillowで切るほうが圧倒的に速い。
    ★clip だけで full_page を付けないと「画面に映っている範囲」しか撮れず、
      スクロールしないと見えないセクションが全部失敗する（実測で8個中1個しか撮れなかった）。
    """
    from PIL import Image

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_DIR / "_full.png"
    try:
        page.screenshot(path=str(tmp), full_page=True)
        im = Image.open(tmp).convert("RGB")
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] full shot failed: {path.name} / {e}")
        return {}

    got = {}
    for sec in data.get("sections", []):
        R = sec["rect"]
        x0, y0 = max(R["x"], 0), max(R["y"], 0)
        x1 = min(x0 + min(R["w"], sec["vw"]), im.width)
        y1 = min(y0 + min(R["h"], 1600), im.height)   # 極端に長い塊は上だけ
        if x1 - x0 < 40 or y1 - y0 < 30:
            continue
        name = f"{path.stem}__{sec['idx']:02d}.jpg"
        try:
            crop = im.crop((x0, y0, x1, y1))
            crop.thumbnail((640, 900))                # 一覧用に軽くする
            crop.save(THUMB_DIR / name, "JPEG", quality=78)
            got[sec["idx"]] = {"thumb": name, **_colors_of(crop)}
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] crop failed: {name} / {e}")
    im.close()
    tmp.unlink(missing_ok=True)
    return got


def _colors_of(im) -> dict:
    """実際に描かれた画素から地の色と主要色を出す。

    ★CSSの background-color を親へ遡る方式は当てにならない：
      セクション自身は透明で、中の入れ物が色を塗っていることが多い
      （実測：真っ青なヒーローが「白」と判定された）。見えている色は画素が唯一の正。
    """
    w, h = im.size
    # 地の色＝上下左右のフチをぐるっと見て一番多い色（中央は写真や文字で当てにならない）
    edge = []
    step = max(1, w // 60)
    for x in range(0, w, step):
        edge.append(im.getpixel((x, 2)))
        edge.append(im.getpixel((x, h - 3)))
    step = max(1, h // 60)
    for y in range(0, h, step):
        edge.append(im.getpixel((2, y)))
        edge.append(im.getpixel((w - 3, y)))
    # 少しの誤差（JPEGのにじみ）は同じ色として数えるので16段階に丸める
    cnt: dict = {}
    for p in edge:
        k = (p[0] // 16, p[1] // 16, p[2] // 16)
        cnt[k] = cnt.get(k, 0) + 1
    top = max(cnt.items(), key=lambda kv: kv[1])[0]
    bg = [top[0] * 16 + 8, top[1] * 16 + 8, top[2] * 16 + 8]

    # 主要色＝色数を6色に減らして多い順（並べ替えの「色が近い」に使う）
    pal = []
    try:
        q = im.convert("RGB").resize((80, 80)).quantize(colors=6, method=2)
        pl = q.getpalette() or []
        for i, c in sorted(q.getcolors() or [], reverse=True)[:4]:
            pal.append([pl[c * 3], pl[c * 3 + 1], pl[c * 3 + 2]])
    except Exception:  # noqa: BLE001
        pal = [bg]
    return {"bg_rgb": bg, "pal": pal}


def build_index(files: list[Path], shots: bool = True) -> dict:
    """複数HTMLをまとめて処理して型辞書JSONを書き出す。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    t0 = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
                                  ignore_https_errors=True)
        page = ctx.new_page()
        page.on("console", lambda m: None)        # クローン側のJSエラーは無視
        for i, f in enumerate(files, 1):
            print(f"[{i}/{len(files)}] {f.name}")
            got = extract_file(f, shots=shots, page=page)
            print(f"    → セクション {len(got)}個 / 型 {sorted({g['type_id'] for g in got})}")
            rows.extend(got)
        browser.close()

    index = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "viewport": [VIEWPORT_W, VIEWPORT_H],
        "n_files": len(files),
        "n_sections": len(rows),
        "sections": rows,
    }
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n書き出し: {INDEX_PATH}  ({len(rows)}件 / {time.time() - t0:.1f}秒)")
    return index


# ══ 候補を「部品」として取り出す（カンプへ差し込むため）═══════════════════
_LINK_RE = re.compile(r"""<link[^>]+rel\s*=\s*["']?stylesheet["']?[^>]*>""", re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*("([^"]*)"|'([^']*)')""", re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)


def _collect_css(path: Path) -> str:
    """そのHTMLが使っているCSSを1本にまとめて返す。

    ★<style>だけでなく <link> の中身も要る：クローンは見た目のほとんどを
      clone_*_files/ 配下のCSSファイルが作っているので、それが無いと素の文字列になる。
    ★file:// では document.styleSheets の cssRules が読めないことがあるので、
      ブラウザ経由ではなくディスクから読む（確実）。
    """
    try:
        html = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""
    css = []
    for m in _STYLE_RE.finditer(html):
        css.append(m.group(1))
    for m in _LINK_RE.finditer(html):
        hm = _HREF_RE.search(m.group(0))
        if not hm:
            continue
        href = (hm.group(2) or hm.group(3) or "").strip()
        if not href or href.startswith(("http://", "https://", "//", "data:")):
            continue          # 外部CSSは取り込まない（オフラインで壊れるため）
        f = (path.parent / href).resolve()
        if f.exists() and f.suffix.lower() == ".css":
            try:
                css.append(f.read_text(encoding="utf-8", errors="ignore"))
            except Exception:  # noqa: BLE001
                pass
    return "\n".join(css)


# 切り出し用：セクションのHTMLと、その「上に乗っていた入れ物」の一覧を返す。
# ★入れ物（祖先）の情報が要る：元サイトのCSSは `.wrapper .top-news{…}` のように
#   祖先を前提に書かれているものが多く、セクションだけ持ち出すとそのルールが当たらない。
#   実測：切り出したセクションが 1095px → 18814px に暴れた（横幅の指定が全部外れたため）。
_OUTER_JS = "() => {\n" + SECS_JS + r"""
  return window.__ceSkelSecs().map(s => {
    const anc = [];
    let n = s.parentElement;
    for (let i = 0; i < 6 && n && n.tagName !== 'BODY' && n.tagName !== 'HTML'; i++) {
      anc.unshift({ tag: n.tagName.toLowerCase(), cls: n.getAttribute('class') || '' });
      n = n.parentElement;
    }
    return { html: s.outerHTML, anc: anc };
  });
}"""


SCOPE_CLASS = "cepkscope"     # 持ち込んだCSSをこの中だけに閉じ込める囲い


def _wrap_with_ancestors(html: str, anc: list) -> str:
    """元サイトで上に乗っていた入れ物を <div> で復元して包む。

    タグは div に揃える（section を二重にすると相手ページのCSSに引っかかるため）が、
    クラス名はそのまま残す＝祖先を前提にしたCSSがちゃんと当たる。
    """
    inner = html
    for a in reversed(anc or []):
        cls = (a.get("cls") or "").strip()
        inner = f'<div class="{cls}" data-cepkwrap="1">{inner}</div>' if cls else inner
    # ★いちばん外に「囲い」を必ず1枚かぶせる。持ち込んだCSSを全部この中に閉じ込めるため。
    #   これが無いと、元サイトのCSSにある素のタグ指定（section{margin-top:-165px} など）が
    #   差し込んだ先のカンプのセクションにも当たる。クラス名の名前空間化だけでは防げない
    #   （renameするのはクラスだけで、タグ名は触らないため）。
    #   実測：入れ替えた次のセクションが141px食い込んだ。
    # ★display:flow-root が要る。これが無いと中身のマージン（元サイトの負のmargin-bottom等）が
    #   囲いを突き抜けて外に効き、差し込んだ次のセクションを引っぱり上げる
    #   （実測：次のセクションが141px食い込んだ。CSSを閉じ込めるだけでは直らない）。
    return (f'<div class="{SCOPE_CLASS}" data-cepkwrap="1" '
            f'style="display:flow-root;margin:0">{inner}</div>')


_REM_RE = re.compile(r"(-?\d*\.?\d+)rem\b")


def _rem_to_px(css: str, root_px: float) -> str:
    """rem を px に焼き込む。

    ★rem は「ルート(html)の文字サイズ」基準で、入れ物にいくら font-size を指定しても効かない。
      元サイトが `html{font-size:clamp(…100vw/…)}` で 1rem≒1設計px にしている作りだと、
      差し込んだ先（html=16px）では全部が16倍近くに膨らむ。
      実測：セクションの幅が 1440px のはずが 18432px、画像が 5483×3926px になった。
    ★逃げ道は「remを使わない形にする」しかない。撮影条件は1440px固定なので px に確定できる
      （このツールは高さや余白も px で焼き込む方針・CLAUDE.md ㊻）。
    """
    if not root_px or abs(root_px - 16.0) < 0.05:
        return css                      # 差し込み先と同じ基準なら触らない
    return _REM_RE.sub(lambda m: f"{float(m.group(1)) * root_px:.3f}px", css)


def extract_part(file_name: str, idx: int) -> dict:
    """候補セクションの HTML と、それを見た目どおりに出すためのCSSを取り出す。

    ★同じセクション判定JSを使い回す＝型辞書の idx と必ず一致する
      （別のやり方で数え直すと1個ずれて「違うセクションが入る」になる）。
    """
    path = config.CAMP_DIR / file_name
    if not path.exists():
        return {"ok": False, "error": "元のファイルが見つかりません"}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": VIEWPORT_W, "height": VIEWPORT_H}).new_page()
        try:
            page.goto(path.resolve().as_uri(), wait_until="load", timeout=NAV_TIMEOUT_MS)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(SETTLE_MS)
        try:
            page.add_style_tag(content=_FORCE_VISIBLE_CSS)
        except Exception:  # noqa: BLE001
            pass
        try:
            htmls = page.evaluate(_OUTER_JS)
            root_px = float(page.evaluate(
                "() => parseFloat(getComputedStyle(document.documentElement).fontSize) || 16"))
        except Exception as e:  # noqa: BLE001
            browser.close()
            return {"ok": False, "error": f"読み取りに失敗しました: {e}"}
        browser.close()

    if not (0 <= idx < len(htmls)):
        return {"ok": False, "error": "セクション番号が範囲外です"}

    got = htmls[idx]
    frag = _wrap_with_ancestors(got["html"], got.get("anc"))
    css_all = _collect_css(path)
    css = _trim_css(frag, css_all)
    css = _rem_to_px(css, root_px)
    print(f"  CSS: {len(css_all)}文字 → {len(css)}文字（入れ物 {len(got.get('anc') or [])}段 / "
          f"元のrem基準 {root_px:.2f}px）")
    return {"ok": True, "html": frag, "css": css, "src": file_name, "root_px": round(root_px, 2)}


_TRIM_JS = r"""
([frag, css, SCOPE]) => {
  document.documentElement.innerHTML = '<head></head><body><div id="__frag"></div></body>';
  const host = document.getElementById('__frag');
  host.innerHTML = frag;
  const st = document.createElement('style');
  st.textContent = css;
  document.head.appendChild(st);

  const sheet = st.sheet;
  if (!sheet) return '';
  const keep = [];
  // ★ページ全体に効くセレクタは絶対に持ち込まない。
  //   差し込んだ先のカンプの body / html / * まで塗り替えてしまい、
  //   「1つ入れ替えたらページ全部の見た目が変わった」になる。
  const GLOBAL = /^(\*|html|body|:root|:where\(html\)|:where\(body\))$/i;
  // ★ページ全体のルールは配置を壊すので入れないが、CSS変数（--色 など）だけは要る。
  //   これを捨てると var(--color-text) が全部無効になって色が落ちる。
  //   受け皿は差し込んだ部分の外側 [data-cepkwrap] ＝そこだけに効く。
  const vars = [];
  const grabVars = (style) => {
    for (const k of style) {
      if (k.startsWith('--')) vars.push(k + ':' + style.getPropertyValue(k) + ';');
    }
  };
  const hit = (sel) => {
    if (GLOBAL.test(sel.trim())) return false;
    // :hover 等の状態や ::before は querySelector に渡すと落ちるので素の形にして試す
    const plain = sel.replace(/::?(hover|focus|active|visited|before|after|first-line|placeholder|not\(|is\(|where\()/g, '');
    for (const s of [sel, plain]) {
      try { if (host.querySelector(s)) return true; } catch (_) {}
    }
    return false;
  };
  // ★元サイトの「スクロールで現れる」CSSは、隠す側(opacity:0)だけがCSSにあり、
  //   見せる側はそのサイトのJSが担当している。JSは持ってこないので、
  //   そのまま持ち込むと差し込んだ部分が永久に透明のまま＝真っ白になる（実測で踏んだ）。
  //   → 隠す指定だけ打ち消す。ズラす指定も opacity:0 とセットの時だけ戻す。
  const unhide = (style) => {
    const op = (style.getPropertyValue('opacity') || '').trim();
    const vis = (style.getPropertyValue('visibility') || '').trim();
    const hidden = (op !== '' && parseFloat(op) < 0.05) || vis === 'hidden';
    if (!hidden) return style.cssText;
    let t = style.cssText;
    t = t.replace(/opacity\s*:\s*[^;]+;?/gi, 'opacity:1;');
    t = t.replace(/visibility\s*:\s*hidden\s*;?/gi, 'visibility:visible;');
    t = t.replace(/(^|[;\s])(transform|translate)\s*:\s*[^;]+;?/gi, '$1');
    return t;
  };
  const walk = (rules, out) => {
    for (const r of rules) {
      if (r.type === 1) {                       // 普通のルール
        const sels = (r.selectorText || '').split(',').map(s => s.trim()).filter(Boolean);
        if (sels.some(s => GLOBAL.test(s.trim()))) grabVars(r.style);
        const used = sels.filter(hit);
        // ★必ず囲い（.SCOPE）の下に閉じ込める＝差し込んだ部分の外には一切効かない
        if (used.length) out.push(used.map(s => '.' + SCOPE + ' ' + s).join(',') + '{' + unhide(r.style) + '}');
      } else if (r.type === 4 || r.type === 12) {   // @media / @supports
        const inner = [];
        walk(r.cssRules || [], inner);
        if (inner.length) out.push('@media ' + (r.conditionText || r.media.mediaText) + '{' + inner.join('') + '}');
      } else if (r.type === 7) {                // @keyframes（動きの定義は丸ごと残す）
        out.push(r.cssText);
      }
      // ★@font-face は入れない：base64で埋め込まれたフォントが数十万文字あり、
      //   ここだけでCSSの大半を占める。書体は差し込んだ先のカンプのものを継ぐ。
    }
  };
  try { walk(sheet.cssRules, keep); } catch (_) { return ''; }
  if (vars.length) keep.unshift('.' + SCOPE + '{' + vars.join('') + '}');
  return keep.join('\n');
}
"""


def _trim_css(frag: str, css: str) -> str:
    """フラグメントに実際に当たるCSSルールだけを残す。

    ★クローンのCSSは1本で500KB超ある（実測 553,446文字）。丸ごと持ち込むと
      カンプが一気に重くなるうえ、相手ページの見た目まで壊す。
    ★判定はブラウザに querySelector させるのが確実（正規表現でセレクタを解釈しない）。
    """
    if not css:
        return ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context(viewport={"width": VIEWPORT_W, "height": VIEWPORT_H}).new_page()
            page.goto("about:blank")
            out = page.evaluate(_TRIM_JS, [frag, css, SCOPE_CLASS])
            browser.close()
        return out or ""
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] CSS絞り込みに失敗（全部を渡します）: {e}")
        return css


# ══ 雰囲気（SigLIP）と色で並べ替える ═══════════════════════════════════
VEC_PATH = OUT_DIR / "vecs.npy"


def add_embeddings(index: dict | None = None) -> dict:
    """サムネをSigLIPでベクトル化して vecs.npy に保存する。

    ★LLM（Claude/GPT）は使わない：候補を出すたびにAPI課金＋数秒かかると
      「次々出る」というこの機能の価値が消える。SigLIPはローカル・無料・一瞬。
    """
    import numpy as np

    from .model import DesignEmbedder

    index = index or json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    rows = index["sections"]
    emb = DesignEmbedder()
    emb.load()
    print(f"モデル: {emb.model_name} / dim={emb.dim}")

    vecs, ok = [], 0
    for i, r in enumerate(rows, 1):
        th = r.get("thumb")
        p = THUMB_DIR / th if th else None
        if p and p.exists():
            try:
                v = emb.encode_image(p)
                vecs.append(v.astype("float32"))
                r["vec"] = ok
                ok += 1
                if i % 20 == 0:
                    print(f"  {i}/{len(rows)} 埋め込み済み")
                continue
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] embed failed: {th} / {e}")
        r["vec"] = -1

    arr = np.stack(vecs) if vecs else np.zeros((0, 1), dtype="float32")
    np.save(VEC_PATH, arr)
    index["embed_model"] = emb.model_name
    index["n_vec"] = ok
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"ベクトル: {arr.shape} → {VEC_PATH}")
    return index


def _lab(rgb) -> tuple[float, float, float]:
    """sRGB → Lab。色の「近さ」は数値の引き算では測れないのでLabに直す。

    ★RGBのまま距離を取ると、人の目には全然違う色が近いと判定される
      （明るさの差だけが効きすぎる）。Labは人の見え方に合わせた座標系。
    """
    def f(u):
        u = u / 255.0
        return u / 12.92 if u <= 0.04045 else ((u + 0.055) / 1.055) ** 2.4
    r, g, b = f(rgb[0]), f(rgb[1]), f(rgb[2])
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 1.0
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def h(t):
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t + 16 / 116)
    fx, fy, fz = h(x), h(y), h(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def color_score(a: dict, b: dict) -> float:
    """0〜1。1に近いほど配色が近い。地の色と主要色の両方を見る。"""
    la, lb = _lab(a.get("bg_rgb") or [255, 255, 255]), _lab(b.get("bg_rgb") or [255, 255, 255])
    d_bg = sum((x - y) ** 2 for x, y in zip(la, lb)) ** 0.5

    # 主要色は「相手の中で一番近い色との距離」の平均＝並び順が違っても効く
    pa, pb = a.get("pal") or [], b.get("pal") or []
    if pa and pb:
        La = [_lab(c) for c in pa[:3]]
        Lb = [_lab(c) for c in pb[:3]]
        ds = [min(sum((x - y) ** 2 for x, y in zip(ca, cb)) ** 0.5 for cb in Lb) for ca in La]
        d_pal = sum(ds) / len(ds)
    else:
        d_pal = d_bg

    d = 0.6 * d_bg + 0.4 * d_pal
    return max(0.0, 1.0 - d / 100.0)     # Lab距離100でだいたい別物


_SITE_RE = re.compile(r"^clone_(.+?)_\d{8}_\d{6}", re.IGNORECASE)


def site_of(file_name: str) -> str:
    """ファイル名から元サイトを取り出す（clone_www_example_co_jp_20260714_073452_js → www_example_co_jp）。"""
    m = _SITE_RE.match(file_name or "")
    return m.group(1).lower() if m else (file_name or "")


def rank(target: dict, index: dict, vecs=None, w_mood: float = 0.7, w_color: float = 0.3,
         same_role: bool = True, exclude_file: str | None = None, limit: int = 60,
         per_site: int = 2, dup_at: float = 0.97) -> list[dict]:
    """お手本セクションに「雰囲気と色が近い順」で候補を並べる。

    w_mood  SigLIPの内積（=cosine。保存時にL2正規化済みなので内積でよい）
    w_color Lab距離から作った配色の近さ
    """
    import numpy as np

    rows = index["sections"]
    if vecs is None:
        vecs = np.load(VEC_PATH) if VEC_PATH.exists() else None

    tv = None
    ti = target.get("vec", -1)
    if vecs is not None and isinstance(ti, int) and 0 <= ti < len(vecs):
        tv = vecs[ti]

    tsite = site_of(target.get("file", ""))
    out = []
    for r in rows:
        if r is target:
            continue
        if exclude_file and r["file"] == exclude_file:
            continue
        if same_role and r.get("role") != target.get("role"):
            continue
        # ★同じサイトの別クローンを候補に出さない。
        #   実測：同じサイトを4回クローンしていたので、上位3件が全部同じ絵（雰囲気1.00）になり
        #   「いろんな形が出る」という機能の目的そのものが消えていた。
        if site_of(r.get("file", "")) == tsite:
            continue
        mood = 0.0
        vi = r.get("vec", -1)
        if tv is not None and isinstance(vi, int) and 0 <= vi < len(vecs):
            mood = float(np.dot(tv, vecs[vi]))
        if mood >= dup_at:
            continue                     # 見分けがつかないほど同じ絵は出さない
        col = color_score(target, r)
        # 型が同じものばかり並ぶと「いろんな形が出る」目的に反するので、
        # 同じ型にはわずかに減点して形が散るようにする
        same_type = 0.04 if r["type_id"] == target.get("type_id") else 0.0
        r2 = dict(r)
        r2["score"] = round(w_mood * mood + w_color * col - same_type, 4)
        r2["mood"] = round(mood, 4)
        r2["color"] = round(col, 4)
        out.append(r2)

    out.sort(key=lambda x: -x["score"])

    # 1サイトが上位を埋め尽くさないように間引く（種類を出すのがこの機能の目的）
    seen: dict = {}
    picked = []
    for r in out:
        s = site_of(r["file"])
        if seen.get(s, 0) >= per_site:
            continue
        seen[s] = seen.get(s, 0) + 1
        picked.append(r)
        if len(picked) >= limit:
            break
    return picked


# ══ 編集中のカンプ側を測る（お手本＝今いじっているセクション）═══════════
CAMP_CACHE_DIR = OUT_DIR / "_camp"


def index_camp(file_name: str) -> dict:
    """編集中のカンプ1本を測ってキャッシュする（お手本のベクトルと色を得るため）。

    ★セクションの数え方は型辞書と同じJSを通す＝画面側が送ってくる番号と必ず一致する。
      別のやり方で数え直すと1個ずれて「違うセクションのお手本で探す」になる。
    ★中身が変わっていなければ作り直さない（毎回6秒待たせない）。
    """
    import numpy as np

    path = config.CAMP_DIR / file_name
    if not path.exists():
        return {"ok": False, "error": "ファイルが見つかりません"}

    CAMP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(path.stat().st_mtime)
    cj = CAMP_CACHE_DIR / f"{path.stem}.json"
    cv = CAMP_CACHE_DIR / f"{path.stem}.npy"
    if cj.exists() and cv.exists():
        try:
            got = json.loads(cj.read_text(encoding="utf-8"))
            if got.get("stamp") == stamp:
                got["vecs"] = np.load(cv)
                got["ok"] = True
                return got
        except Exception:  # noqa: BLE001
            pass

    print(f"[skeleton] カンプを測ります: {file_name}")
    rows = extract_file(path, shots=True)
    if not rows:
        return {"ok": False, "error": "セクションが取れませんでした"}

    from .model import DesignEmbedder

    emb = DesignEmbedder()
    emb.load()
    vecs = []
    for r in rows:
        p = THUMB_DIR / r["thumb"] if r.get("thumb") else None
        if p and p.exists():
            try:
                vecs.append(emb.encode_image(p).astype("float32"))
                r["vec"] = len(vecs) - 1
                continue
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] embed failed: {r.get('thumb')} / {e}")
        r["vec"] = -1

    arr = np.stack(vecs) if vecs else np.zeros((0, 1), dtype="float32")
    np.save(cv, arr)
    cj.write_text(json.dumps({"stamp": stamp, "sections": rows}, ensure_ascii=False),
                  encoding="utf-8")
    print(f"[skeleton] {len(rows)}セクション / ベクトル {arr.shape}")
    return {"ok": True, "stamp": stamp, "sections": rows, "vecs": arr}


_LIB: dict = {}


def load_library() -> dict:
    """型辞書（クローン由来の候補）を読み込んでメモリに置く。"""
    import numpy as np

    if _LIB.get("index") is not None:
        return _LIB
    if not INDEX_PATH.exists():
        _LIB["index"] = {"sections": []}
        _LIB["vecs"] = None
        return _LIB
    _LIB["index"] = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    _LIB["vecs"] = np.load(VEC_PATH) if VEC_PATH.exists() else None
    return _LIB


def candidates(file_name: str, sec_idx: int, same_role: bool = True,
               w_mood: float = 0.7, w_color: float = 0.3, limit: int = 40) -> dict:
    """編集中カンプの sec_idx 番のセクションに似た候補を、雰囲気と色が近い順で返す。"""
    import numpy as np

    lib = load_library()
    if not lib["index"]["sections"]:
        return {"ok": False, "error": "型辞書がまだありません（tools/build_skeleton_index.py を実行してください）"}

    camp = index_camp(file_name)
    if not camp.get("ok"):
        return camp
    rows = camp["sections"]
    if not (0 <= sec_idx < len(rows)):
        return {"ok": False, "error": "セクション番号が範囲外です"}
    tgt = dict(rows[sec_idx])

    # お手本のベクトルは「カンプ側の配列」から取る。型辞書の配列とは別物なので
    # rank に渡す前にここで内積用の値へ差し替える（番号だけ渡すと別人のベクトルを引く）。
    tv = None
    if camp["vecs"] is not None and 0 <= tgt.get("vec", -1) < len(camp["vecs"]):
        tv = camp["vecs"][tgt["vec"]]

    lib_vecs = lib["vecs"]
    out = []
    tsite = site_of(tgt.get("file", ""))
    for r in lib["index"]["sections"]:
        if same_role and r.get("role") != tgt.get("role"):
            continue
        if site_of(r.get("file", "")) == tsite:
            continue
        mood = 0.0
        vi = r.get("vec", -1)
        if tv is not None and lib_vecs is not None and 0 <= vi < len(lib_vecs):
            mood = float(np.dot(tv, lib_vecs[vi]))
        if mood >= 0.97:
            continue
        col = color_score(tgt, r)
        same_type = 0.04 if r["type_id"] == tgt.get("type_id") else 0.0
        out.append({**r, "score": round(w_mood * mood + w_color * col - same_type, 4),
                    "mood": round(mood, 4), "color": round(col, 4)})

    out.sort(key=lambda x: -x["score"])
    seen: dict = {}
    picked = []
    for r in out:
        s = site_of(r["file"])
        if seen.get(s, 0) >= 2:
            continue
        # ★候補どうしが同じ絵にならないようにする。お手本との重複を弾くだけでは足りない
        #   （実測：同じサイトを2回クローンしていて、同じセクションが並んで出た）。
        if lib_vecs is not None and 0 <= r.get("vec", -1) < len(lib_vecs):
            v = lib_vecs[r["vec"]]
            dup = False
            for q in picked:
                if 0 <= q.get("vec", -1) < len(lib_vecs) and float(np.dot(v, lib_vecs[q["vec"]])) >= 0.97:
                    dup = True
                    break
            if dup:
                continue
        seen[s] = seen.get(s, 0) + 1
        picked.append(r)
        if len(picked) >= limit:
            break

    return {"ok": True,
            "target": {k: tgt.get(k) for k in ("type_id", "role", "bg_rgb", "thumb", "h", "cls", "tag")},
            "n_sections": len(rows),
            "cands": picked}


def type_summary(index: dict) -> list[tuple[str, int]]:
    """型ごとの件数（多い順）＝「型は何種類に収束したか」を見るため。"""
    cnt: dict[str, int] = {}
    for s in index.get("sections", []):
        cnt[s["type_id"]] = cnt.get(s["type_id"], 0) + 1
    return sorted(cnt.items(), key=lambda kv: -kv[1])
