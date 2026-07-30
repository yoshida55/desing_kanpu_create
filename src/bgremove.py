"""アップロード画像の背景を除去して透過PNGにする（rembg・ローカル・無料）。

rembg は onnxruntime + U2-Net モデルで動く。モデルは初回実行時に自動DLされ、
以後はローカルキャッシュから読む。重いのでセッションはプロセス内で1回だけ作って使い回す。
"""
from __future__ import annotations

import os
from pathlib import Path

# モデルは環境変数で差し替え可（既定 u2net＝汎用。人物特化なら u2net_human_seg）
_MODEL = os.getenv("DESIGN_STOCK_BGREMOVE_MODEL", "u2net")
_SESSION = None


# 未導入のときに画面へ出す案内。★ここが「背景削除できない」の実際の正体だった（2026-07-30）。
# 旧実装は ModuleNotFoundError がそのまま画面に出ていて（"No module named 'rembg'"）、
# 何をすればいいのか分からず「できない」だけになっていた。手順まで書いて返す。
_MISSING = (
    "背景を切り抜く機能（rembg）がこのPCに入っていません。"
    "コマンドで入れてください：venv\\Scripts\\python.exe -m pip install rembg onnxruntime"
    "（約200MB・初回だけAIモデルを自動ダウンロード）。入れたらサーバーを再起動してください"
)


def available() -> bool:
    """rembg が使える状態か（画面側でボタンを出す/出さないの判定に使える）。"""
    try:
        import rembg  # noqa: F401
    except Exception:
        return False
    return True


def _import_rembg():
    """遅延import＝未導入でもviewer起動は死なせない。無いときは手順つきで知らせる。"""
    try:
        from rembg import new_session, remove
    except ModuleNotFoundError as exc:      # rembg 本体が無い
        raise RuntimeError(_MISSING) from exc
    except ImportError as exc:              # onnxruntime 等の連れ物が足りない
        raise RuntimeError(_MISSING + "（内訳：" + str(exc) + "）") from exc
    return new_session, remove


def _session(new_session):
    global _SESSION
    if _SESSION is None:
        _SESSION = new_session(_MODEL)
    return _SESSION


def remove_background(src: Path) -> bytes:
    """src の画像を背景除去して、透過PNG（RGBA）のバイト列を返す。"""
    new_session, remove = _import_rembg()
    data = Path(src).read_bytes()
    # post_process_mask=True でフチのギザつきを軽減
    return remove(data, session=_session(new_session), post_process_mask=True)
