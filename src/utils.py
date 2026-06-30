"""
小さな共有ユーティリティ。

- URL からの安定ID（ハッシュ）生成
- ログ設定（プロトタイプ方針：ログは多めに出す）
- numpy <-> SQLite BLOB の相互変換
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from urllib.parse import urlsplit, urlunsplit

import numpy as np

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


# 自前のログには関係ない、騒がしい第三者ライブラリ（通信ログ等）
_NOISY_LOGGERS = (
    "httpcore", "httpx", "urllib3", "filelock", "asyncio",
    "huggingface_hub", "transformers", "PIL", "matplotlib",
)


def setup_logging(verbose: bool = True) -> None:
    """プロトタイプ方針で自前ログは多め。第三者ライブラリの通信ログは抑える。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=_LOG_FORMAT, stream=sys.stdout)
    # HuggingFace等の通信DEBUGで画面が埋もれるのを防ぐ（WARNINGだけ通す）
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def normalize_url(url: str) -> str:
    """重複排除の判定がブレないように URL を軽く正規化する。

    - 前後の空白を除去
    - scheme/host を小文字化
    - 末尾スラッシュは保持（パスの意味が変わりうるので触りすぎない）
    """
    url = url.strip()
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        # スキーム省略は https を補う
        url = "https://" + url
    parts = urlsplit(url)
    normalized = urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            parts.query,
            "",  # fragment(#以降)は捨てる：同じページ扱い
        )
    )
    return normalized


def url_to_id(url: str) -> str:
    """URL から安定したID（短いハッシュ）を作る。"""
    normalized = normalize_url(url)
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
    return digest[:16]


def url_to_slug(url: str) -> str:
    """スクショのファイル名に使う、人が読めるスラッグ。"""
    parts = urlsplit(normalize_url(url))
    base = (parts.netloc + parts.path).lower()
    slug = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    return slug[:60] or "site"


def embedding_to_blob(vec: np.ndarray) -> bytes:
    """float32 の numpy ベクトルを SQLite 保存用の bytes に変換。"""
    return np.asarray(vec, dtype=np.float32).tobytes()


def blob_to_embedding(blob: bytes) -> np.ndarray:
    """SQLite から読んだ bytes を float32 numpy ベクトルに戻す。"""
    return np.frombuffer(blob, dtype=np.float32)


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    """L2正規化。これで内積 = コサイン類似度になる。"""
    vec = np.asarray(vec, dtype=np.float32)
    norm = np.linalg.norm(vec)
    if norm == 0:
        return vec
    return vec / norm
