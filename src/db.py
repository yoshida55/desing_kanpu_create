"""
SQLite アクセス層。仕様 4.2 の `site` テーブルをそのまま実装する。

設計上の要点（仕様より）：
- 元画像(firstview/fullpage)は必ず保存 → 何度でも再埋め込みできる
- embed_model_name / embed_version / embed_dim を持つ → 古いレコードを判定できる
- アニメ系カラムは optional（後から欲しいサイトにだけ足す）
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from . import config
from .utils import get_logger

log = get_logger("db")

# 仕様 4.2 のスキーマをそのまま反映
_SCHEMA = """
CREATE TABLE IF NOT EXISTS site (
    id                   TEXT    PRIMARY KEY,        -- URLのハッシュ
    url                  TEXT    UNIQUE NOT NULL,    -- 本物へ飛ぶリンク
    captured_at          TEXT    NOT NULL,           -- 撮影日時(ISO)
    firstview_path       TEXT    NOT NULL,           -- 検索用(viewport)
    fullpage_path        TEXT    NOT NULL,           -- 見返す/分割用
    viewport_w           INTEGER NOT NULL,
    viewport_h           INTEGER NOT NULL,
    device_scale_factor  INTEGER NOT NULL,
    vibe_description     TEXT,                        -- 後でVision LLMが生成
    auto_tags            TEXT,                        -- 自動タグ(語彙固定)
    manual_tags          TEXT,
    image_embedding      BLOB,                        -- SigLIP-2 画像ベクトル(float32)
    embed_dim            INTEGER,                     -- 埋め込み次元(実測値)
    embed_model_name     TEXT,                        -- 差し替え検知用
    embed_version        TEXT,                        -- 再埋め込み判定用
    animation_status     TEXT    NOT NULL DEFAULT 'none',  -- none/video/extracted
    animation_video_path TEXT,
    animation_snippets   TEXT,
    animation_libs       TEXT
);
"""


@contextmanager
def connect(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """DB接続を開く。行は dict ライクに触れるようにする。"""
    path = db_path or config.DB_PATH
    config.ensure_dirs()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# 後から足したカラム（既存DBにも ALTER で足す）。name: 型 の形で書く。
_MIGRATIONS = {
    "vibe_embedding": "BLOB",  # 雰囲気描写文のSigLIPテキストベクトル(float32)
    "design_tokens": "TEXT",   # 配色・フォント・余白等のデザイントークン(JSON)
}


def init_db(db_path: Optional[Path] = None) -> None:
    """テーブルを用意する（無ければ作る）。何度呼んでも安全。"""
    with connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        # 既存DBに後付けカラムを足す（無いものだけ）
        existing = {r[1] for r in conn.execute("PRAGMA table_info(site)")}
        for name, coltype in _MIGRATIONS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE site ADD COLUMN {name} {coltype}")
                log.info("カラム追加: %s %s", name, coltype)
    log.info("DB を初期化しました: %s", db_path or config.DB_PATH)


def get_site(conn: sqlite3.Connection, site_id: str) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM site WHERE id = ?", (site_id,))
    return cur.fetchone()


def upsert_capture(conn: sqlite3.Connection, record: dict) -> None:
    """撮影結果を保存（同じidなら撮影系カラムを更新）。

    埋め込み系カラムは触らない＝撮り直しても embed の有無は別管理。
    ただし撮り直したら画像が変わるので embedding は無効化する。
    """
    # 任意項目（無ければ空文字）
    record = {"animation_libs": "", "design_tokens": "", **record}
    conn.execute(
        """
        INSERT INTO site (
            id, url, captured_at, firstview_path, fullpage_path,
            viewport_w, viewport_h, device_scale_factor, animation_status,
            animation_libs, design_tokens
        ) VALUES (
            :id, :url, :captured_at, :firstview_path, :fullpage_path,
            :viewport_w, :viewport_h, :device_scale_factor, 'none',
            :animation_libs, :design_tokens
        )
        ON CONFLICT(id) DO UPDATE SET
            url                 = excluded.url,
            captured_at         = excluded.captured_at,
            firstview_path      = excluded.firstview_path,
            fullpage_path       = excluded.fullpage_path,
            viewport_w          = excluded.viewport_w,
            viewport_h          = excluded.viewport_h,
            device_scale_factor = excluded.device_scale_factor,
            animation_libs      = excluded.animation_libs,
            design_tokens       = excluded.design_tokens,
            -- 画像が変わったので古い埋め込みは捨てる（再埋め込み対象になる）
            image_embedding     = NULL,
            embed_dim           = NULL,
            embed_model_name    = NULL,
            embed_version       = NULL
        """,
        record,
    )


def update_embedding(
    conn: sqlite3.Connection,
    site_id: str,
    blob: bytes,
    embed_dim: int,
    model_name: str,
    embed_version: str,
) -> None:
    conn.execute(
        """
        UPDATE site
        SET image_embedding = ?, embed_dim = ?,
            embed_model_name = ?, embed_version = ?
        WHERE id = ?
        """,
        (blob, embed_dim, model_name, embed_version, site_id),
    )


def iter_sites_needing_embedding(
    conn: sqlite3.Connection, model_name: str, embed_version: str, force: bool
) -> list[sqlite3.Row]:
    """埋め込みが必要なレコードを返す。

    force=True なら全件。そうでなければ：
    - まだ埋め込みが無い、または
    - モデル名/バージョンが今と違う（=古い）
    """
    if force:
        cur = conn.execute("SELECT * FROM site")
        return cur.fetchall()
    cur = conn.execute(
        """
        SELECT * FROM site
        WHERE image_embedding IS NULL
           OR embed_model_name IS NOT ?
           OR embed_version IS NOT ?
        """,
        (model_name, embed_version),
    )
    return cur.fetchall()


def iter_sites_with_embedding(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """埋め込み済みの全レコード（検索用に全件メモリへ載せる）。"""
    cur = conn.execute(
        "SELECT * FROM site WHERE image_embedding IS NOT NULL"
    )
    return cur.fetchall()


def update_animation(
    conn: sqlite3.Connection, site_id: str, video_path: str, status: str = "video"
) -> None:
    """スクロール録画(アニメ参照)の保存先と状態を記録する。"""
    conn.execute(
        "UPDATE site SET animation_video_path = ?, animation_status = ? WHERE id = ?",
        (video_path, status, site_id),
    )


def update_libraries(conn: sqlite3.Connection, site_id: str, libs: str) -> None:
    """検出したアニメ系ライブラリ名（カンマ区切り）を記録する。"""
    conn.execute(
        "UPDATE site SET animation_libs = ? WHERE id = ?", (libs, site_id)
    )


def update_tokens(conn: sqlite3.Connection, site_id: str, tokens_json: str) -> None:
    """抽出したデザイントークン(JSON文字列)を記録する。"""
    conn.execute(
        "UPDATE site SET design_tokens = ? WHERE id = ?", (tokens_json, site_id)
    )


def update_anim_snippets(
    conn: sqlite3.Connection, site_id: str, snippets_json: str
) -> None:
    """抜き出したアニメ素材(@keyframes/transition/Lottie等・JSON文字列)を記録する。

    animation_status は録画と共用のため、ここでは触らない（録画状態を壊さない）。
    """
    conn.execute(
        "UPDATE site SET animation_snippets = ? WHERE id = ?", (snippets_json, site_id)
    )


def update_vibe(
    conn: sqlite3.Connection, site_id: str, description: str, embedding_blob: bytes
) -> None:
    """雰囲気描写文と、そのテキストベクトルを記録する。"""
    conn.execute(
        "UPDATE site SET vibe_description = ?, vibe_embedding = ? WHERE id = ?",
        (description, embedding_blob, site_id),
    )


def iter_sites_needing_vibe(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """まだ雰囲気描写文が無いレコード（撮影済みのものだけ）。"""
    cur = conn.execute(
        "SELECT * FROM site WHERE vibe_description IS NULL OR vibe_description = ''"
    )
    return cur.fetchall()


def delete_site(conn: sqlite3.Connection, site_id: str) -> Optional[dict]:
    """サイトを1件削除する。削除した行のスクショ相対パスを返す（画像の後始末用）。

    存在しなければ None。画像ファイルの削除は呼び出し側で行う
    （DB操作とファイル操作を混ぜない＝後始末を呼び出し側が制御できる）。
    """
    row = get_site(conn, site_id)
    if row is None:
        return None
    paths = {
        "firstview_path": row["firstview_path"],
        "fullpage_path": row["fullpage_path"],
        "animation_video_path": row["animation_video_path"],
        "url": row["url"],
    }
    conn.execute("DELETE FROM site WHERE id = ?", (site_id,))
    return paths


def count_sites(conn: sqlite3.Connection) -> tuple[int, int]:
    """(全件数, 埋め込み済み件数) を返す。"""
    total = conn.execute("SELECT COUNT(*) FROM site").fetchone()[0]
    embedded = conn.execute(
        "SELECT COUNT(*) FROM site WHERE image_embedding IS NOT NULL"
    ).fetchone()[0]
    return total, embedded
