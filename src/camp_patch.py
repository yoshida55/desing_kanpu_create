"""Codex 用パッチの読み書きと検証（共同編集 Phase 2）。

仕様書: docs/Codexと編集ツールの共同編集仕様書.md

このモジュールの役割はひとつだけ。
「Codex の変更を、カンプHTMLを直接触らずに別ファイル（パッチ）として安全に貯める」。

★HTMLは一切書き換えない。適用（Phase 3）と正式取り込み（Phase 4）は viewer 側の仕事。
★検証はここに集約する。CLI/API どちらから来ても同じ関門を通す（片方だけ緩いと意味がない）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from . import config

SCHEMA_VERSION = 1

# パッチの置き場所。HTMLと同じフォルダに置くと、一覧・削除・同期の対象がぶつかるので必ず分ける。
PATCH_DIR = config.DATA_DIR / "camp_patches"
HISTORY_DIR = config.DATA_DIR / "camp_patch_history"

# ---- 制限（暴走したパッチでカンプを壊さないための上限）-------------------------
MAX_OPERATIONS = 500
MAX_VALUE_LEN = 2000
MAX_PATCH_BYTES = 512 * 1024

# ---- 許可リスト --------------------------------------------------------------
# ★「危険なものを禁止する」ではなく「安全なものだけ許す」方式にする。
#   禁止リスト方式は、新しい書き方が増えるたびに穴が開く。
ALLOWED_OPS = {
    "set_style",
    "remove_style",
    "set_attribute",
    "remove_attribute",
    "replace_image",
    "set_text",
    "set_transform_state",
}

# 位置・寸法・余白・文字・装飾まわりだけ。display/position/z-index は影響が広いので初期版では出さない。
ALLOWED_STYLE_PROPS = {
    "top", "left", "right", "bottom",
    "width", "height", "min-width", "min-height", "max-width", "max-height",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "gap", "row-gap", "column-gap",
    "font-size", "font-weight", "font-style", "font-family",
    "line-height", "letter-spacing", "text-align", "text-transform",
    "color", "-webkit-text-fill-color", "text-shadow", "text-decoration-line",
    "background-color", "background-image", "background-size",
    "background-position", "background-repeat",
    "border", "border-color", "border-style", "border-width", "border-radius",
    "box-shadow", "opacity", "filter",
    "object-fit", "object-position",
}

# alt/aria など「見た目に副作用が無い」ものだけ。on* とリンク先は入れない。
ALLOWED_ATTRS = {"alt", "title", "loading", "decoding"}
ALLOWED_ATTR_PREFIX = ("aria-",)

# 値に混ぜられたら困るもの（CSSの式経由でのスクリプト実行・外部読み込み）
BAD_VALUE = re.compile(
    r"(javascript:|expression\s*\(|@import|behavior\s*:|url\s*\(\s*['\"]?\s*(javascript|data:text))",
    re.I,
)
CEID_RE = re.compile(r"^ce_[A-Za-z0-9_-]{4,64}$")
JST = timezone(timedelta(hours=9))


class PatchError(ValueError):
    """検証で弾いた（呼び出し側はこれをユーザー向けメッセージに使う）。"""


# ---- ファイル解決 -------------------------------------------------------------
def resolve_camp(filename: str) -> Path:
    """カンプHTMLのパスを返す。CAMP_DIR の外・拡張子違いは弾く。

    ★ここを緩めない。`..` や絶対パスを渡されると任意のファイルを指せてしまう。
    """
    if not filename or not isinstance(filename, str):
        raise PatchError("ファイル名がありません")
    name = os.path.basename(filename.strip().replace("\\", "/"))
    if not name or name.startswith("."):
        raise PatchError(f"使えないファイル名です: {filename}")
    if Path(name).suffix.lower() not in (".html", ".htm"):
        raise PatchError("対象は .html / .htm だけです")
    base = config.CAMP_DIR.resolve()
    path = (base / name).resolve()
    if path.parent != base:
        raise PatchError("カンプフォルダの外は指定できません")
    if not path.exists():
        raise PatchError(f"カンプが見つかりません: {name}")
    return path


def patch_path(filename: str) -> Path:
    return PATCH_DIR / (resolve_camp(filename).stem + ".patch.json")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- 検証 --------------------------------------------------------------------
def _check_value(v: Any, label: str) -> str:
    if not isinstance(v, str):
        raise PatchError(f"{label} は文字列で指定してください")
    if len(v) > MAX_VALUE_LEN:
        raise PatchError(f"{label} が長すぎます（{MAX_VALUE_LEN}文字まで）")
    if BAD_VALUE.search(v):
        raise PatchError(f"{label} に使えない書き方が含まれています")
    return v


def _check_target(t: Any) -> str:
    if not isinstance(t, str) or not CEID_RE.match(t):
        raise PatchError(f"target は data-ceid の形式で指定してください: {t!r}")
    return t


def validate_operation(op: dict) -> dict:
    """1操作を検証して、正規化した dict を返す。通らなければ PatchError。"""
    if not isinstance(op, dict):
        raise PatchError("操作の形式が不正です")
    kind = op.get("op")
    if kind not in ALLOWED_OPS:
        raise PatchError(f"未対応の操作です: {kind!r}")
    out: dict[str, Any] = {"op": kind, "target": _check_target(op.get("target"))}

    if kind in ("set_style", "remove_style"):
        prop = str(op.get("property", "")).strip().lower()
        if prop not in ALLOWED_STYLE_PROPS:
            raise PatchError(f"このスタイルは変更できません: {prop!r}")
        out["property"] = prop
        if kind == "set_style":
            out["value"] = _check_value(op.get("value"), "value")
            pr = op.get("priority")
            if pr not in (None, "", "important"):
                raise PatchError("priority は important だけ指定できます")
            if pr:
                out["priority"] = "important"

    elif kind in ("set_attribute", "remove_attribute"):
        name = str(op.get("name", "")).strip().lower()
        ok = name in ALLOWED_ATTRS or name.startswith(ALLOWED_ATTR_PREFIX)
        if not ok or name.startswith("on") or name.startswith("data-ce"):
            raise PatchError(f"この属性は変更できません: {name!r}")
        out["name"] = name
        if kind == "set_attribute":
            out["value"] = _check_value(op.get("value"), "value")

    elif kind == "replace_image":
        src = _check_value(op.get("src"), "src")
        # Windows の絶対パスや外部URLは弾く。ブラウザから参照できる相対URLだけ許す。
        if re.match(r"^[a-zA-Z]:[\\/]", src) or "://" in src or src.startswith("//"):
            raise PatchError("src はサイト内の相対URLで指定してください（例 /uploads/xxx.png）")
        out["src"] = src
        if op.get("alt") is not None:
            out["alt"] = _check_value(op.get("alt"), "alt")

    elif kind == "set_text":
        out["value"] = _check_value(op.get("value"), "value")

    elif kind == "set_transform_state":
        # ★transform を自由入力させない。編集ツールの data-cetx 等と同じ形でだけ渡す
        #   （CSSだけ変えると内部状態とズレて、次のドラッグで位置が飛ぶ）。
        for k, lo, hi in (
            ("translateX", -20000, 20000), ("translateY", -20000, 20000),
            ("scaleX", 0.01, 50), ("scaleY", 0.01, 50), ("rotate", -3600, 3600),
        ):
            if k in op:
                try:
                    n = float(op[k])
                except (TypeError, ValueError):
                    raise PatchError(f"{k} は数値で指定してください")
                if not (lo <= n <= hi):
                    raise PatchError(f"{k} が範囲外です（{lo}〜{hi}）")
                out[k] = n
        if len(out) == 2:
            raise PatchError("set_transform_state に変更内容がありません")
    return out


def _now() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def empty_patch(filename: str, base_sha: str) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "targetFile": os.path.basename(filename),
        "baseSha256": base_sha,
        "revision": 0,
        "createdBy": "codex",
        "createdAt": _now(),
        "updatedAt": _now(),
        "operations": [],
    }


def load(filename: str) -> dict | None:
    """パッチを読む。無ければ None。壊れていれば PatchError。"""
    p = patch_path(filename)
    if not p.exists():
        return None
    if p.stat().st_size > MAX_PATCH_BYTES:
        raise PatchError("パッチが大きすぎます")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise PatchError(f"パッチが読めません: {e}")
    if not isinstance(data, dict):
        raise PatchError("パッチの形式が不正です")
    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise PatchError(f"未対応のパッチ版です: {data.get('schemaVersion')!r}")
    if os.path.basename(str(data.get("targetFile", ""))) != resolve_camp(filename).name:
        raise PatchError("パッチの対象ファイルが一致しません")
    ops = data.get("operations")
    if not isinstance(ops, list):
        raise PatchError("operations が配列ではありません")
    if len(ops) > MAX_OPERATIONS:
        raise PatchError(f"操作が多すぎます（{MAX_OPERATIONS}件まで）")
    data["operations"] = [validate_operation(o) for o in ops]
    return data


def save(filename: str, patch: dict) -> Path:
    """原子的に書き込む（途中で落ちても壊れたパッチを残さない）。"""
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    p = patch_path(filename)
    body = json.dumps(patch, ensure_ascii=False, indent=2)
    if len(body.encode("utf-8")) > MAX_PATCH_BYTES:
        raise PatchError("パッチが大きすぎます")
    fd, tmp = tempfile.mkstemp(dir=str(PATCH_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, p)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


def add_operations(filename: str, ops: list[dict], created_by: str = "codex") -> dict:
    """操作を追加して保存し、保存後のパッチを返す。

    ★べき等にする：同じ target × 同じ対象プロパティの操作は後勝ちで1件にまとめる。
      「今の値に20px足す」のような相対指定は validate 側で許していないので、二度適用しても同じ結果になる。
    """
    camp = resolve_camp(filename)
    cur = load(filename) or empty_patch(camp.name, sha256_of(camp))
    checked = [validate_operation(o) for o in ops]
    if not checked:
        raise PatchError("追加する操作がありません")

    def key(o: dict) -> tuple:
        return (o["target"], o["op"], o.get("property") or o.get("name") or "")

    merged = {key(o): o for o in cur["operations"]}
    for o in checked:
        merged[key(o)] = o
    if len(merged) > MAX_OPERATIONS:
        raise PatchError(f"操作が多すぎます（{MAX_OPERATIONS}件まで）")

    cur["operations"] = list(merged.values())
    cur["revision"] = int(cur.get("revision", 0)) + 1
    cur["createdBy"] = created_by
    cur["updatedAt"] = _now()
    # ★baseSha256 は作り直さない。Codexが見た時点のHTMLを指し続けることで、
    #   その後ツール側で保存された（＝前提が変わった）ことを検知できる。
    save(filename, cur)
    return cur


def rebase_stale(filename: str) -> dict:
    """残った stale パッチを、現在の正式HTMLを基準に作り直す。

    HTMLの変更を取り消す操作ではない。全 target が現在のHTMLにも残っている
    場合に限り既存操作を保持して baseSha256 だけを更新し、元パッチは履歴へ
    複製する。
    """
    camp = resolve_camp(filename)
    cur = load(filename)
    if not cur:
        raise PatchError("作り直すパッチがありません")

    now_sha = sha256_of(camp)
    old_base = cur.get("baseSha256")
    if old_base == now_sha:
        raise PatchError("パッチは stale ではありません")

    html = camp.read_text(encoding="utf-8", errors="replace")
    ids = set(re.findall(r'''\bdata-ceid\s*=\s*["'](ce_[A-Za-z0-9_-]{4,64})["']''', html, re.I))
    missing = sorted({op["target"] for op in cur["operations"] if op["target"] not in ids})
    if missing:
        raise PatchError("現在のHTMLに無い対象IDがあります: " + ", ".join(missing))

    stem = camp.stem
    dest_dir = HISTORY_DIR / stem
    dest_dir.mkdir(parents=True, exist_ok=True)
    old_rev = int(cur.get("revision", 0))
    archive = dest_dir / f"{old_rev:06d}.rebased.json"
    if archive.exists():
        raise PatchError(f"同じrevisionの再基準化履歴が既にあります: {archive.name}")
    archive.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")

    cur["baseSha256"] = now_sha
    cur["revision"] = old_rev + 1
    cur["updatedAt"] = _now()
    cur["rebasedFromSha256"] = old_base
    save(filename, cur)
    return cur


def consume(filename: str, revision: int) -> None:
    """HTMLへの取り込みが成功した後に呼ぶ。パッチを履歴へ移して消化済みにする。

    ★HTMLの保存が成功する前に呼ばない（呼ぶと変更が消える）。
    """
    p = patch_path(filename)
    if not p.exists():
        return
    stem = resolve_camp(filename).stem
    dest_dir = HISTORY_DIR / stem
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{int(revision):06d}.applied.json"
    os.replace(p, dest)


def status(filename: str) -> dict:
    """今の状態（Codex/CLI から見る用）。"""
    camp = resolve_camp(filename)
    cur = load(filename)
    now_sha = sha256_of(camp)
    out = {
        "file": camp.name,
        "currentSha256": now_sha,
        "hasPatch": cur is not None,
        "revision": (cur or {}).get("revision", 0),
        "operationCount": len((cur or {}).get("operations", [])),
        "baseSha256": (cur or {}).get("baseSha256"),
        "stale": bool(cur and cur.get("baseSha256") != now_sha),
    }
    return out
