"""
取り込み（ingest）。仕様 4.1 を実装する。

1回のページロードから2枚撮る：
- firstview (viewport) = 検索用（雰囲気が最も濃縮されている）
- fullpage (全体)     = 見返す/分割用

撮影条件は固定（config.CaptureConfig）。重複排除はURLで判定し、--force で撮り直す。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from . import config, db
from .utils import get_logger, normalize_url, url_to_id, url_to_slug

log = get_logger("ingest")

# bot判定の壁（Cloudflare等）でよく出る文言。撮ってもゴミなので保存しない。
_BOT_WALL_SIGNS = [
    "just a moment",
    "checking your browser",
    "attention required",
    "verify you are human",
    "enable javascript and cookies",
    "アクセスできません",
    "ロボットではないことを",
]

# cookie同意バナーを best-effort で閉じるためのテキスト候補（日英）。
# 文言が合わなければここに足す（仕様の「手動で追加」に対応）。
# ★「OK」「Close」等の汎用語は、ページ下部の無関係ボタンを誤クリックして
#   スクロールが飛ぶ事故が起きるため入れない（同意/許可系に絞る）。
_CONSENT_TEXTS = [
    "すべて受け入れ", "すべて同意", "すべて許可", "同意して進む",
    "同意する", "許可する", "受け入れる",
    "Accept all", "Accept All", "Accept All Cookies",
    "Allow all", "I agree", "Agree", "Accept",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _try_close_consent(page: Page) -> None:
    """cookie同意バナーを best-effort で閉じる。失敗しても撮影は続ける。"""
    for text in _CONSENT_TEXTS:
        try:
            # ボタン/リンクで、その文言を持つ可視要素を1つだけ狙う
            locator = page.get_by_role(
                "button", name=text, exact=False
            ).first
            if locator.count() > 0 and locator.is_visible():
                locator.click(timeout=1_500)
                log.debug("同意バナーを閉じました（'%s'）", text)
                page.wait_for_timeout(400)
                return
        except Exception:
            # best-effort なので握りつぶす
            continue
    log.debug("同意バナーは見つからない or 閉じられませんでした（続行）")


def _trigger_lazy_load(page: Page) -> None:
    """最下部までスクロールして lazy-load を発火 → 先頭に戻す。"""
    try:
        page.evaluate(
            """
            async () => {
              await new Promise((resolve) => {
                let y = 0;
                const step = window.innerHeight;
                const timer = setInterval(() => {
                  window.scrollBy(0, step);
                  y += step;
                  if (y >= document.body.scrollHeight) {
                    clearInterval(timer);
                    resolve();
                  }
                }, 120);
              });
            }
            """
        )
        page.wait_for_timeout(600)  # 画像の読み込み待ち
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
    except Exception as exc:  # noqa: BLE001
        log.debug("lazy-load スクロール中に軽微なエラー（続行）: %s", exc)


# アニメ系ライブラリの検出用。window のグローバル変数と <script src> の両方で見る。
# （カンプ生成時に「あのGSAPみたいな演出」とAIへ言葉で頼む手がかりにする）
_LIB_DETECT_JS = """
() => {
  const found = new Set();
  const w = window;
  const g = {
    'GSAP': w.gsap || w.TweenMax || w.TweenLite,
    'ScrollTrigger': w.ScrollTrigger,
    'AOS': w.AOS,
    'Lenis': w.Lenis || w.lenis,
    'Locomotive': w.LocomotiveScroll || w.locomotive,
    'Swiper': w.Swiper,
    'Splitting': w.Splitting,
    'Lottie': w.lottie || w.bodymovin,
    'ScrollMagic': w.ScrollMagic,
    'Barba': w.barba || w.Barba,
    'Rellax': w.Rellax,
    'Vanta': w.VANTA,
    'Three.js': w.THREE,
    'PixiJS': w.PIXI,
    'anime.js': w.anime,
    'WOW.js': w.WOW,
  };
  for (const k in g) if (g[k]) found.add(k);
  if (document.querySelector('[data-framer-name],[data-framer-component-type]')) found.add('Framer Motion');
  if (document.querySelector('[data-aos]')) found.add('AOS');

  const tokens = {
    'gsap':'GSAP','scrolltrigger':'ScrollTrigger','aos':'AOS','lenis':'Lenis',
    'locomotive':'Locomotive','swiper':'Swiper','splitting':'Splitting','lottie':'Lottie',
    'scrollmagic':'ScrollMagic','barba':'Barba','rellax':'Rellax','vanta':'Vanta',
    'three.min':'Three.js','three.module':'Three.js','pixi':'PixiJS','anime.min':'anime.js',
    'wow.min':'WOW.js','framer-motion':'Framer Motion',
  };
  const srcs = [...document.querySelectorAll('script[src]')].map(s => (s.src||'').toLowerCase());
  for (const s of srcs) for (const t in tokens) if (s.includes(t)) found.add(tokens[t]);
  return [...found];
}
"""


def _detect_libraries(page: Page) -> str:
    """ページが使っているアニメ系ライブラリを検出し、カンマ区切りで返す。"""
    try:
        libs = page.evaluate(_LIB_DETECT_JS)
    except Exception:
        return ""
    return ", ".join(libs) if libs else ""


def _looks_like_bot_wall(page: Page) -> bool:
    """bot判定の壁ページか判定する（ライブのページ内容で見る）。"""
    try:
        title = (page.title() or "").lower()
        body = (page.inner_text("body")[:1500] or "").lower()
    except Exception:
        return False
    text = title + " " + body
    return any(sign in text for sign in _BOT_WALL_SIGNS)


def _looks_blank(image_path: Path) -> bool:
    """firstview が中身の薄いゴミ画像か判定する（描画前/bot壁/白紙対策）。

    グレースケールの「ばらつき（標準偏差）」が極端に小さいものを弾く。
    実データでは ゴミ(std≦13) と 正常(std≧36) にはっきり差があったので、その間で線を引く。
    ※ 黒背景のミニマルデザイン（暗いが中身はある）を誤判定しないよう mean>120 も条件にする。
    """
    try:
        arr = np.asarray(Image.open(image_path).convert("L"), dtype=np.float32)
    except Exception:
        return False
    std, mean = float(arr.std()), float(arr.mean())
    return std < 18.0 and mean > 120.0


def _attempt_capture(
    p, norm_url: str, headless: bool, firstview_path: Path, fullpage_path: Path
) -> tuple[str, Optional[dict]]:
    """1回ぶんの撮影を試みる。状態と（成功時の）レコードを返す。

    返り値の状態：'ok' / 'bot_wall' / 'blank' / 'error'
    headless=False（実ブラウザ）のときは、bot壁が解けるのを少し待ってから判定する。
    """
    cfg = config.CONFIG.capture
    browser = p.chromium.launch(headless=headless)
    context = browser.new_context(
        viewport={"width": cfg.viewport_w, "height": cfg.viewport_h},
        device_scale_factor=cfg.device_scale_factor,
        user_agent=cfg.user_agent,
    )
    page = context.new_page()
    page.set_default_navigation_timeout(cfg.nav_timeout_ms)
    try:
        try:
            page.goto(norm_url, wait_until="domcontentloaded")
        except PWTimeout:
            log.warning("ナビゲーションがタイムアウト（部分的に撮影を試みます）: %s", norm_url)
        except Exception as exc:  # noqa: BLE001
            log.error("ページを開けませんでした: %s (%s)", norm_url, exc)
            return "error", None

        # JS描画が重いSPA（Notion等）対策：通信が落ち着くまで best-effort で待つ。
        try:
            page.wait_for_load_state("networkidle", timeout=cfg.networkidle_timeout_ms)
        except PWTimeout:
            log.debug("networkidle に到達せず（続行）")

        page.wait_for_timeout(cfg.settle_after_load_ms)
        _try_close_consent(page)

        # bot判定の壁。実ブラウザ(非headless)なら、チェックが解けるのを少し待つ
        if _looks_like_bot_wall(page):
            if not headless:
                log.info("bot壁のチェックが解けるのを待っています…（最大%d秒）", cfg.bot_wall_clear_wait_s)
                for _ in range(cfg.bot_wall_clear_wait_s):
                    page.wait_for_timeout(1000)
                    if not _looks_like_bot_wall(page):
                        log.info("bot壁が解けました")
                        break
                page.wait_for_timeout(cfg.settle_after_load_ms)
                _try_close_consent(page)
            if _looks_like_bot_wall(page):
                return "bot_wall", None

        # firstview（viewport）= 検索用。スクロール前に・先頭に戻してから撮る
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
        page.screenshot(path=str(firstview_path), full_page=False)
        log.debug("firstview 保存: %s", firstview_path.name)

        # ほぼ白紙ならゴミ
        if _looks_blank(firstview_path):
            firstview_path.unlink(missing_ok=True)
            return "blank", None

        # lazy-load を発火させてから全体を撮る
        _trigger_lazy_load(page)
        page.screenshot(path=str(fullpage_path), full_page=True)
        log.debug("fullpage 保存: %s", fullpage_path.name)

        # 使っているアニメ系ライブラリを検出（カンプ生成の手がかり）
        libs = _detect_libraries(page)
        if libs:
            log.debug("検出ライブラリ: %s", libs)

        # デザイントークン（配色・フォント・余白等）も同じページから抜く
        from . import tokens as _tokens
        try:
            import json as _json
            _tk = page.evaluate(_tokens._EXTRACT_JS)
            design_tokens = _json.dumps(_tk, ensure_ascii=False) if _tk else ""
        except Exception:
            design_tokens = ""

        record = {
            "id": url_to_id(norm_url),
            "url": norm_url,
            "captured_at": _now_iso(),
            "firstview_path": str(firstview_path.relative_to(config.PROJECT_ROOT)),
            "fullpage_path": str(fullpage_path.relative_to(config.PROJECT_ROOT)),
            "viewport_w": cfg.viewport_w,
            "viewport_h": cfg.viewport_h,
            "device_scale_factor": cfg.device_scale_factor,
            "animation_libs": libs,
            "design_tokens": design_tokens,
        }
        return "ok", record
    finally:
        context.close()
        browser.close()


def capture_one(url: str, force: bool = False) -> Optional[dict]:
    """1件を撮影して DB に保存。保存したレコードを返す。

    既に撮影済みで force=False ならスキップして None を返す。
    bot壁を撮ってしまった場合は、設定により実ブラウザ(非headless)で自動的に撮り直す。
    """
    cfg = config.CONFIG.capture
    norm_url = normalize_url(url)
    site_id = url_to_id(norm_url)
    slug = url_to_slug(norm_url)

    # 重複排除：URL(=id) で判定
    with db.connect() as conn:
        existing = db.get_site(conn, site_id)
    if existing and not force:
        log.info("スキップ（既に撮影済み・--force で撮り直し）: %s", norm_url)
        return None

    firstview_path = config.SCREENSHOT_DIR / f"{slug}__{site_id}__firstview.png"
    fullpage_path = config.SCREENSHOT_DIR / f"{slug}__{site_id}__fullpage.png"

    log.info("撮影開始: %s", norm_url)
    with sync_playwright() as p:
        status, record = _attempt_capture(
            p, norm_url, cfg.headless, firstview_path, fullpage_path
        )
        # bot壁だったら、実ブラウザ（非headless）で撮り直す
        if status == "bot_wall" and cfg.headless and cfg.retry_non_headless_on_bot_wall:
            log.warning("bot判定の壁を検出 → 実ブラウザ(非headless)で撮り直します: %s", norm_url)
            status, record = _attempt_capture(
                p, norm_url, False, firstview_path, fullpage_path
            )

    if status == "ok":
        with db.connect() as conn:
            db.upsert_capture(conn, record)
        log.info("撮影完了・保存しました: %s", norm_url)
        return record

    # 保存しない理由をはっきり残す
    reason = {
        "bot_wall": "bot判定の壁を突破できず",
        "blank": "ファーストビューがほぼ白紙",
        "error": "ページを開けず",
    }.get(status, status)
    log.warning("保存しません（%s）: %s", reason, norm_url)
    return None


def capture_animation(url: str) -> Optional[str]:
    """スクロール録画（アニメ参照用）を撮ってDBに記録する。

    ねらい：静止画には写らない動き（スクロール連動アニメ等）を動画で残す。
    上から下へ少しずつスクロールしながら録画するので、出現アニメが動いて見える。
    返り値：保存した動画の相対パス（失敗時 None）。
    """
    cfg = config.CONFIG.capture
    norm_url = normalize_url(url)
    site_id = url_to_id(norm_url)
    slug = url_to_slug(norm_url)

    with db.connect() as conn:
        if db.get_site(conn, site_id) is None:
            log.warning("未登録のサイトは録画できません（先にサイト登録を）: %s", norm_url)
            return None

    config.VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    out_path = config.VIDEO_DIR / f"{slug}__{site_id}.webm"

    log.info("スクロール録画 開始: %s", norm_url)
    with sync_playwright() as p:
        # bot壁に当たりやすいので、録画は実ブラウザ(非headless)で行う
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": cfg.video_w, "height": cfg.video_h},
            user_agent=cfg.user_agent,
            record_video_dir=str(config.VIDEO_DIR),
            record_video_size={"width": cfg.video_w, "height": cfg.video_h},
        )
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.nav_timeout_ms)
        raw_video_path = None
        try:
            try:
                page.goto(norm_url, wait_until="domcontentloaded")
            except PWTimeout:
                log.warning("ナビゲーションがタイムアウト（録画は続行）: %s", norm_url)
            page.wait_for_timeout(cfg.settle_after_load_ms)
            _try_close_consent(page)

            # bot壁が出ていたら少し待って解けるのを待つ
            if _looks_like_bot_wall(page):
                for _ in range(cfg.bot_wall_clear_wait_s):
                    page.wait_for_timeout(1000)
                    if not _looks_like_bot_wall(page):
                        break

            # アニメ系ライブラリを検出して記録（既存サイトのlibsもここで補える）
            libs = _detect_libraries(page)
            if libs:
                with db.connect() as conn:
                    db.update_libraries(conn, site_id, libs)
                log.info("検出ライブラリ: %s", libs)

            # 先頭から下へ、少しずつスクロール（各ステップで一拍おいてアニメを見せる）
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(800)
            total = page.evaluate("() => document.body.scrollHeight")
            step = max(1, total // cfg.video_scroll_steps)
            for i in range(1, cfg.video_scroll_steps + 1):
                page.evaluate("(y) => window.scrollTo({top: y, behavior: 'smooth'})", i * step)
                page.wait_for_timeout(cfg.video_step_pause_ms)
            page.wait_for_timeout(600)

            raw_video_path = page.video.path() if page.video else None
        except Exception as exc:  # noqa: BLE001
            log.error("録画中にエラー: %s (%s)", norm_url, exc)
        finally:
            # context を閉じると録画が確定・ファイルが書き出される
            context.close()
            browser.close()

    if not raw_video_path or not Path(raw_video_path).exists():
        log.error("録画ファイルが作られませんでした: %s", norm_url)
        return None

    # Playwrightが付けたランダム名 → 分かりやすい名前にリネーム
    out_path.unlink(missing_ok=True)
    Path(raw_video_path).replace(out_path)
    rel = str(out_path.relative_to(config.PROJECT_ROOT))

    with db.connect() as conn:
        db.update_animation(conn, site_id, rel, status="video")
    log.info("スクロール録画 保存: %s", out_path.name)
    return rel


def capture_many(urls: list[str], force: bool = False) -> dict:
    """複数URLを順に撮影。結果サマリを返す（バッチ処理）。"""
    db.init_db()
    saved, skipped, failed = 0, 0, 0
    for i, raw in enumerate(urls, start=1):
        url = raw.strip()
        if not url or url.startswith("#"):
            continue
        log.info("[%d/%d] 処理中: %s", i, len(urls), url)
        try:
            result = capture_one(url, force=force)
            if result is None:
                skipped += 1
            else:
                saved += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.error("失敗: %s (%s)", url, exc)
    summary = {"saved": saved, "skipped": skipped, "failed": failed}
    log.info("取り込みサマリ: 保存=%d / スキップ=%d / 失敗=%d", saved, skipped, failed)
    return summary


def read_url_list(path: Path) -> list[str]:
    """1行1URLのリストファイルを読む（# 始まりはコメント）。"""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
