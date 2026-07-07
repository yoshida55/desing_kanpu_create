"""
ローカルビューア（仕様 4.5 Phase 2）。

ねらい：モデルを常駐させ、検索ボックスで言葉を変えながら何度も探せるようにする。
CLI都度起動だと毎回モデル読込の数秒を待つが、ビューアなら2回目以降が一瞬。

提供するもの：
- GET  /                … 検索ページ（HTML）
- GET  /api/search      … text→image 検索（JSON）
- GET  /api/similar     … 登録済みサイトに似たもの（image→image・モデル不要）
- GET  /api/sites       … 登録済み一覧（初期表示用）
- GET  /img/<id>/<which>… スクショ画像(firstview/fullpage)を返す
"""

from __future__ import annotations

import json as _json
import os
import re
import threading
import uuid
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file

from . import anim, assets, camp, clone, config, db, embed, export_split, ingest, motion, quality, search, style_check, vibe
from .model import DesignEmbedder
from .utils import get_logger

log = get_logger("viewer")

app = Flask(__name__, template_folder=str(config.TEMPLATE_DIR))

# モデルは1つだけ作って常駐させる（プロセスが生きている間ずっと使い回す）
_EMBEDDER = DesignEmbedder()


@app.before_request
def _log_request():
    """届いたリクエストを記録（ブラウザからのPOSTが本当に届いてるか診断用）。"""
    from flask import request as _rq
    log.info("REQ %s %s", _rq.method, _rq.path)

# 登録ジョブの状態（同時に走らせるのは1つだけ）。
# 撮影は1件あたり数秒かかるので、HTTPは即返してここを進捗ポーリングで読む。
_JOB: dict = {"state": "idle"}
_JOB_LOCK = threading.Lock()


# 録画中のサイトID（同時に録るのは1つ）。フロントはこれを見て「録画中…」を出す。
_RECORDING: dict = {"site_id": None}
_REC_LOCK = threading.Lock()

# 雰囲気の言語化ジョブ（単体でも一括でも、まとめて1つのバッチとして扱う）
_DESC_JOB: dict = {"running": False, "done": 0, "total": 0, "current": ""}
_DESC_LOCK = threading.Lock()

# カンプ生成ジョブ（複数同時OK）。job_id -> {state, phase, brief, ...result}。
# 生成は時間がかかるので非同期＋ポーリング。並行して2〜数本まわせる。
_CAMP_JOBS: dict = {}
_CAMP_LOCK = threading.Lock()
_CAMP_MAX = 4  # 同時に走らせる上限（メモリ/APIレート保護）

# 画像抜き出し中のサイトID（同時に1つ）
_EXTRACTING: dict = {"site_id": None}
_EXT_LOCK = threading.Lock()

# アニメ抜き出し中のサイトID（同時に1つ）
_ANIM_EXTRACTING: dict = {"site_id": None}
_ANIM_LOCK = threading.Lock()

# 録画から動きを読み取り中のサイトID（同時に1つ）
_MOTION_RUNNING: dict = {"site_id": None, "error": None}
_MOTION_LOCK = threading.Lock()

# 忠実クローン中の状態（同時に1つ・実ページを開くので重い）
_CLONING: dict = {"site_id": None, "phase": "", "file": None, "error": None}
_CLONE_LOCK = threading.Lock()

# 一括改善（Before→After）中の状態（同時に1つ・LLMを何度も呼ぶ）
_IMPROVING: dict = {"file": None, "phase": "", "result": None, "error": None}
_IMPROVE_LOCK = threading.Lock()


def _swatches(design_tokens_json) -> list[str]:
    """デザイントークンから、カードに出す代表色（CSS色文字列）を数個取り出す。"""
    if not design_tokens_json:
        return []
    try:
        import json as _json
        t = _json.loads(design_tokens_json)
    except Exception:
        return []
    out = []
    for c in (t.get("accent", []) + t.get("bg", [])):
        # 透明・ほぼ白は飛ばして、目立つ色を優先
        if "rgba" in c and c.strip().endswith(", 0)"):
            continue
        if c not in out:
            out.append(c)
        if len(out) >= 5:
            break
    return out


def _site_extra(site_id: str) -> dict:
    """カード表示に足す情報（録画・ライブラリ・雰囲気文・配色）を返す。"""
    with db.connect() as conn:
        row = db.get_site(conn, site_id)
    if not row:
        return {"has_video": False, "libs": [], "vibe": "", "swatches": []}
    has_video = bool(row["animation_video_path"]) and (
        config.PROJECT_ROOT / row["animation_video_path"]
    ).exists()
    libs = [s.strip() for s in (row["animation_libs"] or "").split(",") if s.strip()]
    return {
        "has_video": has_video,
        "libs": libs,
        "vibe": row["vibe_description"] or "",
        "swatches": _swatches(row["design_tokens"]),
    }


def _hits_payload(hits) -> list[dict]:
    """検索結果を、画像URL付きの dict 配列にしてフロントへ渡す。"""
    payload = []
    for h in hits:
        d = h.to_dict()
        # 画像は専用ルート経由で配る（パスを直接さらさない）
        d["img"] = f"/img/{h.site_id}/firstview"
        d.update(_site_extra(h.site_id))  # has_video / libs
        payload.append(d)
    return payload


@app.route("/")
def index():
    """検索ページを返す。"""
    return (config.TEMPLATE_DIR / "viewer.html").read_text(encoding="utf-8")


@app.route("/api/search")
def api_search():
    """text→image 検索。?q=クエリ&top=件数"""
    query = (request.args.get("q") or "").strip()
    top = request.args.get("top", type=int) or config.CONFIG.search.top_n
    if not query:
        return jsonify({"query": "", "hits": []})
    hits = search.search_by_text(query, top_n=top, embedder=_EMBEDDER)
    return jsonify({"query": query, "hits": _hits_payload(hits)})


@app.route("/api/similar")
def api_similar():
    """登録済みサイトに似たもの。?id=サイトID&top=件数（モデル不要で速い）"""
    site_id = (request.args.get("id") or "").strip()
    top = request.args.get("top", type=int) or config.CONFIG.search.top_n
    if not site_id:
        return jsonify({"hits": []})
    hits = search.search_similar_to_site(site_id, top_n=top)
    return jsonify({"site_id": site_id, "hits": _hits_payload(hits)})


def _run_register_job(urls: list[str], force: bool) -> None:
    """バックグラウンドで1件ずつ撮影→最後にまとめて埋め込み。進捗は _JOB に書く。"""
    saved = skipped = failed = 0
    total = len(urls)
    try:
        # ── 撮影フェーズ（重い。1件ずつ進捗を更新する）──
        for i, url in enumerate(urls, start=1):
            with _JOB_LOCK:
                _JOB.update(phase="capture", done=i - 1, current=url)
            try:
                result = ingest.capture_one(url, force=force)
                if result is None:
                    skipped += 1
                else:
                    saved += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.error("撮影失敗: %s (%s)", url, exc)
            with _JOB_LOCK:
                _JOB.update(done=i, saved=saved, skipped=skipped, failed=failed)

        # ── 埋め込みフェーズ（常駐モデルで未埋め込みをまとめて処理）──
        with _JOB_LOCK:
            _JOB.update(phase="embed", current="")
        embed_summary = embed.embed_all(force=False, embedder=_EMBEDDER)

        msg = (
            f"完了：保存{saved} / スキップ{skipped} / 失敗{failed} ／ "
            f"ベクトル化 {embed_summary['embedded']}件"
        )
        with _JOB_LOCK:
            _JOB.update(state="done", phase="done", message=msg,
                        embedded=embed_summary["embedded"])
        log.info("登録ジョブ完了: %s", msg)
    except Exception as exc:  # noqa: BLE001
        log.exception("登録ジョブが落ちました")
        with _JOB_LOCK:
            _JOB.update(state="error", message=f"エラー: {exc}")


@app.route("/api/register", methods=["POST"])
def api_register():
    """URLの登録を開始する（非同期）。即 job_id を返し、撮影は裏で進める。

    入力（JSON）: { "urls": "1行1URLのテキスト", "force": false }
    出力（JSON）: { ok, job_id, total }
    進捗は GET /api/register/status で見る。
    """
    data = request.get_json(silent=True) or {}
    raw = (data.get("urls") or "").strip()
    force = bool(data.get("force"))
    if not raw:
        return jsonify({"ok": False, "message": "URLを入力してください"}), 400

    urls = [
        ln.strip()
        for ln in raw.replace(",", "\n").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not urls:
        return jsonify({"ok": False, "message": "有効なURLがありません"}), 400

    # 同時に走らせるのは1ジョブだけ（多重起動を防ぐ）
    with _JOB_LOCK:
        if _JOB.get("state") == "running":
            return jsonify({"ok": False, "message": "別の登録が進行中です"}), 409
        job_id = uuid.uuid4().hex[:8]
        _JOB.clear()
        _JOB.update(
            state="running", phase="capture", job_id=job_id,
            total=len(urls), done=0, current="",
            saved=0, skipped=0, failed=0, embedded=0, message="",
        )

    log.info("登録ジョブ開始 [%s]: %d 件", job_id, len(urls))
    threading.Thread(target=_run_register_job, args=(urls, force), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "total": len(urls)})


@app.route("/api/register/status")
def api_register_status():
    """登録ジョブの進捗を返す。フロントが定期的に読む。"""
    with _JOB_LOCK:
        return jsonify(dict(_JOB))


@app.route("/api/delete", methods=["POST"])
def api_delete():
    """登録済みサイトを削除する。入力（JSON）: { "id": "サイトID" }

    DBの行を消し、保存していたスクショ画像(firstview/fullpage)も消す。
    """
    data = request.get_json(silent=True) or {}
    site_id = (data.get("id") or "").strip()
    if not site_id:
        return jsonify({"ok": False, "message": "IDがありません"}), 400

    with db.connect() as conn:
        paths = db.delete_site(conn, site_id)
    if paths is None:
        return jsonify({"ok": False, "message": "見つかりません"}), 404

    # スクショ画像・録画の後始末（消えていても気にしない）
    for key in ("firstview_path", "fullpage_path", "animation_video_path"):
        rel = paths.get(key)
        if not rel:
            continue
        f = config.PROJECT_ROOT / rel
        try:
            if f.exists():
                f.unlink()
        except OSError as exc:
            log.warning("画像削除に失敗（続行）: %s (%s)", f, exc)

    # 抜き出し画像フォルダごと後始末
    adir = config.ASSET_DIR / site_id
    if adir.exists():
        try:
            import shutil
            shutil.rmtree(adir)
        except OSError as exc:
            log.warning("抜き出し画像の削除に失敗（続行）: %s (%s)", adir, exc)

    log.info("削除しました: %s", paths.get("url"))
    return jsonify({"ok": True, "message": f"削除しました: {paths.get('url')}"})


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """現在の設定を返す（APIキー本体は出さず、設定済みか否かだけ）。"""
    h = config.CONFIG.htmlgen
    v = config.CONFIG.vibe
    return jsonify(
        {
            "provider": h.provider,
            "edit_provider": h.edit_provider,
            "openai_model": h.openai_model,
            "anthropic_model": v.model,
            "openai_set": h.openai_enabled,
            "anthropic_set": v.enabled,
            "gemini_model": config.CONFIG.gemini.model,
            "gemini_set": config.CONFIG.gemini.enabled,
            "deepseek_model": config.CONFIG.deepseek.model,
            "deepseek_set": config.CONFIG.deepseek.enabled,
            "zai_model": config.CONFIG.zai.model,
            "zai_set": config.CONFIG.zai.enabled,
        }
    )


def _test_deepseek(deepseek_key: str = "") -> tuple[bool, str]:
    """DeepSeek（OpenAI互換）の接続を確かめる。"""
    dcfg = config.CONFIG.deepseek
    key = (deepseek_key or "").strip() or dcfg.api_key
    if not key or "ここに" in key:
        return False, "DeepSeekキーが未入力です"
    try:
        from openai import OpenAI
        OpenAI(api_key=key, base_url=dcfg.base_url, timeout=30.0).chat.completions.create(
            model=dcfg.model, max_tokens=5,
            messages=[{"role": "user", "content": "ok"}],
        )
        return True, f"DeepSeek（{dcfg.model}）接続OK"
    except Exception as exc:  # noqa: BLE001
        return False, "DeepSeek接続NG：" + str(exc)[:120]


def _test_zai(zai_key: str = "") -> tuple[bool, str]:
    """GLM（Z.ai・OpenAI互換）の接続を確かめる。"""
    zcfg = config.CONFIG.zai
    key = (zai_key or "").strip() or zcfg.api_key
    if not key or "ここに" in key:
        return False, "GLM(Z.ai)キーが未入力です"
    try:
        from openai import OpenAI
        OpenAI(api_key=key, base_url=zcfg.base_url, timeout=30.0).chat.completions.create(
            model=zcfg.model, max_tokens=5,
            messages=[{"role": "user", "content": "ok"}],
        )
        return True, f"GLM（{zcfg.model}）接続OK"
    except Exception as exc:  # noqa: BLE001
        return False, "GLM接続NG：" + str(exc)[:120]


def _test_key(provider: str, openai_key: str = "", anthropic_key: str = "", deepseek_key: str = "", zai_key: str = "") -> tuple[bool, str]:
    """キーが実際に使えるか、ごく短いAPI呼び出しで確かめる。

    入力欄のキー(openai_key/anthropic_key)が渡されれば、保存前でもそれをテストする。
    渡されなければ保存済み(.env)のキーをテストする。
    """
    try:
        if provider == "openai":
            h = config.CONFIG.htmlgen
            key = (openai_key or "").strip() or h.openai_api_key
            if not key.startswith("sk-") or "ここに" in key:
                return False, "OpenAIキーが未入力です"
            from openai import OpenAI
            OpenAI(api_key=key).chat.completions.create(
                model=h.openai_model,
                max_completion_tokens=5,
                messages=[{"role": "user", "content": "ok"}],
            )
            return True, f"OpenAI（{h.openai_model}）接続OK"
        elif provider == "gemini":
            g = _test_gemini()
            if g is None:
                return False, "Geminiキーが未入力です"
            return g
        elif provider == "deepseek":
            return _test_deepseek(deepseek_key)
        elif provider == "zai":
            return _test_zai(zai_key)
        else:
            v = config.CONFIG.vibe
            key = (anthropic_key or "").strip() or v.api_key
            if not key.startswith("sk-ant-"):
                return False, "Anthropicキーが未入力です"
            from anthropic import Anthropic
            Anthropic(api_key=key).messages.create(
                model=v.model, max_tokens=5,
                messages=[{"role": "user", "content": "ok"}],
            )
            return True, "Claude 接続OK"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:160]


def _test_gemini(gemini_key: str = "") -> Optional[tuple[bool, str]]:
    """Geminiの接続を確かめる（説明づけ用）。未設定なら None を返す（テスト対象外）。"""
    gcfg = config.CONFIG.gemini
    key = (gemini_key or "").strip() or gcfg.api_key
    if not key or "ここに" in key:
        return None
    try:
        import urllib.request

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{gcfg.model}:generateContent?key={key}"
        )
        body = {"contents": [{"parts": [{"text": "ok"}]}]}
        req = urllib.request.Request(
            url, data=_json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True, f"Gemini（{gcfg.model}）接続OK"
    except Exception as exc:  # noqa: BLE001
        return False, "Gemini接続NG：" + str(exc)[:100]


def _test_all(provider: str, openai_key: str = "", anthropic_key: str = "", gemini_key: str = "", deepseek_key: str = "", zai_key: str = "") -> tuple[bool, str]:
    """生成エンジン＋修正エンジン（DeepSeek/GLM等）＋説明づけ（Gemini）をまとめてテストしメッセージを組む。"""
    ok, msg = _test_key(provider, openai_key, anthropic_key, deepseek_key, zai_key)
    parts = [("✅ " if ok else "⚠ ") + "生成: " + msg]
    # 修正エンジンが生成と別なら、それも確認する（例：生成=GPT／修正=DeepSeek）
    edit_provider = config.CONFIG.htmlgen.edit_provider
    if edit_provider and edit_provider != provider:
        eok, emsg = _test_key(edit_provider, openai_key, anthropic_key, deepseek_key, zai_key)
        parts.append(("✅ " if eok else "⚠ ") + "修正: " + emsg)
        ok = ok and eok
    g = _test_gemini(gemini_key)
    if g is not None:
        parts.append(("✅ " if g[0] else "⚠ ") + g[1])
        ok = ok and g[0]
    return ok, " ／ ".join(parts)


def _provider_ready(provider: str) -> bool:
    """そのプロバイダのキーが入っていて使える状態か。"""
    if provider == "openai":
        return config.CONFIG.htmlgen.openai_enabled
    if provider == "gemini":
        return config.CONFIG.gemini.enabled
    if provider == "deepseek":
        return config.CONFIG.deepseek.enabled
    if provider == "zai":
        return config.CONFIG.zai.enabled
    return config.CONFIG.vibe.enabled


def _gen_ready() -> bool:
    """カンプ生成エンジンが使えるか。"""
    return _provider_ready(config.CONFIG.htmlgen.provider)


def _edit_ready() -> bool:
    """カンプ修正エンジンが使えるか。"""
    return _provider_ready(config.CONFIG.htmlgen.edit_provider)


@app.route("/api/test_key", methods=["POST"])
def api_test_key():
    """選択中エンジン＋Geminiのキーで接続テストする（入力欄のキーがあればそれを使う）。"""
    data = request.get_json(silent=True) or {}
    provider = data.get("provider") or config.CONFIG.htmlgen.provider
    ok, msg = _test_all(
        provider,
        data.get("openai_api_key", ""),
        data.get("anthropic_api_key", ""),
        data.get("gemini_api_key", ""),
        data.get("deepseek_api_key", ""),
        data.get("zai_api_key", ""),
    )
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    """設定画面からの保存。.env に書き込み、即反映する（キー本体は空なら据え置き）。"""
    data = request.get_json(silent=True) or {}
    updates = {}
    if data.get("provider") in ("anthropic", "openai", "gemini", "deepseek", "zai"):
        updates["DESIGN_STOCK_HTML_PROVIDER"] = data["provider"]
    if data.get("edit_provider") in ("anthropic", "openai", "gemini", "deepseek", "zai"):
        updates["DESIGN_STOCK_EDIT_PROVIDER"] = data["edit_provider"]
    if (data.get("openai_model") or "").strip():
        updates["DESIGN_STOCK_OPENAI_MODEL"] = data["openai_model"].strip()
    if (data.get("anthropic_model") or "").strip():
        updates["DESIGN_STOCK_VIBE_MODEL"] = data["anthropic_model"].strip()
    if (data.get("gemini_model") or "").strip():
        updates["DESIGN_STOCK_GEMINI_MODEL"] = data["gemini_model"].strip()
    if (data.get("gemini_api_key") or "").strip():
        updates["GEMINI_API_KEY"] = data["gemini_api_key"].strip()
    if (data.get("deepseek_model") or "").strip():
        updates["DESIGN_STOCK_DEEPSEEK_MODEL"] = data["deepseek_model"].strip()
    if (data.get("deepseek_api_key") or "").strip():
        updates["DEEPSEEK_API_KEY"] = data["deepseek_api_key"].strip()
    if (data.get("zai_model") or "").strip():
        updates["DESIGN_STOCK_ZAI_MODEL"] = data["zai_model"].strip()
    if (data.get("zai_api_key") or "").strip():
        updates["ZAI_API_KEY"] = data["zai_api_key"].strip()
    if (data.get("openai_api_key") or "").strip():
        updates["OPENAI_API_KEY"] = data["openai_api_key"].strip()
    if (data.get("anthropic_api_key") or "").strip():
        updates["ANTHROPIC_API_KEY"] = data["anthropic_api_key"].strip()

    config.update_env_file(updates)
    config.reload()
    h = config.CONFIG.htmlgen
    v = config.CONFIG.vibe
    # 保存した後、生成エンジン＋Geminiのキーで接続テスト（正しいか即わかる）
    key_ok, key_msg = _test_all(h.provider)
    return jsonify(
        {
            "ok": True,
            "provider": h.provider,
            "openai_model": h.openai_model,
            "anthropic_model": v.model,
            "openai_set": h.openai_enabled,
            "anthropic_set": v.enabled,
            "gemini_model": config.CONFIG.gemini.model,
            "gemini_set": config.CONFIG.gemini.enabled,
            "deepseek_model": config.CONFIG.deepseek.model,
            "deepseek_set": config.CONFIG.deepseek.enabled,
            "key_ok": key_ok,
            "message": "保存しました。" + key_msg,
        }
    )


@app.route("/api/site/<site_id>")
def api_site(site_id: str):
    """1サイトの詳細（全体・動画・トークン・ライブラリ・雰囲気）をまとめて返す。"""
    with db.connect() as conn:
        row = db.get_site(conn, site_id)
    if not row:
        abort(404)
    tokens = {}
    if row["design_tokens"]:
        try:
            tokens = _json.loads(row["design_tokens"])
        except Exception:
            tokens = {}
    has_video = bool(row["animation_video_path"]) and (
        config.PROJECT_ROOT / row["animation_video_path"]
    ).exists()
    return jsonify(
        {
            "site_id": row["id"],
            "url": row["url"],
            "captured_at": row["captured_at"],
            "libs": [s.strip() for s in (row["animation_libs"] or "").split(",") if s.strip()],
            "vibe": row["vibe_description"] or "",
            "has_video": has_video,
            "firstview": f"/img/{row['id']}/firstview",
            "fullpage": f"/img/{row['id']}/fullpage",
            "video": f"/video/{row['id']}",
            "tokens": tokens,
            # 抜き出した画像（/assets/<id>/<file> のURL一覧）と、抜き出し中かどうか
            "assets": [
                f"/assets/{site_id}/{Path(p).name}" for p in assets.list_assets(site_id)
            ],
            "extracting": _EXTRACTING.get("site_id") == site_id,
            # 抜き出したアニメ素材（@keyframes/transition/Lottie）と、抜き出し中かどうか
            "anim": _anim_payload(row, site_id),
            "anim_extracting": _ANIM_EXTRACTING.get("site_id") == site_id,
            # 録画からAIが読み取った「動きの仕様書」と、読み取り中かどうか
            "motion": _motion_payload(row),
            "motion_reading": _MOTION_RUNNING.get("site_id") == site_id,
            "motion_error": _MOTION_RUNNING.get("error") if _MOTION_RUNNING.get("site_id") == site_id else None,
        }
    )


def _motion_payload(row) -> dict:
    """DBの motion_spec(JSON) を画面が使う形にして返す（無ければ空）。"""
    if not row["motion_spec"]:
        return {}
    try:
        return _json.loads(row["motion_spec"])
    except Exception:  # noqa: BLE001
        return {}


def _anim_payload(row, site_id: str) -> dict:
    """DBの animation_snippets(JSON) を、画面が使いやすい形にして返す。"""
    snippets = {}
    if row["animation_snippets"]:
        try:
            snippets = _json.loads(row["animation_snippets"])
        except Exception:  # noqa: BLE001
            snippets = {}
    return {
        "keyframes": snippets.get("keyframes", []),
        "transitions": snippets.get("transitions", []),
        "animations": snippets.get("animations", []),
        # 保存済みLottieは配信URL(/anim/<id>/<file>)に直して返す
        "lottie": [
            f"/anim/{site_id}/{Path(p).name}" for p in anim.list_lottie(site_id)
        ],
    }


def _run_anim_job(site_id: str, url: str) -> None:
    """バックグラウンドでアニメ素材を抜き出し、DBに保存する。"""
    try:
        snippets = anim.extract_animations(url)
        with db.connect() as conn:
            db.update_anim_snippets(conn, site_id, _json.dumps(snippets, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        log.exception("アニメ抜き出しに失敗: %s", url)
    finally:
        with _ANIM_LOCK:
            _ANIM_EXTRACTING["site_id"] = None


@app.route("/api/extract_anim", methods=["POST"])
def api_extract_anim():
    """指定サイトのアニメ素材(@keyframes/transition/Lottie)を抜き出す（非同期）。"""
    data = request.get_json(silent=True) or {}
    site_id = (data.get("id") or "").strip()
    with db.connect() as conn:
        row = db.get_site(conn, site_id)
    if not row:
        return jsonify({"ok": False, "message": "見つかりません"}), 404
    with _ANIM_LOCK:
        if _ANIM_EXTRACTING.get("site_id") is not None:
            return jsonify({"ok": False, "message": "別のアニメ抜き出しが進行中です"}), 409
        _ANIM_EXTRACTING["site_id"] = site_id
    log.info("アニメ抜き出しジョブ開始: %s", row["url"])
    threading.Thread(target=_run_anim_job, args=(site_id, row["url"]), daemon=True).start()
    return jsonify({"ok": True, "site_id": site_id})


def _run_motion_job(site_id: str) -> None:
    """バックグラウンドで録画からAIに動きを読み取らせ、motion_spec に保存する。"""
    try:
        motion.describe_motion(site_id)
        with _MOTION_LOCK:
            _MOTION_RUNNING["error"] = None
    except Exception as exc:  # noqa: BLE001
        log.exception("動きの読み取りに失敗: %s", site_id)
        with _MOTION_LOCK:
            _MOTION_RUNNING["error"] = str(exc)
    finally:
        with _MOTION_LOCK:
            _MOTION_RUNNING["site_id"] = None


@app.route("/api/read_motion", methods=["POST"])
def api_read_motion():
    """指定サイトの録画からAIが動きを読み取る（非同期）。録画が必要。"""
    data = request.get_json(silent=True) or {}
    site_id = (data.get("id") or "").strip()
    with db.connect() as conn:
        row = db.get_site(conn, site_id)
    if not row:
        return jsonify({"ok": False, "message": "見つかりません"}), 404
    has_video = bool(row["animation_video_path"]) and (
        config.PROJECT_ROOT / row["animation_video_path"]
    ).exists()
    if not has_video:
        return jsonify({"ok": False, "message": "先に『🎬動き』で録画してください"}), 400
    with _MOTION_LOCK:
        if _MOTION_RUNNING.get("site_id") is not None:
            return jsonify({"ok": False, "message": "別の読み取りが進行中です"}), 409
        _MOTION_RUNNING["site_id"] = site_id
        _MOTION_RUNNING["error"] = None
    log.info("動きの読み取りジョブ開始: %s", row["url"])
    threading.Thread(target=_run_motion_job, args=(site_id,), daemon=True).start()
    return jsonify({"ok": True, "site_id": site_id})


@app.route("/anim/<site_id>/<path:filename>")
def anim_file(site_id: str, filename: str):
    """抜き出したLottie JSONを返す。"""
    path = config.ANIM_DIR / site_id / filename
    if not path.exists() or not path.is_file():
        abort(404)
    return send_file(path, mimetype="application/json")


def _run_extract_job(site_id: str, url: str) -> None:
    """バックグラウンドで画像を抜き出す。"""
    try:
        assets.extract_images(url)
    except Exception:  # noqa: BLE001
        log.exception("画像抜き出しに失敗: %s", url)
    finally:
        with _EXT_LOCK:
            _EXTRACTING["site_id"] = None


@app.route("/api/extract_images", methods=["POST"])
def api_extract_images():
    """指定サイトの画像を抜き出す（非同期・実ページを開く）。"""
    data = request.get_json(silent=True) or {}
    site_id = (data.get("id") or "").strip()
    with db.connect() as conn:
        row = db.get_site(conn, site_id)
    if not row:
        return jsonify({"ok": False, "message": "見つかりません"}), 404
    with _EXT_LOCK:
        if _EXTRACTING.get("site_id") is not None:
            return jsonify({"ok": False, "message": "別の抜き出しが進行中です"}), 409
        _EXTRACTING["site_id"] = site_id
    log.info("画像抜き出しジョブ開始: %s", row["url"])
    threading.Thread(target=_run_extract_job, args=(site_id, row["url"]), daemon=True).start()
    return jsonify({"ok": True, "site_id": site_id})


def _run_clone_job(site_id: str, url: str, keep_js: bool, use_extracted: bool) -> None:
    """バックグラウンドで忠実クローンを作る。"""
    def prog(msg: str) -> None:
        _CLONING["phase"] = msg

    try:
        result = clone.clone_site(url, keep_js=keep_js, use_extracted=use_extracted, progress=prog)
        with _CLONE_LOCK:
            _CLONING["file"] = result["file"]
            _CLONING["phase"] = f"完了（素材 {result['assets']} 件）"
    except Exception as exc:  # noqa: BLE001
        log.exception("クローンジョブが落ちました: %s", url)
        with _CLONE_LOCK:
            _CLONING["error"] = str(exc)
    finally:
        with _CLONE_LOCK:
            _CLONING["site_id"] = None


@app.route("/api/clone_site", methods=["POST"])
def api_clone_site():
    """登録サイトを忠実クローンする（非同期・AI不使用・DOMごと吸い出し）。

    use_extracted=true のときは「🖼画像を抜き出す」で保存済みの画像をそのまま使う
    （未抜き出しなら400で先に抜き出すよう案内する）。
    """
    data = request.get_json(silent=True) or {}
    site_id = (data.get("id") or "").strip()
    keep_js = bool(data.get("keep_js"))
    use_extracted = bool(data.get("use_extracted"))
    with db.connect() as conn:
        row = db.get_site(conn, site_id)
    if not row:
        return jsonify({"ok": False, "message": "見つかりません"}), 404
    if use_extracted and not assets.list_assets(site_id):
        return jsonify({"ok": False, "message": "先に『🖼画像を抜き出す』で画像を抜き出してください"}), 400
    with _CLONE_LOCK:
        if _CLONING.get("site_id") is not None:
            return jsonify({"ok": False, "message": "別のクローンが進行中です"}), 409
        _CLONING.update({"site_id": site_id, "phase": "開始しています…", "file": None, "error": None})
    log.info("クローンジョブ開始: %s (keep_js=%s, use_extracted=%s)", row["url"], keep_js, use_extracted)
    threading.Thread(target=_run_clone_job, args=(site_id, row["url"], keep_js, use_extracted), daemon=True).start()
    return jsonify({"ok": True, "site_id": site_id})


@app.route("/api/clone_site/status")
def api_clone_site_status():
    """クローンの進捗（phase）と完成ファイル名を返す。"""
    with _CLONE_LOCK:
        return jsonify(dict(_CLONING))


def _run_improve_job(filename: str, limit: int, targets: list | None, hint: str, ref_id: str) -> None:
    """バックグラウンドで全セクション一括改善を回す。"""
    def prog(msg: str) -> None:
        _IMPROVING["phase"] = msg

    try:
        result = camp.improve_all(filename, limit=limit, targets=targets, hint=hint,
                                  ref_id=ref_id, progress=prog)
        with _IMPROVE_LOCK:
            _IMPROVING["result"] = result
            _IMPROVING["phase"] = f"完了（{result['improved']}/{result['total']}セクション改善）"
    except Exception as exc:  # noqa: BLE001
        log.exception("一括改善ジョブが落ちました: %s", filename)
        with _IMPROVE_LOCK:
            _IMPROVING["error"] = str(exc)
    finally:
        with _IMPROVE_LOCK:
            _IMPROVING["file"] = None


@app.route("/api/improve_camp", methods=["POST"])
def api_improve_camp():
    """カンプ/クローンの全セクションを一括で今風に改善（非同期・営業のAfter版づくり）。"""
    if not _edit_ready():
        return jsonify({"ok": False, "message": "修正エンジンのAPIキーが未設定です（⚙設定を確認）"}), 400
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    try:
        limit = int(data.get("limit", 0) or 0)  # 0=全部、N=最初のNセクションだけ
    except Exception:  # noqa: BLE001
        limit = 0
    # sections=[1,4] のような0始まり番号リスト（指定があれば limit より優先）
    targets = None
    raw_secs = data.get("sections")
    if isinstance(raw_secs, list):
        targets = [int(x) for x in raw_secs if isinstance(x, (int, float)) and int(x) >= 0] or None
    if not fn or not (config.CAMP_DIR / fn).exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    with _IMPROVE_LOCK:
        if _IMPROVING.get("file") is not None:
            return jsonify({"ok": False, "message": "別の一括改善が進行中です"}), 409
        _IMPROVING.update({"file": fn, "phase": "開始しています…", "result": None, "error": None})
    hint = (data.get("hint") or "").strip()[:500]
    ref_id = (data.get("ref_id") or "").strip()
    log.info("一括改善ジョブ開始: %s (limit=%d, sections=%s, hint=%s, ref=%s)", fn, limit, targets, hint, ref_id)
    threading.Thread(target=_run_improve_job, args=(fn, limit, targets, hint, ref_id), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/improve_camp/status")
def api_improve_camp_status():
    """一括改善の進捗（phase）と結果を返す。"""
    with _IMPROVE_LOCK:
        return jsonify(dict(_IMPROVING))


@app.route("/assets/<site_id>/<path:filename>")
def asset_file(site_id: str, filename: str):
    """抜き出した画像を返す。"""
    path = config.ASSET_DIR / site_id / filename
    if not path.exists() or not path.is_file():
        abort(404)
    return send_file(path)


@app.route("/api/sites")
def api_sites():
    """登録済みの全サイト（初期表示用）。新しい撮影順で返す。"""
    with db.connect() as conn:
        rows = db.iter_sites_with_embedding(conn)
    rows = sorted(rows, key=lambda r: r["captured_at"], reverse=True)
    rec_id = _RECORDING.get("site_id")
    desc_cur = _DESC_JOB.get("current")
    hits = [
        {
            "rank": i + 1,
            "score": None,
            "url": r["url"],
            "captured_at": r["captured_at"],
            "site_id": r["id"],
            "img": f"/img/{r['id']}/firstview",
            "has_video": bool(r["animation_video_path"])
            and (config.PROJECT_ROOT / r["animation_video_path"]).exists(),
            "libs": [s.strip() for s in (r["animation_libs"] or "").split(",") if s.strip()],
            "vibe": r["vibe_description"] or "",
            "swatches": _swatches(r["design_tokens"]),
            "recording": r["id"] == rec_id,
            "describing": r["id"] == desc_cur,
        }
        for i, r in enumerate(rows)
    ]
    return jsonify({"hits": hits, "describe": dict(_DESC_JOB)})


def _run_record_job(site_id: str, url: str) -> None:
    """バックグラウンドでスクロール録画を撮る。"""
    try:
        ingest.capture_animation(url)
    except Exception:  # noqa: BLE001
        log.exception("録画ジョブが落ちました: %s", url)
    finally:
        with _REC_LOCK:
            _RECORDING["site_id"] = None


@app.route("/api/record_animation", methods=["POST"])
def api_record_animation():
    """指定サイトのスクロール録画を開始する（非同期・実ブラウザが一瞬開く）。"""
    data = request.get_json(silent=True) or {}
    site_id = (data.get("id") or "").strip()
    if not site_id:
        return jsonify({"ok": False, "message": "IDがありません"}), 400
    with db.connect() as conn:
        row = db.get_site(conn, site_id)
    if not row:
        return jsonify({"ok": False, "message": "見つかりません"}), 404

    with _REC_LOCK:
        if _RECORDING.get("site_id") is not None:
            return jsonify({"ok": False, "message": "別の録画が進行中です"}), 409
        _RECORDING["site_id"] = site_id

    log.info("録画ジョブ開始: %s", row["url"])
    threading.Thread(
        target=_run_record_job, args=(site_id, row["url"]), daemon=True
    ).start()
    return jsonify({"ok": True, "site_id": site_id})


def _run_describe_batch(site_ids: list[str]) -> None:
    """バックグラウンドで複数サイトを順に言語化（常駐モデルを使い回す）。"""
    with _DESC_LOCK:
        _DESC_JOB.update(running=True, done=0, total=len(site_ids), current="")
    try:
        for sid in site_ids:
            with _DESC_LOCK:
                _DESC_JOB["current"] = sid
            try:
                vibe.describe_one(sid, embedder=_EMBEDDER)
            except Exception:  # noqa: BLE001
                log.exception("言語化に失敗（続行）: %s", sid)
            with _DESC_LOCK:
                _DESC_JOB["done"] += 1
    finally:
        with _DESC_LOCK:
            _DESC_JOB.update(running=False, current="")


def _start_describe(site_ids: list[str]):
    """言語化バッチを開始する。進行中なら None を返す。"""
    with _DESC_LOCK:
        if _DESC_JOB["running"]:
            return None
        _DESC_JOB.update(running=True, done=0, total=len(site_ids), current="")
    threading.Thread(target=_run_describe_batch, args=(site_ids,), daemon=True).start()
    return len(site_ids)


@app.route("/api/describe", methods=["POST"])
def api_describe():
    """指定サイト1件の雰囲気を言語化する（Gemini優先、無ければClaude・非同期）。"""
    if not (config.CONFIG.gemini.enabled or config.CONFIG.vibe.enabled):
        return jsonify({"ok": False, "message": "Gemini または Anthropic のキーが未設定です（.env を確認）"}), 400
    data = request.get_json(silent=True) or {}
    site_id = (data.get("id") or "").strip()
    if not site_id:
        return jsonify({"ok": False, "message": "IDがありません"}), 400
    with db.connect() as conn:
        row = db.get_site(conn, site_id)
    if not row:
        return jsonify({"ok": False, "message": "見つかりません"}), 404
    if _start_describe([site_id]) is None:
        return jsonify({"ok": False, "message": "別の言語化が進行中です"}), 409
    log.info("言語化開始(単体): %s", row["url"])
    return jsonify({"ok": True, "site_id": site_id})


@app.route("/api/describe_all", methods=["POST"])
def api_describe_all():
    """未処理サイトをまとめて裏で言語化する（Gemini優先・その間も検索などは使える）。"""
    if not (config.CONFIG.gemini.enabled or config.CONFIG.vibe.enabled):
        return jsonify({"ok": False, "message": "Gemini または Anthropic のキーが未設定です（.env を確認）"}), 400
    with db.connect() as conn:
        targets = [r["id"] for r in db.iter_sites_needing_vibe(conn)]
    if not targets:
        return jsonify({"ok": True, "total": 0, "message": "未処理のサイトはありません"})
    if _start_describe(targets) is None:
        return jsonify({"ok": False, "message": "別の言語化が進行中です"}), 409
    log.info("言語化開始(一括): %d 件", len(targets))
    return jsonify({"ok": True, "total": len(targets)})


def _camp_set(job_id: str, **kw) -> None:
    """指定ジョブの状態を更新する（フロントが表示する）。"""
    with _CAMP_LOCK:
        j = _CAMP_JOBS.setdefault(job_id, {})
        j.update(kw)


def _run_camp_job(job_id: str, brief: str, base_site_id: str, anim_ref_id: str = "") -> None:
    """バックグラウンドでカンプを生成（参考選び＋Claude/GPT）。複数同時に走ってよい。

    use_model=False：参考選びにSigLIPを使わない（モデルを読まない）。
    base_site_id があれば、そのサイトのトークンが無いときは先に抽出してから生成する。
    anim_ref_id があれば、そのサイトのアニメ素材が無いときは先に抽出してから生成する
    （mix & match：Aの見た目にBの動き）。
    各段階を そのジョブの 'phase' に書くので、フロントで進捗が見える。
    """
    try:
        _camp_set(job_id, phase="参考サイトを準備しています…")
        if base_site_id:
            from . import tokens as _tokens
            with db.connect() as conn:
                row = db.get_site(conn, base_site_id)
            if row and not row["design_tokens"]:
                _camp_set(job_id, phase="手本サイトのデザイントークンを抽出中…")
                try:
                    _tokens.extract_and_store(row["url"])
                except Exception:
                    log.exception("トークン抽出に失敗（続行）")
        if anim_ref_id:
            with db.connect() as conn:
                brow = db.get_site(conn, anim_ref_id)
            if brow and not brow["animation_snippets"]:
                _camp_set(job_id, phase="アニメ参照サイトの動きを抽出中…")
                try:
                    snip = anim.extract_animations(brow["url"])
                    with db.connect() as conn:
                        db.update_anim_snippets(conn, anim_ref_id, _json.dumps(snip, ensure_ascii=False))
                except Exception:
                    log.exception("アニメ抽出に失敗（続行）")
        prov = {"openai": "GPT", "gemini": "Gemini", "deepseek": "DeepSeek"}.get(
            config.CONFIG.htmlgen.provider, "Claude")
        # ベース×アニメ両指定なら相性を一言添える（生成には _pair_fit_block が自動で効く）
        fit_note = ""
        if base_site_id and anim_ref_id:
            score = camp.pair_fit_score(base_site_id, anim_ref_id)
            if score is not None:
                if score >= camp._FIT_NEAR:
                    fit_note = f"（相性◎ {score:.2f}・動きはそのまま移植）"
                elif score >= camp._FIT_FAR:
                    fit_note = f"（相性○ {score:.2f}・動きを微調整して合わせます）"
                else:
                    fit_note = f"（相性△ {score:.2f}・雰囲気が違うので動きを控えめに翻訳）"
        _camp_set(job_id, phase=f"{prov}がHTMLを書いています…（一番長い段階・2分前後）{fit_note}")
        result = camp.generate_camp(
            brief, use_model=False,
            base_site_id=base_site_id or None,
            anim_ref_id=anim_ref_id or None,
        )
        # 仕上がりチェック（薄い出力＝ハズレ回の検出）。警告はカードに出すだけで生成は止めない
        _camp_set(job_id, phase="仕上がりをチェック中…")
        q = quality.check_camp(result["file"])
        if q.get("warn"):
            result["quality_warn"] = q["warn"]
        # どの手本でどんな仕上がりだったかを記録に残す
        # （手本ごとのハズレ率が見えてきたら「選んだ瞬間の事前警告」に使う予定）
        quality.log_result(result["file"], base_site_id, anim_ref_id,
                           result.get("model", ""), q)
        _camp_set(job_id, state="done", **result)
    except Exception as exc:  # noqa: BLE001
        log.exception("カンプ生成に失敗")
        _camp_set(job_id, state="error", message=str(exc))


@app.route("/api/generate_camp", methods=["POST"])
def api_generate_camp():
    """ブリーフからカンプを生成する（非同期・複数同時OK）。進捗は /api/generate_camp/status。

    base_id を渡すと配色・字組みを強く寄せる。anim_id で動きの種類を寄せる（mix & match）。
    返り値の job_id で、そのジョブの進捗を追える。
    """
    if not _gen_ready():
        return jsonify({"ok": False, "message": "生成エンジンのAPIキーが未設定です（⚙設定を確認）"}), 400
    data = request.get_json(silent=True) or {}
    brief = (data.get("brief") or "").strip()
    base_site_id = (data.get("base_id") or "").strip()
    anim_ref_id = (data.get("anim_id") or "").strip()
    if not brief:
        return jsonify({"ok": False, "message": "作りたいサイトの説明を入力してください"}), 400
    with _CAMP_LOCK:
        running = sum(1 for j in _CAMP_JOBS.values() if j.get("state") == "running")
        if running >= _CAMP_MAX:
            return jsonify(
                {"ok": False, "message": f"同時生成は最大{_CAMP_MAX}件までです（少し待って）"}
            ), 429
        job_id = uuid.uuid4().hex
        _CAMP_JOBS[job_id] = {"state": "running", "brief": brief, "phase": "開始しています…"}
    log.info("カンプ生成ジョブ開始[%s]: %s (base=%s, anim=%s)", job_id[:6], brief, base_site_id, anim_ref_id)
    threading.Thread(
        target=_run_camp_job, args=(job_id, brief, base_site_id, anim_ref_id), daemon=True
    ).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/generate_camp/status")
def api_generate_camp_status():
    """全カンプ生成ジョブの状態を返す（複数同時進行を1回で取得）。"""
    with _CAMP_LOCK:
        return jsonify({"jobs": {k: dict(v) for k, v in _CAMP_JOBS.items()}})


@app.route("/api/camp_suggest", methods=["POST"])
def api_camp_suggest():
    """カンプを見てAIが改善案を複数出す（同期。ユーザーは選ぶだけ）。"""
    if not _edit_ready():
        return jsonify({"ok": False, "message": "修正エンジンのAPIキーが未設定です（⚙設定を確認）"}), 400
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    try:
        section = int(data.get("section", -1))
    except Exception:  # noqa: BLE001
        section = -1
    if not fn or not (config.CAMP_DIR / fn).exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    try:
        suggestions = camp.suggest_edits(fn, section=section)
    except Exception as exc:  # noqa: BLE001
        log.exception("改善案の生成に失敗")
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True, "suggestions": suggestions})


@app.route("/api/swap_image", methods=["POST"])
def api_swap_image():
    """カンプ内の画像を、アップロード画像に手で差し替える（AI不使用・無料・一瞬）。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    src = (data.get("src") or "").strip()
    try:
        index = int(data.get("index", -1))
    except Exception:  # noqa: BLE001
        index = -1
    if not fn or not (config.CAMP_DIR / fn).exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    try:
        result = camp.swap_image(fn, index, src)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, **result})


def _run_edit_job(job_id: str, fn: str, section: int, instruction: str, keep_text: bool = False, style_type: str = "") -> None:
    """バックグラウンドでカンプを部分編集する（生成ジョブ一覧に相乗り）。"""
    try:
        _ep = config.CONFIG.htmlgen.edit_provider
        prov = {"openai": "GPT", "gemini": "Gemini", "deepseek": "DeepSeek"}.get(_ep, "Claude")
        scope = "全体" if section is None or section < 0 else f"セクション{section + 1}"
        _camp_set(job_id, phase=f"{prov}が{scope}を直しています…")
        result = camp.edit_camp_section(fn, section, instruction, keep_text=keep_text, style_type=style_type)
        _camp_set(job_id, state="done", **result)
    except Exception as exc:  # noqa: BLE001
        log.exception("部分編集に失敗")
        _camp_set(job_id, state="error", message=str(exc))


@app.route("/api/edit_camp", methods=["POST"])
def api_edit_camp():
    """既存カンプを、指示（または改善案）で直して新バージョンを作る（非同期・複数OK）。"""
    if not _edit_ready():
        return jsonify({"ok": False, "message": "修正エンジンのAPIキーが未設定です（⚙設定を確認）"}), 400
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    instruction = (data.get("instruction") or "").strip()
    try:
        section = int(data.get("section", -1))
    except Exception:  # noqa: BLE001
        section = -1
    if not fn or not (config.CAMP_DIR / fn).exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    if not instruction:
        return jsonify({"ok": False, "message": "修正指示を入れてください"}), 400
    keep_text = bool(data.get("keep_text"))  # ✨おしゃれ化など「中身は変えない」系＝テキスト保全ゲートON
    style_type = str(data.get("style_type") or "").strip()[:40]  # 使った型名→data-cestyleで刻印（型のページ内重複防止）
    with _CAMP_LOCK:
        running = sum(1 for j in _CAMP_JOBS.values() if j.get("state") == "running")
        if running >= _CAMP_MAX:
            return jsonify({"ok": False, "message": f"同時処理は最大{_CAMP_MAX}件までです（少し待って）"}), 429
        job_id = uuid.uuid4().hex
        _CAMP_JOBS[job_id] = {"state": "running", "brief": f"部分編集: {instruction[:24]}", "phase": "開始しています…"}
    log.info("部分編集ジョブ開始[%s]: %s section=%s keep_text=%s style=%s / %s", job_id[:6], fn, section, keep_text, style_type, instruction)
    threading.Thread(
        target=_run_edit_job, args=(job_id, fn, section, instruction, keep_text, style_type), daemon=True
    ).start()
    return jsonify({"ok": True, "job_id": job_id})


# ── ユーザー自前画像のアップロード（AIが説明を付けてカンプに使う）──────────────
_UP_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_CAP_LOCK = threading.Lock()
_CAP_RUNNING = {"on": False}


def _safe_upload_name(orig: str) -> str:
    ext = Path(orig or "").suffix.lower()
    if ext not in _UP_EXTS:
        ext = ".jpg"
    return "up_" + uuid.uuid4().hex[:10] + ext


def _caption_worker() -> None:
    """説明が未設定のアップロード画像に、AIで1行キャプションを付ける（裏で順次）。"""
    try:
        while True:
            target = None
            for u in camp.list_uploads():
                if not u["caption"]:
                    target = u
                    break
            if target is None:
                return
            cap = camp.caption_image(config.UPLOAD_DIR / target["file"]) or "(説明なし)"
            meta = camp.load_uploads_meta()
            meta[target["file"]] = cap
            camp.save_uploads_meta(meta)
    finally:
        with _CAP_LOCK:
            _CAP_RUNNING["on"] = False


def _start_captioning() -> None:
    with _CAP_LOCK:
        if _CAP_RUNNING["on"]:
            return
        _CAP_RUNNING["on"] = True
    threading.Thread(target=_caption_worker, daemon=True).start()


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    """アップロードした自前画像を返す。"""
    path = config.UPLOAD_DIR / filename
    if not path.exists() or not path.is_file():
        abort(404)
    return send_file(path)


@app.route("/api/uploads")
def api_uploads():
    """アップロード画像の一覧（説明つき）。"""
    return jsonify({"uploads": camp.list_uploads(), "captioning": _CAP_RUNNING["on"]})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """画像を1〜複数アップロード → 保存 → 裏でAIが説明を付ける。"""
    files = request.files.getlist("images")
    if not files:
        return jsonify({"ok": False, "message": "画像がありません"}), 400
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    for f in files:
        if not f or not f.filename:
            continue
        if Path(f.filename).suffix.lower() not in _UP_EXTS:
            continue
        f.save(str(config.UPLOAD_DIR / _safe_upload_name(f.filename)))
        saved += 1
    _start_captioning()
    log.info("画像アップロード: %d 枚", saved)
    return jsonify({"ok": True, "saved": saved, "uploads": camp.list_uploads()})


@app.route("/api/import_folder", methods=["POST"])
def api_import_folder():
    """PC上のフォルダを指定して、中の画像をごっそり取り込む（ローカルツール用）。"""
    data = request.get_json(silent=True) or {}
    folder = (data.get("path") or "").strip().strip('"')
    src = Path(folder)
    if not folder or not src.exists() or not src.is_dir():
        return jsonify({"ok": False, "message": "フォルダが見つかりません"}), 404
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    import shutil
    saved = 0
    for p in sorted(src.iterdir()):
        if p.is_file() and p.suffix.lower() in _UP_EXTS:
            try:
                shutil.copy2(p, config.UPLOAD_DIR / _safe_upload_name(p.name))
                saved += 1
            except Exception:  # noqa: BLE001
                continue
    _start_captioning()
    log.info("フォルダ取り込み: %s → %d 枚", folder, saved)
    return jsonify({"ok": True, "saved": saved, "uploads": camp.list_uploads()})


@app.route("/api/upload_delete", methods=["POST"])
def api_upload_delete():
    """アップロード画像を1件削除する。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    p = config.UPLOAD_DIR / fn
    if not fn or p.parent != config.UPLOAD_DIR or not p.exists():
        return jsonify({"ok": False, "message": "見つかりません"}), 404
    try:
        p.unlink()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 500
    meta = camp.load_uploads_meta()
    meta.pop(fn, None)
    camp.save_uploads_meta(meta)
    return jsonify({"ok": True})


@app.route("/api/camps")
def api_camps():
    """保存済みカンプの一覧（履歴）。名前付き（お気に入り）を先頭に、あとは新しい順。"""
    names = camp.load_camp_names()
    items = []
    # 生成カンプ(camp_*)＋お気に入りスナップショット(fav_*)＋忠実クローン(clone_*)を拾う
    globbed = (
        list(config.CAMP_DIR.glob("camp_*.html"))
        + list(config.CAMP_DIR.glob("fav_*.html"))
        + list(config.CAMP_DIR.glob("clone_*.html"))
    )
    for p in sorted(globbed):
        try:
            head = p.read_text(encoding="utf-8", errors="ignore")[:2000]
        except Exception:  # noqa: BLE001
            head = ""
        m = re.search(r"<title>(.*?)</title>", head, flags=re.IGNORECASE | re.DOTALL)
        title = (re.sub(r"\s+", " ", m.group(1)).strip() if m else "")[:60]
        st = p.stat()
        info = names.get(p.name, {})
        items.append({
            "file": p.name, "title": title, "mtime": st.st_mtime, "size": st.st_size,
            "name": info.get("name", ""), "fav": bool(info.get("fav")),
        })
    # お気に入り（名前付き）を上に、その中と外はそれぞれ新しい順
    items.sort(key=lambda x: (0 if x["fav"] else 1, -x["mtime"]))
    return jsonify({"camps": items})


@app.route("/api/camp_name", methods=["POST"])
def api_camp_name():
    """カンプに名前を付けて保存する（お気に入り登録）。名前が空なら登録解除。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    name = (data.get("name") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    info = camp.set_camp_name(fn, name)
    return jsonify({"ok": True, "name": info.get("name", ""), "fav": bool(info.get("fav"))})


@app.route("/api/camp_rate", methods=["POST"])
def api_camp_rate():
    """カンプにユーザー評価（◎○△✖）を付ける。同じ評価をもう一度押すと解除。

    評価は品質ログ(_quality_log.json)に載り、手本ごとのハズレ率集計に使われる。
    """
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    rating = (data.get("rating") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    if quality.get_rating(fn) == rating:
        rating = ""  # 同じ評価をもう一度＝解除
    if not quality.set_rating(fn, rating):
        return jsonify({"ok": False, "message": "評価は ◎ ○ △ ✖ のどれかです"}), 400
    return jsonify({"ok": True, "rating": rating})


@app.route("/api/camp_rate")
def api_camp_rate_get():
    """カンプの現在の評価を返す（編集バーの初期表示用）。"""
    fn = (request.args.get("file") or "").strip()
    return jsonify({"ok": True, "rating": quality.get_rating(fn)})


@app.route("/api/base_stats")
def api_base_stats():
    """手本（ベース）ごとの過去実績（◎○△✖と自動判定NG）を返す。選択時のヒント用。"""
    base_id = (request.args.get("base_id") or "").strip()
    if not base_id:
        return jsonify({"ok": True, "note": ""})
    return jsonify({"ok": True, **quality.base_stats(base_id)})


@app.route("/api/save_favorite", methods=["POST"])
def api_save_favorite():
    """現在の完成形DOM（見た目＋焼き込んだ動き）を『お気に入り』として複製保存する。
    クライアントが編集UIを除いたHTML全体（cleanHtml）＋名前を送ってくる。"""
    data = request.get_json(silent=True) or {}
    html = data.get("html") or ""
    name = (data.get("name") or "").strip()
    try:
        info = camp.save_favorite(html, name)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True, "file": info["file"], "name": info["name"]})


@app.route("/api/camp_delete", methods=["POST"])
def api_camp_delete():
    """保存済みカンプを1件削除する（履歴の掃除用）。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "見つかりません"}), 404
    try:
        p.unlink()
        # クローンの素材フォルダ（<名前>_files）も一緒に消す
        files_dir = config.CAMP_DIR / f"{p.stem}_files"
        if files_dir.is_dir():
            import shutil
            shutil.rmtree(files_dir, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/api/export_split", methods=["POST"])
def api_export_split():
    """カンプを納品用に HTML/CSS/JS＋images に分割し、zip 化して返す（AIなし・後処理）。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    try:
        result = export_split.export_split(fn)
    except Exception as exc:  # noqa: BLE001
        log.exception("分割エクスポートに失敗: %s", fn)
        return jsonify({"ok": False, "message": str(exc)}), 500
    if not result.get("ok"):
        return jsonify(result), 400
    result["download"] = "/exports/" + result["zip"]
    return jsonify(result)


@app.route("/api/style_check", methods=["POST"])
def api_style_check():
    """カンプを Vision AI で採点（有名サイト基準）。同期・十数〜数十秒かかる。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    try:
        result = style_check.style_check(fn, data.get("provider"))
    except Exception as exc:  # noqa: BLE001
        log.exception("おしゃれ度チェックに失敗: %s", fn)
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/exports/<path:filename>")
def export_file(filename: str):
    """分割エクスポートした zip をダウンロードさせる。"""
    path = export_split.EXPORT_DIR / filename
    if not path.exists() or not path.is_file() or path.suffix != ".zip":
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename)


@app.route("/api/save_camp_html", methods=["POST"])
def api_save_camp_html():
    """ドラッグ/矢印での位置調整をHTMLに焼き込む（LLM不使用・その場保存）。

    クライアントが編集UI（#__ce系）を除いたHTML全体を送ってくる。念のため
    最低限の妥当性（htmlタグがある・空でない）を見てから上書き保存する。
    """
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    html = data.get("html") or ""
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "ファイルが見つかりません"}), 404
    if len(html) < 200 or "</html>" not in html.lower():
        return jsonify({"ok": False, "message": "HTMLが空か壊れています（保存中止）"}), 400
    try:
        # 一時ファイルに書いてから差し替え＝書き込み途中で落ちても元ファイルが壊れない
        tmp = p.with_suffix(".html.tmp")
        tmp.write_text(html, encoding="utf-8")
        os.replace(tmp, p)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True, "file": fn})


@app.route("/api/pair_fit")
def api_pair_fit():
    """ベース×アニメ参照の相性スコア（選んだ瞬間にUIへ出す用・LLM不使用で一瞬）。"""
    base_id = (request.args.get("base") or "").strip()
    anim_id = (request.args.get("anim") or "").strip()
    score = camp.pair_fit_score(base_id, anim_id)
    if score is None:
        return jsonify({"ok": True, "score": None, "label": ""})
    if score >= camp._FIT_NEAR:
        label = f"相性◎ {score:.2f}：雰囲気が近い組み合わせ。動きはそのまま移植されます"
    elif score >= camp._FIT_FAR:
        label = f"相性○ {score:.2f}：動きの種類は活かしつつ、速さ・強さをベースに合わせて微調整します"
    else:
        label = f"相性△ {score:.2f}：雰囲気がだいぶ違うので、動きは控えめに翻訳されます（種類のヒントだけ借ります）"
    return jsonify({"ok": True, "score": score, "label": label})


@app.route("/api/pair_fit_all")
def api_pair_fit_all():
    """ベースに対する全サイトの相性スコアを一括で返す（アニメ選択の並び替え用）。"""
    base_id = (request.args.get("base") or "").strip()
    if not base_id:
        return jsonify({"ok": True, "scores": {}})
    scores = {}
    with db.connect() as conn:
        rows = conn.execute("SELECT id FROM site").fetchall()
    for r in rows:
        s = camp.pair_fit_score(base_id, r["id"])
        if s is not None:
            scores[r["id"]] = round(s, 3)
    return jsonify({"ok": True, "scores": scores,
                    "near": camp._FIT_NEAR, "far": camp._FIT_FAR})


@app.route("/api/camp_sections")
def api_camp_sections():
    """カンプのセクション一覧（編集バーのドロップダウン用）。"""
    fn = (request.args.get("file") or "").strip()
    if not fn or not (config.CAMP_DIR / fn).exists():
        return jsonify({"ok": False, "sections": []}), 404
    html = (config.CAMP_DIR / fn).read_text(encoding="utf-8")
    return jsonify({"ok": True, "sections": camp.list_camp_sections(html)})


@app.route("/api/edit_element", methods=["POST"])
def api_edit_element():
    """右クリックした『その要素1つだけ』をAIで直す（他は触らない）。DOM側で差し替える。"""
    data = request.get_json(silent=True) or {}
    try:
        new_html = camp.edit_element(
            data.get("html", ""), data.get("css", ""), data.get("instruction", "")
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, "html": new_html})


@app.route("/api/section_fav/save", methods=["POST"])
def api_section_fav_save():
    """セクション1つを『お気に入り部品』として保存する（AIなし）。"""
    data = request.get_json(silent=True) or {}
    try:
        entry = camp.save_section_fav(
            data.get("html", ""), data.get("headcss", ""), data.get("name", "")
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, "fav": entry})


@app.route("/api/section_fav/list")
def api_section_fav_list():
    """保存済みセクション部品を、プレビュー用HTML/CSS付きで返す。"""
    return jsonify({"ok": True, "favs": camp.list_section_favs()})


@app.route("/api/section_fav/delete", methods=["POST"])
def api_section_fav_delete():
    data = request.get_json(silent=True) or {}
    camp.delete_section_fav((data.get("id") or "").strip())
    return jsonify({"ok": True})


# カンプ画面の隅に出す編集バー（ツール経由で開いた時だけ注入。保存ファイルは汚さない）
_EDIT_BAR = """
<style>
#__ce{position:fixed;right:20px;top:20px;z-index:2147483000;width:480px;max-width:94vw;background:#fff;border:1px solid #e3e3e8;border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,.32);font-family:system-ui,-apple-system,sans-serif;color:#1d1d1f}
#__ce *{box-sizing:border-box}
#__ce .hd{display:flex;align-items:center;gap:8px;background:#1d1d1f;color:#fff;padding:13px 16px;font-weight:700;font-size:15px;cursor:pointer;border-radius:16px 16px 0 0}
#__ce .hd .t{flex:1}
#__ce .hd .x{font-size:13px;background:rgba(255,255,255,.18);padding:3px 10px;border-radius:999px}
#__ce .hd .sv{font-size:12.5px;background:#b8530a;padding:4px 11px;border-radius:999px;font-weight:700;white-space:nowrap}
#__ce .hd .sv.saved{background:#1a7f37}
#__ce.min .hd{border-radius:16px}
#__ce .bd{padding:14px 16px;display:flex;flex-direction:column;gap:9px;max-height:82vh;overflow:auto}
#__ce.min .bd{display:none}
#__ce select,#__ce input{font-size:13.5px;padding:9px 11px;border:1px solid #d0d0d5;border-radius:9px;width:100%;font-family:inherit}
#__ce .row{display:flex;gap:8px}
#__ce button{font-size:13.5px;font-weight:700;border:none;border-radius:9px;padding:9px 13px;cursor:pointer}
#__ce .sg{background:#efe9ff;color:#4b2ea8}
#__ce .go{background:#b8530a;color:#fff;flex:1}
#__ce button:disabled{opacity:.5;cursor:default}
#__ce .chips{display:flex;flex-wrap:wrap;gap:6px}
#__ce .chip{background:#f2f2f4;border:1px solid #ddd;border-radius:999px;padding:6px 10px;font-size:12px;cursor:pointer;text-align:left;color:#1d1d1f}
#__ce .chip:hover{background:#e6e6ec}
#__ce .msg{font-size:12.5px;color:#666;min-height:16px}
#__ce .im{background:#eafbf0;color:#1b8a4b;border:1px solid #b7e6c9;width:100%}
#__ce .lbl{font-size:12.5px;font-weight:700;color:#2b6cb0;margin-top:6px;border-top:1px solid #eee;padding-top:9px}
#__ce .lbl.plain{color:#555;border-top:none;padding-top:0}
#__ce .ags{display:grid;grid-template-columns:1fr 1fr;gap:7px}
#__ce .ag{text-align:left;background:#f4f8ff;border:1px solid #d6e4fb;border-radius:10px;padding:8px 10px;cursor:pointer;line-height:1.3}
#__ce .ag:hover{background:#e6f0fe;border-color:#b9d4f7}
#__ce .ag b{display:block;font-size:13px;color:#1d1d1f;font-weight:700}
#__ce .ag span{font-size:11px;color:#7a7a80;font-weight:400}
.__ce_hl{outline:3px solid #ff8a00 !important;outline-offset:2px;cursor:pointer !important}
.__ce_sechl{outline:3px solid #e8a300 !important;outline-offset:-3px;box-shadow:0 0 0 3px rgba(232,163,0,.2) inset !important}
#__ce_pk{position:fixed;inset:0;z-index:2147483001;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center}
#__ce_pk .bx{background:#fff;border-radius:12px;padding:16px;max-width:640px;width:92%;max-height:80vh;overflow:auto;font-family:system-ui,sans-serif}
#__ce_pk h4{margin:0 0 12px;font-size:15px}
#__ce_pk .secgr{display:grid;grid-template-columns:repeat(auto-fill,160px);gap:10px;justify-content:center}
#__ce_pk .sit{position:relative;width:160px;border:1px solid #e2e2e6;border-radius:8px;overflow:hidden;cursor:pointer;background:#fff}
#__ce_pk .sit:hover{border-color:#e8a300;box-shadow:0 6px 16px rgba(0,0,0,.18)}
#__ce_pk .sit .pv{width:160px;height:101px;overflow:hidden;background:#fff;pointer-events:none}
#__ce_pk .sit .pv iframe{width:1200px;height:760px;border:none;transform:scale(.1333);transform-origin:top left}
#__ce_pk .sit .nm{font-size:11px;font-weight:700;color:#1d1d1f;padding:5px 7px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#__ce_pk .sit .del{position:absolute;top:4px;right:4px;background:rgba(0,0,0,.55);color:#fff;border:none;border-radius:999px;width:20px;height:20px;cursor:pointer;font-size:12px;line-height:18px;padding:0}
#__ce_pk .cl{float:right;cursor:pointer;font-size:18px;font-weight:700;color:#888}
#__ce_pk .gr{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:10px}
#__ce_pk .it{border:1px solid #eee;border-radius:8px;overflow:hidden;cursor:pointer;background:#fff}
#__ce_pk .it:hover{border-color:#2b6cb0;box-shadow:0 4px 12px rgba(0,0,0,.15)}
#__ce_pk .it img{width:100%;height:80px;object-fit:cover;display:block;background:#eef2f7}
#__ce_pk .it span{display:block;font-size:11px;color:#555;padding:4px 6px}
#__ce_pk .favgr{display:grid;grid-template-columns:1fr;gap:8px}
#__ce_pk .favgr .it{padding:10px 12px;cursor:pointer}
#__ce_pk .favgr .it.now{border-color:#e8a300;background:#fffaf0}
#__ce_pk .favgr .nm{font-weight:700;font-size:13px;color:#1d1d1f}
#__ce_pk .favgr .dt{font-size:11px;color:#888;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#__ce_cm{position:fixed;z-index:2147483002;width:290px;max-width:92vw;background:#fff;border:1px solid #ddd;border-radius:12px;box-shadow:0 16px 44px rgba(0,0,0,.32);font-family:system-ui,sans-serif;color:#1d1d1f;overflow:hidden}
#__ce_cm .h{background:#1d1d1f;color:#fff;padding:9px 12px;font-size:12px;font-weight:700;display:flex;gap:6px;align-items:center}
#__ce_cm .h .t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#__ce_cm .h .c{cursor:pointer;background:rgba(255,255,255,.2);border-radius:999px;padding:1px 8px}
#__ce_cm .bd2{padding:9px;max-height:64vh;overflow:auto}
#__ce_cm .cap{font-size:11px;color:#888;margin:2px 0 6px}
#__ce_cm .ag2{display:block;width:100%;text-align:left;background:#f4f8ff;border:1px solid #d6e4fb;border-radius:8px;padding:6px 9px;margin-bottom:5px;cursor:pointer;font-size:12.5px;font-weight:700;color:#1d1d1f}
#__ce_cm .ag2 span{display:block;font-size:10.5px;color:#8a8a90;font-weight:400}
#__ce_cm .ag2:hover{background:#e6f0fe}
#__ce_cm input{width:100%;font-size:12.5px;padding:7px 9px;border:1px solid #d0d0d5;border-radius:8px;font-family:inherit}
#__ce_cm .go2{width:100%;border:none;border-radius:8px;padding:8px;font-weight:700;cursor:pointer;margin-top:6px;font-size:12.5px;color:#fff;background:#b8530a}
#__ce_cm .chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
#__ce_cm .chip{background:#f2f2f4;border:1px solid #ddd;border-radius:999px;padding:5px 9px;font-size:11.5px;cursor:pointer;color:#1d1d1f}
.__ce_sel{outline:2px dashed #2b7fff !important;outline-offset:3px}
html.__ce_altmode{cursor:text}
#__ce_cm .__ce_nudge{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin:4px 0 6px}
#__ce_cm .__ce_nudge button{background:#eef3ff;border:1px solid #cfe0fb;border-radius:7px;padding:8px 0;font-size:14px;cursor:pointer;color:#1d1d1f;font-weight:700}
#__ce_cm .__ce_nudge button:hover{background:#dceafe}
#__ce_cm .__ce_nudge .sp{visibility:hidden}
#__ce_cm .__ce_size{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:2px 0 8px}
#__ce_cm .__ce_size button{background:#eef3ff;border:1px solid #cfe0fb;border-radius:7px;padding:8px 0;font-size:13px;cursor:pointer;color:#1d1d1f;font-weight:700}
#__ce_cm .__ce_size button:hover{background:#dceafe}
#__ce_cm .__ce_grp{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:2px 0 8px}
#__ce_cm .__ce_grp button{background:#f2fbf5;border:1px solid #cfead6;border-radius:7px;padding:8px 0;font-size:12.5px;cursor:pointer;color:#1d1d1f;font-weight:700}
#__ce_cm .__ce_grp button:hover{background:#dff3e4}
#__ce_cm .__ce_grp button.on{outline:2px solid #1a7f37;background:#c9f1d6}
#__ce_savebar{position:fixed;left:20px;bottom:20px;z-index:2147483003;background:#1a7f37;color:#fff;border:none;border-radius:12px;padding:13px 20px;font-size:14px;font-weight:700;cursor:pointer;box-shadow:0 12px 32px rgba(0,0,0,.3);font-family:system-ui,sans-serif;display:none}
#__ce_savebar.show{display:block}
#__ce_cm .__ce_anim{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px}
#__ce_cm .__ce_anim button{text-align:left;background:#fff4fb;border:1px solid #f3cfe8;border-radius:8px;padding:6px 8px;cursor:pointer;line-height:1.25}
#__ce_cm .__ce_anim b{display:block;font-size:12.5px;color:#1d1d1f;font-weight:700}
#__ce_cm .__ce_anim span{font-size:10.5px;color:#a06a8f}
#__ce_cm .__ce_anim button.on{outline:2px solid #1a7f37;background:#eaf8ee}
#__ce_cm .__fx_ctl{background:#f2fbf5;border:1px solid #cfead6;border-radius:8px;padding:8px;margin:0 0 8px}
#__ce_cm .__fx_ctl label{display:block;font-size:11px;color:#3a6b48;margin-bottom:8px;font-weight:700}
#__ce_cm .__fx_ctl label span{float:right;color:#1a7f37}
#__ce_cm .__fx_ctl input[type=range]{width:100%;margin-top:3px;accent-color:#1a7f37}
@keyframes __ceax_fadeup{from{opacity:0;transform:translateY(28px)}to{opacity:1;transform:none}}
@keyframes __ceax_fade{from{opacity:0}to{opacity:1}}
@keyframes __ceax_left{from{opacity:0;transform:translateX(-48px)}to{opacity:1;transform:none}}
@keyframes __ceax_right{from{opacity:0;transform:translateX(48px)}to{opacity:1;transform:none}}
@keyframes __ceax_zoom{from{opacity:0;transform:scale(.86)}to{opacity:1;transform:none}}
@keyframes __ceax_pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.06)}}
@keyframes __ceax_float{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}
@keyframes __ceax_bounce{0%{transform:translateY(0)}30%{transform:translateY(-18px)}60%{transform:translateY(0)}80%{transform:translateY(-8px)}100%{transform:translateY(0)}}
.__ceax_fadeup{animation:__ceax_fadeup .8s ease both}
.__ceax_fade{animation:__ceax_fade .8s ease both}
.__ceax_left{animation:__ceax_left .8s ease both}
.__ceax_right{animation:__ceax_right .8s ease both}
.__ceax_zoom{animation:__ceax_zoom .8s ease both}
.__ceax_pulse{animation:__ceax_pulse 1.6s ease-in-out infinite}
.__ceax_float{animation:__ceax_float 2.4s ease-in-out infinite}
.__ceax_bounce{animation:__ceax_bounce 1.2s ease infinite}
#__ce_rate{display:inline-flex;gap:4px;margin:0 4px;align-items:center}
#__ce_rate .rt{width:26px;height:26px;line-height:26px;text-align:center;border-radius:50%;background:#3a3a3f;color:#bbb;cursor:pointer;font-size:14px;font-weight:700}
#__ce_rate .rt:hover{background:#55555c;color:#fff}
#__ce_rate .rt.on{background:#ff9a3c;color:#fff}
#__ce_rate.done .rt{display:none}
#__ce_rate.done .rt.on{display:inline-block}
#__ce_toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);z-index:2147483004;background:#1d1d1f;color:#fff;border-radius:14px;padding:15px 24px;box-shadow:0 16px 44px rgba(0,0,0,.42);font-family:system-ui,sans-serif;min-width:320px;max-width:92vw;text-align:center}
#__ce_toast .bar{height:7px;background:#43434a;border-radius:4px;overflow:hidden;margin-bottom:9px}
#__ce_toast .bar span{display:block;height:100%;width:35%;background:#ff9a3c;border-radius:4px;animation:__ce_flow 1.2s ease-in-out infinite}
@keyframes __ce_flow{0%{margin-left:-35%}100%{margin-left:100%}}
#__ce_toast .tx{font-size:14.5px;font-weight:700}
/* 改善中のセクションを目立たせる（今どこを処理しているか一目で分かる） */
.__ce_busy{position:relative !important;outline:4px solid #7c3aed !important;outline-offset:-4px;animation:__ce_busypulse 1.1s ease-in-out infinite}
.__ce_busy::after{content:'✨ このセクションをAIが改善中…';position:absolute;top:12px;left:50%;transform:translateX(-50%);background:#7c3aed;color:#fff;padding:9px 18px;border-radius:999px;font-family:system-ui,sans-serif;font-size:15px;font-weight:700;box-shadow:0 10px 28px rgba(124,58,237,.45);z-index:2147483005;white-space:nowrap;pointer-events:none}
@keyframes __ce_busypulse{0%,100%{outline-color:#7c3aed}50%{outline-color:#d3bef7}}
</style>
<div id="__ce" class="min">
  <div class="hd" id="__ce_hd"><span>✏</span><span class="t">このカンプを直す</span><span id="__ce_rate" title="このカンプの出来を評価（手本ごとのハズレ率集計に使われます）"><span class="rt" data-r="◎">◎</span><span class="rt" data-r="○">○</span><span class="rt" data-r="△">△</span><span class="rt" data-r="✖">✖</span></span><span class="x" id="__ce_homeh" style="background:#2b6cb0" title="ツール（ホーム）に戻る">🏠 ホーム</span><span class="sv" id="__ce_undo" style="background:#555;opacity:.4" title="ひとつ前に戻す">⟲ 戻す</span><span class="sv" id="__ce_save">💾 保存</span><span class="x" id="__ce_mn">▲ ひらく</span></div>
  <div class="bd">
    <button class="im" id="__ce_home" style="background:#eef2f7;color:#1d1d1f;border:1px solid #d6deea;font-weight:700">🏠 ツール（ホーム）に戻る</button>
    <div class="lbl plain">🎨 ベース色（テーマ色・AIなし・ページ全体に反映）</div>
    <div class="row" style="align-items:center"><input type="color" id="__ce_base" style="width:54px;height:38px;padding:2px;border:1px solid #d0d0d5;border-radius:9px;cursor:pointer;flex:none"><button class="im" id="__ce_baser" style="background:#f2f2f4;color:#1d1d1f;border:1px solid #ddd;flex:1;margin:0">⟲ 元の色に戻す</button></div>
    <div class="msg" id="__ce_basemsg" style="min-height:0;margin-top:2px"></div>
    <div class="lbl plain">🚫 背景の飾りを消す（わっか/ぼかし等・クリックで消せない装飾）</div>
    <button class="im" id="__ce_nodeco" style="background:#f2f2f4;color:#1d1d1f;border:1px solid #ddd">🚫 背景の飾り（わっか等）を消す</button>
    <div class="lbl plain">➕ 文字・画像を追加（AIなし・ドラッグで置く→右クリックで編集）</div>
    <div class="row" style="gap:8px"><button class="im" id="__ce_addtext" style="background:#0b6bcb;color:#fff;flex:1;margin:0">➕ 文字を追加</button><button class="im" id="__ce_addimg" style="background:#0b6bcb;color:#fff;flex:1;margin:0">🖼 画像を追加</button></div>
    <div class="lbl plain">🧹 全体を規則化（左右の余白・見出しを一律に揃える・明らかに違う余白は別扱い・AIなし＝一貫性UP）</div>
    <button class="im" id="__ce_normalize" style="background:#0b6e4f;color:#fff">🧹 余白・見出しを一律に揃える</button>
    <button class="im" id="__ce_btncolor" style="background:#0b6e4f;color:#fff">🎨 全ボタンをテーマ色に統一</button>
    <div class="lbl plain">🤖 修正・おしゃれに使うAI（モデルは⚙設定で）</div>
    <select id="__ce_ai"><option value="anthropic">Claude</option><option value="openai">GPT</option><option value="gemini">Gemini</option><option value="deepseek">DeepSeek（激安）</option><option value="zai">GLM（Z.ai・激安）</option></select>
    <div class="lbl plain">① 範囲を選ぶ（全体／セクション）</div>
    <select id="__ce_sec"><option value="-1">ページ全体</option></select>
    <div class="lbl">🎬 アニメ・背景装飾を付ける（選んだ所に適用）</div>
    <div class="ags" id="__ce_ags"></div>
    <div class="lbl">💡 選んだ所の改善案（AIが画面を見てたくさん提案）</div>
    <div class="row"><button class="sg" id="__ce_sg">💡 この部分の案を出す</button></div>
    <div class="chips" id="__ce_chips"></div>
    <div class="lbl">✍ 自分で指示</div>
    <div class="row"><input id="__ce_in" placeholder="例：見出しを大きく／CTAを黄色に"><button class="go" id="__ce_go">直す</button></div>
    <button class="im" id="__ce_img">🖼 画像を差し替え（AIなし・無料）</button>
    <button class="im" id="__ce_align" title="各セクションの中身の幅を測り、多数派の幅にそろえます。全幅セクションや明らかに違う幅は触りません">📐 横幅をそろえる（AIなし・無料）</button>
    <div class="lbl plain">🎨 一括改善の手本（ストックの登録サイトに寄せる）</div>
    <select id="__ce_ref"><option value="">なし（AIおまかせ）</option></select>
    <button class="im" id="__ce_improve" style="background:#7c3aed;color:#fff">🚀 ページ全体を今風に（一括改善）</button>
    <div class="lbl plain">🎬 オープニング演出（開いた瞬間に幕→フェードで本体へ・AIなし）</div>
    <button class="im" id="__ce_op_add" style="background:#0b6bcb;color:#fff">🎬 フェードのオープニングを付ける</button>
    <button class="im" id="__ce_op_edit" style="background:#eaf2fd;color:#0b4e8a;border:1px solid #bcd8f7">👁 オープニングを出す／隠す（ロゴ・文字は右クリックで差し替え）</button>
    <div class="lbl plain">⭐ セクションのお気に入り（①で選んだセクションが対象・AIなし）</div>
    <button class="im" id="__ce_fav" style="background:#e8a300;color:#fff">⭐ このセクションをお気に入り</button>
    <button class="im" id="__ce_favlist" style="background:#fff3d6;color:#8a5a00;border:1px solid #f0d38a">🔀 お気に入りからセクションを切り替え</button>
    <div class="lbl plain">🎨 おしゃれ度チェック（AIが有名サイト基準で採点＋改善点）</div>
    <button class="im" id="__ce_stylecheck" style="background:#c026a6;color:#fff">🎨 おしゃれ度をチェック</button>
    <button class="im" id="__ce_autopolish" style="background:#7c3aed;color:#fff">🎯 チェックして自動で磨く（採点→改善を一括・AI）</button>
    <div class="lbl plain">📦 納品用に書き出す（HTML/CSS/JS＋画像を分割・AIなし）</div>
    <button class="im" id="__ce_export" style="background:#0b6e4f;color:#fff">📦 分割エクスポート（zipで保存）</button>
    <div class="msg" id="__ce_msg">💡 直したい所を<b>右クリック</b>すると、その要素に直接アニメ・指示が出せます</div>
  </div>
</div>
<script>
(function(){
  var FILE=%FILE_JSON%;
  var box=document.getElementById('__ce');
  var sec=document.getElementById('__ce_sec'),inp=document.getElementById('__ce_in'),
      sg=document.getElementById('__ce_sg'),go=document.getElementById('__ce_go'),
      chips=document.getElementById('__ce_chips'),msg=document.getElementById('__ce_msg');
  // 修正・おしゃれに使うAIエンジンを、その場で切り替え（設定画面に行かず即反映）
  var aiSel=document.getElementById('__ce_ai');
  if(aiSel){
    aiSel.value=%EDIT_PROVIDER_JSON%;
    aiSel.addEventListener('change',function(){
      var nm=aiSel.options[aiSel.selectedIndex].text;
      fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({edit_provider:aiSel.value})})
        .then(function(r){return r.json();}).then(function(){ msg.textContent='修正・おしゃれに使うAIを「'+nm+'」にしました'; })
        .catch(function(){ msg.textContent='AIの切替に失敗しました'; });
    });
  }
  // 🏠 ツール（ホーム）に戻る＝このタブをツール本体に切り替える（タブを増やさない）
  function goHome(ev){
    if(ev) ev.stopPropagation();  // ヘッダの開閉トグルと競合させない
    if(_dirty && !confirm('保存していない変更があります。ホームに戻ると消えます。よろしいですか？')) return;
    location.href='/';
  }
  var homeBtn=document.getElementById('__ce_home');
  if(homeBtn) homeBtn.addEventListener('click',goHome);
  var homeH=document.getElementById('__ce_homeh');  // ヘッダ側＝畳んでいても常に押せる
  if(homeH) homeH.addEventListener('click',goHome);
  // 🚫 背景の飾り（わっか/ぼかし等）を消す：pointer-events:none で右クリックできない浮遊装飾をまとめて除去。
  var nodecoBtn=document.getElementById('__ce_nodeco');
  if(nodecoBtn) nodecoBtn.addEventListener('click',function(){
    var removed=0;
    [].slice.call(document.querySelectorAll('body *')).forEach(function(el){
      if(el.id && el.id.indexOf('__ce')===0) return;
      if(el.closest && (el.closest('#__ce')||el.closest('#__ce_cm')||el.closest('#__ce_pk'))) return;
      var cs; try{ cs=getComputedStyle(el); }catch(_){ return; }
      if(cs.pointerEvents!=='none') return;                        // クリックで消せない装飾だけを対象
      if((el.textContent||'').trim()!=='') return;                 // 文字を含むものは飾りではない
      if(el.querySelector && el.querySelector('img,video,svg,picture,canvas,input,button,a')) return;  // 中身のあるものは除外
      var pos=cs.position; if(pos!=='fixed' && pos!=='absolute') return;  // 背景に浮く配置だけ
      var isShape=(cs.borderRadius && cs.borderRadius!=='0px')||(cs.backgroundImage && cs.backgroundImage!=='none')||(cs.borderStyle && cs.borderStyle!=='none' && parseFloat(cs.borderWidth)>0)||(cs.filter && cs.filter!=='none');
      if(!isShape) return;                                         // 円/ぼかし/グラデ等の"形のある飾り"だけ
      el.remove(); removed++;
    });
    markDirty();
    msg.textContent = removed ? ('背景の飾りを '+removed+' 個消しました（「💾 保存」で確定・「⟲ 戻す」で復活）') : '消せる背景の飾りは見つかりませんでした（別の作り方かもしれません）';
  });
  // 自由配置（文字/画像）の重なり順：固定/追従ヘッダーより低い値にする。
  //   ★地雷：ここを本文より高い決め打ち値（旧:60）にすると、スクロール中にヘッダーの上へ来た瞬間
  //   要素がヘッダーを覆って見える（サイトごとにヘッダーのz-indexは違うので決め打ちは危険）。
  //   ページの実際のヘッダーを探し、それより必ず低い値にする（見つからなければ本文より上の無難な値）。
  function _freeZIndex(){
    try{
      var hdr=document.querySelector('header,.site-header,[class*="header"]');
      if(hdr){
        var z=parseInt(getComputedStyle(hdr).zIndex,10);
        if(!isNaN(z)) return Math.max(1, z-1);
      }
    }catch(_){}
    return 5;
  }
  // ➕ 文字を追加：新しい文字要素を今見えている位置に置く→すぐドラッグで移動できる。内容は右クリック→文字を編集で。
  var addTextBtn=document.getElementById('__ce_addtext');
  if(addTextBtn) addTextBtn.addEventListener('click',function(){
    var d=document.createElement('div');
    d.textContent='ここに文字';
    var x=Math.round((window.scrollX||window.pageXOffset||0)+window.innerWidth*0.30);
    var y=Math.round((window.scrollY||window.pageYOffset||0)+window.innerHeight*0.32);
    // 画面座標に絶対配置。読みやすい既定（太字・濃いグレー）。z-indexは編集UIより下・本文より上・ヘッダーより下。
    d.setAttribute('style','position:absolute;left:'+x+'px;top:'+y+'px;z-index:'+_freeZIndex()+';font-size:32px;font-weight:700;color:#333;font-family:inherit;line-height:1.4;padding:4px 8px;cursor:move;white-space:nowrap');
    document.body.appendChild(d);
    markDirty();
    try{ d.scrollIntoView({block:'center'}); }catch(_){}
    if(typeof setDragOn==='function'){ if(typeof curEl!=='undefined' && curEl) curEl.classList.remove('__ce_sel'); d.classList.add('__ce_sel'); setDragOn(d); }  // すぐ動かせる状態に
    msg.textContent='文字を追加しました。ドラッグで置く→右クリック→「✏ 文字を編集」で内容・色・大きさを変更（💾保存で確定）';
  });
  // 🖼 画像を追加：今の位置に画像要素を置く→すぐドラッグで移動できる（差し替え・サイズ調整は右クリックで）。
  function insertImageEl(url, idx){
    idx=idx||0;
    var img=document.createElement('img'); img.src=url;
    var x=Math.round((window.scrollX||window.pageXOffset||0)+window.innerWidth*0.30)+idx*24;
    var y=Math.round((window.scrollY||window.pageYOffset||0)+window.innerHeight*0.32)+idx*24;
    img.setAttribute('style','position:absolute;left:'+x+'px;top:'+y+'px;z-index:'+_freeZIndex()+';width:260px;height:auto;cursor:move');
    document.body.appendChild(img);
    markDirty();
    if(idx===0){ try{ img.scrollIntoView({block:'center'}); }catch(_){} }
    if(typeof setDragOn==='function'){ if(typeof curEl!=='undefined' && curEl) curEl.classList.remove('__ce_sel'); img.classList.add('__ce_sel'); setDragOn(img); }
  }
  function openAddImagePicker(){
    fetch('/api/uploads').then(function(r){return r.json();}).then(function(d){
      var ups=d.uploads||[];
      var items = ups.length
        ? ups.map(function(u){return '<div class="it" data-src="'+u.url+'"><img src="'+u.url+'"><span>'+esc(u.caption||u.file)+'</span></div>';}).join('')
        : '<div style="color:#999">まだアップロード画像がありません。下から新しく追加できます</div>';
      var ov=document.createElement('div'); ov.id='__ce_pk';
      ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span><h4>🖼 画像を追加</h4>'
        +'<label class="go2" style="display:block;text-align:center;background:#1a7f37;cursor:pointer;margin-bottom:10px">＋ 新しい画像をアップロード<input type="file" id="__ce_addimgfile" accept="image/*" multiple style="display:none"></label>'
        +'<div class="gr">'+items+'</div></div>';
      document.body.appendChild(ov);
      ov.addEventListener('click',function(e){
        if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
        var it=e.target.closest('.it'); if(it){ ov.remove(); insertImageEl(it.dataset.src); msg.textContent='画像を追加しました。ドラッグで置く（💾保存で確定）'; }
      });
      document.getElementById('__ce_addimgfile').addEventListener('change',function(){
        var files=this.files; if(!files||!files.length) return;
        var before={}; ups.forEach(function(u){ before[u.file]=1; });
        var fd=new FormData(); [].forEach.call(files,function(f){fd.append('images',f);});
        msg.textContent='アップロード中…';
        fetch('/api/upload',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(dd){
          if(!dd.ok){ msg.textContent='アップロード失敗：'+(dd.message||''); return; }
          ov.remove();
          var added=(dd.uploads||[]).filter(function(u){ return !before[u.file]; });
          added.forEach(function(u,i){ insertImageEl(u.url, i); });
          msg.textContent=(added.length||files.length)+'枚 画像を追加しました。ドラッグで置く（💾保存で確定）';
        }).catch(function(){ msg.textContent='通信エラー'; });
      });
    }).catch(function(){msg.textContent='画像一覧の取得に失敗';});
  }
  var addImgBtn=document.getElementById('__ce_addimg');
  if(addImgBtn) addImgBtn.addEventListener('click', openAddImagePicker);
  // 🧹 全体を規則化：セクション余白を一律・主要見出しのサイズと行間を一律にそろえる（AIなし・一貫性UP）。
  // AI一括改善が「シンプルすぎ」になりがちな一貫性(余白/見出し)を、機械的に確実にそろえる用。
  var normBtn=document.getElementById('__ce_normalize');
  if(normBtn) normBtn.addEventListener('click',function(){
    if(!confirm('全体を一律にそろえます（AIなし・即反映）：\\n・セクションの上下余白 100px\\n・セクションの左右余白 → 多数派の値にそろえる（明らかに違うものはそのまま＝意図的な余白として残す）\\n・主要見出し(H1/H2/H3)のサイズ・太さ・行間\\n・本文の行間を1.8に（読みやすく＝呼吸感）\\n・影(box-shadow)を1種類に統一\\n\\n気に入らなければ「⟲ 戻す」で戻せます。実行しますか？')) return;
    function _skip(el){ return el.closest && (el.closest('#__ce')||el.closest('#__ce_cm')||el.closest('#__ce_pk')); }
    // カード等の小見出し・小さい文字は対象外（そこまで大きく/広くすると崩れるため）
    function _inCard(el){ var n=el; while(n && n!==document.body){ var c=(n.className&&n.className.toString())||''; if(/card|item|bubble|benefit|badge|chip|tag|nav|menu|footer|col/i.test(c)) return true; n=n.parentElement; } return false; }
    var secN=0, hN=0, pN=0;
    var secEls=[], lrVals=[];
    [].forEach.call(document.querySelectorAll('section'),function(s){
      if(_skip(s)) return;
      s.style.setProperty('padding-top','100px','important');
      s.style.setProperty('padding-bottom','100px','important');
      s.style.setProperty('margin-top','0','important');
      s.style.setProperty('margin-bottom','0','important');
      secEls.push(s);
      try{ lrVals.push(parseFloat(getComputedStyle(s).paddingLeft)||0); }catch(_){ lrVals.push(0); }
      secN++;
    });
    // 左右の余白：フルブリード(0px)のヒーロー等が混ざるとバラバラに見えるので「多数派の値」にそろえる。
    // ただし現在値が多数派とかけ離れているセクションは、意図した余白とみなして触らずに残す（別扱い）。
    var lrN=0, lrSkip=0;
    if(lrVals.length){
      var sorted=lrVals.slice().sort(function(a,b){return a-b;});
      var target=Math.max(24, Math.round(sorted[Math.floor(sorted.length/2)]));   // 中央値（最低24px）
      var tol=Math.max(16, target*0.35);   // これ以上離れていたら「明らかに余白が違う」として除外
      secEls.forEach(function(s,i){
        if(Math.abs(lrVals[i]-target)<=tol){
          s.style.setProperty('padding-left',target+'px','important');
          s.style.setProperty('padding-right',target+'px','important');
          lrN++;
        } else { lrSkip++; }
      });
    }
    // 見出し：サイズ＋太さも一律（H1=太字700／H2・H3=セミボールド600）
    [['h1',64,700],['h2',40,600],['h3',26,600]].forEach(function(t){
      [].forEach.call(document.querySelectorAll(t[0]),function(h){
        if(_skip(h)||_inCard(h)) return;
        h.style.setProperty('font-size',t[1]+'px','important');
        h.style.setProperty('font-weight',t[2],'important');
        h.style.setProperty('line-height','1.4','important');
        h.style.setProperty('margin-bottom','0.6em','important');
        hN++;
      });
    });
    // 本文(p)の行間を1.8に＝Apple/Vercel的な"呼吸感"。カード内の小さい注記等は除外。
    [].forEach.call(document.querySelectorAll('p'),function(pp){
      if(_skip(pp)||_inCard(pp)) return;
      var fs=16; try{ fs=parseFloat(getComputedStyle(pp).fontSize)||16; }catch(_){}
      if(fs<12) return;  // ごく小さい注記は触らない
      pp.style.setProperty('line-height','1.8','important');
      pN++;
    });
    // 影を1種類に統一＝バラバラなshadowでデザインシステムが崩れる問題を解消。
    var STD_SHADOW='0 4px 16px rgba(0,0,0,.08)', shN=0;
    [].forEach.call(document.querySelectorAll('body *'),function(el){
      if(el.id && el.id.indexOf('__ce')===0) return;
      if(el.closest && (el.closest('#__ce')||el.closest('#__ce_cm')||el.closest('#__ce_pk'))) return;
      var bs=''; try{ bs=getComputedStyle(el).boxShadow; }catch(_){ return; }
      if(bs && bs!=='none'){ el.style.setProperty('box-shadow',STD_SHADOW,'important'); shN++; }
    });
    markDirty();
    msg.textContent='規則化：上下余白 '+secN+'／左右余白 '+lrN+'件そろえ・'+lrSkip+'件は別扱い／見出し '+hN+'／本文行間 '+pN+'／影 '+shN+' 箇所をそろえました（💾保存で確定・⟲で戻せる）';
  });
  // 🎨 全ボタンをテーマ色に統一：CTAの色バラつき（ブランド感の弱さ）をAIなしで一発解消。
  // 主要ボタン＝テーマ色で塗り／secondary系＝同色の枠線、に統一する。
  var btnColBtn=document.getElementById('__ce_btncolor');
  if(btnColBtn) btnColBtn.addEventListener('click',function(){
    // テーマ色：まず --accent、無ければ一番よく使われているボタン背景色を採用
    var accent=(getComputedStyle(document.documentElement).getPropertyValue('--accent')||'').trim();
    if(!accent){
      var freq={};
      [].forEach.call(document.querySelectorAll('a.btn,button,.btn,.cta,.button'),function(el){ var bg=getComputedStyle(el).backgroundColor||''; if(bg && !/rgba?\\(0, 0, 0, 0\\)|transparent/.test(bg)){ freq[bg]=(freq[bg]||0)+1; } });
      var best=null,bn=0; Object.keys(freq).forEach(function(k){ if(freq[k]>bn){bn=freq[k];best=k;} });
      accent=best||'#1a7f37';
    }
    var els=document.querySelectorAll('a.btn,a.button,a[class*="btn"],a[class*="cta"],button,.btn,.cta,.button');
    var n=0, seen=[];
    [].forEach.call(els,function(el){
      if(el.id && el.id.indexOf('__ce')===0) return;
      if(el.closest && (el.closest('#__ce')||el.closest('#__ce_cm')||el.closest('#__ce_pk'))) return;
      if(seen.indexOf(el)>-1) return; seen.push(el);
      var c=(el.className&&el.className.toString())||'';
      if(/secondary|outline|ghost|line|sub/i.test(c)){   // サブボタン＝同色の枠線に
        el.style.setProperty('background','transparent','important');
        el.style.setProperty('background-image','none','important');
        el.style.setProperty('color',accent,'important');
        el.style.setProperty('-webkit-text-fill-color',accent,'important');
        el.style.setProperty('border','2px solid '+accent,'important');
      } else {                                            // 主要ボタン＝テーマ色で塗り
        el.style.setProperty('background',accent,'important');
        el.style.setProperty('background-image','none','important');
        el.style.setProperty('color','#fff','important');
        el.style.setProperty('-webkit-text-fill-color','#fff','important');
        el.style.setProperty('border','none','important');
      }
      n++;
    });
    markDirty();
    msg.textContent = n ? ('ボタンを '+n+' 個テーマ色に統一しました（主要=塗り／sub=枠線・💾保存で確定・⟲で戻せる）') : 'ボタンが見つかりませんでした（.btn等のクラスが無いのかも）';
  });
  // 🎨 ベース色（テーマ色）をAIなしで一括変更：生成HTMLの :root にある色変数(--accent等)を上書きする。
  // <html>のインラインstyleに当てるので :root より優先され、var(--accent)を使う全要素が即変わる＆保存で残る。
  (function(){
    var baseInp=document.getElementById('__ce_base'), baseVar=null, baseOrig=null;
    function _hexOf(c){ c=(c||'').trim(); if(c.charAt(0)==='#'){ return c.length===4?('#'+c[1]+c[1]+c[2]+c[2]+c[3]+c[3]):c.slice(0,7); } var m=c.match(/\\d+/g); if(!m||m.length<3) return '#000000'; return '#'+m.slice(0,3).map(function(x){return ('0'+(+x).toString(16)).slice(-2);}).join(''); }
    function _lighten(hex, amt){ var h=_hexOf(hex).slice(1); var r=parseInt(h.slice(0,2),16),g=parseInt(h.slice(2,4),16),b=parseInt(h.slice(4,6),16); r=Math.round(r+(255-r)*amt); g=Math.round(g+(255-g)*amt); b=Math.round(b+(255-b)*amt); return '#'+[r,g,b].map(function(x){return ('0'+x.toString(16)).slice(-2);}).join(''); }
    var NAMES=['--accent','--brand','--primary','--main','--theme','--key','--color-primary','--color-accent','--accent-color','--primary-color'];
    var SOFT=['-soft','-light','-lighter','-bg','-pale'];
    var cs=getComputedStyle(document.documentElement);
    for(var i=0;i<NAMES.length;i++){ var v=(cs.getPropertyValue(NAMES[i])||'').trim(); if(v && /^#|rgb/.test(v)){ baseVar=NAMES[i]; baseOrig=v; break; } }
    if(!baseVar){ if(baseInp) baseInp.disabled=true; var bm=document.getElementById('__ce_basemsg'); if(bm) bm.textContent='この版はテーマ色の変数が無いので、色は右クリックの「文字の色」かAIで変えてください'; return; }
    if(baseInp){ try{ baseInp.value=_hexOf(baseOrig); }catch(_){} }
    // 色変換ユーティリティ（hex⇔rgb⇔hsl）。テーマ色系を「色相ごと回転」して関連色まとめて塗り替える。
    function _h2r(h){ h=h.replace('#',''); if(h.length===3) h=h[0]+h[0]+h[1]+h[1]+h[2]+h[2]; if(h.length===8) h=h.slice(0,6); return [parseInt(h.slice(0,2),16),parseInt(h.slice(2,4),16),parseInt(h.slice(4,6),16)]; }
    function _r2hsl(r,g,b){ r/=255;g/=255;b/=255; var mx=Math.max(r,g,b),mn=Math.min(r,g,b),h,s,l=(mx+mn)/2; if(mx===mn){h=s=0;} else { var d=mx-mn; s=l>0.5?d/(2-mx-mn):d/(mx+mn); if(mx===r)h=(g-b)/d+(g<b?6:0); else if(mx===g)h=(b-r)/d+2; else h=(r-g)/d+4; h*=60; } return [h,s,l]; }
    function _hsl2r(h,s,l){ h=(((h%360)+360)%360)/360; var r,g,b; if(s===0){r=g=b=l;} else { function q(p,q2,t){ if(t<0)t+=1; if(t>1)t-=1; if(t<1/6)return p+(q2-p)*6*t; if(t<1/2)return q2; if(t<2/3)return p+(q2-p)*(2/3-t)*6; return p; } var qq=l<0.5?l*(1+s):l+s-l*s, pp=2*l-qq; r=q(pp,qq,h+1/3); g=q(pp,qq,h); b=q(pp,qq,h-1/3); } return [Math.round(r*255),Math.round(g*255),Math.round(b*255)]; }
    function _toHex(r,g,b){ return '#'+[r,g,b].map(function(x){return ('0'+Math.max(0,Math.min(255,x|0)).toString(16)).slice(-2);}).join(''); }
    function _parse(tok){ tok=tok.trim(); if(tok.charAt(0)==='#'){ var rr=_h2r(tok); return {r:rr[0],g:rr[1],b:rr[2],a:null,fmt:'hex'}; } var m=tok.match(/rgba?\\(([^)]*)\\)/i); if(!m) return null; var p=m[1].split(',').map(function(x){return x.trim();}); if(p.length<3) return null; return {r:parseFloat(p[0]),g:parseFloat(p[1]),b:parseFloat(p[2]),a:(p.length>3?p[3]:null),fmt:(p.length>3?'rgba':'rgb')}; }
    function _fmtC(r,g,b,a,fmt){ if(fmt==='hex') return _toHex(r,g,b); if(fmt==='rgba') return 'rgba('+r+', '+g+', '+b+', '+a+')'; return 'rgb('+r+', '+g+', '+b+')'; }
    // 元のCSS/inlineを丸ごとキャッシュ（毎回ここから塗り直す＝リアルタイム＆連続変更OK）。編集UIは除く。
    var _sCache=[], _iCache=[];
    [].forEach.call(document.querySelectorAll('style'), function(st){ if(/#__ce|__ce_/.test(st.textContent)) return; _sCache.push({el:st, orig:st.textContent}); });
    [].forEach.call(document.querySelectorAll('[style]'), function(el){ if(el.id && el.id.indexOf('__ce')===0) return; if(el.closest && (el.closest('#__ce')||el.closest('#__ce_cm')||el.closest('#__ce_pk'))) return; _iCache.push({el:el, orig:el.getAttribute('style')}); });
    // SVGの色は fill= / stop-color= などの「属性」で塗られている＝styleとは別。これも拾って塗り替える。
    var _aCache=[]; ['fill','stroke','stop-color','flood-color','lighting-color','color'].forEach(function(attr){
      [].forEach.call(document.querySelectorAll('['+attr+']'), function(el){ if(el.closest && (el.closest('#__ce')||el.closest('#__ce_cm')||el.closest('#__ce_pk'))) return; var v=el.getAttribute(attr); if(v && /#[0-9a-fA-F]{3,8}|rgba?\\(/.test(v)) _aCache.push({el:el, attr:attr, orig:v}); });
    });
    // 元テキストから色トークンを頻度付きで集める（無彩色=グレー/黒/白は除外＝文字色を守る）
    var _allTok=(function(){
      var text=_sCache.map(function(s){return s.orig;}).join('\\n')+'\\n'+_iCache.map(function(s){return s.orig;}).join('\\n')+'\\n'+_aCache.map(function(s){return s.orig;}).join('\\n');
      var freq={}; (text.match(/#[0-9a-fA-F]{3,8}|rgba?\\([^)]*\\)/gi)||[]).forEach(function(t){ freq[t]=(freq[t]||0)+1; });
      var out=[]; Object.keys(freq).forEach(function(tok){ var c=_parse(tok); if(!c) return; var hsl=_r2hsl(c.r,c.g,c.b); if(hsl[1]<0.12||hsl[2]<0.06||hsl[2]>0.96) return; out.push({tok:tok,c:c,hsl:hsl,n:freq[tok]}); });
      return out;
    })();
    // ★基準色は「今のaccent変数の値」ではなく、CSSで一番多く使われている有彩色から決める。
    //   （前回の色替えでaccentが別色に焼き込まれても、本来のテーマ色を正しく掴めるように）
    var refHue=(function(){ if(!_allTok.length) return null; var bins={}; _allTok.forEach(function(t){ var b=Math.round(t.hsl[0]/12); bins[b]=(bins[b]||0)+t.n; }); var best=0,bn=-1; Object.keys(bins).forEach(function(k){ if(bins[k]>bn){bn=bins[k];best=+k;} }); return best*12; })();
    var _family = refHue==null ? [] : _allTok.filter(function(t){ return Math.abs(((t.hsl[0]-refHue+540)%360)-180)<=45; });
    // 代表色（彩度高め×出現多め）を基準HSLに＝色相の回転量と「本体色」判定に使う
    var _ref = _family.slice().sort(function(a,b){ return (b.hsl[1]*Math.log(1+b.n))-(a.hsl[1]*Math.log(1+a.n)); })[0];
    var accHsl = _ref ? _ref.hsl : (refHue!=null?[refHue,0.6,0.5]:_r2hsl.apply(null,_h2r(_hexOf(baseOrig))));
    if(baseInp && _ref){ try{ baseInp.value=_toHex(_ref.c.r,_ref.c.g,_ref.c.b); }catch(_){} }
    // 新しいベース色に合わせて、テーマ色系を色相回転して全置換（毎回キャッシュから塗り直す＝realtime）
    function applyBase(newHex){
      var nH=_r2hsl.apply(null,_h2r(_hexOf(newHex))), delta=nH[0]-accHsl[0];
      var map=_family.map(function(f){
        var out, main=Math.abs(((f.hsl[0]-accHsl[0]+540)%360)-180)<10 && Math.abs(f.hsl[2]-accHsl[2])<0.14;
        if(main){ var m=_h2r(_hexOf(newHex)); out=_fmtC(m[0],m[1],m[2],f.c.a,f.c.fmt); }         // 本体色はピッタリ新色に
        else { var s2=Math.min(1,f.hsl[1]*(nH[1]/(accHsl[1]||1))), rr=_hsl2r(f.hsl[0]+delta,s2,f.hsl[2]); out=_fmtC(rr[0],rr[1],rr[2],f.c.a,f.c.fmt); }  // 派生色は色相だけ回す
        return [f.tok, out, f.tok.toUpperCase(), f.tok.toLowerCase()];
      });
      function rep(s){ for(var i=0;i<map.length;i++){ s=s.split(map[i][0]).join(map[i][1]).split(map[i][2]).join(map[i][1]).split(map[i][3]).join(map[i][1]); } return s; }
      _sCache.forEach(function(sc){ var n=rep(sc.orig); if(sc.el.textContent!==n) sc.el.textContent=n; });
      _iCache.forEach(function(ic){ var n=rep(ic.orig); if(ic.el.getAttribute('style')!==n) ic.el.setAttribute('style',n); });
      _aCache.forEach(function(ac){ var n=rep(ac.orig); if(ac.el.getAttribute(ac.attr)!==n) ac.el.setAttribute(ac.attr,n); });  // SVGの色属性
      document.documentElement.style.setProperty(baseVar,_hexOf(newHex));
      SOFT.forEach(function(sfx){ var nm=baseVar+sfx; if((cs.getPropertyValue(nm)||'').trim()) document.documentElement.style.setProperty(nm,_lighten(_hexOf(newHex),0.86)); });
      markDirty();
    }
    if(baseInp) baseInp.addEventListener('input',function(){ applyBase(this.value); });  // ドラッグ中もリアルタイムで塗り替え
    var baserBtn=document.getElementById('__ce_baser');
    if(baserBtn) baserBtn.addEventListener('click',function(){
      _sCache.forEach(function(sc){ if(sc.el.textContent!==sc.orig) sc.el.textContent=sc.orig; });
      _iCache.forEach(function(ic){ if(ic.el.getAttribute('style')!==ic.orig) ic.el.setAttribute('style',ic.orig); });
      _aCache.forEach(function(ac){ if(ac.el.getAttribute(ac.attr)!==ac.orig) ac.el.setAttribute(ac.attr,ac.orig); });  // SVG色属性も元に戻す
      document.documentElement.style.removeProperty(baseVar);
      SOFT.forEach(function(sfx){ document.documentElement.style.removeProperty(baseVar+sfx); });
      if(baseInp){ try{ baseInp.value=_hexOf(baseOrig); }catch(_){} }
      markDirty(); msg.textContent='ベース色を元に戻しました';
    });
    var _bm=document.getElementById('__ce_basemsg');
    if(_bm) _bm.textContent=_family.length? ('テーマ色系 '+_family.length+' 色をまとめて塗り替えます（色を選ぶと即反映）') : 'テーマ色が見つからないので、色は右クリックの「文字の色」かAIで';
  })();
  // 🎨 手本サイトのドロップダウンをストックから埋める（改善の方向性を画像で見せる用）
  var refSel=document.getElementById('__ce_ref');
  if(refSel){
    fetch('/api/sites').then(function(r){return r.json();}).then(function(d){
      (d.hits||[]).forEach(function(h){
        var o=document.createElement('option');
        o.value=h.site_id;
        var u=h.url.replace(/^https?:\\/\\//,'').replace(/\\/$/,'');
        o.textContent=u.length>42?u.slice(0,42)+'…':u;
        refSel.appendChild(o);
      });
    }).catch(function(){});
  }
  // 🚀 一括改善（Before→After営業デモ）：全セクションを順に今風へ。完了したらAfter版を開く
  var impBtn=document.getElementById('__ce_improve');
  if(impBtn){
    impBtn.addEventListener('click',function(){
      var v=prompt('どのセクションを改善しますか？\\n空欄 or 0 = 全部（目安20〜50円・数分）\\n数字1つ（例 3）= 最初の3セクションだけ\\nカンマ区切り（例 2,5）= その番号のセクションだけ。1つだけなら「5,」\\n※Claude/GPT選択時はスクショ付きで渡します（見た目判断が正確）','');
      if(v===null) return;  // キャンセル
      v=(v||'').trim();
      var lim=0, targets=null;
      if(v.indexOf(',')>-1){
        targets=v.split(',').map(function(x){return parseInt(x.trim(),10);})
                 .filter(function(n){return n>=1;}).map(function(n){return n-1;});
        if(!targets.length){ msg.textContent='セクション番号が読めませんでした'; return; }
      } else { lim=parseInt(v,10)||0; }
      var hint=prompt('デザインの方向性があれば一言で（空欄OK）\\n例：高級ホテルのように上品に／ポップで元気に／黒基調でスタイリッシュに／アニメ多めで動きのあるページに','');
      if(hint===null) return;  // キャンセル
      impBtn.disabled=true; impBtn.textContent='🚀 今の状態を保存中…';
      flushThen(function(){ impBtn.textContent='🚀 一括改善中…';
      fetch('/api/improve_camp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,limit:lim,sections:targets,hint:(hint||'').trim(),ref_id:(refSel?refSel.value:'')})})
        .then(function(r){return r.json();})
        .then(function(d){
          if(!d.ok){ msg.textContent=d.message||'開始できませんでした'; impBtn.disabled=false; impBtn.textContent='🚀 ページ全体を今風に（一括改善）'; return; }
          var poll=function(){
            fetch('/api/improve_camp/status').then(function(r){return r.json();}).then(function(s){
              if(s.file){ var t='🚀 '+(s.phase||'改善中…'); msg.textContent=t; impBtn.textContent=t; setTimeout(poll,2000); return; }
              impBtn.disabled=false; impBtn.textContent='🚀 ページ全体を今風に（一括改善）';
              if(s.error){ msg.textContent='一括改善に失敗: '+s.error; return; }
              if(s.result&&s.result.file){ msg.textContent='完了！After版を開きます'; location.href='/camp/'+encodeURIComponent(s.result.file); }
            }).catch(function(){ setTimeout(poll,3000); });
          };
          setTimeout(poll,2000);
        }).catch(function(){ msg.textContent='通信に失敗しました'; impBtn.disabled=false; impBtn.textContent='🚀 ページ全体を今風に（一括改善）'; });
      });
    });
  }
  // ヘッダを掴んでウィンドウ自体を移動できる（クリックでの開閉と両立：動いた時だけトグルを抑制）
  var hd=document.getElementById('__ce_hd'), hDrag=false, hMoved=false, hSX=0,hSY=0,hL=0,hT=0;
  hd.addEventListener('mousedown',function(e){
    if(e.target.closest('#__ce_save')||e.target.closest('#__ce_undo')||e.target.closest('#__ce_homeh')) return;  // 保存・戻す・ホームボタンは除外
    var r=box.getBoundingClientRect();
    hDrag=true; hMoved=false; hSX=e.clientX; hSY=e.clientY; hL=r.left; hT=r.top;
    box.style.right='auto'; box.style.bottom='auto'; box.style.left=hL+'px'; box.style.top=hT+'px';
    e.preventDefault();
  });
  document.addEventListener('mousemove',function(e){
    if(!hDrag) return;
    var dx=e.clientX-hSX, dy=e.clientY-hSY;
    if(Math.abs(dx)+Math.abs(dy)>3) hMoved=true;
    box.style.left=Math.max(0,Math.min(hL+dx, window.innerWidth-60))+'px';
    box.style.top=Math.max(0,Math.min(hT+dy, window.innerHeight-40))+'px';
  },true);
  document.addEventListener('mouseup',function(){ hDrag=false; },true);
  hd.addEventListener('click',function(){
    if(hMoved){ hMoved=false; return; }  // ドラッグ直後はトグルしない
    box.classList.toggle('min'); document.getElementById('__ce_mn').textContent=box.classList.contains('min')?'▲ ひらく':'✕ 閉じる';
  });
  // ヘッダの保存ボタン＝この版を上書き保存（動き・位置・画像差し替えを現ファイルに焼き込む）
  var saveBtn=document.getElementById('__ce_save');
  saveBtn.addEventListener('click',function(ev){
    ev.stopPropagation();  // ヘッダのトグル(開閉)と競合させない
    saveLayout();
  });
  var undoBtn=document.getElementById('__ce_undo');
  if(undoBtn) undoBtn.addEventListener('click',function(ev){ ev.stopPropagation(); undoStep(); });
  // ◎○△✖評価＝手本ごとのハズレ率集計の教師データ。
  // 評価すると選んだマークだけ残して畳む（邪魔にしない）。そのマークを押すと開いて変更・解除できる
  var rateBox=document.getElementById('__ce_rate');
  function paintRate(v){
    [].forEach.call(rateBox.querySelectorAll('.rt'),function(b){ b.classList.toggle('on', b.dataset.r===v); });
    rateBox.classList.toggle('done', !!v);
  }
  if(rateBox){
    fetch('/api/camp_rate?file='+encodeURIComponent(FILE)).then(function(r){return r.json();})
      .then(function(d){ paintRate(d.rating||''); }).catch(function(){});
    rateBox.addEventListener('click',function(ev){
      var b=ev.target.closest('.rt'); if(!b) return;
      ev.stopPropagation();  // ヘッダの開閉トグルに食われない
      // 畳まれた状態でマークを押したら＝開くだけ（変更・解除のため）
      if(rateBox.classList.contains('done')){ rateBox.classList.remove('done'); return; }
      fetch('/api/camp_rate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,rating:b.dataset.r})})
      .then(function(r){return r.json();}).then(function(d){
        if(d.ok){ paintRate(d.rating); setToast(d.rating?('評価を保存: '+d.rating):'評価を解除しました'); setTimeout(hideToast,1200); }
      }).catch(function(){});
    });
  }
  var esc=function(s){return String(s||'').replace(/[&<>"]/g,function(c){return({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]);});};
  // ①のドロップダウン(sec)で選ばれたセクション要素を返す（ページ全体=-1ならnull）
  function curSecEl(){
    var idx=Number(sec.value);
    if(!(idx>=0)) return null;
    var els=[].slice.call(document.querySelectorAll('section')).filter(function(x){return !x.closest('#__ce');});
    return els[idx]||null;
  }
  // 親カンプのhead内<style>を集める（プレビューで見た目を近づける用）
  function headCss(){
    return [].slice.call(document.head.querySelectorAll('style')).map(function(s){return s.textContent||'';}).join('\\n');
  }
  // head内のCSSから使われている色/フォント等の変数名(--xxx)を集め、実際の値を返す
  function collectRootVars(){
    var names={}, re=/(--[\\w-]+)\\s*:/g, m;
    [].slice.call(document.head.querySelectorAll('style')).forEach(function(s){
      var t=s.textContent||''; while((m=re.exec(t))){ names[m[1]]=1; }
    });
    var cs=getComputedStyle(document.documentElement), out={};
    Object.keys(names).forEach(function(n){ var v=cs.getPropertyValue(n); if(v&&v.trim()) out[n]=v.trim(); });
    return out;
  }
  // 保存前にセクションを"素の状態"へ掃除＝編集で焼き込んだ位置・サイズ・一時クラスを外す。
  // さらに色/フォント変数をセクション自身に埋め込み、貼り先のページに依存せず表示できるようにする。
  function cleanSection(el){
    var c=el.cloneNode(true);
    var stripCls=['__ce_sel','__ce_hl','__ce_sechl','fxa_in'];
    [].slice.call(c.querySelectorAll('*')).concat([c]).forEach(function(n){
      if(n.classList){
        stripCls.forEach(function(k){ n.classList.remove(k); });
        [].slice.call(n.classList).forEach(function(cl){ if(cl.indexOf('__ceax_')===0) n.classList.remove(cl); });
      }
      var edited=false;
      if(n.attributes){ [].slice.call(n.attributes).forEach(function(a){ if(a.name.indexOf('data-ce')===0){ edited=true; n.removeAttribute(a.name); } }); }
      if(n.style){
        ['animation','transition'].forEach(function(p){ n.style.removeProperty(p); });      // 一時アニメは常に除去
        if(edited){ ['transform','transform-origin','width','height','max-width','object-fit','opacity','filter'].forEach(function(p){ n.style.removeProperty(p); }); }  // 編集で動かした要素だけサイズ・位置も戻す
        var sv=n.getAttribute('style'); if(!sv||!sv.trim()) n.removeAttribute('style');
      }
    });
    var vars=collectRootVars();
    Object.keys(vars).forEach(function(k){ c.style.setProperty(k, vars[k]); });  // 色・フォントを自己完結させる
    return c;
  }
  // ①で選んだセクションを画面で薄く光らせる＋そこへスクロール（どこが対象か一目で分かる）
  function highlightSelSec(){
    [].slice.call(document.querySelectorAll('.__ce_sechl')).forEach(function(x){x.classList.remove('__ce_sechl');});
    var t=curSecEl();
    if(t){ t.classList.add('__ce_sechl'); try{ t.scrollIntoView({behavior:'smooth',block:'start'}); }catch(_){} }
  }
  sec.addEventListener('change', highlightSelSec);
  // ⭐ このセクションをお気に入り（自己完結HTMLを部品として保存＝AIなし）
  // 🎬 オープニング演出（プリローダー）：開いた瞬間に全画面の幕→ロゴ/文字→フェードで本体へ。
  // 幕(#__op_screen)と再生JS(#__op_run)は「中身」として焼き込む＝保存すれば単体でも動く。
  // 幕の中のロゴ・文字は普通のimg/spanなので、右クリックで差し替え・編集できる。
  var PH_LOGO="data:image/svg+xml;utf8,"+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="0 0 120 120"><circle cx="60" cy="60" r="54" fill="#e6f0fe" stroke="#0b6bcb" stroke-width="4"/><text x="60" y="76" font-size="48" text-anchor="middle" fill="#0b6bcb" font-family="sans-serif">◎</text></svg>');
  function _opTitle(){ return (document.title||'').trim() || (document.querySelector('h1')?(document.querySelector('h1').textContent||'').trim():'') || 'Your Site'; }
  function _opLogoSrc(){
    var im=document.querySelector('header img,.header img,.logo img,[class*="logo"] img,[id*="logo"] img,[id*="header"] img');
    if(im && (im.currentSrc||im.src)) return im.currentSrc||im.src;
    return PH_LOGO;
  }
  function addOpening(){
    var old=document.getElementById('__op_screen'); if(old) old.remove();
    var oldjs=document.getElementById('__op_run'); if(oldjs) oldjs.remove();
    var sc=document.createElement('div'); sc.id='__op_screen';
    sc.setAttribute('style','position:fixed;inset:0;z-index:2147480000;display:flex;align-items:center;justify-content:center;background:radial-gradient(60% 60% at 35% 40%,#eafff6 0%,#eef4ff 55%,#ffffff 100%)');
    sc.setAttribute('data-paused','1'); /* 付けた直後は編集できるよう止めておく */
    var inner=document.createElement('div');
    inner.setAttribute('style','display:flex;align-items:center;gap:20px');
    inner.innerHTML='<img id="__op_logo" src="'+_opLogoSrc()+'" alt="ロゴ" style="height:72px;width:auto">'
      +'<span id="__op_title" style="font-size:44px;font-weight:800;letter-spacing:.02em;color:#2b3a4a;font-family:system-ui,sans-serif">'+esc(_opTitle())+'</span>';
    sc.appendChild(inner);
    document.body.appendChild(sc);
    // 焼き込み用の再生スクリプト（保存版・単体表示で動く。編集バーがある時は自動退場だけさせて本体を触れるように）
    var js=document.createElement('script'); js.id='__op_run';
    js.textContent="(function(){var s=document.getElementById('__op_screen');if(!s)return;if(s.getAttribute('data-paused')==='1')return;s.style.transition='opacity .6s ease';s.style.opacity='0';requestAnimationFrame(function(){requestAnimationFrame(function(){s.style.opacity='1';});});setTimeout(function(){if(s.getAttribute('data-paused')==='1')return;s.style.opacity='0';setTimeout(function(){s.style.display='none';},650);},1800);})();";
    document.body.appendChild(js);
    markDirty();
    msg.textContent='オープニングを付けました。ロゴ/文字を右クリックで差し替え→「💾 保存」で確定。本体を触るときは「👁 出す／隠す」で一旦隠せます';
  }
  // 幕の表示/非表示を切り替え（編集用）。出す時は data-paused=1 で止めて右クリック編集できるように。
  function toggleOpening(){
    var s=document.getElementById('__op_screen');
    if(!s){ msg.textContent='先に「🎬 フェードのオープニングを付ける」を押してください'; return; }
    var hidden=(s.style.display==='none'||getComputedStyle(s).display==='none'||parseFloat(getComputedStyle(s).opacity)===0);
    if(hidden){ s.setAttribute('data-paused','1'); s.style.display='flex'; s.style.opacity='1'; msg.textContent='オープニングを表示中（ロゴ/文字を右クリックで差し替え）。もう一度押すと隠せます'; }
    else { s.style.display='none'; msg.textContent='オープニングを隠しました（保存版では開いた時に自動で流れます）'; }
  }
  var opAddBtn=document.getElementById('__ce_op_add');
  if(opAddBtn) opAddBtn.addEventListener('click',addOpening);
  var opEditBtn=document.getElementById('__ce_op_edit');
  if(opEditBtn) opEditBtn.addEventListener('click',toggleOpening);
  var favBtn=document.getElementById('__ce_fav');
  if(favBtn) favBtn.addEventListener('click',function(){
    var el=curSecEl();
    if(!el){ msg.textContent='まず①「どこを直す？」で保存したいセクションを選んでください（ページ全体は不可）'; return; }
    var label=((sec.options[sec.selectedIndex]||{}).text||'セクション').replace(/^[0-9]+\\.\\s*/,'');
    var name=window.prompt('このセクションを「部品」として保存します。別のカンプの同じ枠にAIなしで入れ替えできます。\\n名前をどうぞ：', label);
    if(name===null) return;
    favBtn.disabled=true; var old=favBtn.textContent; favBtn.textContent='保存中…';
    fetch('/api/section_fav/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({html:cleanSection(el).outerHTML,headcss:headCss(),name:name})})
    .then(function(r){return r.json();}).then(function(d){
      favBtn.disabled=false; favBtn.textContent=old;
      msg.textContent=d.ok?('⭐保存しました「'+((d.fav&&d.fav.name)||'')+'」。🔀から他のカンプでも使えます'):('保存失敗：'+(d.message||''));
    }).catch(function(){ favBtn.disabled=false; favBtn.textContent=old; msg.textContent='通信エラー'; });
  });
  // 🎨 おしゃれ度チェック：現DOMを保存→AIが有名サイト基準で採点＋改善点。結果はモーダル表示。
  function _scBar(label, n){
    var pct=Math.max(0,Math.min(100,(+n||0)*10)), col=n>=8?'#1a7f37':(n>=6?'#b8860b':'#c0392b');
    return '<div style="display:flex;align-items:center;gap:8px;margin:4px 0"><span style="width:92px;font-size:12px;color:#555">'+label+'</span><span style="flex:1;height:8px;background:#eee;border-radius:5px;overflow:hidden"><span style="display:block;height:100%;width:'+pct+'%;background:'+col+'"></span></span><b style="width:34px;text-align:right;font-size:12.5px;color:'+col+'">'+(n!=null?n:'-')+'</b></div>';
  }
  function showStyleResult(d){
    var ov=document.createElement('div'); ov.id='__ce_pk';
    var sc=d.scores||{};
    // fixesが配列でない形（AIがオブジェクトや文字列で返す）でも落ちないよう正規化
    var _fx=Array.isArray(d.fixes)?d.fixes:(d.fixes&&typeof d.fixes==='object'?Object.keys(d.fixes).map(function(k){return d.fixes[k];}):[]);
    var fixes=_fx.map(function(f,i){ if(f&&typeof f!=='object') f={title:''+f}; f=f||{}; return '<div style="border:1px solid #eee;border-radius:8px;padding:9px 11px;margin-bottom:7px"><b style="font-size:13px;color:#1d1d1f">'+(i+1)+'. '+esc(f.title||f.fix||f.name||'')+'</b>'+(f.why?'<div style="font-size:12px;color:#888;margin-top:3px">👀 '+esc(f.why)+'</div>':'')+(f.how?'<div style="font-size:12.5px;color:#1d1d1f;margin-top:3px">🎯 '+esc(f.how)+'</div>':'')+'</div>'; }).join('');
    var ov100=d.overall!=null?d.overall:'-';
    ov.innerHTML='<div class="bx" style="max-width:560px"><span class="cl" id="__ce_pkx">×</span>'
      +'<h4>🎨 おしゃれ度チェック（有名サイト基準）</h4>'
      +'<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:8px"><span style="font-size:34px;font-weight:800;color:#c026a6">'+ov100+'</span><span style="font-size:14px;color:#888">/ 10（10=有名サイト級）</span></div>'
      +(d.summary?'<div style="font-size:12.5px;color:#444;background:#faf5fb;border:1px solid #f0dcf0;border-radius:8px;padding:9px 11px;margin-bottom:10px">'+esc(d.summary)+'</div>':'')
      +_scBar('余白',sc.whitespace)+_scBar('字組み',sc.typography)+_scBar('配色',sc.color)+_scBar('視覚的階層',sc.hierarchy)+_scBar('一貫性',sc.consistency)+_scBar('画像/あしらい',sc.imagery)
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:12px 0 6px">🔧 効果の大きい改善点</div>'+fixes
      +'<div style="font-size:11px;color:#aaa;margin-top:8px">採点：'+esc(d.model||'')+'</div></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click',function(e){ if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx') ov.remove(); });
  }
  var scBtn=document.getElementById('__ce_stylecheck');
  if(scBtn) scBtn.addEventListener('click',function(){
    scBtn.disabled=true; var old=scBtn.textContent; scBtn.textContent='保存して採点中…（十数〜数十秒）';
    showToast('AIが有名サイト基準で採点中…（十数〜数十秒）');
    fetch('/api/save_camp_html',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,html:cleanHtml()})})
    .then(function(){ return fetch('/api/style_check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE})}); })
    .then(function(r){return r.json();}).then(function(d){
      hideToast(); scBtn.disabled=false; scBtn.textContent=old;
      if(!d.ok){ msg.textContent='採点できませんでした：'+(d.message||''); return; }
      try{ showStyleResult(d); msg.textContent='おしゃれ度：'+ (d.overall!=null?d.overall:'-') +'/10。改善点を表示しました'; }
      catch(e){ msg.textContent='採点は出ました（総合 '+(d.overall!=null?d.overall:'-')+'/10）が、表示でエラー：'+(e&&e.message||e); }
    }).catch(function(err){ hideToast(); scBtn.disabled=false; scBtn.textContent=old; msg.textContent='採点でエラー：'+(err&&err.message||'通信エラー'); });
  });
  // 🎯 チェックして自動で磨く：採点→AIの改善点を"指示"に変換→既存の一括改善に流す（1回）。
  var apBtn=document.getElementById('__ce_autopolish');
  if(apBtn) apBtn.addEventListener('click',function(){
    if(!confirm('AIが「採点 → 改善点 → 自動でページ全体を磨く」を1回実行します。\\n\\n・Vision採点＋ページ全体の改善でAIを使います（目安：数十〜100円）\\n・見た目判断なのでAIはClaude/GPT推奨（DeepSeekは崩れやすい）\\n・仕上がりが気に入らなければ元の版はそのまま残ります\\n\\n実行しますか？')) return;
    apBtn.disabled=true; var old=apBtn.textContent; apBtn.textContent='① 保存して採点中…';
    showToast('① AIが採点中…（十数秒）');
    fetch('/api/save_camp_html',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,html:cleanHtml()})})
    .then(function(){ return fetch('/api/style_check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE})}); })
    .then(function(r){return r.json();}).then(function(sc){
      if(!sc.ok){ throw new Error(sc.message||'採点に失敗'); }
      // AIの改善点をそのまま"指示"に変換して一括改善へ
      var hint='次の改善点をページ全体に反映し、見た目の質を有名サイト級に近づける（文言・画像・レイアウト構成は変えない）：\\n'
        + (sc.fixes||[]).map(function(f,i){ return (i+1)+'. '+(f.title||'')+'：'+(f.how||''); }).join('\\n');
      apBtn.textContent='② 改善点を反映中…（数分）';
      showToast('② 採点 '+(sc.overall!=null?sc.overall:'-')+'/10 → 改善点を自動で反映中…（数分）');
      return fetch('/api/improve_camp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,limit:0,sections:null,hint:hint,ref_id:(refSel?refSel.value:'')})});
    }).then(function(r){ return r.json(); }).then(function(d){
      if(!d.ok){ throw new Error(d.message||'改善を開始できませんでした'); }
      var poll=function(){
        fetch('/api/improve_camp/status').then(function(r){return r.json();}).then(function(s){
          if(s.file){ apBtn.textContent='② '+(s.phase||'改善中…'); setTimeout(poll,2000); return; }
          hideToast(); apBtn.disabled=false; apBtn.textContent=old;
          if(s.error){ msg.textContent='自動ブラッシュアップ失敗: '+s.error; return; }
          if(s.result&&s.result.file){ msg.textContent='磨き上がりました！After版を開きます（開いたら🎨で再採点してみてください）'; location.href='/camp/'+encodeURIComponent(s.result.file); }
        }).catch(function(){ setTimeout(poll,3000); });
      };
      setTimeout(poll,2000);
    }).catch(function(err){ hideToast(); apBtn.disabled=false; apBtn.textContent=old; msg.textContent='失敗：'+(err&&err.message||'通信エラー'); });
  });
  // 📦 納品用に分割エクスポート（保存済みファイルを HTML/CSS/JS＋images に分けてzip）。
  // まず現DOMを保存してから分割（今の編集を反映）。zipは自動ダウンロード。
  var exportBtn=document.getElementById('__ce_export');
  if(exportBtn) exportBtn.addEventListener('click',function(){
    exportBtn.disabled=true; var old=exportBtn.textContent; exportBtn.textContent='保存して書き出し中…';
    // 未保存の編集を先に焼き込む（分割は保存済みファイルを読むため）
    fetch('/api/save_camp_html',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,html:cleanHtml()})})
    .then(function(){ return fetch('/api/export_split',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE})}); })
    .then(function(r){return r.json();}).then(function(d){
      exportBtn.disabled=false; exportBtn.textContent=old;
      if(!d.ok){ msg.textContent='書き出し失敗：'+(d.message||''); return; }
      var miss=d.missing?('／画像 '+d.missing+'枚は取得できず（外部URL切れ等）'):'';
      msg.textContent='📦 書き出し完了：画像 '+d.images+'枚'+miss+'。zipをダウンロードします';
      var a=document.createElement('a'); a.href=d.download; a.download=''; document.body.appendChild(a); a.click(); a.remove();
    }).catch(function(){ exportBtn.disabled=false; exportBtn.textContent=old; msg.textContent='通信エラー'; });
  });
  // 🔀 お気に入りからセクションを切り替え（プレビューから選ぶ→AIなしで差し替え）
  var favListBtn=document.getElementById('__ce_favlist');
  if(favListBtn) favListBtn.addEventListener('click',function(){
    var target=curSecEl();
    if(!target){ msg.textContent='まず①「どこを直す？」で入れ替える先のセクションを選んでください'; return; }
    fetch('/api/section_fav/list').then(function(r){return r.json();}).then(function(d){
      var favs=d.favs||[];
      var items = favs.length
        ? favs.map(function(f){
            // ★プレビューはJSを動かさないので、スクロール表示待ち(opacity:0)のままだと空に見える。
            //   だからプレビュー内は全部見える状態に強制する（本物の入れ替え先はJSで正しく出るので無関係）。
            var doc='<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0;background:#fff}'+(f.css||'')+' *,*::before,*::after{opacity:1 !important;visibility:visible !important;filter:none !important;clip-path:none !important;animation:none !important;transition:none !important}</style></head><body>'+f.html+'</body></html>';
            return '<div class="sit" data-id="'+f.id+'"><div class="pv"><iframe sandbox="allow-same-origin" srcdoc="'+esc(doc)+'"></iframe></div><div class="nm">'+esc(f.name||'')+'</div><button class="del" data-id="'+f.id+'" title="削除">×</button></div>';
          }).join('')
        : '<div style="color:#999;padding:8px">まだセクションのお気に入りがありません（⭐で保存できます）</div>';
      var ov=document.createElement('div'); ov.id='__ce_pk';
      ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span><h4>🔀 入れ替えるセクションを選ぶ（クリックで差し替え）</h4><div class="secgr">'+items+'</div></div>';
      document.body.appendChild(ov);
      ov.addEventListener('click',function(e){
        if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
        var del=e.target.closest('.del');
        if(del){ e.stopPropagation(); var did=del.getAttribute('data-id');
          fetch('/api/section_fav/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:did})}).then(function(){ var c=del.closest('.sit'); if(c) c.remove(); });
          return; }
        var it=e.target.closest('.sit'); if(!it) return;
        var id=it.getAttribute('data-id');
        var f=(favs||[]).filter(function(x){return x.id===id;})[0]; if(!f) return;
        target.outerHTML=f.html;   // AIなしで丸ごと差し替え
        ov.remove(); markDirty();
        msg.textContent='🔀 セクションを入れ替えました。上の「💾 保存」で確定してください';
      });
    }).catch(function(){ msg.textContent='お気に入り一覧の取得に失敗しました'; });
  });
  // その場で再生できるアニメ（AIなし）。g=種類(in/loop/char)、dir=動きの向き、sl=調整スライダー。
  // クリックで即プレビュー→スライダーで調整→「付ける」で無料で焼き込む。
  var FX=[
    {k:'fadeup',b:'ふわっと出現',d:'下から浮かぶ',g:'in',dir:'y',sl:[{k:'dist',l:'移動量',min:6,max:140,def:28},{k:'dur',l:'速さ',min:200,max:2200,def:800,u:'ms'}]},
    {k:'fade',b:'スッと出る',d:'フェード',g:'in',sl:[{k:'dur',l:'速さ',min:200,max:2200,def:800,u:'ms'}]},
    {k:'left',b:'左から',d:'スライドイン',g:'in',dir:'xl',sl:[{k:'dist',l:'移動量',min:10,max:220,def:48},{k:'dur',l:'速さ',min:200,max:2200,def:800,u:'ms'}]},
    {k:'right',b:'右から',d:'スライドイン',g:'in',dir:'xr',sl:[{k:'dist',l:'移動量',min:10,max:220,def:48},{k:'dur',l:'速さ',min:200,max:2200,def:800,u:'ms'}]},
    {k:'zoom',b:'ズームイン',d:'拡大しながら',g:'in',dir:'s',sl:[{k:'scale',l:'開始の大きさ',min:40,max:98,def:86,u:'%'},{k:'dur',l:'速さ',min:200,max:2200,def:800,u:'ms'}]},
    {k:'blur',b:'ぼやけて出現',d:'ブラー→くっきり',g:'in',dir:'bl',sl:[{k:'blur',l:'ぼかし',min:2,max:40,def:14},{k:'dur',l:'速さ',min:200,max:2200,def:900,u:'ms'}]},
    {k:'flip',b:'3Dフリップ',d:'くるっと回転',g:'in',dir:'ry',sl:[{k:'deg',l:'回転角',min:20,max:180,def:90,u:'°'},{k:'dur',l:'速さ',min:200,max:2200,def:800,u:'ms'}]},
    {k:'rise',b:'せり上がり',d:'下からスッと上へ',g:'in',dir:'clip',sl:[{k:'dist',l:'移動量',min:10,max:140,def:40},{k:'dur',l:'速さ',min:200,max:2200,def:900,u:'ms'}]},
    {k:'stagger',b:'一文字ずつ',d:'文字が順に出現',g:'char',sl:[{k:'stag',l:'文字の間隔',min:15,max:150,def:32,u:'ms'},{k:'dist',l:'移動量',min:0,max:56,def:26},{k:'dur',l:'速さ',min:150,max:900,def:340,u:'ms'}]},
    {k:'typewriter',b:'タイプライター',d:'打ち込み風',g:'char',type:1,sl:[{k:'stag',l:'打つ速さ',min:20,max:200,def:60,u:'ms'}]},
    {k:'wave',b:'波打ち',d:'文字が波打つ(ループ)',g:'char',loop:1,sl:[{k:'amp',l:'ゆれ幅',min:4,max:30,def:10},{k:'dur',l:'速さ',min:800,max:3000,def:1600,u:'ms'}]},
    {k:'glow',b:'ネオングロー',d:'光る(ループ)',g:'loop',glow:1,sl:[{k:'dur',l:'速さ',min:600,max:3200,def:1800,u:'ms'}]},
    {k:'pulse',b:'脈打つ',d:'鼓動(ループ)',g:'loop',dir:'ps',sl:[{k:'amp',l:'強さ',min:2,max:20,def:6,u:'%'},{k:'dur',l:'速さ',min:600,max:3000,def:1400,u:'ms'}]},
    {k:'float',b:'ゆらゆら',d:'浮遊(ループ)',g:'loop',dir:'fy',sl:[{k:'amp',l:'ゆれ幅',min:4,max:40,def:12},{k:'dur',l:'速さ',min:1000,max:4000,def:2200,u:'ms'}]},
    {k:'bounce',b:'バウンド',d:'弾む(ループ)',g:'loop',dir:'by',sl:[{k:'amp',l:'高さ',min:6,max:50,def:18},{k:'dur',l:'速さ',min:600,max:2600,def:1200,u:'ms'}]}
  ];
  // 「このセクションをおしゃれに」ボタンの一括指示。中身は保ちつつ誌面として作り直す。
  // ★「角丸＋影＋等間隔」の箱揃えに逃げるのが修正AIの癖なので、レイアウトのメリハリを最優先で指示する。
  // ★型をリストで並べてAIに選ばせると先頭（非対称グリッド）ばかり選ぶ癖がある
  //   → ボタンを押すたびにJS側でランダムに1型だけ指定する（毎回違う仕上がりになる）。
  // ★w=出やすさの重み。「箱が並ぶ」見た目が残る型（非対称/ずらし）は低め、
  //   箱をやめる型（互い違い/横帯）を出やすく。前回と同じ型は連続で選ばない（localStorage記憶）。
  var STYLE_TYPES=[
    {n:'非対称グリッド',w:2,i:'主役1枚をgrid-column:span 2等で2倍幅にし、残りを脇に小さく置く。新着1件目・代表的な1件を主役に選ぶ。3つの同型カードが1行に等間隔で並ぶ構図は残さない'},
    {n:'ずらし配置',w:1,i:'カードの大きさは活かしつつ、奇数番目と偶数番目でmargin-topを変えて段違いに置く（例 nth-child(even){margin-top:56px}）。さらにカードの幅か写真の高さも1枚ごとに変え、「同じ箱が3つ並んでいる」印象を消す'},
    {n:'互い違い型',w:3,i:'カード並びをやめ、写真と文章を左右交互の段に組み直す（1段=1項目で縦に積む。1段目=写真左・文章右、2段目=写真右・文章左）。項目同士を横に並べない。写真は大きく、文章側に番号やラベルを添える'},
    {n:'横帯リスト型',w:3,i:'カードをやめ、1行1項目の横帯に組み直す。罫線や大きな番号(01/02/03)で区切り、写真は小さなサムネイルとして行の端に置く'}
  ];
  // ★トップ（ヒーロー）専用の型。ユーザーが良例として挙げた過去カンプ2本の実CSSから型化
  //   （camp_20260702_223653=青コラージュ / camp_20260703_003012=るわみ。数値は実物から採取）。
  var HERO_TYPES=[
    {n:'縦書きヒーロー',w:3,i:'2カラムgrid（コピー側minmax(330px,460px)／写真側1fr・align-items:center・min-height:min(100vh,860px)）。'
      +'キャッチコピーはwriting-mode:vertical-rlの縦書き大見出し（clamp(40px,5vw,60px)・font-weight:800）にし、'
      +'半透明の紙カード（background:rgba(255,255,255,.85)・角丸・ブランド色の柔らかい影）に載せる。'
      +'写真は大きな角丸で反対側に置き、写真の角に小さなピル型バッジ（白地・11〜12pxの英字1〜2語・例 WARM SUPPORT）を1〜2個重ねる。'
      +'背景はブランド色の極薄グラデにし、::before/::afterで白薄の円や角丸枠を1〜2個浮かせる（pointer-events:none・z-index:0・文字より背面）'},
    {n:'コラージュヒーロー',w:2,i:'2カラムgrid（写真側1.3fr／コピー側.7fr・align-items:end）。'
      +'写真側は、上に小さな正方形写真2〜3枚（aspect-ratio:1/1.08・border:3px solid #fff・角丸10px・gap:12px）を並べ、'
      +'margin-bottom:-46pxで下の大きなメイン写真（width:min(720px,100%)・角丸18px）に差し込むように重ねる（この重なりが命）。'
      +'コピー側は、writing-mode:vertical-rlの縦書きキャッチ（56px級・font-weight:800・薄い白のtext-shadow）＋'
      +'小さな英字キッカー（11px・letter-spacing:.18em）＋本文2〜3行＋11px英字2行組の小ラベルを3個flexで並べる。'
      +'背景は白か極薄のブランド色。箱を等間隔に整列させない'},
    {n:'大見出し2カラム型',w:3,i:'左＝小さな英字キッカー（11px・letter-spacing:.18em）＋横書きの特大見出し'
      +'（clamp(44px,6vw,72px)・ブランド色・font-weight:800・2〜3行）＋本文3行前後＋ボタン2つ（塗り＋枠線の2種）。'
      +'右＝大きな写真1枚を、縦長の角丸パネル2〜3本（ブランド色の極薄・幅違い）のリズムの上に少しだけ重ねて置き、'
      +'右端に短い縦書きの一言（writing-mode:vertical-rl・小さめ）を添える。'
      +'見出しの背後にブランド色系の極薄グラデーション円を1つ大きく敷く（pointer-events:none・文字より背面）'}
  ];
  function pickStyleType(pool,storeKey,excl){
    var last='';
    try{ last=localStorage.getItem(storeKey)||''; }catch(e){}
    var ng={}; (excl||[]).concat([last]).forEach(function(n){ if(n) ng[n]=1; });
    var cand=pool.filter(function(t){return !ng[t.n];});
    if(!cand.length) cand=pool.filter(function(t){return t.n!==last;});  // 全部使用済みなら連続だけ避ける
    if(!cand.length) cand=pool;
    var total=cand.reduce(function(s,t){return s+t.w;},0), r=Math.random()*total, t=cand[0];
    for(var i=0;i<cand.length;i++){ r-=cand[i].w; if(r<=0){ t=cand[i]; break; } }
    try{ localStorage.setItem(storeKey,t.n); }catch(e){}
    return t;
  }
  function styleIns(sIdx){
    // 1番目のセクション＝ファーストビューはヒーロー専用の型、それ以外はリスト系の4型
    var hero=(Number(sIdx)<=0);
    // ★ページ内で使用済みの型（サーバーが data-cestyle として刻印）は抽選から除外
    //   ＝1セクションずつの編集でも「ページ全体が同じ表情」にならない
    var used=[].slice.call(document.querySelectorAll('section[data-cestyle]'))
      .map(function(s){return s.getAttribute('data-cestyle')||'';}).filter(Boolean);
    var t=hero?pickStyleType(HERO_TYPES,'__ce_style_last_hero',used):pickStyleType(STYLE_TYPES,'__ce_style_last',used);
    var usedNote=used.length?('★このページの他セクションでは既に【'+used.join('・')+'】の型を使用済み。'
      +'同じ見た目・同じあしらいをページ内で繰り返さない（特に大きな番号01/02/03は使用済みなら使わない）。'):'';
    return {t:t.n, ins:'プロのWebデザイナーとして、このセクションを「AIが整えた感」のない雑誌の誌面のように仕上げ直す。'
    +'【絶対条件・最優先（1つでも破ったら失格）】'
    +'(A)元のHTMLにある日本語テキストは、見出し・ラベル・説明文・電話番号まで**全文をそのまま新HTMLに残す**。'
    +'要約・省略・英語への置き換えは禁止。飾りの英語ラベルを足すのは可だが、その分日本語を削るのは不可。'
    +'特に各項目の説明文（段落）を落とすのが最頻の失敗。必ず残す。'
    +'(B)文字色は背景との明暗差を最優先：薄い背景・薄いグラデの上に白文字は禁止（濃色#222等にする）。'
    +'白文字を使ってよいのは十分に濃い背景の上だけ。'
    +'(C)中身のない巨大な空白面・色面を作らない：各段の高さは写真と文章の量に合わせ、'
    +'min-heightや過大なpaddingで引き伸ばさない。文章側が寂しければ元の説明文を大きめに組む（新しい文を発明しない）。'
    +'(D)同じ形の箱・項目を**横に3つ以上並べない**（grid-template-columns:repeat(3,..)や3つ横並びのflex禁止。'
    +'列は最大2列。例外は小さなタグ/英字ラベルだけ）。'
    +'(E)写真やカードをposition/負マージンで重ねる時は、相手の**縁に40〜50pxだけ**触れる程度にする。'
    +'写真の被写体や文字を覆い隠す大きな重なりは禁止。'
    +(hero
      ?'【レイアウトの組み替え】ここはページの顔（ファーストビュー）。今回は必ず【'+t.n+'】の構図に組み替える：'+t.i+'。'
       +'★セクション全体の高さは**100vh前後（最大110vh）**に収める。要素を縦に積んで長くせず、'
       +'2カラムの中に収まるよう各要素を小さくまとめる。中に項目リストがあっても3列に並べない。'
       +'★ヒーローの固定ルール（施主の要望・必ず守る）：'
       +'(あ)主役は「写真」と「キャッチコピー」の2つだけ。他の要素は大きさ・彩度を明確に落として脇役にする。'
       +'(い)要素は2カラムに集約する。四隅に散らさない・中央に大きな空白を作らない。'
       +'(う)余白はゆったり派＝要素の数を増やすより、1つ1つを大きく堂々と置く。'
       +'(え)作業・プログラム紹介などのカード群がこのセクション内にある場合は、目立たせず'
       +'最下部に小さな横1行の帯として畳む（文章は消さずに小さく）。細い縦長カードに文字を押し込まない。'
       +'(お)縦書きにする場合は「ゃゅょっ」や句読点が行頭に来ない改行位置にし、はみ出して切れないか確認する。'
       +'英単語は途中で改行しない（white-space:nowrapか十分な幅）。'
      :'【レイアウトの組み替え】同じ大きさの箱が均等に並んでいるだけなら、今回は必ず【'+t.n+'】の型で組み替える：'+t.i+'。')
    +'（この型がこのセクションの内容に物理的に合わない場合のみ＝例:項目が1つしか無い等、他の型に替えてよい）'
    +usedNote
    +'❌禁止：角丸と影を付けて等間隔に並べ直すだけの修正（それが最もAIっぽい）。全カード同じ大きさ・同じ形のまま終わらせない。'
    +'【余白】8pxの倍数（8/16/24/40/64px）で整え、見出しとその本文は近く・別の話題とは大きく空ける（近接の原則）。'
    +'【タイポグラフィ】階層を明確に：見出しは大きく太く（clamp()で流体に）、日本語本文は行間1.9前後・字間0.02em、長い行はmax-widthで制御。日付やカテゴリ等のメタ情報は小さく淡く。'
    +'【配色】既存のブランド色の範囲内。アクセント色はCTAや強調ラベルだけに絞る。背景に極薄のブランド色ティントを敷くのは可（その場合も文字は濃色）。'
    +'【あしらい】ブランド色寄りの柔らかい影（例 0 12px 32px rgba(ブランド色,0.12)）。真っ黒の強い影は禁止。線・番号・小さな英語ラベルなど、雰囲気に合う小物を1つ効かせる。'
    +'【禁じ手】紫グラデ・絵文字アイコン多用・左右対称の繰り返し。'
    +'セクションの順番・情報量は保つ。組み替えに必要ならこのセクション内のHTML構造は変えてよい。'
    +'html.jsが付いた時だけ初期非表示にする保険を入れ、JSが無くても中身が見える状態を保つ。'
    +'★このセクションだけに適用し、他のセクションや他の要素は一切変えない。'
    +'【出力前セルフチェック（全部YESになるまで出力しない）】'
    +'(1)元の日本語テキストが全文残っているか（説明文の消失が最頻の失敗） '
    +'(2)元の画像が全て残り、潰れず表示されるか（枠はoverflow:hidden＋imgはobject-fit:cover） '
    +'(3)全ての文字が背景色に対して読めるコントラストか '
    +'(4)中身のない巨大な色面・空白面ができていないか。1つでもNOなら組み替えを簡素化してでも中身の表示を優先する。'};
  }
  fetch('/api/camp_sections?file='+encodeURIComponent(FILE)).then(function(r){return r.json();}).then(function(d){
    (d.sections||[]).forEach(function(s){var o=document.createElement('option');o.value=s.index;o.textContent=(s.index+1)+'. '+s.label;sec.appendChild(o);});
  }).catch(function(){});
  // アニメのプリセット（説明つき・クリックで選んだ所に適用）
  var PRESETS=[
   {b:'ふわっと出現',d:'スクロールで下から浮かぶ',i:'このセクションの主要な要素に、スクロールで画面に入ったら下から少し上へ動きながらフェードインする出現アニメを付ける。複数要素は時間差(stagger)で。IntersectionObserverで実装し、html.jsが付いた時だけ初期非表示にする保険を入れてJSが無くても中身が見えるようにする'},
   {b:'ホバーで浮く',d:'カード/ボタンが浮く',ai:1,i:'このセクションのカードやボタンに、ホバーで少し浮き上がり影が濃くなる滑らかなtransitionを付ける'},
   {b:'画像ズーム',d:'画像がゆっくり拡大',ai:1,i:'このセクションの画像に、ホバー時にゆっくり拡大するズーム効果を付ける（枠はoverflow:hidden、imgはobject-fit:coverではみ出さない）'},
   {b:'横からスライド',d:'左右からスッと登場',i:'このセクションの要素を、スクロールで画面に入ったら左右から滑り込んで現れるアニメにする（保険付き）'},
   {b:'見出しを強調',d:'タイトルが順に出る',i:'このセクションの見出しを、表示時に単語ごとに少しずつ現れる軽いアニメにする'},
   {b:'背景グラデ',d:'背景色がゆっくり流れる',ai:1,i:'このセクションの背景に、ゆっくり色が移動するグラデーションアニメ(@keyframes)を付ける'},
   {b:'波の区切り',d:'セクション境界に波',ai:1,i:'このセクションの下端に、SVGの波型の区切り装飾を入れる'},
   {b:'パララックス',d:'背景がゆっくり動く',ai:1,i:'このセクションの背景に、スクロールに合わせてゆっくり視差移動する簡易パララックスをCSS/素のJSで付ける'},
   {b:'数字カウント',d:'数値が0から増える',ai:1,i:'このセクションに実績や料金などの数値があれば、表示時に0から目標値までカウントアップするアニメを付ける'},
   {b:'脈打つCTA',d:'ボタンが軽く鼓動',i:'このセクションのメインのボタン(CTA)に、注意を引く軽い鼓動(pulse)アニメを付ける（うるさくない範囲で）'},
   {b:'背景にドット柄',d:'水玉の模様を薄く',bg:1,i:'このセクションの背景に、薄いドット(水玉)柄をCSSのradial-gradientで敷く。既存の背景色の上に重ね、文字が読める薄さにする。中身のレイアウトや文字は変えない'},
   {b:'背景にストライプ',d:'斜めの縞模様',bg:1,i:'このセクションの背景に、ごく薄い斜めストライプをrepeating-linear-gradientで付ける。うるさくない薄さで文字は可読に保つ。中身は変えない'},
   {b:'背景に方眼',d:'細いグリッド線',bg:1,i:'このセクションの背景に、細い方眼(グリッド)線をrepeating-linear-gradientで薄く付ける。既存の背景色を活かし文字は可読に。中身は変えない'},
   {b:'背景に幾何学',d:'図形をあしらう',bg:1,i:'このセクションの背景に、円・斜めブロックなどの幾何学装飾を疑似要素(::before/::after)かインラインSVGで、コンテンツの邪魔にならない薄さ・低い重なり順(z-index)で配置する。文字や画像は前面のまま変えない'},
   {b:'紙の質感',d:'ざらっとした地',bg:1,i:'このセクションの背景に、紙のようなごく薄いノイズ/テクスチャの質感を、CSSの多重グラデかインラインSVGフィルタで付ける。文字は可読に保ち中身は変えない'},
   {b:'背景に水彩にじみ',d:'青のふんわり滲み',bg:1,i:'このセクションの背景に、水彩絵の具がにじんだような柔らかい青系の斑点を、複数のradial-gradient（ぼかし強め=境界をなだらかに、不透明度は低め0.1〜0.25）を数個ランダムな位置に重ねて表現する。既存の背景色の上に敷き、コンテンツより背面(z-index低)に置く。文字・画像・レイアウトは一切変えず、文字は読める濃さに保つ'},
  ];
  document.getElementById('__ce_ags').innerHTML=PRESETS.map(function(p,i){return '<button class="ag" data-i="'+i+'"><b>'+esc(p.b)+'</b><span>'+esc(p.d)+'</span></button>';}).join('');
  document.getElementById('__ce_ags').addEventListener('click',function(e){var b=e.target.closest('.ag');if(b)submit(sec.value,PRESETS[+b.dataset.i].i);});
  function busy(b){go.disabled=b;sg.disabled=b;}
  // 画面中央下に「直しています…○秒」の分かりやすい進捗トースト
  var _toast=null,_toastT=null,_toastStart=0,_toastPhase='AIが直しています…';
  function showToast(t){
    _toastPhase=t||'AIが直しています…';
    if(!_toast){
      _toast=document.createElement('div'); _toast.id='__ce_toast';
      _toast.innerHTML='<div class="bar"><span></span></div><div class="tx" id="__ce_toasttx"></div>';
      document.body.appendChild(_toast); _toastStart=Date.now();
      _toastT=setInterval(function(){
        var s=Math.floor((Date.now()-_toastStart)/1000);
        var el=document.getElementById('__ce_toasttx'); if(el) el.textContent='🔧 '+_toastPhase+'  '+s+'秒';
      },300);
    }
  }
  function setToast(t){ _toastPhase=t; }
  function hideToast(){ if(_toastT){clearInterval(_toastT);_toastT=null;} if(_toast){_toast.remove();_toast=null;} }
  // 改善中のセクションを紫枠＋バッジで目立たせる（今どこを処理しているか分かるように）
  var _busyEl=null;
  function markSectionBusy(idx){
    clearSectionBusy();
    if(!(Number(idx)>=0)) return;
    var els=[].slice.call(document.querySelectorAll('section')).filter(function(x){return !x.closest('#__ce');});
    var el=els[Number(idx)]; if(!el) return;
    _busyEl=el; el.classList.add('__ce_busy');
    try{ el.scrollIntoView({behavior:'smooth',block:'start'}); }catch(_){}
  }
  function clearSectionBusy(){ if(_busyEl){ _busyEl.classList.remove('__ce_busy'); _busyEl=null; } }
  function submit(section,instruction,keepText,styleType){
    if(!instruction){msg.textContent='指示が空です';return;}
    // ページ全体(-1)は"全文を書き直す"＝高い(数十円)・遅い。特定箇所なら安い(数円)。
    if(Number(section)<0){
      if(!confirm('⚠ これは「ページ全体を書き直す」修正です。\\n時間がかかり、料金も高め（数十円〜）になります。\\n\\n特定の場所だけ直すなら【キャンセル】して、\\n・①で直すセクションを選ぶ か\\n・直したい所を右クリック\\nすると安く（数円）速く直せます。\\n\\nこのままページ全体を直しますか？')) { msg.textContent='キャンセルしました（①でセクションを選ぶと安いです）'; return; }
    }
    busy(true); msg.textContent='今の状態を保存中…'; showToast('AIが直しています…（十数秒〜）'); markSectionBusy(section);
    // ★AIに渡す前に、今の見た目（移動・手修正・焼き込みアニメ）をディスクへ保存する。
    //   AIはファイルを読んで直すので、保存しないと「以前の状態」に対してかかり手修正が戻ってしまう。
    flushThen(function(){
      msg.textContent='生成中…';
      fetch('/api/edit_camp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,section:Number(section),instruction:instruction,keep_text:keepText?1:0,style_type:styleType||''})})
      .then(function(r){return r.json();}).then(function(d){
        if(!d.ok){msg.textContent='失敗：'+d.message;busy(false);hideToast();clearSectionBusy();return;}
        poll(d.job_id);
      }).catch(function(){msg.textContent='通信エラー';busy(false);hideToast();clearSectionBusy();});
    });
  }
  // AIに渡す前に、今のDOM（移動・手修正・焼き込み）をディスクに保存してからcbを実行する（＝AIが「今」を見る）。
  function flushThen(cb){
    var html;
    try{ html=cleanHtml(); }catch(_){ cb(); return; }
    fetch('/api/save_camp_html',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,html:html})})
      .then(function(r){return r.json();}).then(function(){
        _dirty=false; var sb=document.getElementById('__ce_save'); if(sb){ sb.textContent='💾 保存'; sb.classList.remove('saved'); }
        cb();
      }).catch(function(){ cb(); });  // 保存に失敗しても処理は続ける（最悪でも従来どおり）
  }
  function poll(id,miss){
    miss=miss||0;
    // ★ジョブが見つからない状態が続く＝サーバー再起動でジョブが消えた（幽霊トースト）。
    //   永遠に回さず、10回（約12秒）で諦めてユーザーに伝える。
    if(miss>=10){
      msg.textContent='⚠ ジョブが見つかりません。サーバーが再起動されて処理が消えた可能性があります。もう一度実行してください';
      setToast('⚠ 処理が中断されました（もう一度どうぞ）'); setTimeout(hideToast,3500);
      busy(false); clearSectionBusy(); return;
    }
    fetch('/api/generate_camp/status').then(function(r){return r.json();}).then(function(d){
      var j=(d.jobs||{})[id];
      if(!j){setTimeout(function(){poll(id,miss+1);},1200);return;}
      if(j.state==='running'){msg.textContent=(j.phase||'生成中…');setToast(j.phase||'AIが直しています…');setTimeout(function(){poll(id,0);},1200);}
      else if(j.state==='done'){setToast('✅ できました。開きます…');msg.textContent='できました。開きます…';location.href='/camp/'+j.file;}
      else{msg.textContent='失敗：'+(j.message||'');busy(false);hideToast();clearSectionBusy();}
    }).catch(function(){setTimeout(function(){poll(id,miss+1);},1500);});
  }
  go.addEventListener('click',function(){submit(sec.value,inp.value.trim());});
  sg.addEventListener('click',function(){
    busy(true); chips.innerHTML=''; msg.textContent='この部分を見て案を考え中…（十数秒）';
    fetch('/api/camp_suggest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,section:Number(sec.value)})})
    .then(function(r){return r.json();}).then(function(d){
      busy(false);
      if(!d.ok){msg.textContent='失敗：'+d.message;return;}
      var arr=d.suggestions||[];
      msg.textContent=arr.length?('案を'+arr.length+'個出しました。押すとその場で直します'):'案が出ませんでした';
      chips.innerHTML=arr.map(function(s){return '<button class="chip" data-sec="'+s.section+'" data-ins="'+esc(s.instruction)+'">'+esc(s.label)+'</button>';}).join('');
    }).catch(function(){busy(false);msg.textContent='通信エラー';});
  });
  chips.addEventListener('click',function(e){var c=e.target.closest('.chip');if(c)submit(c.dataset.sec,c.dataset.ins);});
  // 画像差し替えモード（AI不使用・無料）：カンプの画像をクリック→アップロード画像に置換
  var imgBtn=document.getElementById('__ce_img'), imgMode=false;
  function camImgs(){ return [].slice.call(document.querySelectorAll('img')).filter(function(im){return !im.closest('#__ce')&&!im.closest('#__ce_pk');}); }
  function setImgMode(on){
    imgMode=on;
    camImgs().forEach(function(im){ im.classList.toggle('__ce_hl',on); });
    imgBtn.textContent = on ? '✋ 差し替えを終える' : '🖼 画像を差し替え（AIなし・無料）';
    msg.textContent = on ? '差し替えたい画像をクリックしてください' : 'セクションを選び、指示か改善案で直せます';
    if(box.classList.contains('min')) box.classList.remove('min');
  }
  imgBtn.addEventListener('click',function(){ setImgMode(!imgMode); });
  // 📐 横幅をそろえる（AIなし）：各セクションの「中身の器」の幅を測り、多数派の幅に統一する。
  // ・全幅（ページ幅の93%以上）＝意図的な全幅背景/帯なので触らない
  // ・多数派から30%以上ズレている箱＝意図的に狭い/広い（とっておく）
  // ・画像そのものは器として扱わない（飛ばす）
  function innerBox(s){
    var pageW=document.documentElement.clientWidth, pick=null;
    function scan(kids){
      [].forEach.call(kids,function(k){
        if(k.nodeType!==1||k.tagName==='IMG'||k.closest('#__ce')) return;
        var w=k.getBoundingClientRect().width;
        if(w>0 && w<pageW*0.93 && (!pick||w>pick.w)) pick={el:k,w:w};
      });
    }
    scan(s.children);
    if(!pick){ [].forEach.call(s.children,function(k){ if(k.nodeType===1) scan(k.children); }); }  // 直下が全部全幅ラッパーなら1段降りる
    return pick;
  }
  var alignBtn=document.getElementById('__ce_align');
  if(alignBtn) alignBtn.addEventListener('click',function(){
    var secs=[].slice.call(document.querySelectorAll('section')).filter(function(x){return !x.closest('#__ce');});
    var items=[];
    secs.forEach(function(s){ var p=innerBox(s); if(p) items.push(p); });
    if(items.length<2){ msg.textContent='そろえられるセクションが2つ未満でした（全幅構成のようです）'; return; }
    // 多数派の幅＝20px刻みの最頻値
    var buckets={};
    items.forEach(function(it){ var k=Math.round(it.w/20)*20; buckets[k]=(buckets[k]||0)+1; });
    var target=+Object.keys(buckets).sort(function(a,b){ return buckets[b]-buckets[a]; })[0];
    function applyW(el){
      el.style.setProperty('width','min('+target+'px, calc(100% - 48px))','important');
      el.style.setProperty('max-width','none','important');
      el.style.setProperty('margin-left','auto','important');
      el.style.setProperty('margin-right','auto','important');
    }
    var fixed=0, kept=0;
    items.forEach(function(it){
      var diff=Math.abs(it.w-target);
      if(diff<=6) return;                       // もう揃っている
      if(diff>target*0.3){ kept++; return; }    // 明らかに違う幅＝意図的なのでとっておく
      applyW(it.el); fixed++;
    });
    // 2段目：器が全幅に壊れているセクション（width:100%上書き事故など）を救う。
    // 中身に文章がある全幅の直下ボックスを基準幅に矯正（背景はsection側に残るので全幅のまま）
    secs.forEach(function(s){
      var p=innerBox(s); if(p) return;  // 1段目で器が見つかったセクションは対象外
      var pageW=document.documentElement.clientWidth;
      [].some.call(s.children,function(k){
        if(k.nodeType!==1||k.tagName==='IMG'||k.closest('#__ce')) return false;
        var cs=getComputedStyle(k);
        if(cs.position==='absolute'||cs.position==='fixed') return false;
        var w=k.getBoundingClientRect().width;
        var hasText=(k.textContent||'').replace(/\s+/g,'').length>30;
        if(w>=pageW*0.93 && hasText){ applyW(k); fixed++; return true; }
        return false;
      });
    });
    if(fixed) markDirty();
    msg.textContent = fixed
      ? ('📐 '+fixed+'個のセクションを幅'+target+'pxにそろえました'+(kept?('。'+kept+'個は明らかに違う幅なのでそのまま'):'')+'（💾保存で確定・⟲で戻せます）')
      : ('すでに全部そろっています（基準幅 '+target+'px'+(kept?('・意図的に違う'+kept+'個はそのまま'):'')+'）');
  });
  document.addEventListener('click',function(e){
    if(!imgMode) return;
    var im=e.target.closest('img'); if(!im||im.closest('#__ce')||im.closest('#__ce_pk')) return;
    e.preventDefault(); e.stopPropagation();
    openPicker({el:im,type:'img',url:im.currentSrc||im.src});
  }, true);
  // cand = {el, type:'img'|'bg', url}。img も 背景画像 も同じ入口で差し替える。
  function openPicker(cand){
    fetch('/api/uploads').then(function(r){return r.json();}).then(function(d){
      var ups=d.uploads||[];
      var items = ups.length
        ? ups.map(function(u){return '<div class="it" data-src="'+u.url+'"><img src="'+u.url+'"><span>'+esc(u.caption||u.file)+'</span></div>';}).join('')
        : '<div style="color:#999">アップロード画像がありません（生成パネルの「🖼自分の画像」で追加してください）</div>';
      var ov=document.createElement('div'); ov.id='__ce_pk';
      ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span><h4>差し替える画像を選ぶ</h4><div class="gr">'+items+'</div></div>';
      document.body.appendChild(ov);
      ov.addEventListener('click',function(e){
        if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
        var it=e.target.closest('.it'); if(!it) return;
        if(!cand||!cand.el){ msg.textContent='対象が見つかりません'; ov.remove(); return; }
        ov.remove(); msg.textContent='差し替え中…';
        var url=it.dataset.src;
        // 背景画像でも<img>でも、ブラウザ側で差し替えて位置・角度ごと保存（角度が戻らない）
        if(cand.type==='bg'){
          cand.el.style.setProperty('background-image','url("'+url+'")','important');
          // 画像を持たない要素に新しく入れる時は、要素いっぱいに綺麗に写るよう cover 表示にする
          if(cand.fresh){
            cand.el.style.setProperty('background-size','cover','important');
            cand.el.style.setProperty('background-position','center','important');
            cand.el.style.setProperty('background-repeat','no-repeat','important');
          }
        } else {
          cand.el.src=url; cand.el.removeAttribute('srcset');
          var pic=cand.el.closest?cand.el.closest('picture'):null;
          if(pic){ [].slice.call(pic.querySelectorAll('source')).forEach(function(s){s.removeAttribute('srcset');}); }
        }
        markDirty(); saveLayout();
      });
    }).catch(function(){msg.textContent='画像一覧の取得に失敗';});
  }
  // 重なった画像（img・背景）のうち、どれを差し替えるかをサムネで先に選ばせる
  function pickWhichImg(list){
    var items=list.map(function(c,i){return '<div class="it" data-i="'+i+'"><img src="'+c.url+'"><span>'+(c.type==='bg'?'背景':'画像')+(i+1)+(i===0?'（前面）':'')+'</span></div>';}).join('');
    var ov=document.createElement('div'); ov.id='__ce_pk';
    ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span><h4>どの画像を差し替える？（重なっています）</h4><div class="gr">'+items+'</div></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click',function(e){
      if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
      var it=e.target.closest('.it'); if(!it) return;
      var c=list[+it.getAttribute('data-i')]; ov.remove(); closeMenu();
      openPicker(c);
    });
  }
  // 画像の背後にアップロード画像を敷く（水彩テクスチャ等）。選ぶとAIがその画像URLを背景に敷く。
  function openBgPicker(imgEl, sIdx){
    fetch('/api/uploads').then(function(r){return r.json();}).then(function(d){
      var ups=d.uploads||[];
      var items = ups.length
        ? ups.map(function(u){return '<div class="it" data-src="'+u.url+'"><img src="'+u.url+'"><span>'+esc(u.caption||u.file)+'</span></div>';}).join('')
        : '<div style="color:#999">アップロード画像がありません（生成パネルの「🖼自分の画像」で水彩などを追加してください）</div>';
      var ov=document.createElement('div'); ov.id='__ce_pk';
      ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span><h4>背後に敷く画像を選ぶ（水彩など）</h4><div class="gr">'+items+'</div></div>';
      document.body.appendChild(ov);
      ov.addEventListener('click',function(e){
        if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
        var it=e.target.closest('.it'); if(!it) return;
        ov.remove();
        var url=it.dataset.src, dsc=descEl(imgEl);
        var ins='次の画像「'+dsc+'」の背後に装飾の背景画像を敷く。'
          +'この画像を囲むラッパー要素（無ければ作る）の背景に background-image:url('+url+') を background-size:cover; background-position:center; no-repeat で設定し、'
          +'ラッパーは元画像より少し大きめ(padding)にして背景が画像のまわりに少しはみ出して見えるようにする。'
          +'元のコンテンツ画像は前面(position:relative; z-index上)に保つ。セクション全体には広げず、この画像の周辺だけ。文字・レイアウト・他の要素は変えない。';
        applyEl(sIdx, ins);
      });
    }).catch(function(){msg.textContent='画像一覧の取得に失敗';});
  }
  // 🌸 背景の飾り（やわらかグラデ）を「要素の後ろ」に敷く（AIなし・即反映）。
  //   負のz-indexの装飾divを要素の先頭に入れる。isolationで後ろに逃げないよう囲む。
  //   画像の周りの回る黒リング等は前面のまま＝飾りはその背後に入る。
  var BG_SHAPES={   // 形（border-radius）の作り方
    oval:  '50%',                                          // 丸型（楕円）
    blob:  '62% 38% 55% 45% / 45% 55% 42% 58%',             // しずく型（不定形）
    round: '40px',                                          // 角丸四角
    square:'0'                                               // 四角
  };
  function bgTarget(el){
    var t = el.tagName==='IMG' ? (el.parentElement||el) : el;   // imgには子を入れられないので親に敷く
    return (!t||t===document.body) ? null : t;
  }
  // 対象に飾りdivが無ければ作る（既にあれば取得するだけ）。形・大きさの調整はこれを使い回す。
  function ensureBackdrop(target){
    if(getComputedStyle(target).position==='static') target.style.setProperty('position','relative');
    target.style.setProperty('isolation','isolate');   // 負のz-indexが祖先の後ろへ抜けないよう囲む
    // 角丸写真は親にoverflow:hiddenが付いていることが多く、それだとはみ出す飾りが見えない→強制で見えるようにする
    target.style.setProperty('overflow','visible','important');
    var bg=target.querySelector(':scope > .ce_bgdeco');
    if(!bg){
      bg=document.createElement('div'); bg.className='ce_bgdeco'; bg.setAttribute('aria-hidden','true');
      bg.dataset.size='22'; bg.dataset.shape='oval';
      bg.style.cssText='position:absolute;inset:-22%;z-index:-1;pointer-events:none;border-radius:'+BG_SHAPES.oval+';filter:blur(2px);background:radial-gradient(60% 55% at 50% 45%, #eef1f5 0%, #f6f8fb 60%, #ffffff 100%);';
      target.insertBefore(bg, target.firstChild);   // 先頭＝一番後ろに置く
    }
    return bg;
  }
  function applyBackdrop(el, grad){
    var target=bgTarget(el); if(!target){ msg.textContent='ここには敷けません（すぐ外側の箱を右クリックしてください）'; return; }
    var bg=ensureBackdrop(target);
    bg.style.background=grad;
    markDirty();
    msg.textContent='背景の飾りを要素の後ろに敷きました（保存で確定）。下の「形」「大きさ」でも調整できます';
  }
  function setBackdropShape(el, shape){
    var target=bgTarget(el); if(!target) return;
    var bg=ensureBackdrop(target);
    bg.style.setProperty('border-radius', BG_SHAPES[shape]||BG_SHAPES.oval);
    bg.dataset.shape=shape;
    markDirty();
  }
  function setBackdropSize(el, delta){
    var target=bgTarget(el); if(!target) return;
    var bg=ensureBackdrop(target);
    var cur=parseFloat(bg.dataset.size||'22');
    var next=Math.max(5, Math.min(70, cur+delta));
    bg.dataset.size=String(next);
    bg.style.inset='-'+next+'%';
    markDirty();
  }
  function removeBackdrop(el){
    var target=bgTarget(el); if(!target) return;
    var bg=target.querySelector(':scope > .ce_bgdeco');
    if(bg) bg.remove();
    markDirty();
    msg.textContent='背景の飾りを消しました（保存で確定）';
  }
  // ⭕ 輪郭だけのリング（塗りつぶし無し・ゆっくり回転）。背景の飾り(グラデ)とは別レイヤーで重ねて使える。
  var RING_COLORS={
    soft: 'rgba(255,255,255,.55)',    // 白っぽい・淡い
    blue: 'rgba(120,150,200,.4)',     // 水色寄り
    dark: 'rgba(40,40,45,.25)'        // 濃いめ
  };
  function ensureRingCss(){
    if(document.getElementById('__ce_ringcss')) return;
    var st=document.createElement('style'); st.id='__ce_ringcss';
    // 回転はゆっくり(46秒/周)＝派手にならず「うっすらお洒落」程度に留める
    st.textContent='@keyframes ce_ring_spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}'
      +'.ce_ringdeco{animation:ce_ring_spin 46s linear infinite}';
    document.head.appendChild(st);
  }
  function toggleRing(el, colorKey){
    var target=bgTarget(el); if(!target){ msg.textContent='ここには追加できません'; return; }
    var ring=target.querySelector(':scope > .ce_ringdeco');
    if(ring && ring.dataset.ring===colorKey){ ring.remove(); markDirty(); msg.textContent='リングを消しました（保存で確定）'; return; }
    if(getComputedStyle(target).position==='static') target.style.setProperty('position','relative');
    target.style.setProperty('isolation','isolate');
    target.style.setProperty('overflow','visible','important');
    ensureRingCss();
    if(!ring){
      ring=document.createElement('div'); ring.className='ce_ringdeco'; ring.setAttribute('aria-hidden','true');
      ring.style.cssText='position:absolute;inset:-9%;z-index:-1;pointer-events:none;border-radius:50%;';
      target.insertBefore(ring, target.firstChild);
    }
    ring.dataset.ring=colorKey;
    ring.style.borderColor=RING_COLORS[colorKey]||RING_COLORS.soft;
    ring.style.borderStyle='solid';
    ring.style.borderWidth='1px';
    markDirty();
    msg.textContent='輪郭だけのリングを重ねました（ゆっくり回転・保存で確定／同じ色をもう一度押すと外す）';
  }
  // 🖼 四角い縁取り線をずらして重ねる（塗りつぶし無し・回転しない・写真がもう1枚後ろにあるような奥行き）。
  //   リングと違い角丸四角形で、位置を上下左右にずらして「奥にもう1枚ある」ように見せる。
  var OUTLINE_COLORS={
    soft: 'rgba(255,255,255,.7)',
    blue: 'rgba(90,140,210,.55)',
    dark: 'rgba(40,40,45,.35)'
  };
  // 画像がある場合は「親基準の%オフセット」だと親に余白がある時にズレて見える（斜めに見えない原因）ので、
  // 画像そのものの実寸(offsetLeft/Top/Width/Height)を測って、そこから斜めにずらした位置に直接置く。
  function _outlineImg(el){ return el.tagName==='IMG' ? el : (el.querySelector && el.querySelector('img')); }
  function _positionOutline(ol, img, target, dir){
    var shift=16, dx = dir==='tr'?shift:-shift, dy = dir==='tr'?-shift:shift;
    if(img && img.parentElement===target){
      ol.style.left=(img.offsetLeft+dx)+'px';
      ol.style.top=(img.offsetTop+dy)+'px';
      ol.style.width=img.offsetWidth+'px';
      ol.style.height=img.offsetHeight+'px';
      ol.style.right='auto'; ol.style.bottom='auto';
    } else {
      // 画像を持たない要素そのものを囲む場合は、要素自身基準で少し斜めにずらす
      ol.style.left='auto'; ol.style.top='auto'; ol.style.width='auto'; ol.style.height='auto';
      ol.style.inset = dir==='tr' ? '-16px -16px 16px 16px' : '16px 16px -16px -16px';
    }
  }
  function toggleOutline(el, colorKey){
    var img=_outlineImg(el);
    var target = img ? (img.parentElement||el) : el;
    if(!target || target===document.body){ msg.textContent='ここには追加できません'; return; }
    var ol=target.querySelector(':scope > .ce_outlinedeco');
    if(ol && ol.dataset.outline===colorKey){ ol.remove(); markDirty(); msg.textContent='縁取り線を消しました（保存で確定）'; return; }
    if(getComputedStyle(target).position==='static') target.style.setProperty('position','relative');
    target.style.setProperty('isolation','isolate');   // 負のz-indexが祖先の後ろへ抜けて隠れないよう囲む
    target.style.setProperty('overflow','visible','important');
    if(!ol){
      ol=document.createElement('div'); ol.className='ce_outlinedeco'; ol.setAttribute('aria-hidden','true');
      ol.dataset.dir='tr';
      ol.style.cssText='position:absolute;z-index:-1;pointer-events:none;border-radius:32px;border-style:solid;border-width:2px;';
      // 既にある背景ブロブ(ce_bgdeco)より後ろに置くと隠れて見えなくなる（同じz-index:-1同士はDOM順で後が上）。
      // 写真(position:staticのimg)より必ず後ろに描画される点は変わらないので、末尾に足してブロブより手前に出す。
      target.appendChild(ol);
    }
    _positionOutline(ol, img, target, ol.dataset.dir||'tr');
    ol.dataset.outline=colorKey;
    ol.style.borderColor=OUTLINE_COLORS[colorKey]||OUTLINE_COLORS.blue;
    markDirty();
    msg.textContent='縁取り線をずらして重ねました（保存で確定／同じ色をもう一度押すと外す／向きは↔で変更）';
  }
  function flipOutlineDir(el){
    var img=_outlineImg(el);
    var target = img ? (img.parentElement||el) : el;
    var ol = target && target.querySelector(':scope > .ce_outlinedeco');
    if(!ol){ msg.textContent='先に縁取り線の色を選んで追加してください'; return; }
    var nd = ol.dataset.dir==='tr' ? 'bl' : 'tr';
    ol.dataset.dir=nd;
    _positionOutline(ol, img, target, nd);
    markDirty();
    msg.textContent='縁取り線の向きを変えました（保存で確定）';
  }
  function openGradPicker(el){
    if(!el){ msg.textContent='対象がありません'; return; }
    var GRADS=[
     ['そら（水色）','radial-gradient(60% 55% at 50% 45%, #cdeafe 0%, #e8f4ff 45%, #eef1fb 100%)'],
     ['みず×ミント','radial-gradient(50% 45% at 68% 28%, #bdf3ea 0%, rgba(189,243,234,0) 60%), radial-gradient(60% 55% at 38% 62%, #cfe6ff 0%, #eef2fb 75%)'],
     ['さくら（桃）','radial-gradient(60% 55% at 50% 45%, #ffd9e6 0%, #ffe9f0 45%, #fbeef4 100%)'],
     ['ゆうやけ（暖）','radial-gradient(55% 50% at 30% 30%, #ffe0c2 0%, rgba(255,224,194,0) 60%), radial-gradient(60% 55% at 70% 65%, #ffd0d6 0%, #fdeef0 75%)'],
     ['ラベンダー','radial-gradient(60% 55% at 50% 45%, #e2d7ff 0%, #eee9ff 45%, #f3f0fb 100%)'],
     ['やわらかグレー','radial-gradient(60% 55% at 50% 40%, #eef1f5 0%, #f6f8fb 60%, #ffffff 100%)']
    ];
    var SHAPE_H=[['oval','⬭ 丸型'],['blob','💧 しずく型'],['round','▢ 角丸四角'],['square','◻ 四角']];
    var items=GRADS.map(function(g,i){return '<div class="it" data-i="'+i+'"><div style="height:80px;background:'+g[1]+'"></div><span>'+esc(g[0])+'</span></div>';}).join('');
    var shapeH=SHAPE_H.map(function(s){return '<button class="go2" data-shape="'+s[0]+'" style="background:#0b6bcb;margin:0">'+s[1]+'</button>';}).join('');
    var ov=document.createElement('div'); ov.id='__ce_pk';
    ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span><h4>背景の飾りを選ぶ（要素の後ろ・AIなし）</h4><div class="gr">'+items+'</div>'
      +'<div class="cap" style="margin-top:12px">かたち</div>'
      +'<div class="__ce_size" style="grid-template-columns:repeat(4,1fr)">'+shapeH+'</div>'
      +'<div class="cap" style="margin-top:10px">大きさ</div>'
      +'<div class="__ce_size"><button class="go2" id="__ce_bgsm" style="background:#888;margin:0">－ 小さく</button><button class="go2" id="__ce_bgbg" style="background:#888;margin:0">＋ 大きく</button></div>'
      +'<button class="go2" id="__ce_bgrm" style="background:#c0392b">🚫 飾りを消す</button>'
      +'<div class="cap" style="margin-top:12px">⭕ 輪郭だけのリングを重ねる（ゆっくり回転・お洒落に・任意）</div>'
      +'<div class="__ce_size" style="grid-template-columns:repeat(3,1fr)">'
      +'<button class="go2" data-ring="soft" style="background:#555;margin:0">⭕ 淡い</button>'
      +'<button class="go2" data-ring="blue" style="background:#555;margin:0">⭕ 水色</button>'
      +'<button class="go2" data-ring="dark" style="background:#555;margin:0">⭕ 濃いめ</button></div>'
      +'<div class="cap" style="margin:2px 0 8px">同じ色をもう一度押すとリングを外せます</div>'
      +'<div class="cap" style="margin-top:12px">🖼 四角い縁取り線をずらして重ねる（写真の角丸なり・回転しない）</div>'
      +'<div class="__ce_size" style="grid-template-columns:repeat(3,1fr)">'
      +'<button class="go2" data-outline="soft" style="background:#555;margin:0">▢ 淡い</button>'
      +'<button class="go2" data-outline="blue" style="background:#555;margin:0">▢ 水色</button>'
      +'<button class="go2" data-outline="dark" style="background:#555;margin:0">▢ 濃いめ</button></div>'
      +'<button class="go2" id="__ce_outdir" style="background:#888;margin-top:6px">↔ ずらす向きを変える</button>'
      +'<div class="cap" style="margin:2px 0 8px">同じ色をもう一度押すと縁取り線を外せます</div>'
      +'</div>';
    document.body.appendChild(ov);
    ov.addEventListener('click',function(e){
      if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
      var it=e.target.closest('.it');
      if(it){ applyBackdrop(el, GRADS[+it.dataset.i][1]); return; }
      var sb=e.target.closest('button[data-shape]');
      if(sb){ setBackdropShape(el, sb.getAttribute('data-shape')); return; }
      if(e.target.id==='__ce_bgsm'){ setBackdropSize(el, -6); return; }   // 小さく＝はみ出しを減らす
      if(e.target.id==='__ce_bgbg'){ setBackdropSize(el, 6); return; }    // 大きく＝はみ出しを増やす
      if(e.target.id==='__ce_bgrm'){ removeBackdrop(el); ov.remove(); return; }
      var rb=e.target.closest('button[data-ring]');
      if(rb){ toggleRing(el, rb.getAttribute('data-ring')); return; }
      var ob=e.target.closest('button[data-outline]');
      if(ob){ toggleOutline(el, ob.getAttribute('data-outline')); return; }
      if(e.target.id==='__ce_outdir'){ flipOutlineDir(el); return; }
    });
  }
  // 🖼 写真を白フチで囲む（ポラロイド/カード風・AIなし・即反映）。右上だけ角丸を大きめに。
  //   borderで白い台紙を作るので余計なラッパーは足さない。もう一度押すと外す。
  function toggleWhiteFrame(el){
    if(!el){ msg.textContent='対象がありません'; return; }
    var t = el.tagName==='IMG' ? el : ((el.querySelector && el.querySelector('img')) || el);
    if(t.getAttribute('data-ceframe')){
      t.removeAttribute('data-ceframe');
      ['background','border','border-radius','box-shadow','box-sizing'].forEach(function(p){t.style.removeProperty(p);});
      markDirty(); msg.textContent='白フチを外しました（保存で確定）'; return;
    }
    t.setAttribute('data-ceframe','1');
    t.style.setProperty('background','#fff','important');
    t.style.setProperty('border','14px solid #fff','important');
    t.style.setProperty('border-radius','8px 44px 8px 8px','important');   // 右上だけ角丸を大きめに
    t.style.setProperty('box-shadow','0 12px 30px rgba(0,0,0,.15)','important');
    t.style.setProperty('box-sizing','border-box','important');
    markDirty();
    msg.textContent='写真を白フチで囲みました（右上だけ角丸大きめ・保存で確定／もう一度押すと外す）';
  }
  // 💬 はみ出しキャプションカード：写真の角に、白いカードが少し外側へはみ出して重なる演出（AIなし・無料）。
  //   bgTarget()で親を決め、そこに追加するのでラッパーdivは増やさない。文字はプレースホルダを置き、
  //   中身の編集は既存の「✏文字を編集」、位置調整は既存のドラッグに任せる（もう一度押すと外せる）。
  function addOverlapCaption(el){
    var target=bgTarget(el);
    if(!target){ msg.textContent='ここには追加できません（すぐ外側の箱を右クリックしてください）'; return; }
    var card=target.querySelector(':scope > .ce_capcard');
    if(card){ card.remove(); markDirty(); msg.textContent='はみ出しキャプションカードを外しました（保存で確定）'; return; }
    if(getComputedStyle(target).position==='static') target.style.setProperty('position','relative');
    target.style.setProperty('overflow','visible','important');
    card=document.createElement('div'); card.className='ce_capcard';
    card.style.cssText='position:absolute;right:-24px;bottom:-22px;z-index:2;max-width:64%;background:#fff;'
      +'border-radius:16px;padding:14px 18px;box-shadow:0 12px 30px rgba(0,0,0,.16);font-family:inherit;';
    card.innerHTML='<span style="display:inline-block;background:#eef3ff;color:#1a5fd6;font-size:11px;font-weight:700;'
      +'border-radius:999px;padding:3px 10px;margin-bottom:6px">私たち</span>'
      +'<p style="margin:0;font-size:14px;font-weight:700;line-height:1.5;color:#1d1d1f">ひとこと・キャッチコピー</p>'
      +'<p style="margin:4px 0 0;font-size:11.5px;color:#888">署名・補足</p>';
    target.appendChild(card);
    markDirty();
    msg.textContent='はみ出しキャプションカードを付けました。文字は「✏文字を編集」、位置はドラッグで調整→保存で確定（もう一度押すと外せる）';
  }
  // 🖼 写真を加工：白フチ／はみ出しカード／背景の飾り／背景に設定／水彩(AI) の入口をまとめた1つのボタン用ピッカー。
  //   ボタンを増やさず、選択肢はこの中の一覧から選ぶ形にする。
  function openPhotoDecoPicker(el, imgEl, sIdx){
    if(!el){ msg.textContent='対象がありません'; return; }
    var ov=document.createElement('div'); ov.id='__ce_pk';
    ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span><h4>🖼 写真を加工（基本はAIなし・無料）</h4>'
      +'<button class="go2" id="__ce_pdframe" style="background:#1a7f37;margin-bottom:8px">🖼 白フチで囲む（ポラロイド風）</button>'
      +'<button class="go2" id="__ce_pdcap" style="background:#0b6bcb;margin-bottom:8px">💬 はみ出しキャプションカードを付ける</button>'
      +'<button class="go2" id="__ce_pdgrad" style="background:#c026a6;margin-bottom:8px">🌸 背景の飾り（グラデ）を敷く</button>'
      +'<button class="go2" id="__ce_pdsetbg" style="background:#0b6bcb;margin-bottom:8px">🖼 画像を背景に設定</button>'
      +(imgEl?'<button class="go2" id="__ce_pdwater" style="background:#c026a6">🎨 背後に水彩画像を敷く（AI・数十円）</button>':'')
      +'</div>';
    document.body.appendChild(ov);
    ov.addEventListener('click',function(e){
      if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
      if(e.target.id==='__ce_pdframe'){ ov.remove(); toggleWhiteFrame(el); return; }
      if(e.target.id==='__ce_pdcap'){ ov.remove(); addOverlapCaption(el); return; }
      if(e.target.id==='__ce_pdgrad'){ ov.remove(); openGradPicker(el); return; }
      if(e.target.id==='__ce_pdsetbg'){ ov.remove(); openPicker({el:el, type:'bg', fresh:true}); return; }
      if(e.target.id==='__ce_pdwater'){ ov.remove(); openBgPicker(imgEl, sIdx); return; }
    });
  }
  // ===== 右クリックで、その要素に直接アニメ/指示/改善案を出す =====
  var curMenu=null, curEl=null, lastMenuPos=null;  // lastMenuPos=前回ドラッグで動かした位置を記憶
  try{ lastMenuPos=JSON.parse(localStorage.getItem('__ce_menupos')||'null'); }catch(_){}  // 再読込しても覚える
  function closeMenu(){ if(curMenu){curMenu.remove();curMenu=null;} if(curEl){ stopAnim(curEl); clearPreviewStyle(curEl); curEl.classList.remove('__ce_sel');curEl=null;} curAnim=null; curP={};
    // メニューを閉じたらドラッグ移動モードも解除＝文字をドラッグで「選択」できるようにする（部分色付けと両立）
    if(typeof dragEl!=='undefined' && dragEl){ try{ dragEl.removeEventListener('mousedown',_dDown,true); dragEl.style.cursor=''; }catch(_){} dragEl=null; } }
  // 要素を丸ごと消す（AIなし・即反映）。ドラッグ中なら解除してから消す。保存するまでは確定しない。
  function removeEl(el){
    if(!el||el===document.body){ msg.textContent='これは消せません'; return; }
    if(!confirm('この要素を消しますか？\\n\\n「'+descEl(el)+'」\\n※保存すると元に戻せません（保存前ならページ再読込でキャンセルできます）')) return;
    if(dragEl===el){ dragEl.removeEventListener('mousedown',_dDown,true); dragEl=null; }
    closeMenu();
    el.remove();
    markDirty();
    msg.textContent='要素を消しました（保存で確定）';
  }
  // 背景・枠・影を消す（AIなし）。透過画像の下から出てきた「箱」の装飾を一発で消す用。
  function stripDeco(el){
    if(!el){ msg.textContent='対象がありません'; return; }
    el.style.setProperty('background','transparent','important');
    el.style.setProperty('background-image','none','important');
    el.style.setProperty('box-shadow','none','important');
    el.style.setProperty('border','none','important');
    closeMenu(); markDirty();
    msg.textContent='背景・枠・影を消しました（保存で確定）。まだ枠が残るなら、少し外側の箱を右クリックして同じ操作を';
  }
  // rgb(...)/rgba(...) を #rrggbb に変換（カラーピッカーの初期値用）
  function _rgbToHex(c){
    var m=(c||'').match(/\\d+/g); if(!m||m.length<3) return '#000000';
    return '#'+m.slice(0,3).map(function(x){return ('0'+(+x).toString(16)).slice(-2);}).join('');
  }
  // 文字の大きさを倍率で変える（factor=0でリセット）。歪まないよう font-size を直接いじる。
  function _fontSize(el, factor){
    if(!factor){ el.style.removeProperty('font-size'); markDirty(); return; }
    var cur=parseFloat(getComputedStyle(el).fontSize)||16;
    el.style.setProperty('font-size', (cur*factor).toFixed(1)+'px', 'important');
    markDirty();
  }
  // 行間（ラインハイト）を変える。delta=0.15で広く/-0.15で狭く/reset=trueで元に戻す。単位なし比率で入れる。
  function _lineHeight(el, delta, reset){
    if(reset){ el.style.removeProperty('line-height'); markDirty(); return; }
    var cs=getComputedStyle(el), fs=parseFloat(cs.fontSize)||16, lh=parseFloat(cs.lineHeight);
    var cur=isNaN(lh)?1.5:(lh/fs);          // 今の行間を「文字サイズの何倍か」で取得
    var next=Math.max(0.8, cur+delta);      // 詰めすぎ防止に下限0.8
    el.style.setProperty('line-height', next.toFixed(2), 'important');
    markDirty();
  }
  // 〰 点線の下線（手描き風の演出）。もう一度押すと外す。色は指定した色で塗る。
  function toggleUnderlineDots(el, color){
    if(el.getAttribute('data-ceudot')){
      el.removeAttribute('data-ceudot');
      el.style.removeProperty('border-bottom');
      el.style.removeProperty('padding-bottom');
      markDirty(); msg.textContent='点線の下線を外しました（保存で確定）'; return;
    }
    el.setAttribute('data-ceudot','1');
    el.style.setProperty('border-bottom','3px dotted '+color,'important');
    el.style.setProperty('padding-bottom','0.15em','important');
    markDirty();
    msg.textContent='点線の下線をつけました（保存で確定／もう一度押すと外す）';
  }
  // 📜 縦書き（writing-mode）。もう一度押すと横書きに戻す。
  function toggleVertical(el){
    if(el.getAttribute('data-cevert')){
      el.removeAttribute('data-cevert');
      el.style.removeProperty('writing-mode');
      el.style.removeProperty('text-orientation');
      markDirty(); msg.textContent='横書きに戻しました（保存で確定）'; return;
    }
    el.setAttribute('data-cevert','1');
    el.style.setProperty('writing-mode','vertical-rl','important');
    el.style.setProperty('text-orientation','upright','important');
    markDirty();
    msg.textContent='縦書きにしました（保存で確定／もう一度押すと戻す）';
  }
  // ✏ 文字を編集：改行・大きさ・フォント・色をこの1枠でまとめて変える（すべてAIなし・即反映）。
  function openBreakEditor(el){
    if(!el){ msg.textContent='対象の要素がありません'; return; }
    var cur=(el.innerText||el.textContent||'').replace(/\\u200b/g,'');
    var FONTS=[
      ['','（フォントはそのまま）'],
      ["'Yu Gothic','Hiragino Kaku Gothic ProN',Meiryo,sans-serif",'ゴシック（標準）'],
      ["'Yu Mincho','Hiragino Mincho ProN',serif",'明朝（上品）'],
      ["'Hiragino Maru Gothic ProN','Rounded Mplus 1c',sans-serif",'丸ゴシック（やわらか）'],
      ["Georgia,'Times New Roman',serif",'英字セリフ'],
      ["Helvetica,Arial,sans-serif",'英字サンセリフ'],
      ["'Courier New',monospace",'等幅（コード風）']
    ];
    var opts=FONTS.map(function(f){return '<option value="'+f[0].replace(/"/g,'&quot;')+'">'+f[1]+'</option>';}).join('');
    var ov=document.createElement('div'); ov.id='__ce_pk';
    ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span><h4>✏ 文字を編集（AIなし・即反映）</h4>'
      +'<div style="font-size:12px;color:#888;margin-bottom:8px">改行したい所で Enter を押して「改行を反映」。大きさ・フォント・色はその場で反映します。※1文字ずつの動きは外れます</div>'
      +'<textarea id="__ce_brta" style="width:100%;height:120px;font-size:15px;padding:10px;border:1px solid #d0d0d5;border-radius:8px;font-family:inherit;resize:vertical;box-sizing:border-box"></textarea>'
      +'<button class="go2" id="__ce_brapply" style="background:#1a7f37;margin-top:8px">✅ 改行を反映</button>'
      +'<div style="border-top:1px solid #eee;margin:14px 0 0"></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:12px 0 6px">🔡 文字の大きさ</div>'
      +'<div style="display:flex;gap:6px"><button class="go2" data-fs="1.1" style="background:#0b6bcb;margin:0;flex:1">＋ 大きく</button><button class="go2" data-fs="0.9" style="background:#0b6bcb;margin:0;flex:1">－ 小さく</button><button class="go2" data-fs="0" style="background:#888;margin:0">⟲</button></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">↕ 行間（ラインハイト）</div>'
      +'<div style="display:flex;gap:6px"><button class="go2" data-lh="0.15" style="background:#0b6bcb;margin:0;flex:1">＋ 広く</button><button class="go2" data-lh="-0.15" style="background:#0b6bcb;margin:0;flex:1">－ 狭く</button><button class="go2" data-lhr="1" style="background:#888;margin:0">⟲</button></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">🅰 フォント</div>'
      +'<select id="__ce_brff" style="width:100%;font-size:13px;padding:9px;border:1px solid #d0d0d5;border-radius:8px;font-family:inherit">'+opts+'</select>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">🎨 文字の色</div>'
      +'<div style="display:flex;gap:8px;align-items:center"><input type="color" id="__ce_brcol" style="width:54px;height:38px;padding:2px;border:1px solid #d0d0d5;border-radius:8px;cursor:pointer"><button class="go2" id="__ce_brcolr" style="background:#888;margin:0;flex:1">⟲ 色を元に戻す</button></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">〰 点線の下線（手描き風の演出）</div>'
      +'<div style="display:flex;gap:8px;align-items:center"><input type="color" id="__ce_brudot" style="width:54px;height:38px;padding:2px;border:1px solid #d0d0d5;border-radius:8px;cursor:pointer"><button class="go2" id="__ce_brudotb" style="background:#0b6bcb;margin:0;flex:1">〰 下線をつける／外す</button></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">📜 縦書き</div>'
      +'<button class="go2" id="__ce_brvert" style="background:#0b6bcb">📜 縦書きにする／戻す</button>'
      +'</div>';
    document.body.appendChild(ov);
    var ta=document.getElementById('__ce_brta'); ta.value=cur; ta.focus();
    try{ document.getElementById('__ce_brcol').value=_rgbToHex(getComputedStyle(el).color); }catch(_){}
    try{ document.getElementById('__ce_brudot').value=_rgbToHex(getComputedStyle(el).color); }catch(_){}
    ov.addEventListener('click',function(e){
      if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
      if(e.target.id==='__ce_brapply'){
        stopAnim(el);
        el.innerHTML=esc(ta.value).replace(/\\n/g,'<br>');
        markDirty();
        msg.textContent='改行を反映しました。「💾 保存」で確定できます';
        return;
      }
      var fsb=e.target.closest('button[data-fs]');
      if(fsb){ _fontSize(el, +fsb.getAttribute('data-fs')); return; }
      var lhb=e.target.closest('button[data-lh]');
      if(lhb){ _lineHeight(el, +lhb.getAttribute('data-lh')); return; }
      if(e.target.closest('button[data-lhr]')){ _lineHeight(el, 0, true); return; }
      if(e.target.id==='__ce_brcolr'){ el.style.removeProperty('color'); el.style.removeProperty('-webkit-text-fill-color'); markDirty(); return; }
      if(e.target.id==='__ce_brudotb'){ toggleUnderlineDots(el, document.getElementById('__ce_brudot').value); return; }
      if(e.target.id==='__ce_brvert'){ toggleVertical(el); return; }
    });
    document.getElementById('__ce_brff').addEventListener('change',function(){
      if(this.value) el.style.setProperty('font-family', this.value, 'important');
      else el.style.removeProperty('font-family');
      markDirty();
    });
    document.getElementById('__ce_brcol').addEventListener('input',function(){
      // color だけだと、グラデ文字(-webkit-text-fill-color:transparent)や1文字アニメで「透明のまま＝黒/消える」になる。
      // text-fill-color も同じ色で上書きし、子の文字span(fxa_ch)にも直接当てて確実に色を出す。
      el.style.setProperty('color', this.value, 'important');
      el.style.setProperty('-webkit-text-fill-color', this.value, 'important');
      var v=this.value;
      [].forEach.call(el.querySelectorAll('.fxa_ch,.imp-char'), function(sp){ sp.style.setProperty('color', v, 'important'); sp.style.setProperty('-webkit-text-fill-color', v, 'important'); });
      markDirty();
    });
  }
  // その要素1つだけをAIで直す（他は一切触らない）。結果をDOMでその要素だけ差し替える。
  function editElement(el, instruction){
    if(!el){ msg.textContent='対象の要素がありません'; return; }
    var target=el;
    busy(true); showToast('この要素だけAIが直しています…（十数秒）');
    fetch('/api/edit_element',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({html:target.outerHTML, css:headCss(), instruction:instruction})})
    .then(function(r){return r.json();}).then(function(d){
      busy(false); hideToast();
      if(!d.ok){ msg.textContent='失敗：'+(d.message||''); return; }
      try{ target.outerHTML=d.html; }catch(_){ msg.textContent='反映に失敗しました'; return; }
      closeMenu(); markDirty();
      msg.textContent='✅ 直しました。残すには『💾 変更を保存』を押してください（押さずに更新すると消えます）';
    }).catch(function(){ busy(false); hideToast(); msg.textContent='通信エラー'; });
  }
  // 🖍マーカー(.fxa_hl)の帯を今すぐ再生し直す（太さ/速さを変えた直後にプレビューさせるため）。
  // ★classの付け外し＋CSS transitionでの再生はこの環境で不安定だったため、
  //   FX_RUNのsweepHl（rAFで--hlwを毎フレーム手動更新）を直接呼ぶ方式にした。
  function fxHlReplay(span){
    if(!span) return;
    span.classList.remove('fxa_in'); span.style.setProperty('--hlw',0);
    if(typeof ensureFxAssets==='function') ensureFxAssets();
    if(window.__fxaSweepHl) window.__fxaSweepHl(span); else span.classList.add('fxa_in');
  }
  // 帯の太さ（--hlt0/--hlt1・中心は固定でそこから上下に広げ縮め）。delta%pt単位、8〜70%の範囲。
  function fxHlThick(span, delta){
    if(!span){ if(msg) msg.textContent='先にマーカーを引いてください'; return; }
    var t0=parseFloat(span.style.getPropertyValue('--hlt0'))||70;
    var t1=parseFloat(span.style.getPropertyValue('--hlt1'))||92;
    var center=(t0+t1)/2, th=Math.max(8,Math.min(70,(t1-t0)+delta));
    span.style.setProperty('--hlt0',(center-th/2)+'%'); span.style.setProperty('--hlt1',(center+th/2)+'%');
    fxHlReplay(span); markDirty();
    if(msg) msg.textContent='マーカーの太さを変えました（保存で確定）';
  }
  // 伸びる速さ（--hldur・秒）。delta秒、0.2〜2.5秒の範囲。
  function fxHlSpeed(span, delta){
    if(!span){ if(msg) msg.textContent='先にマーカーを引いてください'; return; }
    var d=parseFloat(span.style.getPropertyValue('--hldur'))||0.45;
    d=Math.max(0.2,Math.min(2.5,+(d+delta).toFixed(2)));
    span.style.setProperty('--hldur', d+'s');
    fxHlReplay(span); markDirty();
    if(msg) msg.textContent='マーカーが伸びる速さを変えました（保存で確定）';
  }
  // 帯の上下位置（--hlt0/--hlt1をまとめてずらす。太さは維持）。delta%pt単位、0〜100%からはみ出ないように収める。
  function fxHlPos(span, delta){
    if(!span){ if(msg) msg.textContent='先にマーカーを引いてください'; return; }
    var t0=parseFloat(span.style.getPropertyValue('--hlt0'))||70;
    var t1=parseFloat(span.style.getPropertyValue('--hlt1'))||92;
    var th=t1-t0;
    var nt0=Math.max(0,Math.min(100-th,t0+delta));
    span.style.setProperty('--hlt0',nt0+'%'); span.style.setProperty('--hlt1',(nt0+th)+'%');
    fxHlReplay(span); markDirty();
    if(msg) msg.textContent='マーカーの位置を変えました（保存で確定）';
  }
  // マーカー色の履歴：使った色を新しい順に覚えておく（Excelの「最近使用した色」と同じ発想）。
  //   ・直前に使った色がそのまま次回のデフォルトになる
  //   ・過去に使った色は小さいスウォッチで並べて、クリック1つで選び直せる
  var HL_COLOR_MAX=10;
  function hlColorHistory(){ try{ return JSON.parse(localStorage.getItem('__ce_hlcolors')||'[]')||[]; }catch(_){ return []; } }
  function hlPushColorHistory(c){
    if(!c) return;
    var list=hlColorHistory().filter(function(x){ return x.toLowerCase()!==c.toLowerCase(); });
    list.unshift(c);
    if(list.length>HL_COLOR_MAX) list=list.slice(0,HL_COLOR_MAX);
    try{ localStorage.setItem('__ce_hlcolors', JSON.stringify(list)); }catch(_){}
  }
  function hlDefaultColor(){ var h=hlColorHistory(); return h.length?h[0]:'#ffe66d'; }
  function hlSwatchesHtml(){
    return hlColorHistory().map(function(c){
      return '<button class="__ce_hlsw" data-c="'+c+'" title="'+c+'" style="width:17px;height:17px;padding:0;border:1px solid rgba(128,128,128,.55);border-radius:4px;cursor:pointer;background:'+c+'"></button>';
    }).join('');
  }
  // スウォッチ（過去に使った色）をクリックしたら、その色を選び直せるようにする（選択ポップアップ／右クリックメニュー共通）。
  function hlBindSwatches(swatchWrap, colorInput, onPick){
    if(!swatchWrap) return;
    [].slice.call(swatchWrap.querySelectorAll('.__ce_hlsw')).forEach(function(btn){
      btn.addEventListener('click', function(){
        var c=this.getAttribute('data-c');
        colorInput.value=c; hlPushColorHistory(c);
        swatchWrap.innerHTML=hlSwatchesHtml(); hlBindSwatches(swatchWrap, colorInput, onPick);
        if(onPick) onPick(c);
      });
    });
  }
  // ===== 文章の一部だけ色を変える：ドラッグで文字を選ぶ→小さな色ボタンが出る（AIなし）=====
  (function(){
    var pop=null, curSpan=null, savedRange=null, curHl=null, curUdot=null;
    function hidePop(){ if(pop){ pop.remove(); pop=null; } curSpan=null; savedRange=null; curHl=null; curUdot=null; }
    function inUI(node){ var el=node&&(node.nodeType===1?node:node.parentElement); return el&&el.closest&&(el.closest('#__ce')||el.closest('#__ce_cm')||el.closest('#__ce_pk')||el.closest('#__ce_selc')||el.closest('#__ce_toast')); }
    // 選んだ範囲に色を当てる。中に色付きの子span（1文字ずつの.fxa_ch等の!important）があると
    // 囲むだけでは負けるので、子孫の色も全部この色で上書きする。
    function _forceColor(root, color){
      root.style.setProperty('color',color,'important'); root.style.setProperty('-webkit-text-fill-color',color,'important');
      [].forEach.call(root.querySelectorAll('*'), function(sp){ sp.style.setProperty('color',color,'important'); sp.style.setProperty('-webkit-text-fill-color',color,'important'); });
    }
    function paint(color){
      if(curSpan){ _forceColor(curSpan,color); markDirty(); return; }
      if(!savedRange) return;
      try{
        var span=document.createElement('span');
        try{ savedRange.surroundContents(span); }
        catch(_){ var frag=savedRange.extractContents(); span.appendChild(frag); savedRange.insertNode(span); }
        _forceColor(span,color);
        curSpan=span; markDirty();
        if(msg) msg.textContent='選んだ文字だけ色を変えました（保存で確定）';
      }catch(err){ if(msg) msg.textContent='この範囲は色を変えられませんでした（別々の要素にまたがっています）'; }
    }
    // 🖍 蛍光ペン：選択文字を .fxa_hl で囲む→スクロールで線が伸びる（保存版でも自動再生）。
    function highlight(color){
      hlPushColorHistory(color);
      if(curHl){ curHl.style.setProperty('--hlc',color); markDirty(); return; }
      if(!savedRange) return;
      try{
        var span=document.createElement('span'); span.className='fxa_hl';
        span.style.setProperty('--hlc',color);
        try{ savedRange.surroundContents(span); }
        catch(_){ var frag=savedRange.extractContents(); span.appendChild(frag); savedRange.insertNode(span); }
        if(typeof ensureFxAssets==='function') ensureFxAssets();  // アニメCSS/監視JSを注入（保存版で動く）
        if(window.__fxaSweepHl) window.__fxaSweepHl(span); else span.classList.add('fxa_in');  // 今すぐ線を引く（プレビュー）
        curHl=span; markDirty();
        if(msg) msg.textContent='マーカーを引きました（スクロールで線がスーッと伸びます・保存で確定）';
      }catch(err){ if(msg) msg.textContent='この範囲はマーカーを引けませんでした（別々の要素にまたがっています）'; }
    }
    // 〰 点線の下線：選択文字だけをspanで囲んでborder-bottom:dottedを当てる（AIなし・即反映）。
    function underline(color){
      if(curUdot){ curUdot.style.setProperty('border-bottom-color',color,'important'); markDirty(); return; }
      if(!savedRange) return;
      try{
        var span=document.createElement('span'); span.className='ceud';
        span.style.setProperty('border-bottom','3px dotted '+color,'important');
        span.style.setProperty('padding-bottom','0.15em','important');
        try{ savedRange.surroundContents(span); }
        catch(_){ var frag=savedRange.extractContents(); span.appendChild(frag); savedRange.insertNode(span); }
        curUdot=span; markDirty();
        if(msg) msg.textContent='選んだ文字に点線の下線をつけました（保存で確定）';
      }catch(err){ if(msg) msg.textContent='この範囲には下線をつけられませんでした（別々の要素にまたがっています）'; }
    }
    // 装飾span（マーカー/下線）を1つ、中身の文字はそのまま残して剥がす（AIなし・即反映）。
    function _unwrap(span){
      if(!span||!span.parentNode) return;
      var parent=span.parentNode;
      while(span.firstChild) parent.insertBefore(span.firstChild, span);
      parent.removeChild(span);
      markDirty();
    }
    function removeHl(){
      if(!curHl){ if(msg) msg.textContent='ここにはマーカーがありません'; return; }
      _unwrap(curHl); curHl=null; hidePop();
      if(msg) msg.textContent='マーカーを消しました（保存で確定）';
    }
    function removeUnderline(){
      if(!curUdot){ if(msg) msg.textContent='ここには下線がありません'; return; }
      _unwrap(curUdot); curUdot=null; hidePop();
      if(msg) msg.textContent='点線の下線を消しました（保存で確定）';
    }
    document.addEventListener('mouseup', function(e){
      if(inUI(e.target)) return;  // 編集UI上の操作は無視
      setTimeout(function(){
        var sel=window.getSelection();
        if(!sel||sel.isCollapsed||!sel.rangeCount||!(''+sel).trim()){ hidePop(); return; }
        var rng=sel.getRangeAt(0);
        if(inUI(rng.commonAncestorContainer)){ hidePop(); return; }
        var r=rng.getBoundingClientRect(); if(!r.width&&!r.height){ hidePop(); return; }
        hidePop(); savedRange=rng.cloneRange();
        // 選んだ範囲が既存のマーカー/下線spanの中（またはちょうどそのspan自体）なら、消せる状態にしておく
        var ancEl=rng.commonAncestorContainer; ancEl=(ancEl.nodeType===1?ancEl:ancEl.parentElement);
        if(ancEl && ancEl.closest){
          var exHl=ancEl.closest('.fxa_hl'); if(exHl) curHl=exHl;
          var exUd=ancEl.closest('.ceud'); if(exUd) curUdot=exUd;
        }
        pop=document.createElement('div'); pop.id='__ce_selc';
        pop.style.cssText='position:fixed;z-index:2147483004;background:#1d1d1f;color:#fff;border-radius:10px;padding:6px 9px;box-shadow:0 10px 30px rgba(0,0,0,.42);font-family:system-ui,sans-serif;font-size:12px;display:flex;flex-wrap:wrap;align-items:center;gap:7px;max-width:360px';
        pop.innerHTML='<span style="opacity:.85">文字色</span><input type="color" id="__ce_selcol" style="width:32px;height:26px;padding:0;border:none;border-radius:5px;cursor:pointer">'
          +'<span style="opacity:.45">｜</span><span style="opacity:.85">🖍</span><input type="color" id="__ce_selhlc" value="'+hlDefaultColor()+'" style="width:32px;height:26px;padding:0;border:none;border-radius:5px;cursor:pointer" title="マーカーの色"><span id="__ce_selhlsw" style="display:inline-flex;gap:3px;flex-wrap:wrap;max-width:90px">'+hlSwatchesHtml()+'</span><button id="__ce_selhl" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:3px 9px;cursor:pointer" title="蛍光ペンを引く">'+(curHl?'消す':'引く')+'</button>'
          +(curHl?'<span style="opacity:.85">太さ</span><button id="__ce_selhlthm" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:3px 8px;cursor:pointer">－</button><button id="__ce_selhlthp" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:3px 8px;cursor:pointer">＋</button>'
            +'<span style="opacity:.85">位置</span><button id="__ce_selhlpu" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:3px 8px;cursor:pointer" title="上へ">▲</button><button id="__ce_selhlpd" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:3px 8px;cursor:pointer" title="下へ">▼</button>'
            +'<span style="opacity:.85">速さ</span><button id="__ce_selhldm" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:3px 8px;cursor:pointer" title="遅く（伸びる時間を長く）">🐢</button><button id="__ce_selhldp" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:3px 8px;cursor:pointer" title="速く（伸びる時間を短く）">🐇</button>':'')
          +'<span style="opacity:.45">｜</span><span style="opacity:.85">〰</span><input type="color" id="__ce_seludc" value="#e07856" style="width:32px;height:26px;padding:0;border:none;border-radius:5px;cursor:pointer" title="点線下線の色"><button id="__ce_seludb" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:3px 9px;cursor:pointer" title="点線の下線を引く">'+(curUdot?'消す':'下線')+'</button>'
          +'<button id="__ce_selx" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:2px 8px;cursor:pointer">×</button>';
        document.body.appendChild(pop);
        pop.style.left=Math.max(8,Math.min(r.left, window.innerWidth-370))+'px';
        pop.style.top=Math.max(8, r.top-42)+'px';
        pop.querySelector('#__ce_selx').addEventListener('click', hidePop);
        var thm=pop.querySelector('#__ce_selhlthm'); if(thm) thm.addEventListener('click',function(){ fxHlThick(curHl,-6); });
        var thp=pop.querySelector('#__ce_selhlthp'); if(thp) thp.addEventListener('click',function(){ fxHlThick(curHl,6); });
        var pum=pop.querySelector('#__ce_selhlpu'); if(pum) pum.addEventListener('click',function(){ fxHlPos(curHl,-4); });
        var pud=pop.querySelector('#__ce_selhlpd'); if(pud) pud.addEventListener('click',function(){ fxHlPos(curHl,4); });
        var hdm=pop.querySelector('#__ce_selhldm'); if(hdm) hdm.addEventListener('click',function(){ fxHlSpeed(curHl,0.2); });
        var hdp=pop.querySelector('#__ce_selhldp'); if(hdp) hdp.addEventListener('click',function(){ fxHlSpeed(curHl,-0.2); });
        pop.querySelector('#__ce_selcol').addEventListener('input', function(){ paint(this.value); });
        pop.querySelector('#__ce_selhl').addEventListener('click', function(){
          if(curHl){ removeHl(); return; }
          highlight(pop.querySelector('#__ce_selhlc').value);
          // マーカーを引いた直後：選び直さなくても、その場に太さ/速さボタンを出す
          if(curHl && !pop.querySelector('#__ce_selhlthm')){
            this.textContent='消す';
            this.insertAdjacentHTML('afterend',
              '<span style="opacity:.85">太さ</span><button id="__ce_selhlthm" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:3px 8px;cursor:pointer">－</button><button id="__ce_selhlthp" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:3px 8px;cursor:pointer">＋</button>'
              +'<span style="opacity:.85">位置</span><button id="__ce_selhlpu" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:3px 8px;cursor:pointer" title="上へ">▲</button><button id="__ce_selhlpd" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:3px 8px;cursor:pointer" title="下へ">▼</button>'
              +'<span style="opacity:.85">速さ</span><button id="__ce_selhldm" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:3px 8px;cursor:pointer" title="遅く（伸びる時間を長く）">🐢</button><button id="__ce_selhldp" style="background:rgba(255,255,255,.2);border:none;color:#fff;border-radius:6px;padding:3px 8px;cursor:pointer" title="速く（伸びる時間を短く）">🐇</button>');
            pop.querySelector('#__ce_selhlthm').addEventListener('click',function(){ fxHlThick(curHl,-6); });
            pop.querySelector('#__ce_selhlthp').addEventListener('click',function(){ fxHlThick(curHl,6); });
            pop.querySelector('#__ce_selhlpu').addEventListener('click',function(){ fxHlPos(curHl,-4); });
            pop.querySelector('#__ce_selhlpd').addEventListener('click',function(){ fxHlPos(curHl,4); });
            pop.querySelector('#__ce_selhldm').addEventListener('click',function(){ fxHlSpeed(curHl,0.2); });
            pop.querySelector('#__ce_selhldp').addEventListener('click',function(){ fxHlSpeed(curHl,-0.2); });
          }
        });
        pop.querySelector('#__ce_selhlc').addEventListener('input', function(){ if(curHl) highlight(this.value); });
        hlBindSwatches(pop.querySelector('#__ce_selhlsw'), pop.querySelector('#__ce_selhlc'), function(c){ if(curHl) highlight(c); });
        pop.querySelector('#__ce_seludb').addEventListener('click', function(){ if(curUdot) removeUnderline(); else underline(pop.querySelector('#__ce_seludc').value); });
        pop.querySelector('#__ce_seludc').addEventListener('input', function(){ if(curUdot) underline(this.value); });
      }, 10);
    }, true);
    document.addEventListener('scroll', hidePop, true);
  })();
  // ===== 位置の直接調整（AIなし・transformで即反映）=====
  // 要素を translate で浮かせて動かす。元のtransformは data-cebt に退避して壊さない。
  // 元のtransformを1度だけ退避（自前のtranslate/scaleが無い時だけ）
  // 元の変形(回転など)を1度だけ退避。インラインに無ければ、CSSクラス由来の計算値(matrix=回転含む)を拾う。
  function _cebt(el){
    if(el.getAttribute('data-cebt')!==null) return;
    var t=el.style.transform||'';
    if(t && t.indexOf('translate')<0 && t.indexOf('scale')<0){ el.setAttribute('data-cebt', t); return; }
    var c=''; try{ c=getComputedStyle(el).transform; }catch(_){}
    el.setAttribute('data-cebt', (c && c!=='none') ? c : '');
  }
  // 位置(translate)＋大きさ(scaleX,scaleY)をまとめて当てる。アニメ/トランジションは止めて最優先で反映。
  function applyTf(el){
    var x=+el.getAttribute('data-cetx')||0, y=+el.getAttribute('data-cety')||0;
    var sx=+el.getAttribute('data-cesx')||1, sy=+el.getAttribute('data-cesy')||1;
    var ro=+el.getAttribute('data-cero')||0;
    // 移動/回転/拡大は個別プロパティ(translate/rotate/scale)で当てる。
    // これで transform を出現アニメ用に空けられ、移動とアニメが奪い合わず両立する（消えない・位置も残る）。
    el.style.setProperty('translate', x+'px '+y+'px', 'important');
    el.style.setProperty('rotate', ro+'deg', 'important');
    el.style.setProperty('scale', sx+' '+sy, 'important');
    el.style.setProperty('transform-origin','center','important');
    var cebt=el.getAttribute('data-cebt')||'';
    // 元の変形があればtransformに残す。無ければtransformは必ず消す
    // （消さないとプレビュー等で付いた一時的なtransformが残り、出現アニメを固定してしまう）。
    if(cebt){ el.style.setProperty('transform', cebt, 'important'); } else { el.style.removeProperty('transform'); }
    markDirty();
  }
  // 移動・拡大・回転で位置を動かした要素か？（動かしていたらアニメはラッパーに当てる）
  function isMoved(el){ return !!el && ['data-cetx','data-cety','data-cesx','data-cesy','data-cero'].some(function(a){ return (+el.getAttribute(a))||0; }); }
  // 今の確定変形（移動＋回転＋拡大＋元の変形）をまとめた文字列。アニメの土台に使う。
  function restTf(el){
    var x=+el.getAttribute('data-cetx')||0, y=+el.getAttribute('data-cety')||0;
    var sx=+el.getAttribute('data-cesx')||1, sy=+el.getAttribute('data-cesy')||1, ro=+el.getAttribute('data-cero')||0;
    return 'translate('+x+'px,'+y+'px) rotate('+ro+'deg) scale('+sx+','+sy+') '+(el.getAttribute('data-cebt')||'');
  }
  function setPos(el,x,y){ _cebt(el); el.setAttribute('data-cetx',x); el.setAttribute('data-cety',y); applyTf(el); }
  function nudge(el,dx,dy){ setPos(el,(+el.getAttribute('data-cetx')||0)+dx,(+el.getAttribute('data-cety')||0)+dy); }
  // fx=横倍率, fy=縦倍率（1なら変えない）。横だけ長く/縦だけ長く/等倍を1関数で。
  function scaleBy(el,fx,fy){
    _cebt(el);
    var sx=(+el.getAttribute('data-cesx')||1)*fx, sy=(+el.getAttribute('data-cesy')||1)*fy;
    if(sx<0.2)sx=0.2; if(sx>5)sx=5; if(sy<0.2)sy=0.2; if(sy>5)sy=5;
    el.setAttribute('data-cesx',sx); el.setAttribute('data-cesy',sy); applyTf(el);
  }
  function rotateBy(el,delta){ _cebt(el); el.setAttribute('data-cero',(+el.getAttribute('data-cero')||0)+delta); applyTf(el); }
  // 画像のサイズ変更：transform:scaleだと引き伸ばされて歪む。幅・高さを変え、object-fit:coverで
  // はみ出しは切り取る（縦横比を保ったまま枠を満たす）＝横に長くしても画像が歪まない。
  function sizeImg(el,fx,fy){
    var w=(+el.getAttribute('data-cew'))||el.offsetWidth;
    var h=(+el.getAttribute('data-ceh'))||el.offsetHeight;
    w*=fx; h*=fy;
    if(w<20)w=20; if(h<20)h=20; if(w>4000)w=4000; if(h>4000)h=4000;
    el.setAttribute('data-cew',w); el.setAttribute('data-ceh',h);
    el.style.setProperty('width',Math.round(w)+'px','important');
    el.style.setProperty('height',Math.round(h)+'px','important');
    el.style.setProperty('object-fit','cover','important');
    el.style.setProperty('max-width','none','important');  // 元CSSのmax-width:100%等に負けないように
    markDirty();
  }
  // 縦の高さ(min-height)を増減。scaleと違い中身は歪まず、余白だけ増減する（セクションを高く保つのに最適）。
  function adjustMinH(el,delta){
    if(!el) return;
    var cur=parseFloat(el.style.minHeight); if(!(cur>0)) cur=el.getBoundingClientRect().height||el.offsetHeight||0;
    var h=Math.max(40, cur+delta);
    el.style.setProperty('min-height',Math.round(h)+'px','important');
    markDirty();
  }
  // 横の幅(width)を増減。scaleと違い中身は歪まず、横に広がる/狭まる。
  function adjustWidth(el,delta){
    if(!el) return;
    var cur=parseFloat(el.style.width); if(!(cur>0)) cur=el.getBoundingClientRect().width||el.offsetWidth||0;
    var w=Math.max(40, cur+delta);
    el.style.setProperty('width',Math.round(w)+'px','important');
    el.style.setProperty('max-width','none','important');  // 元CSSのmax-width:100%等に負けないように
    markDirty();
  }
  // ===== 動きプレビュー（RAFで毎フレーム手動描画＝この環境で確実）＋ 無料の焼き込み =====
  var curAnim=null, curP={};  // いまプレビュー中のアニメkと、その調整値
  // アニメごとに「前回いじった調整値」を記憶（再読込しても覚える＝次からのデフォルトにする）
  var _fxLast={}; try{ _fxLast=JSON.parse(localStorage.getItem('__ce_fxlast')||'{}')||{}; }catch(_){ _fxLast={}; }
  function _fxSaveLast(){ try{ localStorage.setItem('__ce_fxlast', JSON.stringify(_fxLast)); }catch(_){} }
  function fxDef(k){ for(var i=0;i<FX.length;i++){ if(FX[i].k===k) return FX[i]; } return null; }
  function fxParam(a,key){ if(curP[key]!=null) return curP[key]; for(var i=0;i<a.sl.length;i++){ if(a.sl[i].k===key) return a.sl[i].def; } return 0; }
  // プレビューで当てた一時styleを消し、確定状態（位置など）に戻す
  function clearPreviewStyle(el){
    if(!el) return;
    ['opacity','filter','clip-path','text-shadow','animation'].forEach(function(p){ el.style.removeProperty(p); });
    // 位置・拡大・回転・退避のどれかが編集されていたら、その確定変形を戻す（拡大だけでも消えないように）
    var edited=['data-cetx','data-cety','data-cesx','data-cesy','data-cero','data-cebt'].some(function(a){ return el.getAttribute(a)!=null; });
    if(edited){ applyTf(el); } else { ['transform','translate','rotate','scale'].forEach(function(p){ el.style.removeProperty(p); }); }
  }
  function stopAnim(el){
    if(el&&el.__ceRAF){ cancelAnimationFrame(el.__ceRAF); el.__ceRAF=null; }
    if(el&&el.__fxHTML!=null){ el.innerHTML=el.__fxHTML; el.__fxHTML=null; }  // 文字分割していたら元に戻す
  }
  // 文字を1つずつ <span class="fxa_ch"> に包む（子タグは壊さない）。戻すのはinnerHTML復元で。
  function splitChars(el){
    var spans=[], texts=[], w=document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null), t;
    while(t=w.nextNode()){
      if(t.parentNode&&t.parentNode.classList&&t.parentNode.classList.contains('fxa_ch')) continue;  // 既に包まれた文字は二重に包まない
      if(t.nodeValue && t.nodeValue.replace(/\\s/g,'').length) texts.push(t);
    }
    texts.forEach(function(tn){
      var frag=document.createDocumentFragment(), s=tn.nodeValue;
      for(var i=0;i<s.length;i++){
        var ch=s[i];
        if(/\\s/.test(ch)){ frag.appendChild(document.createTextNode(ch)); continue; }
        var sp=document.createElement('span'); sp.className='fxa_ch'; sp.textContent=ch; sp.style.display='inline-block';
        spans.push(sp); frag.appendChild(sp);
      }
      tn.parentNode.replaceChild(frag,tn);
    });
    return spans;
  }
  // 既に1文字ずつ包まれていたら、ほどいて元テキストに戻す（2回目以降の焼き込みで入れ子になり壊れるのを防ぐ）
  function fxUnsplit(el){
    if(!el||!el.querySelector) return;
    var sp;
    while(sp=el.querySelector('.fxa_ch')){ sp.replaceWith(document.createTextNode(sp.textContent||'')); }
    try{ el.normalize(); }catch(_){}  // 隣り合うテキストを1つに結合＝元の文字列に戻す
  }
  // 一括改善(GPT)の文字アニメ(imp-char)をこの要素から取り除く。
  // ★これをしないと：一括改善の分割JSがリロード毎に見出しのinnerHTMLを作り直し、私のfxa_chを毎回消す＝アニメが付かない。
  //   data-imp-split を外すと分割JSがこの見出しを対象外にする→私のアニメが残る。imp-char/imp-lineはプレーン文字に戻す。
  function fxStripImpLetters(el){
    if(!el||!el.querySelector) return;
    // 一括改善は見出しごとに"作り直しJS"を持つ（探し方が imp-title / [data-imp-split] 等バラバラで、
    // 正規表現では塞ぎ切れない）。→ 標的になるクラスを外せば、どの作り直しJSもこの見出しを掴めなくなる
    //   ＝私のアニメが残る。見た目は主要なテキストstyleをインライン化して保つ。
    var isTitle=false, cls=el.classList;
    if(cls){ [].slice.call(cls).forEach(function(c){ if(c.indexOf('imp-title')===0) isTitle=true; }); }
    if(isTitle){
      try{ var cs=getComputedStyle(el);
        ['font-size','font-weight','font-family','line-height','letter-spacing','color','text-align','font-style','text-transform','white-space','margin','max-width','writing-mode','text-orientation','height','min-height','padding','align-items','justify-content'].forEach(function(p){
          if(!el.style.getPropertyValue(p)) el.style.setProperty(p, cs.getPropertyValue(p));
        });
      }catch(_){}
      [].slice.call(cls).forEach(function(c){ if(c.indexOf('imp-title')===0) el.classList.remove(c); });
    }
    if(el.removeAttribute) el.removeAttribute('data-imp-split');
    var sp;
    while(sp=el.querySelector('.imp-line,.imp-char')){ sp.replaceWith(document.createTextNode(sp.textContent||'')); }
    try{ el.normalize(); }catch(_){}
  }
  // 文字系プレビュー（stagger/typewriter/wave）
  function playChar(el,a){
    if(el.__fxHTML==null) el.__fxHTML=el.innerHTML;
    fxUnsplit(el);  // 既に割れていても一旦プレーンに戻してから割り直す
    var spans=splitChars(el);
    if(!spans.length){ el.innerHTML=el.__fxHTML; el.__fxHTML=null; if(msg)msg.textContent='⚠ ここには文字が無いので文字アニメは使えません。画像には「ふわっと出現」「ズームイン」「ぼやけて出現」などを選んでください'; return; }
    var stag=fxParam(a,'stag')||45, dur=fxParam(a,'dur')||(a.loop?1600:500), dist=fxParam(a,'dist')||16, amp=fxParam(a,'amp')||10, start=null;
    function frame(ts){
      if(start===null)start=ts; var tt=ts-start;
      for(var i=0;i<spans.length;i++){ var sp=spans[i];
        if(a.loop){ var ph=(tt/dur*2*Math.PI)-(i*0.5); sp.style.transform='translateY('+(Math.sin(ph)*amp)+'px)'; }
        else if(a.type){ sp.style.opacity=(tt>i*stag)?1:0; }
        else { var lt=tt-i*stag, q=lt<=0?0:Math.min(1,lt/dur); q=q<.5?2*q*q:1-Math.pow(-2*q+2,2)/2; sp.style.opacity=q; sp.style.transform='translateY('+(dist*(1-q))+'px)'; }
      }
      var done=a.loop?false:(tt>spans.length*stag+(a.type?80:dur+20));
      if(!done){ el.__ceRAF=requestAnimationFrame(frame); }
      else { el.__ceRAF=null; el.innerHTML=el.__fxHTML; el.__fxHTML=null; }
    }
    el.__ceRAF=requestAnimationFrame(frame);
  }
  function playAnim(el,k){
    if(!el){ if(msg)msg.textContent='⚠ 要素が選ばれていません（もう一度右クリックで選んでください）'; return; }
    var a=fxDef(k); if(!a){ if(msg)msg.textContent='⚠ 未対応の動き：'+k; return; }
    stopAnim(el);
    el.style.setProperty('animation','none','important');  // プレビュー中は要素自身のCSSアニメを止める（RAFのtransformが上書きされないように）
    var base=el.getAttribute('data-cebt')||'';  // 元の変形(回転など)は保つ。移動はtranslate個別プロパティ側に乗るのでここには含めない
    if(msg) msg.textContent='▶ 再生「'+a.b+'」（スライダーで調整→「付ける」で確定）';
    if(a.g==='char'){ playChar(el,a); return; }
    var dur=fxParam(a,'dur')||800, start=null;  // a.dは説明文なので使わない（速さスライダー無しは800msに）
    function frame(ts){
      if(start===null) start=ts;
      var p=(ts-start)/dur, o=1, tf='';
      if(a.g==='loop'){
        var pp=p%1, tri=pp<.5?pp*2:2-pp*2;  // 0→1→0
        if(a.dir==='ps'){ tf='scale('+(1+(fxParam(a,'amp')/100)*tri)+')'; }
        else if(a.dir==='fy'){ tf='translateY('+(-fxParam(a,'amp')*tri)+'px)'; }
        else if(a.dir==='by'){ var bp=pp<.3?pp/.3:(pp<.6?1-(pp-.3)/.3:(pp<.8?(pp-.6)/.2*.4:(1-(pp-.8)/.2)*.4)); tf='translateY('+(-fxParam(a,'amp')*Math.max(0,bp))+'px)'; }
        else if(a.glow){ var g=Math.round(4+18*tri); el.style.setProperty('text-shadow','0 0 '+g+'px currentColor'+(tri>.35?(',0 0 '+Math.round(g*1.7)+'px currentColor'):''),'important'); el.style.setProperty('filter','brightness('+(1+.18*tri)+')','important'); }
      } else {
        var q=Math.min(1,p); q=q<.5?2*q*q:1-Math.pow(-2*q+2,2)/2;  // easeInOut
        o=q;
        if(a.dir==='y'){ tf='translateY('+(fxParam(a,'dist')*(1-q))+'px)'; }
        else if(a.dir==='xl'){ tf='translateX('+(-fxParam(a,'dist')*(1-q))+'px)'; }
        else if(a.dir==='xr'){ tf='translateX('+(fxParam(a,'dist')*(1-q))+'px)'; }
        else if(a.dir==='s'){ var sc=fxParam(a,'scale')/100; tf='scale('+(sc+(1-sc)*q)+')'; }
        else if(a.dir==='bl'){ el.style.setProperty('filter','blur('+(fxParam(a,'blur')*(1-q))+'px)','important'); }
        else if(a.dir==='ry'){ tf='perspective(800px) rotateY('+(fxParam(a,'deg')*(1-q))+'deg)'; }
        else if(a.dir==='clip'){ tf='translateY('+(fxParam(a,'dist')*(1-q))+'px)'; }  // せり上がり＝下からスッと上へ＋フェード（clip-pathは使わない＝半分で止まらない）
      }
      if(!a.glow){ el.style.setProperty('opacity',o,'important'); }
      if(tf) el.style.setProperty('transform',tf+' '+base,'important');
      if(a.g==='loop' || p<1){ el.__ceRAF=requestAnimationFrame(frame); }
      else { el.__ceRAF=null; clearPreviewStyle(el); }
    }
    el.__ceRAF=requestAnimationFrame(frame);
  }
  // ===== 焼き込み（AIなし・無料）：永続CSS/JSをHTMLへ注入し、要素にクラス＋CSS変数を付ける =====
  // 命名は "fxa" 系（#__ce を含めない）＝保存時の掃除(cleanHtml)で消されず、そのまま残る。
  var FX_CSS='html.fxa-on .fxa_pre{opacity:0;transition:opacity var(--fxa-dur,.8s) ease,transform var(--fxa-dur,.8s) ease,filter var(--fxa-dur,.8s) ease,clip-path var(--fxa-dur,.8s) ease}'
    +'html.fxa-on .fxa_pre.fxa_y{transform:translateY(var(--fxa-dist,28px))}'
    +'html.fxa-on .fxa_pre.fxa_xl{transform:translateX(calc(-1*var(--fxa-dist,48px)))}'
    +'html.fxa-on .fxa_pre.fxa_xr{transform:translateX(var(--fxa-dist,48px))}'
    +'html.fxa-on .fxa_pre.fxa_s{transform:scale(var(--fxa-scale,.86))}'
    +'html.fxa-on .fxa_pre.fxa_bl{filter:blur(var(--fxa-blur,14px))}'
    +'html.fxa-on .fxa_pre.fxa_ry{transform:perspective(800px) rotateY(var(--fxa-deg,90deg))}'
    +'html.fxa-on .fxa_pre.fxa_clip{transform:translateY(var(--fxa-dist,40px))}'
    +'html.fxa-on .fxa_pre.fxa_in{opacity:1!important;transform:none!important;filter:none!important;clip-path:inset(0 0 0 0)!important}'
    +'html.fxa-on .fxa_pre.fxa_cpre,html.fxa-on .fxa_pre.fxa_tw{opacity:1;transform:none;transition:none}'
    +'.fxa_ch{display:inline-block}'
    +'html.fxa-on .fxa_cpre .fxa_ch{opacity:0;transform:translateY(var(--fxa-dist,26px));transition:opacity var(--fxa-dur,.34s) cubic-bezier(.34,1.56,.64,1),transform var(--fxa-dur,.34s) cubic-bezier(.34,1.56,.64,1)}'
    +'html.fxa-on .fxa_cpre.fxa_in .fxa_ch{opacity:1;transform:none;transition-delay:calc(var(--i,0)*var(--fxa-stag,32ms))}'
    +'html.fxa-on .fxa_tw .fxa_ch{opacity:0;transform:translateY(10px) scale(.9);transition:opacity .18s ease,transform .18s ease}'
    +'html.fxa-on .fxa_tw.fxa_in .fxa_ch{opacity:1;transform:none;transition-delay:calc(var(--i,0)*var(--fxa-stag,60ms))}'
    +'@keyframes fxa_pulse{0%,100%{transform:scale(1)}50%{transform:scale(calc(1 + var(--fxa-amp,.06)))}}'
    +'@keyframes fxa_float{0%,100%{transform:translateY(0)}50%{transform:translateY(calc(-1*var(--fxa-amp,12px)))}}'
    +'@keyframes fxa_bounce{0%,100%{transform:translateY(0)}30%{transform:translateY(calc(-1*var(--fxa-amp,18px)))}60%{transform:translateY(0)}80%{transform:translateY(calc(-.4*var(--fxa-amp,18px)))}}'
    +'@keyframes fxa_glow{0%,100%{text-shadow:0 0 4px currentColor;filter:brightness(1)}50%{text-shadow:0 0 16px currentColor,0 0 30px currentColor;filter:brightness(1.16)}}'
    +'@keyframes fxa_wave{0%,100%{transform:translateY(0)}50%{transform:translateY(calc(-1*var(--fxa-amp,10px)))}}'
    +'.fxa_lp_pulse{animation:fxa_pulse var(--fxa-dur,1.4s) ease-in-out infinite}'
    +'.fxa_lp_float{animation:fxa_float var(--fxa-dur,2.2s) ease-in-out infinite}'
    +'.fxa_lp_bounce{animation:fxa_bounce var(--fxa-dur,1.2s) ease infinite}'
    +'.fxa_lp_glow{animation:fxa_glow var(--fxa-dur,1.8s) ease-in-out infinite}'
    +'.fxa_wave .fxa_ch{animation:fxa_wave var(--fxa-dur,1.6s) ease-in-out infinite;animation-delay:calc(var(--i,0)*90ms)}'
    // 🖍 蛍光ペン：文字の下側だけ帯状に塗り、スクロールで左→右にスーッと伸びる。
    // ★CSSのtransitionで付け外しして「もう一度再生」させるのはこの環境で不安定だったため、
    //   幅は--hlw（0〜100の数値）で持ち、rAFで毎フレーム手動更新する方式にした（FX_RUNのsweepHl）。
    // 太さ(--hlt0/--hlt1)と速さ(--hldur)は要素ごとのinlineで上書きできる（fxHlThick/fxHlSpeedが設定）。
    // 太さのデフォルトは文字の下寄り（70%〜92%）＝下線に近い位置に調整済み。
    +'.fxa_hl{background-image:linear-gradient(transparent var(--hlt0,70%),var(--hlc,#ffe66d) var(--hlt0,70%),var(--hlc,#ffe66d) var(--hlt1,92%),transparent var(--hlt1,92%));background-repeat:no-repeat;background-size:calc(var(--hlw,0) * 1%) 100%;padding:0 .06em;-webkit-box-decoration-break:clone;box-decoration-break:clone}';
  // スクロールで画面に入ったら再生。JS無効なら全部表示（消えない保険）。"__ce"を含めない＝保存で残る。
  // ★時間トリガー(setTimeout)は使わない＝「スクロールで画面に入った時に1回だけ再生」に統一。
  //   IntersectionObserverだけで判定→発火したらunobserve（1回きり）。上部の要素は監視開始時に即発火＝読み込みで再生。
  var FX_RUN='(function(){var d=document,h=d.documentElement;'
    +'if(!d.querySelector(".fxa_pre,.fxa_hl")){return;}h.classList.add("fxa-on");'
    +'[].slice.call(d.querySelectorAll(".fxa_pre")).forEach(function(el){if(el.style.transform)el.style.removeProperty("transform");});'  // 自動修復：出現アニメ要素に焼き込まれた古いtransform(プレビュー残骸)を消す＝過去に固まった分も開くだけで直る
    // 🖍マーカーの帯を毎フレーム手動で描く（--hlw を0→100へ）。連打で二重に走らないよう世代番号(__hlGen)で古いループは自分で止める。
    +'function sweepHl(el){var dur=parseFloat(el.style.getPropertyValue("--hldur"))||0.45;var gen=(el.__hlGen=(el.__hlGen||0)+1);var t0=null;'
    +'function step(ts){if(el.__hlGen!==gen)return;if(t0===null)t0=ts;var p=Math.min(1,(ts-t0)/(dur*1000));var e=1+2.70158*Math.pow(p-1,3)+1.70158*Math.pow(p-1,2);'
    +'el.style.setProperty("--hlw",e*100);if(p<1)requestAnimationFrame(step);else el.classList.add("fxa_in");}'
    +'requestAnimationFrame(step);}'
    +'window.__fxaSweepHl=sweepHl;'
    +'function all(){return [].slice.call(d.querySelectorAll(".fxa_pre:not(.fxa_in),.fxa_hl:not(.fxa_in)"));}'
    // 🔢グループ表示：data-cegrp="1/2/3"の要素は①→②→③の順にまとめて動く（グループ間0.3s・グループ内は0.15sずつ）
    +'function groupDelay(el){var g=+el.getAttribute("data-cegrp")||0;if(!g)return 0;'
    +'var mem=[].slice.call(d.querySelectorAll(\\'[data-cegrp="\\'+g+\\'"]\\'));var idx=mem.indexOf(el);'
    +'return (g-1)*300+Math.max(0,idx)*150;}'
    // data-cedelay="ミリ秒" を要素に直接付けると、グループ計算より優先してその通りの遅れで再生する（細かい手動演出用）
    +'function reveal(el){ function go(){ if(el.classList.contains("fxa_hl")) sweepHl(el); else el.classList.add("fxa_in"); }'
    +'var cd=el.getAttribute("data-cedelay"); var gd=cd!=null?+cd:groupDelay(el); if(gd>0) setTimeout(go,gd); else go(); }'
    +'if(!("IntersectionObserver" in window)){all().forEach(reveal);return;}'
    +'var io=new IntersectionObserver(function(es){es.forEach(function(en){if(en.isIntersecting){var t=en.target;io.unobserve(t);requestAnimationFrame(function(){reveal(t);});}});},{threshold:0,rootMargin:"0px 0px -18% 0px"});'
    +'function obs(){requestAnimationFrame(function(){all().forEach(function(el){io.observe(el);});});}'  // 初回描画(fxa_pre隠れ状態)を1フレーム待ってから監視開始＝上部要素も一瞬で終わらずスライドする
    +'if(d.readyState==="loading")d.addEventListener("DOMContentLoaded",obs);else obs();})();';
  // CSSは「消して足す」でなく内容だけ差し替える（一瞬スタイルが消えるチラつき・前のアニメへの干渉を防ぐ）
  function _fxInjCss(){ var st=document.getElementById('fxa-css'); if(st){ if(st.textContent!==FX_CSS) st.textContent=FX_CSS; return; } st=document.createElement('style'); st.id='fxa-css'; st.textContent=FX_CSS; (document.head||document.documentElement).appendChild(st); }
  // runは「無ければ足すだけ」＝既にあれば再実行しない（毎回の焼き込みで再実行→重複observer→前のアニメが乱れるのを防ぐ）
  function _fxInjRun(){ if(document.getElementById('fxa-run')) return; var sc=document.createElement('script'); sc.id='fxa-run'; sc.textContent=FX_RUN; (document.body||document.documentElement).appendChild(sc); }
  function ensureFxAssets(){ _fxInjCss(); _fxInjRun(); }  // applyBakeから毎回呼ばれても副作用が無い
  // 既存カンプを開いた瞬間に1回だけ：焼き込み済みの古いrunを最新版へ入れ替える（clip撤廃・マーカー再生方式の変更などを既存にも反映）
  if(document.querySelector('.fxa_pre,.fxa_wave,.fxa_ch,.fxa_hl,[class*="fxa_lp_"]')){ var _or=document.getElementById('fxa-run'); if(_or) _or.remove(); ensureFxAssets(); }
  function fxClearClasses(el){
    [].slice.call(el.classList).forEach(function(c){ if(c.indexOf('fxa_')===0 && c!=='fxa_ch') el.classList.remove(c); });
    ['--fxa-dur','--fxa-dist','--fxa-scale','--fxa-blur','--fxa-deg','--fxa-amp','--fxa-stag'].forEach(function(p){ el.style.removeProperty(p); });
  }
  function _fxDist(el,a){ el.style.setProperty('--fxa-dist', fxParam(a,'dist')+'px'); }
  // 出現アニメを要素に直接かけると、その要素自身のCSSアニメ（ボタンのループ鼓動など）がtransformを毎フレーム
  // 上書きして「せり上がり等の動きが出ない」。→ そういう要素だけラッパーで包み、ラッパー側で出現させる
  // （中の要素は自分のアニメ・ホバーをそのまま保てる）。
  function fxUnwrap(el){
    var w=el&&el.parentElement;
    if(w&&w.classList&&w.classList.contains('fxa_wrap')){ w.parentNode.insertBefore(el,w); w.remove(); }
  }
  function fxWrap(el){
    var w=document.createElement('span'); w.className='fxa_wrap';
    var disp=''; try{ disp=getComputedStyle(el).display; }catch(_){}
    w.style.display=(disp&&disp.indexOf('inline')===0)?'inline-block':'block';
    el.parentNode.insertBefore(w,el); w.appendChild(el); return w;
  }
  function applyBake(el,k){
    var a=fxDef(k); if(!a){ if(msg)msg.textContent='⚠ まず動きを選んでください'; return; }
    ensureFxAssets();
    fxUnwrap(el);  // 既存の出現ラッパーがあれば解除して素の要素に戻す（付け直し対応）
    stopAnim(el); clearPreviewStyle(el); fxClearClasses(el); fxUnsplit(el); fxStripImpLetters(el);  // 2回目以降も必ずプレーン文字から＋一括改善の文字アニメを外す（上書き消え防止）
    // ★ドラッグ/拡大で付いた transition:none / animation:none を外す。これが残ると出現もループも一瞬で終わって「動かない」に見える。
    el.style.removeProperty('transition'); el.style.removeProperty('animation');
    el.style.setProperty('--fxa-dur', (fxParam(a,'dur')||800)+'ms');  // a.dは説明文なので使わない
    if(a.g==='loop'){
      if(a.dir==='ps'){ el.style.setProperty('--fxa-amp', (fxParam(a,'amp')/100)); el.classList.add('fxa_lp_pulse'); }
      else if(a.dir==='fy'){ el.style.setProperty('--fxa-amp', fxParam(a,'amp')+'px'); el.classList.add('fxa_lp_float'); }
      else if(a.dir==='by'){ el.style.setProperty('--fxa-amp', fxParam(a,'amp')+'px'); el.classList.add('fxa_lp_bounce'); }
      else if(a.glow){ el.classList.add('fxa_lp_glow'); }
    } else if(a.g==='char'){
      var spans=splitChars(el); spans.forEach(function(sp,i){ sp.style.setProperty('--i', i); });  // 上でfxUnsplit済み＝常にプレーンから割る
      if(!spans.length){ el.style.removeProperty('--fxa-dur'); if(msg)msg.textContent='⚠ ここには文字が無いので文字アニメは付けられません。画像には「ふわっと出現」「ズームイン」「ぼやけて出現」などを選んでください'; return; }
      if(a.loop){ el.style.setProperty('--fxa-amp', fxParam(a,'amp')+'px'); el.classList.add('fxa_wave'); }
      else {
        el.classList.add('fxa_pre'); el.classList.add(a.type?'fxa_tw':'fxa_cpre');
        el.style.setProperty('--fxa-stag', fxParam(a,'stag')+'ms');
        if(!a.type) el.style.setProperty('--fxa-dist', fxParam(a,'dist')+'px');
        el.classList.add('fxa_in');  // 編集中はすぐ見えるように（保存時にfxa_inは外す＝再生に戻る）
      }
    } else {
      // 出現(in)：要素自身にCSSアニメ(ボタンのループ等)がある時だけラッパーで包み、それに出現をかける
      // （transformの奪い合いを回避＝せり上がり等がちゃんと動く。中の要素は自分のアニメ・ホバーを保つ）。
      var host=el, an='none';
      try{ an=getComputedStyle(el).animationName||'none'; }catch(_){}
      // 自分のCSSアニメ持ちだけラッパーに出現をかける（transformの奪い合い回避）。
      // 移動は translate 個別プロパティに乗っているので、出現アニメ(transform)と両立＝移動要素もそのまま付けてOK。
      if(an!=='none'){ host=fxWrap(el); }
      host.style.setProperty('--fxa-dur', (fxParam(a,'dur')||800)+'ms');
      host.classList.add('fxa_pre');
      if(a.dir==='y'){ host.classList.add('fxa_y'); host.style.setProperty('--fxa-dist', fxParam(a,'dist')+'px'); }
      else if(a.dir==='xl'){ host.classList.add('fxa_xl'); host.style.setProperty('--fxa-dist', fxParam(a,'dist')+'px'); }
      else if(a.dir==='xr'){ host.classList.add('fxa_xr'); host.style.setProperty('--fxa-dist', fxParam(a,'dist')+'px'); }
      else if(a.dir==='s'){ host.classList.add('fxa_s'); host.style.setProperty('--fxa-scale', (fxParam(a,'scale')/100)); }
      else if(a.dir==='bl'){ host.classList.add('fxa_bl'); host.style.setProperty('--fxa-blur', fxParam(a,'blur')+'px'); }
      else if(a.dir==='ry'){ host.classList.add('fxa_ry'); host.style.setProperty('--fxa-deg', fxParam(a,'deg')+'deg'); }
      else if(a.dir==='clip'){ host.classList.add('fxa_clip'); host.style.setProperty('--fxa-dist', fxParam(a,'dist')+'px'); }
      host.classList.add('fxa_in');
    }
    markDirty();
    if(msg) msg.textContent='✅ 付けました。ヘッダの「💾 変更を保存」で残ります（スクロールで再生）';
  }
  // 付けた動き（出現/ループ/文字アニメ）を全部外して素の要素に戻す（AIなし・即反映）。
  function removeBake(el){
    if(!el) return;
    fxUnwrap(el);  // 自分のCSSアニメ用に包んだラッパーがあれば解除
    stopAnim(el); clearPreviewStyle(el); fxClearClasses(el); fxUnsplit(el); fxStripImpLetters(el);
    el.style.removeProperty('transition'); el.style.removeProperty('animation');
    markDirty();
    if(msg) msg.textContent='この要素の動きを消しました（保存で確定）';
  }
  // アニメ選択：ハイライト＋スライダー表示＋即プレビュー
  function selectFx(k, btn){
    var a=fxDef(k); if(!a) return;
    curAnim=k; curP={};
    var _saved=_fxLast[k]||{};  // 前回この動きでいじった値があればそれを初期値に、無ければ元のデフォルト
    a.sl.forEach(function(s){ curP[s.k]=(_saved[s.k]!=null?_saved[s.k]:s.def); });
    if(curMenu){ [].slice.call(curMenu.querySelectorAll('#__fx_grid button')).forEach(function(b){ b.classList.remove('on'); }); }
    if(btn) btn.classList.add('on');
    var sl=document.getElementById('__fx_sl');
    if(sl){
      sl.innerHTML=a.sl.map(function(s){ return '<label>'+esc(s.l)+'<span>'+curP[s.k]+(s.u||'px')+'</span><input type="range" data-k="'+s.k+'" min="'+s.min+'" max="'+s.max+'" step="'+(s.step||1)+'" value="'+curP[s.k]+'"></label>'; }).join('');
      sl.oninput=function(e){ var inp2=e.target.closest('input'); if(!inp2) return; var kk=inp2.getAttribute('data-k'); curP[kk]=+inp2.value; if(!_fxLast[curAnim]) _fxLast[curAnim]={}; _fxLast[curAnim][kk]=+inp2.value; _fxSaveLast(); var sd=null; for(var i=0;i<a.sl.length;i++){ if(a.sl[i].k===kk) sd=a.sl[i]; } var lb=inp2.parentNode.querySelector('span'); if(lb) lb.textContent=inp2.value+((sd&&sd.u)||'px'); playAnim(curEl,curAnim); };
    }
    var ctl=document.getElementById('__fx_ctl'); if(ctl) ctl.style.display='block';
    playAnim(curEl,k);
  }
  function resetPos(el){
    el.style.removeProperty('transform'); el.style.removeProperty('transform-origin'); el.style.removeProperty('animation'); el.style.removeProperty('transition');
    el.style.removeProperty('translate'); el.style.removeProperty('rotate'); el.style.removeProperty('scale');  // 個別プロパティ方式の移動も戻す
    if(el.getAttribute('data-cew')!=null){ // 画像サイズを変えていたら、それも元に戻す（元からの幅指定は触らない）
      el.style.removeProperty('width'); el.style.removeProperty('height'); el.style.removeProperty('object-fit'); el.style.removeProperty('max-width');
    }
    el.style.removeProperty('min-height'); el.style.removeProperty('width'); el.style.removeProperty('max-width');  // 縦の余白・横幅の増減も戻す
    el.removeAttribute('data-cetx'); el.removeAttribute('data-cety'); el.removeAttribute('data-cesx'); el.removeAttribute('data-cesy'); el.removeAttribute('data-cero'); el.removeAttribute('data-cebt');
    el.removeAttribute('data-cew'); el.removeAttribute('data-ceh');
    markDirty();
  }
  // ドラッグで動かす：対象要素に直接 mousedown を付ける（確実に掴める）
  var dragEl=null, dActive=false, dSX=0,dSY=0,dOX=0,dOY=0;
  // ページ全体の器（body/main/html/section/header/footer）や画面いっぱいの要素は動かさせない。
  // ＝誤って掴むとページ全体がズレて巨大な余白になる事故を防ぐ。
  function _undraggable(el){
    if(!el||el===document.body||el===document.documentElement) return true;
    var tag=el.tagName;
    if(tag==='BODY'||tag==='HTML'||tag==='MAIN') return true;  // ページの根っこだけ禁止
    // 「ページ全体をくるむ器」（画面いっぱい かつ 中に section/header/footer/main を含む）も禁止
    try{
      var r=el.getBoundingClientRect();
      if(r.width>=window.innerWidth*0.9 && r.height>=window.innerHeight*0.9 && el.querySelector && el.querySelector('section,header,footer,main')) return true;
    }catch(_){}
    return false;  // セクション単体・見出し・画像・カード等は動かせる
  }
  function _dDown(e){
    if(e.altKey) return;  // ★地雷：ドラッグモードが固定でONの要素は、Altを押しても文字選択に譲らず飲み込んでしまっていた。ここで先に手放す。
    if(_undraggable(dragEl)){ return; }  // 器は動かさない（保険）
    dActive=true; dSX=e.clientX; dSY=e.clientY;
    dOX=+dragEl.getAttribute('data-cetx')||0; dOY=+dragEl.getAttribute('data-cety')||0;
    document.body.style.userSelect='none'; e.preventDefault(); e.stopPropagation();
  }
  document.addEventListener('mousemove',function(e){ if(dActive&&dragEl) setPos(dragEl, dOX+(e.clientX-dSX), dOY+(e.clientY-dSY)); },true);
  document.addEventListener('mouseup',function(){ if(dActive){ dActive=false; document.body.style.userSelect=''; pushUndo(); } },true);
  // ★普通にクリックしてつかむと、ボタンを押さなくてもその場で即ドラッグできる（既定の動き）。
  //   文字を選んで下線/マーカー/文字色を付けたい時だけ、Altキーを押しながら選ぶ
  //   （Alt無しだとドラッグが割り込むので、Alt有りの時だけ従来通り文字選択に譲る）。
  var _altEl=null, _altActive=false, _aSX=0,_aSY=0,_aOX=0,_aOY=0;
  function _inUI2(node){ var el=node&&(node.nodeType===1?node:node.parentElement); return el&&el.closest&&(el.closest('#__ce')||el.closest('#__ce_cm')||el.closest('#__ce_pk')||el.closest('#__ce_selc')||el.closest('#__ce_toast')); }
  document.addEventListener('mousedown',function(e){
    if(e.altKey || e.button!==0 || _inUI2(e.target)) return;
    var el=pickTarget(e.target); if(!el||_undraggable(el)) return;
    _altEl=el; _altActive=true; _aSX=e.clientX; _aSY=e.clientY;
    _aOX=+el.getAttribute('data-cetx')||0; _aOY=+el.getAttribute('data-cety')||0;
    document.body.style.userSelect='none'; e.preventDefault(); e.stopPropagation();
  },true);
  document.addEventListener('mousemove',function(e){ if(_altActive&&_altEl) setPos(_altEl, _aOX+(e.clientX-_aSX), _aOY+(e.clientY-_aSY)); },true);
  document.addEventListener('mouseup',function(){ if(_altActive){ _altActive=false; document.body.style.userSelect=''; _altEl=null; pushUndo(); } },true);
  document.addEventListener('keydown',function(e){ if(e.key==='Alt') document.documentElement.classList.add('__ce_altmode'); },true);
  document.addEventListener('keyup',function(e){ if(e.key==='Alt') document.documentElement.classList.remove('__ce_altmode'); },true);
  window.addEventListener('blur',function(){ document.documentElement.classList.remove('__ce_altmode'); });
  function toggleDrag(el,btn){
    if(_undraggable(el)){ if(msg) msg.textContent='これはページ全体の器なので動かせません（中の要素を選んでください）'; return; }
    if(dragEl===el){ // 同じ要素をもう一度押したら終了
      el.removeEventListener('mousedown',_dDown,true); el.style.cursor=''; dragEl=null;
      if(btn) btn.textContent='🖱 ドラッグで動かす（押して開始/終了）'; msg.textContent=''; return;
    }
    if(dragEl){ dragEl.removeEventListener('mousedown',_dDown,true); dragEl.style.cursor=''; }
    dragEl=el; el.style.cursor='move'; el.addEventListener('mousedown',_dDown,true);
    if(btn) btn.textContent='✋ ドラッグ終了';
    msg.textContent='要素を掴んで動かせます（もう一度押すと終了）';
  }
  // 右クリック直後に呼ぶ：確実にドラッグON（トグルではない）
  function setDragOn(el,btn){
    if(_undraggable(el)){ if(msg) msg.textContent='これはページ全体の器なので動かせません（中の見出し・画像・カードを選んでください）'; return; }
    if(dragEl && dragEl!==el){ dragEl.removeEventListener('mousedown',_dDown,true); dragEl.style.cursor=''; }
    dragEl=el; el.style.cursor='move'; el.addEventListener('mousedown',_dDown,true);
    if(btn) btn.textContent='✋ ドラッグ中（もう一度押すと解除）';
  }
  // ===== ⟲ ひとつ戻す（AIなしの直接編集＋AI修正を1手ずつ取り消す）=====
  // 本文（ヘッダ/セクション/フッタ等）だけを丸ごとスナップショットして積む。
  // 編集UI(#__ce*)や<script>/<style>は触らない＝復元してもツール自身は壊れない。
  var _undoStack=[], _lastSnap=null;
  function _isContent(n){
    if(n.nodeType!==1) return false;
    if(n.id && n.id.indexOf('__ce')===0) return false;
    if(n.tagName==='SCRIPT'||n.tagName==='STYLE') return false;
    return true;
  }
  function _contentNodes(){ return [].slice.call(document.body.children).filter(_isContent); }
  function snapContent(){ return _contentNodes().map(function(n){ return n.outerHTML; }).join(''); }
  function updUndoBtn(){ var u=document.getElementById('__ce_undo'); if(u) u.style.opacity=_undoStack.length?'1':'.4'; }
  // 変更後に呼ぶ：直前の状態(_lastSnap)を積んで、現在を新しい基準にする（実質変化なしなら積まない）
  function pushUndo(){
    var cur=snapContent();
    if(cur===_lastSnap) return;
    if(_lastSnap!==null){ _undoStack.push(_lastSnap); if(_undoStack.length>25) _undoStack.shift(); }
    _lastSnap=cur; updUndoBtn();
  }
  function _restoreContent(html){
    _contentNodes().forEach(function(n){ n.remove(); });
    var tpl=document.createElement('template'); tpl.innerHTML=html;
    document.body.insertBefore(tpl.content, document.body.firstChild);
    // 復元で opacity:0 等のまま隠れる本文が出ないよう強制表示（保険・_SERVE_SAFETYと同じ考え）
    [].slice.call(document.body.querySelectorAll('*')).forEach(function(e){
      if(e.id && e.id.indexOf('__ce')===0) return;
      var cs; try{ cs=getComputedStyle(e); }catch(_){ return; }
      if(parseFloat(cs.opacity)===0){ e.style.setProperty('opacity','1','important'); e.style.transform='none'; }
      if(cs.visibility==='hidden'){ e.style.setProperty('visibility','visible','important'); }
    });
  }
  function undoStep(){
    if(!_undoStack.length){ msg.textContent='これ以上戻せません'; return; }
    closeMenu();
    if(dragEl){ dragEl=null; dActive=false; }  // 復元で対象要素が入れ替わるため掴み状態は解除
    _restoreContent(_undoStack.pop());
    _lastSnap=snapContent();
    _dirty=true; var b=document.getElementById('__ce_save'); if(b){ b.textContent='💾 変更を保存'; b.classList.add('saved'); }
    updUndoBtn();
    msg.textContent='ひとつ前に戻しました（さらに戻せます／保存で確定）';
  }
  // 位置/大きさを変えたら、ヘッダの保存ボタンを「💾 変更を保存」に変えて緑で目立たせる（ボタンは1つに統一）
  var _dirty=false;
  function markDirty(){
    _dirty=true;
    var b=document.getElementById('__ce_save');
    if(b){ b.textContent='💾 変更を保存'; b.classList.add('saved'); }
    if(!dActive) pushUndo();  // ドラッグ中は積まず、離した時(mouseup)に1回だけ積む
  }
  function cleanHtml(){
    var doc=document.documentElement.cloneNode(true);
    ['#__ce','#__ce_cm','#__ce_pk','#__ce_toast','#__ce_savebar','#__ce_selc'].forEach(function(sel){
      [].slice.call(doc.querySelectorAll(sel)).forEach(function(n){n.remove();});
    });
    // ブラウザ拡張機能（Glasp等）がページに注入したUIが紛れ込むと、保存のたびに増殖してファイルが重くなる。
    // 編集UI以外の"見た目に無関係な"拡張機能の断片はここで丸ごと除去する。
    [].slice.call(doc.querySelectorAll('[class*="glasp-extension"]')).forEach(function(n){ n.remove(); });
    [].slice.call(doc.querySelectorAll('.__ce_sel,.__ce_hl,.__ce_sechl,.__ce_busy')).forEach(function(n){n.classList.remove('__ce_sel','__ce_hl','__ce_sechl','__ce_busy');});
    // プレビュー用アニメ(__ceax_*)は一時的なものなので保存に残さない（クラス・インライン両方）
    [].slice.call(doc.querySelectorAll('[class*="__ceax_"]')).forEach(function(n){ [].slice.call(n.classList).forEach(function(cl){ if(cl.indexOf('__ceax_')===0) n.classList.remove(cl); }); });
    [].slice.call(doc.querySelectorAll('[style*="__ceax"]')).forEach(function(n){ n.style.removeProperty('animation'); });
    // 焼き込みアニメの一時「表示中」クラス(fxa_in)は外す＝保存版はスクロールで再生に戻す（付けた設定fxa_pre等は残す）
    [].slice.call(doc.querySelectorAll('.fxa_in')).forEach(function(n){ n.classList.remove('fxa_in'); });
    // 🖍マーカーは--hlw(0〜100)で伸び具合を持っているので、fxa_inを外すだけでは戻らない。
    // ★これを忘れると「再生し終わった状態(--hlw:100)」がそのまま保存され、次に開いた時に
    //   アニメせず最初から引かれた状態になってしまう（実際に起きたバグ）。必ず0に戻す。
    [].slice.call(doc.querySelectorAll('.fxa_hl')).forEach(function(n){ n.style.setProperty('--hlw',0); });
    // ドラッグモード中だけの目印(cursor:move)は編集中の一時状態。保存に残ると次に開いた時も
    // 十字カーソルのままになり、しかもAltを押しても文字選択に譲らず固まって見える不具合の元になる。
    [].slice.call(doc.querySelectorAll('[style*="cursor: move"],[style*="cursor:move"]')).forEach(function(n){ n.style.removeProperty('cursor'); });
    // オープニングの幕：編集用に「止めて表示」していた状態(data-paused/インライン)を解除＝保存版は開いた時に自動再生に戻す
    var _op=doc.querySelector('#__op_screen');
    if(_op){ _op.removeAttribute('data-paused'); _op.style.removeProperty('display'); _op.style.removeProperty('opacity'); _op.style.removeProperty('transition'); }
    [].slice.call(doc.querySelectorAll('script')).forEach(function(s){ if(/__ce/.test(s.textContent)) s.remove(); });
    [].slice.call(doc.querySelectorAll('style')).forEach(function(s){ if(/#__ce/.test(s.textContent)) s.remove(); });
    return '<!doctype html>\\n'+doc.outerHTML;
  }
  function saveLayout(){
    var b=document.getElementById('__ce_save'); if(b) b.textContent='保存中…';
    fetch('/api/save_camp_html',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,html:cleanHtml()})})
    .then(function(r){return r.json();}).then(function(d){
      if(d.ok){ if(b) b.textContent='✅ 保存しました'; setTimeout(function(){location.reload();},600); }
      else { if(b) b.textContent='⚠ 失敗：'+(d.message||''); }
    }).catch(function(){ if(b) b.textContent='⚠ 通信エラー'; });
  }
  function descEl(el){
    var tag=el.tagName.toLowerCase();
    var cls=(typeof el.className==='string'&&el.className.trim())?'.'+el.className.trim().split(/\\s+/).slice(0,2).join('.'):'';
    var txt=(el.textContent||'').trim().replace(/\\s+/g,' ').slice(0,36);
    var ex='';
    if(tag==='img'){ ex=' 画像['+((el.getAttribute('alt')||'')||((el.getAttribute('src')||'').split('/').pop()))+']'; }
    return tag+cls+(txt?('「'+txt+'」'):'')+ex;
  }
  function secIndexOf(el){
    var s=el.closest('section'); if(!s) return -1;
    var all=[].slice.call(document.querySelectorAll('section')).filter(function(x){return !x.closest('#__ce');});
    return all.indexOf(s);
  }
  function applyEl(sIdx, instruction, keepText, styleType){ closeMenu(); box.classList.remove('min'); submit(sIdx, instruction, keepText, styleType); }
  // 掴んだ要素が「1文字ずつ分割されたspan」などインラインの断片なら、
  // 内包する見出し/段落などのブロックまで親を上る（見出し全体をまとめて選べる）。
  function pickTarget(el){
    var INLINE={SPAN:1,B:1,I:1,EM:1,STRONG:1,SMALL:1,MARK:1,U:1,FONT:1,WBR:1,BR:1};
    var cur=el, hops=0;
    while(cur && cur.parentElement && cur!==document.body && hops<10 && INLINE[cur.tagName]){
      cur=cur.parentElement; hops++;
    }
    return cur||el;
  }
  // 「透明で中身のない膜（フェード用オーバーレイ等）」か判定。
  // 文字も画像も背景画像も持たず、背景色も透明/半透明なら＝下を触りたいのに邪魔する膜。
  var _REAL_TAGS={IMG:1,VIDEO:1,SVG:1,PICTURE:1,CANVAS:1,INPUT:1,BUTTON:1,SELECT:1,TEXTAREA:1,IFRAME:1};
  function _seeThrough(el){
    if(!el||el===document.body||el.nodeType!==1) return false;
    if(_REAL_TAGS[el.tagName]) return false;                           // 要素自身が画像/動画等の実体＝膜ではない
    if((el.textContent||'').trim()!=='') return false;                 // 中に文字がある＝本物の入れ物
    if(el.querySelector && el.querySelector('img,video,svg,picture,input,button,canvas,select,textarea')) return false;
    var s; try{ s=getComputedStyle(el); }catch(_){ return false; }
    if(s.backgroundImage && s.backgroundImage!=='none') return false;  // 背景画像を持つ＝本物
    var m=(s.backgroundColor||'').match(/rgba?\\(([^)]+)\\)/);          // 不透明な色帯はデザイン部品なので残す
    if(m){ var p=m[1].split(',').map(function(x){return parseFloat(x);}); var a=p.length>3?p[3]:1; if(a>=0.95) return false; }
    return true;
  }
  // 右クリック地点で、膜を貫通して「実体のある要素」まで潜る（膜が何枚重なっていてもOK）。
  function _descendOverlay(el, x, y){
    if(!_seeThrough(el)) return el;
    var under=document.elementsFromPoint(x, y);
    for(var i=0;i<under.length;i++){
      var c=under[i];
      if(c.closest('#__ce')||c.closest('#__ce_cm')||c.closest('#__ce_pk')) continue;
      var pc=pickTarget(c);
      if(!_seeThrough(pc)) return pc;   // 実体のある要素が見つかったらそこを選ぶ
    }
    return el;                           // 全部が膜なら元のまま（膜自体を消せるように）
  }
  // capture:true＝キャプチャ段階で先取りする。忠実クローン(元JS保持)の中に元サイト自前の
  // 右クリック禁止スクリプトが残っていても、こちらを優先させて確実にメニューを開く。
  document.addEventListener('contextmenu',function(e){
    var el=pickTarget(e.target);
    if(!el||el.closest('#__ce')||el.closest('#__ce_cm')||el.closest('#__ce_pk')) return;
    el=_descendOverlay(el, e.clientX, e.clientY);  // 透明な膜は貫通して下の実体を掴む
    e.preventDefault(); e.stopPropagation(); if(e.stopImmediatePropagation) e.stopImmediatePropagation(); closeMenu();
    curEl=el; el.classList.add('__ce_sel');
    var sIdx=secIndexOf(el), d=descEl(el);
    // 画像なら「AIなしの即差し替え」を最優先で出す（画像のAI指示は不安定なため）
    var imgEl = (el.tagName==='IMG') ? el : (el.querySelector ? el.querySelector('img') : null);
    // 右クリック座標に重なる「画像」を集める：<img> と 背景画像(background-image) の両方。前面→背面順。
    var cands=[];
    function _has(n){ return cands.some(function(c){return c.el===n;}); }
    function _addBg(n){
      var bg=''; try{ bg=getComputedStyle(n).backgroundImage; }catch(_){}
      var mm=bg&&bg.match(/url\\(["']?(.*?)["']?\\)/);
      if(mm && mm[1] && mm[1].indexOf('data:')!==0 && !_has(n)){ cands.push({el:n,type:'bg',url:mm[1]}); }
    }
    document.elementsFromPoint(e.clientX, e.clientY).forEach(function(n){
      if(!n.closest || n.closest('#__ce')||n.closest('#__ce_cm')||n.closest('#__ce_pk')) return;
      if(n.tagName==='IMG'){ if(!_has(n)) cands.push({el:n,type:'img',url:n.currentSrc||n.src}); return; }
      _addBg(n);
    });
    // pointer-events:none の装飾など、座標検出で拾えない背面の背景画像も、矩形が重なれば候補に足す
    var scope=(el.closest && el.closest('section'))||document.body;
    [].slice.call(scope.querySelectorAll('*')).forEach(function(n){
      if(n.closest('#__ce')||n.closest('#__ce_cm')||n.closest('#__ce_pk')) return;
      var r=n.getBoundingClientRect();
      if(!r.width||e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom) return;
      if(n.tagName==='IMG'){ if(!_has(n)) cands.push({el:n,type:'img',url:n.currentSrc||n.src}); return; }
      _addBg(n);
    });
    if(!cands.length){
      var fb=(el.tagName==='IMG')?[el]:(el.querySelectorAll?[].slice.call(el.querySelectorAll('img')):[]);
      fb.forEach(function(im){cands.push({el:im,type:'img',url:im.currentSrc||im.src});});
    }
    var swapH = (cands.length
      ? '<button class="go2" id="__ce_cmswap" style="background:#1a7f37;margin-bottom:6px">🖼 この画像を差し替え（AIなし・一瞬）</button>'
        +'<div class="cap" style="margin:0 0 8px">画像はこれが確実です（差し替えは一瞬）</div>'
      : '')
      // 白フチ／はみ出しカード／背景の飾り／背景に設定／水彩(AI) をまとめて1つの入口に統合（ボタン数を増やさない）
      + '<button class="go2" id="__ce_cmdeco" style="background:#0b6bcb;margin-bottom:8px">🖼 写真を加工（フチ・カード・背景など）</button>';
    // 右クリックのAIセクションは「無料の焼き込みで出来ないもの」だけに絞る（背景装飾=bg / AI専用=ai）。
    // 単純な出現/ループ系は上の「動きを選ぶ→付ける」に一本化したのでここには出さない。
    var aiList=PRESETS.filter(function(p){return p.bg||p.ai;});
    var agh=aiList.map(function(p,i){return '<button class="ag2" data-i="'+i+'"><b>'+esc(p.b)+'</b><span>'+esc(p.d)+'</span></button>';}).join('');
    var m=document.createElement('div'); m.id='__ce_cm';
    m.innerHTML='<div class="h"><span class="t">'+esc(d)+'</span><span class="c" id="__ce_cmx">✕</span></div>'
      +'<div class="bd2">'+swapH
      +'<div class="cap">🖱 位置を動かす（AIなし・即反映・普通にドラッグでOK／文字を選ぶ時だけAlt+ドラッグ）</div>'
      +'<button class="go2" id="__ce_cmdrag" style="background:#0b6bcb;margin-bottom:8px">🖱 掴んで動かす（押してから要素をドラッグ）</button>'
      +'<div class="__ce_nudge"><span class="sp"></span><button data-nx="0" data-ny="-6">↑</button><span class="sp"></span>'
      +'<button data-nx="-6" data-ny="0">←</button><button data-rst="1">⟲</button><button data-nx="6" data-ny="0">→</button>'
      +'<span class="sp"></span><button data-nx="0" data-ny="6">↓</button><span class="sp"></span></div>'
      +'<div class="__ce_size">'
      +'<button data-sx="1.1" data-sy="1.1">＋ 大きく</button><button data-sx="0.909" data-sy="0.909">－ 小さく</button>'
      +'<button data-sx="1.1" data-sy="1">⇔ 横に長く</button><button data-sx="0.909" data-sy="1">⇔ 横を縮め</button>'
      +'<button data-sx="1" data-sy="1.1">⇕ 縦に長く</button><button data-sx="1" data-sy="0.909">⇕ 縦を縮め</button>'
      +'<button data-ro="-6">⟲ 左に回す</button><button data-ro="6">⟳ 右に回す</button></div>'
      +'<div class="cap">⬍ 縦の高さ・余白（高く/低く・AIなし・歪まない）</div>'
      +'<div class="__ce_size"><button data-mh="80">＋ 高く（余白を足す）</button><button data-mh="-80">－ 低く</button></div>'
      +'<div class="cap">⬌ 横の幅（広く/狭く・AIなし・歪まない）</div>'
      +'<div class="__ce_size"><button data-mw="80">＋ 横に広く</button><button data-mw="-80">－ 横を狭く</button></div>'
      +'<button class="go2" id="__ce_cmbr" style="background:#0b6bcb;margin-bottom:8px">✏ 文字を編集（改行・大きさ・フォント・色／AIなし）</button>'
      +'<button class="go2" id="__ce_cmhl" style="background:#eab308;color:#1d1d1f;margin-bottom:4px">🖍 この文字にマーカー（スクロールで線が伸びる）</button>'
      +'<div class="cap" style="margin-bottom:8px">🖍 色 <input type="color" id="__ce_cmhlc" value="'+hlDefaultColor()+'" style="width:34px;height:22px;padding:0;border:none;border-radius:4px;vertical-align:middle;cursor:pointer"><span id="__ce_cmhlsw" style="display:inline-flex;gap:3px;flex-wrap:wrap;vertical-align:middle;margin-right:4px">'+hlSwatchesHtml()+'</span> 太さ<button id="__ce_cmhlthm" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:1px 7px;cursor:pointer">－</button><button id="__ce_cmhlthp" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:1px 7px;cursor:pointer">＋</button> 位置<button id="__ce_cmhlpu" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:1px 7px;cursor:pointer" title="上へ">▲</button><button id="__ce_cmhlpd" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:1px 7px;cursor:pointer" title="下へ">▼</button> 速さ<button id="__ce_cmhldm" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:1px 7px;cursor:pointer" title="遅く">🐢</button><button id="__ce_cmhldp" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:1px 7px;cursor:pointer" title="速く">🐇</button><br>／ 文字を選んでから引くと「一部だけ」引ける／<a href="#" id="__ce_cmhlrm" style="color:#0b6bcb">🚫 マーカーを消す</a></div>'
      +'<button class="go2" id="__ce_cmstyle" style="background:#c026a6;margin-bottom:8px">✨ このセクションをおしゃれに（AIが一括）</button>'
      +'<div class="cap">✨ 動きを選ぶ（クリックで試す→調整→付ける・AIなし・無料）</div>'
      +'<button class="go2" id="__ce_fxrm" style="background:#888;margin-bottom:6px">🚫 この要素の動きを消す</button>'
      +'<div class="__ce_anim" id="__fx_grid">'+FX.map(function(a){return '<button data-ak="'+a.k+'"><b>'+esc(a.b)+'</b><span>'+esc(a.d)+'</span></button>';}).join('')+'</div>'
      +'<div class="__fx_ctl" id="__fx_ctl" style="display:none"><div id="__fx_sl"></div><button class="go2" id="__fx_apply" style="background:#1a7f37;margin-top:2px">✅ この動きを付ける（無料・保存で残る）</button></div>'
      +'<div class="cap">🔢 グループでまとめて順番に表示（①→②→③の順で・グループ内は0.15s刻み・動きを付けた要素が対象）</div>'
      +'<div class="__ce_grp" id="__ce_grp"><button data-grp="1">① グループ1</button><button data-grp="2">② グループ2</button><button data-grp="3">③ グループ3</button><button data-grp="0" style="background:#f2f2f4">✕ 解除</button></div>'
      +'<div class="cap">🎨 背景・特殊（AIが本組み込み・数円）</div>'+agh
      +'<div class="cap" style="margin-top:8px">✍ この要素に自分で指示</div>'
      +'<input id="__ce_cmin" placeholder="例：もっと大きく赤く"><button class="go2" id="__ce_cmgo">この要素を直す</button>'
      +'<button class="go2" style="background:#4b2ea8" id="__ce_cmsg">💡 この要素の改善案</button>'
      +'<div class="chips" id="__ce_cmchips"></div>'
      +'<div class="cap" style="margin-top:10px">🚫 背景・枠・影を消す（透過画像の下の箱／AIなし）</div>'
      +'<button class="go2" id="__ce_cmnobg" style="background:#0b6bcb;margin-bottom:8px">🚫 この要素の背景・枠・影を消す</button>'
      +'<div class="cap">🗑 いらない要素を消す（AIなし・即反映）</div>'
      +'<button class="go2" id="__ce_cmdel" style="background:#c0392b">🗑 この要素を消す</button></div>';
    document.body.appendChild(m);
    // 既にグループに入っている要素なら、そのグループ番号のボタンを最初からON表示にする
    var _curGrp=el.getAttribute('data-cegrp');
    if(_curGrp){ var _gb=m.querySelector('#__ce_grp button[data-grp="'+_curGrp+'"]'); if(_gb) _gb.classList.add('on'); }
    var mw=290, mh=Math.min(window.innerHeight*0.72, m.offsetHeight||420);
    if(lastMenuPos){  // 前回ドラッグで動かした場所があれば、そこに出す（邪魔にならない位置を覚える）
      m.style.left=Math.max(0,Math.min(lastMenuPos.left, window.innerWidth-60))+'px';
      m.style.top=Math.max(0,Math.min(lastMenuPos.top, window.innerHeight-40))+'px';
    } else {
      m.style.left=Math.max(10,Math.min(e.clientX, window.innerWidth-mw-10))+'px';
      m.style.top=Math.max(10,Math.min(e.clientY, window.innerHeight-mh-10))+'px';
    }
    curMenu=m;
    // このメニュー(パネル)自体も、黒いヘッダ部分を掴んで動かせる
    (function(){
      var mh=m.querySelector('.h'); mh.style.cursor='move';
      mh.addEventListener('mousedown',function(ev){
        if(ev.target.closest('.c')) return;  // 閉じる✕は除く
        var r=m.getBoundingClientRect(), msx=ev.clientX, msy=ev.clientY, ml=r.left, mt=r.top; ev.preventDefault();
        function mv(e){ m.style.left=Math.max(0,Math.min(ml+(e.clientX-msx), window.innerWidth-60))+'px'; m.style.top=Math.max(0,Math.min(mt+(e.clientY-msy), window.innerHeight-40))+'px'; }
        function up(){ var rr=m.getBoundingClientRect(); lastMenuPos={left:rr.left, top:rr.top}; try{localStorage.setItem('__ce_menupos',JSON.stringify(lastMenuPos));}catch(_){}; document.removeEventListener('mousemove',mv,true); document.removeEventListener('mouseup',up,true); }
        document.addEventListener('mousemove',mv,true); document.addEventListener('mouseup',up,true);
      });
    })();
    var tail=' ★重要：この要素だけに適用し、他の要素や他のセクションは一切変えない。対象要素は「'+d+'」。アニメはCSS/素のJSで実装し、スクロール出現系はhtml.jsが付いた時だけ初期非表示にする保険を入れてJSが無くても中身が見えるようにする。';
    m.querySelector('.bd2').addEventListener('click',function(ev){
      var nb=ev.target.closest('.__ce_nudge button');
      if(nb){ if(nb.getAttribute('data-rst')) resetPos(curEl); else nudge(curEl, +nb.getAttribute('data-nx'), +nb.getAttribute('data-ny')); return; }
      var mhb=ev.target.closest('button[data-mh]');  // 高さ(min-height)の増減＝スケールと違い歪まない
      if(mhb){ adjustMinH(curEl, +mhb.getAttribute('data-mh')); return; }
      var mwb=ev.target.closest('button[data-mw]');  // 幅(width)の増減＝歪まない横伸ばし
      if(mwb){ adjustWidth(curEl, +mwb.getAttribute('data-mw')); return; }
      var sb=ev.target.closest('.__ce_size button');
      if(sb){
        if(sb.hasAttribute('data-ro')) rotateBy(curEl, +sb.getAttribute('data-ro'));
        else if(curEl.tagName==='IMG') sizeImg(curEl, +sb.getAttribute('data-sx'), +sb.getAttribute('data-sy'));  // 画像は歪まない方式で
        else scaleBy(curEl, +sb.getAttribute('data-sx'), +sb.getAttribute('data-sy'));
        return;
      }
      var ak=ev.target.closest('#__fx_grid button');
      if(ak){ selectFx(ak.getAttribute('data-ak'), ak); return; }
      var apl=ev.target.closest('#__fx_apply');
      if(apl){ if(!curAnim){ msg.textContent='まず上から動きを選んでください'; return; } applyBake(curEl, curAnim); return; }
      var fxrm=ev.target.closest('#__ce_fxrm');
      if(fxrm){
        removeBake(curEl); curAnim=null; curP={};
        [].slice.call(m.querySelectorAll('#__fx_grid button')).forEach(function(b){ b.classList.remove('on'); });
        var ctl=m.querySelector('#__fx_ctl'); if(ctl) ctl.style.display='none';
        return;
      }
      var gb=ev.target.closest('#__ce_grp button');
      if(gb){
        var gv=gb.getAttribute('data-grp');
        if(gv==='0'){ curEl.removeAttribute('data-cegrp'); if(msg) msg.textContent='グループを解除しました（保存で確定）'; }
        else{ curEl.setAttribute('data-cegrp', gv); if(msg) msg.textContent='グループ'+gv+'に入れました（①→②→③の順で表示・保存で確定）'; }
        [].slice.call(m.querySelectorAll('#__ce_grp button')).forEach(function(b){ b.classList.toggle('on', b===gb && gv!=='0'); });
        markDirty();
        return;
      }
      var dg=ev.target.closest('#__ce_cmdrag');
      if(dg){ toggleDrag(curEl, dg); return; }
      var ag=ev.target.closest('.ag2');
      if(ag){ var pp=aiList[+ag.dataset.i];
        if(pp.bg){
          if(imgEl){
            // 画像を右クリック時：セクション全体でなく「画像の周り」だけに装飾を置く
            var bi=pp.i.replace(/^このセクションの背景に/, 'この画像を囲むラッパー要素の背景（画像の背後〜周囲だけ）に');
            applyEl(sIdx, bi+' ★重要：セクション全体には広げず、この画像の周辺だけに装飾を置く。画像は前面のまま、他の要素やレイアウトは変えない。');
          } else {
            applyEl(sIdx, pp.i);  // 余白を右クリック時：セクション全体の背景に
          }
        } else { applyEl(sIdx, pp.i+tail); }
        return; }
      var chip=ev.target.closest('.chip');
      if(chip){ applyEl(sIdx, chip.getAttribute('data-ins')+tail); return; }
    });
    m.querySelector('#__ce_cmx').addEventListener('click',closeMenu);
    var swBtn=m.querySelector('#__ce_cmswap');
    if(swBtn){ swBtn.addEventListener('click',function(){
      if(cands.length>1){ pickWhichImg(cands); return; }   // 重なっている→どれを差し替えるか選ぶ
      if(!cands.length){ alert('差し替えられる画像がありません'); return; }
      closeMenu(); openPicker(cands[0]);
    }); }
    var decoBtn=m.querySelector('#__ce_cmdeco');
    if(decoBtn){ decoBtn.addEventListener('click',function(){
      var t=curEl, ie=imgEl, si=sIdx; closeMenu(); openPhotoDecoPicker(t, ie, si);
    }); }
    m.querySelector('#__ce_cmgo').addEventListener('click',function(){
      var v=m.querySelector('#__ce_cmin').value.trim(); if(v) editElement(curEl, v);
    });
    var stBtn=m.querySelector('#__ce_cmstyle');
    if(stBtn) stBtn.addEventListener('click',function(){
      // ★おしゃれ化は必ずセクション単位。特定できない時（ヘッダー等セクション外）は
      //   ページ全体編集（高額・全書き直し）に落とさず、その場で中止する。
      var i=Number(sIdx);
      if(!(i>=0)){
        closeMenu();
        msg.textContent='⚠ ここはセクション外（ヘッダー/フッター等）なので一括おしゃれ化は使えません。直したいセクションの中身を右クリックしてください';
        showToast('セクションの中で右クリックしてね'); setTimeout(hideToast,2600);
        return;
      }
      var o=styleIns(i);
      applyEl(i, o.ins, 1, o.t);
    });
    var brBtn=m.querySelector('#__ce_cmbr');
    if(brBtn) brBtn.addEventListener('click',function(){ openBreakEditor(curEl); });
    // 🖍 この文字にマーカー：選択がいらない版。右クリックした要素の中身を .fxa_hl で丸ごと囲む
    //   →スクロールで線がスーッと伸びる（保存版でも再生）。※選択できない箇所でも確実に引ける。
    function hlWhole(el,color){
      if(!el) return;
      hlPushColorHistory(color);
      // 既に丸ごとマーカー済みなら色だけ更新
      if(el.children.length===1 && el.firstChild && el.firstChild.classList && el.firstChild.classList.contains('fxa_hl')){
        el.firstChild.style.setProperty('--hlc',color); markDirty();
        if(msg) msg.textContent='マーカーの色を変えました（保存で確定）'; return;
      }
      var span=document.createElement('span'); span.className='fxa_hl'; span.style.setProperty('--hlc',color);
      while(el.firstChild){ span.appendChild(el.firstChild); }
      el.appendChild(span);
      if(typeof ensureFxAssets==='function') ensureFxAssets();  // アニメCSS/監視JSを注入
      if(window.__fxaSweepHl) window.__fxaSweepHl(span); else span.classList.add('fxa_in');  // 今すぐ線を引く（プレビュー）
      markDirty();
      if(msg) msg.textContent='マーカーを引きました（スクロールで線が伸びます・保存で確定／⟲戻すで取り消し）';
    }
    var hlB=m.querySelector('#__ce_cmhl');
    if(hlB) hlB.addEventListener('click',function(){ hlWhole(curEl, m.querySelector('#__ce_cmhlc').value); });
    var hlC=m.querySelector('#__ce_cmhlc');
    if(hlC) hlC.addEventListener('input',function(){ var s=curEl&&curEl.querySelector&&curEl.querySelector('.fxa_hl'); if(s){ s.style.setProperty('--hlc',this.value); markDirty(); hlPushColorHistory(this.value); } });
    hlBindSwatches(m.querySelector('#__ce_cmhlsw'), m.querySelector('#__ce_cmhlc'), function(c){ var s=curEl&&curEl.querySelector&&curEl.querySelector('.fxa_hl'); if(s){ s.style.setProperty('--hlc',c); markDirty(); } });
    function _cmHlSpan(){ return curEl&&curEl.querySelector&&curEl.querySelector('.fxa_hl'); }
    var hlThm=m.querySelector('#__ce_cmhlthm'); if(hlThm) hlThm.addEventListener('click',function(){ fxHlThick(_cmHlSpan(),-6); });
    var hlThp=m.querySelector('#__ce_cmhlthp'); if(hlThp) hlThp.addEventListener('click',function(){ fxHlThick(_cmHlSpan(),6); });
    var hlPu=m.querySelector('#__ce_cmhlpu'); if(hlPu) hlPu.addEventListener('click',function(){ fxHlPos(_cmHlSpan(),-4); });
    var hlPd=m.querySelector('#__ce_cmhlpd'); if(hlPd) hlPd.addEventListener('click',function(){ fxHlPos(_cmHlSpan(),4); });
    var hlDm=m.querySelector('#__ce_cmhldm'); if(hlDm) hlDm.addEventListener('click',function(){ fxHlSpeed(_cmHlSpan(),0.2); });
    var hlDp=m.querySelector('#__ce_cmhldp'); if(hlDp) hlDp.addEventListener('click',function(){ fxHlSpeed(_cmHlSpan(),-0.2); });
    var hlRm=m.querySelector('#__ce_cmhlrm');
    if(hlRm) hlRm.addEventListener('click',function(ev){
      ev.preventDefault();
      var s=curEl&&curEl.querySelector&&curEl.querySelector('.fxa_hl');
      if(!s){ msg.textContent='ここにはマーカーがありません'; return; }
      var p=s.parentNode; while(s.firstChild) p.insertBefore(s.firstChild, s); p.removeChild(s);
      markDirty(); msg.textContent='マーカーを消しました（保存で確定）';
    });
    m.querySelector('#__ce_cmsg').addEventListener('click',function(){
      var b=m.querySelector('#__ce_cmsg'); b.disabled=true; b.textContent='考え中…';
      fetch('/api/camp_suggest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,section:sIdx})})
      .then(function(r){return r.json();}).then(function(dd){
        b.disabled=false; b.textContent='💡 この要素の改善案';
        if(!dd.ok)return;
        m.querySelector('#__ce_cmchips').innerHTML=(dd.suggestions||[]).map(function(s){return '<button class="chip" data-ins="'+esc(s.instruction)+'">'+esc(s.label)+'</button>';}).join('');
      }).catch(function(){b.disabled=false;b.textContent='💡 この要素の改善案';});
    });
    m.querySelector('#__ce_cmnobg').addEventListener('click',function(){ stripDeco(curEl); });
    m.querySelector('#__ce_cmdel').addEventListener('click',function(){ removeEl(curEl); });
    // 右クリック直後に自動でドラッグONにすると、cursor:moveがmousedownを奪ってしまい
    // 「文字を選んでから下線/マーカー/色」の選択操作ができなくなる（選べない＝消せない）。
    // そのため移動は「🖱 ドラッグで動かす」ボタンを押した時だけONにする（toggleDrag）。
  }, true);
  // メニュー外をクリックしたら閉じる＆選択マーカー(青点線)も消す（枠が残らないように）
  document.addEventListener('click',function(e){ if((curMenu||curEl) && !e.target.closest('#__ce_cm')) closeMenu(); }, true);
  // 保険：読み込み時に、万一残っている選択マーカーのクラスを全部剥がす
  [].slice.call(document.querySelectorAll('.__ce_sel,.__ce_hl')).forEach(function(x){ x.classList.remove('__ce_sel','__ce_hl'); });
  // ⟲戻すの基準＝今の本文を最初のスナップショットに（これ以前には戻せない＝読込直後の状態）
  _lastSnap=snapContent(); updUndoBtn();
})();
</script>
"""


# 配信時の保険：クラス名に関係なく、透明・非表示のまま残った要素を必ず表示する。
# これで「古い壊れたカンプ（出現アニメで真っ黒に消えたもの）」も、見た瞬間に直る。
_SERVE_SAFETY = """
<script>
(function(){
  var h=document.documentElement;
  /* 焼き込みアニメ(fxa)の表示：スクロールで画面に入った時に1回だけ .fxa_in を付けて再生する。
     ★時間トリガー(setTimeout)は使わない＝「スクロール位置で判断」に統一（動く時/動かない時のムラを無くす）。
     上部の要素は監視開始時に即発火＝読み込みで再生。opacityもclip-pathも .fxa_in で表示に戻る。 */
  function fxaStart(){
    if(!document.querySelector('.fxa_pre')) return;
    h.classList.add('fxa-on');
    /* ★地雷（2026-07-06修正）：ここが自前でIntersectionObserverを持つと、_EDIT_BAR側が注入する
       本家(FX_RUN・マーカーのsweepHl対応済み)と2つの監視が同時に同じ要素を見て競走することになり、
       「ヘッダーが一瞬で終わって見えない／マーカーひいたタイミングでヘッダーがまた動く」ような
       ムラ・二重再生の原因になっていた。_EDIT_BARは必ず一緒に注入され、読み込み時に必ずFX_RUNを
       張り直すので、まずそちらに任せる＝ここでは少し待ってFX_RUNが無い時だけの保険にする。 */
    setTimeout(function(){
      if(document.getElementById('fxa-run')) return;  // 本家(FX_RUN)が動いている＝そちらに任せて何もしない
      /* ★フォールバックは fxa_hl(マーカー) も対象にする。本家のようにrAFで伸ばす芸は持たないが、
         --hlw:100 を入れて「少なくとも引かれた状態」にする（0のままだと一生マーカーが出ない）。 */
      function show(t){ if(t.classList.contains('fxa_hl')) t.style.setProperty('--hlw',100); t.classList.add('fxa_in'); }
      function all(){ return [].slice.call(document.querySelectorAll('.fxa_pre:not(.fxa_in),.fxa_hl:not(.fxa_in)')); }
      if(!('IntersectionObserver' in window)){ all().forEach(show); return; }
      var io=new IntersectionObserver(function(es){ es.forEach(function(en){ if(en.isIntersecting){ var t=en.target; io.unobserve(t); var cd=t.getAttribute('data-cedelay'); if(cd!=null){ setTimeout(function(){ show(t); }, +cd); } else { show(t); } } }); }, {threshold:0, rootMargin:'0px 0px -18% 0px'});
      all().forEach(function(el){ io.observe(el); });
    }, 50);
  }
  /* 押した時だけ出す隠しメニュー/オーバーレイ（fixed/absoluteで隠されている）を判定。
     ここに含まれる要素は「本来ずっと隠れているもの」なので、保険で強制表示しない
     （＝クローンでMENUのメガメニューが開いた状態で残る不具合を防ぐ）。 */
  function _inHiddenOverlay(e){
    var n=e;
    while(n && n!==document.body){
      var s=getComputedStyle(n);
      if((s.position==='fixed'||s.position==='absolute') && (parseFloat(s.opacity)===0 || s.visibility==='hidden')) return true;
      n=n.parentElement;
    }
    return false;
  }
  /* 従来の保険：透明/非表示のまま残った要素を強制表示（fxaは上の監視(IntersectionObserver)が担当するので触らない）。 */
  function sweep(){
    var all=document.querySelectorAll('body *');
    for(var i=0;i<all.length;i++){
      var e=all[i];
      if(e.closest('#__ce')||e.closest('#__ce_cm')||e.closest('#__ce_pk')||e.closest('#__ce_toast')) continue;
      /* fxaの焼き込みアニメ(要素・その中の文字span・ラッパー含む)は監視が担当するので、掃除は一切触らない。
         ★特に .fxa_ch(1文字span)を強制表示すると、下部のタイプライター/一文字ずつが「出た状態で固定」され再生されない。 */
      if(e.closest('.fxa_pre')||e.closest('.fxa_wrap')) continue;
      if(e.classList&&(e.classList.contains('__cl_pre')||e.classList.contains('__cl_kid'))) continue; /* クローンのスクロール出現は自前の保険があるので触らない */
      var cs=getComputedStyle(e);
      var hidden=(parseFloat(cs.opacity)===0)||(cs.visibility==='hidden');
      if(hidden && _inHiddenOverlay(e)) continue; /* MENU等「開いたら出す」隠しメニューは無理に表示しない */
      if(parseFloat(cs.opacity)===0){ e.style.setProperty('opacity','1','important'); e.style.transform='none'; e.style.animation='none'; }
      if(cs.visibility==='hidden'){ e.style.setProperty('visibility','visible','important'); }
      var cp=cs.clipPath||cs.webkitClipPath||'';  /* clip-pathで切り取られて消えている(fxa以外)ものも復活 */
      if(cp && cp!=='none' && /100%|inset\\(1/.test(cp)){ e.style.setProperty('clip-path','none','important'); e.style.setProperty('-webkit-clip-path','none','important'); }
    }
  }
  function run(){ fxaStart(); setTimeout(sweep, 2200); }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', run); else run();
})();
</script>
"""


_LETTER_SPLIT_RE = re.compile(r"(\w+)\.innerHTML\.split\(")
# `var title = X.querySelector(...); if(title){` … の入口を捕まえる（分割の始まり）
_LETTER_GUARD_RE = re.compile(
    r"(var\s+(\w+)\s*=\s*\w+\.querySelector\([^;]*\);\s*)if\s*\(\s*\2\s*\)\s*\{"
)


def _guard_letter_splitters(html: str) -> str:
    """一括改善(GPT)が作る『見出しを1文字ずつspanに分割するJS』の事故を無害化する。

    その手のJSは `X.innerHTML.split(...)` で自分の出力を読み直して作り直すため、再読込のたびに
    (1) `&nbsp;`(実体参照)を「&nbsp;」という文字列として再分割して文字化けし、
    (2) その要素に後から付けた別アニメ(私のfxa_ch等)を毎回作り直しで消してしまう。

    対策は2段構え（どちらも既存ファイルを配信時に自己修復する）：
    ① 分割の入口 `if(title){` を「まだ分割していない時だけ」に変える。
       ＝ 既に .imp-char や .fxa_ch があるなら作り直さない → 文字化けも、私のアニメ消しも起きない。
       見出し本体の色/遅延/is-visible演出は既にHTMLに焼けているので、作り直さなくても動く。
    ② 万一①をすり抜けて作り直しが走っても、既に分割済みなら innerHTML でなく textContent から読む
       （textContentは実体参照デコード済み＝空白U+00A0なので文字化けしない）。
    分割していない普通のスクリプトは .imp-char を持たないので挙動は変わらない（安全）。
    """
    html = _LETTER_GUARD_RE.sub(
        lambda m: f"{m.group(1)}if({m.group(2)} && !{m.group(2)}.querySelector('.imp-char,.fxa_ch'))" + "{",
        html,
    )
    html = _LETTER_SPLIT_RE.sub(
        lambda m: f"({m.group(1)}.querySelector('.imp-char')?{m.group(1)}.textContent:{m.group(1)}.innerHTML).split(",
        html,
    )
    return html


_FXA_RUN_RE = re.compile(r'<script id="fxa-run">.*?</script>', re.DOTALL)
_SAFE_BLOCK_RE = re.compile(re.escape(camp._SAFE_START) + r".*?" + re.escape(camp._SAFE_END), re.DOTALL)


def _inject_edit_bar(html: str, filename: str) -> str:
    """カンプHTMLの末尾に編集バーを差し込む（</body>直前）。あわせて保険を注入。"""
    html = _guard_letter_splitters(html)  # 文字化けする文字分割JSを無害化（既存ファイルも自己修復）
    # 焼き込み済みの古い再生スクリプト(fxa-run)を除去。古い版は時間トリガー/scrollリスナーで「動くムラ」を出すため、
    # 配信時に消して、編集バー側が最新版(スクロールで1回だけ再生)を注入し直す＝既存ファイルも安定する。
    html = _FXA_RUN_RE.sub("", html)
    # 焼き込み済みの「全部見える保険」(_REVIEW_FALLBACK)を最新版に差し替える。
    # 古い版は2.5秒後にfxaの文字span(fxa_ch)まで強制表示し、タイプライター等が"出た状態で固定"される不具合があった。
    # 最新版はfxa要素を除外する→既存ファイルもこの差し替えで直る。
    html = _SAFE_BLOCK_RE.sub(lambda m: camp._REVIEW_FALLBACK, html)
    bar = _SERVE_SAFETY + _EDIT_BAR.replace("%FILE_JSON%", _json.dumps(filename)).replace(
        "%EDIT_PROVIDER_JSON%", _json.dumps(config.CONFIG.htmlgen.edit_provider))
    low = html.lower()
    if "</body>" in low:
        i = low.rfind("</body>")
        return html[:i] + bar + html[i:]
    return html + bar


@app.route("/camp/<path:filename>")
def camp_file(filename: str):
    """生成したカンプHTMLを返す（編集バー付き＝見ながらその場で直せる）。"""
    path = config.CAMP_DIR / filename
    if not path.exists() or not path.is_file():
        abort(404)
    if path.suffix != ".html":
        # クローンの素材（<名前>_files/画像・フォント等）はそのまま返す
        return send_file(path)
    html = _inject_edit_bar(path.read_text(encoding="utf-8"), filename)
    return Response(html, mimetype="text/html")


@app.route("/img/<site_id>/<which>")
def img(site_id: str, which: str):
    """スクショ画像を返す。which は firstview / fullpage。"""
    column = "firstview_path" if which != "fullpage" else "fullpage_path"
    with db.connect() as conn:
        row = db.get_site(conn, site_id)
    if not row or not row[column]:
        abort(404)
    path = config.PROJECT_ROOT / row[column]
    if not path.exists():
        abort(404)
    return send_file(path)


@app.route("/video/<site_id>")
def video(site_id: str):
    """スクロール録画(webm)を返す。"""
    with db.connect() as conn:
        row = db.get_site(conn, site_id)
    if not row or not row["animation_video_path"]:
        abort(404)
    path = config.PROJECT_ROOT / row["animation_video_path"]
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="video/webm")


def serve(host: str = "127.0.0.1", port: int = 5000, preload: bool = True) -> None:
    """ビューアを起動する。preload=True で起動時にモデルを読み込んでおく。

    preload=False（--no-preload・このPCの推奨）でも、起動して少し経ったら
    バックグラウンドでこっそり読み込む＝初回検索の「遅い」を体感ゼロに近づける。
    読み込みがメモリ不足で失敗しても握りつぶす（従来どおり初回検索時に再挑戦される）。
    """
    db.init_db()
    if preload:
        log.info("モデルを先読みします（起動後の初回検索を速くするため）…")
        _EMBEDDER.load()
    else:
        def _warmup():
            import time as _time
            _time.sleep(5)  # まず画面を開ける方を優先。落ち着いてから読み込む
            try:
                log.info("モデルをバックグラウンドで先読み中…（初回検索を速くするため）")
                _EMBEDDER.load()
                log.info("モデル先読み完了。検索はすぐ返ります")
            except Exception:  # noqa: BLE001
                log.exception("バックグラウンド先読みに失敗（初回検索時に再挑戦します）")
        threading.Thread(target=_warmup, daemon=True).start()
    log.info("ビューア起動: http://%s:%d  （Ctrl+C で停止）", host, port)
    # Flask開発サーバは POST + keep-alive で接続が切れて「Failed to fetch」になりやすい。
    # 頑丈な waitress（本番品質のWSGIサーバ）で配信する。複数スレッドで並行処理もOK。
    try:
        from waitress import serve as waitress_serve
        waitress_serve(app, host=host, port=port, threads=8, channel_timeout=300)
    except ImportError:
        log.warning("waitress が無いので開発サーバで起動します")
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
