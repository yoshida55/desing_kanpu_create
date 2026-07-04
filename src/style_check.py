"""おしゃれ度チェック（納品前の最終QC）。

完成カンプの全体スクショを Vision AI に見せて、有名サイト（Apple/Stripe/Linear等）基準で
シビアに採点＋"具体的で効果の大きい改善点"を出させる。人の「なんとなくダサい」を
プロの観点（余白・字組み・配色・階層・一貫性・画像の品質）に分解するのが狙い。

★これは評価だけ（AIは直さない）。改善点を見て、右クリック編集やAI修正で手を入れる前提。
★画像を送るので Vision対応の Claude / GPT / Gemini を使う（DeepSeek等テキスト専用は不可→Claudeへ）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from . import camp, config
from .utils import get_logger

log = get_logger("style_check")

_SYSTEM = (
    "あなたは Apple / Stripe / Linear / Vercel クラスの受賞歴あるプロのWebデザイナー。"
    "渡されたランディングページの全体スクリーンショットを、プロの目でシビアに評価する。"
    "甘い点は付けない（「有名サイト級」を10点として辛口に）。日本語で答える。"
    "評価軸ごとに1〜10で採点し、「有名サイト級」に近づけるための、具体的で効果の大きい改善点を挙げる。"
    "返答は次のJSONのみ（前置き・マークダウン・コードフェンス禁止）：\n"
    '{"overall": 数値(1-10), '
    '"scores": {"whitespace": 数値, "typography": 数値, "color": 数値, "hierarchy": 数値, "consistency": 数値, "imagery": 数値}, '
    '"summary": "全体講評を2〜3文", '
    '"fixes": [{"title": "改善点の見出し(20字以内)", "why": "なぜ惜しい/ダサいか", "how": "具体的な直し方"}]}\n'
    "scores のキーはこの6つ固定（whitespace=余白, typography=字組み, color=配色, "
    "hierarchy=視覚的階層, consistency=一貫性, imagery=画像/あしらいの品質）。"
    "fixes は効果の大きい順に最大6個。"
)


def _render_screenshot(camp_filename: str) -> Optional[Path]:
    """カンプHTMLを編集バー無しの素の状態で全体スクショ（file://で開く）。"""
    from playwright.sync_api import sync_playwright

    src = config.CAMP_DIR / camp_filename
    if not src.exists():
        return None
    url = src.resolve().as_uri()
    out = config.DATA_DIR / "_style_shot.png"
    cfg = config.CONFIG.capture
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.headless)
        pg = browser.new_context(
            viewport={"width": cfg.viewport_w, "height": cfg.viewport_h},
            user_agent=cfg.user_agent,
        ).new_page()
        pg.set_default_navigation_timeout(cfg.nav_timeout_ms)
        pg.goto(url, wait_until="domcontentloaded")
        try:
            pg.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        pg.wait_for_timeout(900)
        pg.screenshot(path=str(out), full_page=True)
        browser.close()
    return out if out.exists() else None


def _pick_vision_provider(provider: Optional[str]) -> str:
    """画像を送れるプロバイダを選ぶ（DeepSeek/GLM等はテキスト専用なのでClaudeへ）。"""
    ok = {"anthropic", "openai", "gemini"}
    for cand in (provider, config.CONFIG.htmlgen.edit_provider, config.CONFIG.htmlgen.provider):
        if cand in ok:
            return cand
    return "anthropic"


def _parse_json(s: str) -> Optional[dict]:
    s = (s or "").strip()
    # ```json ... ``` フェンス除去
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None


def style_check(camp_filename: str, provider: Optional[str] = None) -> dict:
    """カンプを採点して {ok, overall, scores, summary, fixes, model} を返す。"""
    png = _render_screenshot(camp_filename)
    if not png:
        return {"ok": False, "message": "カンプの描画に失敗しました"}
    # 全体を1枚に（縦長なので幅760・高さ上限4200で読める範囲に縮小）
    block = camp._ref_image_block(png, max_w=760, max_h=4200)  # noqa: SLF001（既存の画像ブロック生成を再利用）
    if not block:
        return {"ok": False, "message": "スクショの変換に失敗しました"}
    prov = _pick_vision_provider(provider)
    content = [block, {"type": "text", "text": "このランディングページを有名サイト基準で採点し、指定のJSONだけで返してください。"}]
    try:
        raw, model = camp._call_llm(_SYSTEM, content, prov)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}
    data = _parse_json(raw)
    if not data or "scores" not in data:
        return {"ok": False, "message": "AIの返答を解析できませんでした", "raw": (raw or "")[:300]}
    data["ok"] = True
    data["model"] = model
    log.info("おしゃれ度チェック: overall=%s (%s)", data.get("overall"), model)
    return data
