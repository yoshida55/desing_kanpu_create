"""
設定値を1か所に集約するモジュール。

仕様の方針：
- モデルIDは設定値（ハードコードしない）。次元数は埋め込み時に実測して記録する。
- 撮影条件は固定（似た画像検索の精度がブレないように）。
- パスはここから組み立てる（呼び出し側で散らかさない）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# ── プロジェクトの基準パス ───────────────────────────────
# このファイル(src/config.py)の2つ上 = プロジェクトルート
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
VIDEO_DIR = DATA_DIR / "videos"  # スクロール録画(アニメ参照用)
ASSET_DIR = DATA_DIR / "assets"  # サイトから抜き出した画像
CAMP_DIR = DATA_DIR / "camps"  # 生成したカンプHTML
DB_PATH = DATA_DIR / "design_stock.sqlite"
RESULTS_HTML_PATH = PROJECT_ROOT / "results.html"
TEMPLATE_DIR = PROJECT_ROOT / "templates"


@dataclass(frozen=True)
class CaptureConfig:
    """撮影条件。仕様 4.1 に従い固定する。"""

    viewport_w: int = 1440
    viewport_h: int = 900
    device_scale_factor: int = 2
    # User-Agent を固定（headless ばれ＆描画差を減らす）
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    # ページ読み込み完了の待ち時間(ms)とアイドル待ち
    nav_timeout_ms: int = 45_000
    # JS描画サイトで通信が落ち着くのを待つ上限（来なければ諦めて撮る）
    networkidle_timeout_ms: int = 8_000
    settle_after_load_ms: int = 1_800  # 描画が落ち着くまでの保険待ち
    headless: bool = True
    # bot判定の壁を撮ったら、実ブラウザ（非headless）で自動的に撮り直すか
    retry_non_headless_on_bot_wall: bool = True
    # 非headlessでbot壁(Cloudflare等)が解けるのを待つ上限（秒）
    bot_wall_clear_wait_s: int = 15
    # アニメ参照用のスクロール録画の設定（オンデマンドで欲しいサイトだけ）
    video_w: int = 1280
    video_h: int = 720
    video_scroll_steps: int = 12      # 上から下まで何回に分けてスクロールするか
    video_step_pause_ms: int = 700    # 1ステップごとの間（アニメが見える間）


@dataclass(frozen=True)
class EmbedConfig:
    """埋め込みモデル設定。モデルは抽象化して持つ（差し替え可能に）。"""

    # モデルIDは設定値。次元数はハードコードせず実測する。
    model_name: str = os.environ.get(
        "DESIGN_STOCK_MODEL", "google/siglip2-so400m-patch14-384"
    )
    # 再埋め込み判定用のバージョン文字列（前処理を変えたら上げる）
    embed_version: str = "v1"
    # "auto" / "cuda" / "cpu"
    device: str = os.environ.get("DESIGN_STOCK_DEVICE", "auto")


@dataclass(frozen=True)
class VibeConfig:
    """雰囲気描写文（Vision LLM）の設定。"""

    # APIキーは .env から読む（コードに直書きしない）。
    # default_factory にすることで、インスタンス生成のたびに最新の環境変数を読む
    # （設定画面で保存→reload した時に反映できる）。
    api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    model: str = field(
        default_factory=lambda: os.environ.get("DESIGN_STOCK_VIBE_MODEL", "claude-opus-4-8")
    )

    # ハイブリッド検索の重み（画像 : 雰囲気文）。合計1.0でなくてもよい（後で正規化）。
    weight_image: float = 0.5
    weight_vibe: float = 0.5

    @property
    def enabled(self) -> bool:
        """キーが入っていれば有効。プレースホルダのままなら無効扱い。"""
        key = self.api_key.strip()
        return bool(key) and key.startswith("sk-ant-") and "ここに" not in key


@dataclass(frozen=True)
class HtmlGenConfig:
    """カンプHTML生成に使うLLM（Claude / OpenAI を切替）。"""

    # "anthropic"（Claude）/ "openai"（GPT）
    provider: str = field(
        default_factory=lambda: os.environ.get("DESIGN_STOCK_HTML_PROVIDER", "anthropic")
    )
    openai_api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    openai_model: str = field(
        default_factory=lambda: os.environ.get("DESIGN_STOCK_OPENAI_MODEL", "gpt-5.4")
    )

    @property
    def openai_enabled(self) -> bool:
        key = self.openai_api_key.strip()
        return bool(key) and key.startswith("sk-") and "ここに" not in key


@dataclass(frozen=True)
class SearchConfig:
    """検索の既定値。"""

    top_n: int = 24


@dataclass(frozen=True)
class AppConfig:
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    embed: EmbedConfig = field(default_factory=EmbedConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    vibe: VibeConfig = field(default_factory=VibeConfig)
    htmlgen: HtmlGenConfig = field(default_factory=HtmlGenConfig)


# どこからでも import して使う共有インスタンス
CONFIG = AppConfig()


def reload() -> None:
    """環境変数を読み直して CONFIG を作り直す（設定画面で保存した後に呼ぶ）。"""
    global CONFIG
    CONFIG = AppConfig()


ENV_PATH = PROJECT_ROOT / ".env"


def update_env_file(updates: dict) -> None:
    """.env の指定キーを更新（無ければ追記）。他の行はそのまま残す。

    あわせて os.environ も更新するので、reload() すれば即反映できる。
    値が空文字の項目は「変更しない」とみなしてスキップする（キー消し防止）。
    """
    updates = {k: v for k, v in updates.items() if v not in (None, "")}
    if not updates:
        return
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    done = set()
    out = []
    for line in lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f"{key}={updates[key]}")
            done.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in done:
            out.append(f"{key}={val}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    # 動いているプロセスにも反映
    for key, val in updates.items():
        os.environ[key] = str(val)


def ensure_dirs() -> None:
    """データ用ディレクトリを用意する（無ければ作る）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
