"""Codex 用のパッチCLI（共同編集 Phase 5）。

仕様書: docs/Codexと編集ツールの共同編集仕様書.md

使い方（Codexはこれ以外の方法でカンプHTMLを触らない）:

    python tools/camp_patch.py status  --file example.html
    python tools/camp_patch.py inspect --file example.html --section 2
    python tools/camp_patch.py inspect --file example.html --find "軽作業"
    python tools/camp_patch.py set-style --file example.html --id ce_xxx --property left --value 1030px --important
    python tools/camp_patch.py remove-style --file example.html --id ce_xxx --property box-shadow
    python tools/camp_patch.py set-text  --file example.html --id ce_xxx --value "新しい文章"
    python tools/camp_patch.py replace-image --file example.html --id ce_xxx --src /uploads/a.png --alt "説明"
    python tools/camp_patch.py rebase-stale --file example.html
    python tools/camp_patch.py validate --file example.html
    python tools/camp_patch.py report   --file example.html

★作業前に必ず status を見る。`stale: true` なら、そのパッチはもう前提が変わっている（作り直す）。
★inspect は必ず --section / --find / --limit で絞る。全部出すと数百件になって選べない。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import camp_patch as cp  # noqa: E402
from src import config  # noqa: E402

# ブラウザで開いて実際の位置・大きさ・見えている色を測る。
# ★静的にHTMLを読むだけでは「今どこにあるか」が分からない。このカンプは自由配置(absolute)＋
#   ツールのtranslateで動かしてあるので、ソース上の順番と見た目が一致しない。
_PROBE = r"""() => {
  const out = [];
  document.querySelectorAll('[data-ceid]').forEach(e => {
    const r = e.getBoundingClientRect();
    const c = getComputedStyle(e);
    let own = '';
    e.childNodes.forEach(n => { if (n.nodeType === 3) own += (n.nodeValue || ''); });
    const sec = e.closest('section,header,footer');
    out.push({
      id: e.getAttribute('data-ceid'),
      tag: e.tagName.toLowerCase(),
      cls: (e.className && typeof e.className === 'string') ? e.className.trim().split(/\s+/).slice(0, 2).join('.') : '',
      text: own.replace(/\s+/g, ' ').trim().slice(0, 40),
      x: Math.round(r.left + scrollX), y: Math.round(r.top + scrollY),
      w: Math.round(r.width), h: Math.round(r.height),
      section: sec ? (sec.tagName.toLowerCase() + (sec.className ? '.' + (sec.className + '').split(' ')[0] : '')) : '',
      color: c.color, fontSize: c.fontSize, bg: c.backgroundColor,
      src: e.tagName === 'IMG' ? (e.getAttribute('src') || '') : '',
      alt: e.tagName === 'IMG' ? (e.getAttribute('alt') || '') : '',
      parent: e.parentElement ? (e.parentElement.getAttribute('data-ceid') || '') : '',
      leaf: e.children.length === 0,
    });
  });
  return out;
}"""


def _probe(path: Path) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("playwright が入っていません（venv で実行してください）")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        pg.goto(path.as_uri(), wait_until="load", timeout=90000)
        pg.wait_for_timeout(3000)
        rows = pg.evaluate(_PROBE)
        b.close()
    return rows


def cmd_status(a) -> int:
    print(json.dumps(cp.status(a.file), ensure_ascii=False, indent=2))
    return 0


def cmd_inspect(a) -> int:
    path = cp.resolve_camp(a.file)
    rows = _probe(path)
    if not rows:
        print("data-ceid が1つもありません。先にツールでこのカンプを開いて💾保存してください"
              "（保存でIDがHTMLに焼き込まれます）")
        return 1
    secs: list[str] = []
    for r in rows:
        if r["section"] and r["section"] not in secs:
            secs.append(r["section"])
    if a.section is not None:
        if not (1 <= a.section <= len(secs)):
            print(f"セクション番号は 1〜{len(secs)} です: {secs}")
            return 1
        want = secs[a.section - 1]
        rows = [r for r in rows if r["section"] == want]
    if a.find:
        k = a.find
        rows = [r for r in rows if k in r["text"] or k in r["cls"] or k in r["src"]]
    if a.images:
        rows = [r for r in rows if r["tag"] == "img"]
    if a.text_only:
        rows = [r for r in rows if r["text"]]
    total = len(rows)
    rows = rows[: a.limit]
    print(json.dumps({
        "file": path.name,
        "sections": [f"{i+1}: {s}" for i, s in enumerate(secs)],
        "matched": total,
        "shown": len(rows),
        "hint": "絞るなら --section N / --find 文字 / --images / --text-only",
        "items": rows,
    }, ensure_ascii=False, indent=2))
    return 0


def _add(a, op: dict) -> int:
    try:
        patch = cp.add_operations(a.file, [op])
    except cp.PatchError as e:
        print(f"拒否: {e}")
        return 1
    print(json.dumps({"ok": True, "revision": patch["revision"],
                      "operations": len(patch["operations"])}, ensure_ascii=False))
    return 0


def cmd_set_style(a) -> int:
    op = {"op": "set_style", "target": a.id, "property": a.property, "value": a.value}
    if a.important:
        op["priority"] = "important"
    return _add(a, op)


def cmd_remove_style(a) -> int:
    return _add(a, {"op": "remove_style", "target": a.id, "property": a.property})


def cmd_set_text(a) -> int:
    return _add(a, {"op": "set_text", "target": a.id, "value": a.value})


def cmd_replace_image(a) -> int:
    op = {"op": "replace_image", "target": a.id, "src": a.src}
    if a.alt is not None:
        op["alt"] = a.alt
    return _add(a, op)


def cmd_rebase_stale(a) -> int:
    """対象IDが残っている stale パッチだけを現在のHTML基準に作り直す。"""
    try:
        patch = cp.rebase_stale(a.file)
    except cp.PatchError as e:
        print(f"拒否: {e}")
        return 1
    print(json.dumps({"ok": True, "revision": patch["revision"],
                      "operations": len(patch["operations"]),
                      "baseSha256": patch["baseSha256"]}, ensure_ascii=False))
    return 0


def cmd_validate(a) -> int:
    """パッチが今のHTMLに当たるかを、ブラウザを開かずに確かめる。

    ★見た目まで確認したいなら --live（ブラウザで対象IDの存在を実際に見る）。
    """
    try:
        patch = cp.load(a.file)
    except cp.PatchError as e:
        print(f"NG: {e}")
        return 1
    if not patch:
        print(json.dumps({"ok": True, "message": "パッチはありません"}, ensure_ascii=False))
        return 0
    st = cp.status(a.file)
    miss: list[str] = []
    if a.live:
        ids = {r["id"] for r in _probe(cp.resolve_camp(a.file))}
        miss = sorted({o["target"] for o in patch["operations"] if o["target"] not in ids})
    ok = not st["stale"] and not miss
    print(json.dumps({
        "ok": ok,
        "revision": patch["revision"],
        "operations": len(patch["operations"]),
        "stale": st["stale"],
        "missingTargets": miss,
        "message": ("そのまま使えます" if ok else
                    ("HTMLが更新されているのでパッチを作り直してください" if st["stale"]
                     else "存在しないIDがあります")),
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_report(a) -> int:
    """直近に取り込まれたパッチ（履歴）を見る。"""
    stem = cp.resolve_camp(a.file).stem
    d = cp.HISTORY_DIR / stem
    hist = sorted(p.name for p in d.glob("*.applied.json")) if d.exists() else []
    print(json.dumps({"file": a.file, "current": cp.status(a.file),
                      "appliedHistory": hist[-10:]}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Codex用パッチCLI（カンプHTMLは直接編集しない）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def base(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--file", required=True)
        s.set_defaults(fn=fn)
        return s

    base("status", cmd_status, "パッチの有無・世代・前提ズレ(stale)を見る")

    s = base("inspect", cmd_inspect, "変更できる要素を一覧（必ず絞ること）")
    s.add_argument("--section", type=int, help="何番目のセクションか（1始まり）")
    s.add_argument("--find", help="文字・クラス・srcの部分一致")
    s.add_argument("--images", action="store_true", help="画像だけ")
    s.add_argument("--text-only", dest="text_only", action="store_true", help="文字を持つ要素だけ")
    s.add_argument("--limit", type=int, default=40)

    s = base("set-style", cmd_set_style, "スタイルを指定")
    s.add_argument("--id", required=True)
    s.add_argument("--property", required=True)
    s.add_argument("--value", required=True)
    s.add_argument("--important", action="store_true")

    s = base("remove-style", cmd_remove_style, "スタイルを外す")
    s.add_argument("--id", required=True)
    s.add_argument("--property", required=True)

    s = base("set-text", cmd_set_text, "文字を差し替え（子要素が無い要素だけ）")
    s.add_argument("--id", required=True)
    s.add_argument("--value", required=True)

    s = base("replace-image", cmd_replace_image, "画像を差し替え")
    s.add_argument("--id", required=True)
    s.add_argument("--src", required=True)
    s.add_argument("--alt")

    base("rebase-stale", cmd_rebase_stale, "対象IDが残る stale パッチを現在HTML基準に作り直す")

    s = base("validate", cmd_validate, "そのまま使えるか確かめる")
    s.add_argument("--live", action="store_true", help="ブラウザで対象IDの存在も確かめる")

    base("report", cmd_report, "取り込み履歴を見る")

    a = ap.parse_args()
    try:
        return a.fn(a)
    except cp.PatchError as e:
        print(f"拒否: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
