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
import re
import threading
import uuid
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file

from . import anim, assets, camp, config, db, embed, ingest, search, vibe
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


def _test_key(provider: str, openai_key: str = "", anthropic_key: str = "", deepseek_key: str = "") -> tuple[bool, str]:
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


def _test_all(provider: str, openai_key: str = "", anthropic_key: str = "", gemini_key: str = "", deepseek_key: str = "") -> tuple[bool, str]:
    """生成エンジン＋修正エンジン（DeepSeek等）＋説明づけ（Gemini）をまとめてテストしメッセージを組む。"""
    ok, msg = _test_key(provider, openai_key, anthropic_key, deepseek_key)
    parts = [("✅ " if ok else "⚠ ") + "生成: " + msg]
    # 修正エンジンが生成と別なら、それも確認する（例：生成=GPT／修正=DeepSeek）
    edit_provider = config.CONFIG.htmlgen.edit_provider
    if edit_provider and edit_provider != provider:
        eok, emsg = _test_key(edit_provider, openai_key, anthropic_key, deepseek_key)
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
    )
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    """設定画面からの保存。.env に書き込み、即反映する（キー本体は空なら据え置き）。"""
    data = request.get_json(silent=True) or {}
    updates = {}
    if data.get("provider") in ("anthropic", "openai", "gemini", "deepseek"):
        updates["DESIGN_STOCK_HTML_PROVIDER"] = data["provider"]
    if data.get("edit_provider") in ("anthropic", "openai", "gemini", "deepseek"):
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
        }
    )


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
        prov = "GPT" if config.CONFIG.htmlgen.provider == "openai" else "Claude"
        _camp_set(job_id, phase=f"{prov}がHTMLを書いています…（一番長い段階・2分前後）")
        result = camp.generate_camp(
            brief, use_model=False,
            base_site_id=base_site_id or None,
            anim_ref_id=anim_ref_id or None,
        )
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


def _run_edit_job(job_id: str, fn: str, section: int, instruction: str) -> None:
    """バックグラウンドでカンプを部分編集する（生成ジョブ一覧に相乗り）。"""
    try:
        _ep = config.CONFIG.htmlgen.edit_provider
        prov = {"openai": "GPT", "gemini": "Gemini", "deepseek": "DeepSeek"}.get(_ep, "Claude")
        scope = "全体" if section is None or section < 0 else f"セクション{section + 1}"
        _camp_set(job_id, phase=f"{prov}が{scope}を直しています…")
        result = camp.edit_camp_section(fn, section, instruction)
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
    with _CAMP_LOCK:
        running = sum(1 for j in _CAMP_JOBS.values() if j.get("state") == "running")
        if running >= _CAMP_MAX:
            return jsonify({"ok": False, "message": f"同時処理は最大{_CAMP_MAX}件までです（少し待って）"}), 429
        job_id = uuid.uuid4().hex
        _CAMP_JOBS[job_id] = {"state": "running", "brief": f"部分編集: {instruction[:24]}", "phase": "開始しています…"}
    log.info("部分編集ジョブ開始[%s]: %s section=%s / %s", job_id[:6], fn, section, instruction)
    threading.Thread(
        target=_run_edit_job, args=(job_id, fn, section, instruction), daemon=True
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
    for p in sorted(config.CAMP_DIR.glob("camp_*.html")):
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
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True})


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
        p.write_text(html, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True, "file": fn})


@app.route("/api/camp_sections")
def api_camp_sections():
    """カンプのセクション一覧（編集バーのドロップダウン用）。"""
    fn = (request.args.get("file") or "").strip()
    if not fn or not (config.CAMP_DIR / fn).exists():
        return jsonify({"ok": False, "sections": []}), 404
    html = (config.CAMP_DIR / fn).read_text(encoding="utf-8")
    return jsonify({"ok": True, "sections": camp.list_camp_sections(html)})


# カンプ画面の隅に出す編集バー（ツール経由で開いた時だけ注入。保存ファイルは汚さない）
_EDIT_BAR = """
<style>
#__ce{position:fixed;right:20px;bottom:20px;z-index:2147483000;width:480px;max-width:94vw;background:#fff;border:1px solid #e3e3e8;border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,.32);font-family:system-ui,-apple-system,sans-serif;color:#1d1d1f}
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
#__ce_pk{position:fixed;inset:0;z-index:2147483001;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center}
#__ce_pk .bx{background:#fff;border-radius:12px;padding:16px;max-width:640px;width:92%;max-height:80vh;overflow:auto;font-family:system-ui,sans-serif}
#__ce_pk h4{margin:0 0 12px;font-size:15px}
#__ce_pk .cl{float:right;cursor:pointer;font-size:18px;font-weight:700;color:#888}
#__ce_pk .gr{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:10px}
#__ce_pk .it{border:1px solid #eee;border-radius:8px;overflow:hidden;cursor:pointer;background:#fff}
#__ce_pk .it:hover{border-color:#2b6cb0;box-shadow:0 4px 12px rgba(0,0,0,.15)}
#__ce_pk .it img{width:100%;height:80px;object-fit:cover;display:block;background:#eef2f7}
#__ce_pk .it span{display:block;font-size:11px;color:#555;padding:4px 6px}
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
#__ce_cm .__ce_nudge{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin:4px 0 6px}
#__ce_cm .__ce_nudge button{background:#eef3ff;border:1px solid #cfe0fb;border-radius:7px;padding:8px 0;font-size:14px;cursor:pointer;color:#1d1d1f;font-weight:700}
#__ce_cm .__ce_nudge button:hover{background:#dceafe}
#__ce_cm .__ce_nudge .sp{visibility:hidden}
#__ce_cm .__ce_size{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:2px 0 8px}
#__ce_cm .__ce_size button{background:#eef3ff;border:1px solid #cfe0fb;border-radius:7px;padding:8px 0;font-size:13px;cursor:pointer;color:#1d1d1f;font-weight:700}
#__ce_cm .__ce_size button:hover{background:#dceafe}
#__ce_savebar{position:fixed;left:20px;bottom:20px;z-index:2147483003;background:#1a7f37;color:#fff;border:none;border-radius:12px;padding:13px 20px;font-size:14px;font-weight:700;cursor:pointer;box-shadow:0 12px 32px rgba(0,0,0,.3);font-family:system-ui,sans-serif;display:none}
#__ce_savebar.show{display:block}
#__ce_toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);z-index:2147483004;background:#1d1d1f;color:#fff;border-radius:14px;padding:15px 24px;box-shadow:0 16px 44px rgba(0,0,0,.42);font-family:system-ui,sans-serif;min-width:320px;max-width:92vw;text-align:center}
#__ce_toast .bar{height:7px;background:#43434a;border-radius:4px;overflow:hidden;margin-bottom:9px}
#__ce_toast .bar span{display:block;height:100%;width:35%;background:#ff9a3c;border-radius:4px;animation:__ce_flow 1.2s ease-in-out infinite}
@keyframes __ce_flow{0%{margin-left:-35%}100%{margin-left:100%}}
#__ce_toast .tx{font-size:14.5px;font-weight:700}
</style>
<div id="__ce">
  <div class="hd" id="__ce_hd"><span>✏</span><span class="t">このカンプを直す</span><span class="sv" id="__ce_save">⭐ 名前を付けて保存</span><span class="x" id="__ce_mn">✕ 閉じる</span></div>
  <div class="bd">
    <div class="lbl plain">① どこを直す？</div>
    <select id="__ce_sec"><option value="-1">ページ全体</option></select>
    <div class="lbl">🎬 アニメ・背景装飾を付ける（選んだ所に適用）</div>
    <div class="ags" id="__ce_ags"></div>
    <div class="lbl">💡 選んだ所の改善案（AIが画面を見てたくさん提案）</div>
    <div class="row"><button class="sg" id="__ce_sg">💡 この部分の案を出す</button></div>
    <div class="chips" id="__ce_chips"></div>
    <div class="lbl">✍ 自分で指示</div>
    <div class="row"><input id="__ce_in" placeholder="例：見出しを大きく／CTAを黄色に"><button class="go" id="__ce_go">直す</button></div>
    <button class="im" id="__ce_img">🖼 画像を差し替え（AIなし・無料）</button>
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
  // ヘッダを掴んでウィンドウ自体を移動できる（クリックでの開閉と両立：動いた時だけトグルを抑制）
  var hd=document.getElementById('__ce_hd'), hDrag=false, hMoved=false, hSX=0,hSY=0,hL=0,hT=0;
  hd.addEventListener('mousedown',function(e){
    if(e.target.closest('#__ce_save')) return;  // 保存ボタンは除外
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
  // ⭐名前を付けて保存（お気に入り登録）。壊れる前の良い版に名前を付けて残せる。
  var saveBtn=document.getElementById('__ce_save');
  saveBtn.addEventListener('click',function(ev){
    ev.stopPropagation();  // ヘッダのトグル(開閉)と競合させない
    if(_dirty){ saveLayout(); return; }  // 位置/大きさを動かしていたら、それをHTMLに焼き込む
    var cur=(document.title||'').trim();
    var name=window.prompt('このカンプに名前を付けて保存します（履歴の上に「⭐名前」で残ります）\\n例：ヒーロー完成版 / 配色OK版',cur);
    if(name===null) return;  // キャンセル
    saveBtn.textContent='保存中…';
    fetch('/api/camp_name',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,name:name})})
      .then(function(r){return r.json();}).then(function(d){
        if(d.ok && d.name){ saveBtn.textContent='✅ 保存「'+d.name+'」'; saveBtn.classList.add('saved'); }
        else { saveBtn.textContent='⭐ 名前を付けて保存'; saveBtn.classList.remove('saved'); }
      }).catch(function(){ saveBtn.textContent='⭐ 名前を付けて保存'; });
  });
  var esc=function(s){return String(s||'').replace(/[&<>"]/g,function(c){return({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]);});};
  fetch('/api/camp_sections?file='+encodeURIComponent(FILE)).then(function(r){return r.json();}).then(function(d){
    (d.sections||[]).forEach(function(s){var o=document.createElement('option');o.value=s.index;o.textContent=(s.index+1)+'. '+s.label;sec.appendChild(o);});
  }).catch(function(){});
  // アニメのプリセット（説明つき・クリックで選んだ所に適用）
  var PRESETS=[
   {b:'ふわっと出現',d:'スクロールで下から浮かぶ',i:'このセクションの主要な要素に、スクロールで画面に入ったら下から少し上へ動きながらフェードインする出現アニメを付ける。複数要素は時間差(stagger)で。IntersectionObserverで実装し、html.jsが付いた時だけ初期非表示にする保険を入れてJSが無くても中身が見えるようにする'},
   {b:'ホバーで浮く',d:'カード/ボタンが浮く',i:'このセクションのカードやボタンに、ホバーで少し浮き上がり影が濃くなる滑らかなtransitionを付ける'},
   {b:'画像ズーム',d:'画像がゆっくり拡大',i:'このセクションの画像に、ホバー時にゆっくり拡大するズーム効果を付ける（枠はoverflow:hidden、imgはobject-fit:coverではみ出さない）'},
   {b:'横からスライド',d:'左右からスッと登場',i:'このセクションの要素を、スクロールで画面に入ったら左右から滑り込んで現れるアニメにする（保険付き）'},
   {b:'見出しを強調',d:'タイトルが順に出る',i:'このセクションの見出しを、表示時に単語ごとに少しずつ現れる軽いアニメにする'},
   {b:'背景グラデ',d:'背景色がゆっくり流れる',i:'このセクションの背景に、ゆっくり色が移動するグラデーションアニメ(@keyframes)を付ける'},
   {b:'波の区切り',d:'セクション境界に波',i:'このセクションの下端に、SVGの波型の区切り装飾を入れる'},
   {b:'パララックス',d:'背景がゆっくり動く',i:'このセクションの背景に、スクロールに合わせてゆっくり視差移動する簡易パララックスをCSS/素のJSで付ける'},
   {b:'数字カウント',d:'数値が0から増える',i:'このセクションに実績や料金などの数値があれば、表示時に0から目標値までカウントアップするアニメを付ける'},
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
  function submit(section,instruction){
    if(!instruction){msg.textContent='指示が空です';return;}
    // ページ全体(-1)は"全文を書き直す"＝高い(数十円)・遅い。特定箇所なら安い(数円)。
    if(Number(section)<0){
      if(!confirm('⚠ これは「ページ全体を書き直す」修正です。\\n時間がかかり、料金も高め（数十円〜）になります。\\n\\n特定の場所だけ直すなら【キャンセル】して、\\n・①で直すセクションを選ぶ か\\n・直したい所を右クリック\\nすると安く（数円）速く直せます。\\n\\nこのままページ全体を直しますか？')) { msg.textContent='キャンセルしました（①でセクションを選ぶと安いです）'; return; }
    }
    busy(true); msg.textContent='生成中…'; showToast('AIが直しています…（十数秒〜）');
    fetch('/api/edit_camp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,section:Number(section),instruction:instruction})})
    .then(function(r){return r.json();}).then(function(d){
      if(!d.ok){msg.textContent='失敗：'+d.message;busy(false);hideToast();return;}
      poll(d.job_id);
    }).catch(function(){msg.textContent='通信エラー';busy(false);hideToast();});
  }
  function poll(id){
    fetch('/api/generate_camp/status').then(function(r){return r.json();}).then(function(d){
      var j=(d.jobs||{})[id];
      if(!j){setTimeout(function(){poll(id);},1200);return;}
      if(j.state==='running'){msg.textContent=(j.phase||'生成中…');setToast(j.phase||'AIが直しています…');setTimeout(function(){poll(id);},1200);}
      else if(j.state==='done'){setToast('✅ できました。開きます…');msg.textContent='できました。開きます…';location.href='/camp/'+j.file;}
      else{msg.textContent='失敗：'+(j.message||'');busy(false);hideToast();}
    }).catch(function(){setTimeout(function(){poll(id);},1500);});
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
  document.addEventListener('click',function(e){
    if(!imgMode) return;
    var im=e.target.closest('img'); if(!im||im.closest('#__ce')||im.closest('#__ce_pk')) return;
    e.preventDefault(); e.stopPropagation();
    openPicker(camImgs().indexOf(im));
  }, true);
  function openPicker(idx){
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
        ov.remove(); msg.textContent='差し替え中…';
        fetch('/api/swap_image',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,index:idx,src:it.dataset.src})})
        .then(function(r){return r.json();}).then(function(d){
          if(!d.ok){msg.textContent='失敗：'+d.message;return;}
          location.href='/camp/'+d.file;
        }).catch(function(){msg.textContent='通信エラー';});
      });
    }).catch(function(){msg.textContent='画像一覧の取得に失敗';});
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
  // ===== 右クリックで、その要素に直接アニメ/指示/改善案を出す =====
  var curMenu=null, curEl=null;
  function closeMenu(){ if(curMenu){curMenu.remove();curMenu=null;} if(curEl){curEl.classList.remove('__ce_sel');curEl=null;} }
  // ===== 位置の直接調整（AIなし・transformで即反映）=====
  // 要素を translate で浮かせて動かす。元のtransformは data-cebt に退避して壊さない。
  // 元のtransformを1度だけ退避（自前のtranslate/scaleが無い時だけ）
  function _cebt(el){ if(el.getAttribute('data-cebt')===null){ var t=el.style.transform||''; el.setAttribute('data-cebt', (t.indexOf('translate')<0&&t.indexOf('scale')<0)?t:''); } }
  // 位置(translate)＋大きさ(scaleX,scaleY)をまとめて当てる。アニメ/トランジションは止めて最優先で反映。
  function applyTf(el){
    var x=+el.getAttribute('data-cetx')||0, y=+el.getAttribute('data-cety')||0;
    var sx=+el.getAttribute('data-cesx')||1, sy=+el.getAttribute('data-cesy')||1;
    el.style.setProperty('transform-origin','top left','important');
    el.style.setProperty('transform','translate('+x+'px,'+y+'px) scale('+sx+','+sy+') '+(el.getAttribute('data-cebt')||''),'important');
    el.style.setProperty('animation','none','important');
    el.style.setProperty('transition','none','important');
    markDirty();
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
  function resetPos(el){
    el.style.removeProperty('transform'); el.style.removeProperty('transform-origin'); el.style.removeProperty('animation'); el.style.removeProperty('transition');
    var bt=el.getAttribute('data-cebt'); if(bt) el.style.transform=bt;
    el.removeAttribute('data-cetx'); el.removeAttribute('data-cety'); el.removeAttribute('data-cesx'); el.removeAttribute('data-cesy'); el.removeAttribute('data-cebt');
    markDirty();
  }
  // ドラッグで動かす：対象要素に直接 mousedown を付ける（確実に掴める）
  var dragEl=null, dActive=false, dSX=0,dSY=0,dOX=0,dOY=0;
  function _dDown(e){
    dActive=true; dSX=e.clientX; dSY=e.clientY;
    dOX=+dragEl.getAttribute('data-cetx')||0; dOY=+dragEl.getAttribute('data-cety')||0;
    document.body.style.userSelect='none'; e.preventDefault(); e.stopPropagation();
  }
  document.addEventListener('mousemove',function(e){ if(dActive&&dragEl) setPos(dragEl, dOX+(e.clientX-dSX), dOY+(e.clientY-dSY)); },true);
  document.addEventListener('mouseup',function(){ if(dActive){ dActive=false; document.body.style.userSelect=''; } },true);
  function toggleDrag(el,btn){
    if(dragEl===el){ // 同じ要素をもう一度押したら終了
      el.removeEventListener('mousedown',_dDown,true); el.style.cursor=''; dragEl=null;
      if(btn) btn.textContent='🖱 ドラッグで動かす（押して開始/終了）'; msg.textContent=''; return;
    }
    if(dragEl){ dragEl.removeEventListener('mousedown',_dDown,true); dragEl.style.cursor=''; }
    dragEl=el; el.style.cursor='move'; el.addEventListener('mousedown',_dDown,true);
    if(btn) btn.textContent='✋ ドラッグ終了';
    msg.textContent='要素を掴んで動かせます（もう一度押すと終了）';
  }
  // 位置/大きさを変えたら、ヘッダの保存ボタンを「💾 変更を保存」に変えて緑で目立たせる（ボタンは1つに統一）
  var _dirty=false;
  function markDirty(){
    _dirty=true;
    var b=document.getElementById('__ce_save');
    if(b){ b.textContent='💾 変更を保存'; b.classList.add('saved'); }
  }
  function cleanHtml(){
    var doc=document.documentElement.cloneNode(true);
    ['#__ce','#__ce_cm','#__ce_pk','#__ce_toast','#__ce_savebar'].forEach(function(sel){
      [].slice.call(doc.querySelectorAll(sel)).forEach(function(n){n.remove();});
    });
    [].slice.call(doc.querySelectorAll('.__ce_sel,.__ce_hl')).forEach(function(n){n.classList.remove('__ce_sel','__ce_hl');});
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
  function applyEl(sIdx, instruction){ closeMenu(); box.classList.remove('min'); submit(sIdx, instruction); }
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
  document.addEventListener('contextmenu',function(e){
    var el=pickTarget(e.target);
    if(!el||el.closest('#__ce')||el.closest('#__ce_cm')||el.closest('#__ce_pk')) return;
    e.preventDefault(); closeMenu();
    curEl=el; el.classList.add('__ce_sel');
    var sIdx=secIndexOf(el), d=descEl(el);
    // 画像なら「AIなしの即差し替え」を最優先で出す（画像のAI指示は不安定なため）
    var imgEl = (el.tagName==='IMG') ? el : (el.querySelector ? el.querySelector('img') : null);
    var swapH = imgEl
      ? '<button class="go2" id="__ce_cmswap" style="background:#1a7f37;margin-bottom:6px">🖼 この画像を差し替え（AIなし・一瞬）</button>'
        +'<button class="go2" id="__ce_cmbg" style="background:#0b6bcb;margin-bottom:6px">🎨 この画像の背後に画像を敷く（水彩など）</button>'
        +'<div class="cap" style="margin:0 0 8px">画像はこれが確実です（差し替えは一瞬・背景敷きはAIで数秒）</div>'
      : '';
    var agh=PRESETS.map(function(p,i){return '<button class="ag2" data-i="'+i+'"><b>'+esc(p.b)+'</b><span>'+esc(p.d)+'</span></button>';}).join('');
    var m=document.createElement('div'); m.id='__ce_cm';
    m.innerHTML='<div class="h"><span class="t">'+esc(d)+'</span><span class="c" id="__ce_cmx">✕</span></div>'
      +'<div class="bd2">'+swapH
      +'<div class="cap">🖱 位置を動かす（AIなし・即反映）</div>'
      +'<div class="__ce_nudge"><span class="sp"></span><button data-nx="0" data-ny="-6">↑</button><span class="sp"></span>'
      +'<button data-nx="-6" data-ny="0">←</button><button data-rst="1">⟲</button><button data-nx="6" data-ny="0">→</button>'
      +'<span class="sp"></span><button data-nx="0" data-ny="6">↓</button><span class="sp"></span></div>'
      +'<div class="__ce_size">'
      +'<button data-sx="1.1" data-sy="1.1">＋ 大きく</button><button data-sx="0.909" data-sy="0.909">－ 小さく</button>'
      +'<button data-sx="1.1" data-sy="1">⇔ 横に長く</button><button data-sx="0.909" data-sy="1">⇔ 横を縮め</button>'
      +'<button data-sx="1" data-sy="1.1">⇕ 縦に長く</button><button data-sx="1" data-sy="0.909">⇕ 縦を縮め</button></div>'
      +'<button class="go2" id="__ce_cmdrag" style="background:#0b6bcb;margin-bottom:8px">🖱 ドラッグで動かす（押して開始/終了）</button>'
      +'<div class="cap">🎬 この要素に付ける</div>'+agh
      +'<div class="cap" style="margin-top:8px">✍ この要素に自分で指示</div>'
      +'<input id="__ce_cmin" placeholder="例：もっと大きく赤く"><button class="go2" id="__ce_cmgo">この要素を直す</button>'
      +'<button class="go2" style="background:#4b2ea8" id="__ce_cmsg">💡 この要素の改善案</button>'
      +'<div class="chips" id="__ce_cmchips"></div></div>';
    document.body.appendChild(m);
    var mw=290, mh=Math.min(window.innerHeight*0.72, m.offsetHeight||420);
    m.style.left=Math.max(10,Math.min(e.clientX, window.innerWidth-mw-10))+'px';
    m.style.top=Math.max(10,Math.min(e.clientY, window.innerHeight-mh-10))+'px';
    curMenu=m;
    var tail=' ★重要：この要素だけに適用し、他の要素や他のセクションは一切変えない。対象要素は「'+d+'」。アニメはCSS/素のJSで実装し、スクロール出現系はhtml.jsが付いた時だけ初期非表示にする保険を入れてJSが無くても中身が見えるようにする。';
    m.querySelector('.bd2').addEventListener('click',function(ev){
      var nb=ev.target.closest('.__ce_nudge button');
      if(nb){ if(nb.getAttribute('data-rst')) resetPos(curEl); else nudge(curEl, +nb.getAttribute('data-nx'), +nb.getAttribute('data-ny')); return; }
      var sb=ev.target.closest('.__ce_size button');
      if(sb){ scaleBy(curEl, +sb.getAttribute('data-sx'), +sb.getAttribute('data-sy')); return; }
      var dg=ev.target.closest('#__ce_cmdrag');
      if(dg){ toggleDrag(curEl, dg); return; }
      var ag=ev.target.closest('.ag2');
      if(ag){ var pp=PRESETS[+ag.dataset.i];
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
      var idx=camImgs().indexOf(imgEl); closeMenu();
      if(idx<0){ alert('この画像は差し替え対象外です（背景画像かもしれません）'); return; }
      openPicker(idx);
    }); }
    var bgBtn=m.querySelector('#__ce_cmbg');
    if(bgBtn){ bgBtn.addEventListener('click',function(){
      var ie=imgEl, si=sIdx; closeMenu(); openBgPicker(ie, si);
    }); }
    m.querySelector('#__ce_cmgo').addEventListener('click',function(){
      var v=m.querySelector('#__ce_cmin').value.trim(); if(v) applyEl(sIdx, v+tail);
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
  });
  // メニュー外をクリックしたら閉じる＆選択マーカー(青点線)も消す（枠が残らないように）
  document.addEventListener('click',function(e){ if((curMenu||curEl) && !e.target.closest('#__ce_cm')) closeMenu(); }, true);
  // 保険：読み込み時に、万一残っている選択マーカーのクラスを全部剥がす
  [].slice.call(document.querySelectorAll('.__ce_sel,.__ce_hl')).forEach(function(x){ x.classList.remove('__ce_sel','__ce_hl'); });
})();
</script>
"""


# 配信時の保険：クラス名に関係なく、透明・非表示のまま残った要素を必ず表示する。
# これで「古い壊れたカンプ（出現アニメで真っ黒に消えたもの）」も、見た瞬間に直る。
_SERVE_SAFETY = """
<script>
(function(){
  function sweep(){
    var all=document.querySelectorAll('body *');
    for(var i=0;i<all.length;i++){
      var e=all[i];
      if(e.closest('#__ce')||e.closest('#__ce_cm')||e.closest('#__ce_pk')||e.closest('#__ce_toast')) continue;
      var cs=getComputedStyle(e);
      if(parseFloat(cs.opacity)===0){ e.style.setProperty('opacity','1','important'); e.style.transform='none'; e.style.animation='none'; }
      if(cs.visibility==='hidden'){ e.style.setProperty('visibility','visible','important'); }
    }
  }
  function run(){ setTimeout(sweep, 2200); }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', run); else run();
})();
</script>
"""


def _inject_edit_bar(html: str, filename: str) -> str:
    """カンプHTMLの末尾に編集バーを差し込む（</body>直前）。あわせて保険を注入。"""
    bar = _SERVE_SAFETY + _EDIT_BAR.replace("%FILE_JSON%", _json.dumps(filename))
    low = html.lower()
    if "</body>" in low:
        i = low.rfind("</body>")
        return html[:i] + bar + html[i:]
    return html + bar


@app.route("/camp/<path:filename>")
def camp_file(filename: str):
    """生成したカンプHTMLを返す（編集バー付き＝見ながらその場で直せる）。"""
    path = config.CAMP_DIR / filename
    if not path.exists() or path.suffix != ".html":
        abort(404)
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
    """ビューアを起動する。preload=True で起動時にモデルを読み込んでおく。"""
    db.init_db()
    if preload:
        log.info("モデルを先読みします（起動後の初回検索を速くするため）…")
        _EMBEDDER.load()
    log.info("ビューア起動: http://%s:%d  （Ctrl+C で停止）", host, port)
    # Flask開発サーバは POST + keep-alive で接続が切れて「Failed to fetch」になりやすい。
    # 頑丈な waitress（本番品質のWSGIサーバ）で配信する。複数スレッドで並行処理もOK。
    try:
        from waitress import serve as waitress_serve
        waitress_serve(app, host=host, port=port, threads=8, channel_timeout=300)
    except ImportError:
        log.warning("waitress が無いので開発サーバで起動します")
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
