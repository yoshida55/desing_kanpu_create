"""
録画（スクロール動画）から「動きの仕様書」をAIに書かせる（Phase 4 / mix & match B案）。

背景（なぜ）:
- CSSの @keyframes 走査（anim.py）では、今どきの主役である JS(GSAP等)の動きは取れない。
- そこで「コードを盗む」のではなく、録画をAIに見せて **動きを言葉＋数値で説明**させ、
  その"仕様書"を生成プロンプトに渡す＝著作権的に安全で、別業種にも流用しやすい。

流れ:
  録画webm --ffmpeg--> 数フレーム(JPEG) --Vision LLM--> 動きの仕様(JSON) --DB(motion_spec)

仕様書(JSON)の形:
  { "summary": "全体の動きの印象(日本語1〜2文)",
    "items": [ {"section","animation","trigger","duration","easing","stagger","note"} ... ],
    "css_hint": "再利用できるCSS/JSの当て方の短いヒント" }

ffmpeg はCLIを使う（環境にあることを ingest 段階で前提化）。無ければ分かるエラーを返す。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from . import config, db
from .utils import get_logger

log = get_logger("motion")


# ---- フレーム抽出（ffmpeg） -------------------------------------------------

def _even_pick(items: list, n: int) -> list:
    """リストから n 個を等間隔で選ぶ（少なければそのまま）。"""
    if len(items) <= n:
        return items
    step = (len(items) - 1) / (n - 1)
    return [items[round(i * step)] for i in range(n)]


def extract_frames(video_path: Path, site_id: str, n: int = 8) -> list[Path]:
    """録画動画から時系列に n 枚のフレームを取り出す（data/motion/<id>/frame_XX.jpg）。"""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg が見つかりません（PATHに通してください）。")
    out_dir = config.MOTION_DIR / site_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("frame_*.jpg"):
        f.unlink()
    for f in out_dir.glob("raw_*.jpg"):
        f.unlink()

    # 尺が分からなくても確実に拾えるよう、まず一定fpsで書き出してから間引く。
    tmp_pattern = str(out_dir / "raw_%03d.jpg")
    cmd = [
        "ffmpeg", "-loglevel", "error", "-i", str(video_path),
        "-vf", "fps=2,scale=768:-1", "-q:v", "4", "-y", tmp_pattern,
    ]
    try:
        subprocess.run(cmd, timeout=90, check=False)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"ffmpeg のフレーム抽出に失敗: {exc}") from exc

    raws = sorted(out_dir.glob("raw_*.jpg"))
    if not raws:
        return []
    frames: list[Path] = []
    for i, src in enumerate(_even_pick(raws, n)):
        dst = out_dir / f"frame_{i:02d}.jpg"
        src.replace(dst)
        frames.append(dst)
    for f in out_dir.glob("raw_*.jpg"):  # 選ばれなかった残りを掃除
        f.unlink()
    log.info("フレーム抽出: %d 枚 (%s)", len(frames), site_id)
    return frames


# ---- Vision LLM に動きを説明させる -----------------------------------------

_PROMPT = (
    "以下の画像は、あるWebサイトを上から下へスクロールして録画した連続フレームです（時系列順）。"
    "このサイトが使っている『動き・アニメーション』を読み取り、(1)人が動画を見ながら理解できるメモ と "
    "(2)別のAIに渡してそのまま再現させる指示 の両方を作ってください。"
    "コードの複製ではなく、動きの種類・きっかけ・速さ・イージング・ずらし(stagger)を推定して言葉と数値で表します。"
    "専門用語は使ってよいですが、terms に用語と意味を必ず添えてください（利用者が理解できるように）。"
    "必ず次のJSONだけを返してください（前置き・説明・```は不要）:\n"
    '{\n'
    '  "summary": "全体の動きの印象を日本語1〜2文で",\n'
    '  "items": [\n'
    '    {"section":"どの部分(例:ヒーロー/カード/見出し)",\n'
    '     "animation":"動きの種類(例:フェードアップ/スライドイン/ズーム/1文字ずつ)",\n'
    '     "trigger":"きっかけ(例:スクロールで表示/読み込み時/ホバー)",\n'
    '     "duration":"目安の長さ(例:0.8s)",\n'
    '     "easing":"イージング(例:ease-out)",\n'
    '     "stagger":"連続要素のずらし(例:0.1s。無ければ空文字)",\n'
    '     "area":[0.05,0.1,0.9,0.4],\n'
    '     "note":"補足(任意)"}\n'
    '  ],\n'
    '  "terms": [ {"term":"stagger等の専門用語", "meaning":"その意味を一言で"} ],\n'
    '  "reproduce": "別のAIにそのまま貼って渡せる再現指示。日本語で、各セクションの動き・きっかけ・秒数・easing・staggerを具体的に。CSS/素のJSで実装できる粒度で書く。"\n'
    '}\n'
    "※area は、その動きが画面上で起きるだいたいの場所。左上を0,0・右下を1,1として [x, y, 幅, 高さ] の割合で入れる。"
    "※JSONの中にコメント(# や //)は絶対に書かないこと。純粋なJSONだけを返す。"
)


def _extract_json(text: str) -> Optional[dict]:
    """AIの返答から最初の { … } を取り出してJSONにする。"""
    s = (text or "").strip()
    # ```json 等のフェンスや前置きは無視して、最初の { から最後の } だけを見る
    a = s.find("{")
    if a == -1:
        return None
    body = s[a:]
    b = body.rfind("}")
    if b != -1:
        try:
            return json.loads(body[:b + 1])
        except Exception:  # noqa: BLE001
            pass
    # 途中で切れた場合の救済：最後の完全な "}" までで切り、開いた [ { を閉じてから読む
    last = body.rfind("}")
    if last != -1:
        cand = body[:last + 1]
        cand += "]" * max(0, cand.count("[") - cand.count("]"))
        cand += "}" * max(0, cand.count("{") - cand.count("}"))
        try:
            return json.loads(cand)
        except Exception:  # noqa: BLE001
            return None
    return None


def _vision_describe(frames: list[Path], libs: list[str], url: str) -> dict:
    """フレーム列を Vision LLM に見せ、動きの仕様(JSON)を得る。Claude優先・Geminiで代替。"""
    from .vibe import _image_to_base64_jpeg  # 既存の縮小＋base64を再利用

    b64s = [_image_to_base64_jpeg(f, max_side=768) for f in frames]
    hint = f"\n参考: 検出したアニメ系ライブラリ = {', '.join(libs)}" if libs else ""
    text_prompt = _PROMPT + hint

    vcfg = config.CONFIG.vibe
    if vcfg.enabled:
        from anthropic import Anthropic

        client = Anthropic(api_key=vcfg.api_key)
        content: list = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
            for b in b64s
        ]
        content.append({"type": "text", "text": text_prompt})
        msg = client.messages.create(
            model=vcfg.model, max_tokens=3500,
            messages=[{"role": "user", "content": content}],
            timeout=150,
        )
        raw = "".join(
            getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text"
        )
    elif config.CONFIG.gemini.enabled:
        import urllib.request

        gcfg = config.CONFIG.gemini
        parts: list = [{"text": text_prompt}]
        for b in b64s:
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b}})
        body = {"contents": [{"parts": parts}]}
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{gcfg.model}:generateContent?key={gcfg.api_key}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
    else:
        raise RuntimeError(
            "ANTHROPIC_API_KEY も GEMINI_API_KEY も未設定です（.env にキーを入れてください）。"
        )

    spec = _extract_json(raw)
    if not spec:
        # 生JSONは画面に出さない。分かるメッセージにして再試行を促す。
        log.warning("動きJSONの解析に失敗（先頭200字）: %s", (raw or "")[:200])
        spec = {"summary": "動きの読み取りに成功しませんでした。もう一度「読み取る」を押してください。", "items": []}
    spec.setdefault("items", [])
    spec.setdefault("terms", [])
    spec.setdefault("reproduce", "")
    return spec


def describe_motion(site_id: str) -> dict:
    """録画からフレームを抜き、AIに動きを読み取らせて motion_spec に保存する。"""
    db.init_db()
    with db.connect() as conn:
        row = db.get_site(conn, site_id)
    if not row:
        raise RuntimeError("サイトが見つかりません。")
    vpath = row["animation_video_path"]
    if not vpath:
        raise RuntimeError("録画がありません。先にカード右下の『🎬動き』で録画してください。")
    video = config.PROJECT_ROOT / vpath
    if not video.exists():
        raise RuntimeError("録画ファイルが見つかりません（録り直してください）。")

    frames = extract_frames(video, site_id)
    if not frames:
        raise RuntimeError("録画からフレームを取り出せませんでした。")
    libs = [s.strip() for s in (row["animation_libs"] or "").split(",") if s.strip()]
    log.info("動きの読み取り開始: %s (frames=%d)", row["url"], len(frames))
    spec = _vision_describe(frames, libs, row["url"])
    with db.connect() as conn:
        db.update_motion(conn, site_id, json.dumps(spec, ensure_ascii=False))
    log.info("動きの仕様書を保存: %s", row["url"])
    return spec


def motion_to_prompt(spec: dict) -> str:
    """動きの仕様書を、カンプ生成でAIに渡す指定文に整える。"""
    if not spec:
        return ""
    lines: list[str] = []
    if spec.get("summary"):
        lines.append(f"- 全体の動き: {spec['summary']}")
    for it in (spec.get("items") or [])[:8]:
        seg = it.get("section", "")
        parts = [
            it.get("animation", ""),
            f"きっかけ:{it.get('trigger','')}" if it.get("trigger") else "",
            f"長さ:{it.get('duration','')}" if it.get("duration") else "",
            f"ease:{it.get('easing','')}" if it.get("easing") else "",
            f"stagger:{it.get('stagger','')}" if it.get("stagger") else "",
        ]
        detail = " / ".join(p for p in parts if p)
        lines.append(f"  ・{seg}: {detail}")
    if spec.get("reproduce"):
        lines.append(f"- 再現指示: {spec['reproduce']}")
    return "\n".join(lines)
