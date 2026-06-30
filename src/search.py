"""
検索（search）。仕様 4.4 / 4.5 を実装する。

2系統：
- text → image（本命）：日本語クエリをそのまま使える
- image → image       ：参照画像に似たものを芋づる式に

類似度：保存時にL2正規化済みなので「正規化ベクトル同士の内積（=cosine）」。
保存・計算：個人規模なので専用ベクトルDBは使わず、全件メモリに載せて numpy で総当たり。

結果表示：results.html（静的HTML）を出力 → ブラウザで開く。
"""

from __future__ import annotations

import html
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from . import config, db
from .model import DesignEmbedder
from .utils import blob_to_embedding, get_logger

log = get_logger("search")


@dataclass
class SearchHit:
    """検索ヒット1件。カード表示に必要な情報を持つ。"""

    rank: int
    score: float
    url: str
    firstview_path: str
    captured_at: str
    site_id: str = ""  # 「これに似たの」ボタン用（image→image を辿る）

    def to_dict(self) -> dict:
        """ビューア(JSON API)に渡しやすい形にする。"""
        return {
            "rank": self.rank,
            "score": round(self.score, 4),
            "url": self.url,
            "captured_at": self.captured_at,
            "site_id": self.site_id,
        }


def _load_matrix(conn) -> tuple[np.ndarray, list]:
    """埋め込み済み全件を (行列, 行メタ) として読み込む。

    返り値の行列は (件数, 次元)。各行は保存時にL2正規化済み。
    """
    rows = db.iter_sites_with_embedding(conn)
    if not rows:
        return np.empty((0, 0), dtype=np.float32), []
    vectors = [blob_to_embedding(r["image_embedding"]) for r in rows]
    matrix = np.vstack(vectors).astype(np.float32)
    return matrix, list(rows)


def _rank(query_vec: np.ndarray, matrix: np.ndarray, rows: list, top_n: int) -> list[SearchHit]:
    """クエリベクトルと全件の内積を取って上位 top_n を返す。"""
    # query_vec も matrix も L2正規化済み → 内積 = cosine
    scores = matrix @ query_vec  # 形状: (件数,)
    order = np.argsort(-scores)[:top_n]
    hits: list[SearchHit] = []
    for rank, idx in enumerate(order, start=1):
        row = rows[idx]
        hits.append(
            SearchHit(
                rank=rank,
                score=float(scores[idx]),
                url=row["url"],
                firstview_path=row["firstview_path"],
                captured_at=row["captured_at"],
                site_id=row["id"],
            )
        )
    return hits


def _zscore(x: np.ndarray) -> np.ndarray:
    """候補内でスコアを標準化（平均0・分散1）。画像と雰囲気文の尺度差を吸収する。"""
    x = np.asarray(x, dtype=np.float32)
    std = x.std()
    if std == 0:
        return x - x.mean()
    return (x - x.mean()) / std


def search_by_text(
    query: str,
    top_n: Optional[int] = None,
    embedder: Optional[DesignEmbedder] = None,
) -> list[SearchHit]:
    """text → image 検索（雰囲気文があればハイブリッド）。

    - 画像ベクトルとの一致（具体的な見た目）
    - 雰囲気文ベクトルとの一致（高級感などの抽象的な雰囲気）
    の2つを候補内で標準化して重み付き合算する。雰囲気文が無いサイトは画像のみ。
    """
    top_n = top_n or config.CONFIG.search.top_n
    vcfg = config.CONFIG.vibe
    embedder = embedder or DesignEmbedder()
    query_vec = embedder.encode_text(query)

    with db.connect() as conn:
        rows = db.iter_sites_with_embedding(conn)
    if not rows:
        log.warning("埋め込み済みのレコードがありません。先に ingest → embed を実行してください")
        return []

    img_mat = np.vstack([blob_to_embedding(r["image_embedding"]) for r in rows]).astype(np.float32)
    img_scores = img_mat @ query_vec  # text→image

    # 雰囲気文ベクトル（持っている行だけ）
    vibe_scores = np.full(len(rows), np.nan, dtype=np.float32)
    for i, r in enumerate(rows):
        blob = r["vibe_embedding"] if "vibe_embedding" in r.keys() else None
        if blob:
            vibe_scores[i] = float(blob_to_embedding(blob) @ query_vec)
    has_vibe = np.isfinite(vibe_scores).any()

    if has_vibe and vcfg.weight_vibe > 0:
        z_img = _zscore(img_scores)
        z_vibe = np.zeros(len(rows), dtype=np.float32)
        mask = np.isfinite(vibe_scores)
        if mask.sum() > 1:
            z_vibe[mask] = _zscore(vibe_scores[mask])
        final = vcfg.weight_image * z_img + vcfg.weight_vibe * z_vibe
        n_vibe = int(mask.sum())
        log.info("text検索(ハイブリッド): '%s' / 対象 %d 件（雰囲気文 %d 件）", query, len(rows), n_vibe)
    else:
        final = img_scores
        log.info("text検索: '%s' / 対象 %d 件", query, len(rows))

    order = np.argsort(-final)[:top_n]
    hits: list[SearchHit] = []
    for rank, idx in enumerate(order, start=1):
        r = rows[idx]
        hits.append(
            SearchHit(
                rank=rank,
                score=float(final[idx]),
                url=r["url"],
                firstview_path=r["firstview_path"],
                captured_at=r["captured_at"],
                site_id=r["id"],
            )
        )
    return hits


def search_by_image(
    image_path: Path,
    top_n: Optional[int] = None,
    embedder: Optional[DesignEmbedder] = None,
) -> list[SearchHit]:
    """image → image 検索。embedder を渡すと常駐モデルを使い回す。"""
    top_n = top_n or config.CONFIG.search.top_n
    embedder = embedder or DesignEmbedder()
    query_vec = embedder.encode_image(Path(image_path))
    with db.connect() as conn:
        matrix, rows = _load_matrix(conn)
    if matrix.shape[0] == 0:
        log.warning("埋め込み済みのレコードがありません。先に ingest → embed を実行してください")
        return []
    log.info("image検索: '%s' / 対象 %d 件", image_path, matrix.shape[0])
    return _rank(query_vec, matrix, rows, top_n)


def search_similar_to_site(site_id: str, top_n: Optional[int] = None) -> list[SearchHit]:
    """登録済みサイトに似たものを探す（image→image）。

    ★モデル不要：そのサイトの保存済みベクトルをそのままクエリに使う（速い）。
    自分自身は結果から除く。
    """
    top_n = top_n or config.CONFIG.search.top_n
    with db.connect() as conn:
        matrix, rows = _load_matrix(conn)
    if matrix.shape[0] == 0:
        return []
    # 起点サイトの行を探す
    idx = next((i for i, r in enumerate(rows) if r["id"] == site_id), None)
    if idx is None:
        log.warning("似た検索の起点が見つかりません: %s", site_id)
        return []
    query_vec = matrix[idx]  # 保存時にL2正規化済み
    # 自分自身を除くため、起点行は除外して順位付け
    hits = _rank(query_vec, matrix, rows, top_n + 1)
    return [h for h in hits if h.site_id != site_id][:top_n]


def render_results_html(hits: list[SearchHit], query_label: str) -> Path:
    """検索結果を静的HTMLに書き出す。カード = スクショ+スコア+URL+撮影日。

    画像パスはHTMLファイル(プロジェクトルート)からの相対なのでそのまま使える。
    """
    out_path = config.RESULTS_HTML_PATH
    cards = []
    for hit in hits:
        safe_url = html.escape(hit.url)
        safe_img = html.escape(hit.firstview_path.replace("\\", "/"))
        safe_date = html.escape(hit.captured_at)
        cards.append(
            f"""
      <article class="result_item">
        <a class="result_link" href="{safe_url}" target="_blank" rel="noopener noreferrer">
          <!-- 検索用ファーストビュー（押すと実物へ飛び、動き・アニメを確認できる） -->
          <img class="result_img" src="{safe_img}" alt="サイトのファーストビュー" loading="lazy">
        </a>
        <div class="result_meta">
          <span class="result_score">{hit.score:.4f}</span>
          <span class="result_date">{safe_date}</span>
        </div>
        <a class="result_url" href="{safe_url}" target="_blank" rel="noopener noreferrer">{safe_url}</a>
      </article>"""
        )

    cards_html = "\n".join(cards) if cards else '<p class="result_empty">ヒットなし</p>'
    safe_label = html.escape(query_label)

    # 命名規約に沿ったクラス名（2単語・アンスコ）でHTML/CSSを書く
    document = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>デザイン検索結果</title>
  <style>
    /* ┌─────────────────────────────────────────┐
       │ 共通                                       │
       └─────────────────────────────────────────┘ */
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: system-ui, "Segoe UI", "Hiragino Kaku Gothic ProN", sans-serif;
      background: #f5f5f7;
      color: #1d1d1f;
    }}

    /* ┌─────────────────────────────────────────┐
       │ 検索結果                                   │
       └─────────────────────────────────────────┘ */
    .result_head {{
      padding: 2.4rem 3.2rem;
      background: #ffffff;
      border-bottom: 1px solid #e0e0e0;
    }}
    .result_query {{ font-size: 1.4rem; color: #6e6e73; }}
    .result_query strong {{ color: #1d1d1f; }}

    /* グリッドで並べる親（あとで列数を変えやすいよう手法は名前に入れない） */
    .result_list {{
      display: grid; /* カードを横並び→折り返し */
      grid-template-columns: repeat(auto-fill, minmax(32rem, 1fr));
      gap: 2.4rem;
      padding: 3.2rem;
    }}

    /* カード1枚分 */
    .result_item {{
      background: #ffffff;
      border-radius: 1.2rem;
      overflow: hidden; /* 画像の角を丸める */
      box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
    }}
    .result_link {{ display: block; }}
    .result_img {{
      width: 100%;
      aspect-ratio: 1440 / 900; /* 撮影viewport比 */
      object-fit: cover;
      object-position: top; /* ファーストビューなので上端を見せる */
      display: block;
    }}
    .result_meta {{
      display: flex; /* スコアと日付を左右に */
      justify-content: space-between;
      padding: 1.2rem 1.6rem 0.4rem;
      font-size: 1.2rem;
    }}
    .result_score {{
      font-weight: 700;
      color: #0066cc;
    }}
    .result_date {{ color: #86868b; }}
    .result_url {{
      display: block;
      padding: 0.4rem 1.6rem 1.6rem;
      font-size: 1.1rem;
      color: #6e6e73;
      word-break: break-all;
      text-decoration: none;
    }}
    .result_empty {{ padding: 3.2rem; color: #86868b; }}
  </style>
</head>
<body>
  <header class="result_head">
    <p class="result_query">検索クエリ: <strong>{safe_label}</strong> ／ ヒット {len(hits)} 件</p>
  </header>

  <!-- 検索結果カード一覧 -->
  <main class="result_list">
{cards_html}
  </main>
</body>
</html>
"""
    out_path.write_text(document, encoding="utf-8")
    log.info("結果HTMLを書き出しました: %s", out_path)
    return out_path


def search_and_show(
    query: Optional[str] = None,
    image: Optional[str] = None,
    top_n: Optional[int] = None,
    open_browser: bool = True,
) -> Path:
    """検索 → results.html 出力 → ブラウザで開く（任意）。"""
    if image:
        hits = search_by_image(Path(image), top_n=top_n)
        label = f"画像: {Path(image).name}"
    elif query:
        hits = search_by_text(query, top_n=top_n)
        label = query
    else:
        raise ValueError("query か image のどちらかを指定してください")

    for hit in hits:
        log.info("  #%02d  %.4f  %s", hit.rank, hit.score, hit.url)

    out_path = render_results_html(hits, label)
    if open_browser:
        webbrowser.open(out_path.as_uri())
    return out_path
