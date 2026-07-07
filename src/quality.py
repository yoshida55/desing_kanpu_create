"""生成カンプの仕上がりチェック（薄い出力＝ハズレ回アラート）。

「余白が多い・スカスカ」なハズレ生成を検出して警告を出す。

★開発メモ（2026-07-07・実カンプ6本で校正した正直な記録）:
  最初はスクショのピクセル解析（無地行/無地セルの比率）を試したが、
  良いデザインの「意図した余白」とハズレの「スカスカ」を区別できず不採用
  （良0.35〜0.41 vs 悪0.24〜0.44で完全に重なった）。
  実際に良悪を完全分離したのは「情報量」の3指標：
    - 本文テキスト量: 良 2494〜2781字 / 悪 1279〜1506字
    - セクション数:   良 10〜13     / 悪 4〜6
    - ページ実高さ:   良 9223px〜   / 悪 〜5475px（※要ブラウザなので不採用）
  → 静的に測れるテキスト量とセクション数の2つを採用。一瞬で終わり生成を遅くしない。
  しきい値はストックが増えたら見直す（決め打ち禁止ルール準拠）。
"""
from __future__ import annotations

import re

from . import config
from .utils import get_logger

log = get_logger(__name__)

# 校正記録は冒頭docstring参照（良悪の中間に置いた）
_MIN_TEXT = 2000   # 本文テキスト量（タグ・空白除去後の文字数）
_MIN_SECTIONS = 8  # セクション数


def check_camp(file_name: str) -> dict:
    """カンプHTMLの「薄さ」を静的チェックする。

    返り値: {"text_len": 1298, "sections": 4, "warn": "…" or ""}
    失敗しても例外は投げない（チェックは生成を止めない保険機能）。
    """
    try:
        path = config.CAMP_DIR / file_name
        html = path.read_text(encoding="utf-8")

        sections = len(re.findall(r"<section\b", html, re.I))
        m = re.search(r"<body.*", html, re.S)
        body = m.group(0) if m else html
        body = re.sub(r"<(script|style).*?</\1>", "", body, flags=re.S)
        text = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", body))
        text_len = len(text)

        reasons = []
        if text_len < _MIN_TEXT:
            reasons.append(f"文章量が少ない({text_len}字)")
        if sections < _MIN_SECTIONS:
            reasons.append(f"セクションが少ない({sections}個)")
        warn = ""
        if reasons:
            warn = ("⚠ 内容が薄いかも: " + "・".join(reasons)
                    + "。余白だらけのハズレ回の可能性が高いので、再生成をおすすめします")
        log.info("品質チェック %s: %d字 / %dセクション %s",
                 file_name, text_len, sections, "→警告" if warn else "→OK")
        return {"text_len": text_len, "sections": sections, "warn": warn}
    except Exception:  # noqa: BLE001
        log.exception("品質チェックに失敗（生成自体は成功しているので続行）")
        return {"warn": ""}


def log_result(file_name: str, base_id: str, anim_id: str, model: str, q: dict) -> None:
    """生成結果と手本の組み合わせを記録する（data/camps/_quality_log.json）。

    貯まったら「この手本はハズレ率が高い」を選択時点で出す材料にする。
    1行=1生成の追記型。失敗しても生成は止めない。
    """
    import json
    import time

    try:
        path = config.CAMP_DIR / "_quality_log.json"
        rows = []
        if path.exists():
            rows = json.loads(path.read_text(encoding="utf-8"))
        rows.append({
            "file": file_name, "base_id": base_id or "", "anim_id": anim_id or "",
            "model": model, "text_len": q.get("text_len"), "sections": q.get("sections"),
            "warn": bool(q.get("warn")), "ts": int(time.time()),
        })
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        log.exception("品質ログの記録に失敗（続行）")


def _load_log() -> list[dict]:
    import json

    path = config.CAMP_DIR / "_quality_log.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


def _save_log(rows: list[dict]) -> None:
    import json

    path = config.CAMP_DIR / "_quality_log.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ユーザーの手動評価。◎○=当たり、△✖=ハズレ として集計に使う
RATINGS = ("◎", "○", "△", "✖")


def set_rating(file_name: str, rating: str) -> bool:
    """カンプにユーザー評価（◎○△✖）を付ける。空文字で解除。

    生成時のログ行があればそこに書き、無ければ（昔のカンプ）評価だけの行を足す。
    """
    if rating and rating not in RATINGS:
        return False
    rows = _load_log()
    for r in rows:
        if r.get("file") == file_name:
            r["rating"] = rating
            break
    else:
        import time
        rows.append({"file": file_name, "base_id": "", "anim_id": "",
                     "rating": rating, "ts": int(time.time())})
    _save_log(rows)
    log.info("カンプ評価: %s = %s", file_name, rating or "(解除)")
    return True


def get_rating(file_name: str) -> str:
    for r in _load_log():
        if r.get("file") == file_name:
            return r.get("rating", "")
    return ""


def base_stats(base_id: str) -> dict:
    """その手本（ベース）で生成した過去カンプの実績を集計する。

    返り値: {"counts": {"◎":1,"○":2,"△":0,"✖":3}, "auto_warn": 2, "total": 6, "note": "…"}
    note は選択時にそのまま表示できる一言（実績が無ければ空文字）。
    """
    counts = {k: 0 for k in RATINGS}
    auto_warn = 0
    total = 0
    for r in _load_log():
        if r.get("base_id") != base_id:
            continue
        total += 1
        rt = r.get("rating", "")
        if rt in counts:
            counts[rt] += 1
        elif r.get("warn"):
            auto_warn += 1  # 手動評価が無い行は自動判定NGだけ数える
    note = ""
    rated = sum(counts.values())
    if rated:
        bad = counts["△"] + counts["✖"]
        parts = "・".join(f"{k}{v}" for k, v in counts.items() if v)
        note = f"この手本の過去実績: {parts}"
        if rated >= 3 and bad / rated >= 0.5:
            note = f"⚠ {note}（ハズレ率{bad / rated:.0%}・別の手本も検討を）"
    elif auto_warn:
        note = f"この手本: 過去{total}回中{auto_warn}回が自動判定で「薄い」でした"
    return {"counts": counts, "auto_warn": auto_warn, "total": total, "note": note}
