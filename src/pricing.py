"""AI料金の実費記録と見積もり（🌙自動磨きなどの「いくらかかる？」の頭脳）。

仕組み：
- 単価はプロジェクト直下の llm_prices.json（人が編集する・Git同期される）。
- 各APIは返事のたびに実測トークン数(usage)を返す → camp._call_llm が本モジュールの
  record() を呼び、実測×単価の実費を data/camps/_cost_log.json に貯める。
- 見積もりは「同じ作業(task)の過去実測の平均」。実績ゼロのときだけ既定値を使う
  ＝使うほど見積もりが実態に近づく（賢くなる）。
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import config
from .utils import get_logger

log = get_logger(__name__)

_PRICES_PATH = config.PROJECT_ROOT / "llm_prices.json"
_LOG_PATH = config.CAMP_DIR / "_cost_log.json"
_LOCK = threading.Lock()
_KEEP = 800  # ログの保持件数（増えすぎ防止）

# 実績が1件も無いときの初期見積もり（円）。数回使うと実測平均に置き換わる。
_DEFAULT_YEN = {
    "brushup_critique": 10.0,  # 🌙磨きの指摘（画像つき・上位モデル）
    "brushup_fix": 5.0,        # 🌙磨きの修正（セクション書き直し）
    "critique": 10.0,          # 手動の🧐指摘
    "recheck": 2.0,            # ✅直ったか確認（安いモデル）
    "edit": 5.0,               # 通常のセクション修正
    "misc": 5.0,
}


def _prices() -> dict:
    try:
        return json.loads(_PRICES_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log.warning("llm_prices.json が読めません（見積もりは既定単価で続行）")
        return {"usd_jpy": 155, "prices": {}}


def price_for(provider: str, model: str) -> tuple[float, float, float]:
    """(入力USD/100万tok, 出力USD/100万tok, 円レート)。モデル名は前方一致で探す。"""
    d = _prices()
    table = (d.get("prices") or {}).get((provider or "").lower(), {})
    m = (model or "").lower().split(":")[-1]  # "openai:gpt-5.6-sol" 形式も許す
    best = None
    for k, v in table.items():
        if k.startswith("_"):
            continue
        if m.startswith(k.lower()) and (best is None or len(k) > best[0]):
            best = (len(k), v)
    v = best[1] if best else table.get("_default") or {"in": 3.0, "out": 15.0}
    return float(v.get("in", 0)), float(v.get("out", 0)), float(d.get("usd_jpy", 155))


def cost_jpy(provider: str, model: str, tok_in: int, tok_out: int) -> float:
    pin, pout, rate = price_for(provider, model)
    return (tok_in * pin + tok_out * pout) / 1_000_000 * rate


def record(task: str, provider: str, model: str, tok_in: int, tok_out: int) -> float:
    """1回のAI呼び出しの実費を記録して円を返す。記録に失敗しても本処理は止めない。"""
    try:
        yen = round(cost_jpy(provider, model, tok_in, tok_out), 3)
        if tok_in <= 0 and tok_out <= 0:
            return 0.0  # usageが取れなかった呼び出しは平均を汚さないので記録しない
        row = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "task": task or "misc", "provider": provider, "model": model,
            "in": tok_in, "out": tok_out, "yen": yen,
        }
        with _LOCK:
            rows = []
            if _LOG_PATH.exists():
                try:
                    rows = json.loads(_LOG_PATH.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    rows = []
            rows.append(row)
            rows = rows[-_KEEP:]
            tmp = _LOG_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, _LOG_PATH)
        return yen
    except Exception:  # noqa: BLE001
        log.exception("費用ログの記録に失敗（処理は続行）")
        return 0.0


def avg_yen(task: str) -> float:
    """その作業(task)の過去実測の平均円（直近30件）。実績が無ければ既定値。"""
    try:
        rows = json.loads(_LOG_PATH.read_text(encoding="utf-8"))
        vals = [r["yen"] for r in rows if r.get("task") == task and r.get("yen", 0) > 0][-30:]
        if vals:
            return sum(vals) / len(vals)
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT_YEN.get(task, 5.0)
