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

# ── サイトライブラリ（DB・スクショ・録画・抜き出し画像）の保存先 ──
# 既定はプロジェクト内 data/ 配下（従来通り）。
# DESIGN_STOCK_LIBRARY_DIR を .env に設定すると、そこに保存先を変更できる。
# 用途：ナレッジフォルダ(D:\50_knowledge 相当)のGit同期に乗せて、家↔会社でDBとスクショを共有する。
# .env はPCごとに別物（git対象外）なので、家と会社でそれぞれの実パスを書けばよい。
_library_dir_env = os.environ.get("DESIGN_STOCK_LIBRARY_DIR", "").strip()
LIBRARY_DIR = Path(_library_dir_env) if _library_dir_env else DATA_DIR

SCREENSHOT_DIR = LIBRARY_DIR / "screenshots"
VIDEO_DIR = LIBRARY_DIR / "videos"  # スクロール録画(アニメ参照用)
ASSET_DIR = LIBRARY_DIR / "assets"  # サイトから抜き出した画像
ANIM_DIR = LIBRARY_DIR / "anim"  # サイトから抜き出したアニメ素材(Lottie JSON等)
DB_PATH = LIBRARY_DIR / "design_stock.sqlite"

MOTION_DIR = DATA_DIR / "motion"  # 録画から抜いたフレーム(AIが動きを読み取る用・動画から再生成できるので同期不要)
UPLOAD_DIR = DATA_DIR / "uploads"  # ユーザーがアップロードした自前画像（カンプに使う）
CAMP_DIR = DATA_DIR / "camps"  # 生成したカンプHTML（従来通りこのGitリポジトリで同期）
RESULTS_HTML_PATH = PROJECT_ROOT / "results.html"
TEMPLATE_DIR = PROJECT_ROOT / "templates"


# ── DBに保存するパスの読み書き（LIBRARY_DIR対応） ──────────────
# DBは家↔会社で共有するため絶対パスは書けない。従来どおり「data\screenshots\…」形式で
# 記録し、実ファイルが LIBRARY_DIR（ナレッジフォルダ等）へ移動していても読み替えて解決する。


def data_rel_path(path) -> str:
    """DBへ書き込む相対パスを作る。LIBRARY_DIR配下のファイルも従来と同じ
    「data\\…」形式にする（読む側の resolve_data_path が実際の場所へ読み替える）。"""
    p = Path(path)
    try:
        return str(Path("data") / p.relative_to(LIBRARY_DIR))
    except ValueError:
        return str(p.relative_to(PROJECT_ROOT))


def resolve_data_path(rel) -> Path:
    """DBに保存された相対パス（例 data\\screenshots\\x.png）を実ファイルの場所へ解決する。
    プロジェクト内に無ければ、先頭の data\\ を LIBRARY_DIR に読み替えて探す。"""
    candidate = PROJECT_ROOT / rel
    if LIBRARY_DIR == DATA_DIR or candidate.exists():
        return candidate
    parts = Path(rel).parts
    if parts and parts[0] == "data":
        moved = LIBRARY_DIR.joinpath(*parts[1:])
        if moved.exists():
            return moved
    return candidate


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
    # 幅は1280（PCレイアウト維持）、高さを縦長にして"Webページらしい縦長"の録画にする
    video_w: int = 1280
    video_h: int = 1600
    video_scroll_steps: int = 16      # 上から下まで何回に分けてスクロールするか（多い=ゆっくり滑らか）
    video_step_pause_ms: int = 1000   # 1ステップごとの間（長い=アニメが見える。録画時間も伸びる）


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

    # "anthropic"（Claude）/ "openai"（GPT）/ "gemini"
    # provider＝最初のカンプ生成に使うエンジン
    provider: str = field(
        default_factory=lambda: os.environ.get("DESIGN_STOCK_HTML_PROVIDER", "anthropic")
    )
    # edit_provider＝生成後の「修正」に使うエンジン（未指定なら生成と同じ）
    # 修正は小さい差分なので安いGeminiに逃がす、といった使い分けができる。
    edit_provider: str = field(
        default_factory=lambda: os.environ.get(
            "DESIGN_STOCK_EDIT_PROVIDER",
            os.environ.get("DESIGN_STOCK_HTML_PROVIDER", "anthropic"),
        )
    )
    openai_api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    openai_model: str = field(
        default_factory=lambda: os.environ.get("DESIGN_STOCK_OPENAI_MODEL", "gpt-5.6-terra")
    )

    @property
    def openai_enabled(self) -> bool:
        key = self.openai_api_key.strip()
        return bool(key) and key.startswith("sk-") and "ここに" not in key


@dataclass(frozen=True)
class GeminiConfig:
    """画像の説明づけ（キャプション）用の Gemini。無料枠が使えて安上がり。"""

    api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""))
    model: str = field(
        default_factory=lambda: os.environ.get("DESIGN_STOCK_GEMINI_MODEL", "gemini-3.1-flash-lite")
    )

    @property
    def enabled(self) -> bool:
        key = self.api_key.strip()
        return bool(key) and "ここに" not in key


@dataclass(frozen=True)
class DeepSeekConfig:
    """DeepSeek（中国製・激安）。OpenAI互換APIなので base_url 差し替えで使う。

    修正はテキストのみなので相性が良い（画像は送らない）。生成は画像を渡せない前提。
    """

    api_key: str = field(default_factory=lambda: os.environ.get("DEEPSEEK_API_KEY", ""))
    model: str = field(
        default_factory=lambda: os.environ.get("DESIGN_STOCK_DEEPSEEK_MODEL", "deepseek-v4-flash")
    )
    base_url: str = field(
        default_factory=lambda: os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    )

    @property
    def enabled(self) -> bool:
        key = self.api_key.strip()
        return bool(key) and "ここに" not in key


@dataclass(frozen=True)
class ZaiConfig:
    """GLM（Zhipu / Z.ai）。OpenAI互換APIなので base_url 差し替えで使う。

    最新は glm-5.2（フラグシップ）。テキスト生成/修正向け（画像は送らない前提）。
    """

    api_key: str = field(default_factory=lambda: os.environ.get("ZAI_API_KEY", ""))
    model: str = field(
        default_factory=lambda: os.environ.get("DESIGN_STOCK_ZAI_MODEL", "glm-5.2")
    )
    base_url: str = field(
        default_factory=lambda: os.environ.get("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4/")
    )

    @property
    def enabled(self) -> bool:
        key = self.api_key.strip()
        return bool(key) and "ここに" not in key


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
    gemini: GeminiConfig = field(default_factory=GeminiConfig)
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    zai: ZaiConfig = field(default_factory=ZaiConfig)


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
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    ANIM_DIR.mkdir(parents=True, exist_ok=True)
    MOTION_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
