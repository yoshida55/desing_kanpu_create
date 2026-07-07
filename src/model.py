"""
SigLIP-2 のラッパー。仕様 4.3 の方針：

- モデルIDは設定値（差し替え可能）
- 次元数はハードコードせず、埋め込み時に実測した値を返す
- 出力は L2 正規化（→ 内積がコサイン類似度になる）
- 画像エンコーダ / テキストエンコーダの両方を持つ（検索 2系統に対応）

transformers / torch は重いので、このモジュールを import した時点では読み込まない。
DesignEmbedder() を作って .load() したとき（または初回エンコード時）に読み込む。
"""

from __future__ import annotations

import os
import threading

# ★ torch を import する前に設定する（Windows対策・絶対にここで）。
#   torch同梱のOpenMP(libiomp5md.dll)が numpy/MKL 等の別OpenMPと二重ロードされると、
#   モデル読込中にセグメンテーション違反で落ちる（ロード順依存で不定期に発生）。
#   この環境変数で二重ロードを許可するとクラッシュを回避できる。
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

from pathlib import Path
from typing import Optional

import numpy as np

from . import config
from .utils import get_logger

log = get_logger("model")


def _resolve_device(requested: str) -> str:
    """"auto"/"cuda"/"cpu" を実際のデバイス文字列に解決する。"""
    import torch

    if requested == "cpu":
        return "cpu"
    if requested == "cuda":
        if not torch.cuda.is_available():
            log.warning("cuda 指定ですが GPU が見つかりません → cpu にフォールバック")
            return "cpu"
        return "cuda"
    # auto
    if torch.cuda.is_available():
        log.info("GPU を検出しました → cuda を使用")
        return "cuda"
    log.info("GPU が無いため cpu を使用")
    return "cpu"


class DesignEmbedder:
    """画像/テキストを同じ空間のベクトルに変換する。"""

    def __init__(self, cfg: Optional[config.EmbedConfig] = None) -> None:
        self.cfg = cfg or config.CONFIG.embed
        self._model = None
        self._processor = None
        self._device: Optional[str] = None
        self._dim: Optional[int] = None
        # 二重読み込み防止（バックグラウンド先読みと初回検索が同時に走った時、
        # 2つ同時に読むとメモリピークが倍＝このPCでは即クラッシュするため）
        self._load_lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self.cfg.model_name

    @property
    def embed_version(self) -> str:
        return self.cfg.embed_version

    @property
    def dim(self) -> Optional[int]:
        """実測した埋め込み次元（まだエンコードしていなければ None）。"""
        return self._dim

    def load(self) -> None:
        """モデルとプロセッサを読み込む（数秒〜・初回はDLが走る）。

        鍵付き＝同時に呼ばれても実際に読むのは1回だけ（後から来た方は完了を待つ）。
        """
        if self._model is not None:
            return
        with self._load_lock:
            self._load_inner()

    def _load_inner(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoModel, AutoProcessor

        self._device = _resolve_device(self.cfg.device)
        # 読み込む精度を決める：
        # - GPU(cuda) → float16（速い・省VRAM）
        # - CPU       → bfloat16（メモリ半減。fp32だと約4.3GBで、ページングファイルが
        #               小さいPCではコミット不足でクラッシュするため）
        dtype = torch.float16 if self._device == "cuda" else torch.bfloat16
        log.info(
            "モデルを読み込みます: %s (device=%s / dtype=%s)",
            self.model_name, self._device, dtype,
        )
        # dtype を渡して最初から低精度で読む＝読込ピークのメモリを抑える。
        # （transformers 5.x は torch_dtype が非推奨で dtype が新名）
        # 一度DL済みなら、まずローカルキャッシュだけで読む（毎回のHub通信を省く＝速い）。
        load_kwargs = {"dtype": dtype, "low_cpu_mem_usage": True}
        try:
            self._processor = AutoProcessor.from_pretrained(
                self.model_name, local_files_only=True
            )
            self._model = AutoModel.from_pretrained(
                self.model_name, local_files_only=True, **load_kwargs
            )
            log.debug("ローカルキャッシュから読み込みました（オフライン）")
        except Exception:
            log.info("キャッシュが無いためHubからダウンロードします（初回のみ）")
            self._processor = AutoProcessor.from_pretrained(self.model_name)
            self._model = AutoModel.from_pretrained(self.model_name, **load_kwargs)
        self._model = self._model.to(self._device)
        self._model.eval()
        torch.set_grad_enabled(False)
        log.info("モデル読み込み完了")

    def encode_image(self, image_path: Path) -> np.ndarray:
        """画像1枚 → L2正規化済みベクトル(float32)。"""
        self.load()
        from PIL import Image

        image = Image.open(image_path).convert("RGB")
        inputs = self._processor(images=image, return_tensors="pt").to(self._device)
        feats = self._model.get_image_features(**inputs)
        return self._postprocess(feats)

    def encode_text(self, text: str) -> np.ndarray:
        """テキスト1件 → L2正規化済みベクトル(float32)。

        SigLIP は text 側に padding='max_length' が必要。
        """
        self.load()
        inputs = self._processor(
            text=[text],
            return_tensors="pt",
            padding="max_length",
            truncation=True,
        ).to(self._device)
        feats = self._model.get_text_features(**inputs)
        return self._postprocess(feats)

    def _postprocess(self, feats) -> np.ndarray:
        """モデル出力 → float32 numpy(1次元) → L2正規化。次元を実測して記録。

        transformers のバージョンで get_*_features の戻り値が変わる：
        - 旧API: 形 (1, dim) のテンソルを返す
        - 新API(5.x): pooler_output を持つ出力オブジェクトを返す
        どちらでも (dim,) の1本のベクトルに正規化して取り出す。
        """
        import torch

        # 新API：pooler_output（プールされた埋め込み）を優先して使う
        pooled = getattr(feats, "pooler_output", None)
        tensor = pooled if pooled is not None else feats

        with torch.no_grad():
            arr = tensor.float().cpu().numpy().astype(np.float32)
        # (1, dim) のバッチ次元を落として (dim,) の1本にする
        vec = np.squeeze(arr)
        if vec.ndim != 1:
            raise ValueError(f"埋め込みの形が想定外です: {arr.shape}")
        self._dim = int(vec.shape[0])
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec
