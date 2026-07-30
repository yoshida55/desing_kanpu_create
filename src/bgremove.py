"""アップロード画像の背景を除去して透過PNGにする（onnxruntime + U2-Net・ローカル・無料）。

★2026-07-30に rembg 経由をやめて onnxruntime を直接叩く方式に変えた（経緯）。
  rembg は import しただけで pymatting → numba を読み込む。numba の .pyd は署名が無いので
  Windows 11 の「スマートアプリコントロール」に弾かれ、
  `DLL load failed while importing _typeconv: アプリケーション制御ポリシーによって…` で必ず落ちた。
  numba が要るのは「フチをなめらかにする仕上げ」だけで、切り抜き本体（onnxruntime＋u2net.onnx）は
  どちらも正常に動く。＝rembgを通さなければ切り抜ける、というのがこの実装。
  ⚠ここを rembg に戻すと自宅PCで再発する（スマートアプリコントロールはオフにすると
  Windows再インストールしないと戻せないので、OS側を触る解決は採らない）。

モデル（u2net.onnx・約168MB）は rembg と同じ `~/.u2net/` に置く＝既にDL済みなら流用できる。
無ければ初回に自動ダウンロードする。重いのでセッションはプロセス内で1回だけ作って使い回す。
"""
from __future__ import annotations

import io
import logging
import os
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps

log = logging.getLogger(__name__)

# モデルは環境変数で差し替え可（既定 u2net＝汎用。人物特化なら u2net_human_seg）
_MODEL = os.getenv("DESIGN_STOCK_BGREMOVE_MODEL", "u2net")
_SESSION = None

# u2net の入力サイズ・正規化の値（U2-Net の学習時と同じにしないと精度が落ちる）
_SIZE = 320
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# モデルの置き場（rembg と同じ場所を見る＝あちらでDL済みのファイルをそのまま使える）
_MODEL_URLS = {
    "u2net": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx",
    "u2netp": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx",
    "u2net_human_seg": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net_human_seg.onnx",
    "isnet-general-use": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx",
}


def _model_dir() -> Path:
    """モデル置き場。U2NET_HOME があれば従う（rembg と同じ約束）。"""
    env = os.getenv("DESIGN_STOCK_BGREMOVE_DIR") or os.getenv("U2NET_HOME")
    return Path(env) if env else (Path.home() / ".u2net")


def _model_path() -> Path:
    env = os.getenv("DESIGN_STOCK_BGREMOVE_MODEL_PATH")
    if env:
        return Path(env)
    return _model_dir() / (_MODEL + ".onnx")


def _ensure_model() -> Path:
    """モデルファイルを用意する。無ければ公式リリースから落とす（初回だけ・約168MB）。"""
    path = _model_path()
    if path.exists() and path.stat().st_size > 1024 * 1024:
        return path
    url = _MODEL_URLS.get(_MODEL)
    if not url:
        raise RuntimeError(
            "背景を切り抜くAIモデルが見つかりません：" + str(path)
            + "（このモデル名は自動ダウンロードに対応していません。手で置いてください）"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".onnx.part")
    log.info("背景除去モデルをダウンロードします（初回のみ・約168MB）: %s", url)
    try:
        urllib.request.urlretrieve(url, tmp)      # noqa: S310  公式リリースの固定URL
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            "背景を切り抜くAIモデルのダウンロードに失敗しました（"
            + str(exc) + "）。ネットに繋がる状態でもう一度試してください"
        ) from exc
    log.info("背景除去モデルを保存しました: %s", path)
    return path


def available() -> bool:
    """切り抜きが使える状態か（画面側でボタンを出す/出さないの判定に使える）。

    モデルは自動DLできるので、ここでは onnxruntime が読めるかだけ見る。
    """
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return False
    return True


def _session():
    """onnxruntime のセッション（重いので1回だけ作って使い回す）。"""
    global _SESSION
    if _SESSION is None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError(
                "背景を切り抜く機能（onnxruntime）がこのPCに入っていません。"
                "コマンドで入れてください：venv\\Scripts\\python.exe -m pip install onnxruntime"
                "（入れたらサーバーを再起動してください）"
            ) from exc
        _SESSION = ort.InferenceSession(str(_ensure_model()))
    return _SESSION


def _mask_of(im: Image.Image) -> Image.Image:
    """写真から「残す所＝白／背景＝黒」の白黒マスクをAIに作らせる。"""
    sess = _session()
    small = im.resize((_SIZE, _SIZE), Image.LANCZOS)
    x = np.asarray(small, dtype=np.float32)
    x = x / max(float(x.max()), 1e-6)             # rembgと同じ「最大値で割る」正規化
    x = (x - _MEAN) / _STD
    x = x.transpose(2, 0, 1)[None].astype(np.float32)   # (1,3,320,320)

    outs = sess.run(None, {sess.get_inputs()[0].name: x})
    pred = np.asarray(outs[0])[0, 0]              # u2netは多段出力。先頭が本命
    lo, hi = float(pred.min()), float(pred.max())
    pred = (pred - lo) / max(hi - lo, 1e-6)       # 0〜1に伸ばす

    mask = Image.fromarray((pred * 255).astype(np.uint8), mode="L")
    return mask.resize(im.size, Image.LANCZOS)


def _smooth(mask: Image.Image) -> Image.Image:
    """フチのギザつき・薄いモヤを軽減する（rembgのpost_process_maskの代わり）。

    ★numbaを使う pymatting は踏めないので、①ほぼ透明/ほぼ不透明を振り切らせて
    背景に残る薄いモヤを消す ②境目だけを1pxぼかす、の2手で見た目を作る。
    """
    a = np.asarray(mask, dtype=np.float32) / 255.0
    lo, hi = 0.08, 0.92
    a = np.clip((a - lo) / (hi - lo), 0.0, 1.0)   # 端を振り切らせる＝モヤ消し
    out = Image.fromarray((a * 255).astype(np.uint8), mode="L")
    return out.filter(ImageFilter.GaussianBlur(radius=1.0))


def remove_background(src: Path) -> bytes:
    """src の画像を背景除去して、透過PNG（RGBA）のバイト列を返す。"""
    with Image.open(src) as raw:
        im = ImageOps.exif_transpose(raw).convert("RGB")   # スマホ写真の回転を直してから測る
    alpha = _smooth(_mask_of(im))

    # ★「主役が1つも見つからなかった」時は絵がほぼ全部消える。u2netは"主役を1つ選ぶ"AIなので、
    #   風景まるごとの絵・柄・背景素材のように主役が無いものは丸ごと透明になる（rembgでも同じ）。
    #   そのまま返すと「押したら絵が消えた」になるので、返さずに理由を言って止める。
    kept = float((np.asarray(alpha) > 128).mean())
    if kept < 0.02:
        raise RuntimeError(
            "この画像は切り抜けませんでした（主役が見つかりません）。"
            "背景から抜き出せるのは『人・物・生き物が1つはっきり写っているもの』です。"
            "風景まるごとの絵や背景素材のような画像には使えません"
        )

    out = im.convert("RGBA")
    out.putalpha(alpha)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()
