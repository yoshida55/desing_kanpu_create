"""🎨 Figma → カンプHTML の取り込み（REST API・AIなし＝無料）。

**なぜプラグインではなくREST APIか**
このツールのカンプは元々「絶対配置＋インラインstyle」の塊なので、Figmaのノード
（x, y, w, h の絶対座標）をそのまま置ける＝一番むずかしい「いい感じのflex/gridに直す」推測が
要らない。Figma側に何も作らなくてよく、Pythonだけで完結する。

**必要なもの**
`.env` の `FIGMA_TOKEN`（Figma → Settings → Security → Personal access tokens）。
★スコープは「File content: Read-only」が必須。これが無いと 403（実測でハマった）。

**取り込みの流れ**
1. FigmaのURLから fileKey と node-id を取り出す
2. `/v1/files/{key}/nodes` で対象フレームのツリーを取る
3. 「文字」「画像・アイコン」「べた塗りの箱」に振り分ける
   - アイコンや図形（文字を含まないベクター）は **1枚のPNGにまとめて書き出す**（`/v1/images`）
   - 文字は文字のまま出す＝あとからツールの編集バーで直せる
4. カンプHTML（data/camps/camp_日時_figma.html）を書き出す

**割り切っていること（先に知っておく）**
- Auto Layout は座標に潰れる（レスポンシブにはならない）＝本番コーディングは今まで通り実測して書く
- 回転・マスク・ブレンドモードは再現しない
- フォントはFigmaのフォント名がそのまま入る（Webフォントの読み込みは別途）
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config
from .utils import get_logger

log = get_logger("figmaimport")

API = "https://api.figma.com/v1"

# 文字を含まないときは「1枚の絵」として書き出す型（アイコン・イラスト・図形）
_VECTOR_TYPES = {
    "VECTOR", "BOOLEAN_OPERATION", "STAR", "LINE", "ELLIPSE", "REGULAR_POLYGON", "POLYGON",
}
# 中に入り込んで分解する型（＝箱）
_CONTAINER_TYPES = {"FRAME", "GROUP", "COMPONENT", "COMPONENT_SET", "INSTANCE", "SECTION", "CANVAS"}


# ---------------------------------------------------------------- URL / API

def parse_figma_url(url: str) -> tuple[str, str | None]:
    """FigmaのURLから (fileKey, node_id) を取り出す。

    https://www.figma.com/design/AbCd123/名前?node-id=12-345  → ("AbCd123", "12:345")
    ※URLのnode-idは `12-345`、APIが欲しいのは `12:345`（ここを間違えると空で返る）
    """
    url = (url or "").strip()
    m = re.search(r"figma\.com/(?:file|design|proto)/([A-Za-z0-9]+)", url)
    if not m:
        raise ValueError("FigmaのURLではないようです（https://www.figma.com/design/... を貼ってください）")
    key = m.group(1)
    node = None
    q = urllib.parse.urlparse(url).query
    nid = urllib.parse.parse_qs(q).get("node-id", [None])[0]
    if nid:
        node = nid.replace("-", ":")
    return key, node


def _get(path: str, token: str, timeout: int = 60) -> dict[str, Any]:
    req = urllib.request.Request(API + path, headers={"X-Figma-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # noqa: PERF203
        body = e.read().decode("utf-8", "ignore")[:400]
        if e.code == 403:
            raise RuntimeError(
                "Figmaに拒否されました(403)。トークンのスコープに「File content: Read-only」が"
                f"付いているか確認してください。／Figmaの返答: {body}"
            ) from e
        if e.code == 404:
            raise RuntimeError("そのファイルが見つかりません(404)。URLと、自分がアクセスできるファイルかを確認してください。") from e
        raise RuntimeError(f"Figma API エラー {e.code}: {body}") from e


# ---------------------------------------------------------------- 色・数値

def _col(c: dict | None, opacity: float | None = None) -> str:
    """Figmaの色(0〜1)をCSSのrgb/rgbaにする。"""
    if not c:
        return ""
    r, g, b = (int(round((c.get(k, 0) or 0) * 255)) for k in ("r", "g", "b"))
    a = c.get("a", 1)
    if opacity is not None:
        a = a * opacity
    if a >= 0.999:
        return f"rgb({r},{g},{b})"
    return f"rgba({r},{g},{b},{round(a, 3)})"


def _solid_fill(node: dict) -> str:
    """最初の「見えているベタ塗り」を返す（無ければ空）。"""
    for f in node.get("fills") or []:
        if f.get("visible") is False:
            continue
        if f.get("type") == "SOLID":
            return _col(f.get("color"), f.get("opacity"))
        if str(f.get("type", "")).startswith("GRADIENT"):
            stops = f.get("gradientStops") or []
            if stops:
                cs = ", ".join(_col(s.get("color")) for s in stops)
                return f"linear-gradient(180deg, {cs})"
    return ""


def _has_image_fill(node: dict) -> bool:
    return any(
        f.get("type") == "IMAGE" and f.get("visible") is not False
        for f in (node.get("fills") or [])
    )


def _shadow(node: dict) -> str:
    out = []
    for e in node.get("effects") or []:
        if e.get("visible") is False:
            continue
        if e.get("type") in ("DROP_SHADOW", "INNER_SHADOW"):
            o = e.get("offset") or {}
            inset = "inset " if e["type"] == "INNER_SHADOW" else ""
            out.append(
                f"{inset}{round(o.get('x', 0))}px {round(o.get('y', 0))}px "
                f"{round(e.get('radius', 0))}px {_col(e.get('color'))}"
            )
    return ", ".join(out)


def _radius(node: dict) -> str:
    rr = node.get("rectangleCornerRadii")
    if rr and any(rr):
        return " ".join(f"{round(v)}px" for v in rr)
    r = node.get("cornerRadius")
    return f"{round(r)}px" if r else ""


# ---------------------------------------------------------------- ツリー走査

def _visible(node: dict) -> bool:
    return node.get("visible") is not False


def _box(node: dict) -> dict | None:
    b = node.get("absoluteBoundingBox")
    if not b or not b.get("width") or not b.get("height"):
        return None
    return b


def _has_text(node: dict) -> bool:
    if node.get("type") == "TEXT" and (node.get("characters") or "").strip():
        return True
    return any(_has_text(c) for c in node.get("children") or [])


def _is_picture(node: dict) -> bool:
    """1枚の絵として書き出すべきか（＝中に文字が無い図形・写真）。"""
    if _has_text(node):
        return False
    if _has_image_fill(node):
        return True
    if node.get("type") in _VECTOR_TYPES:
        return True
    # 図形だけで構成されたグループ（アイコン等）はまとめて1枚にする
    kids = node.get("children") or []
    if kids and all(_is_picture(k) for k in kids if _visible(k)):
        return True
    return False


def collect(node: dict, out: list[dict], depth: int = 0) -> None:
    """描くものを平らなリストに集める（文字／絵／ベタ塗りの箱）。

    ★マスク（isMask）は必ず飛ばす。Figmaのマスクは「切り抜きの型紙」で、単体で画像化すると
      **真っ黒なシルエット**になる。実測でヒーロー全体が黒く塗り潰された（2026-07-27）。
      クリップ自体は再現しないが、型紙が出るよりは遥かにマシ。
    """
    if not _visible(node) or depth > 24 or node.get("isMask"):
        return
    b = _box(node)
    t = node.get("type")

    if t == "TEXT" and (node.get("characters") or "").strip():
        if b:
            out.append({"kind": "text", "node": node, "box": b})
        return

    if b and _is_picture(node):
        out.append({"kind": "img", "node": node, "box": b})
        return

    # ★写真の上に文字が乗っている枠（キャプション付きの画像など）。
    #   _is_picture は「中に文字があれば絵ではない」と判定するので、このままだと
    #   写真そのものが出ずに真っ白な穴になる（実測：DAILY FLOWの写真が消えた）。
    #   → 枠を背景の画像として先に敷き、そのあと中の文字を上に載せる。
    if b and _has_image_fill(node):
        out.append({"kind": "img", "node": node, "box": b})
        for c in node.get("children") or []:
            collect(c, out, depth + 1)
        return

    if t in _CONTAINER_TYPES or node.get("children"):
        # 器そのものに背景・枠があれば箱として先に置く（中身はこの上に載る）
        if b and depth > 0 and (_solid_fill(node) or _shadow(node) or node.get("strokes")):
            out.append({"kind": "box", "node": node, "box": b})
        for c in node.get("children") or []:
            collect(c, out, depth + 1)
        return

    if b and (_solid_fill(node) or node.get("strokes")):
        out.append({"kind": "box", "node": node, "box": b})


# ---------------------------------------------------------------- 画像書き出し

def export_images(key: str, ids: list[str], token: str, cfg: config.FigmaConfig) -> dict[str, str]:
    """ノードIDのリストをPNG URLに変換（Figma側でレンダリングしてもらう）。"""
    got: dict[str, str] = {}
    for i in range(0, len(ids), cfg.image_batch):
        chunk = ids[i : i + cfg.image_batch]
        q = urllib.parse.urlencode(
            {"ids": ",".join(chunk), "format": "png", "scale": cfg.image_scale}
        )
        d = _get(f"/images/{key}?{q}", token, cfg.timeout_s)
        for nid, url in (d.get("images") or {}).items():
            if url:
                got[nid] = url
    return got


def download(url: str, dest: Path) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=90) as r:
            data = r.read()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("画像の取得に失敗: %s (%s)", url, e)
        return False


# ---------------------------------------------------------------- HTML化

def _style(css: list[str]) -> str:
    """style="…" に入れる文字列を作る。★" が混ざると属性がそこで切れて以降が全部消えるので必ず潰す。"""
    return ";".join(c for c in css if c).replace('"', "'")


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _text_html(item: dict, ox: float, oy: float) -> str:
    n, b = item["node"], item["box"]
    st = n.get("style") or {}
    chars = n.get("characters") or ""
    # ★ブラウザとFigmaで文字幅がわずかに違う＝箱ぴったりだと最後の1文字が折り返して2行になる
    #   （実測：「1日の流れ」「週1日・1日2時間から相談」等が軒並み2行に割れた）。
    #   Figmaで1行のものは折り返さない・複数行のものは少し余裕を持たせる。
    lh = st.get("lineHeightPx") or (st.get("fontSize") or 16) * 1.5
    one_line = ("\n" not in chars) and (
        st.get("textAutoResize") == "WIDTH_AND_HEIGHT" or b["height"] <= lh * 1.6
    )
    css = [
        "position:absolute",
        f"left:{round(b['x'] - ox)}px",
        f"top:{round(b['y'] - oy)}px",
        f"width:{round(b['width']) + (2 if one_line else 8)}px",
    ]
    if one_line:
        css.append("white-space:pre")
    fs = st.get("fontSize")
    if fs:
        css.append(f"font-size:{round(fs, 1)}px")
    fam = st.get("fontFamily")
    if fam:
        # ★フォント名は必ずシングルクォート。style="…" の中で " を使うと**属性がそこで終わり**、
        #   以降の色・太さ・行間・字間が丸ごと消える（実測：全文字がYu Gothicの黒になった）
        css.append("font-family:'{}',sans-serif".format(str(fam).replace("'", "").replace('"', "")))
    w = st.get("fontWeight")
    if w:
        css.append(f"font-weight:{int(w)}")
    lh = st.get("lineHeightPx")
    if lh:
        css.append(f"line-height:{round(lh, 1)}px")
    ls = st.get("letterSpacing")
    if ls:
        css.append(f"letter-spacing:{round(ls, 2)}px")
    al = {"CENTER": "center", "RIGHT": "right", "JUSTIFIED": "justify"}.get(
        st.get("textAlignHorizontal", ""), ""
    )
    if al:
        css.append(f"text-align:{al}")
    col = _solid_fill(n)
    if col:
        css.append(f"color:{col}")
    else:
        # ★塗りの無い文字＝アウトライン文字（背景の飾り大文字など）。何もしないと
        #   ブラウザ既定の真っ黒になり、巨大な黒文字がページを覆う（実測：ABOUT US）。
        sc = ""
        for s in n.get("strokes") or []:
            if s.get("visible") is not False:
                sc = _col(s.get("color"), s.get("opacity"))
                break
        css.append("color:transparent")
        if sc:
            css.append(f"-webkit-text-stroke:{round(n.get('strokeWeight', 1), 1)}px {sc}")
    if st.get("textCase") == "UPPER":
        css.append("text-transform:uppercase")
    if st.get("textDecoration") == "UNDERLINE":
        css.append("text-decoration:underline")
    if n.get("opacity") is not None and n["opacity"] < 1:
        css.append(f"opacity:{round(n['opacity'], 3)}")
    if not one_line:
        css.append("white-space:pre-wrap")
    body = _esc(chars).replace("\n", "<br>")
    return f'<div style="{_style(css)}">{body}</div>'


def _img_html(item: dict, ox: float, oy: float, src: str) -> str:
    n, b = item["node"], item["box"]
    css = [
        "position:absolute",
        f"left:{round(b['x'] - ox)}px",
        f"top:{round(b['y'] - oy)}px",
        f"width:{round(b['width'])}px",
        f"height:{round(b['height'])}px",
        # 写真枠は cover（枠いっぱい）、アイコン等は contain（欠けない）
        "object-fit:" + ("cover" if _has_image_fill(n) else "contain"),
    ]
    r = _radius(n)
    if r:
        css.append(f"border-radius:{r}")
    sh = _shadow(n)
    if sh:
        css.append(f"box-shadow:{sh}")
    if n.get("opacity") is not None and n["opacity"] < 1:
        css.append(f"opacity:{round(n['opacity'], 3)}")
    alt = _esc(n.get("name") or "図")
    return f'<img src="{_esc(src)}" alt="{alt}" style="{_style(css)}">'


def _box_html(item: dict, ox: float, oy: float) -> str:
    n, b = item["node"], item["box"]
    css = [
        "position:absolute",
        f"left:{round(b['x'] - ox)}px",
        f"top:{round(b['y'] - oy)}px",
        f"width:{round(b['width'])}px",
        f"height:{round(b['height'])}px",
    ]
    fill = _solid_fill(n)
    if fill:
        css.append(("background:" if "gradient" in fill else "background-color:") + fill)
    r = _radius(n)
    if r:
        css.append(f"border-radius:{r}")
    sh = _shadow(n)
    if sh:
        css.append(f"box-shadow:{sh}")
    for s in n.get("strokes") or []:
        if s.get("visible") is False:
            continue
        c = _col(s.get("color"), s.get("opacity"))
        if c:
            css.append(f"border:{round(n.get('strokeWeight', 1))}px solid {c}")
            break
    if n.get("opacity") is not None and n["opacity"] < 1:
        css.append(f"opacity:{round(n['opacity'], 3)}")
    return f'<div style="{_style(css)}"></div>'


# ---------------------------------------------------------------- 本体

def import_from_url(url: str, cfg: config.FigmaConfig | None = None) -> dict[str, Any]:
    """FigmaのURL → カンプHTML。戻り値に file 名と統計を返す。"""
    cfg = cfg or config.FigmaConfig()
    if not cfg.enabled:
        raise RuntimeError(".env の FIGMA_TOKEN が未設定です（figd_… で始まるトークン）")

    key, node_id = parse_figma_url(url)
    log.info("Figma取り込み開始: key=%s node=%s", key, node_id)

    if node_id:
        d = _get(f"/files/{key}/nodes?ids={urllib.parse.quote(node_id)}", cfg.token, cfg.timeout_s)
        nodes = d.get("nodes") or {}
        first = next(iter(nodes.values()), None)
        if not first or not first.get("document"):
            raise RuntimeError("そのノードが取得できませんでした（URLのnode-idを確認してください）")
        root = first["document"]
        title = d.get("name") or root.get("name") or "figma"
    else:
        d = _get(f"/files/{key}", cfg.token, cfg.timeout_s)
        title = d.get("name") or "figma"
        page = (d.get("document") or {}).get("children") or []
        if not page:
            raise RuntimeError("ページが空です")
        frames = [c for c in (page[0].get("children") or []) if _visible(c) and _box(c)]
        if not frames:
            raise RuntimeError("フレームが見つかりません（Figmaでフレームを選んでURLをコピーしてください）")
        # 一番大きいフレームを主役にする
        root = max(frames, key=lambda n: _box(n)["width"] * _box(n)["height"])

    rb = _box(root)
    if not rb:
        raise RuntimeError("大きさが取れませんでした（フレームを選んでください）")
    ox, oy, W, H = rb["x"], rb["y"], round(rb["width"]), round(rb["height"])

    items: list[dict] = []
    collect(root, items, 0)
    if not items:
        raise RuntimeError("中身が空でした")

    # --- 画像を用意する。2通りある（ここを間違えると文字が二重に写る）
    #   ①写真（IMAGE fill）… **元画像そのもの**を取る。フレームごとレンダリングすると
    #     上に乗っている文字まで焼き込まれ、HTML側の文字と二重に見える（実測）
    #   ②アイコン・図形 … Figmaにレンダリングしてもらう（/v1/images）
    imgs = [it for it in items if it["kind"] == "img"]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    srcs: dict[str, str] = {}
    refs: dict[str, str] = {}
    try:
        refs = ((_get(f"/files/{key}/images", cfg.token, cfg.timeout_s).get("meta") or {})
                .get("images") or {})
    except Exception as e:  # noqa: BLE001
        log.warning("画像fillの一覧が取れませんでした（レンダリングで代用）: %s", e)

    render_ids: list[str] = []
    seq = 0
    for it in imgs:
        n = it["node"]
        ref = ""
        for f in n.get("fills") or []:
            if f.get("type") == "IMAGE" and f.get("visible") is not False and f.get("imageRef"):
                ref = f["imageRef"]
                break
        u = refs.get(ref) if ref else None
        if u:
            name = f"fig_{ts}_{seq:03d}.png"
            seq += 1
            if download(u, config.UPLOAD_DIR / name):
                srcs[n["id"]] = f"http://127.0.0.1:5000/uploads/{name}"
                continue
        render_ids.append(n["id"])

    if render_ids:
        urls = export_images(key, render_ids, cfg.token, cfg)
        for nid, u in urls.items():
            name = f"fig_{ts}_{seq:03d}.png"
            seq += 1
            if download(u, config.UPLOAD_DIR / name):
                srcs[nid] = f"http://127.0.0.1:5000/uploads/{name}"
    ids = [it["node"]["id"] for it in imgs]

    # --- HTML組み立て（Figmaの重なり順＝リストの順）
    parts: list[str] = []
    n_text = n_img = n_box = 0
    for it in items:
        if it["kind"] == "text":
            parts.append(_text_html(it, ox, oy)); n_text += 1
        elif it["kind"] == "img":
            src = srcs.get(it["node"]["id"])
            if src:
                parts.append(_img_html(it, ox, oy, src)); n_img += 1
            else:  # 書き出せなかったらベタ塗りの箱で代用（真っ白の穴にしない）
                parts.append(_box_html(it, ox, oy)); n_box += 1
        else:
            parts.append(_box_html(it, ox, oy)); n_box += 1

    bg = _solid_fill(root) or "#ffffff"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"camp_{stamp}_figma.html"
    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="ce-figma-src" content="{_esc(url)}">
<title>{_esc(title)}</title>
<style>
  *{{box-sizing:border-box}}
  body{{margin:0;background:{bg};font-family:"Yu Gothic","Hiragino Kaku Gothic ProN",Meiryo,sans-serif}}
  .fg_root{{position:relative;width:{W}px;height:{H}px;margin:0 auto;background:{bg};overflow:hidden}}
</style></head>
<body>
<section class="fg_root">
{chr(10).join(parts)}
</section>
</body></html>
"""
    out = config.CAMP_DIR / fname
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log.info("Figma取り込み完了: %s （文字%d・画像%d・箱%d）", fname, n_text, n_img, n_box)
    return {
        "file": fname,
        "title": title,
        "width": W,
        "height": H,
        "text": n_text,
        "images": n_img,
        "boxes": n_box,
        "missing_images": len(ids) - len(srcs),
    }
