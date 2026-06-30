"""
埋め込み（embed）パイプライン。仕様 4.3 を実装する。

- firstview.png を画像エンコーダに通す → L2正規化 → DBに BLOB 保存
- embed_model_name / embed_version / embed_dim を記録（差し替え検知・再埋め込み判定用）
- 対象は「まだ埋め込みが無い or モデルが変わったレコード」だけ（--force で全件）
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import config, db
from .model import DesignEmbedder
from .utils import embedding_to_blob, get_logger

log = get_logger("embed")


def embed_all(force: bool = False, embedder: Optional[DesignEmbedder] = None) -> dict:
    """埋め込みが必要なレコードをまとめて処理（バッチ処理）。

    embedder を渡すと常駐モデルを使い回す（ビューアからの登録で再読込しない）。
    """
    db.init_db()
    embedder = embedder or DesignEmbedder()
    model_name = embedder.model_name
    embed_version = embedder.embed_version

    with db.connect() as conn:
        targets = db.iter_sites_needing_embedding(
            conn, model_name, embed_version, force
        )

    if not targets:
        log.info("埋め込みが必要なレコードはありません（全て最新）")
        return {"embedded": 0, "skipped": 0, "failed": 0}

    log.info(
        "埋め込み対象: %d 件 / モデル=%s / version=%s / force=%s",
        len(targets), model_name, embed_version, force,
    )

    embedded, failed = 0, 0
    for i, row in enumerate(targets, start=1):
        site_id = row["id"]
        firstview = config.PROJECT_ROOT / row["firstview_path"]
        log.info("[%d/%d] 埋め込み中: %s", i, len(targets), row["url"])
        if not firstview.exists():
            log.error("firstview 画像が見つかりません（撮り直しが必要）: %s", firstview)
            failed += 1
            continue
        try:
            vec = embedder.encode_image(firstview)
            blob = embedding_to_blob(vec)
            with db.connect() as conn:
                db.update_embedding(
                    conn, site_id, blob, embedder.dim, model_name, embed_version
                )
            log.debug("保存: dim=%d", embedder.dim)
            embedded += 1
        except Exception as exc:  # noqa: BLE001
            log.error("埋め込み失敗: %s (%s)", row["url"], exc)
            failed += 1

    summary = {"embedded": embedded, "skipped": 0, "failed": failed}
    log.info("埋め込みサマリ: 完了=%d / 失敗=%d", embedded, failed)
    return summary
