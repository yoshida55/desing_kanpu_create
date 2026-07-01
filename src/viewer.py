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
import threading
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

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
            "openai_model": h.openai_model,
            "openai_set": h.openai_enabled,
            "anthropic_set": v.enabled,
        }
    )


def _test_key(provider: str, openai_key: str = "", anthropic_key: str = "") -> tuple[bool, str]:
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


@app.route("/api/test_key", methods=["POST"])
def api_test_key():
    """選択中プロバイダのキーで接続テストする（入力欄のキーがあればそれを使う）。"""
    data = request.get_json(silent=True) or {}
    provider = data.get("provider") or config.CONFIG.htmlgen.provider
    ok, msg = _test_key(
        provider, data.get("openai_api_key", ""), data.get("anthropic_api_key", "")
    )
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    """設定画面からの保存。.env に書き込み、即反映する（キー本体は空なら据え置き）。"""
    data = request.get_json(silent=True) or {}
    updates = {}
    if data.get("provider") in ("anthropic", "openai"):
        updates["DESIGN_STOCK_HTML_PROVIDER"] = data["provider"]
    if (data.get("openai_model") or "").strip():
        updates["DESIGN_STOCK_OPENAI_MODEL"] = data["openai_model"].strip()
    if (data.get("openai_api_key") or "").strip():
        updates["OPENAI_API_KEY"] = data["openai_api_key"].strip()
    if (data.get("anthropic_api_key") or "").strip():
        updates["ANTHROPIC_API_KEY"] = data["anthropic_api_key"].strip()

    config.update_env_file(updates)
    config.reload()
    h = config.CONFIG.htmlgen
    v = config.CONFIG.vibe
    # 保存した後、選択中エンジンのキーで接続テスト（正しいか即わかる）
    key_ok, key_msg = _test_key(h.provider)
    head = "保存しました。" + ("✅ " if key_ok else "⚠ キー検証NG：")
    return jsonify(
        {
            "ok": True,
            "provider": h.provider,
            "openai_model": h.openai_model,
            "openai_set": h.openai_enabled,
            "anthropic_set": v.enabled,
            "key_ok": key_ok,
            "message": head + key_msg,
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
    """指定サイト1件の雰囲気をClaudeで言語化する（非同期）。"""
    if not config.CONFIG.vibe.enabled:
        return jsonify({"ok": False, "message": "APIキーが未設定です（.env を確認）"}), 400
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
    """未処理サイトをまとめて裏で言語化する（その間も検索などは使える）。"""
    if not config.CONFIG.vibe.enabled:
        return jsonify({"ok": False, "message": "APIキーが未設定です（.env を確認）"}), 400
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
    if not config.CONFIG.vibe.enabled:
        return jsonify({"ok": False, "message": "APIキーが未設定です（.env を確認）"}), 400
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
    if not config.CONFIG.vibe.enabled:
        return jsonify({"ok": False, "message": "APIキーが未設定です（.env を確認）"}), 400
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    if not fn or not (config.CAMP_DIR / fn).exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    try:
        suggestions = camp.suggest_edits(fn)
    except Exception as exc:  # noqa: BLE001
        log.exception("改善案の生成に失敗")
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True, "suggestions": suggestions})


def _run_edit_job(job_id: str, fn: str, section: int, instruction: str) -> None:
    """バックグラウンドでカンプを部分編集する（生成ジョブ一覧に相乗り）。"""
    try:
        prov = "GPT" if config.CONFIG.htmlgen.provider == "openai" else "Claude"
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
    if not config.CONFIG.vibe.enabled:
        return jsonify({"ok": False, "message": "APIキーが未設定です（.env を確認）"}), 400
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


@app.route("/camp/<path:filename>")
def camp_file(filename: str):
    """生成したカンプHTMLを返す（ブラウザで開いて最終イメージを見る）。"""
    path = config.CAMP_DIR / filename
    if not path.exists() or path.suffix != ".html":
        abort(404)
    return send_file(path, mimetype="text/html")


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
