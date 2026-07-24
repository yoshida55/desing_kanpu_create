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


def _session():
    global _SESSION
    if _SESSION is None:
        from rembg import new_session  # 遅延import＝未導入でもviewer起動は死なせない
        _SESSION = new_session(_MODEL)
    return _SESSION


def remove_background(src: Path) -> bytes:
    """src の画像を背景除去して、透過PNG（RGBA）のバイト列を返す。"""
    from rembg import remove
    data = Path(src).read_bytes()
    # post_process_mask=True でフチのギザつきを軽減
    return remove(data, session=_session(), post_process_mask=True)
