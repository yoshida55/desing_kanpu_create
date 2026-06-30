"""
雰囲気描写文ハイブリッド（仕様 Phase 3）。

ねらい：SigLIP は「色・余白・レイアウト」など具体的な見た目は得意だが、
「高級感」「上品」「信頼感」などの抽象的な雰囲気は苦手。
そこで Vision LLM(Claude) にスクショを見せて雰囲気を文章で説明させ、
その文章も SigLIP のテキストエンコーダでベクトル化して持つ。
検索時に「画像ベクトル」＋「雰囲気文ベクトル」の両方で照合する（ハイブリッド）。

APIキーは .env の ANTHROPIC_API_KEY（config.VibeConfig 経由）。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Optional

from . import config, db
from .model import DesignEmbedder
from .utils import embedding_to_blob, get_logger

log = get_logger("vibe")

# 検索しやすい簡潔な1段落を書かせる指示（見出しや箇条書きは付けさせない）
_PROMPT = (
    "あなたはWebデザインの目利きです。この画像はWebサイトのファーストビューです。"
    "このサイトの『雰囲気・印象』を、日本語の1段落（120字程度）で説明してください。"
    "色調・余白の取り方・フォントの印象・全体から受ける印象（高級感／上品／親しみやすさ／"
    "信頼感／先進的／ポップ等）を、検索で当てやすい言葉で盛り込んでください。"
    "見出し・箇条書き・前置きは書かず、説明文だけを返すこと。"
)


def _image_to_base64_jpeg(image_path: Path, max_side: int = 1024) -> str:
    """画像を縮小してJPEGのbase64に（送信コストを抑える）。"""
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def generate_vibe_description(image_path: Path) -> str:
    """スクショ1枚 → Claude が雰囲気を1段落で説明した文章を返す。"""
    cfg = config.CONFIG.vibe
    if not cfg.enabled:
        raise RuntimeError(
            "ANTHROPIC_API_KEY が未設定です（.env にキーを貼ってください）"
        )
    from anthropic import Anthropic

    b64 = _image_to_base64_jpeg(image_path)
    client = Anthropic(api_key=cfg.api_key)
    msg = client.messages.create(
        model=cfg.model,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
    )
    return msg.content[0].text.strip()


def describe_one(
    site_id: str, embedder: Optional[DesignEmbedder] = None
) -> Optional[str]:
    """1サイト：雰囲気文を生成 → SigLIPでテキスト埋め込み → DBに保存。"""
    db.init_db()
    embedder = embedder or DesignEmbedder()
    with db.connect() as conn:
        row = db.get_site(conn, site_id)
    if not row:
        log.warning("見つかりません: %s", site_id)
        return None

    firstview = config.PROJECT_ROOT / row["firstview_path"]
    if not firstview.exists():
        log.error("firstview がありません: %s", firstview)
        return None

    log.info("雰囲気を言語化中: %s", row["url"])
    description = generate_vibe_description(firstview)
    # 雰囲気文を SigLIP テキストエンコーダでベクトル化（画像と同じ空間）
    vec = embedder.encode_text(description)
    with db.connect() as conn:
        db.update_vibe(conn, site_id, description, embedding_to_blob(vec))
    log.info("雰囲気文を保存: %s", row["url"])
    log.debug("内容: %s", description)
    return description


def describe_all(embedder: Optional[DesignEmbedder] = None) -> dict:
    """まだ雰囲気文が無いサイトをまとめて言語化（バッチ処理）。"""
    db.init_db()
    embedder = embedder or DesignEmbedder()
    with db.connect() as conn:
        targets = db.iter_sites_needing_vibe(conn)
    if not targets:
        log.info("雰囲気文が必要なサイトはありません")
        return {"described": 0, "failed": 0}

    described, failed = 0, 0
    for i, row in enumerate(targets, start=1):
        log.info("[%d/%d] %s", i, len(targets), row["url"])
        try:
            describe_one(row["id"], embedder=embedder)
            described += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.error("失敗: %s (%s)", row["url"], exc)
    log.info("雰囲気文サマリ: 生成=%d / 失敗=%d", described, failed)
    return {"described": described, "failed": failed}
