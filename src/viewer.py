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

import base64 as _b64
import json as _json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file

from . import anim, animkit, assets, bgremove, camp, clone, config, db, embed, export_split, figmaimport, figmakit, ingest, motion, quality, respcheck, search, sp_convert, spec, style_check, vibe
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

# コーディング仕様書の作成中状態（同時に1つ・Playwrightでカンプを実測する）
_SPEC_RUNNING: dict = {"file": None, "result": None, "error": None}
_SPEC_LOCK = threading.Lock()
# 📱レスポンシブ監査ジョブ（仕様書と同じ作り・同時1つ）
_RESP_RUNNING: dict = {"file": None, "result": None, "error": None}
_RESP_LOCK = threading.Lock()
# 📱スマホ版おおよそ変換ジョブ（同時1つ）
_SP_RUNNING: dict = {"file": None, "result": None, "error": None}
_SP_LOCK = threading.Lock()


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
    has_video = bool(row["animation_video_path"]) and config.resolve_data_path(
        row["animation_video_path"]
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
            "advice_provider": h.advice_provider,
            "advice_model": "" if h.advice_model == "default" else h.advice_model,
            "recheck_provider": h.recheck_provider or h.advice_provider,
            "recheck_model": "" if h.recheck_model == "default" else h.recheck_model,
            "dcfix_provider": h.dcfix_provider,
            "dcfix_model": "" if h.dcfix_model == "default" else h.dcfix_model,
            "codex_model": "" if h.codex_model == "default" else h.codex_model,
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
            # gpt-5.6系（Sol/Terra/Luna）はResponses API専用＝chat.completionsだと404になる。
            # Responses APIは旧モデルも受け付けるので分岐せず統一する（src/camp.py の _call_openai と同じ理由）。
            OpenAI(api_key=key).responses.create(
                model=h.openai_model,
                input="ok",
                max_output_tokens=16,
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
        elif provider == "codex":
            import shutil as _sh
            if not _sh.which("codex"):
                return False, "Codex CLIが見つかりません（npm i -g @openai/codex → codex login）"
            return True, "Codex CLIあり（ChatGPT定額枠・追加0円）"
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
    if provider == "codex":
        import shutil as _sh
        return _sh.which("codex") is not None  # CLIが入っていればOK（課金はChatGPT定額枠）
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
    if data.get("provider") in ("anthropic", "openai", "gemini", "deepseek", "zai", "codex"):
        updates["DESIGN_STOCK_HTML_PROVIDER"] = data["provider"]
    if data.get("edit_provider") in ("anthropic", "openai", "gemini", "deepseek", "zai", "codex"):
        updates["DESIGN_STOCK_EDIT_PROVIDER"] = data["edit_provider"]
    # 🧐デザイン指摘の3役（指摘・評価はスクショを見るので画像対応エンジンのみ。codexも画像OK）
    if data.get("advice_provider") in ("anthropic", "openai", "gemini", "codex"):
        updates["DESIGN_STOCK_ADVICE_PROVIDER"] = data["advice_provider"]
    if data.get("recheck_provider") in ("anthropic", "openai", "gemini", "codex"):
        updates["DESIGN_STOCK_RECHECK_PROVIDER"] = data["recheck_provider"]
    # 🔧指摘どおりに直す専用エンジン（テキスト仕事＝deepseek/zaiもOK・カンプ修正エンジンとは別枠）
    if data.get("dcfix_provider") in ("anthropic", "openai", "gemini", "deepseek", "zai", "codex"):
        updates["DESIGN_STOCK_DCFIX_PROVIDER"] = data["dcfix_provider"]
    # モデル欄は「空に戻す＝既定モデル」を許すため、空文字を "default" として保存する
    # （update_env_fileは空文字を「変更しない」と扱う仕様＝APIキー消し防止のため）
    if "advice_model" in data:
        updates["DESIGN_STOCK_ADVICE_MODEL"] = (data.get("advice_model") or "").strip() or "default"
    if "recheck_model" in data:
        updates["DESIGN_STOCK_RECHECK_MODEL"] = (data.get("recheck_model") or "").strip() or "default"
    if "dcfix_model" in data:
        updates["DESIGN_STOCK_DCFIX_MODEL"] = (data.get("dcfix_model") or "").strip() or "default"
    # Codexの共通モデル（空＝~/.codex/config.tomlの既定）
    if "codex_model" in data:
        updates["DESIGN_STOCK_CODEX_MODEL"] = (data.get("codex_model") or "").strip() or "default"
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
    has_video = bool(row["animation_video_path"]) and config.resolve_data_path(
        row["animation_video_path"]
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
    has_video = bool(row["animation_video_path"]) and config.resolve_data_path(
        row["animation_video_path"]
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


# 📚お手本パネルの💡アドバイス：手順を「ツールの機能名」だけで書かせる＝ユーザーが自分の手で
# 再現できる（修正と勉強を兼ねる）。コードは書かせない。テキストのみ＝修正エンジンで安い。
_ADVICE_SYSTEM = (
    "あなたはWebデザインの先生。生徒が作ったカンプの1セクションを見て、"
    "『このツールで自分の手で出来る操作』だけを使った改善手順を日本語で出す。\n"
    "使ってよい操作（この名前のまま書く）：\n"
    "・動きを付ける：ふわっと出現／上から降りる／左から／右から／ズームイン／ぼやけて出現／3Dフリップ／"
    "せり上がり／一文字ずつ／タイプライター／波打ち／ネオングロー／脈打つ／ゆらゆら／バウンド／行マスク／"
    "カーテンワイプ／カーテン開き(左から)／カーテン開き(真ん中)／📖ページめくり／🔢カウントアップ\n"
    "・🖍マーカー（文字に蛍光ペン・太さ/速さ/色を調整できる）\n"
    "・⏳動きの演出（順番・遅らせ・速さ＝『上から順に0.3秒刻み』のような指定）\n"
    "・🎨セクションの背景色を変える\n"
    "・🖼写真を加工（白フチで囲む／はみ出しキャプションカード／背景の飾りグラデ）\n"
    "・✏文字を編集（大きさ・行間・フォント・色・縦書き・点線下線）\n"
    "・位置・サイズ・余白の調整（＋高く/－低く・横幅・移動）\n"
    "出力形式：番号リストで5〜8個。各行は「対象（どの文字・画像か具体的な文言で）→ 操作 →（なぜ良くなるか1行）」。\n"
    "ベース情報があれば、その雰囲気・動き・配色に寄せる提案を最優先にする。\n"
    "コードは書かない。リスト以外の前置き・まとめも書かない。"
)


@app.route("/api/section_advice", methods=["POST"])
def api_section_advice():
    """📚お手本パネル：セクション1つ分の演出・見た目の改善手順をAIに出させる（テキストのみ）。"""
    data = request.get_json(silent=True) or {}
    sec_html = (data.get("html") or "").strip()
    if not sec_html:
        return jsonify({"ok": False, "message": "セクションが取れませんでした（セクション内で右クリックしてください）"}), 400
    sec_html = sec_html[:8000]
    base_txt = ""
    base_id = (data.get("base") or "").strip()
    if base_id:
        with db.connect() as conn:
            row = db.get_site(conn, base_id)
        if row:
            parts = [f"■ベースサイト（このカンプの手本・寄せる先）: {row['url']}"]
            if row["vibe_description"]:
                parts.append("雰囲気: " + row["vibe_description"][:200])
            if row["design_tokens"]:
                try:
                    from . import tokens as tokens_mod
                    parts.append("デザイントークン:\n" + tokens_mod.tokens_to_prompt(_json.loads(row["design_tokens"])))
                except Exception:  # noqa: BLE001
                    pass
            if row["motion_spec"]:
                try:
                    mt = motion.motion_to_prompt(_json.loads(row["motion_spec"]))
                    if mt:
                        parts.append("動きの仕様（録画から読み取り）:\n" + mt)
                except Exception:  # noqa: BLE001
                    pass
            base_txt = "\n".join(parts) + "\n\n"
    try:
        txt, used = camp._call_llm(
            _ADVICE_SYSTEM,
            [{"type": "text", "text": base_txt + "■生徒のセクションHTML:\n" + sec_html}],
            provider=config.CONFIG.htmlgen.edit_provider,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("演出アドバイスに失敗")
        return jsonify({"ok": False, "message": str(exc)[:200]}), 500
    return jsonify({"ok": True, "advice": (txt or "").strip()[:4000], "model": used})


# 🧐デザイン指摘：スクショ（見た目）＋HTMLをプロ基準で採点させ、指摘4つを数値つきで返す。
# ポイント＝画像を渡すこと（テキストだけだとAIは見た目を見られない＝当たり障りない感想になる）。
_CRITIQUE_SYSTEM = (
    "あなたは経験豊富なアートディレクター。Webデザインカンプの1セクションの"
    "スクリーンショット（実際の見た目）とHTMLを見て、デザインの問題点を指摘する。\n"
    "採点基準（この物差しで見る）：\n"
    "・余白：8pxの倍数か／要素間の近接（関係が近いものほど近く）／見出し上下の余白は2:1\n"
    "・文字：階層が4段以内で明確か／日本語本文の行間1.9〜2.1／1行38字以内／サイズのジャンプ率\n"
    "・色：70(ベース):25(サブ):5(アクセント)の比率／コントラスト（薄すぎる文字）／色数の締まり\n"
    "・構図：視線の流れ／均等な箱並びの単調さ／写真と文字の重なりの処理／揃え（グリッド）\n"
    "・コピー：抽象的なスローガンになっていないか（具体的な数字・固有名詞があるか）\n"
    "出力ルール（厳守）：\n"
    "・指摘はちょうど4つ。効果が大きい順。\n"
    "・各指摘は3行以内で「①どこ（要素の文言で特定） ②何が問題 ③どう直す（px/色コード等の数値で）」。\n"
    "・ただし行間(line-height)だけは必ず倍率で書く（例：行間1.9）。行間のpx指定は禁止"
    "（「行間16px」のような指摘は大見出しの行を重ねて壊す）。\n"
    "・読み手はデザイン初心者。専門用語には直後に（かっこ）で短い説明を添える。"
    "例：行間1.9（文字サイズの1.9倍の高さ）／#222（ほぼ黒のこげ茶色）／余白24px（指1本分くらいの隙間）。\n"
    "・褒め言葉・前置き・まとめは書かない。コードも書かない。\n"
    "・形式：「1. 【場所】問題 → 直し方」の番号リスト。"
)


# ✅直ったか確認：前回の指摘リストだけを判定させる（新しい指摘の追加を禁止）。
# 指摘モードは「必ず4つ出せ」なので何回でも新ネタを探してくる＝終わらない。
# 確認モードを分けることで「全部✅→卒業」ができる。判定だけなので安いモデル(Luna)で十分。
_RECHECK_SYSTEM = (
    "あなたはアートディレクター。以前あなたが出したデザイン指摘のリストと、"
    "修正後のセクションのスクリーンショット＋HTMLを見比べて、各指摘が直ったかを判定する。\n"
    "ルール（厳守）：\n"
    "・判定するのは前回の指摘リストの項目だけ。新しい指摘は絶対に追加しない。\n"
    "・前回と同じ番号順に、各項目を1〜2行で：「✅ 直りました」／"
    "「⚠ あと少し（何が足りないか具体的に1行）」／「❌ まだ（何が変わっていないか1行）」\n"
    "・全項目が✅なら最後に「🎉 全部直りました！このセクションは合格です」と1行足す。\n"
    "・前置き・まとめ・コード・新しい提案は書かない。"
)


@app.route("/api/design_critique", methods=["POST"])
def api_design_critique():
    """🧐デザイン指摘：セクションのスクショを撮り、画像対応AIに指摘4つを出させる。

    スクショは保存済みファイルから撮る（未保存の編集は写らない＝フロントで保存を促す）。
    エンジンは advice_provider（画像非対応のdeepseek/zaiならopenaiに退避）、
    モデルは advice_model で上書きできる（例：DESIGN_STOCK_ADVICE_MODEL=gpt-5.6-sol）。
    """
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    kind = (data.get("kind") or "section").strip()
    idx = int(data.get("idx") or 0)
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    if kind not in ("section", "header", "footer"):
        return jsonify({"ok": False, "message": "対象はセクション/ヘッダー/フッターだけです"}), 400
    try:
        shot = spec.shot_part(fn, kind, idx)
    except Exception as exc:  # noqa: BLE001
        log.exception("デザイン指摘のスクショに失敗")
        return jsonify({"ok": False, "message": "スクショ失敗：" + str(exc)[:150]}), 500
    sec_html = (data.get("html") or "").strip()[:8000]
    hcfg = config.CONFIG.htmlgen
    # モード分岐：recheck＝前回の指摘だけを✅❌判定（安いモデル）／通常＝指摘4つ（上位モデル）
    mode = (data.get("mode") or "").strip()
    prev = (data.get("prev") or "").strip()[:4000]
    if mode == "recheck" and prev:
        system = _RECHECK_SYSTEM
        body_txt = "■前回の指摘リスト:\n" + prev + "\n\n■修正後のセクションの見た目は上の画像。参考にHTMLも渡す:\n" + sec_html
        provider = hcfg.recheck_provider or hcfg.advice_provider
        model = hcfg.recheck_model
    else:
        system = _CRITIQUE_SYSTEM
        body_txt = "■このセクションの見た目（スクショ）は上の画像。参考にHTMLも渡す:\n" + sec_html
        provider = hcfg.advice_provider
        model = hcfg.advice_model
    if provider in ("deepseek", "zai"):
        provider = "openai"  # 画像を送れないエンジンでは見た目を指摘・評価できない
    if model in ("", "default"):
        model = None  # "default"＝そのエンジンの既定モデル（設定画面で空に戻した状態）
    content = [
        {"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": _b64.b64encode(shot).decode("ascii"),
        }},
        {"type": "text", "text": body_txt},
    ]
    try:
        camp.TASK_LABEL = "recheck" if (mode == "recheck" and prev) else "critique"
        txt, used = camp._call_llm(system, content, provider=provider, model=model)
    except Exception as exc:  # noqa: BLE001
        log.exception("デザイン指摘に失敗")
        return jsonify({"ok": False, "message": str(exc)[:200]}), 500
    finally:
        camp.TASK_LABEL = "misc"
    return jsonify({"ok": True, "critique": (txt or "").strip()[:4000], "model": used})


# ===== 🌙 自動ブラッシュアップ：🧐指摘→🔧修正を全セクションに自動で回す =====
# 人間がボタンを2回押していた流れ（指摘をもらう→この指摘どおりに直して）を、指定周回数だけ自動化。
# 暴走・高額の防止（設計で保証）：
#   ・周回数はforループの回数で固定＝「直るまで回り続ける」構造がそもそも存在しない
#   ・上限は brushup_max_rounds（.envの DESIGN_STOCK_BRUSHUP_MAX_ROUNDS・既定3）でクランプ
#   ・実行前に /api/brushup_estimate が過去実測ベースの見積もり円を返し、UIが確認ダイアログを出す
#   ・途中版は作業用コピー1ファイルに上書き＝修正のたびにファイルもタブも増えない。元カンプは無傷
# 見本の注入：カンプの <meta name="ce-base"> からベースサイトのスクショ・トークン・雰囲気文を取り、
# 指摘AIに毎回見せる＝「AIの好み」ではなく「ユーザーが選んだ手本」を物差しにする。


def _brushup_base_ctx(html: str) -> dict | None:
    """ce-baseメタ→ベースサイトの見本素材（firstviewスクショ・トークン・雰囲気）を集める。無ければNone。"""
    m = re.search(r'<meta\b[^>]*name=["\']ce-base["\'][^>]*>', html, re.I)
    if not m:
        return None
    c = re.search(r'content=["\']([^"\']+)["\']', m.group(0))
    if not c:
        return None
    with db.connect() as conn:
        row = db.get_site(conn, c.group(1).strip())
    if not row:
        return None
    parts = [f"■見本（このカンプの手本サイト・寄せる先）: {row['url']}"]
    if row["vibe_description"]:
        parts.append("雰囲気: " + row["vibe_description"][:200])
    if row["design_tokens"]:
        try:
            from . import tokens as tokens_mod
            parts.append("デザイントークン:\n" + tokens_mod.tokens_to_prompt(_json.loads(row["design_tokens"])))
        except Exception:  # noqa: BLE001
            pass
    ctx: dict = {"txt": "\n".join(parts), "img": None, "mime": "image/png"}
    try:
        fp = config.resolve_data_path(row["firstview_path"])
        if fp.exists():
            ctx["img"] = fp.read_bytes()
            ctx["mime"] = "image/jpeg" if fp.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    except Exception:  # noqa: BLE001
        pass
    return ctx


def _run_brushup_job(job_id: str, fn: str, rounds: int, secs: list[int] | None = None) -> None:
    """🌙自動磨き本体：作業コピー1つを作り、(指摘→修正)×セクション×周回で上書きしていく。

    secs＝磨くセクション番号（0始まり）の絞り込み。None/空なら全セクション。
    テスト時に「2セクションだけ」のような小さい実行ができる（Codexの枠・API課金の節約）。
    """
    try:
        h = config.CONFIG.htmlgen
        src_html = (config.CAMP_DIR / fn).read_text(encoding="utf-8")
        work = f"camp_{datetime.now().strftime('%Y%m%d_%H%M%S')}_brush.html"
        # どのカンプから磨いたかをmetaで記録＝「⬆元に反映」ボタンが反映先を思い出せる
        work_html = re.sub(r'<meta name="ce-brush-src"[^>]*>\s*', "", src_html)
        work_html = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + f'<meta name="ce-brush-src" content="{fn}">',
                           work_html, count=1)
        (config.CAMP_DIR / work).write_text(work_html, encoding="utf-8")
        base = _brushup_base_ctx(src_html)
        # エンジン：指摘＝advice（画像必須・deepseek/zaiはopenaiへ退避）／修正＝dcfix（無ければ修正エンジン）
        adv_p = h.advice_provider if h.advice_provider not in ("deepseek", "zai") else "openai"
        adv_m = None if h.advice_model in ("", "default") else h.advice_model
        fix_p = h.dcfix_provider or h.edit_provider
        fix_m = h.dcfix_model if h.dcfix_provider else ""
        skipped: list[str] = []
        for r in range(rounds):  # ★回数固定ループ＝これ以上は物理的に回らない
            n = len(list(camp._SEC_RE.finditer((config.CAMP_DIR / work).read_text(encoding="utf-8"))))
            targets = [i for i in (secs or range(n)) if 0 <= i < n]
            done_cnt = 0
            for i in targets:
                done_cnt += 1
                tag = f"{r + 1}周目S{i + 1}"
                _camp_set(job_id, phase=f"🌙 {r + 1}/{rounds}周目 セクション{i + 1}（{done_cnt}/{len(targets)}）：🧐指摘中…")
                try:
                    shot = spec.shot_part(work, "section", i)
                except Exception:  # noqa: BLE001
                    log.exception("自動磨き: スクショ失敗 %s", tag)
                    skipped.append(tag + "(スクショ失敗)")
                    continue
                mm = list(camp._SEC_RE.finditer((config.CAMP_DIR / work).read_text(encoding="utf-8")))
                if i >= len(mm):
                    break
                content: list = [{"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg",
                    "data": _b64.b64encode(shot).decode("ascii"),
                }}]
                body = "■このセクション（1枚目の画像）の指摘をする。参考にHTMLも渡す:\n" + mm[i].group(0)[:8000]
                if base:
                    if base.get("img"):
                        content.append({"type": "image", "source": {
                            "type": "base64", "media_type": base["mime"],
                            "data": _b64.b64encode(base["img"]).decode("ascii"),
                        }})
                        body += ("\n\n■2枚目の画像＝このカンプの見本（手本サイト）。"
                                 "指摘は自分の好みではなく、この見本の雰囲気・密度・余白感に近づける方向で出すこと。"
                                 "見本に無い装飾（角丸カード・影・グラデ等）を新たに足す提案は禁止。")
                    body += "\n\n" + base["txt"]
                content.append({"type": "text", "text": body})
                try:
                    camp.TASK_LABEL = "brushup_critique"
                    critique, _u = camp._call_llm(_CRITIQUE_SYSTEM, content, provider=adv_p, model=adv_m)
                except Exception:  # noqa: BLE001
                    log.exception("自動磨き: 指摘失敗 %s", tag)
                    skipped.append(tag + "(指摘失敗)")
                    continue
                _camp_set(job_id, phase=f"🌙 {r + 1}/{rounds}周目 セクション{i + 1}（{done_cnt}/{len(targets)}）：🔧修正中…")
                ins = ("以下のデザイン指摘リストのとおりに、このセクションを修正して。\n"
                       "・指摘に書かれた数値（px・色コード・行間など）をそのまま正確に使う\n"
                       "・ただし行間(line-height)は倍率で適用する。文字サイズより小さいpx行間の指摘"
                       "（例：見出しに行間16px）は明らかな誤りなので適用しない\n"
                       "・指摘されていない部分のデザインは変えない\n"
                       "・文章・画像は1つも消さない\n\n" + (critique or "").strip()[:4000])
                try:
                    camp.TASK_LABEL = "brushup_fix"
                    camp.edit_camp_section(work, i, ins, keep_text=True,
                                           provider=fix_p, model=fix_m, out_name=work)
                except Exception:  # noqa: BLE001
                    # テキスト保全ゲートで中止など＝このセクションは触らず次へ（壊れた版は書かれない）
                    log.exception("自動磨き: 修正スキップ %s", tag)
                    skipped.append(tag + "(修正スキップ)")
                    continue
        note = ("一部スキップ: " + "、".join(skipped[:6])) if skipped else ""
        _camp_set(job_id, state="done", file=work, message=note)
    except Exception as exc:  # noqa: BLE001
        log.exception("自動磨きに失敗")
        _camp_set(job_id, state="error", message=str(exc))
    finally:
        camp.TASK_LABEL = "misc"


@app.route("/api/brushup_estimate")
def api_brushup_estimate():
    """🌙自動磨きの見積もり（円）。過去の実測平均×セクション数×周回数＝使うほど正確になる。"""
    from . import pricing
    fn = (request.args.get("file") or "").strip()
    max_r = config.CONFIG.htmlgen.brushup_max_rounds
    try:
        rounds = max(1, min(int(request.args.get("rounds") or 2), max_r))
    except Exception:  # noqa: BLE001
        rounds = min(2, max_r)
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    n = len(list(camp._SEC_RE.finditer(p.read_text(encoding="utf-8"))))
    if n == 0:
        return jsonify({"ok": False, "message": "セクションが見つかりません（<section>が無いカンプは対象外）"}), 400
    # codexエンジンぶんはChatGPT定額枠＝0円で見積もる
    h = config.CONFIG.htmlgen
    adv_p = h.advice_provider if h.advice_provider not in ("deepseek", "zai") else "openai"
    fix_p = h.dcfix_provider or h.edit_provider
    per = (0.0 if adv_p == "codex" else pricing.avg_yen("brushup_critique")) \
        + (0.0 if fix_p == "codex" else pricing.avg_yen("brushup_fix"))
    return jsonify({"ok": True, "sections": n, "rounds": rounds, "max_rounds": max_r,
                    "adv_engine": adv_p, "fix_engine": fix_p,
                    "per_sec_yen": round(per, 1), "yen": int(round(n * rounds * per))})


@app.route("/api/auto_brushup", methods=["POST"])
def api_auto_brushup():
    """🌙自動磨きを開始（非同期ジョブ）。kind=editなのでホーム画面が勝手にタブを開かない。"""
    h = config.CONFIG.htmlgen
    adv_p = h.advice_provider if h.advice_provider not in ("deepseek", "zai") else "openai"
    fix_p = h.dcfix_provider or h.edit_provider
    if not _provider_ready(adv_p):
        return jsonify({"ok": False, "message": "指摘エンジン（" + adv_p + "）が使えません（APIキーまたはCodex CLIを確認）"}), 400
    if not _provider_ready(fix_p):
        return jsonify({"ok": False, "message": "修正エンジン（" + fix_p + "）が使えません（APIキーまたはCodex CLIを確認）"}), 400
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    try:
        rounds = max(1, min(int(data.get("rounds") or 2), h.brushup_max_rounds))
    except Exception:  # noqa: BLE001
        rounds = min(2, h.brushup_max_rounds)
    # 磨くセクションの絞り込み（1始まりで来る→0始まりへ。空なら全部）
    secs: list[int] = []
    try:
        secs = sorted({int(x) - 1 for x in (data.get("secs") or []) if int(x) >= 1})
    except Exception:  # noqa: BLE001
        secs = []
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    with _CAMP_LOCK:
        running = sum(1 for j in _CAMP_JOBS.values() if j.get("state") == "running")
        if running >= _CAMP_MAX:
            return jsonify({"ok": False, "message": f"同時処理は最大{_CAMP_MAX}件までです（少し待って）"}), 429
        job_id = uuid.uuid4().hex
        _CAMP_JOBS[job_id] = {"state": "running", "kind": "edit",
                              "brief": f"🌙自動磨き {rounds}周" + (f"（S{','.join(str(s + 1) for s in secs)}）" if secs else ""),
                              "phase": "開始しています…"}
    log.info("自動磨きジョブ開始[%s]: %s rounds=%s secs=%s", job_id[:6], fn, rounds, secs or "全部")
    threading.Thread(target=_run_brushup_job, args=(job_id, fn, rounds, secs), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


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
    raw_url = (data.get("url") or "").strip()
    keep_js = bool(data.get("keep_js"))
    use_extracted = bool(data.get("use_extracted"))
    if site_id:
        with db.connect() as conn:
            row = db.get_site(conn, site_id)
        if not row:
            return jsonify({"ok": False, "message": "見つかりません"}), 404
        url = row["url"]
        if use_extracted and not assets.list_assets(site_id):
            return jsonify({"ok": False, "message": "先に『🖼画像を抜き出す』で画像を抜き出してください"}), 400
    elif raw_url.startswith(("http://", "https://")):
        # 🌐 URL直指定クローン（Chrome拡張の右クリック用）：登録していないサイトでもOK。
        # 抽出済み画像の再利用はsite_idが無いと引けないので、URL直のときは常にその場DL。
        url = raw_url
        site_id = "direct"
        use_extracted = False
    else:
        return jsonify({"ok": False, "message": "見つかりません（idかurlを指定してください）"}), 404
    with _CLONE_LOCK:
        if _CLONING.get("site_id") is not None:
            return jsonify({"ok": False, "message": "別のクローンが進行中です"}), 409
        _CLONING.update({"site_id": site_id, "phase": "開始しています…", "file": None, "error": None})
    log.info("クローンジョブ開始: %s (keep_js=%s, use_extracted=%s)", url, keep_js, use_extracted)
    threading.Thread(target=_run_clone_job, args=(site_id, url, keep_js, use_extracted), daemon=True).start()
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
            and config.resolve_data_path(r["animation_video_path"]).exists(),
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


# 進捗メッセージに出すエンジン名。表に無いプロバイダはそのまま出す（"Claude"に化けさせない）
_PROV_LABELS = {"anthropic": "Claude", "openai": "GPT", "gemini": "Gemini",
                "deepseek": "DeepSeek", "zai": "GLM"}


def _prov_label(provider: str) -> str:
    return _PROV_LABELS.get(provider, provider or "AI")


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
        prov = _prov_label(config.CONFIG.htmlgen.provider)
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


def _run_edit_job(job_id: str, fn: str, section: int, instruction: str, keep_text: bool = False, style_type: str = "", engine: str = "",
                  ref_b64: str = "", ref_mime: str = "image/jpeg") -> None:
    """バックグラウンドでカンプを部分編集する（生成ジョブ一覧に相乗り）。

    engine="dcfix"＝🧐指摘の🔧修正専用エンジン（未設定ならカンプ修正エンジンに退避）。
    ref_b64＝📷見本画像（構図を寄せる）。deepseek/zaiは画像を送れないのでopenaiへ退避。
    """
    try:
        h = config.CONFIG.htmlgen
        if engine == "dcfix":
            _ep = h.dcfix_provider or h.edit_provider
            _model = h.dcfix_model if h.dcfix_provider else ""
        else:
            _ep, _model = h.edit_provider, ""
        if ref_b64 and _ep in ("deepseek", "zai"):
            _ep, _model = "openai", ""
        prov = _prov_label(_ep)
        scope = "全体" if section is None or section < 0 else f"セクション{section + 1}"
        _camp_set(job_id, phase=f"{prov}が{scope}を直しています…" + ("（📷見本つき）" if ref_b64 else ""))
        camp.TASK_LABEL = "edit"
        result = camp.edit_camp_section(fn, section, instruction, keep_text=keep_text, style_type=style_type,
                                        provider=_ep, model=_model, ref_b64=ref_b64, ref_mime=ref_mime)
        _camp_set(job_id, state="done", **result)
    except Exception as exc:  # noqa: BLE001
        log.exception("部分編集に失敗")
        _camp_set(job_id, state="error", message=str(exc))
    finally:
        camp.TASK_LABEL = "misc"


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
    engine = "dcfix" if data.get("engine") == "dcfix" else ""  # 🧐指摘の🔧修正だけ専用エンジンを使う
    # 📷 見本画像（dataURL）。構図を寄せる用。大きすぎは弾く（クライアントで1400pxに縮小済みの想定）
    ref_b64, ref_mime = "", "image/jpeg"
    ref = str(data.get("ref_image") or "")
    if ref.startswith("data:image/") and ";base64," in ref and len(ref) < 8_000_000:
        head, _, ref_b64 = ref.partition(";base64,")
        ref_mime = head[5:] or "image/jpeg"
    with _CAMP_LOCK:
        running = sum(1 for j in _CAMP_JOBS.values() if j.get("state") == "running")
        if running >= _CAMP_MAX:
            return jsonify({"ok": False, "message": f"同時処理は最大{_CAMP_MAX}件までです（少し待って）"}), 429
        job_id = uuid.uuid4().hex
        # kind="edit"＝修正ジョブの目印。ホーム画面はこれを自動で別タブに開かない
        # （修正はカンプタブ自身が完了時に同じタブで開き直す＝タブが増えない）。
        _CAMP_JOBS[job_id] = {"state": "running", "kind": "edit", "brief": f"部分編集: {instruction[:24]}", "phase": "開始しています…"}
    log.info("部分編集ジョブ開始[%s]: %s section=%s keep_text=%s style=%s / %s", job_id[:6], fn, section, keep_text, style_type, instruction)
    threading.Thread(
        target=_run_edit_job, args=(job_id, fn, section, instruction, keep_text, style_type, engine, ref_b64, ref_mime), daemon=True
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


# ============================================================
# 📋 スクショを貼り付けてセクションにする（2026-07-25）
#   ⭐セクション保存は「ページのDOMからsectionを掴む」方式なので、クローン元の作りが
#   変だと取れないことが多い。そこで「見えている通りにスクショを撮って貼る」逃げ道を作る。
#   ① AIなし＝貼った画像をそのまま1セクションにする（確実・無料・数秒）
#   ② AIあり＝画像を見てHTML/CSSに作り直す（文字が本物のテキストになる）
# ============================================================
@app.route("/api/paste_image", methods=["POST"])
def api_paste_image():
    """クリップボードから貼られた画像を保存してURLを返す（AI説明は付けない＝速い）。"""
    f = request.files.get("image")
    if f is None:
        return jsonify({"ok": False, "message": "画像がありません"}), 400
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = "paste_%s.png" % datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = config.UPLOAD_DIR / name
    f.save(str(path))
    w = h = 0
    try:
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001
        pass
    log.info("貼り付け画像を保存: %s (%dx%d)", name, w, h)
    # URLは既存の「🖼画像を追加」と同じ絶対URL（camp._UPLOAD_BASE）に揃える
    #   ＝分割エクスポート・Figma書き出しが既に対応済みの形なので、後工程がそのまま動く。
    return jsonify({"ok": True, "file": name, "url": camp._UPLOAD_BASE + name, "w": w, "h": h})


# ★地雷：ここは % 書式（"%(ns)s"）で埋めてはいけない。本文に「幅は % で作る」等の
#   生の % が入るため ValueError で必ず落ちる（実際に「AI生成が失敗」した原因）。
#   {NS}/{SHOT} を str.replace で差し替える方式にしてある。
_PASTE_SYS = (
    "あなたは日本語Webサイトのフロントエンド実装者です。渡された1枚のスクリーンショットは、"
    "あるWebページの『1セクション』を切り取ったものです。これを HTML+CSS で作り直してください。\n"
    "【厳守】\n"
    "1) 出力は <section> ... </section> の1ブロックだけ。前後に説明文やコードフェンスを書かない。\n"
    "2) CSSは <style> を section の中に入れ、セレクタは必ず .{NS} から始める"
    "（例 .{NS} .ttl{...}）。既存ページのCSSと絶対にぶつからないようにする。\n"
    "3) section には class=\"{NS}\" を付ける。\n"
    "4) 画像の中の文字は、読み取れる限りそのまま本物のテキストとして書き出す"
    "（画像のまま貼らない・あとで編集できるようにする）。\n"
    "5) 写真部分は <img src=\"{SHOT}\" style=\"...object-fit:cover\"> のように"
    "『渡したスクショのURL』を仮の画像として使い、object-position で該当部分が見えるよう寄せる。"
    "写真が何枚あっても同じURLで構わない（あとで差し替える前提）。\n"
    "6) レイアウトは flex / grid で組み、幅は % か max-width で作る（固定pxで横幅を決めない）。\n"
    "7) 文字サイズ・色・余白・角丸・線は、スクショから読み取った実際の見た目に合わせる。\n"
    "8) JavaScript は使わない。外部CSS/フォントも読み込まない。"
)


@app.route("/api/paste_to_section", methods=["POST"])
def api_paste_to_section():
    """貼り付けた画像をAIに見せて、1セクション分のHTMLを作らせる。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    path = config.UPLOAD_DIR / fn
    if not fn or not path.exists():
        return jsonify({"ok": False, "message": "貼り付けた画像が見つかりません"}), 404
    ns = "psec" + datetime.now().strftime("%H%M%S")
    blk = camp._ref_image_block(path, max_w=1400, max_h=1800)
    if blk is None:
        return jsonify({"ok": False, "message": "画像を読み込めませんでした"}), 400
    hint = (data.get("hint") or "").strip()
    content = [blk, {"type": "text", "text": "このスクショの見た目を、上のルールで再現してください。"
                     + (("\n【追加の指示】" + hint) if hint else "")}]
    sys_txt = _PASTE_SYS.replace("{NS}", ns).replace("{SHOT}", camp._UPLOAD_BASE + fn)
    # これは「作り直す＝修正系」の作業なので、修正用エンジン(edit_provider)を使う。
    #   .env で DESIGN_STOCK_EDIT_PROVIDER=codex にしてあれば ChatGPT定額枠＝追加課金ゼロで動く。
    try:
        html, used = camp._call_llm(sys_txt, content,
                                    provider=config.CONFIG.htmlgen.edit_provider)
    except Exception as e:  # noqa: BLE001
        log.exception("貼り付け→AI生成に失敗")
        m = str(e)
        # Codexのログイン切れ（401/revoked）は文言が英語で分かりにくいので、やることだけ日本語で出す
        if "401" in m or "revoked" in m or "sign in again" in m:
            m = "Codexのログインが切れています。ターミナルで『codex login』を実行してChatGPTにログインし直してください。"
        return jsonify({"ok": False, "message": m}), 500
    m = re.search(r"```(?:html)?\s*(.*?)```", html, flags=re.DOTALL | re.IGNORECASE)
    if m:
        html = m.group(1)
    i, j = html.find("<section"), html.rfind("</section>")
    if i < 0 or j < 0:
        return jsonify({"ok": False, "message": "AIがセクションを返しませんでした"}), 500
    return jsonify({"ok": True, "html": html[i:j + len("</section>")], "model": used, "ns": ns})


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


@app.route("/api/remove_bg_url", methods=["POST"])
def api_remove_bg_url():
    """カンプに『すでに置いてある画像』の背景を抜く（src がどんな形でもOK）。

    ⭐アップロード済みファイル名ではなく src を受け取るのがミソ。クローン元の相対パス
    （clone_xxx_files/…）・外部URL・data: も、分割エクスポートの取得ロジックで実体を読む。
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("src") or "").strip()
    camp_file = (data.get("camp") or "").strip()
    if not url:
        return jsonify({"ok": False, "message": "画像のURLがありません"}), 400
    camp_dir = config.CAMP_DIR
    if camp_file:
        p = config.CAMP_DIR / Path(camp_file).name
        if p.exists():
            camp_dir = p.parent
    raw, _ct = export_split._fetch_bytes(url, camp_dir)
    if not raw:
        return jsonify({"ok": False, "message": "この画像の元データを取得できませんでした（外部URLは取りに行けないことがあります）"}), 404
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.UPLOAD_DIR / ("_bgsrc_" + uuid.uuid4().hex[:8] + ".png")
    try:
        tmp.write_bytes(raw)
        out = bgremove.remove_background(tmp)
    except Exception as exc:  # noqa: BLE001
        log.exception("背景除去に失敗（URL指定）")
        return jsonify({"ok": False, "message": "背景除去に失敗：" + str(exc)}), 500
    finally:
        tmp.unlink(missing_ok=True)   # 元データの控えは残さない（uploads一覧を汚さない）
    newname = "up_" + uuid.uuid4().hex[:10] + ".png"
    (config.UPLOAD_DIR / newname).write_bytes(out)
    log.info("背景除去(URL): %s → %s", url[:80], newname)
    return jsonify({"ok": True, "file": newname, "url": camp._UPLOAD_BASE + newname})


@app.route("/api/remove_bg", methods=["POST"])
def api_remove_bg():
    """アップロード画像の背景を除去 → 透過PNGを新規アップロードとして保存する（このPCの中だけで処理・無料）。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    src = config.UPLOAD_DIR / fn
    if not fn or src.parent != config.UPLOAD_DIR or not src.exists():
        return jsonify({"ok": False, "message": "画像が見つかりません"}), 404
    try:
        out = bgremove.remove_background(src)
    except Exception as exc:  # noqa: BLE001
        log.exception("背景除去に失敗")
        return jsonify({"ok": False, "message": "背景除去に失敗：" + str(exc)}), 500
    newname = "up_" + uuid.uuid4().hex[:10] + ".png"
    (config.UPLOAD_DIR / newname).write_bytes(out)
    meta = camp.load_uploads_meta()
    base = meta.get(fn, "")
    meta[newname] = (base + "（背景除去）") if base else "背景除去済み"
    camp.save_uploads_meta(meta)
    log.info("背景除去: %s → %s", fn, newname)
    return jsonify({"ok": True, "file": newname, "uploads": camp.list_uploads()})


@app.route("/api/camps")
def api_camps():
    """保存済みカンプの一覧（履歴）。お気に入りを先頭に、あとは「更新が新しい順」。"""
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
        # ファイル名に埋め込まれた作成日時（camp_/fav_/clone_共通のYYYYMMDD_HHMMSS）を優先して
        # 並び替えのキーにする。git経由の移行等でmtimeが全ファイル同時刻に揃ってしまっても、
        # 本来の作成順が崩れないようにするため（mtimeだけだと順序が失われる実例が発生した）。
        ts_m = re.search(r"(\d{8})_(\d{6})", p.name)
        made_key = int(ts_m.group(1) + ts_m.group(2)) if ts_m else int(datetime.fromtimestamp(st.st_mtime).strftime("%Y%m%d%H%M%S"))
        # 並びは「更新時間（最後に保存した時）の新しい順」。一覧に出している日時も mtime なので、
        # 表示とならびが一致する。mtimeが同じ（git clone等で一括コピーされた）ときだけ、
        # ファイル名の作成日時で細かい順を決める＝PC移行しても順番が完全には崩れない。
        sort_key = (st.st_mtime, made_key)
        item = {
            "file": p.name, "title": title, "mtime": st.st_mtime, "size": st.st_size,
            "name": info.get("name", ""), "fav": bool(info.get("fav")),
        }
        items.append((item, sort_key))
    # お気に入りを上に、その中と外はそれぞれ「更新が新しい順」
    items.sort(key=lambda pair: (0 if pair[0]["fav"] else 1, -pair[1][0], -pair[1][1]))
    return jsonify({"camps": [item for item, _ in items]})


@app.route("/api/camp_name", methods=["POST"])
def api_camp_name():
    """カンプに名前を付ける（⭐お気に入りとは別。名前だけでは自動でお気に入りにしない）。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    name = (data.get("name") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    info = camp.set_camp_name(fn, name)
    return jsonify({"ok": True, "name": info.get("name", ""), "fav": bool(info.get("fav"))})


@app.route("/api/camp_fav", methods=["POST"])
def api_camp_fav():
    """カンプの⭐お気に入りを単独でトグルする（名前は変えない）。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    info = camp.toggle_camp_fav(fn)
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


@app.route("/api/menu_layout")
def api_menu_layout_get():
    """右クリックメニューの並び順・グループ設定を返す（Git同期の共有ファイルから）。

    まだ保存が無ければ null を返す＝ブラウザ側は既定レイアウト(QM_DEF_LAYOUT)を使う。
    """
    try:
        if config.MENU_LAYOUT_PATH.exists():
            data = _json.loads(config.MENU_LAYOUT_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return jsonify({"ok": True, "layout": data})
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"ok": True, "layout": None})


@app.route("/api/menu_layout", methods=["POST"])
def api_menu_layout_save():
    """右クリックメニューの並び順を共有ファイルに保存する（家↔会社でGit同期）。"""
    data = request.get_json(silent=True) or {}
    layout = data.get("layout")
    if not isinstance(layout, list) or not all(isinstance(x, str) for x in layout):
        return jsonify({"ok": False, "message": "レイアウトの形式が不正です"}), 400
    if len(layout) > 200:  # 項目数の暴走ガード（通常は30〜40）
        return jsonify({"ok": False, "message": "項目が多すぎます"}), 400
    try:
        config.MENU_LAYOUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = config.MENU_LAYOUT_PATH.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(layout, ensure_ascii=False, indent=0), encoding="utf-8")
        os.replace(tmp, config.MENU_LAYOUT_PATH)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/api/shortcuts")
def api_shortcuts_get():
    """ショートカットキーの割り当て（操作→キー）を返す（Git同期の共有ファイルから）。

    まだ保存が無ければ null を返す＝ブラウザ側は既定の割り当て(SC_DEF)を使う。
    """
    try:
        if config.SHORTCUTS_PATH.exists():
            data = _json.loads(config.SHORTCUTS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return jsonify({"ok": True, "keys": data})
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"ok": True, "keys": None})


@app.route("/api/shortcuts", methods=["POST"])
def api_shortcuts_save():
    """ショートカットキーの割り当てを共有ファイルに保存する（家↔会社でGit同期）。"""
    data = request.get_json(silent=True) or {}
    keys = data.get("keys")
    if not isinstance(keys, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in keys.items()
    ):
        return jsonify({"ok": False, "message": "割り当ての形式が不正です"}), 400
    if len(keys) > 60:  # 操作数の暴走ガード
        return jsonify({"ok": False, "message": "項目が多すぎます"}), 400
    try:
        config.SHORTCUTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = config.SHORTCUTS_PATH.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(keys, ensure_ascii=False, indent=0), encoding="utf-8")
        os.replace(tmp, config.SHORTCUTS_PATH)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/api/base_stats_all")
def api_base_stats_all():
    """全ベース(手本)ぶんの実績をまとめて返す（一覧・ベース選択グリッドのバッジ表示用）。

    site_idごとに◎○△✖の内訳と代表マークを1回のログ走査で返す。選択1件ずつ叩く
    /api/base_stats と違い、カードが多い一覧画面でもリクエスト1回で済む。
    """
    return jsonify({"ok": True, "stats": quality.all_base_stats()})


@app.route("/api/base_stats")
def api_base_stats():
    """手本（ベース）ごとの過去実績（◎○△✖と自動判定NG）を返す。選択時のヒント用。

    anim_id も渡されたら「その組み合わせ」の実績も足して返す（ログには生成時から
    base×animが残っているのに、手本単位でしか集計していなかったのを解消）。
    """
    base_id = (request.args.get("base_id") or "").strip()
    anim_id = (request.args.get("anim_id") or "").strip()
    if not base_id:
        return jsonify({"ok": True, "note": ""})
    res = quality.base_stats(base_id)
    if anim_id:
        pair_note = quality.pair_stats(base_id, anim_id).get("note", "")
        if pair_note:
            res["note"] = (res.get("note", "") + ("　" if res.get("note") else "") + pair_note).strip()
    return jsonify({"ok": True, **res})


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


@app.route("/api/camp_backup", methods=["POST"])
def api_camp_backup():
    """🗂 いまのカンプ（最後に保存した状態）を複製してバックアップ（AIなし・一瞬）。

    複製は camp_元名_bkYYYYMMDD_HHMMSS.html でcamps直下に置く＝履歴一覧にそのまま並ぶ。
    並び順はファイル名先頭の元日時で決まるので、元カンプのすぐ近くに出る。
    """
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "ファイルが見つかりません"}), 404
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    new = config.CAMP_DIR / f"{p.stem}_bk{stamp}.html"
    try:
        new.write_bytes(p.read_bytes())
        try:  # 一覧で分かるように名前を付ける（失敗しても複製自体は成功扱い）
            camp.set_camp_name(new.name, f"🗂バックアップ {stamp[4:6]}/{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}")
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True, "file": new.name})


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
    # 焼き込み保険(_REVIEW_FALLBACK)を最新版に入れ替える＝古いカンプも保存するだけで
    # 保険のバグ修正（例：data-cedelay無視で全要素同時に出る）が反映される
    html = camp._finalize_html(html)
    try:
        # 一時ファイルに書いてから差し替え＝書き込み途中で落ちても元ファイルが壊れない
        tmp = p.with_suffix(".html.tmp")
        tmp.write_text(html, encoding="utf-8")
        os.replace(tmp, p)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True, "file": fn})


@app.route("/api/brush_apply", methods=["POST"])
def api_brush_apply():
    """🌙磨き版の内容で元カンプを上書きする（⬆元に反映ボタン）。

    磨きファイルのce-brush-srcメタが反映先。磨き版は残る（消さない）＝気に入らなければ元に戻せるよう
    上書き前の元カンプを .bak に控える。
    """
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "ファイルが見つかりません"}), 404
    html = p.read_text(encoding="utf-8")
    m = re.search(r'<meta name="ce-brush-src" content="([^"]+)"', html)
    if not m:
        return jsonify({"ok": False, "message": "元カンプの記録が無いファイルです（この機能は今後の磨き版から使えます）"}), 400
    src = m.group(1)
    sp = config.CAMP_DIR / src
    if sp.parent != config.CAMP_DIR or sp.suffix != ".html" or not sp.exists():
        return jsonify({"ok": False, "message": "元カンプが見つかりません: " + src}), 404
    try:
        sp.with_suffix(".html.bak").write_text(sp.read_text(encoding="utf-8"), encoding="utf-8")
        out = re.sub(r'<meta name="ce-brush-src"[^>]*>\s*', "", html)  # 反映後の元カンプに磨きメタは残さない
        out = camp._finalize_html(out)
        tmp = sp.with_suffix(".html.tmp")
        tmp.write_text(out, encoding="utf-8")
        os.replace(tmp, sp)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 500
    log.info("磨き版を元カンプへ反映: %s → %s", fn, src)
    return jsonify({"ok": True, "source": src})


def _run_spec_job(filename: str) -> None:
    """バックグラウンドでカンプを実測し、仕様書HTMLを作る。"""
    try:
        result = spec.build_spec(filename)
        with _SPEC_LOCK:
            _SPEC_RUNNING["result"] = result
            _SPEC_RUNNING["error"] = None
    except Exception as exc:  # noqa: BLE001
        log.exception("仕様書の作成に失敗: %s", filename)
        with _SPEC_LOCK:
            _SPEC_RUNNING["error"] = str(exc)
    finally:
        with _SPEC_LOCK:
            _SPEC_RUNNING["file"] = None


@app.route("/api/make_spec", methods=["POST"])
def api_make_spec():
    """カンプの実測仕様書を作る（非同期・AIなし）。進捗は /api/make_spec/status。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    with _SPEC_LOCK:
        if _SPEC_RUNNING.get("file") is not None:
            return jsonify({"ok": False, "message": "別の仕様書を作成中です"}), 409
        _SPEC_RUNNING.update({"file": fn, "result": None, "error": None})
    log.info("仕様書ジョブ開始: %s", fn)
    threading.Thread(target=_run_spec_job, args=(fn,), daemon=True).start()
    return jsonify({"ok": True, "file": fn})


@app.route("/api/make_spec/status")
def api_make_spec_status():
    """仕様書作成の進捗（ポーリング用）。"""
    with _SPEC_LOCK:
        running = _SPEC_RUNNING.get("file") is not None
        result = _SPEC_RUNNING.get("result")
        error = _SPEC_RUNNING.get("error")
    return jsonify({"running": running, "result": result, "error": error})


@app.route("/spec/<path:filename>")
def spec_file(filename: str):
    """仕様書HTMLを返す（保存JSは生成時に焼き込み済み）。"""
    path = spec.SPEC_DIR / filename
    if not path.exists() or not path.is_file() or path.suffix != ".html":
        abort(404)
    return Response(path.read_text(encoding="utf-8"), mimetype="text/html")


def _run_resp_job(filename: str) -> None:
    """バックグラウンドでカンプを3画面幅で実測し、レスポンシブ検査レポートを作る。"""
    try:
        result = respcheck.run_check(filename)
        with _RESP_LOCK:
            _RESP_RUNNING["result"] = result
            _RESP_RUNNING["error"] = None
    except Exception as exc:  # noqa: BLE001
        log.exception("レスポンシブ監査に失敗: %s", filename)
        with _RESP_LOCK:
            _RESP_RUNNING["error"] = str(exc)
    finally:
        with _RESP_LOCK:
            _RESP_RUNNING["file"] = None


@app.route("/api/resp_check", methods=["POST"])
def api_resp_check():
    """レスポンシブ自動監査（非同期・AIなし）。進捗は /api/resp_check/status。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    with _RESP_LOCK:
        if _RESP_RUNNING.get("file") is not None:
            return jsonify({"ok": False, "message": "別のレスポンシブ検査を実行中です"}), 409
        _RESP_RUNNING.update({"file": fn, "result": None, "error": None})
    log.info("レスポンシブ監査ジョブ開始: %s", fn)
    threading.Thread(target=_run_resp_job, args=(fn,), daemon=True).start()
    return jsonify({"ok": True, "file": fn})


@app.route("/api/resp_check/status")
def api_resp_check_status():
    """レスポンシブ監査の進捗（ポーリング用）。"""
    with _RESP_LOCK:
        running = _RESP_RUNNING.get("file") is not None
        result = _RESP_RUNNING.get("result")
        error = _RESP_RUNNING.get("error")
    return jsonify({"running": running, "result": result, "error": error})


@app.route("/check/<path:filename>")
def check_file(filename: str):
    """レスポンシブ検査レポートHTMLを返す（自己完結・スクショ焼き込み済み）。"""
    path = respcheck.CHECK_DIR / filename
    if not path.exists() or not path.is_file() or path.suffix != ".html":
        abort(404)
    return Response(path.read_text(encoding="utf-8"), mimetype="text/html")


def _run_sp_job(filename: str) -> None:
    """バックグラウンドでカンプを375px実測し、SP用CSSを注入した新ファイルを作る。"""
    try:
        result = sp_convert.run_convert(filename)
        with _SP_LOCK:
            _SP_RUNNING["result"] = result
            _SP_RUNNING["error"] = None
    except Exception as exc:  # noqa: BLE001
        log.exception("スマホ版変換に失敗: %s", filename)
        with _SP_LOCK:
            _SP_RUNNING["error"] = str(exc)
    finally:
        with _SP_LOCK:
            _SP_RUNNING["file"] = None


@app.route("/api/sp_convert", methods=["POST"])
def api_sp_convert():
    """📱 スマホ版おおよそ変換（非同期・AIなし）。進捗は /api/sp_convert/status。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    with _SP_LOCK:
        if _SP_RUNNING.get("file") is not None:
            return jsonify({"ok": False, "message": "別のスマホ版変換を実行中です"}), 409
        _SP_RUNNING.update({"file": fn, "result": None, "error": None})
    log.info("スマホ版変換ジョブ開始: %s", fn)
    threading.Thread(target=_run_sp_job, args=(fn,), daemon=True).start()
    return jsonify({"ok": True, "file": fn})


@app.route("/api/sp_convert/status")
def api_sp_convert_status():
    """スマホ版変換の進捗（ポーリング用）。"""
    with _SP_LOCK:
        running = _SP_RUNNING.get("file") is not None
        result = _SP_RUNNING.get("result")
        error = _SP_RUNNING.get("error")
    return jsonify({"running": running, "result": result, "error": error})


@app.route("/sp/<path:filename>")
def sp_file(filename: str):
    """スマホ版に変換したカンプHTMLを返す（元カンプにSP用CSSを足しただけの1ファイル）。"""
    path = sp_convert.SP_DIR / filename
    if not path.exists() or not path.is_file() or path.suffix != ".html":
        abort(404)
    return Response(path.read_text(encoding="utf-8"), mimetype="text/html")


@app.route("/api/anim_kit", methods=["POST"])
def api_anim_kit():
    """🎬 アニメ実装キットを書き出す（静的解析のみ＝同期で一瞬・AIなし）。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    try:
        result = animkit.build_kit(fn)
    except Exception as exc:  # noqa: BLE001
        log.exception("アニメ実装キットの作成に失敗: %s", fn)
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True, **result})


@app.route("/api/prod_kit", methods=["POST"])
def api_prod_kit():
    """📦 本番化キット（AI変換用フォルダ）を書き出す（同期で一瞬・AIなし）。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    try:
        from . import prodkit
        result = prodkit.build_prodkit(fn, out_dir=(data.get("out_dir") or "").strip() or None)
    except Exception as exc:  # noqa: BLE001
        log.exception("本番化キットの作成に失敗: %s", fn)
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True, **result})


@app.route("/kit/<path:filename>")
def kit_file(filename: str):
    """アニメ実装キットHTMLを返す（自己完結・デモも動く）。"""
    path = animkit.KIT_DIR / filename
    if not path.exists() or not path.is_file() or path.suffix != ".html":
        abort(404)
    return Response(path.read_text(encoding="utf-8"), mimetype="text/html")


@app.route("/api/figma_kit", methods=["POST"])
def api_figma_kit():
    """🎨 Figma取り込み用の書き出し（掃除＋アニメ潰し＋画像埋め込み・同期で一瞬・AIなし）。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    p = config.CAMP_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != config.CAMP_DIR or not p.exists():
        return jsonify({"ok": False, "message": "カンプが見つかりません"}), 404
    try:
        result = figmakit.build_figmakit(fn)
    except Exception as exc:  # noqa: BLE001
        log.exception("Figma書き出しに失敗: %s", fn)
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True, **result})


@app.route("/api/figma_import", methods=["POST"])
def api_figma_import():
    """🎯 Figma → カンプHTML（逆方向の取り込み・REST API・AIなし＝無料）。

    Figma側にプラグインは要らない。`.env` の FIGMA_TOKEN（File content: Read-only）だけ。
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "message": "FigmaのURLを入れてください"}), 400
    try:
        result = figmaimport.import_from_url(url)
    except Exception as exc:  # noqa: BLE001
        log.exception("Figma取り込みに失敗: %s", url)
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True, **result})


@app.route("/figma/<path:sub>")
def figma_file(sub: str):
    """Figma書き出しフォルダ（data/camps/figma/…）の成果物を返す（持ち運び用HTML・手順書）。"""
    path = (figmakit.FIGMA_DIR / sub).resolve()
    if figmakit.FIGMA_DIR.resolve() not in path.parents or not path.is_file():
        abort(404)
    if path.suffix == ".html":
        return Response(path.read_text(encoding="utf-8"), mimetype="text/html")
    if path.suffix == ".md":
        return Response(path.read_text(encoding="utf-8"), mimetype="text/plain; charset=utf-8")
    abort(404)


@app.route("/api/save_spec_html", methods=["POST"])
def api_save_spec_html():
    """仕様書の編集（セルの数値・メモ書き換え）をファイルに焼き込む（AIなし）。"""
    data = request.get_json(silent=True) or {}
    fn = (data.get("file") or "").strip()
    html = data.get("html") or ""
    p = spec.SPEC_DIR / fn
    if not fn or p.suffix != ".html" or p.parent != spec.SPEC_DIR or not p.exists():
        return jsonify({"ok": False, "message": "仕様書が見つかりません"}), 404
    if len(html) < 200 or "</html>" not in html.lower():
        return jsonify({"ok": False, "message": "HTMLが空か壊れています（保存中止）"}), 400
    try:
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
            data.get("html", ""), data.get("headcss", ""), data.get("name", ""),
            kind=data.get("kind", "section"),
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
/* 編集中だけ：文字選択の色をブラウザ標準の青系に固定（2026-07-19）。
   カンプ自身が ::selection を黄色系にデザインしていると、Alt+ドラッグの選択が
   マーカーとそっくりに見えて「マーカーが消せない」誤解が実際に起きた。
   このstyleは #__ce を含むので💾保存時に丸ごと消える＝デザインのselection色は無傷 */
::selection{background:rgba(51,144,255,.4)!important;color:inherit!important}
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
#__ce_pk{position:fixed;inset:0;z-index:2147483001;background:rgba(0,0,0,.12);display:flex;align-items:center;justify-content:center}
#__ce_pk .bx{background:#fff;border-radius:12px;padding:16px;max-width:720px;width:92%;max-height:80vh;overflow:auto;font-family:system-ui,sans-serif;box-shadow:0 18px 50px rgba(0,0,0,.35)}
/* 🔍 画像・セクションを「選ぶ」パネルだけ大きくする（2026-07-30・要望「見づらいのでもう少し大きく」）。
   ★:has() で中身を見て広げる＝⭐お気に入り一覧など「文字だけのパネル」は今までの幅のまま。
   全部を広げると、短いリストが横に間延びして逆に読みにくくなる。 */
#__ce_pk .bx:has(.gr),#__ce_pk .bx:has(.secgr){max-width:1120px;max-height:88vh}
#__ce_pk h4{cursor:move;user-select:none}
#__ce_pk h4{margin:0 0 12px;font-size:15px}
#__ce_pk .secgr{display:grid;grid-template-columns:repeat(auto-fill,224px);gap:12px;justify-content:center}
#__ce_pk .sit{position:relative;width:224px;border:1px solid #e2e2e6;border-radius:8px;overflow:hidden;cursor:pointer;background:#fff}
#__ce_pk .sit:hover{border-color:#e8a300;box-shadow:0 6px 16px rgba(0,0,0,.18)}
#__ce_pk .sit .pv{width:224px;height:142px;overflow:hidden;background:#fff;pointer-events:none}
/* 縮小率は 224/1200＝0.1867。幅を変えたらこの数字も必ず合わせる（ズレると中身が切れる） */
#__ce_pk .sit .pv iframe{width:1200px;height:760px;border:none;transform:scale(.1867);transform-origin:top left}
#__ce_pk .sit .nm{font-size:12px;font-weight:700;color:#1d1d1f;padding:6px 8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#__ce_pk .sit .del{position:absolute;top:4px;right:4px;background:rgba(0,0,0,.55);color:#fff;border:none;border-radius:999px;width:22px;height:22px;cursor:pointer;font-size:13px;line-height:20px;padding:0}
#__ce_pk .cl{float:right;cursor:pointer;font-size:18px;font-weight:700;color:#888}
#__ce_pk .gr{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}
#__ce_pk .it{border:1px solid #eee;border-radius:8px;overflow:hidden;cursor:pointer;background:#fff}
#__ce_pk .it:hover{border-color:#2b6cb0;box-shadow:0 4px 12px rgba(0,0,0,.15)}
#__ce_pk .it img{width:100%;height:140px;object-fit:cover;display:block;background:#eef2f7}
#__ce_pk .it span{display:block;font-size:12px;color:#555;padding:5px 7px}
#__ce_pk .favgr{display:grid;grid-template-columns:1fr;gap:8px}
#__ce_pk .favgr .it{padding:10px 12px;cursor:pointer}
#__ce_pk .favgr .it.now{border-color:#e8a300;background:#fffaf0}
#__ce_pk .favgr .nm{font-weight:700;font-size:13px;color:#1d1d1f}
#__ce_pk .favgr .dt{font-size:11px;color:#888;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#__ce_pkpos{position:fixed;inset:0;z-index:2147483001;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center}
#__ce_pkpos .bx{background:#fff;border-radius:12px;padding:16px;max-width:420px;width:92%;max-height:80vh;overflow:auto;font-family:system-ui,sans-serif}
#__ce_pkpos h4{margin:0 0 12px;font-size:15px}
#__ce_pkpos .cl{float:right;cursor:pointer;font-size:18px;font-weight:700;color:#888}
#__ce_pkpos .poslist{display:flex;flex-direction:column;gap:6px}
#__ce_pkpos .sit-pos{border:1px solid #e2e2e6;border-radius:8px;padding:10px 12px;cursor:pointer;font-size:13px;color:#1d1d1f;background:#fff}
#__ce_pkpos .sit-pos:hover{border-color:#e8a300;background:#fffaf0}
#__ce_cm{position:fixed;z-index:2147483002;width:290px;max-width:92vw;background:#fff;border:1px solid #ddd;border-radius:12px;box-shadow:0 16px 44px rgba(0,0,0,.32);font-family:system-ui,sans-serif;color:#1d1d1f;overflow:hidden}
/* ★クローン元サイトのCSS（例：button{color:#fff}）がツールのボタンにも効いて、白背景に白文字＝
   ボタンが空箱に見える事故が起きた。ツールUIの中だけ文字色と書体を取り戻す（inline指定のある
   暗いパネルの白文字は、inlineが勝つのでそのまま）。 */
html body #__ce_cm button,html body #__ce_cm select,html body #__ce_cm input,
html body #__ce_bgp button,html body #__ce_bgp select{color:#1d1d1f;font-family:system-ui,sans-serif;text-transform:none;letter-spacing:normal}
html body #__ce_bgp button,html body #__ce_bgp select{color:#fff}
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
  <div class="hd" id="__ce_hd"><span>✏</span><span class="t">このカンプを直す</span><span id="__ce_rate" title="このカンプの出来を評価（手本ごとのハズレ率集計に使われます）"><span class="rt" data-r="◎">◎</span><span class="rt" data-r="○">○</span><span class="rt" data-r="△">△</span><span class="rt" data-r="✖">✖</span></span><span class="x" id="__ce_homeh" style="background:#2b6cb0" title="ツール（ホーム）に戻る">🏠 ホーム</span><span class="sv" id="__ce_undo" style="background:#555;opacity:.4" title="ひとつ前に戻す">⟲ 戻す</span><span class="sv" id="__ce_apply" style="background:#b45309;display:none" title="この磨き版の内容で、元のカンプを上書きする（上書き前の元は.bakに控えます）">⬆ 元に反映</span><span class="sv" id="__ce_save">💾 保存</span><span class="x" id="__ce_mn">▲ ひらく</span></div>
  <div class="bd">
    <button class="im" id="__ce_home" style="background:#eef2f7;color:#1d1d1f;border:1px solid #d6deea;font-weight:700">🏠 ツール（ホーム）に戻る</button>
    <div class="lbl plain">🎨 ベース色（テーマ色・AIなし・ページ全体に反映）</div>
    <div class="row" style="align-items:center"><input type="color" id="__ce_base" style="width:54px;height:38px;padding:2px;border:1px solid #d0d0d5;border-radius:9px;cursor:pointer;flex:none"><button class="im" id="__ce_baser" style="background:#f2f2f4;color:#1d1d1f;border:1px solid #ddd;flex:1;margin:0">⟲ 元の色に戻す</button></div>
    <div class="msg" id="__ce_basemsg" style="min-height:0;margin-top:2px"></div>
    <div class="lbl plain">🚫 背景の飾りを消す（わっか/ぼかし等・クリックで消せない装飾）</div>
    <button class="im" id="__ce_nodeco" style="background:#f2f2f4;color:#1d1d1f;border:1px solid #ddd">🚫 背景の飾り（わっか等）を消す</button>
    <div class="lbl plain">🧹 全体を規則化（左右の余白・見出しを一律に揃える・明らかに違う余白は別扱い・AIなし＝一貫性UP）</div>
    <button class="im" id="__ce_normalize" style="background:#0b6e4f;color:#fff">🧹 余白・見出しを一律に揃える</button>
    <div class="lbl plain">📍 動かした跡の一覧（ドラッグ移動を1個ずつ確認して選んで戻す・わざと動かした所は残せる・AIなし）</div>
    <button class="im" id="__ce_unshift" style="background:#a04b00;color:#fff">📍 動かした跡を一覧で見る</button>
    <button class="im" id="__ce_btncolor" style="background:#0b6e4f;color:#fff">🎨 全ボタンをテーマ色に統一</button>
    <div class="lbl plain">🎨 使っている色を置き換える（ヘッダーの色もOK・変数なしのクローンでも効く・AIなし）</div>
    <button class="im" id="__ce_colrepbtn" style="background:#0b6bcb;color:#fff">🎨 ヘッダー・文字・背景の色を置き換える（Shift+ダブルクリックでも開く）</button>
    <div class="lbl plain">➖ 区切り線（各セクションの先頭に短い線・AIなし。不要な所は右クリック→削除）</div>
    <div class="row" style="gap:10px;align-items:center">
      <label style="font-size:12px;color:#555">太さ<input id="__ce_divline_h" type="number" value="2" min="1" max="20" style="width:52px;margin-left:4px"></label>
      <label style="font-size:12px;color:#555">長さ<input id="__ce_divline_w" type="number" value="64" min="10" max="400" style="width:60px;margin-left:4px"></label>
    </div>
    <button class="im" id="__ce_divline" style="background:#0b6e4f;color:#fff">➖ 全セクションの先頭に区切り線を入れる</button>
    <div class="lbl plain">🤖 修正・おしゃれに使うAI（モデルは⚙設定で）</div>
    <select id="__ce_ai"><option value="anthropic">Claude</option><option value="openai">GPT</option><option value="codex">Codex（ChatGPT定額・追加0円）</option><option value="gemini">Gemini</option><option value="deepseek">DeepSeek（激安）</option><option value="zai">GLM（Z.ai・激安）</option></select>
    <div class="lbl plain">① 範囲を選ぶ（全体／セクション）</div>
    <select id="__ce_sec"><option value="-1">ページ全体</option></select>
    <div class="lbl">💡 選んだ所の改善案（AIが画面を見てたくさん提案）</div>
    <div class="row"><button class="sg" id="__ce_sg">💡 この部分の案を出す</button></div>
    <div class="chips" id="__ce_chips"></div>
    <div class="lbl">✍ 自分で指示</div>
    <div class="row"><input id="__ce_in" placeholder="例：見出しを大きく／CTAを黄色に"><button class="go" id="__ce_go">直す</button></div>
    <div class="row" style="align-items:center;gap:6px;flex-wrap:wrap">
      <button class="im" id="__ce_refimg_btn" style="margin:0" title="見本サイトのスクショを添付すると、AIが構図（見出しの位置・色面・並び）を読み取って寄せます">📷 見本画像を付ける（構図を寄せる）</button>
      <img id="__ce_refimg_thumb" style="display:none;height:34px;border-radius:6px;border:1px solid #ddd" alt="見本">
      <span id="__ce_refimg_x" style="display:none;cursor:pointer;font-weight:700">✖</span>
      <input type="file" id="__ce_refimg_file" accept="image/*" style="display:none">
    </div>
    <button class="im" id="__ce_align" title="各セクションの中身の幅を測り、多数派の幅にそろえます。全幅セクションや明らかに違う幅は触りません">📐 横幅をそろえる（AIなし・無料）</button>
    <div class="lbl plain">🎨 一括改善の手本（ストックの登録サイトに寄せる）</div>
    <select id="__ce_ref"><option value="">なし（AIおまかせ）</option></select>
    <button class="im" id="__ce_improve" style="background:#7c3aed;color:#fff">🚀 ページ全体を今風に（一括改善）</button>
    <div class="lbl plain">🔶 図形を置く（〇・四角・線／色はこのPCに記憶・AIなし）</div>
    <button class="im" id="__ce_shapes" style="background:#0b6bcb;color:#fff">🔶 図形バーを出す（画面の下）</button>
    <div class="lbl plain">🎬 オープニング演出（幕→フェードで本体へ・AIなし）<br><span style="opacity:.75">※編集中は流しません（白い待ちが出ないように）。保存版では自動で流れます</span></div>
    <button class="im" id="__ce_op_add" style="background:#0b6bcb;color:#fff">🎬 フェードのオープニングを付ける</button>
    <button class="im" id="__ce_op_edit" style="background:#eaf2fd;color:#0b4e8a;border:1px solid #bcd8f7" title="出すと画面下に専用バーが出て、▶で本番と同じ動きを1回だけ確認できます">👁 オープニングを出す／隠す（▶で本番の動きを確認・文字は右クリックで差し替え）</button>
    <button class="im" id="__ce_op_del" style="background:#fdecea;color:#a8231b;border:1px solid #f3bdb7" title="幕・待機スクリプトごと取り外します。白い開始と、元サイトの動きとのズレが無くなります">🗑 オープニングを外す（もう出さない）</button>
    <div class="lbl plain">⭐ セクションのお気に入り（保存は右クリック→⭐・AIなし）</div>
    <button class="im" id="__ce_favlist" style="background:#fff3d6;color:#8a5a00;border:1px solid #f0d38a">🔀 お気に入りからセクションを切り替え</button>
    <button class="im" id="__ce_favadd" style="background:#fff3d6;color:#8a5a00;border:1px solid #f0d38a">➕ お気に入りからセクションを追加（場所を選ぶ）</button>
    <button class="im" id="__ce_hdpick" style="background:#fff3d6;color:#8a5a00;border:1px solid #f0d38a">🧢 ヘッダーの種類を選ぶ（標準6種＋⭐保存分）</button>
    <button class="im" id="__ce_ftpick" style="background:#fff3d6;color:#8a5a00;border:1px solid #f0d38a">🦶 フッターの種類を選ぶ（⭐保存分）</button>
    <div class="lbl plain">🎨 おしゃれ度チェック（AIが有名サイト基準で採点＋改善点）</div>
    <button class="im" id="__ce_stylecheck" style="background:#c026a6;color:#fff">🎨 おしゃれ度をチェック</button>
    <button class="im" id="__ce_autopolish" style="background:#7c3aed;color:#fff">🎯 チェックして自動で磨く（採点→改善を一括・AI）</button>
    <div class="lbl plain">📦 納品用に書き出す（HTML/CSS/JS＋画像を分割・AIなし）</div>
    <button class="im" id="__ce_export" style="background:#0b6e4f;color:#fff">📦 分割エクスポート（zipで保存）</button>
    <div class="lbl plain">📐 コーディング仕様書（寸法・色・フォント・動きを実測で1枚に・AIなし）</div>
    <button class="im" id="__ce_spec" style="background:#0b6bcb;color:#fff">📐 仕様書を作る（コーディング担当に渡す用）</button>
    <button class="im" id="__ce_resp" style="background:#7a3fa8;color:#fff">📱 レスポンシブ検査（スマホ/タブレットで崩れないか）</button>
    <button class="im" id="__ce_sp" style="background:#e8590c;color:#fff">📱 スマホ版を作る（おおよそ変換・AIなし）</button>
    <button class="im" id="__ce_insp" style="background:#263238;color:#fff">🔍 インスペクト（コーダーに数値を渡す）</button>
    <button class="im" id="__ce_kit" style="background:#b3541e;color:#fff">🎬 アニメ実装キット（動きをコードで渡す）</button>
    <button class="im" id="__ce_prod" style="background:#5b21b6;color:#fff">📦 本番化キット（AIに本番コードを書かせる下ごしらえ）</button>
    <button class="im" id="__ce_figma" style="background:#0d99ff;color:#fff">🎨 Figma用に書き出す（取り込んでデザイン化）</button>
    <button class="im" id="__ce_secswap" style="background:#0e7490;color:#fff">🔃 セクション並べ替え（順番を入れ替える）</button>
    <button class="im" id="__ce_bigclean" style="background:#4d7c0f;color:#fff">🧹 大掃除（分割span・残骸を消してソースを軽く）</button>
    <button class="im" id="__ce_bk" style="background:#475569;color:#fff">🗂 バックアップを取る（今の保存状態を複製）</button>
    <div class="lbl plain">🛑 アニメを止める（編集中だけ・全部を「動き終わった形」で固定／保存版には残りません）</div>
    <button class="im" id="__ce_stopanim" style="background:#f2f2f4;color:#1d1d1f;border:1px solid #ddd">🛑 アニメを全部止める</button>
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
    location.href='/?open=camp';  // ホームの「カンプ生成」パネルを開いた状態で戻す
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
      if(el.closest && (el.closest('[id^="__ce"]'))) return;
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
  function _freeZIndex(over){
    var z=5;
    try{
      var hdr=document.querySelector('header,.site-header,[class*="header"]');
      if(hdr){
        var hz=parseInt(getComputedStyle(hdr).zIndex,10);
        if(!isNaN(hz)) z=Math.max(1, hz-1);
      }
    }catch(_){}
    // ★置く場所を覆っている「重なりの親」より必ず手前にする（2026-07-30・ユーザー報告）。
    //   実害：クローン元の <section class="fv"> が z-index 2147480000（過去の操作で最大値へ飛んだ）
    //   だったため、その上に置いた画像が z=49 で永久に裏へ潜り「乗せたのに見えない・掴めない」。
    //   置き先の先祖を辿って、いちばん大きい z より1つ上を使う（ツールUIの2147483001より下に抑える）。
    try{
      for(var a=over; a&&a.nodeType===1&&a!==document.body&&a!==document.documentElement; a=a.parentElement){
        if(a.id&&a.id.indexOf('__ce')===0) continue;      // ツール自身のUIは相手に数えない
        if(a.closest&&a.closest('[id^="__ce"]')) continue;
        var v=parseInt(getComputedStyle(a).zIndex,10);
        if(!isNaN(v)&&v>z) z=Math.min(v+1, 2147480500);
      }
    }catch(_){}
    return z;
  }
  // ※文字/画像の「追加」ボタンは編集バーから廃止（右クリックメニューに統一・2026-07-11）。
  //   下のinsertImageEl/openAddImagePickerは右クリックメニューの「🖼 画像を追加」が使うので残す。
  // 追加した文字/画像を「その場所のセクションの中に・left%」で置く＝画面幅が変わっても追従する。
  // ★bodyに固定px(left:473px等)で置くと、画面を少し狭くしただけで右外に消える・縦位置もズレる
  //   （実際に起きた）。セクション相対の%なら、どの幅でも「そのセクションのその辺り」に居続ける。
  function placeFree(el, pageX, pageY){
    var host=null;
    [].slice.call(document.querySelectorAll('header,section,footer')).some(function(s){
      if(s.closest('[id^="__ce"]')) return false;
      // ★position:fixed/sticky の器には入れない（2026-07-20）：固定ヘッダーは「画面に貼り付いた」ままなので
      //   ページ座標での上端が常に今のスクロール位置＝どこをクリックしても「ここに入る」と判定されてしまう。
      //   その中に置くと、追加した画像がページと一緒にスクロールせず全ページに居座る（実際に起きた）。
      var ps=getComputedStyle(s).position;
      if(ps==='fixed'||ps==='sticky') return false;
      var r=s.getBoundingClientRect(), top=r.top+(window.scrollY||0);
      if(pageY>=top && pageY<=top+r.height){ host=s; return true; }
      return false;
    });
    el.style.position='absolute';
    // ★z-index必須：カンプの.container等がz-index:2を持つことが多く、無指定(auto)だと
    //   貼り付け/追加した要素がその下に潜り「重なった場所で二度と掴めない」（Ctrl+V貼り付けで実際に発生）
    //   ★置く場所の真下にある要素を渡す＝その重なりより手前の数字を選ばせる（スライドの上に乗せる用）
    if(!el.style.zIndex){
      var _zu=null;
      // ★elementFromPoint 単体はダメ：画像を選ぶパネル(#__ce_pk)が画面いっぱいに出ている最中なので
      //   必ずツールUIが返る＝真下のセクションを見られない（実測でここに引っかかった）。
      //   重なりを手前から全部見て、ツールUIでない最初の要素を使う。
      try{
        var _vx=pageX-(window.scrollX||0), _vy=pageY-(window.scrollY||0);
        var _st=document.elementsFromPoint(_vx,_vy)||[];
        for(var _i=0;_i<_st.length;_i++){
          var _n=_st[_i];
          if(_n&&_n.nodeType===1&&!(_n.closest&&_n.closest('[id^="__ce"]'))){ _zu=_n; break; }
        }
      }catch(_){}
      el.style.zIndex=_freeZIndex(_zu||host);
    }
    if(!host){  // セクションの外（ページ余白）だけは従来どおりbody基準
      el.style.left=Math.round(pageX)+'px'; el.style.top=Math.round(pageY)+'px';
      document.body.appendChild(el); return el;
    }
    var r=host.getBoundingClientRect(), hx=r.left+(window.scrollX||0), hy=r.top+(window.scrollY||0);
    if(getComputedStyle(host).position==='static') host.style.position='relative';  // 中の絶対配置の基準にする
    el.style.left=Math.max(0,Math.min(96,(pageX-hx)/r.width*100)).toFixed(1)+'%';
    el.style.top=Math.round(pageY-hy)+'px';
    host.appendChild(el);
    _wakePlaced(el);   // 置き先が pointer-events:none だと掴めない要素になるので必ず生き返らせる
    // 置いた後に実寸を測り、どの画面幅でも右にはみ出さない形へ整える：
    //   画像＝幅も%にする（画面と一緒に伸び縮み）／文字＝置いた位置から右端までで折り返す
    requestAnimationFrame(function(){
      var hw=host.getBoundingClientRect().width||1;
      var maxPct=Math.max(0,(1-(el.offsetWidth||0)/hw)*100);
      if(parseFloat(el.style.left)>maxPct) el.style.left=maxPct.toFixed(1)+'%';
      var pct=parseFloat(el.style.left)||0;
      if(el.tagName==='IMG'){ el.style.width=Math.min(96,(el.offsetWidth||260)/hw*100).toFixed(1)+'%'; el.style.height='auto'; }
      else { el.style.whiteSpace='normal'; el.style.maxWidth=Math.max(10,99-pct).toFixed(1)+'%'; }
    });
    return el;
  }
  // ===== 🔶 図形バー（〇・四角・線をどこにでも置く・AIなし・2026-07-28） =====
  //   画面下に出しっぱなしのバー。押した瞬間に「今見えている画面の中央」へ図形を1つ置く。
  //   置いた図形はただのdiv＝掴んで移動・■で伸縮・右クリックで動き/重なり順も付けられる
  //   （専用の編集画面を作らない＝覚えることを増やさない）。
  //   色はマイ色に記憶（背景の飾りと同じ置き場＝どちらで登録しても両方に出る）。
  //   ★バーのidは "__ce" 始まり＝💾保存時に自動で除去される（板が焼き込まれる事故が起きない）
  var SHAPE_ST={col:'#7fd0e6', fill:1, op:1, water:0};
  // 形の作り方（clip-pathは中身の無い飾りなので安全に使える）
  var SHAPE_DEF={
    circle:{lb:'⭕ 丸',  css:'width:180px;height:180px;border-radius:50%;'},
    rect:  {lb:'▭ 四角', css:'width:220px;height:140px;border-radius:14px;'},
    line:  {lb:'／ 線',  css:'width:260px;height:4px;border-radius:3px;'},
    pill:  {lb:'⬭ 帯',  css:'width:260px;height:70px;border-radius:999px;'},
    tri:   {lb:'🔺 三角', css:'width:180px;height:160px;clip-path:polygon(50% 0%,100% 100%,0% 100%);'},
    star:  {lb:'⭐ 星',  css:'width:170px;height:170px;clip-path:polygon(50% 0%,61% 35%,98% 35%,68% 57%,79% 91%,50% 70%,21% 91%,32% 57%,2% 35%,39% 35%);'},
    blob:  {lb:'💧 しずく', css:'width:190px;height:190px;border-radius:62% 38% 55% 45% / 45% 55% 42% 58%;'},
    slash: {lb:'／ 斜め線', css:'width:300px;height:3px;border-radius:3px;rotate:-28deg;'},
    arc:   {lb:'⌒ カーブ線', css:'width:340px;height:340px;border-radius:50%;rotate:-20deg;'}
  };
  // ▶ 丸アイコン（丸の中に記号）7種。見出しの前・リンクの横・箇条書きの先頭に置く小さな飾り。
  //   中身は文字なので、大きさ・色は図形バーの色/塗り、書体はページの書体をそのまま継ぐ。
  var ICON_DEF=[
    ['→','進む・リンク'],
    ['✓','チェック・できること'],
    ['＋','追加・開く'],
    ['？','よくある質問'],
    ['！','注意・ポイント'],
    ['★','おすすめ'],
    ['▶','再生・はじめる']
  ];
  // ★「今いじっている図形」を自前で覚える（2026-07-28）。
  //   バーのボタンを押すと、ページ側の捕捉フェーズの処理が先に走って選択(curEl)が外れるため、
  //   curEl頼みだと「色を変えても選択中の図形に反映されない」。触った図形をここに控える。
  var _lastShape=null;
  document.addEventListener('mousedown',function(e){
    try{ var s=e.target&&e.target.closest&&e.target.closest('.ce_shape'); if(s) _lastShape=s; }catch(_){}
  },true);
  try{ var _shs=JSON.parse(localStorage.getItem('__ce_shapest')||'null'); if(_shs&&_shs.col){ SHAPE_ST.col=_shs.col; SHAPE_ST.fill=_shs.fill?1:0; } }catch(_){}
  function _shapeStSave(){ try{ localStorage.setItem('__ce_shapest', JSON.stringify(SHAPE_ST)); }catch(_){} }
  // 🎨 水彩：中心が濃く縁がにじむ円を3つ重ね、ぼかして紙に落とした絵の具のようにする。
  //   ★1枚のグラデだと「きれいな円」にしか見えない。ずらした円を重ねてムラを作るのが水彩らしさの肝。
  //   mix-blend-mode:multiply＝重なった所が濃くなる（絵の具が重なった感じ）。
  function paintWater(el, col, op){
    var c1=_rgbaWith(col,.5), c2=_rgbaWith(col,.34), c3=_rgbaWith(col,.22), z='rgba(255,255,255,0)';
    el.style.setProperty('background',
       'radial-gradient(58% 54% at 34% 38%, '+c1+' 0%, '+c2+' 46%, '+z+' 74%),'
      +'radial-gradient(52% 58% at 70% 64%, '+c2+' 0%, '+c3+' 52%, '+z+' 80%),'
      +'radial-gradient(70% 68% at 52% 50%, '+c3+' 0%, '+z+' 72%)','important');
    el.style.removeProperty('border');
    el.style.setProperty('filter','blur(7px)','important');
    el.style.setProperty('mix-blend-mode','multiply');
    if(!el.getAttribute('data-cewshape')){                 // 形は不定形に（まん丸だと絵の具に見えない）
      el.setAttribute('data-cewshape','1');
      el.style.setProperty('border-radius','62% 38% 55% 45% / 45% 55% 42% 58%');
    }
    if(op!=null) el.style.setProperty('opacity', String(op));
  }
  function shapePaint(el, col, fill, op){
    var kind=el.getAttribute('data-ceshape')||'rect';
    if(el.getAttribute('data-cewater')){ paintWater(el, col, op); return; }
    if(kind==='arc'){     // ⌒カーブ線＝円の枠の一部だけ描く（上側の弧）
      el.style.setProperty('background','transparent','important');
      el.style.setProperty('border','3px solid '+col,'important');
      el.style.setProperty('border-right-color','transparent','important');
      el.style.setProperty('border-bottom-color','transparent','important');
      el.style.setProperty('border-left-color','transparent','important');
      if(op!=null) el.style.setProperty('opacity', String(op));
      return;
    }
    if(kind==='icon'){    // 丸アイコン＝塗りなら中の記号は白、フチだけなら記号もフチと同じ色
      if(fill){ el.style.setProperty('background',col); el.style.setProperty('color','#fff'); el.style.removeProperty('border'); }
      else { el.style.setProperty('background','transparent'); el.style.setProperty('color',col); el.style.setProperty('border','2px solid '+col); }
      if(op!=null) el.style.setProperty('opacity', String(op));
      return;
    }
    // ★三角・星はclip-pathで形を作る＝borderは形どおりに切られて出ないので、必ず塗りで描く
    var noBorder=(kind==='line'||kind==='tri'||kind==='star'||kind==='slash');
    if(noBorder||fill){ el.style.setProperty('background',col); el.style.removeProperty('border'); }
    else { el.style.setProperty('background','transparent'); el.style.setProperty('border','3px solid '+col); }
    if(op!=null) el.style.setProperty('opacity', String(op));
  }
  // 透明度を1段ずつ（0.05〜1）。選んでいる図形と、次に置く図形の両方に効く
  function shapeOpacity(delta){
    var t=(_lastShape&&document.contains(_lastShape))?_lastShape:null;
    var cur=t?(parseFloat(t.style.opacity)||1):SHAPE_ST.op;
    var next=Math.max(0.05, Math.min(1, Math.round((cur+delta)*100)/100));
    SHAPE_ST.op=next; _shapeStSave();
    if(t){ t.style.setProperty('opacity', String(next)); markDirty(); }
    return next;
  }
  function addShape(kind, sym){
    var d=document.createElement('div');
    d.className='ce_shape'; d.setAttribute('data-ceshape',kind);
    if(kind==='icon'){
      d.setAttribute('data-ceicon',sym);
      d.setAttribute('style','box-sizing:border-box;width:44px;height:44px;border-radius:50%;'
        +'display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:700;line-height:1;font-family:inherit;');
      d.textContent=sym;
    }else{
      var def=SHAPE_DEF[kind]||SHAPE_DEF.rect;
      d.setAttribute('style','box-sizing:border-box;'+def.css);
    }
    if(SHAPE_ST.water && kind!=='icon' && kind!=='arc') d.setAttribute('data-cewater','1');
    shapePaint(d, SHAPE_ST.col, SHAPE_ST.fill, SHAPE_ST.op);
    placeFree(d, (window.scrollX||0)+window.innerWidth/2, (window.scrollY||0)+window.innerHeight/2);
    // placeFreeは文字用に max-width / white-space を足す＝〇が横だけ縮んで楕円になるので外す
    requestAnimationFrame(function(){ requestAnimationFrame(function(){
      d.style.removeProperty('max-width'); d.style.removeProperty('white-space');
    }); });
    myColsAdd(SHAPE_ST.col);      // 使った色はマイ色に残す（次も1クリックで同じ色）
    _lastShape=d;                 // 置いた直後は「これをいじっている」＝色替えがすぐ効く
    markDirty();
    try{ closeMenu(); }catch(_){}
    curEl=d; selEls=[d]; d.classList.add('__ce_sel');
    try{ showHandles(d); }catch(_){}
    if(msg) msg.textContent='🔶 図形を置きました（掴んで移動／■で大きさ／右クリックで動き・重なり順）';
    return d;
  }
  function openShapeBar(){
    var old=document.getElementById('__ce_shapebar');
    if(old){ old.remove(); if(msg) msg.textContent='図形バーを閉じました'; return; }   // もう一度押すと閉じる
    var bar=document.createElement('div'); bar.id='__ce_shapebar';
    bar.setAttribute('style','position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:2147483000;display:flex;align-items:center;gap:7px;background:#fff;border:1px solid #c9c9d2;border-radius:14px;box-shadow:0 10px 30px rgba(0,0,0,.26);padding:8px 12px;font:13px/1.4 system-ui,sans-serif;max-width:96vw;flex-wrap:wrap');
    function btn(id,lb,bg){
      return '<button data-sb="'+id+'" style="background:'+(bg||'#f2f2f4')+';color:'+(bg?'#fff':'#1d1d1f')+';border:1px solid '+(bg||'#d5d5dc')+';border-radius:9px;padding:6px 11px;cursor:pointer;font:inherit">'+lb+'</button>';
    }
    function render(){
      var chips=myColsGet().map(function(c){
        return '<span class="__ce_shc" data-c="'+c+'" title="'+esc(c)+'" style="display:inline-block;width:22px;height:22px;border-radius:6px;border:2px solid '+(c===SHAPE_ST.col?'#0b6bcb':'rgba(0,0,0,.18)')+';background:'+c+';cursor:pointer;vertical-align:middle"></span>';
      }).join('');
      var shapes=Object.keys(SHAPE_DEF).map(function(k){ return btn(k, SHAPE_DEF[k].lb); }).join('');
      var icons=ICON_DEF.map(function(a){
        return '<button data-sb="ic:'+a[0]+'" title="'+esc(a[1])+'（丸アイコンを置く）" style="background:#f2f2f4;color:#1d1d1f;'
          +'border:1px solid #d5d5dc;border-radius:50%;width:28px;height:28px;padding:0;cursor:pointer;font:inherit;font-size:14px;line-height:1">'+a[0]+'</button>';
      }).join('');
      bar.innerHTML='<b style="font-size:12px;color:#5b6472">🔶 図形</b>'
        +shapes
        +'<span style="width:1px;height:22px;background:#e2e2e8"></span>'
        +'<b style="font-size:12px;color:#5b6472">アイコン</b>'+icons
        +'<span style="width:1px;height:22px;background:#e2e2e8"></span>'
        +btn('fill', SHAPE_ST.fill?'● 塗り':'○ フチだけ')
        +btn('water', SHAPE_ST.water?'🎨 水彩 ON':'🎨 水彩', SHAPE_ST.water?'#0ea5a3':null)
        +btn('opm','－ 薄く')+btn('opp','＋ 濃く')
        +'<span style="font-size:11px;color:#5b6472">'+Math.round(SHAPE_ST.op*100)+'%</span>'
        +'<input type="color" id="__ce_shcol" value="'+SHAPE_ST.col+'" title="色を選ぶ（このPCに記憶）" style="width:38px;height:28px;padding:0;border:1px solid #d5d5dc;border-radius:7px;cursor:pointer">'
        +chips
        +'<span style="width:1px;height:22px;background:#e2e2e8"></span>'
        +btn('x','✕');
    }
    function repaintSel(){    // 直前に置いた／触った図形に、色と塗りの変更をその場で反映
      var t=(_lastShape&&document.contains(_lastShape))?_lastShape
           :((curEl&&curEl.getAttribute&&curEl.getAttribute('data-ceshape'))?curEl:null);
      if(t){
        if(SHAPE_ST.water) t.setAttribute('data-cewater','1');
        else { t.removeAttribute('data-cewater'); t.removeAttribute('data-cewshape'); t.style.removeProperty('mix-blend-mode'); t.style.removeProperty('filter'); }
        shapePaint(t,SHAPE_ST.col,SHAPE_ST.fill,null); markDirty();
      }
    }
    render();
    document.body.appendChild(bar);
    // バーの中の操作はページ側へ伝えない（選択が外れる・ドラッグが始まるのを防ぐ）
    bar.addEventListener('mousedown',function(e){ e.stopPropagation(); });
    bar.addEventListener('click',function(e){
      e.stopPropagation();
      var b=e.target.closest('[data-sb]');
      if(b){
        var k=b.getAttribute('data-sb');
        if(k==='x'){ bar.remove(); return; }
        if(k==='fill'){ SHAPE_ST.fill=SHAPE_ST.fill?0:1; _shapeStSave(); render(); repaintSel(); return; }
        if(k==='water'){
          SHAPE_ST.water=SHAPE_ST.water?0:1; _shapeStSave(); render(); repaintSel();
          if(msg) msg.textContent=SHAPE_ST.water?'🎨 水彩ON：置く図形が「にじんだ絵の具」になります（選んでいる図形にもすぐ効きます）':'🎨 水彩をやめました';
          return;
        }
        if(k==='opm'||k==='opp'){                                   // 透明度（薄く／濃く）
          var v=shapeOpacity(k==='opm'?-0.1:0.1); render();
          if(msg) msg.textContent='🔶 濃さ '+Math.round(v*100)+'%（0に近いほど透明・次に置く図形にも効きます）';
          return;
        }
        if(k.indexOf('ic:')===0){ addShape('icon', k.slice(3)); render(); return; }   // 丸アイコン
        addShape(k); render(); return;
      }
      var c=e.target.closest('.__ce_shc');
      if(c){ SHAPE_ST.col=c.getAttribute('data-c'); _shapeStSave(); render(); repaintSel(); }
    });
    bar.addEventListener('input',function(e){
      if(e.target.id!=='__ce_shcol') return;
      SHAPE_ST.col=e.target.value; _shapeStSave(); repaintSel();
    });
    bar.addEventListener('change',function(e){ if(e.target.id==='__ce_shcol'){ myColsAdd(e.target.value); render(); } });
    if(msg) msg.textContent='🔶 図形バーを出しました（押すと画面の中央に置きます／色はこのPCに記憶します）';
  }
  // ★掴めない要素の救済（2026-07-20）：クローン元サイトの<header>等が pointer-events:none を
  //   持つことがある（固定ヘッダーの透明部分で下のページを触らせる定番の書き方）。
  //   pointer-events は「子へ受け継がれる」性質があるため、その中に置いた追加画像/文字まで
  //   まとめて反応しなくなり、右クリックもドラッグも一切効かない＝「見えているのに掴めない」。
  //   置いた要素だけ pointer-events:auto に戻す（元のCSSは触らないので他は無影響）。
  //   あわせて目印(__ceFree)を付ける＝この「後から乗せた飾り」だけは、絵が無い透明部分のクリックを
  //   下へ通す（_clearPixel）。大きな透過PNG（葉っぱ等）が下の見出しに覆いかぶさって
  //   「その辺り一帯が右クリックできない」のを防ぐため（実際に949x744が画面を覆った）。
  function _wakePlaced(el){
    if(!el||el.nodeType!==1) return;
    try{
      el.__ceFree=true;                                           // 保存HTMLには何も足さない（JS上の目印だけ）
      var p=el.parentElement;
      if(!p||getComputedStyle(p).pointerEvents!=='none') return;  // 置き先が普通ならここまで
      if(!el.style.pointerEvents) el.style.pointerEvents='auto';
    }catch(_){}
  }
  // 既存カンプの救済：上の対策より前に置かれた要素は「掴めないまま」保存されている。
  // 置いた要素の目印＝body/header/section/footer直下＋インラインに position:absolute と left と z-index
  // （placeFreeが必ず書く3点セット）。カンプ自身の要素はクラスCSSで配置するのでここには当たらない。
  function _scanPlaced(){
    try{
      [].slice.call(document.querySelectorAll('body>*,header>*,section>*,footer>*')).forEach(function(el){
        if(el.closest('[id^="__ce"]')) return;
        var s=el.style;
        if(s.position==='absolute' && s.left && s.zIndex) _wakePlaced(el);
      });
    }catch(_){}
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',_scanPlaced); else _scanPlaced();
  // 🖼 画像切れの警告（2026-07-20・AIなし）：クローンの画像は `<カンプ名>_files/` に入るが、
  //   このフォルダは容量が大きく同期対象外＝別PCでは出ない。フォルダごと失うこともある
  //   （実際に154枚中151枚が壊れたカンプで一日中作業していたのに、何の表示も出ていなかった）。
  //   読み込み後に数えて、多ければ編集バーに1行出すだけ。元URLが刻んであれば取り直し先も示す。
  function _warnBrokenImages(){
    setTimeout(function(){
      try{
        var imgs=[].slice.call(document.images).filter(function(i){ return !i.closest('#__ce'); });
        var bad=imgs.filter(function(i){ return i.complete && i.naturalWidth===0; });
        if(bad.length<3) return;                       // 1〜2枚は差し替え途中などの可能性＝黙る
        var m=document.querySelector('meta[name="ce-clone-src"]');
        var from=m?('　元URL: '+m.getAttribute('content')):'';
        var box=document.createElement('div');
        box.id='__ce_imgwarn';
        box.style.cssText='margin:6px 0;padding:6px 8px;background:#fff4e5;border:1px solid #f0c48a;'
          +'border-radius:7px;font-size:11.5px;line-height:1.6;color:#7a4a10';
        box.textContent='🖼 画像が '+bad.length+'/'+imgs.length+'枚 表示できません（素材フォルダが無い可能性）。'
          +'このまま本番化キットを作ると画像なしで渡ります。'+from;
        var host=document.getElementById('__ce_msg');
        if(host&&host.parentNode) host.parentNode.insertBefore(box, host);
      }catch(_){}
    }, 2500);   // 遅れて読み込まれる画像を巻き込まないよう少し待つ
  }
  // ページを開いたら、前に作った飾りにも「クリックして調整できる」状態を付け直す（古いカンプでも効く）
  // 🧯 すでに焼き込まれてしまった「強制表示」を開いた時に剥がす（2026-07-29・既存カンプの自己修復）。
  //   印(data-cesafe)が付く前に保存されたぶんは、保険が書く3点セットが揃っている事だけが手がかり：
  //   opacity:1 !important ＋ animation-name:none ＋ transform:none。デザインの意図では有り得ない組み合わせ。
  //   剥がした結果まだ透明なら、2.5秒後に保険がまた見せる＝真っ白になる事故は起きない。
  function _unbakeSafety(){
    var n=0;
    try{
      [].slice.call(document.querySelectorAll('[style*="opacity"]')).forEach(function(el){
        if(!el.style||(el.closest&&(el.closest('#__ce')||el.closest('#__op_screen')))) return;
        if(el.classList&&el.classList.contains('fxa_pre')) return;          // ツールで付けた動きは触らない
        if(el.style.getPropertyValue('opacity')!=='1') return;
        if(el.style.getPropertyPriority('opacity')!=='important') return;
        if(el.style.animationName!=='none') return;                        // 保険が animation:none を当てた印
        el.style.removeProperty('opacity'); el.style.removeProperty('animation');
        if(el.style.getPropertyValue('transform')==='none') el.style.removeProperty('transform');
        n++;
      });
    }catch(_){}
    if(n && msg) msg.textContent='🧯 動かなくなっていた出現アニメ '+n+' 個を元に戻しました（保険の焼き込みを剥がしました・💾保存で確定）';
    return n;
  }
  // 🧯 クローンの保険(__clone_safety)は4秒後に「透明な要素」を全部 opacity:1!important で見せる。
  //   打ち込み待ちの文字(.fxa_ch)まで巻き込むため、タイプライター等が一度も再生されない（2026-07-30）。
  //   ★_unbakeSafety とは別物：あちらは animation-name:none を目印にするが、クローンの保険は
  //     opacity と transform しか書かないので素通りしていた。ここで文字アニメ側だけを後始末する。
  //   ★インラインの !important はCSSでは上書きできない＝JSで剥がすしかない。
  function _fixCharAnimSafety(){
    var hosts=[], n=0;
    try{
      [].slice.call(document.querySelectorAll('[class*="fxa_"] [style*="opacity"],[class*="fxa_"][style*="opacity"]')).forEach(function(el){
        if(el.closest && el.closest('[id^="__ce"]')) return;
        if(el.style.getPropertyValue('opacity')!=='1') return;
        if(el.style.getPropertyPriority('opacity')!=='important') return;
        el.style.removeProperty('opacity');
        if(el.style.getPropertyValue('transform')==='none') el.style.removeProperty('transform');
        var h=el.closest('.fxa_pre,.fxa_cpre,.fxa_tw,.fxa_sk,.fxa_lines,.fxa_wave');
        if(h && hosts.indexOf(h)<0) hosts.push(h);
        n++;
      });
      // 剥がしただけでは「出たまま」なので、その場で再生し直す（void offsetWidth＝やり直しの合図）
      hosts.forEach(function(h){ if(h.classList.contains('fxa_in')){ h.classList.remove('fxa_in'); void h.offsetWidth; h.classList.add('fxa_in'); } });
    }catch(_){}
    return n;
  }
  // 🧯 ⏳遅らせのせいで「開いた瞬間ファーストビューが真っ白」になるのを自動で防ぐ（2026-07-30・要望）
  // ★実際に踏んだ形：<header> に data-cedelay="3600" が付いていて、その中にヒーローの
  //   スライドショー(1911×948)が入っていた＝ヘッダーが出るまで3.6秒、画面全部が空っぽ。
  //   ヘッダー自身の箱は 1440×64 と小さいので、**中の要素まで見ないと気づけない**。
  // 小さい飾りの遅らせは普通の演出なので触らない。「画面の3割以上を覆う」物だけが対象。
  function _fixBlankingDelay(){
    var vw=window.innerWidth||1, vh=window.innerHeight||1, hit=[];
    function cover(r){
      var w=Math.min(r.right,vw)-Math.max(r.left,0), h=Math.min(r.bottom,vh)-Math.max(r.top,0);
      return Math.max(0,w)*Math.max(0,h);
    }
    try{
      [].slice.call(document.querySelectorAll('.fxa_pre[data-cedelay],[data-fxa-fly][data-cedelay]')).forEach(function(el){
        if(el.closest&&el.closest('[id^="__ce"]')) return;
        var d=+el.getAttribute('data-cedelay')||0; if(d<600) return;   // 一瞬の遅れは邪魔にならない
        var r=el.getBoundingClientRect();
        if(!(r.bottom>0&&r.top<vh)) return;                            // 画面外＝スクロールで見る物は関係ない
        var big=cover(r), kids=el.querySelectorAll?el.querySelectorAll('*'):[];
        for(var i=0;i<kids.length&&i<300;i++){ big=Math.max(big,cover(kids[i].getBoundingClientRect())); }
        if(big < vw*vh*0.30) return;                                   // 小さい物の遅らせは演出＝そのまま
        try{ pushUndo(el); }catch(_){}
        el.removeAttribute('data-cedelay');
        el.classList.add('fxa_in');                                    // その場で出す（開き直さなくていい）
        hit.push(Math.round(d));
      });
    }catch(_){}
    if(hit.length && msg){
      msg.textContent='🧯 開いた時に画面が'+(Math.max.apply(null,hit)/1000).toFixed(1)
        +'秒ほど真っ白になる設定（⏳遅らせ）が'+hit.length+'件あったので外しました'
        +'（画面の大部分を覆う要素の遅らせだけが対象・⟲で戻せます・💾保存で確定）';
      markDirty();
    }
    return hit.length;
  }
  function _bootDeco(){
    _warnBrokenImages(); try{ dqArm(); }catch(_){ } try{ opUpgrade(); }catch(_){ } try{ _unbakeSafety(); }catch(_){ }
    try{ _fixBlankingDelay(); }catch(_){ }
    try{ _fixCharAnimSafety(); }catch(_){ }
    // 古いクローンは保険が内蔵されたまま＝4秒後にまた塗られるので、その直後にもう一度だけ後始末する
    if(document.getElementById('__clone_safety')){
      setTimeout(function(){
        var k=_fixCharAnimSafety();
        if(k && msg) msg.textContent='🧯 古い保険が文字アニメを止めていたので直しました（'+k+'個・💾保存で確定）';
      }, 4300);
    }
  }
  if(document.readyState==='complete') _bootDeco();
  else window.addEventListener('load', _bootDeco);
  // 🖼 画像を追加：画像要素を置く→すぐドラッグで移動できる（差し替え・サイズ調整は右クリックで）。
  // px/py（ページ座標）を渡すとそこへ置く＝右クリックメニューの「ここに画像を追加」用。省略時は画面中央あたり。
  function insertImageEl(url, idx, px, py){
    idx=idx||0;
    var img=document.createElement('img'); img.src=url;
    var x=(px!=null?Math.round(px):Math.round((window.scrollX||window.pageXOffset||0)+window.innerWidth*0.30))+idx*24;
    var y=(py!=null?Math.round(py):Math.round((window.scrollY||window.pageYOffset||0)+window.innerHeight*0.32))+idx*24;
    // ★z-index はここで決め打ちしない：placeFree が「置く場所の真下にある物より手前」を計算する。
    //   ここで先に入れてしまうと placeFree の計算が使われず、大きなz-index（例:2147480000のセクション）
    //   の上に置いた画像が永久に裏へ潜る（実測でここに引っかかった・2026-07-30）。
    img.setAttribute('style','width:260px;height:auto;cursor:move');
    placeFree(img, x, y);
    markDirty();
    if(idx===0){ try{ img.scrollIntoView({block:'center'}); }catch(_){} }
    if(typeof setDragOn==='function'){ if(typeof curEl!=='undefined' && curEl) curEl.classList.remove('__ce_sel'); img.classList.add('__ce_sel'); setDragOn(img); }
  }
  // 🔍 画像ピッカー共通の検索ボックス（2026-07-19）：キャプション＋ファイル名で絞り込み（AIなし）。
  //   AIが裏で付けている1行キャプション（「花畑のイラスト」等）が検索対象になるので、
  //   「花」「人物」のように内容の言葉で探せる。画像が少ない時（8枚以下）は出さない。
  function attachPickerSearch(ov){
    var bx=ov.querySelector('.bx'), gr=ov.querySelector('.gr');
    if(!bx||!gr||gr.querySelectorAll('.it').length<9) return;
    var inp=document.createElement('input');
    inp.type='text'; inp.placeholder='🔍 検索（例：花 / 人物 / ロゴ）';
    inp.style.cssText='display:block;width:100%;box-sizing:border-box;margin:0 0 10px;padding:8px 10px;border:1px solid #ccc;border-radius:8px;font-size:13px';
    bx.insertBefore(inp, gr);
    inp.addEventListener('input',function(){
      var q=inp.value.trim().toLowerCase();
      [].slice.call(gr.querySelectorAll('.it')).forEach(function(it){
        var t=(it.textContent||'').toLowerCase();
        it.style.display=(!q||t.indexOf(q)>=0)?'':'none';
      });
    });
    inp.addEventListener('click',function(e){ e.stopPropagation(); });
    setTimeout(function(){ try{ inp.focus(); }catch(_){} },0);
  }
  function openAddImagePicker(px, py){
    fetch('/api/uploads').then(function(r){return r.json();}).then(function(d){
      var ups=d.uploads||[];
      // ★2026-07-25：「✂ 背景を除去」ボタンをここに出す。押した時の処理(data-rmbg)と
      //   API(/api/remove_bg・rembg)は前からあったのに、ボタンだけどこにも描かれておらず
      //   ＝機能があるのに一生たどり着けない状態だった（発見できない機能は無い機能と同じ）。
      var items = ups.length
        ? ups.map(function(u){return '<div class="it" data-src="'+u.url+'" style="position:relative"><img src="'+u.url+'"><span>'+esc(u.caption||u.file)+'</span>'
            +'<button data-rmbg="'+esc(u.file)+'" title="人物や商品を切り抜いて透過PNGにする（このPCの中だけで処理・無料・数秒）" style="position:absolute;right:5px;top:5px;background:rgba(17,17,17,.78);color:#fff;border:none;border-radius:6px;padding:3px 7px;font-size:11px;cursor:pointer;font-family:inherit">✂ 背景を除去</button>'
            +'</div>';}).join('')
        : '<div style="color:#999">まだアップロード画像がありません。下から新しく追加できます</div>';
      var ov=document.createElement('div'); ov.id='__ce_pk';
      ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span><h4>🖼 画像を追加</h4>'
        +'<label class="go2" style="display:block;text-align:center;background:#1a7f37;cursor:pointer;margin-bottom:10px">＋ 新しい画像をアップロード<input type="file" id="__ce_addimgfile" accept="image/*" multiple style="display:none"></label>'
        +'<div class="gr">'+items+'</div></div>';
      document.body.appendChild(ov);
      attachPickerSearch(ov);
      ov.addEventListener('click',function(e){
        if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
        var rb=e.target.closest('[data-rmbg]');
        if(rb){ e.stopPropagation();
          var rfn=rb.getAttribute('data-rmbg');
          rb.textContent='除去中…（初回はモデル取得で少し待ちます）'; rb.disabled=true;
          fetch('/api/remove_bg',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:rfn})})
            .then(function(r){return r.json();}).then(function(dd){
              if(!dd.ok){ rb.textContent='✂ 背景を除去'; rb.disabled=false; msg.textContent='背景除去に失敗：'+(dd.message||''); return; }
              ov.remove(); openAddImagePicker(px,py);
              msg.textContent='背景を除去した透過画像を追加しました（一覧の先頭）。クリックで配置できます';
            }).catch(function(){ rb.textContent='✂ 背景を除去'; rb.disabled=false; msg.textContent='通信エラー'; });
          return;
        }
        var it=e.target.closest('.it'); if(it){ ov.remove(); insertImageEl(it.dataset.src, 0, px, py); msg.textContent='画像を追加しました。ドラッグで置く（💾保存で確定）'; }
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
          added.forEach(function(u,i){ insertImageEl(u.url, i, px, py); });
          msg.textContent=(added.length||files.length)+'枚 画像を追加しました。ドラッグで置く（💾保存で確定）';
        }).catch(function(){ msg.textContent='通信エラー'; });
      });
    }).catch(function(){msg.textContent='画像一覧の取得に失敗';});
  }
  // ===== 🖼 スライドショー（画像が次々切り替わる・AIなし） =====
  // 右クリックした画像の場所で、選んだ画像を「間隔◯秒・フェード◯秒」で自動フェード切り替え。
  // 仕組み＝元サイトのJSはコピーせず、自前のミニ実行スクリプト(#__sl_run)を「中身」として焼き込む
  // （オープニング演出の#__op_runと同じ流儀＝保存すれば単体HTMLでも動く）。
  // ⚠命名注意：スクリプト本文・ラッパー属性に「__ce」「data-ce」を含めないこと。
  //   保存時の掃除がscriptを/__ce/で除去し、⭐部品保存がdata-ce*属性を剥がすため（実装済みの仕様）。
  //   だから data-slshow / data-slint / data-sldur / __sl_run という名前にしてある。
  // ★切り替え方式（2026-07-30 改良）：古い画像と新しい画像を同時にフェードさせると、中間で
  //   両方が半透明になり下地が透けて「白く抜ける谷」ができる（フェード2.5秒なら白い時間も2〜3秒）。
  //   → 新しい画像を「上に重ねてフェードイン」させ、隠れ切ってから古い画像を消す。谷が出ない。
  // ★フェード≧間隔だと setTimeout が次の回と重なり、共有変数 cur が入れ替わって
  //   「消えたまま／同じ画像のまま」になる。①次の番号を local(nx) で捕まえる ②フェードを間隔未満に丸める。
  var SL_RUN='(function(){function boot(){var ws=document.querySelectorAll("[data-slshow]");for(var i=0;i<ws.length;i++)(function(w){if(w.__slOn)return;w.__slOn=1;var imgs=[].slice.call(w.querySelectorAll("img"));if(imgs.length<2)return;var iv=parseInt(w.getAttribute("data-slint"))||4000;var du=parseInt(w.getAttribute("data-sldur"))||1200;if(du+150>iv)du=Math.max(200,iv-150);var cur=0;for(var j=0;j<imgs.length;j++){var im=imgs[j];im.style.transition="opacity "+(du/1000)+"s";if(!im.style.position)im.style.position=(j===0?"relative":"absolute");im.style.zIndex=(j===0?"1":"0");im.style.setProperty("opacity",j===0?"1":"0","important");}setInterval(function(){var pv=cur,nx=(cur+1)%imgs.length;cur=nx;imgs[nx].style.zIndex="2";imgs[nx].style.setProperty("opacity","1","important");setTimeout(function(){imgs[pv].style.setProperty("opacity","0","important");imgs[pv].style.zIndex="0";imgs[nx].style.zIndex="1";},du+80);},iv);})(ws[i]);}window.__slBoot=boot;if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",boot);else boot();})();';
  var SL_VER='3';   // 記録用（実際の入れ替え判定は本体の文字列比較。上げ忘れても事故らない）
  function ensureSlRun(){
    var s=document.getElementById('__sl_run');
    // ★版番号(data-slver)ではなく**本体の文字列そのもの**を比べる（2026-07-30 作り直し）。
    //   実害を出した地雷：SL_RUNの中身を直したのに SL_VER を上げ忘れると、保存済みカンプは
    //   「data-slver=2 なのに本体は旧方式」のまま＝版チェックが一致して永久に入れ替わらない。
    //   実際にユーザーのカンプがその状態だった（本体は旧方式・属性は2）。文字列比較なら上げ忘れが起きない。
    if(s && s.textContent!==SL_RUN){
      s.remove(); s=null;
      // ⚠古い版の setInterval は止める手段が無い（タイマーIDを持っていない）。
      //   そこで画像を作り直して参照を切る＝古いタイマーは外れた幽霊要素を触るだけになり無害化される。
      [].slice.call(document.querySelectorAll('[data-slshow]')).forEach(function(w){
        w.__slOn=0;
        [].slice.call(w.querySelectorAll('img')).forEach(function(im){
          var c=im.cloneNode(true);
          c.style.removeProperty('opacity'); c.style.removeProperty('transition');
          c.style.removeProperty('z-index');
          if(im.parentNode) im.parentNode.replaceChild(c,im);
        });
      });
    }
    if(!s){
      s=document.createElement('script'); s.id='__sl_run'; s.setAttribute('data-slver',SL_VER); s.textContent=SL_RUN;
      document.body.appendChild(s);   // createElementで足すと今すぐ実行される＝編集画面でもその場で動き出す
    }
    if(window.__slBoot) window.__slBoot();
  }
  // 🖼 スライドショーは常に「いちばん奥」に置く（2026-07-30・要望）。
  // ★これが無いと、あとから追加した文字や画像がスライドの裏に潜って見えない。
  //   スライドの入れ物がクローン元の大きな z-index（実例：49）を持っていて、新しく置いた物に勝つため。
  //   入れ物を z-index:0 にすれば、中の画像（0/1/2）はその中だけで重なり、外に置いた物には必ず負ける。
  function slideToBack(){
    var n=0;
    try{
      [].slice.call(document.querySelectorAll('[data-slshow]')).forEach(function(w){
        if(w.style.getPropertyValue('z-index')!=='0'){ w.style.setProperty('z-index','0'); n++; }
      });
    }catch(_){}
    return n;
  }
  // 既に作ってあるスライドショーも開いた時に新方式へ入れ替える（作り直さなくても直る）
  (function(){
    // 実体が無くても「古い再生スクリプトだけ残っている」ページは入れ替える。
    // これが無いと、開いたままのページに古い版が居座り、直したのに白く抜けたまま＝原因が分からなくなる。
    function up(){
      if(document.querySelector('[data-slshow]')||document.getElementById('__sl_run')){
        try{ ensureSlRun(); }catch(_){ }
      }
      try{ slideToBack(); }catch(_){ }
    }
    if(document.readyState==='complete') setTimeout(up,300); else window.addEventListener('load',function(){ setTimeout(up,300); });
  })();
  // 🖼 スライドショーを「ダブルクリック」→ 写真を選び直す画面（2026-07-30・要望で1回クリックから変更）。
  // ★click / dblclick イベントで判定してはいけない（§7 ㉖と同じ地雷）：ドラッグ機構が mousedown を
  //   掴んで preventDefault するので、実際のマウス操作では飛んで来ないことがある。
  //   mouseup を自分で数える＝「400ms以内に2回・どちらも移動4px未満」をダブルクリックと見なす。
  (function(){
    var dx=0, dy=0, lastT=0, lastX=0, lastY=0;
    document.addEventListener('mousedown',function(e){ dx=e.clientX; dy=e.clientY; },true);
    document.addEventListener('mouseup',function(e){
      if(e.button!==0) return;                                            // 右クリックは今までどおりメニュー
      if(Math.abs(e.clientX-dx)>4||Math.abs(e.clientY-dy)>4){ lastT=0; return; }  // ドラッグ＝移動なので数えない
      var now=Date.now(), near=(Math.abs(e.clientX-lastX)<8&&Math.abs(e.clientY-lastY)<8);
      var dbl=(now-lastT<400)&&near;
      lastT=dbl?0:now; lastX=e.clientX; lastY=e.clientY;                  // 3回目が続けて反応しないよう1回リセット
      if(!dbl) return;
      if(window.__ceInspOn||window.__ceFlyMode) return;                   // 🔍/🕊 モード中は邪魔しない
      var t=e.target;
      if(!t||!t.closest||t.closest('[id^="__ce"]')) return;               // ツールのUIの上は無視
      var w=t.closest('[data-slshow]'); if(!w) return;
      if(document.getElementById('__ce_pk')) return;                      // 画像パネルを開いている最中は二重に出さない
      try{ slidePanel(w.querySelector('img'), w); }catch(_){}
    });
  })();
  // スライドショーを解除して1枚目だけに戻す
  function slideUndo(w0){
    try{ pushUndo(w0.parentElement||w0); }catch(_){}
    var base=w0.querySelector('img');
    if(base){
      base.style.removeProperty('opacity'); base.style.removeProperty('transition');
      base.style.removeProperty('z-index');
      w0.parentNode.insertBefore(base,w0);
    }
    w0.remove(); markDirty();
    msg.textContent='🖼 スライドショーを解除して1枚目の画像に戻しました（間違えたら ⟲戻す・💾保存で確定）';
  }
  function slideMake(el,cx,cy){
    // ★すでにスライドショーの中で押された時は「無言で解除」しない（2026-07-30・作り直し）。
    //   旧仕様は押すたびにON/OFFが入れ替わる作りで、設定を変えようともう一度押した人が
    //   気づかないまま解除していた（＝スライドが1枚に戻り「同じ画像しか出ない」に見える）。
    //   実際にユーザーのカンプが、実行スクリプトだけ残って入れ物が無い＝解除された跡の状態だった。
    //   今は「画像を選び直す」画面を開き、解除はその中の赤いボタンだけにする。
    var w0=el&&el.closest&&el.closest('[data-slshow]');
    if(w0){ slidePanel(w0.querySelector('img'), w0); return; }
    // ★<picture>やアニメのラッパー、figure等の「入れ物」が選ばれている事がある。
    //   中に画像が1枚だけならそれを対象にする＝「画像の上で右クリックして」で弾かれて
    //   スライドショーが作れない、を防ぐ（2026-07-30・実報告。解除したあと作れない原因）。
    //   ★ただし「入れ物の中に1枚だけ」で拾うのは危険：大きな箱（セクション等）に1枚しか画像が
    //     無いと、離れた場所の画像が対象になる（「一番下のセクションに追加される」報告・2026-07-30）。
    //     まず右クリックした座標の真下にある画像を探し、それが取れない時だけ小さな入れ物に限って拾う。
    if(el && el.tagName!=='IMG'){
      var _hit=null;
      try{
        if(cx!=null&&cy!=null){
          var _us=document.elementsFromPoint(cx,cy);
          for(var _i=0;_i<_us.length;_i++){
            var _u=_us[_i];
            if(_u&&_u.tagName==='IMG'&&!(_u.closest&&_u.closest('[id^="__ce"]'))){ _hit=_u; break; }
          }
        }
      }catch(_){}
      // ★「中に1枚だけ」の縛りをやめた（2026-07-30・実報告「画像をクリックしないと画面が出ない」）。
      //   タグ名の決め打ちもやめる（div/li等で包まれた写真が拾えず入口で弾かれていた）。
      //   代わりに大きさで安全を確保：入れ物が「いちばん大きい画像の3倍」以内なら、その写真の
      //   入れ物と見なして中の最大画像を拾う。セクションのような大きな箱では拾わない（遠くの画像に
      //   付いてしまう事故＝「一番下のセクションに追加される」報告の再発防止）。
      if(!_hit && el.querySelectorAll){
        var _ims=[].slice.call(el.querySelectorAll('img')).filter(function(im){
          if(im.closest&&im.closest('[id^="__ce"]')) return false;
          var r=im.getBoundingClientRect(); return r.width>=24&&r.height>=24;
        });
        if(_ims.length){
          _ims.sort(function(a,b){
            var ra=a.getBoundingClientRect(), rb=b.getBoundingClientRect();
            return (rb.width*rb.height)-(ra.width*ra.height);
          });
          var _br=_ims[0].getBoundingClientRect(), _er=el.getBoundingClientRect();
          if((_er.width*_er.height)<=(_br.width*_br.height)*3+40000) _hit=_ims[0];
        }
      }
      if(_hit) el=_hit;
    }
    if(!el||el.tagName!=='IMG'){
      msg.textContent='スライドショーにする写真が決められませんでした。写真の上で右クリックしてください'
        +'（大きな箱やセクションの上で押すと、離れた画像に付いてしまうので拾いません）';
      return;
    }
    slidePanel(el, null);
  }
  // 画像を選ぶ画面。wrap を渡すと「作り直し（選び直し）」モードになる
  function slidePanel(el, wrap){
    if(!el){ msg.textContent='元になる画像が見つかりませんでした'; return; }
    // ★window.prompt をやめてパネル内の入力欄にした（2026-07-30・実報告）。
    //   Chromeは「このページでこれ以上ダイアログを表示しない」を一度チェックすると、以降 prompt() が
    //   問答無用で null を返す。すると isNaN で静かに return ＝「ボタンを押しても何も反応しない」に見える。
    //   ブラウザのダイアログに依存しない作りにすれば、この事故は起きない。
    var edit=!!wrap;
    // 作り直しモードでは今の設定・今選ばれている画像を初期値にする（＝「選び直し」ができる）
    var iv=edit?((parseInt(wrap.getAttribute('data-slint'))||4000)/1000):4;
    var fd=edit?((parseInt(wrap.getAttribute('data-sldur'))||1200)/1000):1.2;
    var now=[];
    if(edit){
      // ★1枚目も選び直せるようにする（2026-07-31・報告「2枚目しか画像を選べない」）。
      //   以前は slice(1)＝1枚目を「変えられない土台」として一覧から外していた。
      //   今は全部を候補に入れ、選んだ順の1番目がそのまま1枚目になる。
      var _all=[].slice.call(wrap.querySelectorAll('img'));
      now=_all.map(function(im){ return im.getAttribute('src')||''; });
    }
    fetch('/api/uploads').then(function(r){return r.json();}).then(function(d){
      var ups=d.uploads||[];
      if(!ups.length){ msg.textContent='アップロード画像がまだありません。先に「🖼 画像を追加」からアップロードしてください'; return; }
      var picked=now.slice();
      var items=ups.map(function(u){return '<div class="it" data-src="'+u.url+'" style="position:relative"><img src="'+u.url+'"><span>'+esc(u.caption||u.file)+'</span><b data-badge="1" style="display:none;position:absolute;left:6px;top:6px;background:#1a7f37;color:#fff;border-radius:50%;width:22px;height:22px;text-align:center;line-height:22px;font-size:12px"></b></div>';}).join('');
      var ov=document.createElement('div'); ov.id='__ce_pk';
      ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span>'
        +'<h4>'+(edit
            ? ('🖼 スライドショーを選び直す（今は全'+now.length+'枚。緑の番号＝出る順番。1枚目から選び直せます）')
            : '🖼 スライドショーにする画像を順番にクリック（右クリックした今の画像が1枚目・選んだ順に続く）')+'</h4>'
        +'<div class="gr">'+items+'</div>'
        +'<div style="display:flex;gap:12px;align-items:center;margin-top:10px;font-size:12.5px;color:#333">'
        +'<label>切り替え間隔 <input id="__ce_sliv" type="number" value="'+iv+'" min="1" max="60" step="0.5" style="width:64px">秒</label>'
        +'<label>フェード <input id="__ce_slfd" type="number" value="'+fd+'" min="0.2" max="10" step="0.1" style="width:64px">秒</label>'
        +'<span style="color:#888;font-size:11px">ゆっくり切り替えたいならフェード2〜3秒</span></div>'
        +'<button class="go2" id="__ce_slok" style="display:block;width:100%;background:#1a7f37;margin-top:10px">✔ '
        +(edit?'この順番に作り直す':'この順番でスライドショー化')+'（'+picked.length+'枚選択中）</button>'
        // ★解除はここだけ（右クリックの押し直しでは解除しない＝気づかず消える事故の防止）
        +(edit?('<button class="go2" id="__ce_sloff" style="display:block;width:100%;background:#c0392b;margin-top:6px">🗑 スライドショーを解除して1枚に戻す</button>'):'')
        +'</div>';
      document.body.appendChild(ov);
      var okb=ov.querySelector('#__ce_slok');
      var sync=function(){
        [].forEach.call(ov.querySelectorAll('.it'),function(x){
          var b=x.querySelector('[data-badge]'), n=picked.indexOf(x.getAttribute('data-src'));
          // 番号＝出る順番。選び直しでは1枚目から選ぶので n+1、新規では今の画像が1枚目なので n+2
          if(b){ b.style.display=n>-1?'block':'none'; b.textContent=n>-1?String(n+(edit?1:2)):''; }
          x.style.outline=n>-1?'3px solid #1a7f37':'';
        });
        okb.textContent='✔ '+(edit?'この順番に作り直す':'この順番でスライドショー化')+'（'+picked.length+'枚選択中）';
      };
      sync();   // 作り直しモードでは開いた時点で今の選択が緑の番号で見える
      ov.addEventListener('click',function(e){
        if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
        if(e.target.id==='__ce_sloff'){ ov.remove(); slideUndo(wrap); return; }
        var it=e.target.closest('.it');
        if(it){
          var src=it.getAttribute('data-src'), ix=picked.indexOf(src);
          if(ix>-1) picked.splice(ix,1); else picked.push(src);
          sync();
          return;
        }
        if(e.target.id==='__ce_slok'){
          if(!picked.length){ okb.textContent='⚠ 切り替え先の画像を1枚以上クリックで選んでください'; return; }
          // 選び直しは1枚目も含めて選ぶので、2枚ないとスライドショーにならない
          if(edit&&picked.length<2){ okb.textContent='⚠ 2枚以上えらんでください（1枚だと切り替わりません）'; return; }
          // 秒数はパネルの入力欄から読む（ブラウザのダイアログに頼らない）
          var _iv=parseFloat((ov.querySelector('#__ce_sliv')||{}).value); if(!isNaN(_iv)) iv=Math.max(1,Math.min(60,_iv));
          var _fd=parseFloat((ov.querySelector('#__ce_slfd')||{}).value); if(!isNaN(_fd)) fd=Math.max(0.2,Math.min(10,_fd));
          ov.remove();
          var base=el, box=wrap;
          var br=''; try{ br=getComputedStyle(base).borderRadius; }catch(_){}
          if(edit){
            // 追加ぶんだけ捨てて作り直す。★元画像はクローンで置き換える＝止められない古い
            //   setInterval の参照を切る（タイマーIDを持っていないので、これが唯一の止め方）
            var b0=box.querySelector('img');
            [].slice.call(box.querySelectorAll('img')).forEach(function(im){ if(im!==b0) im.remove(); });
            var c0=b0.cloneNode(true);
            ['opacity','transition','z-index'].forEach(function(p){ c0.style.removeProperty(p); });
            c0.src=picked[0];        // ★1枚目も選んだとおりに差し替える（土台の画像を固定しない）
            b0.parentNode.replaceChild(c0,b0); base=c0;
            box.__slOn=0;
          } else {
            var bcs=null; try{ bcs=getComputedStyle(base); }catch(_){}
            var disp=bcs?bcs.display:'';
            var brc=base.getBoundingClientRect();
            // ★元画像が「自由配置(absolute/fixed)」だと、包んだ入れ物の中身が全部absoluteになり
            //   入れ物の高さが0になる。重ねた画像は inset:0 なので0サイズ＝見えない。
            //   結果：元画像だけ見えて「同じ画像のまま」、しかも元画像が消える番になると
            //   何も無い＝「白い空白が長い」になる（2026-07-30・実報告の正体）。
            //   → 位置は入れ物へ移し、元画像は入れ物いっぱいに敷き直す。
            var abs=!!(bcs&&(bcs.position==='absolute'||bcs.position==='fixed'));
            box=document.createElement('span');
            box.setAttribute('data-slshow','1');
            if(abs){
              var st='position:'+bcs.position+';display:block;line-height:0'
                +';width:'+Math.round(brc.width)+'px;height:'+Math.round(brc.height)+'px';
              ['left','top','right','bottom','zIndex','margin','marginLeft','marginTop','translate','rotate','scale','transform','transformOrigin'].forEach(function(p){
                var v=base.style[p]; if(v) st+=';'+p.replace(/[A-Z]/g,function(c){return '-'+c.toLowerCase();})+':'+v;
              });
              box.style.cssText=st;
              base.parentNode.insertBefore(box,base); box.appendChild(base);
              // 中の元画像は入れ物いっぱいへ（位置指定は入れ物が持つので消す）
              ['left','top','right','bottom','translate','rotate','scale','transform','margin','margin-left','margin-top'].forEach(function(p){ base.style.removeProperty(p); });
              base.style.setProperty('position','absolute','important');
              base.style.setProperty('inset','0','important');
              base.style.setProperty('width','100%','important');
              base.style.setProperty('height','100%','important');
              if(!base.style.objectFit) base.style.setProperty('object-fit','cover','important');
            } else {
              // 通常配置：元画像が大きさの基準（下敷き）。
              // ★幅を実測px で必ず入れる（2026-07-30・ユーザー指摘「一番大きい幅に合わせられている？」）。
              //   display:block の入れ物は幅が"親いっぱい"になる＝重ねた画像(width:100%)が
              //   元の写真より横に伸びて別サイズで表示される。写真の実寸に釘付けする。
              //   高さも実測で下限を入れる（元画像が読み込み前だと0になり「見えない」事故になる）。
              box.style.cssText='position:relative;display:'+((disp==='inline'||disp==='inline-block')?'inline-block':'block')+';max-width:100%;line-height:0'
                +(brc.width>4?(';width:'+Math.round(brc.width)+'px'):'')
                +(brc.height>4?(';min-height:'+Math.round(brc.height)+'px'):'');
              base.parentNode.insertBefore(box,base); box.appendChild(base);
            }
          }
          box.setAttribute('data-slint',Math.round(iv*1000));
          box.setAttribute('data-sldur',Math.round(fd*1000));
          // 選び直しでは picked[0] を土台（1枚目）に当てたので、重ねるのは2枚目から
          (edit?picked.slice(1):picked).forEach(function(u){
            var im=document.createElement('img'); im.src=u;
            im.setAttribute('style','position:absolute;inset:0;width:100% !important;height:100% !important;object-fit:cover;margin:0;opacity:0'+(br&&br!=='0px'?(';border-radius:'+br):''));
            box.appendChild(im);
          });
          ensureSlRun();
          slideToBack();
          markDirty();
          msg.textContent='🖼 スライドショーを'+(edit?'作り直しました':'作りました')
            +'（間隔'+iv+'秒・フェード'+fd+'秒・全'+(edit?picked.length:picked.length+1)+'枚）。'
            +'選び直し・解除＝同じ画像を右クリック→同じボタン。💾保存で確定';
        }
      });
    }).catch(function(){ msg.textContent='通信エラー（アップロード一覧が取れませんでした）'; });
  }
  // 🧹 全体を規則化：セクション余白を一律・主要見出しのサイズと行間を一律にそろえる（AIなし・一貫性UP）。
  // AI一括改善が「シンプルすぎ」になりがちな一貫性(余白/見出し)を、機械的に確実にそろえる用。
  // ➖ 全セクションの先頭に区切り線＋見出しラベルを入れる（AIなし）。
  // ラベルの文字は①のセクション選択と同じ「H1/H2/H3を拾う」ロジックを流用＝AI不要で自動抽出できる。
  var divBtn=document.getElementById('__ce_divline');
  if(divBtn) divBtn.addEventListener('click',function(){
    var hIn=document.getElementById('__ce_divline_h'), wIn=document.getElementById('__ce_divline_w');
    var hpx=Math.max(1,Math.min(20,Number(hIn&&hIn.value)||2));
    var wpx=Math.max(10,Math.min(400,Number(wIn&&wIn.value)||64));
    if(!confirm('全セクションの先頭に「太さ'+hpx+'px・長さ'+wpx+'pxの線＋見出しラベル」を入れます（AIなし）。\\n既にある区切り線は太さ/長さだけ更新されます。\\n不要な所は右クリック→🗑で個別に消せます。実行しますか？')) return;
    var secs=[].slice.call(document.querySelectorAll('section')).filter(function(x){return !x.closest('#__ce');});
    var n=0;
    secs.forEach(function(s,i){
      var exist=s.querySelector(':scope > .__ce_divline');
      if(exist){
        var bar=exist.querySelector('span'); if(bar){ bar.style.width=wpx+'px'; bar.style.height=hpx+'px'; }
        n++; return;
      }
      var h=s.querySelector('h1,h2,h3');
      var label=h ? (h.textContent||'').replace(/\\s+/g,' ').trim().slice(0,24) : '';  // 番号は付けない・見出しが無ければ線だけ
      var wrap=document.createElement('div');
      wrap.className='__ce_divline';
      wrap.setAttribute('data-cediv','1');
      wrap.style.cssText='display:flex;align-items:center;gap:14px;margin:0 0 20px';
      wrap.innerHTML='<span style="display:inline-block;width:'+wpx+'px;height:'+hpx+'px;background:#c9c9c9;flex:none"></span>'
        +(label?'<span style="font-size:13px;color:#888;letter-spacing:.05em">'+esc(label)+'</span>':'');
      s.insertBefore(wrap, s.firstChild);
      n++;
    });
    markDirty();
    msg.textContent='➖ '+n+'件のセクションの区切り線を更新しました。不要な所は右クリック→🗑で消してください。上の「💾 保存」で確定';
  });
  var normBtn=document.getElementById('__ce_normalize');
  if(normBtn) normBtn.addEventListener('click',function(){
    if(!confirm('全体を一律にそろえます（AIなし・即反映）：\\n・誤ドラッグで中身の箱に残ったズレ(translate)を掃除\\n・セクションの上下余白 100px\\n・セクションの左右余白 → 多数派の値にそろえる（左右別々に判定・明らかに違うものはそのまま＝意図的な余白として残す）\\n・中身の箱（.container等）の幅も多数派にそろえる（右端のズレを解消）\\n・主要見出し(H1/H2/H3)のサイズ・太さ・行間\\n・本文の行間を1.8に（読みやすく＝呼吸感）\\n・影(box-shadow)を1種類に統一\\n\\n気に入らなければ「⟲ 戻す」で戻せます。実行しますか？')) return;
    function _skip(el){ return el.closest && (el.closest('[id^="__ce"]')); }
    // カード等の小見出し・小さい文字は対象外（そこまで大きく/広くすると崩れるため）
    function _inCard(el){ var n=el; while(n && n!==document.body){ var c=(n.className&&n.className.toString())||''; if(/card|item|bubble|benefit|badge|chip|tag|nav|menu|footer|col/i.test(c)) return true; n=n.parentElement; } return false; }
    var secN=0, hN=0, pN=0;
    var secEls=[], lVals=[], rVals=[];
    // 0) ズレ掃除：セクション本体と「中身の大きな箱」に焼き込まれた誤ドラッグの移動(translate)を先に除去。
    //   これが残っていると (1)見た目がズレたまま (2)「中央寄せでない」と誤判定→幅そろえの対象外、の二重で直らない。
    //   小物（ボタン・画像など）の意図した移動は触らない＝大きな箱だけ。
    var mvN=0;
    function _clearShift(el){
      if(!el||!el.style) return;
      // 触るのは「ドラッグ機能の署名があるズレ」だけ。left/topやCSS由来の位置はデザインの一部なので絶対に触らない
      // （left/topまで消すと、絶対配置の箱が飛んで逆に大ズレする事故が起きた）。
      var pos=''; try{ pos=getComputedStyle(el).position; }catch(_){}
      if(pos==='absolute'||pos==='fixed') return;
      var had=false;
      if(el.getAttribute('data-cetx')!=null||el.getAttribute('data-cety')!=null){
        had=true;
        el.style.removeProperty('translate');
        el.removeAttribute('data-cetx'); el.removeAttribute('data-cety');
      }
      if(had) mvN++;
    }
    // <section>が無いページ（忠実クローン等）は「main/body直下の大きな塊」をセクション扱いで同じ処理をかける
    var secsAll=[].slice.call(document.querySelectorAll('section'));
    if(!secsAll.length){
      var _host=document.querySelector('main')||document.body;
      secsAll=[].slice.call(_host.children).filter(function(c){
        if(c.nodeType!==1||_skip(c)) return false;
        if(/^(SCRIPT|STYLE|HEADER|FOOTER)$/.test(c.tagName)) return false;
        var r; try{ r=c.getBoundingClientRect(); }catch(_){ return false; }
        return r.height>200;
      });
    }
    [].forEach.call(secsAll,function(s){
      if(_skip(s)) return;
      _clearShift(s);
      var _sr2=null; try{ _sr2=s.getBoundingClientRect(); }catch(_){}
      [].forEach.call(s.children,function(c){
        if(c.nodeType!==1) return;
        var r; try{ r=c.getBoundingClientRect(); }catch(_){ return; }
        if(_sr2 && (r.width>=_sr2.width*0.5 || r.height>150)) _clearShift(c);
      });
      s.style.setProperty('padding-top','100px','important');
      s.style.setProperty('padding-bottom','100px','important');
      s.style.setProperty('margin-top','0','important');
      s.style.setProperty('margin-bottom','0','important');
      secEls.push(s);
      try{ var _cs=getComputedStyle(s); lVals.push(parseFloat(_cs.paddingLeft)||0); rVals.push(parseFloat(_cs.paddingRight)||0); }
      catch(_){ lVals.push(0); rVals.push(0); }
      secN++;
    });
    // 左右の余白：左と右を別々に測って別々に判定する（旧実装は左しか測っておらず「右だけ違う」を見逃していた）。
    // フルブリード(0px)のヒーロー等が混ざるとバラバラに見えるので「多数派の値」にそろえる。
    // ただし現在値が多数派とかけ離れている側は、意図した余白とみなして触らずに残す（別扱い）。
    var lrN=0, lrSkip=0;
    if(secEls.length){
      var sorted=lVals.concat(rVals).sort(function(a,b){return a-b;});
      var target=Math.max(24, Math.round(sorted[Math.floor(sorted.length/2)]));   // 中央値（最低24px）
      var tol=Math.max(16, target*0.35);   // これ以上離れていたら「明らかに余白が違う」として除外
      secEls.forEach(function(s,i){
        var okL=Math.abs(lVals[i]-target)<=tol, okR=Math.abs(rVals[i]-target)<=tol;
        if(okL) s.style.setProperty('padding-left',target+'px','important');
        if(okR) s.style.setProperty('padding-right',target+'px','important');
        if(okL&&okR) lrN++; else lrSkip++;
      });
    }
    // 中身の箱の幅もそろえる＝「右端が揃わない」の主犯対策。セクションのpaddingを揃えても、
    // 中の.container等（max-width持ちの中央寄せ箱）の幅がセクションごとに違うと右端の通り位置がズレる。
    // 実測幅の多数派（中央値）にmax-widthを統一。中央寄せでない箱・かけ離れた幅は意図とみなして触らない。
    var wraps=[], wVals=[], wN=0, wSkip=0;
    secEls.forEach(function(s){
      var sr, csS; try{ sr=s.getBoundingClientRect(); csS=getComputedStyle(s); }catch(_){ return; }
      var innerW=sr.width-(parseFloat(csS.paddingLeft)||0)-(parseFloat(csS.paddingRight)||0);
      var best=null, bw=0;
      [].forEach.call(s.children,function(c){
        if(c.nodeType!==1||_skip(c)) return;
        if(c.className && String(c.className).indexOf('__ce')>-1) return;
        var r; try{ r=c.getBoundingClientRect(); }catch(_){ return; }
        if(r.width<320||r.height<10) return;                              // 小物は箱ではない
        if(r.width>=innerW-8) return;                                     // 幅いっぱい＝paddingで揃済み・対象外
        if(Math.abs((r.left-sr.left)-(sr.right-r.right))>24) return;      // 中央寄せでない＝意図した片寄せは触らない
        if(r.width>bw){ bw=r.width; best=c; }
      });
      if(best){ wraps.push(best); wVals.push(bw); }
    });
    if(wVals.length>=2){
      var ws=wVals.slice().sort(function(a,b){return a-b;});
      var wTarget=Math.round(ws[Math.floor(ws.length/2)]);
      var wTol=Math.max(24, wTarget*0.15);   // 幅が15%以上違う箱は意図した狭さ/広さとして残す
      wraps.forEach(function(w,i){
        if(Math.abs(wVals[i]-wTarget)>wTol){ wSkip++; return; }
        w.style.setProperty('max-width',wTarget+'px','important');
        w.style.setProperty('margin-left','auto','important');
        w.style.setProperty('margin-right','auto','important');
        // 今より広げる必要がある箱はwidth:100%で伸ばす（border-boxの箱だけ＝padding分のはみ出し防止）
        var csW=null; try{ csW=getComputedStyle(w); }catch(_){}
        if(wVals[i]<wTarget-2 && csW && csW.boxSizing==='border-box'){ w.style.setProperty('width','100%','important'); }
        wN++;
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
      if(el.closest && (el.closest('[id^="__ce"]'))) return;
      var bs=''; try{ bs=getComputedStyle(el).boxShadow; }catch(_){ return; }
      if(bs && bs!=='none'){ el.style.setProperty('box-shadow',STD_SHADOW,'important'); shN++; }
    });
    markDirty();
    msg.textContent='規則化：ズレ掃除 '+mvN+'件／上下余白 '+secN+'／左右余白 '+lrN+'件そろえ・'+lrSkip+'件は別扱い／中身の箱幅 '+wN+'件そろえ'+(wSkip?('・'+wSkip+'件は別扱い'):'')+'／見出し '+hN+'／本文行間 '+pN+'／影 '+shN+' 箇所をそろえました（💾保存で確定・⟲で戻せる）';
  });
  // 📍 動かした跡の一覧：ドラッグ移動（data-cetx/cety）を一覧パネルで見せて、選んで元に戻す。
  // 全部自動で戻すと「わざと動かした位置」まで消えて困る（ユーザー要望）ため、
  // 1行=1要素で移動量を見せ、行クリックで場所を確認しながら1個ずつ or 全部を選んで戻す方式。
  // うっかりドラッグの跡が積み重なると、要素が重なったりoverflow:hiddenのセクション境界で
  // 切られたりして「アニメで動いている間に隠れる」ズレになる（camp_20260710の実事故）。
  var unshiftBtn=document.getElementById('__ce_unshift');
  if(unshiftBtn) unshiftBtn.addEventListener('click',function(){
    var oldP=document.getElementById('__ce_shp'); if(oldP) oldP.remove();
    var moved=[].slice.call(document.querySelectorAll('[data-cetx],[data-cety]')).filter(function(el){
      if(el.closest && (el.closest('[id^="__ce"]'))) return false;
      return ((+el.getAttribute('data-cetx'))||0)!==0 || ((+el.getAttribute('data-cety'))||0)!==0;
    });
    if(!moved.length){ msg.textContent='ドラッグで動かした跡は見つかりませんでした'; return; }
    // 開いた時点の値を控える＝✖閉じるで全部元どおりにできる
    var snap=moved.map(function(el){ return {el:el, tx:+el.getAttribute('data-cetx')||0, ty:+el.getAttribute('data-cety')||0}; });
    function shpLabel(el){
      var t=el.tagName.toLowerCase();
      var tx=(el.textContent||'').replace(/\\s+/g,' ').trim().slice(0,12);
      if(!tx) tx=(t==='img')?'画像':(String(el.className).split(' ')[0]||'');
      return '<'+t+'>'+(tx?('「'+tx+'…」'):'');
    }
    function amount(o){ var a=[]; if(o.tx) a.push((o.tx>0?'→':'←')+Math.abs(o.tx)+'px'); if(o.ty) a.push((o.ty>0?'↓':'↑')+Math.abs(o.ty)+'px'); return a.join(' '); }
    function secName(el){
      var s=el.closest('section,header,footer'); if(!s) return 'ページ直下';
      if(s.tagName==='HEADER') return '🧢 ヘッダー';
      if(s.tagName==='FOOTER') return '🦶 フッター';
      var h=s.querySelector('h1,h2,h3');
      return h?(h.textContent||'').replace(/\\s+/g,' ').trim().slice(0,14):'セクション';
    }
    function resetOne(o){
      o.el.setAttribute('data-cetx',0); o.el.setAttribute('data-cety',0); applyTf(o.el);
      // 移動以外の編集も無い要素は、跡（0値のinlineと内部印）ごときれいに消す
      if(!((+o.el.getAttribute('data-cero'))||0) && ((+o.el.getAttribute('data-cesx'))||1)===1 && ((+o.el.getAttribute('data-cesy'))||1)===1 && !o.el.getAttribute('data-cebt')){
        o.el.style.removeProperty('translate'); o.el.style.removeProperty('rotate'); o.el.style.removeProperty('scale'); o.el.style.removeProperty('transform-origin');
        ['data-cetx','data-cety','data-cesx','data-cesy','data-cero','data-cebt'].forEach(function(a){ o.el.removeAttribute(a); });
      }
    }
    function restoreOne(o){ o.el.setAttribute('data-cetx',o.tx); o.el.setAttribute('data-cety',o.ty); applyTf(o.el); }
    var p=document.createElement('div'); p.id='__ce_shp';
    p.setAttribute('style','position:fixed;right:14px;top:64px;z-index:2147483647;background:#1d1d2b;color:#fff;border-radius:12px;padding:10px 14px;box-shadow:0 6px 24px rgba(0,0,0,.4);font:12.5px/1.6 sans-serif;width:430px;max-width:96vw;max-height:72vh;overflow:auto');
    var rows='', lastSec=null;
    snap.forEach(function(o,i){
      var sn=secName(o.el);
      if(sn!==lastSec){ rows+='<div style="margin-top:6px;padding:2px 0;color:#9ad;font-weight:700;border-bottom:1px solid #46466a">📄 '+esc(sn)+'</div>'; lastSec=sn; }
      rows+='<div class="__shprow" data-i="'+i+'" style="display:flex;align-items:center;gap:6px;padding:4px 0;border-bottom:1px solid #34344a">'
        +'<span title="クリックでその要素の場所を表示" style="flex:1;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(shpLabel(o.el))+'</span>'
        +'<span style="color:#fbbf24;flex:none">'+amount(o)+'</span>'
        +'<button data-rs="1" style="background:#dc2626;color:#fff;border:none;border-radius:5px;padding:2px 8px;cursor:pointer;flex:none">⟲ 戻す</button>'
        +'</div>';
    });
    p.innerHTML='<b>📍 動かした跡の一覧（'+snap.length+'件）</b>'
      +'<div style="opacity:.75;font-size:11px">行クリック＝その場所を表示して確認／「⟲戻す」＝その要素だけ動かす前の位置へ（もう一度押すと復元）。<br>わざと動かした所はそのまま残してOK</div>'
      +'<div id="__ce_shprows" style="margin-top:4px">'+rows+'</div>'
      +'<div style="margin-top:10px;display:flex;gap:6px">'
      +'<button id="__ce_shpall" style="background:#7f1d1d;color:#fff;border:none;border-radius:7px;padding:5px 10px;cursor:pointer">⟲ 全部戻す</button>'
      +'<span style="flex:1"></span>'
      +'<button id="__ce_shpok" style="background:#16a34a;color:#fff;border:none;border-radius:7px;padding:5px 12px;cursor:pointer">✔ 決定</button>'
      +'<button id="__ce_shpx" style="background:#555;color:#fff;border:none;border-radius:7px;padding:5px 12px;cursor:pointer">✖ 閉じる（開いた時に戻す）</button>'
      +'</div>';
    document.body.appendChild(p);
    var done={};
    p.addEventListener('click',function(ev){
      var t=ev.target;
      if(t.id==='__ce_shpok'){ p.remove(); markDirty(); msg.textContent='📍 決定しました（💾保存で確定・⟲で戻せる）'; return; }
      if(t.id==='__ce_shpx'){ snap.forEach(function(o,i){ if(done[i]) restoreOne(o); }); p.remove(); msg.textContent='📍 開いた時点の位置に戻して閉じました'; return; }
      if(t.id==='__ce_shpall'){
        if(!confirm('一覧の '+snap.length+' 件を全部「動かす前の位置」に戻します。よろしいですか？\\n（やり過ぎたら「✖ 閉じる」で開いた時点に戻せます）')) return;
        snap.forEach(function(o,i){ if(!done[i]){ resetOne(o); done[i]=1; } });
        [].forEach.call(p.querySelectorAll('.__shprow'),function(r){ r.style.opacity='.45'; var b=r.querySelector('button'); if(b){ b.textContent='済(押すと復元)'; b.style.background='#374151'; } });
        return;
      }
      var row=t.closest?t.closest('.__shprow'):null; if(!row) return;
      var i=+row.getAttribute('data-i'), o=snap[i]; if(!o) return;
      if(t.getAttribute&&t.getAttribute('data-rs')){
        if(done[i]){ restoreOne(o); done[i]=0; row.style.opacity='1'; t.textContent='⟲ 戻す'; t.style.background='#dc2626'; }
        else { resetOne(o); done[i]=1; row.style.opacity='.45'; t.textContent='済(押すと復元)'; t.style.background='#374151'; }
        return;
      }
      // 行クリック：その要素の場所へスクロールして一瞬光らせる（どれのことか確認できる）
      try{ o.el.scrollIntoView({block:'center'}); }catch(_){}
      var oldOl=o.el.style.outline; o.el.style.outline='3px solid #f59e0b';
      setTimeout(function(){ o.el.style.outline=oldOl||''; },1200);
    });
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
      if(el.closest && (el.closest('[id^="__ce"]'))) return;
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
    [].forEach.call(document.querySelectorAll('[style]'), function(el){ if(el.id && el.id.indexOf('__ce')===0) return; if(el.closest && (el.closest('[id^="__ce"]'))) return; _iCache.push({el:el, orig:el.getAttribute('style')}); });
    // SVGの色は fill= / stop-color= などの「属性」で塗られている＝styleとは別。これも拾って塗り替える。
    var _aCache=[]; ['fill','stroke','stop-color','flood-color','lighting-color','color'].forEach(function(attr){
      [].forEach.call(document.querySelectorAll('['+attr+']'), function(el){ if(el.closest && (el.closest('[id^="__ce"]'))) return; var v=el.getAttribute(attr); if(v && /#[0-9a-fA-F]{3,8}|rgba?\\(/.test(v)) _aCache.push({el:el, attr:attr, orig:v}); });
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
    if(e.target.closest('#__ce_save')||e.target.closest('#__ce_undo')||e.target.closest('#__ce_homeh')||e.target.closest('#__ce_apply')) return;  // 保存・戻す・ホーム・反映ボタンは除外
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
  // ⬆ 元に反映：🌙磨き版（ce-brush-srcメタ持ち）でだけ出るボタン。未保存分を保存→元カンプを上書き
  var applyBtn=document.getElementById('__ce_apply');
  if(applyBtn){
    var _bsrc=(document.querySelector('meta[name="ce-brush-src"]')||{}).content||'';
    if(_bsrc) applyBtn.style.display='';
    applyBtn.addEventListener('click',function(ev){
      ev.stopPropagation();
      if(!confirm('この磨き版の内容で、元のカンプを上書きします。\\n（元カンプ: '+_bsrc+'）\\n上書き前の元は .bak に控えるので戻せます。よろしいですか？')) return;
      flushThen(function(){
        fetch('/api/brush_apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE})})
        .then(function(r){return r.json();}).then(function(d){
          if(msg) msg.textContent=d.ok?('⬆ 元カンプ('+d.source+')に反映しました！次からは元カンプを開けばこの内容です'):('反映できません：'+(d.message||''));
        }).catch(function(){ if(msg) msg.textContent='通信エラー'; });
      });
    });
  }
  // ◎○△✖評価＝手本ごとのハズレ率集計の教師データ。
  // 評価したらボタンごと消す（画面に残ると邪魔なため）。変更したいときは同じカンプを
  // もう一度評価し直せないので、解除・変更はAI（チャット）に頼む運用
  var rateBox=document.getElementById('__ce_rate');
  function paintRate(v){
    [].forEach.call(rateBox.querySelectorAll('.rt'),function(b){ b.classList.toggle('on', b.dataset.r===v); });
    rateBox.classList.toggle('done', !!v);
    rateBox.style.display = v ? 'none' : '';  // 評価済みならボタンごと消す
  }
  if(rateBox){
    fetch('/api/camp_rate?file='+encodeURIComponent(FILE)).then(function(r){return r.json();})
      .then(function(d){ paintRate(d.rating||''); }).catch(function(){});
    // 評価を変更・解除したいとき用：ヘッダ左の✏をダブルクリックでボタンを再表示
    var hdIco=hd.querySelector('span');
    if(hdIco) hdIco.addEventListener('dblclick',function(ev){
      ev.stopPropagation(); rateBox.style.display=''; rateBox.classList.remove('done');
    });
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
  // 特殊値 hd=ヘッダー / ft=フッター（⭐部品保存・🔀入れ替え用）
  function curSecEl(){
    var v=sec.value;
    if(v==='hd'||v==='ft'){
      var tag=(v==='hd')?'header':'footer';
      return [].slice.call(document.querySelectorAll(tag)).filter(function(x){return !x.closest('#__ce');})[0]||null;
    }
    var idx=Number(v);
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
  function cleanSection(el, selfContain){
    var c=el.cloneNode(true);
    // 絶対配置(position:absolute)の子要素は、元ページ固有の入れ子（この部品の外側にある
    // position:relativeな祖先）を位置の基準にしていることがある。別カンプに持っていくと
    // その基準が失われ、代わりに部品自身（セクション丸ごと）が基準になってしまい、
    // 本来は小さな飾りバッジだった要素が画面の隅に巨大にズレて出る事故が実際に起きた。
    // → 保存の瞬間に「セクション自身を基準にした見た目の位置(px)」を実測して焼き込み、
    //   位置の基準がどこになっても保存時の見た目を再現できるようにする。
    (function(){
      var baseRect=el.getBoundingClientRect();
      var srcEls=el.querySelectorAll('*'), dstEls=c.querySelectorAll('*');
      for(var i=0;i<srcEls.length;i++){
        var s=srcEls[i], d=dstEls[i]; if(!d) continue;
        var cs; try{ cs=getComputedStyle(s); }catch(_){ continue; }
        if(cs.position!=='absolute') continue;
        // ★位置の基準（一番近いpositionedな祖先）が部品の「中」にある要素は焼き込まない。
        //   元のCSS座標のままで正しく、部品全体基準の座標で上書きすると基準違いでズレて
        //   overflow:hiddenの外へ飛び「写真が消えた」ように見える（実例：figure基準のカード写真）。
        var anc=s.parentElement, refInside=false;
        while(anc){
          var ap='static';
          try{ ap=getComputedStyle(anc).position; }catch(_){ }
          if(ap!=='static'){ refInside=el.contains(anc); break; }
          anc=anc.parentElement;
        }
        if(refInside) continue;
        var r=s.getBoundingClientRect();
        if(!r.width && !r.height) continue;
        d.style.setProperty('top',(r.top-baseRect.top)+'px','important');
        d.style.setProperty('left',(r.left-baseRect.left)+'px','important');
        // ★right/bottom を auto にすると、left+right の組で幅を作っていた要素が潰れて消える
        //   （実例：カードの写真が空っぽの枠になった）。実測した幅・高さも一緒に焼き込む。
        if(r.width) d.style.setProperty('width',Math.round(r.width)+'px','important');
        if(r.height) d.style.setProperty('height',Math.round(r.height)+'px','important');
        d.style.setProperty('right','auto','important');
        d.style.setProperty('bottom','auto','important');
      }
    })();
    // 前回⭐保存で抱かせた持ち運びCSS(style[data-cepart])は一旦捨てて、今の状態から取り直す
    // ★下のstripループがdata-ce*属性を外す前にやること（後だとセレクタで見つけられず二重に溜まる）
    var oldPart=c.querySelector('style[data-cepart]'); if(oldPart) oldPart.remove();
    var stripCls=['__ce_sel','__ce_hl','__ce_sechl','fxa_in'];
    [].slice.call(c.querySelectorAll('*')).concat([c]).forEach(function(n){
      if(n.classList){
        stripCls.forEach(function(k){ n.classList.remove(k); });
        [].slice.call(n.classList).forEach(function(cl){ if(cl.indexOf('__ceax_')===0) n.classList.remove(cl); });
      }
      var edited=false;
      // data-cedelay（⏳遅らせ・順番の演出タイミング）は「動きの一部」なので部品に残す。
      // それ以外のdata-ce*（ドラッグ署名data-cetx等）は編集の内部印なので外す。
      // data-cepin/data-cepinbg（📌貼り付け固定）も「動き＝残す」側＝保存後も解除できるように保持する
      var _keepCe={'data-cedelay':1,'data-cepin':1,'data-cepinbg':1};
      if(n.attributes){ [].slice.call(n.attributes).forEach(function(a){ if(a.name.indexOf('data-ce')===0 && !_keepCe[a.name]){ edited=true; n.removeAttribute(a.name); } }); }
      if(n.style){
        ['animation','transition'].forEach(function(p){ n.style.removeProperty(p); });      // 一時アニメは常に除去
        if(edited){ ['transform','transform-origin','width','height','max-width','object-fit','opacity','filter'].forEach(function(p){ n.style.removeProperty(p); }); }  // 編集で動かした要素だけサイズ・位置も戻す
        var sv=n.getAttribute('style'); if(!sv||!sv.trim()) n.removeAttribute('style');
      }
    });
    var vars=collectRootVars();
    Object.keys(vars).forEach(function(k){ c.style.setProperty(k, vars[k]); });  // 色・フォントを自己完結させる
    if(selfContain){
      // ヘッダー/フッター用：効いているCSSルールを<style data-cepart>として本体に抱かせる＝
      // 別カンプに🔀で入れても見た目がそのまま付いてくる（@scopeでこの部品の中だけに効く）
      var st=document.createElement('style'); st.setAttribute('data-cepart','1');
      st.textContent=partCss(el);
      c.appendChild(st);
    }
    return c;
  }
  // ★部品（ヘッダー/フッター）用：この要素に「実際に効いているCSSルールだけ」を全スタイルから抽出する。
  //   計算済みスタイルの丸埋めは試したがNG（min-height:auto→0px化・grid列のpx固定などで
  //   レイアウトの生きた計算が死んで崩れた）。ルールごと持ち運び、入れ替え先では@scopeで
  //   その部品の中だけに効かせる＝レスポンシブも:hoverもそのまま生きる。
  function partCss(el){
    var els=[el].concat([].slice.call(el.querySelectorAll('*')));
    // ★rem対策（2026-07-18）：元サイトが「html{font-size:1px}」等のrem=px手法だと、
    //   持ち込み先カンプ（1rem=16px）でrem値が16倍に膨張する（実例：たんぽぽ園フッターの
    //   padding:325rem→5200pxでページが縦にドーンと伸びた）。
    //   保存時に「このページの実際のroot font-size」でrem→pxに焼き直して自己完結させる。
    //   普通のサイト（root=16px）は無変換＝今まで通り。
    var rootPx=parseFloat(getComputedStyle(document.documentElement).fontSize)||16;
    // ★開いているページで元サイトの html{font-size:...} が生きていないと rootPx が16のままになり、
    //   「変換不要」と誤判定して rem を素のまま保存してしまう（実例：1rem≒0.89pxのサイトの
    //   300rem が貼り先で4800pxに膨張）。その時はCSSに書いてある指定を実際に計算して本当の値を得る。
    if(Math.abs(rootPx-16)<0.01){
      var _pv='';
      [].slice.call(document.styleSheets).forEach(function(ss){
        var rr; try{ rr=ss.cssRules; }catch(_){ return; }
        [].slice.call(rr||[]).forEach(function(r){
          if(!r.selectorText||!r.style) return;
          if(!/(^|,)\\s*(html|:root)\\s*(,|$)/.test(r.selectorText)) return;
          var v=r.style.getPropertyValue('font-size'); if(v) _pv=v;
        });
      });
      if(_pv){
        try{
          var _p=document.createElement('div');
          _p.style.cssText='position:absolute;left:-9999px;top:0;visibility:hidden;font-size:'+_pv;
          document.documentElement.appendChild(_p);
          var _px=parseFloat(getComputedStyle(_p).fontSize)||0;
          _p.remove();
          if(_px>0 && Math.abs(_px-16)>=0.01) rootPx=_px;
        }catch(_){}
      }
    }
    window.__cePartRem=0;                       // 変換できずに残った大きなrem（保存後に警告を出す用）
    function remFix(t){
      if(Math.abs(rootPx-16)<0.01){
        var big=(t.match(/(?:^|[^0-9.])([0-9]{2,})(?:\\.[0-9]+)?rem\\b/g)||[]).length;
        window.__cePartRem=big;                 // 1rem=16px のはずなのに2桁remが多い＝縮小前提のサイト
        return t;
      }
      return t.replace(/(-?\\d*\\.?\\d+)rem\\b/g,function(_,n){ return (Math.round(parseFloat(n)*rootPx*1000)/1000)+'px'; });
    }
    function hitAny(selText){
      return selText.split(',').some(function(s){
        // :hover/::before等は保存の瞬間は誰にも当たっていないので、疑似部分を外した本体で判定する
        s=s.replace(/::?[a-zA-Z-]+(\\((?:[^()]|\\([^()]*\\))*\\))?/g,'').replace(/[>+~\\s]+$/,'').trim();
        if(!s) return true;  // ::selection など疑似だけのセレクタは念のため持っていく
        for(var i=0;i<els.length;i++){ try{ if(els[i].matches(s)) return true; }catch(_){} }
        return false;
      });
    }
    // ★@scope内のセレクタは暗黙で「:scopeの子孫」扱い（img{}＝:scope img{}）。だから
    //   ①ルート自身に当たるルール（.hero{…}）②ルートを先祖に使うルール（.hero .nav{…}）は
    //   素のままでは両方とも死ぬ（①ルートは子孫でない ②ルートは先祖として数えられない・実測で確認）。
    // → :scope に書き換えたコピーも一緒に持っていく（.hero→:scope・.hero .nav→:scope .nav）。
    //   入れ替え先に同名クラス（.hero等）があっても、部品のstyleは文書の後ろ＝同点なら勝てる。
    function extraSels(selText){
      var res=[];
      selText.split(',').forEach(function(s){
        s=s.trim(); if(!s) return;
        // ①セレクタ全体がルート自身に当たる → :scope（末尾の疑似は残す。.hero:hover→:scope:hover）
        var m=s.match(/^(.*?)((?:::?[a-zA-Z-]+(?:\\([^()]*\\))?)*)$/);
        var base=(m?m[1]:s), pseudo=(m?m[2]:'');
        var probe=base.replace(/::?[a-zA-Z-]+(\\((?:[^()]|\\([^()]*\\))*\\))?/g,'').replace(/[>+~\\s]+$/,'').trim();
        try{ if(probe && el.matches(probe)){ res.push(':scope'+pseudo); return; } }catch(_){}
        // ②先頭の固まりがルート自身に当たる → その部分だけ:scopeに（.hero .nav→:scope .nav）
        var lead=s.match(/^([^\\s>+~]+)([\\s>+~].*)$/);
        if(lead){
          var lp=lead[1].replace(/::?[a-zA-Z-]+(\\((?:[^()]|\\([^()]*\\))*\\))?/g,'').trim();
          try{ if(lp && el.matches(lp)){ res.push(':scope'+lead[2]); return; } }catch(_){}
        }
        // ③セレクタの前半が「ルートより上の先祖」に当たる（例：.slick-initialized .slick-slide）
        //   → 部品として切り出すと先祖ごと消えて二度と当たらない。実例：本物サイト由来のslickスライダーで
        //   「.slick-initialized .slick-slide{display:block}」が死に、全スライドがdisplay:noneのまま＝真っ白部品。
        //   前半を:scopeに置き換えたコピーを足す（後半が部品の中の要素に実際に当たる時だけ・子/隣接の切れ目は対象外）。
        var toks=s.split(/\\s+/);
        for(var k=1;k<toks.length;k++){
          if(/^[>+~]$/.test(toks[k-1])||/^[>+~]$/.test(toks[k])) continue;
          var aP=toks.slice(0,k).join(' ').replace(/::?[a-zA-Z-]+(\\((?:[^()]|\\([^()]*\\))*\\))?/g,'').trim();
          var aR=toks.slice(k).join(' ');
          var aRp=aR.replace(/::?[a-zA-Z-]+(\\((?:[^()]|\\([^()]*\\))*\\))?/g,'').replace(/[>+~\\s]+$/,'').trim();
          if(!aP||!aRp) continue;
          var anc=el.parentElement, hitAnc=false;
          while(anc && anc.nodeType===1){ try{ if(anc.matches(aP)){ hitAnc=true; break; } }catch(_){ break; } anc=anc.parentElement; }
          if(!hitAnc) continue;
          var hitIn=false;
          for(var j=0;j<els.length;j++){ try{ if(els[j].matches(aRp)){ hitIn=true; break; } }catch(_){} }
          if(hitIn){ res.push(':scope '+aR); break; }
        }
      });
      return res;
    }
    var out=[], kf=[], hideSel=[];
    function scan(rules, mediaTxt){
      [].slice.call(rules||[]).forEach(function(r){
        if(r.media && r.cssRules){ scan(r.cssRules, r.media.mediaText); return; }        // @media
        if(r.name && r.cssRules){ kf.push(r.cssText); return; }                           // @keyframes は丸ごと（@scopeの外に置く）
        if(!r.selectorText){ if(r.cssRules) scan(r.cssRules, mediaTxt); return; }         // @scope/@supports等の入れ物は中身だけ拾う（🔀済み部品の再⭐保存でも取りこぼさない）
        if(!r.style) return;
        if(r.parentStyleSheet && r.parentStyleSheet.ownerNode && r.parentStyleSheet.ownerNode.id==='fxa-css') return;  // fxaは両ページにあるので除外
        if(hitAny(r.selectorText)){
          // ★元サイトがJSで表示する作り（CSSでは opacity:0 で隠しておく）だと、部品にした時に
          //   そのJSが無いので永久に見えない（実例：インタビューのカードが真っ白のまま）。
          //   隠す指定を控えておき、あとで「元サイトで見えていたもの」だけ表示に戻す。
          try{
            if(r.style && (parseFloat(r.style.opacity)===0 || r.style.visibility==='hidden')) hideSel.push(r.selectorText);
          }catch(_){}
          out.push(mediaTxt?('@media '+mediaTxt+'{'+r.cssText+'}'):r.cssText);
          var rs=extraSels(r.selectorText);
          if(rs.length){ var rule=rs.join(',')+'{'+r.style.cssText+'}'; out.push(mediaTxt?('@media '+mediaTxt+'{'+rule+'}'):rule); }
        }
      });
    }
    [].slice.call(document.styleSheets).forEach(function(ss){
      if(ss.ownerNode && /#__ce/.test(ss.ownerNode.textContent||'')) return;  // 編集UIのCSSは除外
      var rr; try{ rr=ss.cssRules; }catch(_){ return; }
      // ★<link media="(max-width:767px)">のようなシートまるごとの幅条件を無視すると、
      //   スマホ専用CSSが無条件で紛れ込む（実例：overflow-x:scrollでPC表示にスクロールバーが出た）。
      //   シート側のmedia条件も@mediaとして引き継ぐ。
      var _mt=''; try{ _mt=(ss.media&&ss.media.mediaText)||''; }catch(_){}
      scan(rr, _mt||undefined);
    });
    // 護身用：入れ替え先に「画面基準で浮く絶対配置（親にrelative無しの.hero-media等）」があると
    // 部品の上に被さってくる（実際に起きた）。部品ルートを relative+z-index:1 にして上に出す。
    // 元がsticky/fixed等の部品はそのまま尊重する（staticのときだけ）。
    if(getComputedStyle(el).position==='static') out.push(':scope{position:relative;z-index:1}');
    // 隠す指定のうち、保存の瞬間に「実際は見えていた」ものだけ表示に戻す（JS前提の演出対策）
    hideSel.forEach(function(sel){
      var vis=false;
      for(var i=0;i<els.length;i++){
        try{ if(els[i].matches(sel) && parseFloat(getComputedStyle(els[i]).opacity)>=0.99) { vis=true; break; } }catch(_){}
      }
      if(vis) out.push(sel+'{opacity:1!important;visibility:visible!important}');
    });
    // @scope（中括弧だけ・条件なし）＝「このstyleタグの親要素の中だけに効く」。部品に抱かせる用にぴったり
    return remFix(kf.join('\\n')+'\\n@scope{\\n'+out.join('\\n')+'\\n}');
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
  // ★幕の並べ方（中央そろえ）は必ずこのCSSで持つ（2026-07-29）。
  //   インラインstyleだけで持たせていたら、保存時の後片付けで display:flex が消えて幕が block になり、
  //   ロゴ/文字が左上に寄る事故が起きた。id が __ce 始まりでないので保存版にもそのまま残る＝単体でも正しく出る。
  //   !important は付けない：隠す時のインライン display:none を勝たせる必要があるため（#idの優先度で十分勝てる）。
  function opEnsureCss(){
    if(!document.getElementById('__op_screen')) return null;
    var st=document.getElementById('__op_css');
    if(!st){ st=document.createElement('style'); st.id='__op_css'; document.head.appendChild(st); }
    st.textContent='#__op_screen{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;'
      +'margin:0;padding:0;text-align:center}'
      +'#__op_screen>*{position:static;margin:0;max-width:92vw}'
      // 幕が出ている間はページ側のCSSアニメを一時停止＝ヒーローが幕の裏で先に動き終わるのを防ぐ。
      // ★止めるのは animation だけ（中身は隠さない）。万一JSが転んで解除できなくても、
      //   ページが真っ白のまま固まらないようにするため。
      +'html.op-wait body>*:not(#__op_screen):not([id^="__ce"]),'
      +'html.op-wait body>*:not(#__op_screen):not([id^="__ce"]) *{animation-play-state:paused!important}';
    return st;
  }
  // 幕の再生スクリプト本体（保存版でもこれがそのまま動く）
  // ★body の先頭に置く＝最初の1フレーム目から幕が出る。末尾に置くとヒーローが先に描かれて
  //   「ヒーローのアニメ→幕→アニメ済みのヒーロー」という順序になる（実際に起きた・2026-07-29）。
  var OP_RUN='(function(){if(window.__opRan)return;window.__opRan=1;'
    +'var d=document,s=d.getElementById("__op_screen");if(!s)return;var h=d.documentElement;'
    +'function release(){h.classList.remove("op-wait");window.__opWait=0;'
    +'try{window.dispatchEvent(new Event("ce-op-done"));}catch(_){}}'
    +'if(s.getAttribute("data-paused")==="1"){release();return;}'
    +'if(getComputedStyle(s).display==="none"){release();return;}'
    +'h.classList.add("op-wait");window.__opWait=1;'
    +'s.style.transition="opacity .6s ease";s.style.opacity="0";'
    +'requestAnimationFrame(function(){requestAnimationFrame(function(){s.style.opacity="1";});});'
    +'setTimeout(function(){if(s.getAttribute("data-paused")==="1"){release();return;}s.style.opacity="0";'
    +'setTimeout(function(){s.style.display="none";release();},650);},1800);'
    +'setTimeout(release,8000);})();';   // 保険：何があってもページのアニメは必ず動き出す
  // <head>に置く先出しスクリプト。ページの中身が描かれる前に「待て」の合図を出す役だけ。
  // ★readyStateが loading の時（＝本当のページ読み込み中）だけ効く＝編集中に差し込んでも誤発動しない。
  var OP_EARLY='if(document.readyState==="loading"){document.documentElement.classList.add("op-wait");window.__opWait=1;}';
  // 既存カンプ（幕が末尾にある古い作り）を開いた時に、正しい並びへ直す
  function opUpgrade(){
    var sc=document.getElementById('__op_screen'); if(!sc) return;
    window.__opRan=1;                                   // 編集中に差し込んだ拍子に幕が再生されないように
    opEnsureCss();
    var b=document.body;
    /* ★中身は毎回「本物」に上書きする（2026-07-30）。編集中はサーバーが『幕を流さない版』の
       __op_early / __op_run を配るので、ここで戻さないと💾保存でそちらが焼き込まれ、
       保存版のオープニングが二度と流れなくなる（__op_early が window.__opRan=1 を立てるため）。
       本物の OP_EARLY は readyState==='loading' の時だけ効く＝いま差し込んでも誤発動しない。 */
    var e=document.getElementById('__op_early');
    if(!e){ e=document.createElement('script'); e.id='__op_early'; document.head.appendChild(e); }
    e.textContent=OP_EARLY;
    if(b.firstElementChild!==sc) b.insertBefore(sc, b.firstChild);   // 幕を先頭へ
    var run=document.getElementById('__op_run');
    if(run) run.remove();
    var r=document.createElement('script'); r.id='__op_run'; r.textContent=OP_RUN;
    sc.parentNode.insertBefore(r, sc.nextSibling);                   // 幕のすぐ後ろへ
  }
  // 幕そのものを掴んで動かしてしまった跡（ドラッグの translate 等）を落として中央に戻す
  function opCenter(){
    var sc=document.getElementById('__op_screen'); if(!sc) return;
    opEnsureCss();
    [sc, sc.firstElementChild].forEach(function(n){
      if(!n) return;
      ['translate','rotate','scale','transform','left','top','right','bottom','position'].forEach(function(p){ n.style.removeProperty(p); });
      ['data-cetx','data-cety','data-cebt'].forEach(function(a){ n.removeAttribute(a); });
    });
    sc.style.display='flex';
    markDirty();
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
    document.body.insertBefore(sc, document.body.firstChild);   // ★先頭に置く（§7 ㉜）
    opUpgrade();                                                // CSS・先出しスクリプト・再生スクリプトを正しい並びで置く
    markDirty();
    opBarShow();
    msg.textContent='オープニングを付けました。画面下のバーで直せます（終わったら「✔ 修正完了」→「💾 保存」）';
  }
  // 🎬 オープニング編集バー（幕の上に出す・2026-07-29）
  //   ★幕は全画面なので、出したままだと本体を触れない＝「戻り方が分からない」状態になっていた。
  //     直す操作と「✔ 修正完了」を幕の上にまとめて、ここだけ見れば終われるようにする。
  //   id が __ce 始まり＝💾保存時に自動で除去される（焼き込まれない）。
  function opBarHide(){ var b=document.getElementById('__ce_opbar'); if(b) b.remove(); }
  function opBarShow(){
    opBarHide();
    var sc=document.getElementById('__op_screen'); if(!sc) return;
    var BTN='background:#3a3a44;color:#fff;border:none;border-radius:8px;padding:7px 11px;font-size:12.5px;font-weight:700;cursor:pointer;font-family:system-ui,sans-serif';
    var b=document.createElement('div'); b.id='__ce_opbar';
    b.setAttribute('style','position:fixed;left:50%;bottom:26px;transform:translateX(-50%);z-index:2147483005;'
      +'background:#1d1d1f;color:#fff;border-radius:14px;padding:10px 12px;display:flex;gap:7px;flex-wrap:wrap;'
      +'align-items:center;justify-content:center;box-shadow:0 14px 40px rgba(0,0,0,.42);'
      +'font:13px/1.4 system-ui,sans-serif;max-width:94vw');
    // ★文字の書き換えに prompt() を使ってはいけない（2026-07-29・ユーザー報告）。
    //   Chromeは同じページで繰り返しダイアログが出ると「これ以上ダイアログを表示しない」を出し、
    //   以後 prompt は黙って null を返す＝「押しても何も起きない・直しても反映されない」になる。
    //   バーに入力欄を置いて、打った文字がその場で入るようにする（確定操作すら要らない）。
    var _opT=document.getElementById('__op_title')||sc.querySelector('span');
    var _opTv=_opT?String(_opT.textContent||'').trim():'';
    b.innerHTML='<span style="font-weight:700;margin-right:2px">🎬 オープニングを編集中</span>'
      +'<button data-op="logo" style="'+BTN+'">🖼 ロゴを変える</button>'
      +'<label style="'+BTN+';display:inline-flex;align-items:center;gap:5px">✏ 文字'
        +'<input data-op="text" type="text" value="'+esc(_opTv)+'" placeholder="オープニングの文字" '
        +'style="width:180px;padding:3px 6px;border:1px solid #6b6b8a;border-radius:5px;'
        +'background:#fff!important;color:#111!important;-webkit-text-fill-color:#111!important;'
        +'font:13px/1.4 system-ui,sans-serif"></label>'
      +'<label style="'+BTN+';display:inline-flex;align-items:center;gap:5px">🎨 背景'
        +'<input type="color" data-op="bg" value="#eef4ff" style="width:26px;height:22px;padding:0;border:none;border-radius:4px;cursor:pointer;background:none"></label>'
      +'<button data-op="center" style="'+BTN+'">⤺ 中央にそろえる</button>'
      +'<button data-op="play" style="'+BTN+';background:#0b6bcb">▶ 動きを試す</button>'
      +'<button data-op="del" style="'+BTN+';background:#7f1d1d">🗑 消す</button>'
      +'<button data-op="done" style="'+BTN+';background:#22c55e;color:#08240f">✔ 修正完了</button>';
    document.body.appendChild(b);
    b.addEventListener('input',function(ev){
      var k=ev.target.getAttribute('data-op'), s=document.getElementById('__op_screen');
      if(!s) return;
      if(k==='bg'){ s.style.background=ev.target.value; markDirty(); return; }
      if(k==='text'){                                  // 打った字がその場で幕に入る
        var tt=document.getElementById('__op_title')||s.querySelector('span');
        if(!tt){ if(msg) msg.textContent='⚠ オープニングの文字が見つかりません'; return; }
        tt.textContent=ev.target.value; markDirty();
      }
    });
    b.addEventListener('click',function(ev){
      var t=ev.target.closest('[data-op]'); if(!t) return;
      var k=t.getAttribute('data-op'), s=document.getElementById('__op_screen');
      if(k==='bg'||k==='text'||!s) return;
      if(k==='logo'){
        var lg=document.getElementById('__op_logo')||s.querySelector('img');
        if(!lg){ msg.textContent='ロゴ画像が見つかりません'; return; }
        openPicker({el:lg, type:'img', url:lg.currentSrc||lg.src});
        return;
      }
      if(k==='center'){ opCenter(); ceFlash('⤺ 中央にそろえました'); return; }
      if(k==='play'){ opPlay(); return; }
      if(k==='del'){
        // ★confirm() も prompt と同じでブラウザに抑止されると黙って false を返す。
        //   1回目は「本当に消す？」に変わるだけ＝2回押して初めて消える（ダイアログに頼らない）。
        if(t.getAttribute('data-arm')!=='1'){
          t.setAttribute('data-arm','1'); t.textContent='🗑 本当に消す？';
          setTimeout(function(){ if(t&&t.parentNode){ t.removeAttribute('data-arm'); t.textContent='🗑 消す'; } },4000);
          return;
        }
        s.remove();
        ['__op_run','__op_css'].forEach(function(id){ var n=document.getElementById(id); if(n) n.remove(); });
        opBarHide(); markDirty();
        msg.textContent='🗑 オープニングを消しました（💾 保存で確定）';
        return;
      }
      if(k==='done'){
        s.style.display='none'; opBarHide();
        msg.textContent='✔ オープニングの修正を終えました。「💾 変更を保存」で確定してください（保存版では開いた時に自動で流れます）';
        ceFlash('✔ 修正完了（💾保存で確定）');
      }
    });
  }
  // ▶ 本番と同じ動き（フェードイン→1.8秒→フェードアウト）をその場で1回だけ再生する
  function opPlay(){
    var s=document.getElementById('__op_screen'); if(!s) return;
    opBarHide();
    s.style.display='flex'; s.style.transition='opacity .6s ease'; s.style.opacity='0';
    requestAnimationFrame(function(){ requestAnimationFrame(function(){ s.style.opacity='1'; }); });
    setTimeout(function(){
      s.style.opacity='0';
      setTimeout(function(){
        s.style.display='none'; s.style.removeProperty('transition'); s.style.removeProperty('opacity');
        msg.textContent='▶ 再生しました（保存版でもこの動きです）。もう一度直すときは「👁 オープニングを出す／隠す」';
      },700);
    },1800);
  }
  // 幕の表示/非表示を切り替え（編集用）。出す時は data-paused=1 で止めて右クリック編集できるように。
  function toggleOpening(){
    var s=document.getElementById('__op_screen');
    if(!s){ msg.textContent='先に「🎬 フェードのオープニングを付ける」を押してください'; return; }
    var hidden=(s.style.display==='none'||getComputedStyle(s).display==='none'||parseFloat(getComputedStyle(s).opacity)===0);
    if(hidden){
      s.setAttribute('data-paused','1'); opEnsureCss(); s.style.display='flex'; s.style.opacity='1';
      opBarShow();
      msg.textContent='オープニングを表示中。画面下のバーで直せます（終わったら「✔ 修正完了」）';
    }
    else { s.style.display='none'; opBarHide(); msg.textContent='オープニングを隠しました（保存版では開いた時に自動で流れます）'; }
  }
  // 🗑 オープニングを完全に外す（2026-07-30・ユーザー要望）
  // ★👁「出す／隠す」は編集中だけ隠すもので、保存時に必ず表示へ戻る作りだった
  //   （cleanHtml が data-paused を外して display:flex に戻す＝「消したのに保存すると復活」）。
  //   ＝幕をやめる手段が無かったので、ここで本当に取り外す。
  // ★幕本体だけ消しても足りない：<head>の __op_early が残ると読み込み時に op-wait が付き、
  //   出現アニメが "ce-op-done" を待って**7秒間なにも動かない**（保険のタイマーまで待つ）。
  //   待機スクリプトとCSSも一緒に外し、待ちを今すぐ解除する。
  function removeOpening(){
    var n=0;
    ['__op_screen','__op_run','__op_early','__op_css'].forEach(function(id){
      var e=document.getElementById(id); if(e){ e.remove(); n++; }
    });
    try{ document.documentElement.classList.remove('op-wait'); }catch(_){}
    try{ window.__opWait=0; window.dispatchEvent(new Event('ce-op-done')); }catch(_){}
    try{ opBarHide(); }catch(_){}
    if(!n){ msg.textContent='オープニングは付いていません（外すものがありません）'; return; }
    markDirty();
    msg.textContent='🗑 オープニングを外しました（'+n+'個）。白い開始と、元サイトの動きとのズレが無くなります・💾保存で確定・⟲で戻せます';
  }
  var opDelBtn=document.getElementById('__ce_op_del');
  if(opDelBtn) opDelBtn.addEventListener('click',removeOpening);
  var shapesBtn=document.getElementById('__ce_shapes');           // 🔶 図形バーの出し入れ
  if(shapesBtn) shapesBtn.addEventListener('click',openShapeBar);
  var opAddBtn=document.getElementById('__ce_op_add');
  if(opAddBtn) opAddBtn.addEventListener('click',addOpening);
  var opEditBtn=document.getElementById('__ce_op_edit');
  if(opEditBtn) opEditBtn.addEventListener('click',toggleOpening);
  // ⭐このセクションをお気に入り（右クリックメニューから呼ぶ・2026-07-12に編集バーから移設）
  function favSaveSection(el){
    if(!el){ msg.textContent='セクションが見つかりません（保存したいセクションの中で右クリックしてください）'; return; }
    var kind=el.tagName.toLowerCase(); if(kind!=='header'&&kind!=='footer') kind='section';
    var h=el.querySelector('h1,h2,h3');
    var label=(h&&(h.textContent||'').replace(/\\s+/g,' ').trim().slice(0,20))||'セクション';
    var name=window.prompt('この'+(kind==='section'?'セクション':kind==='header'?'ヘッダー':'フッター')+'を「部品」として保存します。別のカンプの同じ枠にAIなしで入れ替えできます。\\n名前をどうぞ：', label);
    if(name===null) return;
    msg.textContent='⭐保存中…';
    // 効いているCSSルールを@scopeで抱かせて自己完結させる（selfContain=true・種類問わず）。
    // ★以前はセクションだけselfContain=falseだった＝別カンプ（特にクローン由来）に🔀/➕すると
    //   持ち込み先にクラス定義が無く、レイアウトが崩れて縦に間延びする実害があった→常時trueに統一。
    fetch('/api/section_fav/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({html:cleanSection(el, true).outerHTML,headcss:headCss(),name:name,kind:kind})})
    .then(function(r){return r.json();}).then(function(d){
      var _warn=(window.__cePartRem>5)?'　⚠この部品はrem基準のサイトです。貼り先で大きさが数倍になる可能性があります（元サイトのクローンページを開いた状態で保存し直すと直ります）':'';
      msg.textContent=d.ok?('⭐保存しました「'+((d.fav&&d.fav.name)||'')+'」。🔀から他のカンプでも使えます'+_warn):('保存失敗：'+(d.message||''));
    }).catch(function(){ msg.textContent='通信エラー'; });
  }
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
  // 📐 コーディング仕様書（実測・AIなし）。未保存の編集を先に焼き込んでから測る
  var specBtn=document.getElementById('__ce_spec');
  if(specBtn) specBtn.addEventListener('click',function(){
    specBtn.disabled=true; var old=specBtn.textContent; specBtn.textContent='📐 実測中…（10〜30秒）';
    function reset(){ specBtn.disabled=false; specBtn.textContent=old; }
    fetch('/api/save_camp_html',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,html:cleanHtml()})})
    .then(function(){ return fetch('/api/make_spec',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE})}); })
    .then(function(r){return r.json();}).then(function(d){
      if(!d.ok){ reset(); msg.textContent='仕様書の開始に失敗：'+(d.message||''); return; }
      var t=setInterval(function(){
        fetch('/api/make_spec/status').then(function(r){return r.json();}).then(function(s){
          if(s.running) return;
          clearInterval(t); reset();
          if(s.error){ msg.textContent='仕様書の作成に失敗：'+s.error; return; }
          if(s.result&&s.result.file){
            msg.textContent='📐 仕様書ができました（'+s.result.sections+'セクション・'+s.result.items+'要素を実測）';
            window.open('/spec/'+encodeURIComponent(s.result.file),'_blank');
          }
        }).catch(function(){});
      },1200);
    }).catch(function(){ reset(); msg.textContent='通信エラー'; });
  });
  // 📱 レスポンシブ検査（3画面幅で実測・AIなし）。未保存の編集を先に焼き込んでから測る
  var respBtn=document.getElementById('__ce_resp');
  if(respBtn) respBtn.addEventListener('click',function(){
    respBtn.disabled=true; var old=respBtn.textContent; respBtn.textContent='📱 3画面幅で検査中…（20〜40秒）';
    function reset(){ respBtn.disabled=false; respBtn.textContent=old; }
    fetch('/api/save_camp_html',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,html:cleanHtml()})})
    .then(function(){ return fetch('/api/resp_check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE})}); })
    .then(function(r){return r.json();}).then(function(d){
      if(!d.ok){ reset(); msg.textContent='検査の開始に失敗：'+(d.message||''); return; }
      var t=setInterval(function(){
        fetch('/api/resp_check/status').then(function(r){return r.json();}).then(function(s){
          if(s.running) return;
          clearInterval(t); reset();
          if(s.error){ msg.textContent='レスポンシブ検査に失敗：'+s.error; return; }
          if(s.result&&s.result.file){
            msg.textContent=(s.result.issues===0?'📱 ✅ 3つの画面幅すべて問題なし！':'📱 ⚠ 崩れ候補 '+s.result.issues+'件。レポートを開きます');
            window.open('/check/'+encodeURIComponent(s.result.file),'_blank');
          }
        }).catch(function(){});
      },1200);
    }).catch(function(){ reset(); msg.textContent='通信エラー'; });
  });
  // 📱 スマホ版を作る（375px実測→SP用CSSを注入した新ファイル・AIなし）。未保存の編集を焼き込んでから変換
  var spBtn=document.getElementById('__ce_sp');
  if(spBtn) spBtn.addEventListener('click',function(){
    spBtn.disabled=true; var old=spBtn.textContent; spBtn.textContent='📱 スマホ版に変換中…（20〜40秒）';
    function reset(){ spBtn.disabled=false; spBtn.textContent=old; }
    fetch('/api/save_camp_html',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,html:cleanHtml()})})
    .then(function(){ return fetch('/api/sp_convert',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE})}); })
    .then(function(r){return r.json();}).then(function(d){
      if(!d.ok){ reset(); msg.textContent='スマホ版変換の開始に失敗：'+(d.message||''); return; }
      var t=setInterval(function(){
        fetch('/api/sp_convert/status').then(function(r){return r.json();}).then(function(s){
          if(s.running) return;
          clearInterval(t); reset();
          if(s.error){ msg.textContent='スマホ版変換に失敗：'+s.error; return; }
          if(s.result&&s.result.file){
            msg.textContent='📱 スマホ版ができました（'+s.result.fixes+'箇所を調整）。元カンプは無傷です';
            window.open('/sp/'+encodeURIComponent(s.result.file),'_blank');
          }
        }).catch(function(){});
      },1200);
    }).catch(function(){ reset(); msg.textContent='通信エラー'; });
  });
  // 🎬 アニメ実装キット（静的解析＝同期で一瞬・AIなし）。未保存の編集を焼き込んでから作る
  var kitBtn=document.getElementById('__ce_kit');
  if(kitBtn) kitBtn.addEventListener('click',function(){
    kitBtn.disabled=true; var old=kitBtn.textContent; kitBtn.textContent='🎬 書き出し中…';
    function reset(){ kitBtn.disabled=false; kitBtn.textContent=old; }
    fetch('/api/save_camp_html',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,html:cleanHtml()})})
    .then(function(){ return fetch('/api/anim_kit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE})}); })
    .then(function(r){return r.json();}).then(function(d){
      reset();
      if(!d.ok){ msg.textContent='キットの作成に失敗：'+(d.message||''); return; }
      msg.textContent='🎬 実装キットができました（アニメ付き要素 '+d.rows+'件）';
      window.open('/kit/'+encodeURIComponent(d.file),'_blank');
    }).catch(function(){ reset(); msg.textContent='通信エラー'; });
  });
  // 🧹 大掃除（2026-07-20・AIなし）：AI修正に投げる時のトークン膨らみの犯人を一括除去。
  //  ①文字アニメの分割span(.fxa_ch/.fxa_ln)→素のテキストへ（一文字ずつ/タイプライター/波打ち/行マスクは解除）
  //  ②昔の保険が焼き込んだ無意味なinline style（opacity:1等・消して隠れる物は自動で残す）
  //  ③属性も中身も無い空のspan/div。実行後は⟲（Ctrl+Z）で1手で戻せる。
  var bigCleanBtn=document.getElementById('__ce_bigclean');
  if(bigCleanBtn) bigCleanBtn.addEventListener('click',function(){
    var chN=document.querySelectorAll('.fxa_ch,.fxa_ln').length;
    var junk=0,emp=0;
    var all=[].slice.call(document.body.querySelectorAll('*')).filter(function(e){
      return !(e.closest&&e.closest('#__ce,#__ce_cm,#__ce_pk,#__ce_dlyp,#__ce_flypn'))&&!(e.id&&e.id.indexOf('__ce')===0);
    });
    all.forEach(function(e){
      if(!e.style) return;
      if(e.style.opacity==='1') junk++;
      if(e.style.visibility==='visible') junk++;
      if(e.style.transform==='none') junk++;
      if((e.tagName==='SPAN'||e.tagName==='DIV')&&!e.attributes.length&&!e.childNodes.length) emp++;
    });
    if(!chN&&!junk&&!emp){ if(msg) msg.textContent='🧹 掃除する物が見つかりませんでした（もう軽い状態です）'; return; }
    if(!confirm('🧹 大掃除します：\\n・文字分割span '+chN+'個 → 素のテキストへ（一文字ずつ系の動きは解除）\\n・残骸style '+junk+'件\\n・空のspan/div '+emp+'個\\n\\n⟲（Ctrl+Z）で戻せます。実行しますか？')) return;
    fxUnsplit(document.body);
    [].slice.call(document.querySelectorAll('.fxa_tw,.fxa_cpre,.fxa_sk,.fxa_wave,.fxa_lines')).forEach(function(e){
      e.classList.remove('fxa_tw','fxa_cpre','fxa_sk','fxa_wave','fxa_lines');
      e.style.removeProperty('--fxa-stag'); e.style.removeProperty('--fxa-bnc');
      if(!/fxa_(y|yd|xl|xr|s|bl|ry|fl|wp|cl|cc|clip)( |$)/.test(e.className)){ e.classList.remove('fxa_pre','fxa_in'); e.style.removeProperty('--fxa-dist'); }
    });
    all.forEach(function(e){
      if(!e.style||!document.contains(e)) return;
      // 消して隠れてしまう物（CSS側がopacity:0等）は自動で元に戻す＝見た目は絶対に変えない
      if(e.style.opacity==='1'){ e.style.removeProperty('opacity'); if(parseFloat(getComputedStyle(e).opacity)<0.9) e.style.setProperty('opacity','1','important'); }
      if(e.style.visibility==='visible'){ e.style.removeProperty('visibility'); if(getComputedStyle(e).visibility==='hidden') e.style.setProperty('visibility','visible','important'); }
      if(e.style.transform==='none'){ e.style.removeProperty('transform'); if(getComputedStyle(e).transform!=='none') e.style.setProperty('transform','none','important'); }
      if(e.getAttribute&&e.getAttribute('style')==='') e.removeAttribute('style');
      if((e.tagName==='SPAN'||e.tagName==='DIV')&&!e.attributes.length&&!e.childNodes.length) e.remove();
    });
    markDirty(); pushUndo('bigclean');
    if(msg) msg.textContent='🧹 大掃除しました（分割span'+chN+'個・残骸'+junk+'件・空要素'+emp+'個）。⟲で戻せます／💾保存で確定';
  });
  // 🗂 バックアップ（AIなし・一瞬）：最後に💾保存した状態のファイルを複製→履歴一覧に並ぶ
  var bkBtn=document.getElementById('__ce_bk');
  if(bkBtn) bkBtn.addEventListener('click',function(){
    if(_dirty && !confirm('未保存の変更はバックアップに入りません（「最後に💾保存した状態」の複製です）。\\nこのまま作りますか？（キャンセルして先に💾保存もできます）')) return;
    fetch('/api/camp_backup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE})})
      .then(function(r){ return r.json(); })
      .then(function(j){ if(msg) msg.textContent=j.ok?('🗂 バックアップを作りました（ホームの履歴一覧に「🗂バックアップ」の名前で並びます）'):('⚠ '+(j.message||'バックアップ失敗')); })
      .catch(function(){ if(msg) msg.textContent='⚠ バックアップに失敗しました'; });
  });
  // 🛑 アニメを全部止める（編集中だけの一時停止・2026-07-29）
  //   ★「消す」のではなく「動き終わった形で固定する」。animation/transitionを切るだけだと
  //     出現前(opacity:0)のまま固まって真っ白になるので、出現系だけ見える状態に上書きする。
  //   ★保存版には残さない：注入するのは <style id="__ce_noanimcss"> 1枚だけで、cleanHtmlの
  //     除去リストに明示してある（<style>は「id^=__ce 丸ごと除去」の例外なので書かないと焼き込まれる・§7㉔）。
  //   ★translate: は触らない：このツールのドラッグ移動が translate を使っているため（§7㉕）。
  //     transform だけ none にすれば、出現アニメのズレは消えて手で動かした位置は残る。
  var NOANIM_SHOW='.fxa_pre,.fxa_cpre,.fxa_tw,.fxa_sk,.fxa_lines,.fxa_ch,.fxa_lni,.fxa_hl,.fxa_ud,'
    +'.scrollanime,.updown,.downup,.slide-left,.slide-right,.scaleup,.eachTextAnime,'
    +'[class*="reveal"],[class*="fade"],[class*="animate"],[class*="inview"],[class*="in-view"],'
    +'[class*="stagger"],[class*="slide"],[class*="appear"],[data-reveal]';
  function noAnimCss(){
    // ツール自身のUI(#__ce…)は止めない＝編集バー・パネルの操作感はそのまま
    var kill='html.__ce_noanim *:not([id^="__ce"]):not([id^="__ce"] *)';
    var show=NOANIM_SHOW.split(',').map(function(s){ return 'html.__ce_noanim '+s.trim(); }).join(',');
    return kill+','+kill+'::before,'+kill+'::after{animation:none!important;transition:none!important}'
      +'html.__ce_noanim{scroll-behavior:auto!important}'
      +'html.__ce_noanim #__op_screen{display:none!important}'
      +show+'{opacity:1!important;transform:var(--fxa-tf0,none)!important;filter:none!important;clip-path:none!important}';
  }
  function stopAnimSet(on){
    var st=document.getElementById('__ce_noanimcss');
    if(on){
      if(!st){ st=document.createElement('style'); st.id='__ce_noanimcss'; document.head.appendChild(st); }
      st.textContent=noAnimCss();
      document.documentElement.classList.add('__ce_noanim');
      // 出現待ちのものは「出たあと」の状態にしておく（マーカー/下線は--hlwを引き切る）
      [].slice.call(document.querySelectorAll('.fxa_pre,.fxa_cpre,.fxa_tw,.fxa_sk,.fxa_lines')).forEach(function(n){ n.classList.add('fxa_in'); });
      [].slice.call(document.querySelectorAll('.fxa_hl,.fxa_ud')).forEach(function(n){ n.style.setProperty('--hlw',100); n.classList.add('fxa_in'); });
    } else {
      document.documentElement.classList.remove('__ce_noanim');
      if(st) st.remove();
      [].slice.call(document.querySelectorAll('.fxa_in')).forEach(function(n){ n.classList.remove('fxa_in'); });
      [].slice.call(document.querySelectorAll('.fxa_hl,.fxa_ud')).forEach(function(n){ n.style.setProperty('--hlw',0); });
    }
    var b=document.getElementById('__ce_stopanim');
    if(b){
      b.textContent=on?'▶ アニメを動かす（元に戻す）':'🛑 アニメを全部止める';
      b.style.background=on?'#b91c1c':'#f2f2f4';
      b.style.color=on?'#fff':'#1d1d1f';
    }
  }
  window.__ceNoAnimSet=stopAnimSet;                                   // Escの最後の砦などから呼べるよう公開
  var stopAnimBtn=document.getElementById('__ce_stopanim');
  if(stopAnimBtn) stopAnimBtn.addEventListener('click',function(){
    var on=!document.documentElement.classList.contains('__ce_noanim');
    stopAnimSet(on);
    ceFlash(on?'🛑 アニメを全部止めました（もう一度押すと戻ります）':'▶ アニメを元に戻しました');
    if(msg) msg.textContent=on
      ? '🛑 アニメ停止中：出現・ループ・文字送りを全部止めて「動き終わった形」で固定中です。この状態は保存版には残りません（💾保存してもアニメは消えません）'
      : '▶ アニメを元に戻しました（スクロールするとまた再生されます）';
  });
  // 📦 本番化キット（AIなし・一瞬）。そうじ済み見本＋anim部品＋変換指示.md＋規約を1フォルダに書き出す
  var prodBtn=document.getElementById('__ce_prod');
  if(prodBtn) prodBtn.addEventListener('click',function(){
    // 出力フォルダを指定できる（空欄=既定のdata/camps/prod）。前回の指定はこのブラウザに記憶
    var _od=prompt('出力フォルダをフルパスで指定（例 D:\\\\web\\\\kit）\\n空欄のままOK＝いつもの場所（data\\\\camps\\\\prod）に出します', localStorage.getItem('__ce_prod_dir')||'');
    if(_od===null) return;  // キャンセル
    _od=_od.trim();
    try{ if(_od) localStorage.setItem('__ce_prod_dir',_od); else localStorage.removeItem('__ce_prod_dir'); }catch(_){}
    // 📐仕様書・📱レスポンシブ検査の実測（Playwright）を含むので1分前後かかる。無言だと固まったように見える
    prodBtn.disabled=true; var old=prodBtn.textContent; prodBtn.textContent='📦 書き出し中…（実測に1分ほど）';
    msg.textContent='📦 仕様書とレスポンシブ検査も一緒に作っています…（1分ほどかかります）';
    function reset(){ prodBtn.disabled=false; prodBtn.textContent=old; }
    fetch('/api/save_camp_html',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,html:cleanHtml()})})
    .then(function(){ return fetch('/api/prod_kit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,out_dir:_od})}); })
    .then(function(r){return r.json();}).then(function(d){
      reset();
      if(!d.ok){ msg.textContent='本番化キットの作成に失敗：'+(d.message||''); return; }
      msg.textContent='📦 できました → '+d.dir+' ｜ このフォルダでClaude Code/Codexを開いて「変換指示.mdどおりにやって」と言うだけ'+(d.rules?'':'（⚠規約ファイルが見つからず未同梱）');
      try{ navigator.clipboard.writeText(d.dir); }catch(_){ }
    }).catch(function(){ reset(); msg.textContent='通信エラー'; });
  });
  // 🎨 Figma用に書き出す（AIなし・一瞬）：掃除＋アニメ潰し＋画像埋め込み→キャプチャ用ページを開く。
  //   Figmaには動きは付かない（静止画で入る）＝動きは同梱のアニメキットでコーダーへ渡す設計。
  var figBtn=document.getElementById('__ce_figma');
  if(figBtn) figBtn.addEventListener('click',function(){
    figBtn.disabled=true; var old=figBtn.textContent; figBtn.textContent='🎨 書き出し中…';
    function reset(){ figBtn.disabled=false; figBtn.textContent=old; }
    fetch('/api/save_camp_html',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,html:cleanHtml()})})
    .then(function(){ return fetch('/api/figma_kit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE})}); })
    .then(function(r){return r.json();}).then(function(d){
      reset();
      if(!d.ok){ msg.textContent='Figma書き出しに失敗：'+(d.message||''); return; }
      var mn=d.missing?('・⚠画像'+d.missing+'枚は取得できず'):'';
      msg.innerHTML='🎨 Figma用ページを開きました。右上の <b>html.to.design 拡張 → Capture Current Page</b>（幅1440px）で取り込めます。'
        +'<br>持ち運び用ファイル＋動きの引き継ぎ(アニメキット)は <b>'+d.dir+'</b> に出しました（画像'+d.embedded+'枚埋め込み'+mn+'）';
      window.open(d.capture_url,'_blank');
    }).catch(function(){ reset(); msg.textContent='通信エラー'; });
  });
  // 🔃 セクション並べ替え（AIなし・無料）：DOMごと入れ替え→💾保存で確定。
  //   一覧は最上位の<section>だけ（入れ子の中身は数えない）。入れ物が違う同士は安全のため拒否
  var swapBtn=document.getElementById('__ce_secswap');
  if(swapBtn) swapBtn.addEventListener('click',function(){
    var old=document.getElementById('__ce_secp'); if(old){ old.remove(); return; }
    var p=document.createElement('div'); p.id='__ce_secp'; p.className='__ce_hdl';
    p.style.cssText='position:fixed;left:20px;top:70px;z-index:2147483040;width:300px;max-height:76vh;overflow:auto;background:#1d1d1f;color:#eee;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.45);font:12.5px/1.6 sans-serif;padding:10px 12px';
    document.body.appendChild(p);
    function secs(){
      return [].slice.call(document.body.querySelectorAll('section')).filter(function(s){
        return !(s.parentElement&&s.parentElement.closest('section'));
      });
    }
    function label(s){
      var h=s.querySelector('h1,h2,h3');
      var t=((h?h.textContent:s.textContent)||'').replace(/\s+/g,' ').trim().slice(0,14);
      return t||'(無題)';
    }
    function render(){
      var list=secs();
      var html='<div style="display:flex;align-items:center;margin-bottom:8px"><b>🔃 セクション並べ替え</b><span id="__ce_secpx" style="margin-left:auto;cursor:pointer;padding:0 6px">✕</span></div>';
      list.forEach(function(s,i){
        html+='<div style="display:flex;align-items:center;gap:6px;padding:4px 0;border-top:1px solid #333">'
          +'<span style="width:20px;color:#9ab">'+(i+1)+'</span>'
          +'<span style="flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">'+label(s).replace(/</g,'&lt;')+'</span>'
          +'<button data-mv="up" data-i="'+i+'" '+(i===0?'disabled':'')+' style="border:0;border-radius:5px;background:#3a4763;color:#fff;cursor:pointer;padding:2px 8px">↑</button>'
          +'<button data-mv="dn" data-i="'+i+'" '+(i===list.length-1?'disabled':'')+' style="border:0;border-radius:5px;background:#3a4763;color:#fff;cursor:pointer;padding:2px 8px">↓</button>'
          +'</div>';
      });
      html+='<div style="margin-top:8px;color:#9ab;font-size:11px">↑↓を押すとページに即反映されます。最後に💾保存で確定（保存しなければ開き直しで元どおり）</div>';
      p.innerHTML=html;
      p.querySelector('#__ce_secpx').addEventListener('click',function(){ p.remove(); });
      [].slice.call(p.querySelectorAll('button[data-mv]')).forEach(function(b){
        b.addEventListener('click',function(){
          var list=secs(), i=+b.getAttribute('data-i');
          var s=list[i], o=(b.getAttribute('data-mv')==='up')?list[i-1]:list[i+1];
          if(!s||!o) return;
          if(s.parentNode!==o.parentNode){ if(msg) msg.textContent='この2つは入れ物が違うため入れ替えできません'; return; }
          if(b.getAttribute('data-mv')==='up') s.parentNode.insertBefore(s,o);
          else s.parentNode.insertBefore(o,s);
          markDirty(); render();
          try{ s.scrollIntoView({block:'center'}); }catch(_){ }
          if(msg) msg.textContent='🔃 「'+label(s)+'」を移動しました（💾保存で確定）';
        });
      });
    }
    render();
  });
  // 🔍 インスペクトモード（コーダー受け渡し用・AIなし・無料）
  // ONの間：ホバー＝青枠＋寸法タグ／クリック＝数値パネル（サイズ・文字・色・余白・CSSコピー）／
  // 選択中に他要素へホバー＝Figma風のすき間距離（px）。ドラッグ・右クリックメニューは全部お休み
  // （_inUI2がwindow.__ceInspOnで常にtrue＝既存の編集系が反応しない）。Escか同じボタンで終了。
  // UI要素は全部 class="__ce_ipui"＝cleanHtmlの除去リスト登録済み＝保存に紛れない。
  (function(){
    var inspBtn=document.getElementById('__ce_insp'); if(!inspBtn) return;
    var on=false, sel=null, ov=null, dim=null, l1=null, l2=null, panel=null;
    var pin=null;  // クリック2連打で固定した距離測定ペア {a,b}（ホバーを外しても消えない）
    var bA=null, bB=null;  // 測定中の2要素を囲むピンク枠
    var l3=null, l4=null;  // 親子測定用の追加ライン（右・下）
    var shield=null;  // 透明シールド：クリックがページ（リンク等）に一切届かないようにする板
    function mk(tag,css){ var d=document.createElement(tag); d.className='__ce_ipui'; d.style.cssText=css; document.body.appendChild(d); return d; }
    function ensure(){
      if(ov) return;
      // シールドは編集バー(z:2147483000)より下＝編集バーのボタンは押せる。ページのリンクには届かない
      shield=mk('div','position:fixed;left:0;top:0;right:0;bottom:0;z-index:2147482998;background:transparent;cursor:crosshair;display:none');
      ov=mk('div','position:fixed;z-index:2147483000;pointer-events:none;border:1.5px solid #18a0fb;background:rgba(24,160,251,.08);display:none');
      dim=mk('div','position:fixed;z-index:2147483001;pointer-events:none;background:#18a0fb;color:#fff;font:11px/1.6 sans-serif;padding:1px 7px;border-radius:3px;display:none;white-space:nowrap');
      l1=mk('div','position:fixed;z-index:2147483001;pointer-events:none;background:#e91e63;height:1.5px;display:none');
      l2=mk('div','position:fixed;z-index:2147483001;pointer-events:none;background:#e91e63;width:1.5px;display:none');
      l1.innerHTML='<span style="position:absolute;left:50%;top:-22px;transform:translateX(-50%);background:#e91e63;color:#fff;font:11px/1.5 sans-serif;padding:0 6px;border-radius:3px;white-space:nowrap"></span>';
      l2.innerHTML='<span style="position:absolute;top:50%;left:8px;transform:translateY(-50%);background:#e91e63;color:#fff;font:11px/1.5 sans-serif;padding:0 6px;border-radius:3px;white-space:nowrap"></span>';
      // 親子測定用の追加ライン（右・下）。l1/l2と同じ構造
      l3=mk('div','position:fixed;z-index:2147483001;pointer-events:none;background:#e91e63;height:1.5px;display:none');
      l4=mk('div','position:fixed;z-index:2147483001;pointer-events:none;background:#e91e63;width:1.5px;display:none');
      l3.innerHTML=l1.innerHTML; l4.innerHTML=l2.innerHTML;
      // 測定中の2要素を囲む枠（どれとどれの距離か一目で分かるように）
      bA=mk('div','position:fixed;z-index:2147483000;pointer-events:none;border:1.5px dashed #e91e63;display:none');
      bB=mk('div','position:fixed;z-index:2147483000;pointer-events:none;border:1.5px dashed #e91e63;display:none');
      // XD風の右サイド固定パネル（全高・ダーク）
      panel=mk('div','position:fixed;z-index:2147483002;top:0;right:0;bottom:0;width:300px;overflow:auto;background:#18191b;color:#dde;font:12px/1.7 sans-serif;box-shadow:-6px 0 24px rgba(0,0,0,.35);display:none');
      panel.id='__ce_ip';
      panel.addEventListener('mousedown',function(e){ e.stopPropagation(); });
      panel.addEventListener('click',function(e){ e.stopPropagation(); });
    }
    function esc(t){ return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
    function px(v){ return Math.round(parseFloat(v)||0); }
    function hex(c){
      if(!c) return '';
      var i=c.indexOf('('); if(i<0) return c;
      var p=c.slice(i+1,c.indexOf(')')).split(',').map(function(x){return parseFloat(x);});
      if(p.length>3&&p[3]===0) return '';
      var s='#'; for(var k=0;k<3;k++){ var h=Math.round(p[k]).toString(16); s+=(h.length<2?'0':'')+h; }
      return s;
    }
    function selectorOf(el){
      var s=el.tagName.toLowerCase();
      var cls=el.className; if(cls&&cls.baseVal!==undefined) cls=cls.baseVal;
      var cs=String(cls||'').trim().split(/\\s+/).filter(function(c){return c&&c.indexOf('__ce')!==0&&c!=='fxa_in';}).slice(0,3);
      if(el.id) s+='#'+el.id; else if(cs.length) s+='.'+cs.join('.');
      return s;
    }
    function fourVal(s,name){
      var t=px(s[name+'Top']),r=px(s[name+'Right']),b=px(s[name+'Bottom']),l=px(s[name+'Left']);
      if(!t&&!r&&!b&&!l) return '';
      if(t===b&&r===l) return (t===r)?(t+'px'):(t+'px '+r+'px');
      return t+'px '+r+'px '+b+'px '+l+'px';
    }
    function remOf(fs){
      var root=parseFloat(getComputedStyle(document.documentElement).fontSize)||16;
      var r=Math.round(fs/root*1000)/1000; return r+'rem';
    }
    function cssOf(el){
      var s=getComputedStyle(el), r=el.getBoundingClientRect(), out=[];
      out.push('/* '+selectorOf(el)+'  実寸 '+Math.round(r.width)+'x'+Math.round(r.height)+'px */');
      out.push(selectorOf(el)+' {');
      function add(p,v){ if(v) out.push('  '+p+': '+v+';'); }
      if(s.display!=='block'&&s.display!=='inline') add('display',s.display);
      if(s.position!=='static') add('position',s.position);
      if(s.display.indexOf('flex')>=0||s.display.indexOf('grid')>=0){
        add('gap',(s.gap&&s.gap!=='normal'&&px(s.gap))?s.gap:'');
        if(s.justifyContent!=='normal'&&s.justifyContent!=='flex-start') add('justify-content',s.justifyContent);
        if(s.alignItems!=='normal'&&s.alignItems!=='stretch') add('align-items',s.alignItems);
        if(s.display.indexOf('grid')>=0) add('grid-template-columns',s.gridTemplateColumns);
      }
      // 文字を持たない要素（画像等）に文字系CSSを出しても意味がないので出し分ける
      if(hasTxt(el)){
        var fam=(s.fontFamily||'').split(',')[0].trim();
        add('font-family',fam);
        add('font-size',px(s.fontSize)+'px  /* '+remOf(parseFloat(s.fontSize))+' */');
        if(s.fontWeight!=='400') add('font-weight',s.fontWeight);
        if(s.lineHeight!=='normal'){ var lh=Math.round(parseFloat(s.lineHeight)/parseFloat(s.fontSize)*100)/100; add('line-height',lh+'  /* '+px(s.lineHeight)+'px */'); }
        if(s.letterSpacing!=='normal') add('letter-spacing',s.letterSpacing);
        if(s.textAlign!=='start'&&s.textAlign!=='left') add('text-align',s.textAlign);
        add('color',hex(s.color));
      } else if(el.tagName==='IMG'){
        add('width',Math.round(r.width)+'px');
        add('height',Math.round(r.height)+'px');
        if(s.objectFit&&s.objectFit!=='fill') add('object-fit',s.objectFit);
      }
      var bg=hex(s.backgroundColor); if(bg) add('background-color',bg);
      if(s.backgroundImage&&s.backgroundImage!=='none') add('background-image',s.backgroundImage.length>90?'/* グラデ/画像あり（長いので省略） */':s.backgroundImage);
      add('padding',fourVal(s,'padding'));
      add('margin',fourVal(s,'margin'));
      if(s.borderTopStyle!=='none'&&px(s.borderTopWidth)) add('border',s.borderTopWidth+' '+s.borderTopStyle+' '+hex(s.borderTopColor));
      if(px(s.borderRadius)||s.borderRadius.indexOf('%')>=0) add('border-radius',s.borderRadius);
      if(s.boxShadow&&s.boxShadow!=='none') add('box-shadow',s.boxShadow);
      // 配置と変形：これが無いと「飾りをどこにどう置くか」が実装できない
      if(s.position==='absolute'||s.position==='fixed'){
        add('top',px(s.top)+'px'); add('left',px(s.left)+'px');
        out.push('  /* 親要素に position: relative を付けること */');
      }
      if(s.transform&&s.transform!=='none') add('transform',(el.style&&el.style.transform)||s.transform);
      out.push('}');
      return out.join('\\n');
    }
    function copyText(t,btn){
      function done(){ if(btn){ var o=btn.textContent; btn.textContent='コピーしました ✅'; setTimeout(function(){ btn.textContent=o; },1200); } }
      if(navigator.clipboard&&navigator.clipboard.writeText){ navigator.clipboard.writeText(t).then(done); return; }
      var ta=document.createElement('textarea'); ta.value=t; document.body.appendChild(ta); ta.select();
      try{ document.execCommand('copy'); }catch(e){} ta.remove(); done();
    }
    function hasTxt(el){ return el.tagName!=='IMG' && (el.textContent||'').trim().length>0; }
    // matrix(...)を読める形（translate/rotate）に直す（純回転のときだけ・無理なら素のまま）
    function tfNice(m){
      var mm=/^matrix\\(([-\\d.]+), ?([-\\d.]+), ?([-\\d.]+), ?([-\\d.]+), ?([-\\d.]+), ?([-\\d.]+)\\)$/.exec(m||'');
      if(!mm) return m;
      var a=+mm[1],b=+mm[2],e=+mm[5],f=+mm[6];
      var sc=Math.sqrt(a*a+b*b), ang=Math.round(Math.atan2(b,a)*180/Math.PI);
      if(Math.abs(sc-1)<0.02){
        var t=(Math.round(e)||Math.round(f))?('translate('+Math.round(e)+'px, '+Math.round(f)+'px)'):'';
        var r=ang?('rotate('+ang+'deg)'):'';
        return (t+' '+r).trim()||'none';
      }
      return m;
    }
    // ::before/::after（CSSだけで描かれた飾り）を、コピペで再現できるCSS見本に起こす
    function pseudoCss(el){
      var out=[];
      ['before','after'].forEach(function(k){
        var ps=null; try{ ps=getComputedStyle(el,'::'+k); }catch(_){ return; }
        if(!ps||!ps.content||ps.content==='none'||ps.content==='normal') return;
        var o=[selectorOf(el)+'::'+k+' {'];
        o.push('  content: '+ps.content+';');
        if(ps.position!=='static'){
          o.push('  position: '+ps.position+';  /* 親に position: relative */');
          ['top','left','right','bottom'].forEach(function(p){ if(ps[p]&&ps[p]!=='auto') o.push('  '+p+': '+ps[p]+';'); });
        }
        if(parseFloat(ps.width)) o.push('  width: '+ps.width+';');
        if(parseFloat(ps.height)) o.push('  height: '+ps.height+';');
        if(ps.content!=='\"\"'){ o.push('  font-size: '+ps.fontSize+';'); var c=hex(ps.color); if(c) o.push('  color: '+c+';'); }
        var bg=hex(ps.backgroundColor); if(bg) o.push('  background: '+bg+';');
        if(ps.backgroundImage&&ps.backgroundImage!=='none') o.push('  background-image: '+(ps.backgroundImage.length>90?'/* グラデ/画像（長いので省略） */':ps.backgroundImage)+';');
        if(ps.borderRadius&&parseFloat(ps.borderRadius)) o.push('  border-radius: '+ps.borderRadius+';');
        if(ps.transform&&ps.transform!=='none') o.push('  transform: '+tfNice(ps.transform)+';');
        if(ps.opacity&&ps.opacity!=='1') o.push('  opacity: '+ps.opacity+';');
        o.push('}');
        out.push(o.join('\\n'));
      });
      return out.join('\\n\\n');
    }
    // この要素の動き→アニメ実装キットのクラス名に翻訳（animkit.pyの_FX_MAPと同じ対応表）
    function animOf(el){
      var M=[['fxa_yd','rv rv-down'],['fxa_y','rv rv-up'],['fxa_xl','rv rv-left'],['fxa_xr','rv rv-right'],
        ['fxa_s','rv rv-zoom'],['fxa_bl','rv rv-blur'],['fxa_ry','rv rv-flip'],['fxa_clip','rv rv-up'],
        ['fxa_fl','rv rv-page'],['fxa_wp','rv rv-curtain-l'],['fxa_cl','rv rv-curtain-l'],['fxa_cc','rv rv-curtain-c'],
        ['fxa_lines','rv-lines'],['fxa_cpre','chars'],['fxa_tw','chars'],['fxa_sk','chars'],['fxa_wave','chars lp-wave'],
        ['fxa_lp_pulse','lp-pulse'],['fxa_lp_float','lp-float'],['fxa_lp_bounce','lp-bounce'],['fxa_lp_glow','lp-glow'],
        ['fxa_hl','mk'],['fxa_ud','ud'],['fxa_cnt','cnt']];
      var out=[];
      for(var i=0;i<M.length;i++){ if(el.classList&&el.classList.contains(M[i][0])&&out.indexOf(M[i][1])<0) out.push(M[i][1]); }
      if(!out.length&&el.classList&&el.classList.contains('reveal')) out.push('rv rv-up');
      if(!out.length) return '';
      var kit=out.join(' ');
      var d=el.getAttribute('data-cedelay'); if(d) kit+=' ＋ data-delay="'+d+'"';
      return kit;
    }
    // クローン元サイトが元々持っていた動き（ツールの fxa_* ではない）を拾って言葉で出す。
    //   ツールの翻訳表(animOf)に載らないので「出なかった」動きを、実装の手がかりごと見せる。
    function animOrigin(el){
      var hits=[];
      // ① カウントアップ：data-count（＋書式）を持つ＝数字を0→目標へ数え上げる自前JS
      if(el.hasAttribute&&el.hasAttribute('data-count')){
        var suf=el.getAttribute('data-suffix')||'';
        hits.push('カウントアップ：data-count="'+el.getAttribute('data-count')+'"'+(suf?(' data-suffix="'+suf+'"'):'')+'（0→この数字へ数え上げ）');
      }
      // ② よく使う動きライブラリの目印（属性・クラス）
      if(el.hasAttribute&&el.hasAttribute('data-aos')) hits.push('AOS：data-aos="'+el.getAttribute('data-aos')+'"');
      var cls=(el.className&&el.className.split)?el.className.split(/\\s+/):[];
      cls.forEach(function(c){
        if(/^animate__/.test(c)) hits.push('animate.css：.'+c);
        else if(c==='wow') hits.push('WOW.js：.wow（スクロールで発火）');
        else if(/^(aos|reveal|fade|slide|zoom|count|counter|num)/.test(c) && hits.join(' ').indexOf('.'+c)<0 && !/^count$/.test(c)) hits.push('動きの目印クラス：.'+c);
      });
      if(el.classList&&el.classList.contains('count')&&!el.hasAttribute('data-count')) hits.push('カウントアップ用クラス：.count');
      // ③ CSSアニメ本体（@keyframes名）が乗っているか＝名前で正体が分かることがある
      try{ var cs=getComputedStyle(el); if(cs.animationName&&cs.animationName!=='none') hits.push('CSSアニメ：@keyframes '+cs.animationName+'（'+cs.animationDuration+'）'); }catch(_){}
      return hits;
    }
    // @keyframes名の実物CSSを全シートから抜き出す（クローンCSSは同一オリジン＝読める）。
    function _kf(name){
      for(var i=0;i<document.styleSheets.length;i++){
        var rs; try{ rs=document.styleSheets[i].cssRules; }catch(_){ continue; }   // 他オリジンは触ると例外
        if(!rs) continue;
        for(var j=0;j<rs.length;j++){ if((rs[j].type===7||rs[j].name!=null)&&rs[j].name===name) return rs[j].cssText; }
      }
      return '';
    }
    // 「元の動き」クリックで渡す実装コード（本番コーディングにそのまま使える形）。
    function animOriginCode(el){
      var out=[];
      var cs; try{ cs=getComputedStyle(el); }catch(_){ cs=null; }
      if(cs && cs.animationName && cs.animationName!=='none'){
        var kf=_kf(cs.animationName);
        out.push('/* CSSアニメ */\\n'+(kf||('@keyframes '+cs.animationName+' { /* 元CSSから取得できませんでした */ }'))
          +'\\n.target { animation: '+cs.animationName+' '+cs.animationDuration+' '+cs.animationTimingFunction+' '+cs.animationDelay+' '+(cs.animationIterationCount||'1')+'; }');
      }
      if(el.hasAttribute&&el.hasAttribute('data-count')){
        var suf=el.getAttribute('data-suffix')||'';
        out.push('<!-- カウントアップ（0→'+el.getAttribute('data-count')+'）-->\\n'
          +'<span class="count" data-count="'+el.getAttribute('data-count')+'"'+(suf?(' data-suffix="'+suf+'"'):'')+'>0</span>\\n'
          +'<script>\\ndocument.querySelectorAll(".count").forEach(function(el){\\n'
          +'  var end=+el.dataset.count, suf=el.dataset.suffix||"", t0=null, dur=1200;\\n'
          +'  new IntersectionObserver(function(es,ob){es.forEach(function(e){ if(!e.isIntersecting)return; ob.unobserve(e.target);\\n'
          +'    requestAnimationFrame(function step(t){ t0=t0||t; var p=Math.min(1,(t-t0)/dur);\\n'
          +'      el.textContent=Math.round(end*p)+suf; if(p<1)requestAnimationFrame(step); });\\n'
          +'  });},{threshold:.4}).observe(el);\\n});\\n<\\/script>');
      }
      return out.join('\\n\\n');
    }
    function row(k,v){ return v?('<div style="display:flex;gap:8px;margin:2px 0"><span style="color:#8fa3b8;min-width:64px;flex:none">'+k+'</span><span style="word-break:break-all">'+v+'</span></div>'):''; }
    function sw(c){ return c?('<span style="display:inline-block;width:11px;height:11px;border-radius:3px;background:'+c+';border:1px solid #556;vertical-align:-1px;margin-right:4px"></span>'+c):''; }
    function renderPanel(el){
      ensure();
      var s=getComputedStyle(el), r=el.getBoundingClientRect();
      var fam=(s.fontFamily||'').split(',')[0].trim().replace(/"/g,'');
      var lh=(s.lineHeight==='normal')?'-':(Math.round(parseFloat(s.lineHeight)/parseFloat(s.fontSize)*100)/100+'（'+px(s.lineHeight)+'px）');
      var fx='';
      if(s.display.indexOf('flex')>=0||s.display.indexOf('grid')>=0){
        fx=s.display+(s.gap&&s.gap!=='normal'&&px(s.gap)?(' / gap '+s.gap):'');
      }
      var cell=function(k,v){ return '<div><span style="color:#8fa3b8;display:inline-block;width:16px">'+k+'</span> <b>'+v+'</b></div>'; };
      // 疑似要素の飾り：自分に無ければ親のも見る（飾りをクリックすると中の空要素が選ばれがちなため）
      var pcss=pseudoCss(el);
      if(!pcss&&el.parentElement&&el.parentElement!==document.body) pcss=pseudoCss(el.parentElement);
      // この要素が表示している画像（img / background-image / 中のimg）＝「どの画像か」をサムネで見せる
      var isrc='';
      if(el.tagName==='IMG') isrc=el.currentSrc||el.src||'';
      if(!isrc){ var _bm=/url\\(["']?([^"')]+)/.exec(s.backgroundImage||''); if(_bm) isrc=_bm[1]; }
      if(!isrc&&el.querySelector){ var _im=el.querySelector('img'); if(_im) isrc=_im.currentSrc||_im.src||''; }
      var html=''
        +'<div id="__ce_iph" style="padding:11px 12px;background:#101113;display:flex;align-items:center;gap:8px;position:sticky;top:0">'
        +'<b style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">🔍 '+esc(selectorOf(el))+'</b>'
        +'<button id="__ce_ipup" style="margin-left:auto;border:0;border-radius:5px;background:#3a4763;color:#fff;cursor:pointer;font-size:11px;padding:2px 8px" title="1つ外側の要素を選ぶ">⬆ 親</button>'
        +'<span id="__ce_ipx" style="cursor:pointer;padding:0 4px">✕</span></div>'
        +'<div style="padding:10px 12px;border-bottom:1px solid #2a2d31">'
        +'<div style="display:grid;grid-template-columns:1fr 1fr;gap:2px 14px;font-size:12px">'
        +cell('W',Math.round(r.width)+'px')+cell('X',Math.round(r.left+window.scrollX)+'px')
        +cell('H',Math.round(r.height)+'px')+cell('Y',Math.round(r.top+window.scrollY)+'px')
        +'</div></div>'
        +'<div style="padding:10px 12px">'
        +(isrc?('<div style="margin:2px 0 8px"><img src="'+esc(isrc)+'" style="max-width:100%;max-height:90px;border-radius:6px;border:1px solid #2a2d31;display:block">'
          +'<div style="color:#8fa3b8;font-size:11px;margin-top:3px;word-break:break-all">画像: '
          +esc(isrc.indexOf('data:')===0?'（HTML埋め込み画像）':String(isrc).split('/').pop().split('?')[0].slice(0,48))+'</div></div>'):'')
        +(hasTxt(el)?(
           row('フォント',esc(fam))
          +row('文字',px(s.fontSize)+'px（'+remOf(parseFloat(s.fontSize))+'）/ 太さ'+s.fontWeight)
          +row('行間',lh)
          +(s.letterSpacing!=='normal'?row('字間',esc(s.letterSpacing)):'')
          +row('文字色',sw(hex(s.color)))
        ):'')
        +row('背景色',sw(hex(s.backgroundColor))||(s.backgroundImage!=='none'?'グラデ/画像':''))
        +row('padding',fourVal(s,'padding')||'0')
        +row('margin',fourVal(s,'margin')||'0')
        +(px(s.borderRadius)||s.borderRadius.indexOf('%')>=0?row('角丸',esc(s.borderRadius)):'')
        +(s.borderTopStyle!=='none'&&px(s.borderTopWidth)?row('枠線',esc(s.borderTopWidth+' '+s.borderTopStyle+' ')+sw(hex(s.borderTopColor))):'')
        +(s.boxShadow&&s.boxShadow!=='none'?row('影','あり'):'')
        +(s.position!=='static'?row('配置',esc(s.position)+'（top '+px(s.top)+'px / left '+px(s.left)+'px）'):'')
        +(s.transform&&s.transform!=='none'?row('変形',esc((el.style&&el.style.transform)||'あり（transform）')):'')
        +(fx?row('並べ方',esc(fx)):'')
        +(animOf(el)?row('動き','<code id="__ce_ipanim" title="クリックで🎬キットのこの動きのデモを開く" style="background:#233527;color:#8ee08e;padding:1px 6px;border-radius:4px;cursor:pointer;text-decoration:underline dotted">'+esc(animOf(el))+'</code><span style="color:#8fa3b8">（付けるクラス・クリックでデモ）</span>'):'')
        // クローン元の動き（ツールのfxa_*でない＝翻訳表に無い）も、実装の手がかりごと出す
        +(animOrigin(el).length?row('元の動き','<code id="__ce_iporig" title="クリックで実装コード（keyframes/カウントアップJS）をコピー" style="display:inline-block;background:#3a3320;color:#e0c68e;padding:3px 7px;border-radius:4px;cursor:pointer;text-decoration:underline dotted">'+animOrigin(el).map(function(h){return esc(h);}).join('<br>')+'</code><br><span style="color:#8fa3b8;font-size:10.5px">クローン元由来。クリックで実装コードをコピー（🎬キットにも書き出せます）</span>'):'')
        +'</div>'
        +'<div style="padding:10px 12px;border-top:1px solid #2a2d31">'
        +'<div style="display:flex;align-items:center;margin-bottom:6px"><b style="font-size:11.5px;color:#8fa3b8;letter-spacing:.05em">CSS</b>'
        +'<button id="__ce_iphtml" style="margin-left:auto;border:0;background:none;color:#18a0fb;cursor:pointer;font-size:11.5px" title="飾り文字など複雑な要素はHTMLごとコピペが早い">HTMLをコピー</button>'
        +'<button id="__ce_ipcss" style="border:0;background:none;color:#18a0fb;cursor:pointer;font-size:11.5px;margin-left:10px">CSSをコピー</button></div>'
        +'<pre style="margin:0;background:#24262a;border-radius:8px;padding:10px;font:11px/1.65 Consolas,monospace;white-space:pre-wrap;word-break:break-all;color:#cde">'+esc(cssOf(el))+'</pre>'
        +'<div style="margin-top:8px;color:#8fa3b8;font-size:11px">別の要素をクリック＝距離を固定表示／ホバー＝仮の距離</div>'
        +(pcss?('<div style="padding:10px 12px 0 12px;border-top:1px solid #2a2d31;margin:10px -12px 0 -12px">'
          +'<div style="display:flex;align-items:center;margin-bottom:6px;padding:0 12px"><b style="font-size:11.5px;color:#8fa3b8;letter-spacing:.05em">飾り（CSSだけで描く疑似要素）</b>'
          +'<button id="__ce_ippse" style="margin-left:auto;border:0;background:none;color:#18a0fb;cursor:pointer;font-size:11.5px">見本をコピー</button></div>'
          +'<pre style="margin:0 12px;background:#24262a;border-radius:8px;padding:10px;font:11px/1.65 Consolas,monospace;white-space:pre-wrap;word-break:break-all;color:#cde">'+esc(pcss)+'</pre></div>'):'')
        +'<details style="margin-top:8px;font-size:11px;color:#8fa3b8"><summary style="cursor:pointer;list-style-position:inside">📏 距離線の読み方（実装で使う値はどっち？）</summary>'
        +'<div style="margin-top:6px;padding:8px 10px;background:#242830;border-left:3px solid #eab308;border-radius:0 6px 6px 0;color:#cbd5e1;line-height:1.7">'
        +'ピンクの距離線＝<b>見た目の距離</b>（文字の上下に付く「行間の余り」を含む）。<br>'
        +'CSSに書く値は<b>このパネルのmargin / padding をそのまま</b>使うこと。<br>'
        +'例：見た目36px ＝ margin 24px ＋ 行間の余り12px。36と書くと広がりすぎる。</div></details>'
        +'</div>';
      panel.innerHTML=html; panel.style.display='block';
      panel.querySelector('#__ce_ipx').addEventListener('click',function(){ sel=null; panel.style.display='none'; hideLines(); });
      panel.querySelector('#__ce_ipcss').addEventListener('click',function(){ copyText(cssOf(el),this); });
      panel.querySelector('#__ce_iphtml').addEventListener('click',function(){ copyText(el.outerHTML,this); });
      var pb=panel.querySelector('#__ce_ippse');
      if(pb) pb.addEventListener('click',function(){ copyText(pcss,this); });
      var orig=panel.querySelector('#__ce_iporig');
      if(orig) orig.addEventListener('click',function(){
        var code=animOriginCode(el);
        // btnにthisを渡すと複数行表示が「コピーしました✅」で潰れるので、コピーはサイレント＋トーストで知らせる
        if(code){ copyText(code,null); if(msg) msg.textContent='📋 この動きの実装コードをコピーしました（本番コーディングに貼れます）'; orig.style.outline='2px solid #8ee08e'; setTimeout(function(){ orig.style.outline=''; },700); }
        else if(msg) msg.textContent='この動きはコード化できませんでした（クラス名だけ表示）';
      });
      var an=panel.querySelector('#__ce_ipanim');
      if(an) an.addEventListener('click',function(){
        // 🎬キットを（無ければ作って）開き、この動きのカタログカードへ直接ジャンプ
        var kit=an.textContent.split('＋')[0].trim();
        var anchor='cat-'+kit.replace(/[^a-zA-Z0-9-]+/g,'-');
        fetch('/api/anim_kit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE})})
          .then(function(r){ return r.json(); })
          .then(function(d){ if(d&&d.file) window.open('/kit/'+encodeURIComponent(d.file)+'?hl='+anchor.slice(4)+'&el='+encodeURIComponent(selectorOf(el))+'&tx='+encodeURIComponent((el.textContent||'').replace(/\\s+/g,'').slice(0,16))+'#'+anchor,'_blank'); });
      });
      panel.querySelector('#__ce_ipup').addEventListener('click',function(){
        var p=el.parentElement; if(p&&p!==document.body&&p.tagName!=='HTML'){ sel=p; renderPanel(p); showBox(p); } });
      // 固定サイドバー化に伴いパネルのドラッグ移動は廃止（XD風・位置は右端固定）
    }
    function showBox(el){
      ensure();
      var r=el.getBoundingClientRect();
      ov.style.display='block'; ov.style.left=r.left+'px'; ov.style.top=r.top+'px';
      ov.style.width=r.width+'px'; ov.style.height=r.height+'px';
      dim.style.display='block'; dim.textContent=Math.round(r.width)+' × '+Math.round(r.height);
      var dt=r.top-24; if(dt<2) dt=r.bottom+4;
      dim.style.left=Math.max(2,r.left)+'px'; dim.style.top=dt+'px';
    }
    function hideBox(){ if(ov){ ov.style.display='none'; dim.style.display='none'; } }
    function hideLines(){ [l1,l2,l3,l4,bA,bB].forEach(function(d){ if(d) d.style.display='none'; }); }
    function boxAt(d,r){ d.style.display='block'; d.style.left=r.left+'px'; d.style.top=r.top+'px'; d.style.width=(r.right-r.left)+'px'; d.style.height=(r.bottom-r.top)+'px'; }
    // 距離測定用の矩形＝「実際に文字が描かれている範囲」（マーキー選択と同じ流儀）。
    // CSSの箱は余白ぶん大きくて隣と重なりやすく、重なると「すき間なし」で線が出ない
    // （実際に起きた）。見た目どおりの文字間の距離を出す。文字なし要素（画像等）は箱で測る
    function vrect(el){
      var r=el.getBoundingClientRect();
      try{
        if((el.textContent||'').trim()){
          var rg=document.createRange(); rg.selectNodeContents(el);
          var t=rg.getBoundingClientRect();
          if(t&&t.width>=1&&t.height>=1) return t;
        }
      }catch(_){ }
      return r;
    }
    function hline(el,x1,x2,y){
      if(x2-x1>=1){ el.style.display='block'; el.style.left=x1+'px'; el.style.width=(x2-x1)+'px'; el.style.top=y+'px';
        el.firstChild.textContent=gpx(x2-x1)+'px'; return true; }
      el.style.display='none'; return false;
    }
    function vline(el,y1,y2,x){
      if(y2-y1>=1){ el.style.display='block'; el.style.top=y1+'px'; el.style.height=(y2-y1)+'px'; el.style.left=x+'px';
        el.firstChild.textContent=gpx(y2-y1)+'px'; return true; }
      el.style.display='none'; return false;
    }
    function showGap(a,b){
      ensure();
      var shown=false;
      // 親子（片方がもう片方を含む）＝XD風に「内側の余白」上下左右を測る。
      // 以前は親子をスキップしていて「行→01とクリックしても何も出ない」が実際に起きた
      if(a.contains(b)||b.contains(a)){
        var outEl=a.contains(b)?a:b, inEl=(outEl===a)?b:a;
        var O=outEl.getBoundingClientRect(), I=vrect(inEl);
        boxAt(bA,O); boxAt(bB,I);
        var cy=(I.top+I.bottom)/2, cx=(I.left+I.right)/2;
        shown=hline(l1,O.left,I.left,cy)||shown;
        shown=hline(l3,I.right,O.right,cy)||shown;
        shown=vline(l2,O.top,I.top,cx)||shown;
        shown=vline(l4,I.bottom,O.bottom,cx)||shown;
        return shown;
      }
      var A=vrect(a), B=vrect(b);
      boxAt(bA,A); boxAt(bB,B);
      l3.style.display='none'; l4.style.display='none';
      var ovT=Math.max(A.top,B.top), ovB=Math.min(A.bottom,B.bottom);
      var ovL=Math.max(A.left,B.left), ovR=Math.min(A.right,B.right);
      var y=(ovT<ovB)?((ovT+ovB)/2):((A.top+A.bottom)/2);
      var x=(ovL<ovR)?((ovL+ovR)/2):((A.left+A.right)/2);
      var g1=null;
      if(B.left>=A.right) g1={x1:A.right,x2:B.left}; else if(A.left>=B.right) g1={x1:B.right,x2:A.left};
      if(g1) shown=hline(l1,g1.x1,g1.x2,y)||shown; else l1.style.display='none';
      var g2=null;
      if(B.top>=A.bottom) g2={y1:A.bottom,y2:B.top}; else if(A.top>=B.bottom) g2={y1:B.bottom,y2:A.top};
      if(g2) shown=vline(l2,g2.y1,g2.y2,x)||shown; else l2.style.display='none';
      return shown;
    }
    function inOwnUI(t){ var el=t&&(t.nodeType===1?t:t.parentElement); if(el===shield) return false; return el&&el.closest&&(el.closest('.__ce_ipui')||el.closest('#__ce')); }
    // シールド越しに「その座標にある実際の要素」を透視して拾う
    function pickAt(e){
      var ls=(document.elementsFromPoint?document.elementsFromPoint(e.clientX,e.clientY):[])||[];
      for(var i=0;i<ls.length;i++){
        var n=ls[i];
        if(n===shield) continue;
        if(n.classList&&n.classList.contains('__ce_ipui')) continue;
        if(n.closest&&n.closest('#__ce')) continue;
        return n;
      }
      return null;
    }
    // 距離の表示丸め：50px未満はそのまま（小さい余白は1pxに意味がある）、50px以上は10px単位
    // （元がAI生成＋実測で±数px揺れる＝1桁目は見せても役に立たないため）
    function gpx(v){ v=Math.round(v); return v<50?v:Math.round(v/10)*10; }
    function showPin(){ if(pin){ showGap(pin.a,pin.b); } else hideLines(); }
    function onMove(e){
      if(!on) return;
      if(inOwnUI(e.target)){ if(!sel) hideBox(); showPin(); return; }
      var el=pickAt(e);
      if(!el||el===document.body||el===document.documentElement){ hideBox(); showPin(); return; }
      showBox(el);
      if(sel&&el!==sel) showGap(sel,el); else showPin();
    }
    function onClick(e){
      if(!on||inOwnUI(e.target)) return;
      e.preventDefault(); e.stopPropagation();
      // 何もない所（body直）をクリック＝固定表示と選択をクリア（前の測定が残って紛らわしいため）
      var el=pickAt(e);
      if(!el||el===document.body||el===document.documentElement){
        pin=null; sel=null; hideBox(); hideLines(); if(panel) panel.style.display='none';
        if(msg) msg.textContent='🔍 クリック＝数値パネル／続けて別の要素をクリック＝距離を固定表示。Escで終了';
        return;
      }
      // 2回目のクリック＝前の選択との距離を「固定表示」する（ホバーを外しても消えない）。
      // クリックした要素が次の選択になるので、クリックで渡り歩くと隣同士の距離を順に測れる
      if(sel&&el!==sel){
        pin={a:sel,b:el};
        var isPc=(sel.contains(el)||el.contains(sel));
        if(msg) msg.textContent=showGap(pin.a,pin.b)
          ?(isPc?'📏 親子の距離（内側の余白）を固定表示中（余白クリック＝リセット／Escで終了）'
                :'📏 距離を固定表示中（別の要素をクリック＝次の距離／余白クリック＝リセット）')
          :'📏 この2つは重なっていて、すき間はありません（内側の余白はパネルのpadding/marginを見る）';
      }
      sel=el; renderPanel(el); showBox(el); showPin();
    }
    function onDown(e){ if(on&&!inOwnUI(e.target)){ e.preventDefault(); e.stopPropagation(); } }
    // リンク対策の二重ロック：中クリック(auxclick)・ダブルクリック・他スクリプトのクリック処理も封じる
    function onAux(e){ if(on&&!inOwnUI(e.target)){ e.preventDefault(); e.stopImmediatePropagation(); } }
    function onCtx(e){ if(on&&!inOwnUI(e.target)){ e.preventDefault(); e.stopPropagation(); } }
    function onKey(e){ if(on&&e.key==='Escape'){ toggle(); } }
    function onScroll(){ if(!on) return; if(sel) showBox(sel); else hideBox(); showPin(); }
    function toggle(){
      on=!on; window.__ceInspOn=on; ensure();
      inspBtn.textContent=on?'🔍 インスペクト中（Escで終了）':'🔍 インスペクト（コーダーに数値を渡す）';
      inspBtn.style.background=on?'#18a0fb':'#263238';
      document.documentElement.style.cursor=on?'crosshair':'';
      if(shield) shield.style.display=on?'block':'none';
      if(on){
        document.addEventListener('mousemove',onMove,true);
        document.addEventListener('click',onClick,true);
        document.addEventListener('auxclick',onAux,true);
        document.addEventListener('dblclick',onAux,true);
        document.addEventListener('mousedown',onDown,true);
        document.addEventListener('contextmenu',onCtx,true);
        document.addEventListener('keydown',onKey,true);
        window.addEventListener('scroll',onScroll,true);
        if(msg) msg.textContent='🔍 クリック＝数値パネル／続けて別の要素をクリック＝距離を固定表示。Escで終了';
      } else {
        document.removeEventListener('mousemove',onMove,true);
        document.removeEventListener('click',onClick,true);
        document.removeEventListener('auxclick',onAux,true);
        document.removeEventListener('dblclick',onAux,true);
        document.removeEventListener('mousedown',onDown,true);
        document.removeEventListener('contextmenu',onCtx,true);
        document.removeEventListener('keydown',onKey,true);
        window.removeEventListener('scroll',onScroll,true);
        sel=null; pin=null; hideBox(); hideLines(); if(panel) panel.style.display='none';
        if(msg) msg.textContent='';
      }
    }
    inspBtn.addEventListener('click',toggle);
    window.__ceInspExit=function(){ if(on) toggle(); };   // Escの全体復旧から呼べるように外へ出す
  })();
  // 差し替え/追加した新要素には出現アニメの監視(IntersectionObserver)が付いていない＝
  // fxa_pre/reveal系が透明のまま永久に出ない（実際に起きた）。「もう見えた」状態にして表示する。
  // ここで付けるfxa_in/SHOWクラス/--hlwは💾保存時にcleanHtmlが必ず素に戻す＝保存後の開き直しでは普通に再生される。
  function markRevealed(nw){
    if(!nw) return;
    var SHOW=['in','show','is-visible','active','visible','in-view','inview','animated','revealed','aos-animate','is-inview','is-show','reveal-show','show-up','on','enter'];
    var SEL='[class*="reveal"],[class*="fade"],[class*="animate"],[class*="inview"],[class*="in-view"],[class*="stagger"],[class*="slide"],[class*="appear"],[data-reveal]';
    [].slice.call(nw.querySelectorAll('*')).concat([nw]).forEach(function(n){
      if(!n.classList) return;
      if(n.classList.contains('fxa_pre')) n.classList.add('fxa_in');
      if(n.classList.contains('fxa_hl')||n.classList.contains('fxa_ud')) n.style.setProperty('--hlw',100);
      try{
        var cs=getComputedStyle(n);
        if((cs.opacity==='0'||cs.visibility==='hidden') && n.matches(SEL)) SHOW.forEach(function(k){ n.classList.add(k); });
      }catch(_){}
    });
  }
  // 🧢 デフォルトヘッダー集（AIなし・無料）：⭐が貯まってなくても選べる標準ヘッダー5種。
  // サイト名とメニュー文言は「今のヘッダー」から流用し、色はテーマ色を自動採用＝どのカンプでも馴染む。
  function presetHeaders(target){
    // テーマ色：--accent系のCSS変数 → よく使われているボタン背景色 → 紺 の順で採用
    var accent='';
    ['--accent','--brand','--primary','--main','--theme','--key'].some(function(v){ var c=(getComputedStyle(document.documentElement).getPropertyValue(v)||'').trim(); if(c){accent=c;return true;} return false; });
    if(!accent){
      var freq={};
      [].forEach.call(document.querySelectorAll('a.btn,button,.btn,.cta,.button'),function(el){ var bg=getComputedStyle(el).backgroundColor||''; if(bg && !/rgba?\\(0, 0, 0, 0\\)|transparent/.test(bg)){ freq[bg]=(freq[bg]||0)+1; } });
      var best=null,bn=0; Object.keys(freq).forEach(function(k){ if(freq[k]>bn){bn=freq[k];best=k;} });
      accent=best||'#1f3a5f';
    }
    // サイト名：今のヘッダーのロゴ/見出し → ページタイトル の順
    var name='';
    try{ var lg=target.querySelector('.logo,[class*="logo"],h1,strong,a'); if(lg) name=(lg.textContent||'').replace(/\\s+/g,' ').trim().slice(0,24); }catch(_){}
    if(!name) name=((document.title||'').split(/[|｜]/)[0]||'').trim().slice(0,24)||'SITE NAME';
    // メニュー：今のヘッダーのリンク文言を流用（2〜12文字・重複除去・最大5個）
    var links=[];
    try{ [].slice.call(target.querySelectorAll('a')).forEach(function(a){ var t=(a.textContent||'').replace(/\\s+/g,' ').trim(); if(t && t.length>=2 && t.length<=12 && t!==name && links.indexOf(t)<0) links.push(t); }); }catch(_){}
    links=links.slice(0,5);
    if(links.length<2) links=['ホーム','サービス','料金','お問い合わせ'];
    function navHtml(color,size){ return links.map(function(t){ return '<a href="#" style="color:'+color+';text-decoration:none;font-size:'+size+'px;font-weight:600;letter-spacing:.02em">'+esc(t)+'</a>'; }).join(''); }
    var H='position:relative;z-index:50;font-family:inherit;';
    return [
      {name:'🤍 シンプル白（左ロゴ・右メニュー）', html:
        '<header style="'+H+'display:flex;align-items:center;justify-content:space-between;padding:18px 4%;background:#fff;border-bottom:1px solid rgba(0,0,0,.08)">'
        +'<div style="font-size:20px;font-weight:800;color:#1a1a1a;letter-spacing:.04em">'+esc(name)+'</div>'
        +'<nav style="display:flex;gap:26px;align-items:center">'+navHtml('#333',14)+'</nav></header>'},
      {name:'🎯 中央ロゴ（メニュー下段）', html:
        '<header style="'+H+'text-align:center;padding:22px 4% 14px;background:#fff;border-bottom:1px solid rgba(0,0,0,.08)">'
        +'<div style="font-size:24px;font-weight:800;color:#1a1a1a;letter-spacing:.1em">'+esc(name)+'</div>'
        +'<nav style="display:flex;gap:30px;justify-content:center;margin-top:12px">'+navHtml('#444',13)+'</nav></header>'},
      {name:'🔘 CTAボタン付き（テーマ色）', html:
        '<header style="'+H+'display:flex;align-items:center;justify-content:space-between;padding:14px 4%;background:#fff;box-shadow:0 2px 10px rgba(0,0,0,.06)">'
        +'<div style="font-size:20px;font-weight:800;color:'+accent+';letter-spacing:.04em">'+esc(name)+'</div>'
        +'<div style="display:flex;gap:24px;align-items:center"><nav style="display:flex;gap:24px;align-items:center">'+navHtml('#333',14)+'</nav>'
        +'<a href="#" style="background:'+accent+';color:#fff;padding:10px 22px;border-radius:999px;font-weight:700;font-size:13.5px;text-decoration:none;white-space:nowrap">お問い合わせ</a></div></header>'},
      {name:'📞 上帯付き（テーマ色の細帯）', html:
        '<header style="'+H+'background:#fff;border-bottom:1px solid rgba(0,0,0,.08)">'
        +'<div style="background:'+accent+';color:#fff;font-size:12px;padding:6px 4%;text-align:right;letter-spacing:.05em">お気軽にご相談ください（平日 9:00〜18:00）</div>'
        +'<div style="display:flex;align-items:center;justify-content:space-between;padding:14px 4%">'
        +'<div style="font-size:20px;font-weight:800;color:#1a1a1a;letter-spacing:.04em">'+esc(name)+'</div>'
        +'<nav style="display:flex;gap:26px;align-items:center">'+navHtml('#333',14)+'</nav></div></header>'},
      {name:'🖤 ダーク（黒ベース）', html:
        '<header style="'+H+'display:flex;align-items:center;justify-content:space-between;padding:20px 4%;background:#14181f">'
        +'<div style="font-size:20px;font-weight:800;color:#fff;letter-spacing:.06em">'+esc(name)+'</div>'
        +'<nav style="display:flex;gap:28px;align-items:center">'+navHtml('rgba(255,255,255,.88)',14)+'</nav></header>'},
      // 💊 LinkWorks(business.html)の実CSSから数値採取：白88%+blur・薄青枠・大影・明朝リンク・ピルCTA
      {name:'💊 カプセルナビ（浮遊ピル・LinkWorks風）', html:(function(){
        var pill='background:rgba(255,255,255,.92);border:1px solid rgba(220,226,239,.7);border-radius:999px;box-shadow:0 12px 34px rgba(7,16,38,.12);';
        var lk=links.filter(function(t){ return t.indexOf('問い合わせ')<0 && t.indexOf('お問合せ')<0 && t.toLowerCase().indexOf('contact')<0; });
        if(!lk.length) lk=['ホーム','サービス','会社概要'];
        var nv=lk.map(function(t){ return '<a href="#" style="color:#06122d;text-decoration:none;font-size:14px;font-weight:700;padding:10px 18px;border-radius:999px;font-family:\\'Shippori Mincho\\',\\'Zen Old Mincho\\',serif;white-space:nowrap">'+esc(t)+'</a>'; }).join('');
        return '<header style="'+H+'display:flex;align-items:flex-start;justify-content:space-between;padding:24px 3%;background:transparent">'
          +'<div style="'+pill+'padding:14px 28px;font-size:20px;font-weight:800;color:#111;letter-spacing:.02em">'+esc(name)+'</div>'
          +'<nav style="'+pill+'display:flex;align-items:center;gap:2px;padding:8px">'+nv
          +'<a href="#" style="background:'+accent+';color:#fff;padding:12px 24px;border-radius:999px;font-weight:700;font-size:14px;text-decoration:none;white-space:nowrap;font-family:\\'Shippori Mincho\\',\\'Zen Old Mincho\\',serif">お問い合わせ</a></nav></header>';
      })()}
    ];
  }
  // 🎨 使っている色を置き換える（2026-07-30・AIなし）
  //   ★なぜ必要か：ツールの「ベース色」は CSS変数(--accent等)を書き換える方式なので、
  //     変数を持たないクローン系カンプでは丸ごと無効になる（ヘッダーの色が変えられない実報告）。
  //     そこで「画面で実際に使われている色」を拾い、色→色で置換する＝変数の有無に関係なく効く。
  //   ★当て方は inline の !important：クローン元CSSは詳細度が高く、普通の指定では勝てない。
  //   ★半透明(rgba)は透明度を保ったまま色だけ差し替える（薄い帯が急に濃くなるのを防ぐ）。
  var CR_PROPS=['backgroundColor','color','borderTopColor','borderRightColor','borderBottomColor','borderLeftColor'];
  function crNorm(c){
    c=(c||'').trim();
    if(!c||c==='transparent'||c==='none') return '';
    if(/,\\s*0\\)\\s*$/.test(c)) return '';        // 完全に透明な色は対象外
    return c;
  }
  function crKebab(p){ return p.replace(/[A-Z]/g,function(m){ return '-'+m.toLowerCase(); }); }
  function crEls(root){
    var out=[root].concat([].slice.call(root.querySelectorAll('*')));
    return out.slice(0,4000).filter(function(el){ return !(el.closest&&el.closest('[id^="__ce"]')); });
  }
  function crCollect(root){
    var map={};
    crEls(root).forEach(function(el){
      var cs; try{ cs=getComputedStyle(el); }catch(_){ return; }
      CR_PROPS.forEach(function(p){
        if(p.indexOf('border')===0 && !(parseFloat(cs['border'+p.replace('border','').replace('Color','')+'Width']||'0')>0)) return;
        var v=crNorm(cs[p]); if(!v) return;
        (map[v]=map[v]||{n:0}).n++;
      });
      var bi=cs.backgroundImage||'';
      if(bi && bi!=='none'){
        (bi.match(/rgba?\\([^)]+\\)/g)||[]).forEach(function(v){
          v=crNorm(v); if(!v) return; (map[v]=map[v]||{n:0}).n++;
        });
      }
    });
    return Object.keys(map).map(function(k){ return {c:k,n:map[k].n}; })
      .sort(function(a,b){ return b.n-a.n; }).slice(0,28);
  }
  function crApply(oldC,newHex,root){
    var am=oldC.match(/^rgba\\([^,]+,[^,]+,[^,]+,\\s*([0-9.]+)\\)/);
    var alpha=am?parseFloat(am[1]):1, val=newHex;
    if(alpha<1){
      var h=newHex.replace('#',''); if(h.length===3) h=h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
      val='rgba('+parseInt(h.slice(0,2),16)+', '+parseInt(h.slice(2,4),16)+', '+parseInt(h.slice(4,6),16)+', '+alpha+')';
    }
    var cnt=0;
    crEls(root).forEach(function(el){
      var cs; try{ cs=getComputedStyle(el); }catch(_){ return; }
      var hit=false;
      CR_PROPS.forEach(function(p){
        if(cs[p]!==oldC) return;
        if(p.indexOf('border')===0 && !(parseFloat(cs['border'+p.replace('border','').replace('Color','')+'Width']||'0')>0)) return;
        try{ pushUndo(el); }catch(_){}
        el.style.setProperty(crKebab(p),val,'important');
        // クローン元CSSは -webkit-text-fill-color を持つことがあり、これが残ると文字色が変わらない
        if(p==='color') el.style.setProperty('-webkit-text-fill-color',val,'important');
        hit=true;
      });
      var bi=cs.backgroundImage||'';
      if(bi && bi!=='none' && bi.indexOf(oldC)>=0){
        try{ pushUndo(el); }catch(_){}
        el.style.setProperty('background-image',bi.split(oldC).join(val),'important');
        hit=true;
      }
      if(hit) cnt++;
    });
    try{ markDirty(); }catch(_){}
    return cnt;
  }
  function openColorReplace(scopeEl){
    var old=document.getElementById('__ce_colrep'); if(old) old.remove();
    var here=scopeEl||document.body;
    var sec=(here.closest&&here.closest('section,header,footer,main'))||document.body;
    var p=document.createElement('div'); p.id='__ce_colrep';
    p.setAttribute('style','position:fixed;right:16px;top:70px;z-index:2147483646;width:334px;max-height:78vh;overflow:auto;background:#fff;border:1px solid #d0d0d5;border-radius:12px;box-shadow:0 16px 44px rgba(0,0,0,.28);padding:10px 12px;font:12.5px/1.6 system-ui,sans-serif;color:#1d1d1f');
    function label(el){
      if(el===document.body) return 'ページ全体';
      var t=el.tagName.toLowerCase();
      return t+(el.className?('.'+String(el.className).trim().split(/\\s+/)[0]):'');
    }
    function render(){
      var root=(p.querySelector('input[name=__crsc]:checked')||{}).value==='all'?document.body
             :(p.querySelector('input[name=__crsc]:checked')||{}).value==='sec'?sec:here;
      var list=crCollect(root);
      var sw=list.map(function(it,i){
        return '<button class="__crsw" data-i="'+i+'" title="'+it.c+'（'+it.n+'箇所）" '
          +'style="width:38px;height:30px;border:1px solid rgba(0,0,0,.2);border-radius:6px;background:'+it.c+';cursor:pointer;padding:0;position:relative">'
          +'<span style="position:absolute;right:1px;bottom:0;font-size:9px;color:#000;background:rgba(255,255,255,.75);border-radius:3px;padding:0 2px">'+it.n+'</span></button>';
      }).join('');
      p.querySelector('#__crgrid').innerHTML=sw||'<div style="color:#999">色が見つかりませんでした</div>';
      p.__crlist=list; p.__crroot=root;
      p.querySelector('#__crinfo').textContent='範囲：'+label(root)+'（'+list.length+'色）';
    }
    p.innerHTML='<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">'
      +'<b style="flex:1">🎨 使っている色を置き換える</b>'
      +'<button id="__crx" style="border:none;background:#eee;border-radius:999px;padding:1px 9px;cursor:pointer">×</button></div>'
      +'<div style="font-size:11px;color:#777;margin-bottom:6px">変えたい色をクリック → 新しい色を選ぶ。CSS変数が無いクローンでも効きます</div>'
      +'<div style="display:flex;gap:10px;margin-bottom:6px;font-size:11.5px">'
      +'<label><input type="radio" name="__crsc" value="here" checked> この中</label>'
      +'<label><input type="radio" name="__crsc" value="sec"> セクション</label>'
      +'<label><input type="radio" name="__crsc" value="all"> ページ全体</label></div>'
      +'<div id="__crinfo" style="font-size:11px;color:#0b6bcb;margin-bottom:4px"></div>'
      +'<div id="__crgrid" style="display:flex;flex-wrap:wrap;gap:4px"></div>'
      +'<div id="__crpick" style="display:none;margin-top:8px;border-top:1px solid #eee;padding-top:8px">'
      +'<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">'
      +'<span id="__crfrom" style="width:34px;height:26px;border:1px solid rgba(0,0,0,.2);border-radius:5px"></span>'
      +'<span>→</span><input type="color" id="__crto" style="width:46px;height:30px;padding:1px;border:1px solid #d0d0d5;border-radius:6px;cursor:pointer">'
      +'<button id="__crgo" style="flex:1;border:none;background:#0b6bcb;color:#fff;border-radius:7px;padding:7px;font-weight:700;cursor:pointer">置き換える</button></div>'
      +'<div id="__crmsg" style="font-size:11px;color:#555"></div></div>'
      // ★背景が「透明」の帯は、置き換える色が存在しないので上のスウォッチには出ない。
      //   （2段構えのヘッダーで、下段だけ色が変えられない実報告・2026-07-30）
      //   その場合はここから直接塗る＝無い色は置き換えではなく「付ける」しかない。
      +'<div style="border-top:1px solid #eee;margin-top:9px;padding-top:8px">'
      +'<div style="font-size:11px;color:#777;margin-bottom:5px">背景が透明な帯は上の一覧に出ません（塗る色が無いため）。その場合はここから直接付けます。</div>'
      +'<div style="display:flex;align-items:center;gap:6px">'
      +'<input type="color" id="__crbgc" value="#0b6bcb" style="width:46px;height:30px;padding:1px;border:1px solid #d0d0d5;border-radius:6px;cursor:pointer">'
      +'<button id="__crbg" style="flex:1;border:none;background:#0b6e4f;color:#fff;border-radius:7px;padding:7px;font-weight:700;cursor:pointer">この場所に背景色を付ける</button></div>'
      +'<div id="__crbgt" style="font-size:11px;color:#0b6bcb;margin-top:3px"></div>'
      +'<div id="__crmsg2" style="font-size:11px;color:#555"></div></div>';
    document.body.appendChild(p);
    render();
    p.querySelector('#__crx').addEventListener('click',function(){ p.remove(); });
    [].forEach.call(p.querySelectorAll('input[name=__crsc]'),function(r){ r.addEventListener('change',render); });
    p.querySelector('#__crgrid').addEventListener('click',function(ev){
      var b=ev.target.closest('.__crsw'); if(!b) return;
      var it=p.__crlist[+b.getAttribute('data-i')]; if(!it) return;
      p.__crsel=it;
      p.querySelector('#__crpick').style.display='';
      p.querySelector('#__crfrom').style.background=it.c;
      p.querySelector('#__crmsg').textContent=it.c+' を '+it.n+'箇所で使っています';
    });
    p.querySelector('#__crbgt').textContent='対象：'+label(here)+'（Shift+ダブルクリックした場所）';
    p.querySelector('#__crbg').addEventListener('click',function(){
      try{ pushUndo(here); }catch(_){}
      here.style.setProperty('background-color', p.querySelector('#__crbgc').value, 'important');
      here.style.setProperty('background-image','none','important');   // 画像やグラデが乗っていると色が見えないので外す
      try{ markDirty(); }catch(_){}
      p.querySelector('#__crmsg2').textContent='✅ '+label(here)+' に背景色を付けました（⟲戻す・💾保存で確定）';
      if(msg) msg.textContent='🎨 '+label(here)+' に背景色を付けました。💾保存で確定してください';
      render();
    });
    p.querySelector('#__crgo').addEventListener('click',function(){
      var it=p.__crsel; if(!it){ return; }
      var n=crApply(it.c, p.querySelector('#__crto').value, p.__crroot);
      p.querySelector('#__crmsg').textContent=n?('✅ '+n+'個の要素を塗り替えました（⟲戻す・💾保存で確定）'):'該当が見つかりませんでした';
      if(msg) msg.textContent='🎨 色を置き換えました（'+n+'個）。💾保存で確定してください';
      render();
    });
  }
  // ⇕ 押した場所の「縦の空間」を広げる／狭める（2026-07-30・AIなし）
  //   ★margin/padding を足す方式は採らない：カンプは position:absolute が多くて効かない事があり、
  //     クローン元CSSの !important にも負ける。空のdivを1枚差し込むのが一番確実で、消すのも簡単。
  //   ★差し込む位置は「押したYがどの隙間に入るか」を実測して決める＝決め打ちしないのでどのカンプでも効く。
  //   ★中身が全部 position:absolute の所（FV等）は"隙間"の概念が無いので、
  //     囲っているセクションの min-height を伸ばす方式へ自動で切り替える。
  function _flowKids(el){
    if(!el||!el.children) return [];
    return [].slice.call(el.children).filter(function(c){
      if(c.closest&&c.closest('[id^="__ce"]')) return false;
      if(c.tagName==='SCRIPT'||c.tagName==='STYLE'||c.tagName==='BR') return false;
      var cs=null; try{ cs=getComputedStyle(c); }catch(_){ return false; }
      if(!cs||cs.position==='absolute'||cs.position==='fixed'||cs.display==='none') return false;
      return c.getBoundingClientRect().height>0;
    });
  }
  function _spacerHost(el){
    var cur=el;
    while(cur&&cur!==document.body){
      var cs=null; try{ cs=getComputedStyle(cur); }catch(_){}
      if(cs&&cs.position!=='absolute'&&cs.position!=='fixed'&&_flowKids(cur).length>0) return cur;
      cur=cur.parentElement;
    }
    return _flowKids(document.body).length>0?document.body:null;
  }
  function openSpacer(el,y){
    var oldp=document.getElementById('__ce_vsp'); if(oldp) oldp.remove();
    var host=_spacerHost(el||document.body);
    var mode='spacer', sp=null, secEl=null, where='';
    if(host){
      var kids=_flowKids(host), before=null;
      for(var i=0;i<kids.length;i++){
        var r=kids[i].getBoundingClientRect();
        if(y < r.top + r.height/2){ before=kids[i]; break; }
      }
      // 直前に作った空間が隣にあればそれを伸ばす（押すたびに空divが増えるのを防ぐ）
      var nb=before?before.previousElementSibling:host.lastElementChild;
      if(nb&&nb.getAttribute&&nb.getAttribute('data-cespacer')){ sp=nb; }
      else{
        sp=document.createElement('div');
        sp.className='ce_spacer'; sp.setAttribute('data-cespacer','1');
        sp.style.setProperty('height','40px');
        sp.style.setProperty('width','100%');
        sp.style.setProperty('flex','0 0 auto');   // flexの親でも潰れないように
        try{ pushUndo(host); }catch(_){}
        if(before) host.insertBefore(sp,before); else host.appendChild(sp);
      }
      where=host.tagName.toLowerCase()+(host.className&&typeof host.className==='string'?('.'+host.className.trim().split(/\\s+/)[0]):'')
        +(before?'（'+before.tagName.toLowerCase()+' の上）':'（いちばん下）');
    }else{
      mode='section';
      secEl=(el&&el.closest)?el.closest('section,header,footer,main'):null;
      if(!secEl){ if(msg) msg.textContent='ここには縦の空間を作れませんでした（セクションの中で試してください）'; return; }
      where=secEl.tagName.toLowerCase()+' の高さを伸ばす（中身が自由配置なので隙間を作れないため）';
    }
    function nowPx(){
      if(mode==='spacer') return Math.round(parseFloat(sp.style.height)||0);
      var cs=getComputedStyle(secEl);
      return Math.round(parseFloat(cs.minHeight)||secEl.offsetHeight||0);
    }
    var p=document.createElement('div'); p.id='__ce_vsp';
    function setPx(v){
      v=Math.max(0,Math.round(v));
      if(mode==='spacer'){ sp.style.setProperty('height',v+'px'); }
      else{ try{ pushUndo(secEl); }catch(_){} secEl.style.setProperty('min-height',v+'px','important'); }
      try{ markDirty(); }catch(_){}
      var f=p.querySelector('#__vspnum'); if(f) f.textContent=nowPx()+'px';
    }
    p.setAttribute('style','position:fixed;left:50%;transform:translateX(-50%);bottom:78px;z-index:2147483646;background:#fff;border:1px solid #d0d0d5;border-radius:12px;box-shadow:0 14px 40px rgba(0,0,0,.26);padding:9px 12px;font:12.5px/1.6 system-ui,sans-serif;color:#1d1d1f;max-width:94vw');
    p.innerHTML='<div style="display:flex;align-items:center;gap:8px;margin-bottom:5px">'
      +'<b>⇕ 縦の空間を広げる</b><span id="__vspnum" style="color:#0b6bcb;font-weight:700"></span>'
      +'<button id="__vspx" style="margin-left:auto;border:none;background:#eee;border-radius:999px;padding:1px 9px;cursor:pointer">×</button></div>'
      +'<div style="font-size:11px;color:#777;margin-bottom:6px">'+esc(where)+'</div>'
      +'<div style="display:flex;gap:5px;align-items:center">'
      +'<button class="__vspb" data-d="-40" style="border:1px solid #ddd;background:#f7f7f9;border-radius:7px;padding:5px 9px;cursor:pointer">−40</button>'
      +'<button class="__vspb" data-d="-10" style="border:1px solid #ddd;background:#f7f7f9;border-radius:7px;padding:5px 9px;cursor:pointer">−10</button>'
      +'<button class="__vspb" data-d="10" style="border:1px solid #ddd;background:#f7f7f9;border-radius:7px;padding:5px 9px;cursor:pointer">＋10</button>'
      +'<button class="__vspb" data-d="40" style="border:1px solid #ddd;background:#f7f7f9;border-radius:7px;padding:5px 9px;cursor:pointer">＋40</button>'
      +'<span id="__vspdrag" title="上下にドラッグで調整" style="cursor:ns-resize;background:#0b6bcb;color:#fff;border-radius:7px;padding:5px 12px;user-select:none">⇕ ドラッグ</span>'
      +(mode==='spacer'?'<button id="__vspdel" style="border:1px solid #f0c0c0;background:#fdeeee;color:#b03636;border-radius:7px;padding:5px 9px;cursor:pointer">消す</button>':'')
      +'</div>';
    document.body.appendChild(p);
    setPx(nowPx());
    p.querySelector('#__vspx').addEventListener('click',function(){ p.remove(); });
    [].forEach.call(p.querySelectorAll('.__vspb'),function(b){
      b.addEventListener('click',function(){ setPx(nowPx()+(+b.getAttribute('data-d'))); });
    });
    var dl=p.querySelector('#__vspdel');
    if(dl) dl.addEventListener('click',function(){
      try{ pushUndo(sp.parentElement); }catch(_){}
      sp.remove(); try{ markDirty(); }catch(_){}
      p.remove(); if(msg) msg.textContent='⇕ 作った縦の空間を消しました';
    });
    // ⇕ ドラッグで調整（下へ引くほど広がる）
    (function(){
      var dg=false, sy=0, st=0;
      var g=p.querySelector('#__vspdrag');
      g.addEventListener('mousedown',function(ev){ dg=true; sy=ev.clientY; st=nowPx(); ev.preventDefault(); });
      document.addEventListener('mousemove',function(ev){ if(!dg) return; setPx(st+(ev.clientY-sy)); });
      document.addEventListener('mouseup',function(){ dg=false; });
    })();
    if(msg) msg.textContent='⇕ 縦の空間を作りました（数字ボタンかドラッグで調整・💾保存で確定）';
  }
  // 🔀 お気に入りからセクションを切り替え（プレビューから選ぶ→AIなしで差し替え）
  // 編集バー（①で選ぶ）と右クリックメニュー（右クリック位置）の両方から呼べるよう関数化。
  function favSwapOpen(target){
    if(!target){ msg.textContent='入れ替える先が見つかりません（①で選ぶか、セクションの中で右クリックしてください）'; return; }
    // 同じ種類同士だけ出す（セクションの枠にヘッダーが入る事故を防ぐ）
    var tKind=target.tagName.toLowerCase(); if(tKind!=='header'&&tKind!=='footer') tKind='section';
    // ★rem基準のサイトから保存した古い部品は、この小窓（1rem=16px）では十数倍に膨らみ、
    //   端っこだけが写って「真っ白・細長い帯」に見える（実報告）。中身の実寸を測って、
    //   小窓に収まるよう文字の基準サイズを縮める＝古い部品でも見た目が分かるようにする。
    if(!window.favPvFit) window.favPvFit=function(root){
      [].slice.call(root.querySelectorAll('.pv iframe')).forEach(function(f){
        var fit=function(){
          try{
            var d=f.contentDocument; if(!d||!d.body) return;
            for(var i=0;i<3;i++){
              var w=Math.max(d.body.scrollWidth, d.documentElement.scrollWidth||0);
              if(!(w>1260)) break;
              var cur=parseFloat(getComputedStyle(d.documentElement).fontSize)||16;
              d.documentElement.style.fontSize=Math.max(0.2, cur*1200/w)+'px';
            }
          }catch(_){}
        };
        f.addEventListener('load',fit);
        setTimeout(fit,80); setTimeout(fit,400);
      });
    };
    var tKindJp=(tKind==='section'?'セクション':tKind==='header'?'ヘッダー':'フッター');
    fetch('/api/section_fav/list').then(function(r){return r.json();}).then(function(d){
      var favs=(d.favs||[]).filter(function(f){ return (f.kind||'section')===tKind; });
      var presets=(tKind==='header')?presetHeaders(target):[];   // 🧢標準ヘッダー（⭐が無くても選べる）
      var items = favs.length
        ? favs.map(function(f){
            // ★プレビューはJSを動かさないので、スクロール表示待ち(opacity:0)のままだと空に見える。
            //   だからプレビュー内は全部見える状態に強制する（本物の入れ替え先はJSで正しく出るので無関係）。
            var doc='<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0;background:#fff}'+(f.css||'')+' *,*::before,*::after{opacity:1 !important;visibility:visible !important;filter:none !important;clip-path:none !important;animation:none !important;transition:none !important}</style></head><body>'+f.html+'</body></html>';
            return '<div class="sit" data-id="'+f.id+'"><div class="pv"><iframe sandbox="allow-same-origin" srcdoc="'+esc(doc)+'"></iframe></div><div class="nm">'+esc(f.name||'')+'</div><button class="del" data-id="'+f.id+'" title="削除">×</button></div>';
          }).join('')
        : (presets.length ? '' : '<div style="color:#999;padding:8px">まだ'+tKindJp+'のお気に入りがありません（⭐で保存できます）</div>');
      if(presets.length){
        items += '<div style="grid-column:1/-1;font-size:12.5px;font-weight:700;color:#8a5a00;background:#fff3d6;border-radius:6px;padding:6px 10px">🧢 標準ヘッダー（サイト名・メニュー・色はこのページに自動で合わせ済み）</div>'
          + presets.map(function(p,i){
              var doc='<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0;background:#fff}</style></head><body>'+p.html+'</body></html>';
              return '<div class="sit" data-preset="'+i+'"><div class="pv"><iframe sandbox="allow-same-origin" srcdoc="'+esc(doc)+'"></iframe></div><div class="nm">'+esc(p.name)+'</div></div>';
            }).join('');
      }
      var ov=document.createElement('div'); ov.id='__ce_pk';
      ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span><h4>🔀 入れ替える'+tKindJp+'を選ぶ（クリックで差し替え）</h4><div class="secgr">'+items+'</div></div>';
      document.body.appendChild(ov);
      favPvFit(ov);
      ov.addEventListener('click',function(e){
        if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
        var del=e.target.closest('.del');
        if(del){ e.stopPropagation(); var did=del.getAttribute('data-id');
          fetch('/api/section_fav/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:did})}).then(function(){ var c=del.closest('.sit'); if(c) c.remove(); });
          return; }
        var it=e.target.closest('.sit'); if(!it) return;
        // 🧢標準ヘッダーのカード＝プリセットHTMLで差し替え（⭐保存分と同じ流儀）
        if(it.hasAttribute('data-preset')){
          var pp=presets[+it.getAttribute('data-preset')]; if(!pp) return;
          var par2=target.parentElement, ci2=[].indexOf.call(par2.children,target);
          target.outerHTML=pp.html;
          markRevealed(par2.children[ci2]);
          ov.remove(); markDirty();
          msg.textContent='🧢 標準ヘッダー「'+pp.name+'」に入れ替えました。上の「💾 保存」で確定してください';
          return;
        }
        var id=it.getAttribute('data-id');
        var f=(favs||[]).filter(function(x){return x.id===id;})[0]; if(!f) return;
        var par=target.parentElement, ci=[].indexOf.call(par.children,target);
        target.outerHTML=f.html;   // AIなしで丸ごと差し替え
        markRevealed(par.children[ci]);
        ov.remove(); markDirty();
        msg.textContent='🔀 '+tKindJp+'を入れ替えました。上の「💾 保存」で確定してください';
      });
    }).catch(function(){ msg.textContent='お気に入り一覧の取得に失敗しました'; });
  }
  var colRepBtn=document.getElementById('__ce_colrepbtn');
  if(colRepBtn) colRepBtn.addEventListener('click',function(){
    // ①で選んだセクション → 無ければヘッダー → 無ければページ全体、の順で範囲の初期値を決める
    var t=null; try{ t=curSecEl(); }catch(_){}
    if(!t) t=document.querySelector('header')||document.body;
    openColorReplace(t);
  });
  var favListBtn=document.getElementById('__ce_favlist');
  if(favListBtn) favListBtn.addEventListener('click',function(){ favSwapOpen(curSecEl()); });
  // 🧢/🦶 ヘッダー・フッターは「①で範囲を選んでから🔀」の2段構えで、実際に見つけられない事故が起きた
  //   （2026-07-30・検索でも当たらない＝ボタン名に「ヘッダー」の文字が無かった）。
  //   専用ボタンを置いて1クリックで一覧を出す。名前に「ヘッダー」が入るので曖昧検索にも必ず出る。
  [['__ce_hdpick','header','ヘッダー'],['__ce_ftpick','footer','フッター']].forEach(function(t){
    var b=document.getElementById(t[0]);
    if(!b) return;
    b.addEventListener('click',function(){
      var el=document.querySelector(t[1]);
      if(!el && t[1]==='header'){ try{ el=findTopBar(); }catch(_){ } }
      if(!el){ msg.textContent=t[2]+'（ページ'+(t[1]==='header'?'上':'下')+'部の横長の帯）が見つかりません'; return; }
      favSwapOpen(el);
    });
  });
  // 🗑 セクションを削除：一覧から選んで消す（AIなし・行にマウスを載せると本体を赤枠で示す）
  function secDeleteOpen(defEl){
    var parts=[].slice.call(document.querySelectorAll('header,section,footer')).filter(function(x){ return !x.closest('[id^="__ce"]') && !(x.parentElement&&x.parentElement.closest('section')); });
    if(!parts.length){ msg.textContent='削除できるセクションが見つかりません'; return; }
    var n=0;
    var rows=parts.map(function(s,i){
      var lbl, tag=s.tagName;
      if(tag==='HEADER') lbl='🧢 ヘッダー';
      else if(tag==='FOOTER') lbl='🦶 フッター';
      else { n++; var hEl=s.querySelector('h1,h2,h3'); lbl='セクション'+n+'「'+(((hEl&&hEl.textContent)||'').replace(/\\s+/g,' ').trim().slice(0,22)||'見出しなし')+'」'; }
      return '<div class="sit-pos" data-di="'+i+'"'+(s===defEl?' style="background:#ffe9e9"':'')+'>🗑 '+esc(lbl)+'</div>';
    }).join('');
    var ov=document.createElement('div'); ov.id='__ce_pkpos';
    ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkposx">×</span><h4>🗑 削除するセクションを選ぶ（行に載せると赤枠で確認できます）</h4><div class="poslist">'+rows+'</div></div>';
    document.body.appendChild(ov);
    function clearOl(){ parts.forEach(function(p){ p.style.outline=''; }); }
    ov.addEventListener('mouseover',function(e){
      clearOl();
      var it=e.target.closest('.sit-pos'); if(!it) return;
      var p=parts[+it.getAttribute('data-di')]; if(p) p.style.outline='3px solid #e05656';
    });
    ov.addEventListener('click',function(e){
      if(e.target.id==='__ce_pkpos'||e.target.id==='__ce_pkposx'){ clearOl(); ov.remove(); return; }
      var it=e.target.closest('.sit-pos'); if(!it) return;
      var p=parts[+it.getAttribute('data-di')]; if(!p) return;
      if(!confirm('このセクションを削除しますか？\\n（💾保存で確定。保存する前なら開き直せば戻ります）')) return;
      clearOl(); p.remove(); ov.remove(); markDirty();
      msg.textContent='🗑 削除しました。「💾 変更を保存」で確定（保存前なら開き直しで復活します）';
    });
  }
  // 〰 セクションの境目の形（clip-pathで端をけずる・AIなし）。%指定のpolygonなので画面幅が変わっても崩れない。
  // 上端・下端は別々に持てる（data-cecliptop/data-ceclipbot）＝両方かけると1つのpolygonに合成。
  function edgeShapeApply(sec, edge, kind){
    if(edge==='off'){ sec.removeAttribute('data-cecliptop'); sec.removeAttribute('data-ceclipbot'); sec.removeAttribute('data-ceclipamp'); sec.style.removeProperty('clip-path'); markDirty(); return; }
    // 深さ（2026-07-19）：%は対象の高さ基準なので、背の低い帯だと同じ6%でも浅く見える
    // → data-ceclipampで深さを選べるように（形はそのまま作り直す）
    if(edge==='amp'){ sec.setAttribute('data-ceclipamp', kind); }
    else sec.setAttribute(edge==='top'?'data-cecliptop':'data-ceclipbot', kind);
    var tk=sec.getAttribute('data-cecliptop')||'', bk=sec.getAttribute('data-ceclipbot')||'';
    var amp=+(sec.getAttribute('data-ceclipamp'))||6, steps=40, pts=[];
    function yOf(k,x){
      if(k==='slantL') return amp*(1-x/100);
      if(k==='slantR') return amp*x/100;
      if(k==='curve') return amp*Math.sin(Math.PI*x/100);            // ⌒ へこむ（真ん中を深くけずる）
      if(k==='arch') return amp*(1-Math.sin(Math.PI*x/100));         // ◠ ふくらむ（端をけずって真ん中を残す＝カーブの逆）
      if(k==='wave') return amp/2*(1+Math.sin(2*Math.PI*x/100));
      if(k==='wave2') return amp/2*(1-Math.sin(2*Math.PI*x/100));    // 〰 逆波（波の山谷が反対）
      if(k==='zig'){ var tt=(x%25)/25; return amp*(tt<0.5?tt*2:2-tt*2); }  // ⩙ ギザギザ（三角波）
      return 0;
    }
    for(var i=0;i<=steps;i++){ var x=i/steps*100; pts.push(x.toFixed(1)+'% '+yOf(tk,x).toFixed(1)+'%'); }
    for(var j=steps;j>=0;j--){ var x2=j/steps*100; pts.push(x2.toFixed(1)+'% '+(100-yOf(bk,x2)).toFixed(1)+'%'); }
    sec.style.setProperty('clip-path','polygon('+pts.join(',')+')','important');
    markDirty();
  }
  function edgeOpen(sec){
    if(!sec){ msg.textContent='セクションの中で右クリックしてください'; return; }
    var ov=document.createElement('div'); ov.id='__ce_pkpos';
    function row(a,b,l){ return '<div class="sit-pos" data-eg="'+a+'" data-kd="'+b+'">'+l+'</div>'; }
    ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkposx">×</span><h4>〰 境目の形（このセクションの端をけずって直線の境目をなくす・押すたび即反映）</h4><div class="poslist">'
      +'<div style="font-size:11px;color:#888;padding:2px 8px">上端（上のセクションとの境目）</div>'
      +row('top','slantL','＼ 斜め（左が深い）')+row('top','slantR','／ 斜め（右が深い）')+row('top','curve','⌒ カーブ（へこむ）')+row('top','arch','◠ アーチ（ふくらむ）')+row('top','wave','〰 波')+row('top','wave2','〰 逆波')+row('top','zig','⩙ ギザギザ')
      +'<div style="font-size:11px;color:#888;padding:2px 8px">下端（下のセクションとの境目）</div>'
      +row('bot','slantL','＼ 斜め（左が深い）')+row('bot','slantR','／ 斜め（右が深い）')+row('bot','curve','⌒ カーブ（へこむ）')+row('bot','arch','◠ アーチ（ふくらむ）')+row('bot','wave','〰 波')+row('bot','wave2','〰 逆波')+row('bot','zig','⩙ ギザギザ')
      +'<div style="font-size:11px;color:#888;padding:2px 8px">⛰ 深さ（けずる量・形を選んだあとに押す）</div>'
      +row('amp','4','浅め')+row('amp','6','普通（既定）')+row('amp','10','深め')+row('amp','16','もっと深め')
      +row('off','off','⟲ まっすぐに戻す')
      +'</div></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click',function(e){
      if(e.target.id==='__ce_pkpos'||e.target.id==='__ce_pkposx'){ ov.remove(); return; }
      var it=e.target.closest('.sit-pos'); if(!it) return;
      edgeShapeApply(sec, it.getAttribute('data-eg'), it.getAttribute('data-kd'));
      msg.textContent='〰 境目の形を変えました（上端と下端は併用OK・💾保存で確定）';
    });
  }
  // 💬 がやがやの範囲調整：記号が出る枠（上端%・左右%）を動かす＝文字を避けたり外側に広げたり。
  function gayaApplyBox(gd){
    var gt=+(gd.getAttribute('data-gt')||16), gs=+(gd.getAttribute('data-gs')||4);
    gd.style.setProperty('inset', gt+'% '+gs+'% 0 '+gs+'%');
    markDirty();
  }
  function gayaPanel(gd){
    gayaApplyBox(gd);  // 既定＝上から16%下げた範囲（見出しの上に出ないように）
    var ov=document.createElement('div'); ov.id='__ce_pkpos';
    function row(id,l){ return '<div class="sit-pos" data-ga="'+id+'">'+l+'</div>'; }
    ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkposx">×</span><h4>💬 がやがやの範囲（押すたび即反映・記号はこの枠の中に出る）</h4><div class="poslist">'
      +row('up','⬆ 範囲を上へ')+row('down','⬇ 範囲を下へ（文字を避ける）')
      +row('in','↕ 内側に寄せる')+row('out','↔ 外側に広げる')
      +row('reset','⟲ 位置を初期に戻す')+row('off','🚫 がやがやを外す')
      +'</div></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click',function(e){
      if(e.target.id==='__ce_pkpos'||e.target.id==='__ce_pkposx'){ ov.remove(); return; }
      var it=e.target.closest('.sit-pos'); if(!it) return;
      var k=it.getAttribute('data-ga');
      var gt=+(gd.getAttribute('data-gt')||16), gs=+(gd.getAttribute('data-gs')||4);
      if(k==='off'){ gd.remove(); ov.remove(); markDirty(); msg.textContent='💬 がやがやを外しました（💾保存で確定）'; return; }
      if(k==='reset'){ gt=16; gs=4; }
      if(k==='up') gt=Math.max(-20, gt-6);
      if(k==='down') gt=Math.min(70, gt+6);
      if(k==='in') gs=Math.min(30, gs+4);
      if(k==='out') gs=Math.max(-15, gs-4);
      gd.setAttribute('data-gt',gt); gd.setAttribute('data-gs',gs);
      gayaApplyBox(gd);
      msg.textContent='💬 範囲: 上から'+gt+'%・左右'+gs+'%（💾保存で確定）';
    });
  }
  // ➕ お気に入りからセクションを追加（既存を壊さず、選んだ場所に挿入・AIなし）
  var favAddBtn=document.getElementById('__ce_favadd');
  if(favAddBtn) favAddBtn.addEventListener('click',function(){
    var secs=[].slice.call(document.querySelectorAll('section')).filter(function(x){return !x.closest('#__ce');});
    if(!secs.length){ msg.textContent='追加先の目印になるセクションがまだページにありません'; return; }
    // ステップ1：どこに追加するか選ばせる（先頭 or 各セクションの直後）
    var posRows='<div class="sit-pos" data-pos="-1">▲ 一番上（先頭）に追加</div>'
      +secs.map(function(s,i){
        var hEl=s.querySelector('h1,h2,h3');
        var lbl=(hEl?hEl.textContent:('セクション'+(i+1))).replace(/\\s+/g,' ').trim().slice(0,26);
        return '<div class="sit-pos" data-pos="'+i+'">▼ 「'+esc(lbl||('セクション'+(i+1)))+'」の後ろに追加</div>';
      }).join('');
    var ovp=document.createElement('div'); ovp.id='__ce_pkpos';
    ovp.innerHTML='<div class="bx"><span class="cl" id="__ce_pkposx">×</span><h4>➕ どこに追加しますか？</h4><div class="poslist">'+posRows+'</div></div>';
    document.body.appendChild(ovp);
    ovp.addEventListener('click',function(e){
      if(e.target.id==='__ce_pkpos'||e.target.id==='__ce_pkposx'){ ovp.remove(); return; }
      var pit=e.target.closest('.sit-pos'); if(!pit) return;
      var posIdx=Number(pit.getAttribute('data-pos'));
      ovp.remove();
      // ステップ2：追加するセクションのお気に入りを選ばせる（同じ見た目のピッカーを再利用）
      fetch('/api/section_fav/list').then(function(r){return r.json();}).then(function(d){
        // ➕挿入は種類を問わず全部出す（🧢ヘッダー/🦶フッター部品もセクションとして差し込めると便利）。
        // ※🔀入れ替えは従来どおり同じ種類だけ（枠違い事故防止のルールはそちらで維持）。
        var favs=(d.favs||[]);
        var items=favs.length
          ? favs.map(function(f){
              var doc='<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0;background:#fff}'+(f.css||'')+' *,*::before,*::after{opacity:1 !important;visibility:visible !important;filter:none !important;clip-path:none !important;animation:none !important;transition:none !important}</style></head><body>'+f.html+'</body></html>';
              var kmark=(f.kind==='header'?'🧢 ':f.kind==='footer'?'🦶 ':'');
              return '<div class="sit" data-id="'+f.id+'"><div class="pv"><iframe sandbox="allow-same-origin" srcdoc="'+esc(doc)+'"></iframe></div><div class="nm">'+kmark+esc(f.name||'')+'</div><button class="del" data-id="'+f.id+'" title="削除">×</button></div>';
            }).join('')
          : '<div style="color:#999;padding:8px">まだセクションのお気に入りがありません（⭐で保存できます）</div>';
        var ov=document.createElement('div'); ov.id='__ce_pk';
        ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span><h4>➕ 追加するセクションを選ぶ（クリックで挿入）</h4><div class="secgr">'+items+'</div></div>';
        document.body.appendChild(ov);
        if(window.favPvFit) window.favPvFit(ov);
        ov.addEventListener('click',function(e){
          if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
          var del=e.target.closest('.del');
          if(del){ e.stopPropagation(); var did=del.getAttribute('data-id');
            fetch('/api/section_fav/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:did})}).then(function(){ var c=del.closest('.sit'); if(c) c.remove(); });
            return; }
          var it=e.target.closest('.sit'); if(!it) return;
          var id=it.getAttribute('data-id');
          var f=(favs||[]).filter(function(x){return x.id===id;})[0]; if(!f) return;
          var wrap=document.createElement('div'); wrap.innerHTML=f.html;
          var newEl=wrap.firstElementChild; if(!newEl){ ov.remove(); return; }
          if(posIdx<0){ secs[0].parentElement.insertBefore(newEl,secs[0]); }
          else { var anchor=secs[posIdx]; anchor.parentElement.insertBefore(newEl,anchor.nextSibling); }
          markRevealed(newEl);
          ov.remove(); markDirty();
          msg.textContent='➕ セクションを追加しました。上の「💾 保存」で確定してください';
        });
      }).catch(function(){ msg.textContent='お気に入り一覧の取得に失敗しました'; });
    });
  });
  // その場で再生できるアニメ（AIなし）。g=種類(in/loop/char)、dir=動きの向き、sl=調整スライダー。
  // クリックで即プレビュー→スライダーで調整→「付ける」で無料で焼き込む。
  var FX=[
    {k:'fadeup',b:'ふわっと出現',d:'下から浮かぶ',g:'in',dir:'y',sl:[{k:'dist',l:'移動量',min:6,max:140,def:28},{k:'dur',l:'速さ',min:200,max:2200,def:800,u:'ms'}]},
    {k:'fade',b:'スッと出る',d:'フェード',g:'in',sl:[{k:'dur',l:'速さ',min:200,max:2200,def:800,u:'ms'}]},
    {k:'fadedown',b:'上から降りる',d:'上からスライドイン',g:'in',dir:'yd',sl:[{k:'dist',l:'移動量',min:6,max:220,def:36},{k:'dur',l:'速さ',min:200,max:2200,def:800,u:'ms'}]},
    {k:'left',b:'左から',d:'スライドイン',g:'in',dir:'xl',sl:[{k:'dist',l:'移動量',min:10,max:220,def:48},{k:'dur',l:'速さ',min:200,max:2200,def:800,u:'ms'}]},
    {k:'right',b:'右から',d:'スライドイン',g:'in',dir:'xr',sl:[{k:'dist',l:'移動量',min:10,max:220,def:48},{k:'dur',l:'速さ',min:200,max:2200,def:800,u:'ms'}]},
    {k:'zoom',b:'ズームイン',d:'拡大しながら',g:'in',dir:'s',sl:[{k:'scale',l:'開始の大きさ',min:40,max:98,def:86,u:'%'},{k:'dur',l:'速さ',min:200,max:2200,def:800,u:'ms'}]},
    {k:'blur',b:'ぼやけて出現',d:'ブラー→くっきり',g:'in',dir:'bl',sl:[{k:'blur',l:'ぼかし',min:2,max:40,def:14},{k:'dur',l:'速さ',min:200,max:2200,def:900,u:'ms'}]},
    {k:'flip',b:'3Dフリップ',d:'くるっと回転',g:'in',dir:'ry',sl:[{k:'deg',l:'回転角',min:20,max:180,def:90,u:'°'},{k:'dur',l:'速さ',min:200,max:2200,def:800,u:'ms'}]},
    {k:'rise',b:'せり上がり',d:'下からスッと上へ',g:'in',dir:'clip',sl:[{k:'dist',l:'移動量',min:10,max:140,def:40},{k:'dur',l:'速さ',min:200,max:2200,def:900,u:'ms'}]},
    {k:'stagger',b:'一文字ずつ',d:'文字が順に出現',g:'char',sl:[{k:'stag',l:'文字の間隔',min:15,max:150,def:32,u:'ms'},{k:'dist',l:'跳ねる高さ（移動量）',min:0,max:180,def:26},{k:'bnc',l:'跳ね具合（大きいほど飛び跳ねる）',min:0,max:100,def:30},{k:'dur',l:'速さ',min:150,max:900,def:340,u:'ms'}]},
    {k:'typewriter',b:'タイプライター',d:'打ち込み風',g:'char',type:1,sl:[{k:'stag',l:'打つ速さ',min:20,max:200,def:60,u:'ms'}]},
    // にじみ出る：ぼかしを解きながらゆっくり浮かび上がる。1文字の時間(dur)を文字の間隔(stag)より
    // ずっと長くすることで隣の文字と重なり、カタカタせず「染み込む」ように見える（2026-07-30）。
    {k:'soak',b:'にじみ出る',d:'ゆったり染み込むように',g:'char',type:2,sl:[{k:'stag',l:'文字の間隔',min:40,max:400,def:140,u:'ms'},{k:'dur',l:'にじむ速さ（1文字）',min:600,max:3000,def:1600,u:'ms'},{k:'blur',l:'にじみの強さ',min:2,max:24,def:10,u:'px'}]},
    {k:'wave',b:'波打ち',d:'文字が波打つ(ループ)',g:'char',loop:1,sl:[{k:'amp',l:'ゆれ幅',min:4,max:30,def:10},{k:'dur',l:'速さ',min:800,max:3000,def:1600,u:'ms'}]},
    {k:'glow',b:'ネオングロー',d:'光る(ループ)',g:'loop',glow:1,sl:[{k:'dur',l:'速さ',min:600,max:3200,def:1800,u:'ms'}]},
    {k:'pulse',b:'脈打つ',d:'鼓動(ループ)',g:'loop',dir:'ps',sl:[{k:'amp',l:'強さ',min:2,max:20,def:6,u:'%'},{k:'dur',l:'速さ',min:600,max:3000,def:1400,u:'ms'}]},
    {k:'float',b:'ゆらゆら',d:'浮遊(ループ)',g:'loop',dir:'fy',sl:[{k:'amp',l:'ゆれ幅',min:4,max:40,def:12},{k:'dur',l:'速さ',min:1000,max:4000,def:2200,u:'ms'}]},
    {k:'bounce',b:'バウンド',d:'弾む(ループ)',g:'loop',dir:'by',sl:[{k:'amp',l:'高さ',min:6,max:50,def:18},{k:'dur',l:'速さ',min:600,max:2600,def:1200,u:'ms'}]},
    // ▼2026-07-11追加（全部AIなし）。lines=行マスク／wp=カーテンワイプ／fl=ページめくり／cnt=数字カウント
    {k:'lines',b:'行マスク',d:'行ごとに下からせり上がる',g:'lines',sl:[{k:'dur',l:'速さ',min:300,max:1600,def:700,u:'ms'},{k:'stag',l:'行の間隔',min:40,max:400,def:130,u:'ms'}]},
    {k:'wipe',b:'カーテンワイプ',d:'色帯が走って現れる',g:'in',dir:'wp',sl:[{k:'dur',l:'速さ',min:300,max:2000,def:800,u:'ms'}]},
    {k:'curtain',b:'カーテン開き(左から)',d:'左端から幕が開く',g:'in',dir:'cl',sl:[{k:'dur',l:'速さ',min:300,max:2200,def:900,u:'ms'}]},
    {k:'curtainc',b:'カーテン開き(真ん中)',d:'真ん中から左右へ開く',g:'in',dir:'cc',sl:[{k:'dur',l:'速さ',min:300,max:2200,def:900,u:'ms'}]},
    {k:'pageflip',b:'📖 ページめくり',d:'本をめくるように現れる',g:'in',dir:'fl',sl:[{k:'deg',l:'めくれ角度',min:40,max:120,def:80,u:'°'},{k:'dur',l:'速さ',min:300,max:2200,def:900,u:'ms'}]},
    {k:'count',b:'🔢 カウントアップ',d:'数字が0から増えて止まる',g:'cnt',sl:[{k:'dur',l:'速さ',min:400,max:3000,def:1200,u:'ms'}]}
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
    {n:'横帯リスト型',w:3,i:'カードをやめ、1行1項目の横帯に組み直す。罫線や大きな番号(01/02/03)で区切り、写真は小さなサムネイルとして行の端に置く'},
    {n:'タイムライン型',w:2,i:'カードをやめ、縦のタイムラインに組み直す。左端（またはモバイルは左寄せ）に2pxのブランド色の縦線を1本通し、'
      +'各項目は縦線上の丸い点（直径14px前後・ブランド色・白フチ3px）から横に伸ばして置く。'
      +'項目内は「小さなラベル（STEP 01等・11px英字か日付）＋見出し＋説明文」を縦に積み、項目間はmargin-bottom:56px前後。'
      +'写真がある項目は説明文の下に小さめ（幅60%前後・角丸）で添える。流れ・手順・沿革・1日のスケジュール系の内容に特に合う'},
    {n:'写真下敷き型',w:2,i:'カードをやめ、各項目を「大きな写真の上に文章カードを重ねる」構図に組み直す（1項目=1段で縦に積む・横に並べない）。'
      +'写真は幅85〜100%・高さ320〜420px・角丸・object-fit:cover。文章側は白カード（background:rgba(255,255,255,.94)・'
      +'padding:32〜40px・角丸・柔らかい影）にし、写真の下辺に負マージン-60〜-80pxで重ね、左右どちらかにoffsetして中央揃えにしない'
      +'（段ごとに左右を入れ替えるとリズムが出る）。文字は必ず白カードの上＝写真に直接文字を載せない'}
  ];
  // ★トップ（ヒーロー）専用の型。ユーザーが良例として挙げた過去カンプ2本の実CSSから型化
  //   （camp_20260702_223653=青コラージュ / camp_20260703_003012=るわみ。数値は実物から採取）。
  var HERO_TYPES=[
    {n:'縦書きヒーロー',w:3,i:'2カラムgrid（コピー側minmax(330px,460px)／写真側1fr・align-items:center・min-height:min(100vh,860px)）。'
      +'キャッチコピーはwriting-mode:vertical-rlの縦書き大見出し（clamp(40px,5vw,60px)・font-weight:800）にし、'
      +'半透明の紙カード（background:rgba(255,255,255,.85)・角丸・ブランド色の柔らかい影）に載せる。'
      +'写真は大きな角丸で反対側に置き、写真の角に小さなピル型バッジ（白地・11〜12pxの英字1〜2語・例 WARM SUPPORT）を1〜2個重ねる。'
      +'背景はブランド色の極薄グラデにし、::before/::afterで白薄の円や角丸枠を1〜2個浮かせる（pointer-events:none・z-index:0・文字より背面）'},
    {n:'コラージュヒーロー',w:2,i:'2カラムgrid（写真側1.2fr／コピー側.8fr・align-items:center）。'
      +'写真側は画面の約半分を使い、大きなメイン写真1枚（写真側の85〜95%幅・高さ60vh級・角丸18px）を主役にする。'
      +'そこにサイズの違う小写真を1〜2枚だけ（メインの1/3〜1/4幅・border:3px solid #fff・角丸10px・柔らかい影・'
      +'rotate(-3deg)〜rotate(3deg)）、メインの角に負マージン-30〜-50pxで斜めに重ねる（この大小の重なりが命）。'
      +'★写真を同じ大きさ・同じ形で横に並べるのは失格（正方形3枚が1行に等間隔で並ぶ構図は最頻の失敗例）。'
      +'コピー側は、writing-mode:vertical-rlの縦書きキャッチ（56px級・font-weight:800・薄い白のtext-shadow）＋'
      +'小さな英字キッカー（11px・letter-spacing:.18em）＋本文2〜3行＋11px英字2行組の小ラベルを3個flexで並べる。'
      +'背景は白か極薄のブランド色。箱を等間隔に整列させない'},
    {n:'大見出し2カラム型',w:3,i:'左＝小さな英字キッカー（11px・letter-spacing:.18em）＋横書きの特大見出し'
      +'（clamp(44px,6vw,72px)・ブランド色・font-weight:800・2〜3行）＋本文3行前後＋ボタン2つ（塗り＋枠線の2種）。'
      +'右＝大きな写真1枚を、縦長の角丸パネル2〜3本（ブランド色の極薄・幅違い）のリズムの上に少しだけ重ねて置き、'
      +'右端に短い縦書きの一言（writing-mode:vertical-rl・小さめ）を添える。'
      +'見出しの背後にブランド色系の極薄グラデーション円を1つ大きく敷く（pointer-events:none・文字より背面）'},
    {n:'全幅写真ヒーロー',w:3,i:'写真1枚を全幅・高さmin(100vh,820px)で敷く（object-fit:cover）。'
      +'写真の上に下から上への暗色グラデーションのスクリム（linear-gradient(to top, rgba(0,0,0,.55), rgba(0,0,0,0) 55%)）を重ね、'
      +'キャッチコピーは左下に白の特大文字（clamp(36px,5vw,64px)・font-weight:800・line-height:1.4・text-shadow:0 2px 24px rgba(0,0,0,.35)）、'
      +'その上に小さな英字キッカー（11px・letter-spacing:.2em・白85%）、下に本文1〜2行とボタン1つ。要素は左下の1箇所に集約し四隅に散らさない。'
      +'セクション最下辺に半透明白（rgba(255,255,255,.92)）の細い情報帯（高さ64px前後・営業時間/電話/お知らせ等の既存テキストを横1行で）を敷くと実務感が出る。'
      +'★文字は必ずスクリムの濃い側に置く（薄い部分に白文字を置かない）'},
    {n:'タイポ主役ヒーロー',w:2,i:'写真より文字を主役にする型。キャッチコピーをclamp(56px,9vw,110px)の特大サイズ・font-weight:900・'
      +'line-height:1.25で左上から大きく置き、1単語か1フレーズだけ色替えか-webkit-text-stroke:2px（中抜き文字）でアクセントにする。'
      +'本文と小さなボタンはその下に小さく添える。写真は右下に1枚だけ小さめ（画面の25〜35%幅・角丸・rotate(2deg)前後・柔らかい影）に置き、'
      +'あえて特大文字の端に少しだけ（40px程度）重ねる。背景は白か極薄ティント一色にし、'
      +'::beforeでブランド色の大きな円か帯を1つだけ文字の背面に敷く（pointer-events:none）。'
      +'余白をたっぷり取り、要素は「特大文字・本文＋ボタン・写真1枚」の3つだけに絞る'},
    {n:'アーチ写真ヒーロー',w:2,i:'2カラムgrid（コピー側1fr／写真側1fr・align-items:center）。'
      +'写真はアーチ型（border-radius:50% 50% 12px 12px / 42% 42% 12px 12px・高さ520px前後・object-fit:cover）に切り抜いて置く。'
      +'アーチの背面に同じアーチ形の輪郭線（border:2px solid ブランド色・右下に12pxずらす・pointer-events:none）を1つ重ねると奥行きが出る。'
      +'コピー側は小さな丸ピルのラベル（ブランド色の極薄地）＋大見出し（clamp(36px,4.5vw,56px)・行間1.5）＋本文＋ボタン。'
      +'背景は生成りか極薄ティントにし、小さな飾り（直径8〜16pxの円・十字・葉形）を2〜3個だけ余白に散らす（多用禁止）。'
      +'丸み主体なので保育・美容・医療・花などの柔らかい業種に特に合う'}
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
       +'(う2)写真が複数あるときは**同じ大きさで並べない**。大1枚を主役に、残りはサイズ違いで'
       +'角に少し重ねる（大:小=3:1程度）。同サイズの写真が2枚以上横に並んだ時点で失格。'
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
    // ヘッダー/フッターも①から選べるようにする（⭐部品保存・🔀入れ替え用。AI修正は対象外＝submitでガード）
    [['header','hd','🧢 ヘッダー（⭐保存・🔀入れ替え用）'],['footer','ft','🦶 フッター（⭐保存・🔀入れ替え用）']].forEach(function(t){
      var el=[].slice.call(document.querySelectorAll(t[0])).filter(function(x){return !x.closest('#__ce');})[0];
      if(el){ var o=document.createElement('option'); o.value=t[1]; o.textContent=t[2]; sec.appendChild(o); }
    });
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
  // ※PRESETSの適用ボタンは編集バーから廃止（右クリック大メニューの「🎨背景・特殊」に統一・2026-07-11）。
  //   PRESETS配列自体は右クリック側が使うので残す。
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
  function submit(section,instruction,keepText,styleType,engine){
    // ヘッダー/フッター選択はAI修正の対象外（サーバーの差し替えは<section>限定の正規表現のため）
    if(section==='hd'||section==='ft'){ msg.textContent='ヘッダー/フッターはAI修正の対象外です（⭐で部品として保存→🔀で別カンプと入れ替えできます）'; return; }
    if(!instruction){msg.textContent='指示が空です';return;}
    // ページ全体(-1)は"全文を書き直す"＝高い(数十円)・遅い。特定箇所なら安い(数円)。
    if(Number(section)<0){
      if(!confirm('⚠ これは「ページ全体を書き直す」修正です。\\n時間がかかり、料金も高め（数十円〜）になります。\\n\\n特定の場所だけ直すなら【キャンセル】して、\\n・①で直すセクションを選ぶ か\\n・直したい所を右クリック\\nすると安く（数円）速く直せます。\\n\\nこのままページ全体を直しますか？')) { msg.textContent='キャンセルしました（①でセクションを選ぶと安いです）'; return; }
    }
    // 🧐指摘の記憶の引っ越しメモ：AI修正は「新しい版ファイル」を作るので、完了後に
    // 新ファイル側へ指摘の記憶をコピーするための出発点を残す（受け取りは読み込み時の_mvブロック）
    try{ localStorage.setItem('__ce_dcq_mv', JSON.stringify({from:FILE, ts:Date.now()})); }catch(_){}
    busy(true); msg.textContent='今の状態を保存中…'; showToast('AIが直しています…（十数秒〜）'); markSectionBusy(section);
    // ★AIに渡す前に、今の見た目（移動・手修正・焼き込みアニメ）をディスクへ保存する。
    //   AIはファイルを読んで直すので、保存しないと「以前の状態」に対してかかり手修正が戻ってしまう。
    flushThen(function(){
      msg.textContent='生成中…';
      fetch('/api/edit_camp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,section:Number(section),instruction:instruction,keep_text:keepText?1:0,style_type:styleType||'',engine:engine||'',ref_image:window.__ceRefImg||''})})
      .then(function(r){return r.json();}).then(function(d){
        if(!d.ok){msg.textContent='失敗：'+d.message;busy(false);hideToast();clearSectionBusy();return;}
        if(window.__ceRefImg&&window.__ceRefImgClear) window.__ceRefImgClear();  // 📷見本は1回使ったら外す（毎回の課金・誤爆防止）
        poll(d.job_id);
      }).catch(function(){msg.textContent='通信エラー';busy(false);hideToast();clearSectionBusy();});
    });
  }
  // 📷 見本画像つき修正：スクショを選ぶと1400pxに縮小してbase64で控える。次の「直す」1回だけに使う
  (function(){
    var btn=document.getElementById('__ce_refimg_btn'), fi=document.getElementById('__ce_refimg_file'),
        th=document.getElementById('__ce_refimg_thumb'), x=document.getElementById('__ce_refimg_x');
    if(!btn||!fi) return;
    window.__ceRefImg='';
    window.__ceRefImgClear=function(){ window.__ceRefImg=''; th.style.display='none'; x.style.display='none'; };
    btn.addEventListener('click',function(){ fi.click(); });
    x.addEventListener('click',function(){ window.__ceRefImgClear(); if(msg) msg.textContent='📷 見本画像を外しました'; });
    fi.addEventListener('change',function(){
      var f=fi.files&&fi.files[0]; fi.value=''; if(!f) return;
      var img=new Image();
      img.onload=function(){
        var w=img.width,h=img.height,mx=1400;
        if(w>mx){ h=Math.round(h*mx/w); w=mx; }
        var cv=document.createElement('canvas'); cv.width=w; cv.height=h;
        cv.getContext('2d').drawImage(img,0,0,w,h);
        window.__ceRefImg=cv.toDataURL('image/jpeg',0.85);
        th.src=window.__ceRefImg; th.style.display=''; x.style.display='';
        if(msg) msg.textContent='📷 見本を付けました。①で場所を選び、✍指示（例：この見本の構図に寄せて）→「直す」';
      };
      img.src=URL.createObjectURL(f);
    });
  })();
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
  // ===== 🌙 自動磨き：🧐指摘→🔧修正を全セクションに自動で数周（回数指定・見積もり確認つき） =====
  // 周回数はサーバー側で上限クランプ（DESIGN_STOCK_BRUSHUP_MAX_ROUNDS・既定3）＝暴走しない。
  // 元カンプは無傷＝磨きは新しい作業ファイル1つに上書きされ、完了したらそれを同じタブで開く。
  function brushOpen(defIdx){
    fetch('/api/brushup_estimate?file='+encodeURIComponent(FILE)+'&rounds=2')
    .then(function(x){return x.json();}).then(function(d){
      if(!d.ok){ alert('見積もりできません：'+(d.message||'')); return; }
      var r=prompt('🌙 自動磨き＝AIが「指摘→修正」を自動で回します。\\n何周磨きますか？（1〜'+d.max_rounds+'・おすすめ2）\\n※上限'+d.max_rounds+'周で必ず止まります（暴走しません）','2');
      if(r===null) return;
      var rounds=Math.max(1,Math.min(parseInt(r,10)||2,d.max_rounds));
      // 「5」と言われても分からないので、番号と見出しの対応表を作って一緒に見せる
      var secEls=[].slice.call(document.querySelectorAll('section')).filter(function(x){return !x.closest('#__ce');});
      function secLabel(i){
        var s=secEls[i]; if(!s) return 'セクション'+(i+1);
        var h=s.querySelector('h1,h2,h3'); var t=(h?h.textContent:'').replace(/\\s+/g,' ').trim();
        return t?t.slice(0,16):('セクション'+(i+1));
      }
      var lines=[]; for(var i=0;i<d.sections;i++){ lines.push((i+1)+': '+secLabel(i)); }
      // 磨くセクションの絞り込み。右クリックしたセクションを既定値に入れておく（そこだけ磨くのが一番多い使い方）
      var defSec=(typeof defIdx==='number'&&defIdx>=0)?(defIdx+1):0;
      var sIn=prompt('磨くセクション番号（カンマ区切り可／空欄＝全部）\\n\\n'+lines.join('\\n'),defSec>0?String(defSec):'');
      if(sIn===null) return;
      var secs=(sIn||'').split(',').map(function(s){return parseInt(s.trim(),10);}).filter(function(v){return v>=1&&v<=d.sections;});
      var cnt=secs.length||d.sections;
      var yen=Math.round(d.per_sec_yen*cnt*rounds);
      var engTxt='指摘: '+d.adv_engine+' ／ 修正: '+d.fix_engine+((d.adv_engine==='codex'||d.fix_engine==='codex')?'（codex＝ChatGPT定額枠・追加0円）':'');
      var tgtTxt=secs.length?secs.map(function(n){return n+': '+secLabel(n-1);}).join('、'):('全'+d.sections+'セクション');
      if(!confirm('🌙 見積もり\\n・対象: '+tgtTxt+'\\n・周回数: '+rounds+'\\n・エンジン: '+engTxt+'\\n・予想料金: 約'+yen+'円（過去の実測から自動計算）\\n\\n元のカンプは無傷で残り、磨き版は別ファイル1つに上書きされていきます。\\n時間は1セクション1周あたり1〜2分くらいかかります。実行しますか？')) return;
      showToast('🌙 自動磨きを開始しています…');
      flushThen(function(){
        fetch('/api/auto_brushup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE,rounds:rounds,secs:secs})})
        .then(function(x){return x.json();}).then(function(d2){
          if(!d2.ok){ hideToast(); alert('開始できませんでした：'+(d2.message||'')); return; }
          poll(d2.job_id);
        }).catch(function(){ hideToast(); alert('通信エラー'); });
      });
    }).catch(function(){ alert('通信エラー（サーバーは起動していますか？）'); });
  }
  go.addEventListener('click',function(){submit(sec.value,inp.value.trim());});
  sg.addEventListener('click',function(){
    if(sec.value==='hd'||sec.value==='ft'){ msg.textContent='ヘッダー/フッターはAI改善案の対象外です（⭐部品保存・🔀入れ替えで使えます）'; return; }
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
  // ※「画像差し替えモード」は編集バーから廃止（右クリック→「🖼 この画像を差し替え」に統一・2026-07-11）。
  //   openPickerは右クリック側が使うので残す。
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
      attachPickerSearch(ov);
      ov.addEventListener('click',function(e){
        if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
        var it=e.target.closest('.it'); if(!it) return;
        if(!cand||!cand.el){ msg.textContent='対象が見つかりません'; ov.remove(); return; }
        ov.remove(); msg.textContent='差し替え中…';
        var url=it.dataset.src;
        // 背景画像でも<img>でも、ブラウザ側で差し替えて位置・角度ごと保存（角度が戻らない）
        if(cand.type==='bg'){
          cand.el.style.setProperty('background-image','url("'+url+'")','important');
          // ★背景は既定でタイル（繰り返し）。元が小さな模様だと写真に替えた瞬間、同じ写真が
          //   下へ延々と並ぶ（body差し替えで実際に発生）。写真を敷き詰めたい場面は無いので必ず止める。
          cand.el.style.setProperty('background-repeat','no-repeat','important');
          var _bsz=''; try{ _bsz=getComputedStyle(cand.el).backgroundSize; }catch(_){}
          if(!_bsz||_bsz==='auto'||_bsz==='auto auto'){       // 元が原寸タイル＝写真だと小さすぎるので画面に合わせる
            cand.el.style.setProperty('background-size','cover','important');
            cand.el.style.setProperty('background-position','center','important');
          }
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
  // 👉 「今つかんでいるもの」を画面に出す（枠＋名前＋背景画像のサムネ）。
  //   背景や疑似要素は見た目と持ち主がズレるので、どれを操作しているか目で確かめられないと
  //   「青い形を選んだのに真ん中の写真が変わる」事故になる（実報告あり）。
  function grabHintHide(){ var o=document.getElementById('__ce_grab'); if(o) o.remove(); }
  // 疑似要素（::before/::after）が実際に画面のどこに出ているかを計算する。
  //   DOMに実体が無いので getBoundingClientRect が使えず、そのままだと「親の箱」を指してしまい
  //   別のものを掴んでいるように見える（実報告あり）。absolute配置なら親の内側＋top/left から出せる。
  function psRect(el, ps){
    var cs, hr; try{ cs=getComputedStyle(el,ps); hr=el.getBoundingClientRect(); }catch(_){ return null; }
    var w=parseFloat(cs.width), h=parseFloat(cs.height);
    if(!(w>0&&h>0)) return null;
    if(cs.position!=='absolute'&&cs.position!=='fixed') return null;   // 通常フローは位置を割り出せない
    var hs; try{ hs=getComputedStyle(el); }catch(_){ return null; }
    var bl=parseFloat(hs.borderLeftWidth)||0, bt=parseFloat(hs.borderTopWidth)||0;
    var br=parseFloat(hs.borderRightWidth)||0, bb=parseFloat(hs.borderBottomWidth)||0;
    var x, y;
    if(cs.left!=='auto') x=hr.left+bl+(parseFloat(cs.left)||0);
    else if(cs.right!=='auto') x=hr.right-br-(parseFloat(cs.right)||0)-w;
    else x=hr.left+bl;
    if(cs.top!=='auto') y=hr.top+bt+(parseFloat(cs.top)||0);
    else if(cs.bottom!=='auto') y=hr.bottom-bb-(parseFloat(cs.bottom)||0)-h;
    else y=hr.top+bt;
    // ドラッグで動かした分（translate）は top/left に出ないので足す＝枠が飾りに付いてくる
    var tm=/(-?[0-9.]+)px\\s+(-?[0-9.]+)px/.exec(cs.translate||'');
    if(tm){ x+=parseFloat(tm[1]); y+=parseFloat(tm[2]); }
    return {left:x, top:y, width:w, height:h};
  }
  function grabHintShow(el, label, thumb, sub, ps, onDrag){
    grabHintHide();
    if(!el||!el.getBoundingClientRect) return;
    var o=document.createElement('div'); o.id='__ce_grab';
    o.style.cssText='position:fixed;z-index:2147483646;pointer-events:none;border:2px solid #ff3b30;border-radius:4px;box-shadow:0 0 0 9999px rgba(0,0,0,.04)';
    var lb=document.createElement('div');
    // ★クラス名（div.top_main_wrap 等）は素人には意味が無い＝「絵そのもの」を大きめに見せて、
    //   言葉は「背景の絵」「飾りの絵」など見たまま。細かい名前は title（マウスを乗せた時）だけ。
    lb.style.cssText='position:absolute;left:0;top:-52px;display:flex;align-items:center;gap:7px;background:#ff3b30;color:#fff;font:700 12px/1.4 sans-serif;padding:4px 9px 4px 4px;border-radius:8px;white-space:nowrap;max-width:70vw;overflow:hidden;box-shadow:0 3px 10px rgba(0,0,0,.3)';
    lb.innerHTML=(thumb?'<img src="'+esc(thumb)+'" style="width:40px;height:40px;object-fit:contain;background:#fff;border:1px solid rgba(255,255,255,.6);border-radius:5px">':'')
      +'<span>今つかんでいるのはこれ<br><b style="font-size:13px">'+esc(label)+'</b>'+(sub?'<span style="font-weight:400;opacity:.85"> '+esc(sub)+'</span>':'')+'</span>';
    o.appendChild(lb);
    document.body.appendChild(o);
    var place=function(){
      var r=(ps?psRect(el,ps):null)||el.getBoundingClientRect();
      o.style.left=Math.round(r.left)+'px'; o.style.top=Math.round(r.top)+'px';
      o.style.width=Math.max(2,Math.round(r.width))+'px'; o.style.height=Math.max(2,Math.round(r.height))+'px';
      lb.style.top=(r.top<54?'3px':'-52px');
    };
    place();
    o.__place=place;
    // 赤枠そのものをドラッグして動かせるようにする（飾り＝疑似要素はDOMに実体が無く掴めないため、
    // この枠が唯一の取っ手になる）。枠はパネルを開いている間だけ出るので、普段の操作は邪魔しない。
    if(onDrag){
      o.style.pointerEvents='auto'; o.style.cursor='move';
      lb.style.pointerEvents='none';
      o.addEventListener('mousedown',function(e){
        if(e.button!==0) return;
        e.preventDefault(); e.stopPropagation();
        var sx=e.clientX, sy=e.clientY;
        onDrag(0,0,'start');
        var mv=function(e2){ onDrag(e2.clientX-sx, e2.clientY-sy,'move'); place(); };
        var up=function(){ document.removeEventListener('mousemove',mv,true); document.removeEventListener('mouseup',up,true); onDrag(0,0,'end'); };
        document.addEventListener('mousemove',mv,true);
        document.addEventListener('mouseup',up,true);
      },true);
    }
    window.addEventListener('scroll', place, true);
    window.addEventListener('resize', place);
    o.__off=function(){ window.removeEventListener('scroll', place, true); window.removeEventListener('resize', place); };
  }
  // 背景画像の候補を集める（疑似要素 ::before/::after も含む）。
  //   ★見えている「形」の正体が疑似要素なことが多い（例：ロゴ裏の青い形＝.site-logo::before）。
  //   実体が無いのでDOM走査では拾えず、拾わないと一生選べない＝ここで同列に並べる。
  function bgCandsAt(cx, cy, el){
    var out=[], seen=[];
    if(!isFinite(cx)||!isFinite(cy)) return out;
    var add=function(n, ps){
      var cs; try{ cs=ps?getComputedStyle(n,ps):getComputedStyle(n); }catch(_){ return; }
      if(ps&&(cs.content==='none'||!cs.content)) return;
      var bg=cs.backgroundImage||'';
      var mm=bg.match(/url\\(["']?(.*?)["']?\\)/);
      if(!mm||!mm[1]||mm[1].indexOf('data:')===0) return;
      if(ps&&!(parseFloat(cs.width)>0&&parseFloat(cs.height)>0)) return;
      var key=n; if(seen.some(function(s){ return s.el===n&&s.ps===ps; })) return;
      seen.push({el:n,ps:ps});
      out.push({el:n, ps:ps||'', type:'bg', url:mm[1]});
    };
    var stack=[];
    try{ stack=[].slice.call(document.elementsFromPoint(cx,cy)); }catch(_){ stack=[]; }
    // 疑似要素は座標判定に出ないので、重なっている要素それぞれの ::before/::after も見る
    stack.forEach(function(n){
      if(!n.closest||n.closest('[id^="__ce"]')) return;
      add(n,null); add(n,'::before'); add(n,'::after');
    });
    return out;
  }
  // 疑似要素の背景は style で直接いじれない＝専用クラス＋CSSルールを書き足して当てる
  var _bgpRules={}, _bgpLoaded=false;
  // ★保存済みHTMLに残っている飾りのCSSを読み戻す。これをしないと、別の飾りを触った瞬間に
  //   <style>を丸ごと書き直して前回の調整が消える。
  function bgpLoad(){
    if(_bgpLoaded) return; _bgpLoaded=true;
    var st=document.getElementById('__ce_bgpcss'); if(!st) return;
    var re=/html body [.]([A-Za-z0-9_-]+)(::before|::after)?[{]([^}]*)[}]/g, m;
    while((m=re.exec(st.textContent||''))){
      var k=m[1]+(m[2]||''), d={};
      (m[3]||'').split(';').forEach(function(s){
        var i=s.indexOf(':'); if(i<1) return;
        d[s.slice(0,i).trim()]=s.slice(i+1).replace('!important','').trim();
      });
      _bgpRules[k]={sel:'.'+m[1]+(m[2]||''), d:d};
    }
  }
  function bgpApply(cand, prop, val){
    bgpLoad();
    // val=null＝「この指定をやめる」＝元のCSSの値に戻る（revertと書くと既定値=autoに落ちてしまう）
    if(!cand.ps){ if(val==null) cand.el.style.removeProperty(prop); else cand.el.style.setProperty(prop, val,'important'); return; }
    var cls=cand.el.getAttribute('data-cebgp');
    // ★連番だと危険：保存でクラス名がHTMLに焼き込まれ、開き直すと連番が1から振り直されて
    //   別の飾りに同じ名前が付く＝2つの飾りが一緒に動く（実報告あり）。毎回ユニークな名前にする。
    if(!cls){ if(val==null) return;
      cls='cebgp'+Date.now().toString(36)+Math.floor(Math.random()*1679616).toString(36);
      cand.el.setAttribute('data-cebgp',cls); cand.el.classList.add(cls); }
    var k=cls+cand.ps;
    _bgpRules[k]=_bgpRules[k]||{sel:'.'+cls+cand.ps, d:{}};
    // ★飾りは「絵」なのにクリックを拾う：大きくすると、その面積ぶん親の大きな箱が選ばれてしまい
    //   「全体がグループ化された」ように見える（実報告）。触った飾りはクリックを素通しにする。
    if(val!=null&&!('pointer-events' in _bgpRules[k].d)) _bgpRules[k].d['pointer-events']='none';
    // 同じ要素のもう片方の飾り（::before/::after は2つ持てる）も素通しにする。
    // 触っていない方が上に乗っていると、結局その大きな箱が選ばれてしまうため。
    if(val!=null) ['::before','::after'].forEach(function(ps2){
      if(ps2===cand.ps) return;
      var bi=''; try{ bi=getComputedStyle(cand.el,ps2).backgroundImage; }catch(_){}
      if(!bi||bi==='none') return;
      var k2=cls+ps2;
      _bgpRules[k2]=_bgpRules[k2]||{sel:'.'+cls+ps2, d:{}};
      if(!('pointer-events' in _bgpRules[k2].d)) _bgpRules[k2].d['pointer-events']='none';
    });
    if(val==null){ delete _bgpRules[k].d[prop]; if(!Object.keys(_bgpRules[k].d).length) delete _bgpRules[k]; }
    else _bgpRules[k].d[prop]=val;
    var st=document.getElementById('__ce_bgpcss');
    if(!st){ st=document.createElement('style'); st.id='__ce_bgpcss'; (document.head||document.documentElement).appendChild(st); }
    st.textContent=Object.keys(_bgpRules).map(function(k2){
      var r=_bgpRules[k2];
      return 'html body '+r.sel+'{'+Object.keys(r.d).map(function(p){ return p+':'+r.d[p]+'!important'; }).join(';')+'}';
    }).join('');
  }
  // その要素（と外側4段）が持っている背景の絵を1つ返す。＝「写真を加工」からも背景を扱えるように。
  function bgOfEl(el){
    var t=el;
    for(var i=0;i<4&&t&&t!==document.body&&t.nodeType===1;i++,t=t.parentElement){
      var bi=''; try{ bi=getComputedStyle(t).backgroundImage; }catch(_){}
      var m=bi&&bi.match(/url\\(["']?(.*?)["']?\\)/);
      if(m&&m[1]&&m[1].indexOf('data:')!==0) return {el:t, ps:'', type:'bg', url:m[1]};
    }
    return null;
  }
  // ✂ 背景の絵・飾りを切り抜いて透過にする／もう一度で元に戻す（<img>と同じ /api/remove_bg_url を使う）
  function bgCutToggle(c, btn, after){
    var lab=btn?btn.textContent:'';
    if(c.__cutOrig){
      try{ pushUndo(c.el); }catch(_){}
      bgpApply(c,'background-image','url("'+c.__cutOrig+'")');
      c.url=c.__cutOrig; c.__cutOrig=null;
      try{ if(!c.ps) c.el.removeAttribute('data-cecutbg'); }catch(_){}
      if(btn) btn.textContent='✂ 切り抜いて透過';
      try{ markDirty(); }catch(_){}
      if(after) after();
      if(msg) msg.textContent='切り抜く前の絵に戻しました（💾保存で確定）';
      return;
    }
    if(!c.url){ if(msg) msg.textContent='この絵のURLが取れませんでした'; return; }
    // SVG（図形データ）は写真ではないので切り抜けない。サーバーに投げる前に分かる言葉で止める
    if(/[.]svg([?#]|$)/i.test(c.url)){
      if(msg) msg.textContent='この絵はSVG（図形のデータ）なので切り抜けません。切り抜けるのは写真（jpg/png）だけです';
      return;
    }
    if(btn){ btn.textContent='✂ 切り抜き中…'; btn.disabled=true; btn.style.opacity='.7'; }
    if(msg) msg.textContent='✂ 切り抜き中です（数秒かかります）';
    var src=c.url;
    fetch('/api/remove_bg_url',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({src:src, camp:FILE})}).then(function(r){return r.json();}).then(function(d){
      if(btn){ btn.disabled=false; btn.style.opacity='1'; }
      if(!d||!d.ok){ if(btn) btn.textContent=lab||'✂ 切り抜いて透過'; if(msg) msg.textContent='切り抜きに失敗：'+((d&&d.message)||'不明'); return; }
      try{ pushUndo(c.el); }catch(_){}
      c.__cutOrig=src; c.url=d.url;
      bgpApply(c,'background-image','url("'+d.url+'")');
      // 透明な部分は掴まない目印（＝空いた所を右クリックしても、この大きな箱は選ばれない）
      try{ if(!c.ps){ c.el.setAttribute('data-cecutbg','1'); bgpApply(c,'background-repeat','no-repeat'); } }catch(_){}
      if(btn) btn.textContent='⟲ 切り抜き前に戻す';
      try{ markDirty(); }catch(_){}
      if(after) after();
      if(msg) msg.textContent='✂ 背景を切り抜きました（もう一度押すと元に戻せます・💾保存で確定）';
    }).catch(function(){
      if(btn){ btn.disabled=false; btn.style.opacity='1'; btn.textContent=lab||'✂ 切り抜いて透過'; }
      if(msg) msg.textContent='切り抜きに失敗しました（サーバーに届いていません）';
    });
  }
  function bgpRead(cand, prop){
    try{ return (cand.ps?getComputedStyle(cand.el,cand.ps):getComputedStyle(cand.el))[prop]; }catch(_){ return ''; }
  }
  // 🖼 背景画像の大きさ・位置パネル（AIなし・即反映）。
  //   背景画像は「箱いっぱいに敷いた絵」なので、ツールのサイズ操作（箱を変える）では調整できない。
  //   background-size / background-position を直接いじる専用パネルをここで用意する。
  function openBgSizePanel(list, startIdx){
    var old=document.getElementById('__ce_bgp'); if(old){ if(old.__close) old.__close(); else old.remove(); }
    var idx=(startIdx>0&&startIdx<list.length)?startIdx:0;
    var p=document.createElement('div'); p.id='__ce_bgp';
    p.setAttribute('style','position:fixed;right:14px;top:64px;z-index:2147483647;background:#1d1d2b;color:#fff;border-radius:12px;padding:10px 14px;box-shadow:0 6px 24px rgba(0,0,0,.4);font:12.5px/1.7 sans-serif;width:290px;max-width:96vw');
    // 見たままの言葉で呼ぶ（クラス名は素人に意味が無いので出さない・詳細はマウスを乗せた時だけ）
    var nameOf=function(c){
      var t=c.el.tagName;
      if(t==='BODY'||t==='HTML') return '⚠ ページ全体の背景';
      return c.ps?'飾りの絵':'背景の絵';
    };
    var sizeOf=function(c){
      var r=c.el.getBoundingClientRect();
      if(c.ps){ try{ var cs=getComputedStyle(c.el,c.ps); return Math.round(parseFloat(cs.width))+'×'+Math.round(parseFloat(cs.height)); }catch(_){} }
      return Math.round(r.width)+'×'+Math.round(r.height);
    };
    var techOf=function(c){
      return c.el.tagName.toLowerCase()+((c.el.className&&typeof c.el.className==='string'&&c.el.className.split(' ')[0])?('.'+c.el.className.split(' ')[0]):'')+(c.ps||'');
    };
    var opts=list.map(function(c,i){
      return '<button class="__ce_bgp_t2" data-i="'+i+'" title="'+esc(techOf(c))+'" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:62px;padding:3px;background:#33334a;border:2px solid #4a4a66;border-radius:7px;cursor:pointer;color:#fff;font:11px/1.3 sans-serif">'
        +'<img src="'+esc(c.url)+'" style="width:52px;height:34px;object-fit:contain;background:#fff;border-radius:4px">'
        +'<span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:56px">'+esc(nameOf(c).replace('⚠ ','⚠'))+'</span></button>';
    }).join('');
    var _bs='background:#33334a;color:#fff;border:1px solid #4a4a66;border-radius:6px;padding:3px 9px;cursor:pointer;font-size:11.5px;font-family:inherit';
    p.innerHTML='<b>🖼 背景画像の大きさ・位置</b>'
      +'<span id="__ce_bgpx" style="float:right;cursor:pointer;opacity:.8">×</span>'
      +(list.length>1?('<div style="font-size:10.5px;opacity:.75;margin-top:4px">どれを直す？（クリックで切り替え）</div>'
          +'<div id="__ce_bgp_t" style="display:flex;gap:4px;flex-wrap:wrap;margin-top:3px">'+opts+'</div>'):'')
      +'<div style="margin-top:6px">大きさ <span id="__ce_bgp_v" style="color:#fbbf24;font-weight:700"></span></div>'
      +'<input type="range" id="__ce_bgp_r" min="10" max="300" step="1" style="width:100%;cursor:pointer">'
      +'<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:2px">'
      +'<button class="__ce_bgp_p" data-v="cover" style="'+_bs+'">箱いっぱい(cover)</button>'
      +'<button class="__ce_bgp_p" data-v="contain" style="'+_bs+'">全部見せる(contain)</button>'
      +'<button class="__ce_bgp_p" data-v="auto" style="'+_bs+'">原寸</button></div>'
      +'<div style="margin-top:8px">位置</div>'
      +'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:3px;width:120px">'
      +['left top','center top','right top','left center','center center','right center','left bottom','center bottom','right bottom']
        .map(function(v){ return '<button class="__ce_bgp_g" data-v="'+v+'" title="'+v+'" style="'+_bs+';padding:6px 0">・</button>'; }).join('')
      +'</div>'
      +'<div style="margin-top:8px">前後（重なり） <span id="__ce_bgp_zn" style="color:#fbbf24;font-weight:700"></span></div>'
      +'<div style="font-size:10.5px;opacity:.75;margin-bottom:2px">動くのは<b style="color:#ff6b62">赤枠のもの</b>だけです</div>'
      +'<div style="display:flex;gap:4px"><button id="__ce_bgp_zup" title="赤枠のものを、上に乗っているものより前に出す（触れると邪魔しているものが青枠で光ります）" style="'+_bs+'">⬆ 赤枠を手前に</button>'
      +'<button id="__ce_bgp_zdn" title="赤枠のものを奥へ戻す（手前に出す前の重なりに戻します）" style="'+_bs+'">⬇ 赤枠を奥へ</button></div>'
      +'<div id="__ce_bgp_cv" style="font-size:10.5px;color:#9fd0ff;margin-top:2px;min-height:14px"></div>'
      +'<div style="display:flex;gap:5px;margin-top:8px;flex-wrap:wrap"><button id="__ce_bgp_nr" style="'+_bs+'">繰り返しを止める</button>'
      +'<button id="__ce_bgp_cut" title="人物や商品だけを残して背景を透明にする（このPCの中だけで処理・無料・数秒）" style="'+_bs+';background:#7c3aed;border-color:#7c3aed">✂ 切り抜いて透過</button>'
      +'<button id="__ce_bgp_del" title="この絵を消す（もう一度押すと戻る・箱やレイアウトはそのまま）" style="'+_bs+';background:#7a2b2b;border-color:#9b3d3d">✕ この絵を消す</button>'
      +'<button id="__ce_bgp_rs" style="'+_bs+'">⟲ 元に戻す</button></div>'
      +'<div id="__ce_bgp_tip" style="font-size:10.5px;opacity:.7;margin-top:6px">💾保存で確定／Escで閉じる</div>';
    document.body.appendChild(p);
    var cur=function(){ return list[idx]; };
    var snap=list.map(function(c){ return c.ps?null:(c.el.getAttribute('style')||''); });   // 開いた時点の見た目＝⟲の戻り先
    // 飾り（疑似要素）は赤枠をドラッグ＝そのまま移動できる。translateなので周りのレイアウトは動かない
    var dragBase={x:0,y:0};
    var readTr=function(c){
      var v=''; try{ v=(c.ps?getComputedStyle(c.el,c.ps):getComputedStyle(c.el)).translate||''; }catch(_){}
      var m=/(-?[0-9.]+)px\\s+(-?[0-9.]+)px/.exec(v);
      return m?{x:parseFloat(m[1]), y:parseFloat(m[2])}:{x:0,y:0};
    };
    // 背景の絵（実体のある要素）は箱を動かすとレイアウトが崩れるので、絵だけを箱の中でずらす。
    //   ★元の指定（50% など）に calc で足し引きする＝画像の実寸を測らなくても正確にずれる。
    // ★2回目以降が効かなくなる罠：一度ずらすと値が calc(50% + 80px) になる。これを空白で切ると
    //   "calc(50%" と "+" に割れて、次に書く指定が壊れて何も動かなくなる（実報告）。
    //   カッコの中の空白では切らず、前回のズレ量は取り出して引き継ぐ。
    var splitPos=function(v){
      var out=[], d=0, cur='';
      for(var i=0;i<v.length;i++){
        var ch=v.charAt(i);
        if(ch==='(') d++; else if(ch===')') d--;
        if(ch===' '&&d===0){ if(cur){ out.push(cur); cur=''; } continue; }
        cur+=ch;
      }
      if(cur) out.push(cur);
      return out;
    };
    var unwrapPos=function(s){
      var m=/^calc[(](.+?)\\s*([+-])\\s*(-?[0-9.]+)px[)]$/.exec(s||'');
      return m? {b:m[1].replace(/\\s+$/,''), o:(m[2]==='-'?-1:1)*parseFloat(m[3])} : {b:(s||'0px'), o:0};
    };
    var bpBase=function(c){
      if(c.__bp1==null){
        var v=''; try{ v=getComputedStyle(c.el).backgroundPosition||'0px 0px'; }catch(_){ v='0px 0px'; }
        var t=splitPos(v), a=unwrapPos(t[0]), b=unwrapPos(t[1]);
        c.__bp1=a.b; c.__bp2=b.b; c.__bpx=a.o; c.__bpy=b.o;
      }
      return c;
    };
    var hint=function(){
      var c=cur();
      grabHintShow(c.el, nameOf(c), c.url, sizeOf(c), c.ps, function(dx,dy,ph){
        if(ph==='start'){ dragBase=c.ps?readTr(c):{x:(bpBase(c).__bpx||0), y:(c.__bpy||0)}; try{ pushUndo(c.el); }catch(_){} return; }
        if(ph==='end'){ try{ markDirty(); }catch(_){} if(msg) msg.textContent=(c.ps?'🖼 飾りを動かしました':'🖼 背景の絵をずらしました')+'（💾保存で確定・⟲で戻せます）'; return; }
        if(c.ps){ bgpApply(c,'translate',Math.round(dragBase.x+dx)+'px '+Math.round(dragBase.y+dy)+'px'); return; }
        bpBase(c);
        c.__bpx=Math.round(dragBase.x+dx); c.__bpy=Math.round(dragBase.y+dy);
        bgpApply(c,'background-position','calc('+c.__bp1+' + '+c.__bpx+'px) calc('+c.__bp2+' + '+c.__bpy+'px)');
      });
    };
    var markSel=function(){
      [].slice.call(p.querySelectorAll('.__ce_bgp_t2')).forEach(function(b){
        var on=(+b.getAttribute('data-i')===idx);
        b.style.borderColor=on?'#ff3b30':'#4a4a66'; b.style.background=on?'#4a3040':'#33334a';
      });
    };
    // 飾り（疑似要素）は「箱の大きさ」を基準にする。★背景画像だけ大きくすると箱からはみ出た分が
    //   切れて見えなくなる（実報告）ので、箱と絵を一緒に大きくする。
    var baseOf=function(c){
      if(c.__bw>0) return c;
      try{ var cs=getComputedStyle(c.el,c.ps); c.__bw=parseFloat(cs.width)||0; c.__bh=parseFloat(cs.height)||0; }catch(_){}
      return c;
    };
    var sync=function(){
      var c=cur();
      var v=p.querySelector('#__ce_bgp_v'), r=p.querySelector('#__ce_bgp_r');
      if(c.ps){
        baseOf(c);
        var nowW=0; try{ nowW=parseFloat(getComputedStyle(c.el,c.ps).width)||0; }catch(_){}
        var pct=(c.__bw>0&&nowW>0)?Math.round(nowW/c.__bw*100):100;
        if(v) v.textContent=pct+'%（'+Math.round(nowW)+'px）';
        if(r) r.value=Math.max(10,Math.min(300,pct));
      }else{
        var bs=bgpRead(c,'backgroundSize')||'auto', pc=/^([0-9.]+)%/.exec(bs);
        if(v) v.textContent=bs;
        if(r) r.value=pc?Math.max(10,Math.min(300,Math.round(parseFloat(pc[1])))):100;
      }
      var zn=p.querySelector('#__ce_bgp_zn');
      if(zn){ var zv=bgpRead(c,'zIndex'); zn.textContent='今 '+((zv==null||zv===''||zv==='auto')?'ふつう':zv); }
      hint(); markSel();
      var tip=p.querySelector('#__ce_bgp_tip');
      if(tip) tip.innerHTML='🖱 <b>赤い枠をドラッグ</b>＝'+(c.ps?'飾りを動かす':'絵を箱の中でずらす')+'<br>💾保存で確定／Escで閉じる';
    };
    var apply=function(prop,val){ try{ pushUndo(cur().el); }catch(_){} bgpApply(cur(),prop,val); try{ markDirty(); }catch(_){} sync(); };
    sync();
    // サムネを押して対象を切り替え／触れている間はその対象を赤枠で見せる（押す前に確かめられる）
    [].slice.call(p.querySelectorAll('.__ce_bgp_t2')).forEach(function(b){
      var i=+b.getAttribute('data-i');
      b.addEventListener('mouseenter',function(){ var c=list[i]; grabHintShow(c.el, nameOf(c), c.url, sizeOf(c), c.ps); });
      b.addEventListener('mouseleave',function(){ hint(); });
      b.addEventListener('click',function(){ idx=i; sync(); });
    });
    p.querySelector('#__ce_bgp_r').addEventListener('input',function(){
      var n=+this.value, c=cur(), v=p.querySelector('#__ce_bgp_v');
      if(c.ps){
        baseOf(c);
        var w=Math.round(c.__bw*n/100), h=Math.round(c.__bh*n/100);
        bgpApply(c,'width', w+'px'); bgpApply(c,'height', h+'px');
        bgpApply(c,'background-size','contain');       // 絵も箱に合わせて一緒に大きくなる
        bgpApply(c,'background-repeat','no-repeat');
        if(v) v.textContent=n+'%（'+w+'px）';
      }else{
        bgpApply(c,'background-size', n+'%'); bgpApply(c,'background-repeat','no-repeat');
        if(v) v.textContent=n+'%';
      }
      try{ markDirty(); }catch(_){}
      var o=document.getElementById('__ce_grab'); if(o&&o.__place) o.__place();
    });
    p.querySelector('#__ce_bgp_r').addEventListener('change',function(){ sync(); });   // 離したら表示（寸法）を測り直す
    [].slice.call(p.querySelectorAll('.__ce_bgp_p')).forEach(function(b){
      b.addEventListener('click',function(){ apply('background-size', b.getAttribute('data-v')); });
    });
    [].slice.call(p.querySelectorAll('.__ce_bgp_g')).forEach(function(b){
      b.addEventListener('click',function(){
        var c=cur(); c.__bp1=null; c.__bpx=0; c.__bpy=0;   // 9マスで置き直したらドラッグのずれ量は0から
        apply('background-position', b.getAttribute('data-v'));
      });
    });
    // ⬆⬇ 前後（重なり）：飾りが文字にかぶった時は「奥へ送る」で解決する。
    //   ★飾り（疑似要素）は実体が無く、通常のz-index機能では掴めない＝ここが唯一の操作口になる。
    var zNow=function(){ var n=parseInt(bgpRead(cur(),'zIndex'),10); return isNaN(n)?0:n; };
    var zStep=function(d){
      var c=cur();
      try{ pushUndo(c.el); }catch(_){}
      var n=Math.max(-5,Math.min(50, zNow()+d));
      var pos=bgpRead(c,'position');
      if(pos==='static'||!pos) bgpApply(c,'position','relative');   // z-indexは位置指定が無いと効かない
      bgpApply(c,'z-index', String(n));
      try{ markDirty(); }catch(_){}
      sync();
      if(msg) msg.textContent='重なりを'+(d>0?'手前に':'奥に')+'しました（今 '+n+'）。まだ隠れる時はもう一度押してください';
    };
    // ★数字をいくら上げても前に出ないことがある：相手が別の「重なりの箱」に入っていると、
    //   その中でいくら大きくしても外側の順番には勝てない（実報告：18まで上げても変わらない）。
    //   共通の親までさかのぼって、その直下の枝ごと相手より上に持ち上げるのが正しい直し方。
    // ★中央だけ見ると、角だけ覆われている相手を見逃す（実報告：左上の箱だけ前に残った）。
    //   要素の上を格子状に何点も調べて、1つでも上に乗っている相手が居れば返す。
    var zCoverOf=function(el){
      var r=el.getBoundingClientRect();
      if(!(r.width>2&&r.height>2)) return null;
      var fs=[0.08,0.3,0.5,0.7,0.92];
      for(var a=0;a<fs.length;a++){
        for(var b=0;b<fs.length;b++){
          var x=r.left+r.width*fs[a], y=r.top+r.height*fs[b];
          if(x<2||y<2||x>window.innerWidth-2||y>window.innerHeight-2) continue;
          var ls=[]; try{ ls=document.elementsFromPoint(x,y); }catch(_){ continue; }
          for(var i=0;i<ls.length;i++){
            var n=ls[i];
            if(n===el||el.contains(n)) break;      // 自分に到達＝この点では上に誰も居ない
            if(n.contains(el)) break;              // 親に到達＝同上
            if(_inUI2(n)) continue;                // ツールのUIは無視
            return n;
          }
        }
      }
      return null;
    };
    // ★「手前に出す」は対象そのものではなく、共通の親の直下（枝）に印を付ける。
    //   だから「奥へ送る」で対象の数字を下げても元に戻らない（実報告）。触った枝を控えておき、
    //   奥へ送るときは新しい順に戻す＝押した手順をそのまま巻き戻す。
    var zLifts=[], zPend=[];
    var zLiftOver=function(el, cover){
      var chain=[]; for(var n=el;n;n=n.parentElement) chain.push(n);
      var lca=null; for(var m=cover;m;m=m.parentElement){ if(chain.indexOf(m)>=0){ lca=m; break; } }
      if(!lca) lca=document.body;
      var A=el; while(A&&A.parentElement&&A.parentElement!==lca) A=A.parentElement;
      var B=cover; while(B&&B.parentElement&&B.parentElement!==lca) B=B.parentElement;
      if(!A||!B||A===B) return false;
      try{ pushUndo(A); }catch(_){}
      zPend.push({el:A, z:A.style.getPropertyValue('z-index'), zp:A.style.getPropertyPriority('z-index'),
                   ps:A.style.getPropertyValue('position'), pp:A.style.getPropertyPriority('position')});
      var bz=parseInt(getComputedStyle(B).zIndex,10); if(isNaN(bz)) bz=0;
      if(getComputedStyle(A).position==='static') A.style.setProperty('position','relative','important');
      A.style.setProperty('z-index', String(Math.min(999,bz+1)),'important');
      A.setAttribute('data-cezlift','1');   // 空っぽの場所ではクリックを素通しにする目印
      return true;
    };
    var zFront=function(){
      var c=cur(), el=c.el, done=0;
      try{ pushUndo(el); }catch(_){}
      zPend=[];                                  // この1回で触った枝＝まとめて1手として戻せるようにする
      // 相手が複数（別々の重なりの箱）のことがあるので、上に居るものが無くなるまで繰り返す
      for(var pass=0; pass<4; pass++){
        var cover=zCoverOf(el);
        if(!cover) break;
        if(!zLiftOver(el, cover)) break;
        done++;
      }
      if(!done){ zStep(1); return; }
      if(zPend.length){ zLifts.push(zPend); zPend=[]; }
      try{ markDirty(); }catch(_){}
      sync();
      if(msg) msg.textContent = zCoverOf(el)
        ? '手前に出しましたが、まだ上に何かが残っています（もう一度押してください）'
        : ('手前に出しました（'+done+'つの相手より上になりました）');
    };
    var zRestore=function(o){
      try{ pushUndo(o.el); }catch(_){}
      if(o.z) o.el.style.setProperty('z-index',o.z,o.zp); else o.el.style.removeProperty('z-index');
      if(o.ps) o.el.style.setProperty('position',o.ps,o.pp); else o.el.style.removeProperty('position');
      try{ o.el.removeAttribute('data-cezlift'); }catch(_){}
    };
    // 「何が邪魔しているか」を触れるだけで見せる（青枠）＝手前に出すの意味が分かる
    var cvHide=function(){ var o=document.getElementById('__ce_grab2'); if(o) o.remove();
      var t2=p.querySelector('#__ce_bgp_cv'); if(t2) t2.textContent=''; };
    var cvShow=function(){
      cvHide();
      var cover=zCoverOf(cur().el);
      var t2=p.querySelector('#__ce_bgp_cv');
      if(!cover){ if(t2) t2.textContent='上に乗っているものはありません（もう一番前です）'; return; }
      var r=cover.getBoundingClientRect();
      var o=document.createElement('div'); o.id='__ce_grab2';
      o.style.cssText='position:fixed;z-index:2147483645;pointer-events:none;border:2px dashed #2f9bff;border-radius:4px;'
        +'left:'+Math.round(r.left)+'px;top:'+Math.round(r.top)+'px;width:'+Math.max(2,Math.round(r.width))+'px;height:'+Math.max(2,Math.round(r.height))+'px';
      var lb=document.createElement('div');
      lb.style.cssText='position:absolute;left:0;top:'+(r.top<24?'2px':'-22px')+';background:#2f9bff;color:#fff;font:700 11px/1.8 sans-serif;padding:0 7px;border-radius:5px;white-space:nowrap';
      lb.textContent='これが上に乗っています';
      o.appendChild(lb); document.body.appendChild(o);
      if(t2) t2.textContent='青枠のものより前に出します';
    };
    p.querySelector('#__ce_bgp_zup').addEventListener('mouseenter',cvShow);
    p.querySelector('#__ce_bgp_zup').addEventListener('mouseleave',cvHide);
    p.querySelector('#__ce_bgp_zup').addEventListener('click',function(){ cvHide(); zFront(); });
    p.querySelector('#__ce_bgp_zdn').addEventListener('click',function(){
      if(zLifts.length){                       // 手前に出した1回分（複数の枝）をまとめて取り消す
        var b=zLifts.pop();
        for(var i=b.length-1;i>=0;i--) zRestore(b[i]);
        try{ markDirty(); }catch(_){} sync();
        if(msg) msg.textContent='奥へ戻しました（手前に出す前の重なりに戻っています）';
        return;
      }
      zStep(-1);
    });
    p.querySelector('#__ce_bgp_nr').addEventListener('click',function(){ apply('background-repeat','no-repeat'); });
    // ✂ 背景の絵・飾りも切り抜いて透過にする（<img>と同じ切り抜きを使う。もう一度押すと元の絵に戻る）
    p.querySelector('#__ce_bgp_cut').addEventListener('click',function(){ bgCutToggle(cur(), this, sync); });
    // ✕ この絵を消す（もう一度で戻る）。飾りは丸ごと非表示、背景は絵だけ外す＝箱やレイアウトは動かさない
    p.querySelector('#__ce_bgp_del').addEventListener('click',function(){
      var c=cur();
      try{ pushUndo(c.el); }catch(_){}
      if(c.__hidden){
        if(c.ps) bgpApply(c,'display',null); else bgpApply(c,'background-image',null);
        c.__hidden=false; this.textContent='✕ この絵を消す';
        if(msg) msg.textContent='絵を戻しました（💾保存で確定）';
      }else{
        if(c.ps) bgpApply(c,'display','none'); else bgpApply(c,'background-image','none');
        c.__hidden=true; this.textContent='↩ 消したのを戻す';
        if(msg) msg.textContent='絵を消しました（もう一度押すと戻ります・💾保存で確定・⟲でも戻せます）';
      }
      try{ markDirty(); }catch(_){} sync();
    });
    p.querySelector('#__ce_bgp_rs').addEventListener('click',function(){
      while(zLifts.length){ var _b=zLifts.pop(); for(var _i=_b.length-1;_i>=0;_i--) zRestore(_b[_i]); }   // 手前に出すで触った枝も全部戻す
      var c=cur(); try{ pushUndo(c.el); }catch(_){}
      if(c.ps){ ['background-size','background-position','background-repeat','translate','width','height','pointer-events','background-image','z-index','position','display'].forEach(function(pr){ bgpApply(c,pr,null); }); }
      else if(snap[idx]) c.el.setAttribute('style', snap[idx]); else c.el.removeAttribute('style');
      try{ markDirty(); }catch(_){} sync();
      if(msg) msg.textContent='🖼 背景の見た目を開いた時の状態に戻しました';
    });
    var close=function(){ var o=document.getElementById('__ce_grab'); if(o&&o.__off) o.__off(); grabHintHide();
      var o2=document.getElementById('__ce_grab2'); if(o2) o2.remove(); p.remove(); };
    p.querySelector('#__ce_bgpx').addEventListener('click',close);
    p.__close=close;
    if(msg) msg.textContent='🖼 背景画像の大きさ・位置を調整中（スライダーで大きさ／9つのボタンで位置・Escで閉じる）';
  }
  // 右クリック座標に重なる「差し替えられる画像」を集める：<img> と 背景画像(background-image) の両方。
  // 前面→背面順。★クイックメニューと⚙大メニューの両方から使う（同じ挙動にするため関数に切り出し）。
  function imgCandsAt(cx, cy, el){
    var cands=[];
    // ★座標が壊れていても例外で右クリック全体を道連れにしない（elementsFromPointは非有限値で throw する）
    if(!isFinite(cx)||!isFinite(cy)) return cands;
    function _has(n){ return cands.some(function(c){return c.el===n;}); }
    function _addBg(n){
      var bg=''; try{ bg=getComputedStyle(n).backgroundImage; }catch(_){}
      var mm=bg&&bg.match(/url\\(["']?(.*?)["']?\\)/);
      if(mm && mm[1] && mm[1].indexOf('data:')!==0 && !_has(n)){ cands.push({el:n,type:'bg',url:mm[1]}); }
    }
    document.elementsFromPoint(cx, cy).forEach(function(n){
      if(!n.closest || n.closest('[id^="__ce"]')) return;
      if(n.tagName==='IMG'){ if(!_has(n)) cands.push({el:n,type:'img',url:n.currentSrc||n.src}); return; }
      _addBg(n);
    });
    // pointer-events:none の装飾など、座標検出で拾えない背面の背景画像も、矩形が重なれば候補に足す
    var scope=(el&&el.closest&&el.closest('section'))||document.body;
    [].slice.call(scope.querySelectorAll('*')).forEach(function(n){
      if(n.closest('[id^="__ce"]')) return;
      var r=n.getBoundingClientRect();
      if(!r.width||cx<r.left||cx>r.right||cy<r.top||cy>r.bottom) return;
      if(n.tagName==='IMG'){ if(!_has(n)) cands.push({el:n,type:'img',url:n.currentSrc||n.src}); return; }
      _addBg(n);
    });
    if(!cands.length && el){
      var fb=(el.tagName==='IMG')?[el]:(el.querySelectorAll?[].slice.call(el.querySelectorAll('img')):[]);
      fb.forEach(function(im){cands.push({el:im,type:'img',url:im.currentSrc||im.src});});
    }
    return cands;
  }
  // 重なった画像（img・背景）のうち、どれを差し替えるかをサムネで先に選ばせる
  function pickWhichImg(list){
    // ★body/html＝ページ全体の背景は、選ぶと「ページ全部が変わる」ので名前で分かるようにする
    //   （気づかず選んで、写真がページ全体にタイル表示された事故があった）
    var items=list.map(function(c,i){
      var tg=c.el&&c.el.tagName, whole=(tg==='BODY'||tg==='HTML');
      var nm=whole?'⚠ページ全体の背景':((c.type==='bg'?'背景':'画像')+(i+1)+(i===0?'（前面）':''));
      return '<div class="it" data-i="'+i+'"><img src="'+c.url+'"><span'+(whole?' style="color:#c0392b"':'')+'>'+nm+'</span></div>';
    }).join('');
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
      attachPickerSearch(ov);
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
  // ★「🚫この飾りを消す」で隠した飾りは display:none が残る（保存すればファイルにも残る）。
  //   その状態で色や形を選んでも見えない＝「グラデがつかない」ように見える（実際に起きた）。
  //   飾りを付け直す操作は必ずここを通して、隠した状態を解除する。
  function _unhideDeco(n){
    if(!n) return;
    if(n.getAttribute('data-ceflatvis')!=null){ n.style.removeProperty('display'); n.removeAttribute('data-ceflatvis'); }
    if(n.style.getPropertyValue('display')==='none') n.style.removeProperty('display');
  }
  function bgTarget(el){
    var t = el.tagName==='IMG' ? (el.parentElement||el) : el;   // imgには子を入れられないので親に敷く
    if(!t||t===document.body) return null;
    // どの写真に対する飾りかを覚える（親が大きい時に、写真の箱へ合わせて置くため）
    try{
      t.__ceDecoImg=(el.tagName==='IMG'||el.tagName==='VIDEO')?el
        :((el.querySelector&&el.querySelector('img,video'))||t.querySelector('img,video')||null);
    }catch(_){}
    return t;
  }
  // 飾りパネルを「写真の後ろから斜めにずらして覗かせる」位置に置く（Dpx＝覗く量／dir＝ずらす向き）。
  //   ★旧方式は左右対称(inset:-N%)で、写真が不透明だと真裏に完全に隠れて色が見えなかった
  //     （大きくしても薄い縁が広がるだけ）。同じ大きさのパネルをdir方向へDpxずらし、
  //     その2辺を写真の外へ確実にはみ出させる＝色の帯が必ず見える（デザインの「オフセット色面」）。
  // ★グラデが「効かない」ように見えた原因（2026-07-28 実測で判明）
  //   飾りは写真と同じ大きさをDpxずらして置くだけ＝見えるのは外側のフチ帯だけ。
  //   なのに色は「中心が濃い→外へいくほど白」の放射グラデだったので、
  //   覗いている帯の色は実測 #fbeef4（ほぼ白）。色を選んでも変わらないのは当然だった。
  //   → ①ずらす向きの角へ濃い色が来る線形グラデにする ②「☀後光」＝全周にふんわり広げる形を足す。
  var BG_ANG={br:'135deg',bl:'225deg',tr:'45deg',tl:'315deg'};
  function _bgGradCss(cols, dir, mode){
    if(!cols||cols.length<3) return '';
    if(mode==='halo')   // 後光＝中心は写真の裏なので、外に出る 50〜82% の位置に色を置く
      return 'radial-gradient(62% 62% at 50% 50%, '+cols[2]+' 0%, '+cols[1]+' 52%, rgba(255,255,255,0) 82%)';
    return 'linear-gradient('+(BG_ANG[dir]||BG_ANG.br)+', '+cols[0]+' 0%, '+cols[1]+' 42%, '+cols[2]+' 100%)';
  }
  // ★この改修より前に作った飾りは色を持っていない（背景の文字列しか無い）。
  //   そのまま形だけ変えると、また"外側が白い"古いグラデのままで色が出ない＝
  //   今のグラデから色を読み取って、明るい順に 薄い/中間/濃い として引き継ぐ。
  function _bgColsFrom(bg){
    var s='';
    try{ s=getComputedStyle(bg).backgroundImage||''; }catch(_){ }
    var re=/rgba?\(([^)]+)\)/g, m, list=[];
    while((m=re.exec(s))){
      var v=m[1].split(',').map(parseFloat);
      if(v.length>3&&v[3]<0.05) continue;                       // 透明は色として数えない
      var lum=0.299*v[0]+0.587*v[1]+0.114*v[2];
      if(!list.some(function(x){ return Math.abs(x.lum-lum)<2; })) list.push({c:'rgb('+v[0]+','+v[1]+','+v[2]+')', lum:lum});
    }
    if(list.length<2) return [];
    list.sort(function(a,b){ return b.lum-a.lum; });             // 明るい→暗い
    return [list[0].c, list[Math.floor(list.length/2)].c, list[list.length-1].c];
  }
  // 好きな色1つから「薄い・中間・濃い」を自動で作る（素人に3色選ばせない＝1色だけ選べばいい）
  function _mixW(hex, w){
    var m=(hex||'').replace('#','');
    if(m.length===3) m=m[0]+m[0]+m[1]+m[1]+m[2]+m[2];
    if(m.length<6) return hex;
    var r=parseInt(m.slice(0,2),16), g=parseInt(m.slice(2,4),16), b=parseInt(m.slice(4,6),16);
    return 'rgb('+Math.round(r+(255-r)*w)+','+Math.round(g+(255-g)*w)+','+Math.round(b+(255-b)*w)+')';
  }
  function _colsFromOne(hex){ return [_mixW(hex,0.84), _mixW(hex,0.45), hex]; }
  // 🎨 自分で選んだ色は「マイ色」としてこのPCに記憶する（次に開いた時も並ぶ）
  function myColsGet(){ try{ return JSON.parse(localStorage.getItem('__ce_bgmycols')||'[]'); }catch(_){ return []; } }
  function myColsPut(a){ try{ localStorage.setItem('__ce_bgmycols', JSON.stringify(a.slice(0,10))); }catch(_){} }
  function myColsAdd(hex){ var a=myColsGet().filter(function(c){ return c!==hex; }); a.unshift(hex); myColsPut(a); }
  function myColsDel(hex){ myColsPut(myColsGet().filter(function(c){ return c!==hex; })); }
  // ★飾りは「写真の箱」に合わせる（2026-07-29）。
  //   親が大きい（左に文章＋右に写真／セクション丸ごと 等）と、飾りが親いっぱいに広がって
  //   「セクション全体が青くなった」ように見える（実報告）。写真の実寸を測って、その周りだけに置く。
  //   ★測るのは offsetLeft/Top/Width/Height（rectは出現アニメのtransform途中の値を拾うため使わない）
  // ★位置は offsetLeft/Top だけでは足りない（2026-07-29 実測で判明）
  //   カンプの写真は「自由配置＋ツールのtranslate」で動かしてあることが多い。offsetLeftは
  //   translateを含まない＝飾りだけ"写真が元いた場所"に置かれて右下へ大きく飛ぶ
  //   （実測：img.offsetLeft=1087 なのに実際の表示は x=524／差 -563px＝報告どおり右下にポツンと出た）。
  //   ⭕大きさは offsetWidth/Height のまま（transformで変わらない＝出現アニメ中でも安定）
  //   ⭕位置だけ translate を足す（rect差分は出現アニメ途中の値を拾うので使わない）
  function _pxOf(v, base){
    if(!v) return 0;
    var n=parseFloat(v); if(!isFinite(n)) return 0;
    return (v.indexOf('%')>=0) ? n*(base||0)/100 : n;   // translateの%は要素自身の大きさが基準
  }
  function _txOf(el){
    var x=0, y=0, cs=null;
    try{ cs=getComputedStyle(el); }catch(_){ return null; }
    var t=cs.transform||'';                       // ① transform: translate(...) 系
    if(t&&t!=='none'){
      var done=false;
      try{ var m=new DOMMatrixReadOnly(t); if(isFinite(m.e)&&isFinite(m.f)){ x+=m.e; y+=m.f; done=true; } }catch(_){ }
      if(!done){ var mm=/matrix\(([^)]+)\)/.exec(t); if(mm){ var v=mm[1].split(',').map(parseFloat); x+=v[4]||0; y+=v[5]||0; } }
    }
    // ★② 単体プロパティ translate（2026-07-29 実測で判明・ここが本命）
    //   このツールが要素を動かすのに使っているのは transform ではなく **こっち**（rotate/scaleも単体プロパティ）。
    //   transformだけ見ていると matrix(1,0,0,1,0,0)＝「動いていない」判定になり、飾りが元の位置に取り残される。
    var tr=cs.translate||'';
    if(tr&&tr!=='none'){
      var p=tr.trim().split(' ').filter(Boolean);   // 計算値は "-562.9px -189px" の形（単一スペース区切り）
      x+=_pxOf(p[0], el.offsetWidth); y+=_pxOf(p[1], el.offsetHeight);
    }
    if(!x&&!y) return null;
    return {x:x, y:y};
  }
  function _decoBox(host){
    if(!host) return null;
    var img=null;
    try{ img=host.__ceDecoImg||host.querySelector('img,video'); }catch(_){ }
    if(!img||img.offsetParent!==host) return null;
    var iw=img.offsetWidth, ih=img.offsetHeight;
    if(iw<20||ih<20) return null;
    var hw=host.clientWidth||host.offsetWidth||1, hh=host.clientHeight||host.offsetHeight||1;
    var t=_txOf(img), mx=t?t.x:0, my=t?t.y:0;
    // 写真自体をtranslateで動かしてある時は、親が写真と同じ大きさでも箱合わせが要る
    // （inset方式だと親基準＝写真だけ動いて飾りが取り残される）
    if(Math.abs(mx)<1 && Math.abs(my)<1 && hw<=iw*1.15 && hh<=ih*1.15) return null;
    return {x:img.offsetLeft+mx, y:img.offsetTop+my, w:iw, h:ih, hw:hw, hh:hh};
  }
  function _decoFit(el, box, x, y, w, h){        // %で置く＝画面幅が変わっても写真に付いていく
    ['inset','right','bottom'].forEach(function(p){ el.style.removeProperty(p); });
    el.style.setProperty('left',(x/box.hw*100).toFixed(2)+'%');
    el.style.setProperty('top',(y/box.hh*100).toFixed(2)+'%');
    el.style.setProperty('width',(w/box.hw*100).toFixed(2)+'%');
    el.style.setProperty('height',(h/box.hh*100).toFixed(2)+'%');
  }
  // 色・形・大きさ・向き・モードを飾りdivに塗り直す（どのボタンからも必ずここを通す）
  function _bgPaint(bg){
    // ★区切りは「|」：色が rgb(255,217,230) のようにカンマを含むので , で繋ぐと壊れる
    var cols=(bg.dataset.cols||'').split('|').filter(Boolean);
    if(cols.length<3){ cols=_bgColsFrom(bg); if(cols.length>=3) bg.dataset.cols=cols.join('|'); }
    var dir=bg.dataset.dir||'br', mode=bg.dataset.mode||'shift', D=parseFloat(bg.dataset.size||'26');
    // 濃さは opacity でなく色のアルファで持つ（アニメを付けても薄さが飛ばない）
    var _al=parseFloat(bg.dataset.alpha||'1');
    var _cc=(_al<1)?cols.map(function(c){ return _rgbaWith(c,_al); }):cols;
    if(_cc.length>=3) bg.style.background=_bgGradCss(_cc,dir,mode);
    var box=_decoBox(bg.parentElement);
    if(box){                                   // 写真の箱に合わせて置く（親が大きい時）
      if(mode==='halo'){
        var pad=D+14;
        _decoFit(bg, box, box.x-pad, box.y-pad, box.w+pad*2, box.h+pad*2);
        bg.style.setProperty('filter','blur('+Math.max(16,Math.round(D*0.7))+'px)');
      }else{
        var dx=(dir==='br'||dir==='tr')?D:-D, dy=(dir==='br'||dir==='bl')?D:-D;
        _decoFit(bg, box, box.x+dx, box.y+dy, box.w, box.h);
        bg.style.setProperty('filter','blur(2px)');
      }
    }else if(mode==='halo'){
      ['top','right','bottom','left','width','height'].forEach(function(p){ bg.style.removeProperty(p); });
      bg.style.setProperty('inset', (-D-14)+'px');                        // 全周に広げる
      bg.style.setProperty('filter','blur('+Math.max(16,Math.round(D*0.7))+'px)');
    }else{
      ['width','height'].forEach(function(p){ bg.style.removeProperty(p); });
      _bgPlace(bg, D, dir);
      bg.style.setProperty('filter','blur(2px)');
    }
    // 手で動かしたぶん（←↑↓→）。位置の指定(top/left)とは別に持つので、形や大きさを変えてもズレない
    var ox=parseFloat(bg.dataset.ox||'0'), oy=parseFloat(bg.dataset.oy||'0');
    if(ox||oy) bg.style.setProperty('transform','translate('+ox+'px,'+oy+'px)');
    else bg.style.removeProperty('transform');
  }
  function _bgPlace(bg, D, dir){
    dir=dir||'br';
    var t,r,b,l;                                   // 覗く辺は -D（外へはみ出す）／隠れる辺は +D（写真の下に潜る）
    if(dir==='br'){ t=D; l=D; r=-D; b=-D; }        // 右下へずらす＝右・下に色帯
    else if(dir==='bl'){ t=D; r=D; l=-D; b=-D; }   // 左下
    else if(dir==='tr'){ b=D; l=D; t=-D; r=-D; }   // 右上
    else { b=D; r=D; t=-D; l=-D; }                 // tl 左上
    bg.style.removeProperty('inset');
    bg.style.top=t+'px'; bg.style.right=r+'px'; bg.style.bottom=b+'px'; bg.style.left=l+'px';
  }
  // 飾りは1つの要素に何個でも置ける（重ねて雲みたいにできる）。今いじっている1枚＝data-act="1"。
  function bgList(target){ return target?[].slice.call(target.querySelectorAll(':scope > .ce_bgdeco')):[]; }
  function bgActive(target){
    var l=bgList(target); if(!l.length) return null;
    return l.filter(function(n){ return n.dataset.act==='1'; })[0] || l[l.length-1];
  }
  function bgSetActive(target, bg){ bgList(target).forEach(function(n){ if(n===bg) n.dataset.act='1'; else n.removeAttribute('data-act'); }); }
  // 対象に飾りdivが無ければ作る（既にあれば「今いじっている1枚」を返す）。newOne=true で必ずもう1枚足す。
  function ensureBackdrop(target, newOne){
    if(getComputedStyle(target).position==='static') target.style.setProperty('position','relative');
    target.style.setProperty('isolation','isolate');   // 負のz-indexが祖先の後ろへ抜けないよう囲む
    // 角丸写真は親にoverflow:hiddenが付いていることが多く、それだとはみ出す飾りが見えない→強制で見えるようにする
    target.style.setProperty('overflow','visible','important');
    var bg=newOne?null:bgActive(target);
    if(!bg){
      bg=document.createElement('div'); bg.className='ce_bgdeco'; bg.setAttribute('aria-hidden','true');
      bg.dataset.size='26'; bg.dataset.shape='round'; bg.dataset.dir='br'; bg.dataset.mode='shift';
      // pointer-events:auto ＝飾りを直接クリックして色/薄さ/形を変えられるようにするため（2026-07-29）。
      // 写真より後ろ(z-index:-1)なので、写真の上をクリックしても写真が優先＝じゃまにならない。
      bg.style.cssText='position:absolute;z-index:-1;pointer-events:auto;cursor:move;border-radius:'+BG_SHAPES.round+';background:radial-gradient(60% 55% at 50% 45%, #eef1f5 0%, #f6f8fb 60%, #ffffff 100%);';
      _bgPlace(bg, 26, 'br');                        // 右下へずらして色帯を覗かせる（初期）
      target.insertBefore(bg, target.firstChild);   // 先頭＝一番後ろに置く
    }
    bgSetActive(target, bg);
    _unhideDeco(bg);      // 前に「🚫消す」で隠していたら、色を選んだ時点で必ず出す
    return bg;
  }
  // ＋ もう1つ足す（2枚目は少しずらして置く＝重なって1枚に見えないように）
  function addBackdrop(el){
    var target=bgTarget(el); if(!target) return null;
    var n=bgList(target).length;
    var bg=ensureBackdrop(target, true);
    bg.dataset.ox=String(30*n); bg.dataset.oy=String(-24*n);
    _bgPaint(bg); markDirty();
    if(msg) msg.textContent='飾りをもう1つ足しました（今いじっているのは '+(n+1)+' 枚目です）';
    return bg;
  }
  // ←↑↓→ で今いじっている飾りだけを動かす
  function nudgeBackdrop(el, dx, dy){
    var target=bgTarget(el); if(!target) return;
    var bg=ensureBackdrop(target);
    if(dx===0&&dy===0){ bg.dataset.ox='0'; bg.dataset.oy='0'; }
    else{ bg.dataset.ox=String((parseFloat(bg.dataset.ox||'0'))+dx); bg.dataset.oy=String((parseFloat(bg.dataset.oy||'0'))+dy); }
    _bgPaint(bg); markDirty();
  }
  function applyBackdrop(el, cols){
    var target=bgTarget(el); if(!target){ msg.textContent='ここには敷けません（すぐ外側の箱を右クリックしてください）'; return; }
    var bg=ensureBackdrop(target);
    bg.dataset.cols=(cols||[]).join('|');
    _bgPaint(bg);
    markDirty();
    msg.textContent='背景の飾りを敷きました。★この飾りを直接クリックすると、色・薄さ・形・傾きをその場で変えられます（保存で確定）';
  }
  // ☀後光（全周にふんわり）↔ ◧ずらす（右下などに色帯）を切り替える
  function toggleBackdropMode(el){
    var target=bgTarget(el); if(!target) return;
    var bg=ensureBackdrop(target);
    var next=(bg.dataset.mode==='halo')?'shift':'halo';
    bg.dataset.mode=next;
    _bgPaint(bg);
    markDirty();
    if(msg) msg.textContent=(next==='halo')?'☀ 後光：写真の全周にふんわり広げました（大きさで広がりを調整できます）':'◧ ずらす：写真の外へ色帯を覗かせます';
    return next;
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
    var cur=parseFloat(bg.dataset.size||'26');
    var next=Math.max(8, Math.min(120, cur+delta));   // 覗く量(px)。大きいほど色帯が太く見える
    bg.dataset.size=String(next);
    _bgPaint(bg);
    markDirty();
  }
  // ずらす向きを 右下→右上→左上→左下 と切り替える（色帯を出したい辺を選べる）
  function flipBackdropDir(el){
    var target=bgTarget(el); if(!target) return;
    var bg=ensureBackdrop(target);
    var order=['br','tr','tl','bl'];
    var next=order[(order.indexOf(bg.dataset.dir||'br')+1)%order.length];
    bg.dataset.dir=next;
    _bgPaint(bg);        // 向きを変えたら濃い色の来る角も一緒に付け替える（帯が白くならないように）
    markDirty();
    if(msg) msg.textContent='ずらす向き：'+({br:'右下',tr:'右上',tl:'左上',bl:'左下'}[next]);
  }
  // 今いじっている1枚だけ消す（複数あるとき全部消えないように）
  function removeBackdrop(el){
    var target=bgTarget(el); if(!target) return 0;
    var bg=bgActive(target);
    if(bg) bg.remove();
    var rest=bgList(target);
    if(rest.length) bgSetActive(target, rest[rest.length-1]);
    markDirty();
    msg.textContent=rest.length?('飾りを1枚消しました（残り '+rest.length+' 枚）'):'背景の飾りを消しました（保存で確定）';
    return rest.length;
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
      ring.style.cssText='position:absolute;inset:-9%;z-index:-1;pointer-events:auto;cursor:move;border-radius:50%;';
      target.insertBefore(ring, target.firstChild);
    }
    _unhideDeco(ring);
    // 親が大きいと inset:-9% は「セクション全体を囲む巨大な輪」になる＝写真の箱に合わせ直す
    (function(){
      var box=_decoBox(target); if(!box) return;
      var pad=Math.round(Math.min(box.w,box.h)*0.09);
      _decoFit(ring, box, box.x-pad, box.y-pad, box.w+pad*2, box.h+pad*2);
    })();
    ring.dataset.ring=colorKey;
    ring.style.borderColor=RING_COLORS[colorKey]||RING_COLORS.soft;
    ring.style.borderStyle='solid';
    ring.style.borderWidth='1px';
    markDirty();
    msg.textContent='輪郭だけのリングを重ねました（ゆっくり回転）。★リングを直接クリックすると色・太さ・形を変えられます';
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
  // 角丸は写真と同じ形にそろえる（見本のように"もう1枚後ろにある"感を出すため。写真が特殊な角丸でも追従）
  function _outlineRadius(img, target){
    var r='';
    try{ if(img) r=getComputedStyle(img).borderRadius||''; }catch(_){ }
    if(!r||/^0(px|%)?$/.test(r)){ try{ r=getComputedStyle(target).borderRadius||''; }catch(_){ } }
    if(!r||/^0(px|%)?$/.test(r)) return '32px';
    return r;
  }
  var OUT_DIRS=['br','tr','tl','bl'];
  var OUT_DIRNAME={br:'右下',tr:'右上',tl:'左上',bl:'左下'};
  function _positionOutline(ol, img, target, dir){
    dir=OUT_DIRS.indexOf(dir)>=0?dir:'br';
    var s=parseFloat(ol.dataset.shift||'28');
    var dx=(dir==='br'||dir==='tr')? s : -s, dy=(dir==='br'||dir==='bl')? s : -s;
    if(img && img.offsetParent===target){
      var t=_txOf(img), mx=t?t.x:0, my=t?t.y:0;   // ★写真をtranslateで動かしてある分を足す（足さないと線だけ元の位置に残る）
      ol.style.left=(img.offsetLeft+mx+dx)+'px';
      ol.style.top=(img.offsetTop+my+dy)+'px';
      ol.style.width=img.offsetWidth+'px';
      ol.style.height=img.offsetHeight+'px';
      ol.style.right='auto'; ol.style.bottom='auto';
    } else {
      // 画像を持たない要素そのものを囲む場合は、要素自身基準で斜めにずらす
      ol.style.left='auto'; ol.style.top='auto'; ol.style.width='auto'; ol.style.height='auto';
      ol.style.inset = (dy)+'px '+(-dx)+'px '+(-dy)+'px '+(dx)+'px';
    }
  }
  function _outlineTarget(el){ var img=_outlineImg(el); return {img:img, target: img ? (img.parentElement||el) : el}; }
  function toggleOutline(el, colorKey){
    var g=_outlineTarget(el), img=g.img, target=g.target;
    if(!target || target===document.body){ msg.textContent='ここには追加できません'; return; }
    var ol=target.querySelector(':scope > .ce_outlinedeco');
    if(ol && ol.dataset.outline===colorKey){ ol.remove(); markDirty(); msg.textContent='縁取り線を消しました（保存で確定）'; return; }
    if(getComputedStyle(target).position==='static') target.style.setProperty('position','relative');
    target.style.setProperty('isolation','isolate');   // 負のz-indexが祖先の後ろへ抜けて隠れないよう囲む
    target.style.setProperty('overflow','visible','important');
    if(!ol){
      ol=document.createElement('div'); ol.className='ce_outlinedeco'; ol.setAttribute('aria-hidden','true');
      ol.dataset.dir='br'; ol.dataset.shift='28';    // 既定＝右下へ28px（見本の「もう1枚後ろにある」見え方）
      ol.style.cssText='position:absolute;z-index:-1;pointer-events:auto;cursor:move;border-style:solid;border-width:2px;';
      // 既にある背景ブロブ(ce_bgdeco)より後ろに置くと隠れて見えなくなる（同じz-index:-1同士はDOM順で後が上）。
      // 写真(position:staticのimg)より必ず後ろに描画される点は変わらないので、末尾に足してブロブより手前に出す。
      target.appendChild(ol);
    }
    _unhideDeco(ol);
    ol.style.borderRadius=_outlineRadius(img, target);   // 写真と同じ角丸にそろえる
    _positionOutline(ol, img, target, ol.dataset.dir||'br');
    ol.dataset.outline=colorKey;
    ol.style.borderColor=OUTLINE_COLORS[colorKey]||OUTLINE_COLORS.blue;
    markDirty();
    msg.textContent='縁取り線をずらして重ねました（↔で向き・＋－でずらす量）。★線を直接クリックすると色・太さ・傾きも変えられます';
  }
  function flipOutlineDir(el){
    var g=_outlineTarget(el), ol=g.target && g.target.querySelector(':scope > .ce_outlinedeco');
    if(!ol){ msg.textContent='先に縁取り線の色を選んで追加してください'; return; }
    var nd=OUT_DIRS[(OUT_DIRS.indexOf(ol.dataset.dir||'br')+1)%OUT_DIRS.length];
    ol.dataset.dir=nd;
    _positionOutline(ol, g.img, g.target, nd);
    markDirty();
    msg.textContent='縁取り線のずらす向き：'+OUT_DIRNAME[nd];
  }
  // ずらす量（＝どれだけ覗かせるか）を増減する
  function nudgeOutlineShift(el, delta){
    var g=_outlineTarget(el), ol=g.target && g.target.querySelector(':scope > .ce_outlinedeco');
    if(!ol){ msg.textContent='先に縁取り線の色を選んで追加してください'; return; }
    var next=Math.max(6, Math.min(140, (parseFloat(ol.dataset.shift||'28'))+delta));
    ol.dataset.shift=String(next);
    _positionOutline(ol, g.img, g.target, ol.dataset.dir||'br');
    markDirty();
    msg.textContent='縁取り線のずらす量：'+next+'px';
  }
  function openGradPicker(el){
    if(!el){ msg.textContent='対象がありません'; return; }
    // 色は「薄い→中間→濃い」の3つで持つ（濃い色が必ず"見えている側"へ来るように塗り分けるため）
    var GRADS=[
     ['そら（水色）',    ['#eaf6ff','#bfe0fb','#8fc7f0']],
     ['みず×ミント',    ['#e9fbf6','#a8ecdd','#7fd0e6']],
     ['さくら（桃）',    ['#fff0f5','#ffd0e0','#f9a9c4']],
     ['ゆうやけ（暖）',  ['#fff2e6','#ffd8b8','#ffb49b']],
     ['ラベンダー',      ['#f4f0ff','#ddd0ff','#bda9f2']],
     ['やわらかグレー',  ['#f8fafc','#e7ecf3','#cfd8e4']]
    ];
    var SHAPE_H=[['oval','⬭ 丸型'],['blob','💧 しずく型'],['round','▢ 角丸四角'],['square','◻ 四角']];
    var items=GRADS.map(function(g,i){return '<div class="it" data-i="'+i+'"><div style="height:80px;background:'+_bgGradCss(g[1],'br','shift')+'"></div><span>'+esc(g[0])+'</span></div>';}).join('');
    var shapeH=SHAPE_H.map(function(s){return '<button class="go2" data-shape="'+s[0]+'" style="background:#0b6bcb;margin:0">'+s[1]+'</button>';}).join('');
    // 🎨 自分の色＋マイ色（このPCに記憶）／🧩 何枚目をいじっているかのチップ
    function myRow(){
      var my=myColsGet();
      var chips=my.map(function(c){
        return '<span class="__ce_myc" data-c="'+c+'" title="'+esc(c)+'（クリックで敷く／✕で削除）" style="position:relative;display:inline-block;width:30px;height:30px;border-radius:7px;border:1px solid rgba(0,0,0,.2);background:'+_bgGradCss(_colsFromOne(c),'br','shift')+';cursor:pointer;margin:0 5px 5px 0;vertical-align:middle">'
          +'<b class="__ce_mycx" data-c="'+c+'" style="position:absolute;top:-6px;right:-6px;width:15px;height:15px;line-height:14px;text-align:center;border-radius:50%;background:#c0392b;color:#fff;font-size:10px;cursor:pointer">✕</b></span>';
      }).join('');
      return '<div class="cap" style="margin-top:12px">🎨 好きな色で作る（1色選ぶと薄い→濃いを自動で作ります）</div>'
        +'<div style="display:flex;align-items:center;gap:6px;margin:4px 0 6px">'
        +'<input type="color" id="__ce_bgmyc" value="#7fd0e6" style="width:44px;height:30px;padding:0;border:1px solid #ccc;border-radius:6px;cursor:pointer">'
        +'<button class="go2" id="__ce_bgmyg" style="background:#0b6bcb;margin:0;flex:1">この色で敷く＋マイ色に登録</button></div>'
        +(chips?('<div class="cap" style="margin:2px 0 4px">マイ色（このPCに保存・クリックで敷く）</div><div>'+chips+'</div>'):'');
    }
    function layerRow(){
      var target=bgTarget(el), list=bgList(target), act=bgActive(target);
      var chips=list.map(function(n,i){
        var on=(n===act);
        return '<button class="go2 __ce_bglayer" data-i="'+i+'" style="background:'+(on?'#c026a6':'#888')+';margin:0">'+(on?'●':'○')+' '+(i+1)+'枚目</button>';
      }).join('');
      return '<div class="cap" style="margin-top:12px">🧩 飾りは何枚でも重ねられます（今いじっている1枚だけが変わります）</div>'
        +'<div class="__ce_size" style="grid-template-columns:repeat(auto-fit,minmax(84px,1fr))">'+chips
        +'<button class="go2" id="__ce_bgadd" style="background:#0b6bcb;margin:0">＋ もう1つ</button></div>';
    }
    var ov=document.createElement('div'); ov.id='__ce_pk';
    ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span><h4>背景の飾りを選ぶ（要素の後ろ・AIなし）</h4><div class="gr">'+items+'</div>'
      +'<div id="__ce_bgmy">'+myRow()+'</div>'
      +'<div id="__ce_bglayers">'+layerRow()+'</div>'
      +'<div class="cap" style="margin-top:12px">かたち</div>'
      +'<div class="__ce_size" style="grid-template-columns:repeat(4,1fr)">'+shapeH+'</div>'
      +'<div class="cap" style="margin-top:10px">出し方（☀後光＝全周にふんわり／◧ずらす＝右下などに色帯）</div>'
      +'<div class="__ce_size" style="grid-template-columns:1fr"><button class="go2" id="__ce_bgmode" style="background:#c026a6;margin:0">☀ 後光にする ↔ ◧ ずらす</button></div>'
      +'<div class="cap" style="margin-top:10px">大きさ（覗く色帯の太さ／後光の広がり）・ずらす向き</div>'
      +'<div class="__ce_size" style="grid-template-columns:repeat(3,1fr)"><button class="go2" id="__ce_bgsm" style="background:#888;margin:0">－ 細く</button><button class="go2" id="__ce_bgbg" style="background:#888;margin:0">＋ 太く</button><button class="go2" id="__ce_bgdir" style="background:#0b6bcb;margin:0">↔ ずらす向き</button></div>'
      +'<div class="cap" style="margin-top:10px">↕ 位置を動かす（今いじっている1枚だけ・10pxずつ）</div>'
      +'<div class="__ce_size" style="grid-template-columns:repeat(5,1fr)">'
      +'<button class="go2 __ce_bgmv" data-x="-10" data-y="0" style="background:#888;margin:0">←</button>'
      +'<button class="go2 __ce_bgmv" data-x="10" data-y="0" style="background:#888;margin:0">→</button>'
      +'<button class="go2 __ce_bgmv" data-x="0" data-y="-10" style="background:#888;margin:0">↑</button>'
      +'<button class="go2 __ce_bgmv" data-x="0" data-y="10" style="background:#888;margin:0">↓</button>'
      +'<button class="go2 __ce_bgmv" data-x="0" data-y="0" data-rst="1" style="background:#555;margin:0">⟲ 位置</button></div>'
      +'<button class="go2" id="__ce_bgrm" style="background:#c0392b">🚫 この1枚を消す</button>'
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
      +'<div class="__ce_size" style="grid-template-columns:repeat(3,1fr);margin-top:6px">'
      +'<button class="go2" id="__ce_outdir" style="background:#888;margin:0">↔ 向き</button>'
      +'<button class="go2" id="__ce_outsm" style="background:#888;margin:0">－ 近づける</button>'
      +'<button class="go2" id="__ce_outbg" style="background:#888;margin:0">＋ ずらす</button></div>'
      +'<div class="cap" style="margin:2px 0 8px">同じ色をもう一度押すと縁取り線を外せます</div>'
      +'</div>';
    document.body.appendChild(ov);
    // 枚数や登録色が変わったら、その行だけ描き直す（パネルを閉じずに続けて作業できる）
    function reMy(){ var n=ov.querySelector('#__ce_bgmy'); if(n) n.innerHTML=myRow(); }
    function reLayers(){ var n=ov.querySelector('#__ce_bglayers'); if(n) n.innerHTML=layerRow(); }
    ov.addEventListener('click',function(e){
      if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
      var it=e.target.closest('.it');
      if(it){ applyBackdrop(el, GRADS[+it.dataset.i][1]); reLayers(); return; }
      // ✕ マイ色を捨てる（色チップより先に判定する＝✕を押したのに敷かれてしまうのを防ぐ）
      var mx=e.target.closest('.__ce_mycx');
      if(mx){ myColsDel(mx.getAttribute('data-c')); reMy(); return; }
      var mc=e.target.closest('.__ce_myc');
      if(mc){ applyBackdrop(el, _colsFromOne(mc.getAttribute('data-c'))); reLayers(); return; }
      if(e.target.id==='__ce_bgmyg'){                                      // 好きな色で敷く＋登録
        var hx=(ov.querySelector('#__ce_bgmyc')||{}).value||'#7fd0e6';
        applyBackdrop(el, _colsFromOne(hx)); myColsAdd(hx); reMy(); reLayers();
        if(msg) msg.textContent='この色で敷いて「マイ色」に登録しました（次に開いた時も出ます）';
        return;
      }
      var lb=e.target.closest('.__ce_bglayer');
      if(lb){ var t2=bgTarget(el), l2=bgList(t2); bgSetActive(t2, l2[+lb.getAttribute('data-i')]); reLayers();
        if(msg) msg.textContent='いじる飾りを '+(+lb.getAttribute('data-i')+1)+' 枚目に変えました'; return; }
      if(e.target.id==='__ce_bgadd'){ addBackdrop(el); reLayers(); return; }
      var mv=e.target.closest('.__ce_bgmv');
      if(mv){ nudgeBackdrop(el, +mv.getAttribute('data-x'), +mv.getAttribute('data-y')); return; }
      var sb=e.target.closest('button[data-shape]');
      if(sb){ setBackdropShape(el, sb.getAttribute('data-shape')); return; }
      if(e.target.id==='__ce_bgmode'){ toggleBackdropMode(el); return; }   // ☀後光 ↔ ◧ずらす
      if(e.target.id==='__ce_bgsm'){ setBackdropSize(el, -14); return; }   // 細く＝覗く色帯を減らす
      if(e.target.id==='__ce_bgbg'){ setBackdropSize(el, 14); return; }    // 太く＝覗く色帯を増やす
      if(e.target.id==='__ce_bgdir'){ flipBackdropDir(el); return; }       // ずらす向きを切替
      if(e.target.id==='__ce_bgrm'){ var rest=removeBackdrop(el); if(rest) reLayers(); else ov.remove(); return; }
      var rb=e.target.closest('button[data-ring]');
      if(rb){ toggleRing(el, rb.getAttribute('data-ring')); return; }
      var ob=e.target.closest('button[data-outline]');
      if(ob){ toggleOutline(el, ob.getAttribute('data-outline')); return; }
      if(e.target.id==='__ce_outdir'){ flipOutlineDir(el); return; }
      if(e.target.id==='__ce_outsm'){ nudgeOutlineShift(el, -8); return; }   // 写真に近づける
      if(e.target.id==='__ce_outbg'){ nudgeOutlineShift(el, 8); return; }    // もっとずらして覗かせる
    });
  }
  // ===== 🔵 飾り・線を「クリックして、その場で調整」（2026-07-29・要望）=====
  //   ★これまでは 右クリック→🖼写真を加工→🌸背景の飾り と3階層たどらないと色ひとつ変えられなかった。
  //   飾りそのものをクリック＝一番短い導線。小さい板をその場に出す（idが__ce始まり＝保存時に自動で消える）。
  var DQ_COLORS=[['#7fd0e6','みず'],['#a8ecdd','ミント'],['#f9a9c4','さくら'],['#ffb49b','ゆうやけ'],
                 ['#bda9f2','ラベンダー'],['#cfd8e4','グレー'],['#f0c14b','やまぶき'],['#2b2b30','黒']];
  var DQ_NAME={bg:'🌸 背景の飾り',ring:'⭕ リング',outline:'▢ 縁取り線',line:'➖ 線',shape:'🔶 図形・線'};
  function dqKind(el){
    if(!el||!el.classList) return '';
    if(el.classList.contains('ce_bgdeco')) return 'bg';
    if(el.classList.contains('ce_ringdeco')) return 'ring';
    if(el.classList.contains('ce_outlinedeco')) return 'outline';
    if(el.classList.contains('ce_shape')) return 'shape';
    if(el.getAttribute&&el.getAttribute('data-celine')) return 'line';
    return '';
  }
  // 前に作った飾り（pointer-events:noneで焼き込まれている）にも後からクリック可能を付ける
  function dqArm(root){
    try{
      [].slice.call((root||document).querySelectorAll('.ce_bgdeco,.ce_ringdeco,.ce_outlinedeco')).forEach(function(n){
        n.style.setProperty('pointer-events','auto'); n.style.setProperty('cursor','move');
      });
    }catch(_){ }
  }
  var DQ_SEL='.ce_bgdeco,.ce_ringdeco,.ce_outlinedeco,.ce_shape,[data-celine]';
  function dqHit(t){
    if(!t||!t.closest) return null;
    if(t.closest('[id^=__ce]')) return null;                 // ツール自身のUIの上は対象外
    return t.closest(DQ_SEL);
  }
  // ★飾りは写真の後ろ(z-index:-1)にいるので、上に透明なコンテナが1枚あるだけでクリックを横取りされる
  //   （実測：飾りのはみ出し帯を押しても DIV.container が返ってきて板が出なかった）。
  //   そこで重なりを手前から全部見る。ただし飾りより手前に「中身のあるもの（写真・背景色・文字）」が
  //   あれば、そちらが本命なので飾りは拾わない＝写真をクリックした時にじゃまをしない。
  // ★「文字を持つ要素」で切ると外れる：段落の箱は広く、飾りが覗いている余白まで箱に含まれる
  //   （実測：写真の右にはみ出た帯を押しても、上に段落の箱があるだけで飾りを選べなかった）。
  //   なので実際の行（テキストノードの矩形）に当たっているかまで見る。
  function _dqTextAt(n,x,y){
    for(var i=0;i<n.childNodes.length;i++){
      var c=n.childNodes[i];
      if(c.nodeType!==3||!(c.textContent||'').trim()) continue;
      var rects;
      try{ var r=document.createRange(); r.selectNodeContents(c); rects=r.getClientRects(); }catch(_){ continue; }
      for(var j=0;j<rects.length;j++){
        var q=rects[j];
        if(x>=q.left&&x<=q.right&&y>=q.top&&y<=q.bottom) return true;
      }
    }
    return false;
  }
  function _dqInk(n,x,y){
    if(!n||n.nodeType!==1) return false;
    var cs; try{ cs=getComputedStyle(n); }catch(_){ return false; }
    if(parseFloat(cs.opacity||'1')<0.05) return false;      // 出現アニメ待ちで透明＝まだ見えていない
    var tg=n.tagName;
    if(tg==='IMG'||tg==='VIDEO'||tg==='CANVAS'||tg==='SVG'||tg==='BUTTON'||tg==='A'||tg==='INPUT') return true;
    if(cs.backgroundImage&&cs.backgroundImage!=='none') return true;
    var bc=cs.backgroundColor||'';
    if(bc&&bc!=='transparent'&&bc.indexOf('rgba(0, 0, 0, 0)')<0) return true;
    return _dqTextAt(n,x,y);
  }
  function dqHitAt(x,y,t){
    var d=dqHit(t); if(d) return d;
    var list=[];
    try{ list=document.elementsFromPoint(x,y)||[]; }catch(_){ return null; }
    for(var i=0;i<list.length;i++){
      var n=list[i];
      if(n.closest&&n.closest('[id^=__ce]')) continue;
      var hit=(n.closest&&n.closest(DQ_SEL))||null;
      if(hit) return hit;
      if(_dqInk(n,x,y)) return null;      // 飾りより手前に中身がある＝そっちが本命
    }
    return null;
  }
  // 色に「薄さ」を混ぜる（rgb/rgba/#hex → rgba）
  function _rgbaWith(col, a){
    var m=/rgba?\(([^)]+)\)/.exec(col||'');
    if(m){ var v=m[1].split(',').map(function(s){return parseFloat(s);}); return 'rgba('+v[0]+','+v[1]+','+v[2]+','+a+')'; }
    var h=(col||'').trim().replace('#','');
    if(h.length===3) h=h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
    if(h.length>=6) return 'rgba('+parseInt(h.slice(0,2),16)+','+parseInt(h.slice(2,4),16)+','+parseInt(h.slice(4,6),16)+','+a+')';
    return col;
  }
  function _alphaOf(col){ var m=/rgba\(([^)]+)\)/.exec(col||''); if(m){ var v=m[1].split(','); return v.length>3?parseFloat(v[3]):1; } return 1; }
  function dqColor(el, hex){
    var k=dqKind(el); pushUndo(el);
    if(el.getAttribute('data-cewater')){ el.setAttribute('data-cedqbase',hex); paintWater(el,hex,null); markDirty(); return; }
    var a=parseFloat(el.getAttribute('data-cedqa')||'1');
    if(k==='bg'){ el.dataset.cols=_colsFromOne(hex).join('|'); el.dataset.alpha=String(a); _bgPaint(el); }
    else{
      el.setAttribute('data-cedqbase',hex);
      var col=(a<1)?_rgbaWith(hex,a):hex;
      if(k==='ring'||k==='outline'){ el.style.setProperty('border-color',col,'important'); el.style.setProperty('border-style','solid','important'); }
      else el.style.setProperty('background',col,'important');
    }
    markDirty();
  }
  // ★「薄く」を opacity でやってはいけない（2026-07-29 実測）
  //   アニメを付けると purgeInlineFx が opacity を無条件で消す＝薄さが飛んで
  //   「アニメを付けたら色が濃く（青く）なった」に見える。色のアルファで持てば消されないし、
  //   出現アニメ側の opacity 制御ともケンカしない。
  function dqFade(el, d){
    var k=dqKind(el); pushUndo(el);
    el.style.removeProperty('opacity');                       // 旧方式で薄くしていた分は捨てる
    var a=Math.max(0.08, Math.min(1, (parseFloat(el.getAttribute('data-cedqa')||'1'))+d));
    a=Math.round(a*100)/100;
    el.setAttribute('data-cedqa', String(a));
    if(k==='bg'){ el.dataset.alpha=String(a); _bgPaint(el); }
    else{
      var base=el.getAttribute('data-cedqbase');
      if(!base){
        base=(k==='ring'||k==='outline')?(getComputedStyle(el).borderTopColor||'#888888')
                                        :(getComputedStyle(el).backgroundColor||'#888888');
        el.setAttribute('data-cedqbase', base);
      }
      var col=_rgbaWith(base, Math.round(_alphaOf(base)*a*100)/100);
      if(k==='ring'||k==='outline') el.style.setProperty('border-color',col,'important');
      else el.style.setProperty('background',col,'important');
    }
    markDirty(); return a;
  }
  function dqShape(el, shape){
    pushUndo(el);
    el.style.setProperty('border-radius', BG_SHAPES[shape]||BG_SHAPES.oval);
    if(dqKind(el)==='bg') el.dataset.shape=shape;
    markDirty();
  }
  function dqBigger(el, d){
    var k=dqKind(el); pushUndo(el);
    if(k==='bg'){
      el.dataset.size=String(Math.max(8,Math.min(200,(parseFloat(el.dataset.size||'26'))+d)));
      _bgPaint(el);
    }else if(k==='outline'){
      var tg=el.parentElement, im=tg?tg.querySelector('img'):null;
      el.dataset.shift=String(Math.max(6,Math.min(140,(parseFloat(el.dataset.shift||'28'))+(d>0?8:-8))));
      _positionOutline(el, im, tg, el.dataset.dir||'br');
    }else if(k==='ring'){
      var w=Math.max(1,Math.min(20,(parseFloat(getComputedStyle(el).borderTopWidth)||1)+(d>0?1:-1)));
      el.style.setProperty('border-width',w+'px','important');
    }else{
      // 線（細長い）は太さ、それ以外の図形は縦横まとめて拡大縮小する
      var ew=el.offsetWidth, eh=el.offsetHeight;
      if(eh<=8&&ew>eh) el.style.setProperty('height',Math.max(1,Math.min(60,eh+(d>0?1:-1)))+'px','important');
      else if(ew<=8&&eh>ew) el.style.setProperty('width',Math.max(1,Math.min(60,ew+(d>0?1:-1)))+'px','important');
      else { var r2=(d>0?1.12:0.89); el.style.setProperty('width',Math.round(ew*r2)+'px','important'); el.style.setProperty('height',Math.round(eh*r2)+'px','important'); }
    }
    markDirty();
  }
  function dqMove(el, dx, dy){
    pushUndo(el);
    if(dqKind(el)==='bg'){
      if(dx===0&&dy===0){ el.dataset.ox='0'; el.dataset.oy='0'; }
      else { el.dataset.ox=String((parseFloat(el.dataset.ox||'0'))+dx); el.dataset.oy=String((parseFloat(el.dataset.oy||'0'))+dy); }
      _bgPaint(el);
    }else{
      var ox=(dx===0&&dy===0)?0:(parseFloat(el.getAttribute('data-cedqx')||'0'))+dx;
      var oy=(dx===0&&dy===0)?0:(parseFloat(el.getAttribute('data-cedqy')||'0'))+dy;
      el.setAttribute('data-cedqx',String(ox)); el.setAttribute('data-cedqy',String(oy));
      if(ox||oy) el.style.setProperty('translate', ox+'px '+oy+'px');
      else el.style.removeProperty('translate');
    }
    markDirty();
  }
  // ⤴ 少し斜めに（おしゃれ度の調整）。線は既存の回転(data-cero)に合わせる＝他の機能とケンカしない。
  function dqTilt(el, deg){
    pushUndo(el);
    if(dqKind(el)==='line'){ rotateBy(el, deg); markDirty(); return; }
    var cur=(parseFloat(el.getAttribute('data-cedqr')||'0'))+deg;
    el.setAttribute('data-cedqr',String(cur));
    el.style.setProperty('rotate', cur+'deg');
    markDirty();
  }
  function dqTiltReset(el){
    if(dqKind(el)==='line'){ var c=+el.getAttribute('data-cero')||0; if(c) rotateBy(el,-c); }
    else { el.removeAttribute('data-cedqr'); el.style.removeProperty('rotate'); }
    markDirty();
  }
  function dqFlipDir(bg){
    var order=['br','tr','tl','bl'];
    bg.dataset.dir=order[(order.indexOf(bg.dataset.dir||'br')+1)%order.length];
    _bgPaint(bg); markDirty();
    if(msg) msg.textContent='ずらす向き：'+({br:'右下',tr:'右上',tl:'左上',bl:'左下'}[bg.dataset.dir]);
  }
  function openDecoQuick(el, x, y){
    var k=dqKind(el); if(!k) return;
    var old=document.getElementById('__ce_dqp'); if(old){ if(old.__close) old.__close(); else old.remove(); }
    var B='background:#eef2f7;color:#333;border:1px solid #d7e0ea;border-radius:6px;padding:4px 8px;cursor:pointer;font:inherit';
    function sw(c,t){ return '<button class="__ce_dqsw" data-c="'+c+'" title="'+esc(t)+'" style="width:24px;height:24px;border:1px solid rgba(0,0,0,.25);border-radius:5px;cursor:pointer;background:'+c+';padding:0;margin:2px 2px 0 0;vertical-align:middle"></button>'; }
    var h='<b>'+DQ_NAME[k]+'</b><span style="opacity:.6;font-size:11px">（選択中）</span>'
      +'<div style="opacity:.7;margin:6px 0 2px">色</div><div>'+DQ_COLORS.map(function(c){return sw(c[0],c[1]);}).join('')
      +'<input type="color" class="__ce_dqc" title="好きな色" style="width:32px;height:24px;padding:0;border:1px solid #ccc;border-radius:5px;cursor:pointer;vertical-align:middle;margin-left:4px"></div>'
      +'<div style="opacity:.7;margin-top:8px">濃さ（薄くするとふんわり）</div>'
      +'<div style="display:flex;gap:5px;margin-top:3px"><button data-op="-0.15" style="'+B+'">◻ 薄く</button><button data-op="0.15" style="'+B+'">◼ 濃く</button></div>';
    h+='<div style="opacity:.7;margin-top:8px">かたち（角の丸み）</div>'
      +'<div style="display:flex;gap:5px;margin-top:3px;flex-wrap:wrap">'
      +'<button data-sh="oval" style="'+B+'">⬭ 丸</button><button data-sh="blob" style="'+B+'">💧 しずく</button>'
      +'<button data-sh="round" style="'+B+'">▢ 角丸</button><button data-sh="square" style="'+B+'">◻ 四角</button></div>';
    h+='<div style="opacity:.7;margin-top:8px">'+(k==='line'?'太さ':(k==='ring'?'線の太さ':'大きさ'))+'</div>'
      +'<div style="display:flex;gap:5px;margin-top:3px"><button data-sz="-14" style="'+B+'">－ 小さく</button><button data-sz="14" style="'+B+'">＋ 大きく</button></div>';
    if(k==='bg'){
      h+='<div style="opacity:.7;margin-top:8px">向き・位置</div>'
        +'<div style="display:flex;gap:5px;margin-top:3px;flex-wrap:wrap"><button data-dir="1" style="'+B+'">↔ 向き</button>'
        +'<button data-mv="-10,0" style="'+B+'">←</button><button data-mv="10,0" style="'+B+'">→</button>'
        +'<button data-mv="0,-10" style="'+B+'">↑</button><button data-mv="0,10" style="'+B+'">↓</button>'
        +'<button data-mv="0,0" style="'+B+'">⟲</button></div>';
    }
    if(k==='shape'||k==='bg'){
      h+='<div style="opacity:.7;margin-top:8px">🎨 水彩（にじんだ絵の具にする）</div>'
        +'<div style="display:flex;gap:5px;margin-top:3px"><button data-water="1" style="'+B+'">🎨 水彩にする／戻す</button></div>';
    }
    h+='<div style="opacity:.7;margin-top:8px">⤴ 傾き（少し斜めにするとおしゃれ）</div>'
      +'<div style="display:flex;gap:5px;margin-top:3px"><button data-ro="-3" style="'+B+'">↖ 左へ</button><button data-ro="3" style="'+B+'">↗ 右へ</button><button data-ror="1" style="'+B+'">⟲ まっすぐ</button></div>'
      +'<div style="display:flex;gap:6px;margin-top:10px">'
      +'<button data-del="1" style="background:#c0392b;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer">🚫 消す</button>'
      +'<button data-x="1" style="background:#555;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer">閉じる</button></div>';
    var p=document.createElement('div'); p.id='__ce_dqp';
    p.setAttribute('style','position:fixed;z-index:2147483647;background:#fff;color:#1d1d1f;border:1px solid #dbe4ee;border-radius:11px;padding:10px 12px;font:12px/1.6 sans-serif;box-shadow:0 8px 28px rgba(0,0,0,.28);max-width:300px');
    p.innerHTML=h;
    document.body.appendChild(p);
    p.style.left=Math.max(6,Math.min(x,window.innerWidth-p.offsetWidth-8))+'px';
    p.style.top=Math.max(6,Math.min(y,window.innerHeight-p.offsetHeight-8))+'px';
    var hadOl=el.style.outline;                       // 選択中の目印（閉じたら必ず戻す＝保存に残さない）
    el.style.setProperty('outline','2px dashed #2f6fd0');
    p.__close=function(){
      if(hadOl) el.style.outline=hadOl; else el.style.removeProperty('outline');
      if(p.__off) p.__off();
      p.remove();
    };
    var offFn=function(ev){ if(p.parentElement && !p.contains(ev.target)) p.__close(); };
    // 右クリックしたら板は引っ込める＝いつもの右クリックメニュー（動きを付ける等）の邪魔をしない
    var ctxFn=function(){ if(p.parentElement) p.__close(); };
    p.__off=function(){ document.removeEventListener('mousedown',offFn,true); document.removeEventListener('contextmenu',ctxFn,true); };
    setTimeout(function(){ document.addEventListener('mousedown',offFn,true); document.addEventListener('contextmenu',ctxFn,true); },0);
    p.addEventListener('click',function(ev){
      ev.stopPropagation();
      var t=ev.target;
      if(t.getAttribute('data-x')){ p.__close(); return; }
      if(t.getAttribute('data-del')){ p.__close(); pushUndo(el); el.remove(); markDirty(); if(msg) msg.textContent='消しました（💾保存で確定・⟲戻すで取り消せます）'; return; }
      var c=t.closest('.__ce_dqsw'); if(c){ dqColor(el,c.getAttribute('data-c')); if(msg) msg.textContent='色を変えました（💾保存で確定）'; return; }
      var op=t.getAttribute('data-op'); if(op){ var n=dqFade(el,parseFloat(op)); if(msg) msg.textContent='濃さ：'+Math.round(n*100)+'%'; return; }
      var sh=t.getAttribute('data-sh'); if(sh){ dqShape(el,sh); return; }
      var sz=t.getAttribute('data-sz'); if(sz){ dqBigger(el,parseFloat(sz)); return; }
      if(t.getAttribute('data-dir')){ dqFlipDir(el); return; }
      var mv=t.getAttribute('data-mv'); if(mv){ var a=mv.split(','); dqMove(el,parseFloat(a[0]),parseFloat(a[1])); return; }
      if(t.getAttribute('data-water')){
        if(el.getAttribute('data-cewater')){
          el.removeAttribute('data-cewater'); el.removeAttribute('data-cewshape');
          el.style.removeProperty('mix-blend-mode'); el.style.removeProperty('filter');
          if(dqKind(el)==='bg') _bgPaint(el);
          else shapePaint(el, el.getAttribute('data-cedqbase')||SHAPE_ST.col, SHAPE_ST.fill, null);
          if(msg) msg.textContent='水彩をやめました';
        }else{
          el.setAttribute('data-cewater','1');
          var wc=el.getAttribute('data-cedqbase')
            || ((el.dataset&&el.dataset.cols)?((el.dataset.cols||'').split('|')[2]||SHAPE_ST.col):null)
            || getComputedStyle(el).backgroundColor || SHAPE_ST.col;
          paintWater(el, wc, null);
          if(msg) msg.textContent='🎨 水彩にしました（色を押すと水彩のまま色が変わります）';
        }
        markDirty(); return;
      }
      var ro=t.getAttribute('data-ro'); if(ro){ dqTilt(el,parseFloat(ro)); if(msg) msg.textContent='少し斜めにしました（もう一度押すともっと傾きます）'; return; }
      if(t.getAttribute('data-ror')){ dqTiltReset(el); return; }
    });
    p.querySelector('.__ce_dqc').addEventListener('input',function(){ dqColor(el,this.value); });
  }
  // ===== ➖ 実体のない線（要素のborder）も掴めるようにする（2026-07-29・要望）=====
  //   一覧の区切り線などは <div> ではなく border-bottom で描かれている＝DOMに実体が無く、
  //   押しても「行」が選ばれるだけで線そのものに触れなかった。クリック位置が辺のそばなら
  //   その border を対象にする。同じ見た目の線は「まとめて」変えられる（一覧は揃っているのが普通）。
  var DQ_EDGE=6;                                   // 1pxの線でも押しやすいよう当たり判定に余裕を持たせる
  var BD_SIDE={top:'上',bottom:'下',left:'左',right:'右'};
  function dqBorderAt(x,y){
    // ★境界ちょうどを押すと elementsFromPoint は「隣の要素」を返す（実測：区切り線の上を押しても
    //   線を持つ行ではなく次の行が返ってきて掴めなかった）。少しずらした点も候補に混ぜる。
    var cands=[], seen=[];
    [[0,0],[0,-DQ_EDGE],[0,DQ_EDGE],[-DQ_EDGE,0],[DQ_EDGE,0]].forEach(function(o){
      var l=[];
      try{ l=document.elementsFromPoint(x+o[0],y+o[1])||[]; }catch(_){ return; }
      for(var i=0;i<l.length&&i<6;i++){ if(seen.indexOf(l[i])<0){ seen.push(l[i]); cands.push(l[i]); } }
    });
    for(var k=0;k<cands.length;k++){
      var n=cands[k];
      if(!n||n===document.body||n.tagName==='HTML') continue;
      if(n.closest&&n.closest('[id^=__ce]')) continue;
      var cs; try{ cs=getComputedStyle(n); }catch(_){ continue; }
      var r=n.getBoundingClientRect();
      var sides=[['top',Math.abs(y-r.top)],['bottom',Math.abs(y-r.bottom)],['left',Math.abs(x-r.left)],['right',Math.abs(x-r.right)]];
      for(var j=0;j<sides.length;j++){
        var sd=sides[j][0], dist=sides[j][1];
        var w=parseFloat(cs.getPropertyValue('border-'+sd+'-width'))||0;
        var st=cs.getPropertyValue('border-'+sd+'-style');
        if(w<=0||st==='none'||st==='hidden') continue;
        if(dist>Math.max(DQ_EDGE, w+3)) continue;
        // 横の線は「押した点が要素の左右の範囲内」／縦の線は「上下の範囲内」のときだけ（誤爆よけ）
        if((sd==='top'||sd==='bottom')&&(x<r.left-2||x>r.right+2)) continue;
        if((sd==='left'||sd==='right')&&(y<r.top-2||y>r.bottom+2)) continue;
        return {el:n, side:sd};
      }
    }
    return null;
  }
  // 同じ親の中で「同じ見た目の線」を持つ仲間（一覧の区切り線をまとめて変えるため）
  function bdSiblings(el, side){
    var p=el.parentElement; if(!p) return [el];
    var cls=(el.className||'')+'';
    var list=[].slice.call(p.children).filter(function(n){
      if(n===el) return true;
      if(n.tagName!==el.tagName||((n.className||'')+'')!==cls) return false;
      var cs; try{ cs=getComputedStyle(n); }catch(_){ return false; }
      return (parseFloat(cs.getPropertyValue('border-'+side+'-width'))||0)>0;
    });
    return list.length?list:[el];
  }
  function bdEach(t, all, fn){
    var els=all?bdSiblings(t.el,t.side):[t.el];
    els.forEach(function(n){ pushUndo(n); fn(n); });
    markDirty();
    return els.length;
  }
  function openLineQuick(t, x, y){
    var old=document.getElementById('__ce_dqp'); if(old){ if(old.__close) old.__close(); else old.remove(); }
    var B='background:#eef2f7;color:#333;border:1px solid #d7e0ea;border-radius:6px;padding:4px 8px;cursor:pointer;font:inherit';
    function sw(c,nm){ return '<button class="__ce_dqsw" data-c="'+c+'" title="'+esc(nm)+'" style="width:24px;height:24px;border:1px solid rgba(0,0,0,.25);border-radius:5px;cursor:pointer;background:'+c+';padding:0;margin:2px 2px 0 0;vertical-align:middle"></button>'; }
    var p=document.createElement('div'); p.id='__ce_dqp';
    p.setAttribute('style','position:fixed;z-index:2147483647;background:#fff;color:#1d1d1f;border:1px solid #dbe4ee;border-radius:11px;padding:10px 12px;font:12px/1.6 sans-serif;box-shadow:0 8px 28px rgba(0,0,0,.28);max-width:300px');
    p.innerHTML='<b>➖ 線</b><span style="opacity:.6;font-size:11px">（〈'+t.el.tagName.toLowerCase()+'〉の'+BD_SIDE[t.side]+'の線）</span>'
      +'<label style="display:block;margin:6px 0 2px;cursor:pointer"><input type="checkbox" id="__ce_dqall" style="vertical-align:middle"> <span style="opacity:.75">同じ線をまとめて変える（一覧の区切り線をそろえたい時だけON）</span></label>'
      +'<div style="opacity:.7;margin-top:6px">色</div><div>'+DQ_COLORS.map(function(c){return sw(c[0],c[1]);}).join('')
      +'<input type="color" class="__ce_dqc" title="好きな色" style="width:32px;height:24px;padding:0;border:1px solid #ccc;border-radius:5px;cursor:pointer;vertical-align:middle;margin-left:4px"></div>'
      +'<div style="opacity:.7;margin-top:8px">濃さ</div>'
      +'<div style="display:flex;gap:5px;margin-top:3px"><button data-op="-0.15" style="'+B+'">◻ 薄く</button><button data-op="0.15" style="'+B+'">◼ 濃く</button></div>'
      +'<div style="opacity:.7;margin-top:8px">太さ</div>'
      +'<div style="display:flex;gap:5px;margin-top:3px"><button data-w="-1" style="'+B+'">－ 細く</button><button data-w="1" style="'+B+'">＋ 太く</button></div>'
      +'<div style="opacity:.7;margin-top:8px">線の種類</div>'
      +'<div style="display:flex;gap:5px;margin-top:3px;flex-wrap:wrap"><button data-st="solid" style="'+B+'">─ 実線</button><button data-st="dashed" style="'+B+'">╌ 破線</button><button data-st="dotted" style="'+B+'">┄ 点線</button></div>'
      +'<div style="display:flex;gap:6px;margin-top:10px">'
      +'<button data-del="1" style="background:#c0392b;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer">🚫 消す</button>'
      +'<button data-x="1" style="background:#555;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer">閉じる</button></div>';
    document.body.appendChild(p);
    p.style.left=Math.max(6,Math.min(x,window.innerWidth-p.offsetWidth-8))+'px';
    p.style.top=Math.max(6,Math.min(y,window.innerHeight-p.offsetHeight-8))+'px';
    // ★選択中の目印は「その1辺だけ」を光らせる（要素を枠で囲むと上下どちらの線を掴んだのか分からない）
    var mk=document.createElement('div'); mk.id='__ce_dqmark';
    var MK_BASE='position:fixed;z-index:2147483000;pointer-events:none;background:rgba(47,111,208,.6);box-shadow:0 0 0 1px rgba(47,111,208,.95)';
    mk.style.cssText=MK_BASE;
    document.body.appendChild(mk);
    function placeMark(){
      var r=t.el.getBoundingClientRect(), s;
      if(t.side==='top')          s='left:'+r.left+'px;top:'+(r.top-1)+'px;width:'+r.width+'px;height:3px';
      else if(t.side==='bottom')  s='left:'+r.left+'px;top:'+(r.bottom-2)+'px;width:'+r.width+'px;height:3px';
      else if(t.side==='left')    s='left:'+(r.left-1)+'px;top:'+r.top+'px;width:3px;height:'+r.height+'px';
      else                        s='left:'+(r.right-2)+'px;top:'+r.top+'px;width:3px;height:'+r.height+'px';
      mk.style.cssText=MK_BASE+';'+s;
    }
    placeMark();
    var mkT=setInterval(placeMark, 400);      // スクロールや太さ変更に追従
    p.__close=function(){
      clearInterval(mkT); if(mk.parentElement) mk.remove();
      if(p.__off) p.__off();
      p.remove();
    };
    var offFn=function(ev){ if(p.parentElement && !p.contains(ev.target)) p.__close(); };
    // 右クリックしたら板は引っ込める＝いつもの右クリックメニュー（動きを付ける等）の邪魔をしない
    var ctxFn=function(){ if(p.parentElement) p.__close(); };
    p.__off=function(){ document.removeEventListener('mousedown',offFn,true); document.removeEventListener('contextmenu',ctxFn,true); };
    setTimeout(function(){ document.addEventListener('mousedown',offFn,true); document.addEventListener('contextmenu',ctxFn,true); },0);
    function all(){ var c=p.querySelector('#__ce_dqall'); return !!(c&&c.checked); }
    function curCol(n){ return getComputedStyle(n).getPropertyValue('border-'+t.side+'-color')||'rgb(136,136,136)'; }
    function setCol(hex){
      var n=bdEach(t, all(), function(n){
        n.setAttribute('data-cedqbd', hex);
        n.style.setProperty('border-'+t.side+'-color', hex, 'important');
        n.style.setProperty('border-'+t.side+'-style', getComputedStyle(n).getPropertyValue('border-'+t.side+'-style')||'solid', 'important');
      });
      if(msg) msg.textContent='線の色を変えました（'+n+'本・💾保存で確定）';
    }
    p.addEventListener('click',function(ev){
      ev.stopPropagation();
      var e=ev.target;
      if(e.id==='__ce_dqall') return;
      if(e.getAttribute('data-x')){ p.__close(); return; }
      if(e.getAttribute('data-del')){
        var n0=bdEach(t, all(), function(n){ n.style.setProperty('border-'+t.side,'none','important'); });
        p.__close(); if(msg) msg.textContent='線を消しました（'+n0+'本・💾保存で確定・⟲戻すで取り消せます）'; return;
      }
      var c=e.closest('.__ce_dqsw'); if(c){ setCol(c.getAttribute('data-c')); return; }
      var op=e.getAttribute('data-op');
      if(op){
        var d=parseFloat(op);
        var n1=bdEach(t, all(), function(n){
          var a=Math.max(0.05, Math.min(1, (parseFloat(n.getAttribute('data-cedqbda')||'1'))+d));
          a=Math.round(a*100)/100;
          n.setAttribute('data-cedqbda', String(a));
          var base=n.getAttribute('data-cedqbd');
          if(!base){ base=curCol(n); n.setAttribute('data-cedqbd', base); }
          n.style.setProperty('border-'+t.side+'-color', _rgbaWith(base, Math.round(_alphaOf(base)*a*100)/100), 'important');
        });
        if(msg) msg.textContent='線の濃さを変えました（'+n1+'本）'; return;
      }
      var w=e.getAttribute('data-w');
      if(w){
        var dw=parseFloat(w);
        var n2=bdEach(t, all(), function(n){
          var cw=parseFloat(getComputedStyle(n).getPropertyValue('border-'+t.side+'-width'))||1;
          n.style.setProperty('border-'+t.side+'-width', Math.max(1,Math.min(24,cw+dw))+'px', 'important');
        });
        if(msg) msg.textContent='線の太さを変えました（'+n2+'本）'; return;
      }
      var st=e.getAttribute('data-st');
      if(st){
        var n3=bdEach(t, all(), function(n){ n.style.setProperty('border-'+t.side+'-style', st, 'important'); });
        if(msg) msg.textContent='線の種類を変えました（'+n3+'本）'; return;
      }
    });
    p.querySelector('.__ce_dqc').addEventListener('input',function(){ setCol(this.value); });
  }
  // 左クリックで飾り・線を選ぶ（4px以上動いたらドラッグ扱い＝板は出さない）
  var _dqX=0,_dqY=0,_dqDown=null,_dqBd=null,_dqDrag=null;
  document.addEventListener('mousedown',function(e){
    if(e.button!==0) return;
    _dqX=e.clientX; _dqY=e.clientY; _dqDown=dqHitAt(e.clientX,e.clientY,e.target);
    _dqBd=_dqDown?null:dqBorderAt(e.clientX,e.clientY);    // 飾りが無ければ「要素のborderで描かれた線」を探す
    // 飾りは既存のドラッグ機構に渡さない（あちらが動かすと_bgPaintの再計算と食い違うため）。
    // 代わりに自前でドラッグする＝ずらし量を data に持つので、色や形を変え直しても位置が戻らない。
    if(_dqDown){
      var _kk=dqKind(_dqDown);
      if(_kk!=='line'&&_kk!=='shape'){
        e.stopPropagation();
        _dqDrag={el:_dqDown, kind:_kk, x:e.clientX, y:e.clientY, moved:false,
          ox:(_kk==='bg')?parseFloat(_dqDown.dataset.ox||'0'):parseFloat(_dqDown.getAttribute('data-cedqx')||'0'),
          oy:(_kk==='bg')?parseFloat(_dqDown.dataset.oy||'0'):parseFloat(_dqDown.getAttribute('data-cedqy')||'0')};
      }
    }
  },true);
  // 飾りを掴んで動かす（4px動かしたらドラッグ開始＝板は出ない）
  document.addEventListener('mousemove',function(e){
    if(!_dqDrag) return;
    var dx=e.clientX-_dqDrag.x, dy=e.clientY-_dqDrag.y;
    if(!_dqDrag.moved){
      if(Math.abs(dx)<4&&Math.abs(dy)<4) return;
      _dqDrag.moved=true; pushUndo(_dqDrag.el);
      try{ document.documentElement.style.cursor='move'; }catch(_){}
    }
    var el=_dqDrag.el, nx=_dqDrag.ox+dx, ny=_dqDrag.oy+dy;
    if(_dqDrag.kind==='bg'){ el.dataset.ox=String(Math.round(nx)); el.dataset.oy=String(Math.round(ny)); _bgPaint(el); }
    else{
      el.setAttribute('data-cedqx',String(Math.round(nx))); el.setAttribute('data-cedqy',String(Math.round(ny)));
      el.style.setProperty('translate', Math.round(nx)+'px '+Math.round(ny)+'px');
    }
    e.preventDefault(); e.stopPropagation();
  },true);
  // ★clickではなくmouseupで見る（2026-07-29 実測）
  //   図形や線は既存のドラッグ機構が mousedown を掴んで preventDefault するので、
  //   実際のマウス操作では click が飛んでこない＝「押しても板が出ない」になっていた。
  // マウスがウィンドウの外で離されると mouseup が来ない＝掴んだままの状態が残るので保険で落とす
  window.addEventListener('blur',function(){ _dqDrag=null; _dqDown=null; _dqBd=null; try{ document.documentElement.style.cursor=''; }catch(_){} });
  document.addEventListener('mouseup',function(e){
    if(e.button!==0){ _dqDrag=null; _dqDown=null; _dqBd=null; return; }
    var d=_dqDown, bd=_dqBd; _dqDown=null; _dqBd=null;
    if(_dqDrag){                                  // 掴んで動かしていた＝確定して終わり（板は出さない）
      // ★ここを取りこぼすと以降ずっと飾りのドラッグ中扱いになり、他の要素が動かなくなる
      var wasMoved=_dqDrag.moved; _dqDrag=null;
      try{ document.documentElement.style.cursor=''; }catch(_){}
      if(wasMoved){ markDirty(); if(msg) msg.textContent='飾りを動かしました（💾保存で確定・⟲戻すで取り消せます）'; return; }
    }
    if(!d&&!bd) return;
    if(Math.abs(e.clientX-_dqX)>4||Math.abs(e.clientY-_dqY)>4) return;   // 動かしていたらドラッグ＝板は出さない
    if(d){
      if(d!==dqHitAt(e.clientX,e.clientY,e.target)) return;
      openDecoQuick(d, e.clientX+10, e.clientY+10);
      return;
    }
    var now=dqBorderAt(e.clientX,e.clientY);
    if(!now||now.el!==bd.el||now.side!==bd.side) return;
    // 線がリンクの上にあるとページが飛んでしまうので、直後のclickを1回だけ止める。
    // ★ただし板の中のクリックは通すこと（止めると板を開いた直後の1回目の操作＝色選びが効かない・実測）
    var _kill=function(ev){
      if(ev.target&&ev.target.closest&&ev.target.closest('#__ce_dqp')) return;
      ev.preventDefault(); ev.stopPropagation(); document.removeEventListener('click',_kill,true);
    };
    document.addEventListener('click',_kill,true);
    setTimeout(function(){ document.removeEventListener('click',_kill,true); },350);
    openLineQuick(bd, e.clientX+10, e.clientY+10);
  },true);
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
  // ✂ 今カンプに置いてある写真の背景を切り抜いて、その場で透過画像に差し替える（このPCの中だけで処理・無料）。
  //   元に戻せるよう、切り抜く前のsrcを data-cebgorig に控える（もう一度押すと戻る）。
  function cutoutImg(imgEl, btn){
    if(!imgEl){ msg.textContent='写真の上で右クリックしてから使ってください'; return; }
    var back=imgEl.getAttribute('data-cebgorig');
    if(back){   // 2回目＝元に戻す
      imgEl.src=back; imgEl.removeAttribute('data-cebgorig'); markDirty();
      msg.textContent='切り抜く前の写真に戻しました（💾保存で確定）';
      var ovx=document.getElementById('__ce_pk'); if(ovx) ovx.remove();
      return;
    }
    var src=imgEl.getAttribute('src')||'';
    if(!src){ msg.textContent='この写真のURLが取れませんでした'; return; }
    if(btn){ btn.textContent='✂ 切り抜き中…（数秒）'; btn.disabled=true; btn.style.opacity='.7'; }
    fetch('/api/remove_bg_url',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({src:src, camp:FILE})}).then(function(r){return r.json();}).then(function(d){
      var ov=document.getElementById('__ce_pk'); if(ov) ov.remove();
      if(!d||!d.ok){ msg.textContent='切り抜きに失敗：'+((d&&d.message)||'不明'); return; }
      pushUndo(imgEl);
      imgEl.setAttribute('data-cebgorig', src);
      imgEl.src=d.url;
      markDirty();
      msg.textContent='✂ 背景を切り抜きました（もう一度押すと元に戻せます・💾保存で確定）';
    }).catch(function(){
      var ov=document.getElementById('__ce_pk'); if(ov) ov.remove();
      msg.textContent='切り抜きに失敗しました（サーバーに届いていません）';
    });
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
      +'<button class="go2" id="__ce_pdglow" style="background:#0ea5a3;margin-bottom:8px">🌫 ふわっと白い光を出す（左下・右下）</button>'
      +'<button class="go2" id="__ce_pdsetbg" style="background:#0b6bcb;margin-bottom:8px">🖼 画像を背景に設定</button>'
      // ✂背景を切り抜く：カンプに『すでに置いてある画像』にも効かせる（2026-07-25）。
      //   ⭐アップロード一覧の同名ボタンはファイル名で動くが、こちらは src をサーバーに渡す＝
      //     クローン元の画像（相対パス）・外部URLでも切り抜ける。
      +(imgEl?'<button class="go2" id="__ce_pdcut" style="background:#7c3aed;margin-bottom:8px">✂ この写真の背景を切り抜く（透過・AIなし・無料）</button>':'')
      // ★<img>でない「要素の背景として敷かれた写真」もここから切り抜けるようにする。
      //   素人目にはどちらも同じ写真で、img/背景の違いは見えない（実報告：ボタンが出ず迷子になった）。
      +((!imgEl&&bgOfEl(el))?'<button class="go2" id="__ce_pdcutbg" style="background:#7c3aed;margin-bottom:8px">✂ この背景の写真を切り抜く（透過・AIなし・無料）</button>':'')
      +(imgEl?'<button class="go2" id="__ce_pdwater" style="background:#c026a6">🎨 背後に水彩画像を敷く（AI・数十円）</button>':'')
      +'</div>';
    document.body.appendChild(ov);
    ov.addEventListener('click',function(e){
      if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); return; }
      if(e.target.id==='__ce_pdframe'){ ov.remove(); toggleWhiteFrame(el); return; }
      if(e.target.id==='__ce_pdcap'){ ov.remove(); addOverlapCaption(el); return; }
      if(e.target.id==='__ce_pdgrad'){ ov.remove(); openGradPicker(el); return; }
      if(e.target.id==='__ce_pdglow'){ ov.remove(); glowSpots(el); return; }
      if(e.target.id==='__ce_pdsetbg'){ ov.remove(); openPicker({el:el, type:'bg', fresh:true}); return; }
      if(e.target.id==='__ce_pdcut'){ cutoutImg(imgEl, e.target); return; }
      if(e.target.id==='__ce_pdcutbg'){
        var _bc=ov.__bgc||(ov.__bgc=bgOfEl(el));      // 押すたびに拾い直すと「戻す」が効かないので覚える
        if(_bc) bgCutToggle(_bc, e.target);
        return;
      }
      if(e.target.id==='__ce_pdwater'){ ov.remove(); openBgPicker(imgEl, sIdx); return; }
    });
  }

  // 🌫 ふわっと白い光（写真の左下・右下）：白いボケ玉ふうの飾りを付ける／調整する／外す。AIなし・即反映。
  //   実体は写真枠の中に置く子div2つ（radial-gradientの白円）。class名が__ce始まりでないので保存で消えない。
  function _glowHost(el){
    if(!el) return el;
    var h=el;
    if(h.tagName==='IMG'||h.tagName==='PICTURE'){ h=h.parentElement||h; }
    if(h.tagName==='PICTURE'){ h=h.parentElement||h; }
    if(h===document.body) h=el;
    return h;
  }
  // ★光の置き場は「写真の箱の中」に限定する（2026-07-28修正）。
  //   旧版は host（＝imgの親）いっぱいに bottom:6%/left:4% で置いていたため、親が
  //   「左に文章・右に写真」のような大きな箱だと、白い円が左の文章の上に乗って文字が消えた（実際に起きた）。
  //   写真の実寸(offsetLeft/Top/Width/Height)を測って、その中の左下・右下に置き直す。
  //   ★サイズは getBoundingClientRect ではなく offset系で測る（出現アニメのtransform中の値を拾わない）。
  function _glowStyle(d, side, size, op, spread, host){
    var bg='border-radius:50%;pointer-events:none;z-index:2;'
      +'background:radial-gradient(circle, rgba(255,255,255,'+op+') 0%, rgba(255,255,255,0) '+spread+'%)';
    var img=null;
    try{ img=(host&&(host.__ceGlowImg||host.querySelector('img,video')))||null; }catch(_){}
    if(img && host && img.offsetParent===host && img.offsetWidth>20 && img.offsetHeight>20){
      var hw=host.clientWidth||host.offsetWidth||1, hh=host.clientHeight||host.offsetHeight||1;
      var iw=img.offsetWidth, ih=img.offsetHeight, ix=img.offsetLeft, iy=img.offsetTop;
      var sz=Math.min(iw,ih)*size/100;
      var x=(side==='left') ? (ix+iw*0.04) : (ix+iw-sz-iw*0.04);
      var y=iy+ih-sz-ih*0.06;
      d.style.cssText='position:absolute;left:'+(x/hw*100).toFixed(2)+'%;top:'+(y/hh*100).toFixed(2)+'%;'
        +'width:'+(sz/hw*100).toFixed(2)+'%;aspect-ratio:1 / 1;'+bg;
      return;
    }
    // 写真が特定できない時だけ従来どおり箱の左下・右下（要素そのものに光らせたい場合）
    d.style.cssText='position:absolute;bottom:6%;'+side+':4%;width:'+size+'%;aspect-ratio:1 / 1;'+bg;
  }
  function _glowAdd(host, size, op, spread){
    if(getComputedStyle(host).position==='static'){ host.style.setProperty('position','relative'); host.setAttribute('data-ceglowpos','1'); }
    ['left','right'].forEach(function(side){
      var d=document.createElement('div'); d.className='ceglow-spot ceglow-'+side[0];
      _glowStyle(d, side, size, op, spread, host); host.appendChild(d);
    });
    host.setAttribute('data-ceglow', size+'|'+op+'|'+spread);
    markDirty();
  }
  function _glowRemove(host){
    [].slice.call(host.querySelectorAll('.ceglow-spot')).forEach(function(n){ n.remove(); });
    host.removeAttribute('data-ceglow');
    if(host.getAttribute('data-ceglowpos')!=null){ host.style.removeProperty('position'); host.removeAttribute('data-ceglowpos'); }
    markDirty();
  }
  function glowSpots(el){
    var host=_glowHost(el);
    if(!host){ msg.textContent='対象がありません'; return; }
    // どの写真に光らせるかを覚える（親が大きい時に写真の中へ置くため）
    try{ host.__ceGlowImg=(el.tagName==='IMG'||el.tagName==='VIDEO')?el:(el.querySelector&&el.querySelector('img,video'))||null; }catch(_){}
    // 既に付いているか＝子の.ceglow-spotの有無で判定（属性が消えても堅牢）
    if(!host.querySelector('.ceglow-spot')){ _glowAdd(host, 30, 0.85, 70); msg.textContent='ふわっとした白い光を付けました（同じボタンで大きさ・濃さを調整・外せます）'; }
    openGlowPanel(host);
  }
  function openGlowPanel(host){
    var p=(host.getAttribute('data-ceglow')||'30|0.85|70').split('|');
    var size=+p[0]||30, op=parseFloat(p[1]); if(isNaN(op)) op=0.85; var spread=+p[2]||70;
    var old=document.getElementById('__ce_pk'); if(old) old.remove();
    var ov=document.createElement('div'); ov.id='__ce_pk';
    ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span><h4>🌫 ふわっと白い光（写真の左下・右下）</h4>'
      +'<div style="font-size:12px;color:#888;margin-bottom:10px">写真の左下・右下に白いボケ光を出します。数値はその場で反映。<br><b style="color:#1a7f37">閉じるのは右上の × だけ</b>（タイトルを掴めば動かせます）</div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:8px 0 6px">🔵 大きさ</div>'
      +'<div style="display:flex;gap:6px"><button class="go2" data-gs="4" style="background:#0b6bcb;margin:0;flex:1">＋ 大きく</button><button class="go2" data-gs="-4" style="background:#0b6bcb;margin:0;flex:1">－ 小さく</button></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">💧 濃さ（白の強さ）</div>'
      +'<div style="display:flex;gap:6px"><button class="go2" data-go="0.1" style="background:#0b6bcb;margin:0;flex:1">＋ 濃く</button><button class="go2" data-go="-0.1" style="background:#0b6bcb;margin:0;flex:1">－ うすく</button></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">🌫 ぼかしの広がり</div>'
      +'<div style="display:flex;gap:6px"><button class="go2" data-gp="8" style="background:#0b6bcb;margin:0;flex:1">＋ ふんわり</button><button class="go2" data-gp="-8" style="background:#0b6bcb;margin:0;flex:1">－ くっきり</button></div>'
      +'<div style="border-top:1px solid #eee;margin:16px 0 0"></div>'
      +'<button class="go2" id="__ce_glowoff" style="background:#b45309;margin-top:12px">🗑 白い光を外す</button>'
      +'</div>';
    document.body.appendChild(ov);
    function _apply(){
      [].slice.call(host.querySelectorAll('.ceglow-spot')).forEach(function(d){
        _glowStyle(d, d.classList.contains('ceglow-l')?'left':'right', size, op, spread, host);
      });
      host.setAttribute('data-ceglow', size+'|'+op+'|'+spread); markDirty();
    }
    ov.addEventListener('click',function(e){
      if(e.target.id==='__ce_pkx'){ ov.remove(); return; }
      if(e.target.id==='__ce_pk') return;   // 暗幕クリックでは閉じない（調整中に消えないように）
      if(e.target.id==='__ce_glowoff'){ _glowRemove(host); ov.remove(); msg.textContent='白い光を外しました'; return; }
      var gs=e.target.closest('button[data-gs]'); if(gs){ size=Math.max(8,Math.min(80,size+(+gs.getAttribute('data-gs')))); _apply(); return; }
      var go=e.target.closest('button[data-go]'); if(go){ op=Math.max(0.15,Math.min(1,+(op+(+go.getAttribute('data-go'))).toFixed(2))); _apply(); return; }
      var gp=e.target.closest('button[data-gp]'); if(gp){ spread=Math.max(35,Math.min(92,spread+(+gp.getAttribute('data-gp')))); _apply(); return; }
    });
  }
  // 🖱 #__ce_pk系パネル（写真加工・文字編集・画像選択など全部）を、見出し(h4)を掴んでドラッグ移動
  //   できるようにする（委譲＝どのパネルにも自動で効く）。背後のプレビューを見たい時に避けられる。
  document.addEventListener('mousedown',function(e){
    if(e.button!==0) return;
    var h=e.target.closest&&e.target.closest('#__ce_pk h4'); if(!h) return;
    var ov=h.closest('#__ce_pk'), bx=ov&&ov.querySelector('.bx'); if(!bx) return;
    var r=bx.getBoundingClientRect(), sx=e.clientX, sy=e.clientY, moved=false;
    bx.style.position='fixed'; bx.style.left=r.left+'px'; bx.style.top=r.top+'px'; bx.style.margin='0';
    e.preventDefault();
    function mv(ev){
      if(Math.abs(ev.clientX-sx)+Math.abs(ev.clientY-sy)>3) moved=true;
      bx.style.left=Math.max(0,Math.min(r.left+(ev.clientX-sx), window.innerWidth-80))+'px';
      bx.style.top=Math.max(0,Math.min(r.top+(ev.clientY-sy), window.innerHeight-40))+'px';
    }
    function up(){
      if(moved) _pkDragged=Date.now();  // ★この直後のclickで暗幕が押された事にされ、パネルが閉じるのを防ぐ
      document.removeEventListener('mousemove',mv,true); document.removeEventListener('mouseup',up,true);
    }
    document.addEventListener('mousemove',mv,true); document.addEventListener('mouseup',up,true);
  },true);
  // パネルを掴んで動かして「箱の外」で手を離すと、clickの対象が暗幕(#__ce_pk)になり
  // 各パネルの「暗幕クリック＝閉じる」に化けて勝手に閉じていた（＝動かしただけで消える事故）。
  // ドラッグ直後の暗幕クリックだけキャプチャ段階で握りつぶす（全パネル共通で効く）。
  var _pkDragged=0;
  document.addEventListener('click',function(e){
    if(!_pkDragged) return;
    var fresh=(Date.now()-_pkDragged)<400; _pkDragged=0;
    if(fresh && e.target && e.target.id==='__ce_pk'){
      e.stopPropagation(); if(e.stopImmediatePropagation) e.stopImmediatePropagation();
    }
  },true);
  // ===== 📚 お手本と演出アドバイス（修正しながら勉強する用・2026-07-13） =====
  // ベース（このカンプの元になったサイト）のスクショ・似た雰囲気の登録サイト・⭐部品を見比べながら、
  // 💡AIから「ツールの機能名で書かれた改善手順」をもらう。適用はユーザーが自分の手で＝操作と理屈を覚える。
  function refBaseId(){ var m=document.querySelector('meta[name="ce-base"]'); return m?(m.getAttribute('content')||''):''; }
  function refSetBase(id){
    var m=document.querySelector('meta[name="ce-base"]');
    if(!m){ m=document.createElement('meta'); m.setAttribute('name','ce-base'); (document.head||document.documentElement).appendChild(m); }
    m.setAttribute('content', id); markDirty();
  }
  function refOpen(secEl){
    var ov=document.createElement('div'); ov.id='__ce_pk';
    ov.innerHTML='<div class="bx" style="max-width:760px"><span class="cl" id="__ce_pkx">×</span><h4>📚 お手本と演出アドバイス（タイトルを掴んで移動できます）</h4>'
      +'<div id="__ce_refbase"></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:12px 0 6px">🔍 似た雰囲気のお手本（クリックで全体スクショを別タブ表示）</div>'
      +'<div id="__ce_refsim" style="display:flex;gap:8px;overflow-x:auto;padding-bottom:6px;font-size:12px;color:#888">読み込み中…</div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:12px 0 6px">⭐ 保存済みのお気に入り部品（別タブで開いて見比べる）</div>'
      +'<div id="__ce_reffav" style="display:flex;gap:6px;flex-wrap:wrap;font-size:12px;color:#888">読み込み中…</div>'
      +'<div style="border-top:1px solid #eee;margin:14px 0 10px"></div>'
      +'<button class="go2" id="__ce_refadv" style="background:#4b2ea8"'+(secEl?'':' disabled')+'>💡 このセクションの演出アドバイスをもらう（AI・数円）</button>'
      +(secEl?'':'<div style="font-size:11px;color:#c00;margin-top:4px">※セクションの中で右クリックすると、そのセクション向けのアドバイスが出せます</div>')
      +'<div id="__ce_refadvout" style="display:none;white-space:pre-wrap;font-size:13px;line-height:1.9;background:#f7f5ff;border:1px solid #ddd2f7;border-radius:9px;padding:10px 12px;margin-top:8px"></div>'
      +'</div>';
    document.body.appendChild(ov);
    ov.addEventListener('click',function(e){ if(e.target.id==='__ce_pk'||e.target.id==='__ce_pkx'){ ov.remove(); } });
    function renderLinkGrid(){
      var g=ov.querySelector('#__ce_reflinkgrid'); if(!g) return;
      g.style.display='flex';
      g.innerHTML='<span style="font-size:12px;color:#888">読み込み中…</span>';
      fetch('/api/sites').then(function(r){return r.json();}).then(function(d){
        var arr=(d&&(d.sites||d.hits||d.items))||(Array.isArray(d)?d:[]);
        if(!arr.length){ g.innerHTML='<span style="font-size:12px;color:#888">登録サイトがありません</span>'; return; }
        g.innerHTML=arr.map(function(s){
          var sid=s.id||s.site_id; if(!sid) return '';
          var host=''; try{ host=new URL(s.url).hostname.replace(/^www\\./,''); }catch(_){ host=(s.url||'').slice(0,20); }
          return '<div data-sid="'+sid+'" style="width:104px;cursor:pointer;text-align:center">'
            +'<img src="/img/'+sid+'/firstview" style="width:100%;border:2px solid #ddd;border-radius:8px;display:block" loading="lazy">'
            +'<div style="font-size:10px;color:#666;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">'+esc(host)+'</div></div>';
        }).join('');
        g.addEventListener('click',function(ev){
          var c=ev.target.closest('[data-sid]'); if(!c) return;
          refSetBase(c.getAttribute('data-sid'));
          renderBase();
          if(msg) msg.textContent='🔗 ベースをひも付けました（💾保存で残ります）';
        });
      }).catch(function(){ g.innerHTML='<span style="font-size:12px;color:#c00">読み込み失敗</span>'; });
    }
    function renderBase(){
      var bid=refBaseId(), bx=ov.querySelector('#__ce_refbase');
      if(!bid){
        bx.innerHTML='<div style="font-size:12px;color:#888;margin:0 0 6px">このカンプはまだ「元になったベース」が記録されていません（今後の新規生成では自動で記録されます）。</div>'
          +'<button class="go2" id="__ce_reflink" style="background:#0b6bcb;margin:0">🔗 ベースサイトをひも付ける（ストックから選ぶ）</button>'
          +'<div id="__ce_reflinkgrid" style="display:none;flex-wrap:wrap;gap:6px;max-height:34vh;overflow:auto;margin-top:8px"></div>';
      } else {
        bx.innerHTML='<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:0 0 6px">🎨 ベースサイト（このカンプの手本）'
          +'<button id="__ce_reflink" style="margin-left:8px;font-size:11px;border:1px solid #ccc;background:#fff;border-radius:6px;padding:1px 8px;cursor:pointer">🔗 変更</button></div>'
          +'<div style="max-height:42vh;overflow:auto;border:1px solid #eee;border-radius:9px"><img src="/img/'+bid+'/fullpage" style="width:100%;display:block" loading="lazy"></div>'
          +'<div id="__ce_reflinkgrid" style="display:none;flex-wrap:wrap;gap:6px;max-height:34vh;overflow:auto;margin-top:8px"></div>';
      }
      var lb=bx.querySelector('#__ce_reflink');
      if(lb) lb.addEventListener('click',function(){ renderLinkGrid(); });
      loadSim(bid);
    }
    function loadSim(bid){
      var sim=ov.querySelector('#__ce_refsim');
      if(!bid){ sim.innerHTML='<span>ベースをひも付けると、似た雰囲気のお手本がここに並びます</span>'; return; }
      fetch('/api/similar?id='+encodeURIComponent(bid)+'&top=6').then(function(r){return r.json();}).then(function(d){
        var arr=(d&&(d.results||d.hits||d.items))||(Array.isArray(d)?d:[]);
        arr=arr.filter(function(s){ return (s.id||s.site_id)!==bid; });
        if(!arr.length){ sim.innerHTML='<span>似た例が見つかりませんでした</span>'; return; }
        sim.innerHTML=arr.map(function(s){
          var sid=s.id||s.site_id; if(!sid) return '';
          var host=''; try{ host=new URL(s.url).hostname.replace(/^www\\./,''); }catch(_){ host=''; }
          return '<a href="/img/'+sid+'/fullpage" target="_blank" style="flex:0 0 104px;text-decoration:none;text-align:center">'
            +'<img src="/img/'+sid+'/firstview" style="width:100%;border:1px solid #ddd;border-radius:8px;display:block" loading="lazy">'
            +'<span style="font-size:10px;color:#666">'+esc(host)+'</span></a>';
        }).join('');
      }).catch(function(){ sim.innerHTML='<span style="color:#c00">読み込み失敗</span>'; });
    }
    // ⭐お気に入り部品（fav_*.html）を別タブリンクで並べる
    fetch('/api/camps').then(function(r){return r.json();}).then(function(d){
      var arr=(d&&(d.items||d.camps))||(Array.isArray(d)?d:[]);
      arr=arr.filter(function(x){ return x.file&&x.file.indexOf('fav_')===0; }).slice(0,20);
      var fv=ov.querySelector('#__ce_reffav');
      if(!arr.length){ fv.innerHTML='<span>まだありません（右クリック→⭐このセクションをお気に入り で貯まります）</span>'; return; }
      fv.innerHTML=arr.map(function(x){
        var nm=x.name||x.title||x.file;
        return '<a href="/camp/'+encodeURIComponent(x.file)+'" target="_blank" style="text-decoration:none;background:#fff7e0;border:1px solid #f0dfa8;border-radius:7px;padding:3px 9px;color:#7a5c00">⭐ '+esc((''+nm).slice(0,18))+'</a>';
      }).join('');
    }).catch(function(){ var fv=ov.querySelector('#__ce_reffav'); if(fv) fv.innerHTML='<span style="color:#c00">読み込み失敗</span>'; });
    // 💡 AIアドバイス（テキストのみ＝修正エンジンで安く。適用はユーザーの手＝勉強を兼ねる）
    var advBtn=ov.querySelector('#__ce_refadv');
    if(secEl) advBtn.addEventListener('click',function(){
      var out=ov.querySelector('#__ce_refadvout');
      advBtn.disabled=true; advBtn.textContent='💡 AIが考えています…（10〜30秒）';
      var sh='';
      try{
        var cl=secEl.cloneNode(true);
        [].slice.call(cl.querySelectorAll('script,style')).forEach(function(n){ n.remove(); });
        [].slice.call(cl.querySelectorAll('*')).forEach(function(n){ if(n.id&&n.id.indexOf('__ce')===0) n.remove(); });
        sh=cl.outerHTML;
      }catch(_){ sh=secEl.outerHTML; }
      fetch('/api/section_advice',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({html:sh, base:refBaseId(), file:FILE})})
      .then(function(r){return r.json();}).then(function(d){
        advBtn.disabled=false; advBtn.textContent='💡 もう一度アドバイスをもらう（AI・数円）';
        out.style.display='block';
        out.textContent=d.ok?d.advice:('失敗：'+(d.message||''));
      }).catch(function(){
        advBtn.disabled=false; advBtn.textContent='💡 このセクションの演出アドバイスをもらう（AI・数円）';
        out.style.display='block'; out.textContent='通信エラー';
      });
    });
    renderBase();
  }
  // ===== 🧐デザイン指摘：セクションのスクショをAIに見せて指摘4つ（数値つき・効果順） =====
  // 指摘の記憶の引っ越し（受け取り側）：AI修正で新しい版ファイルに移った直後なら、
  // 前の版の指摘の記憶を新ファイル名のキーへコピーする＝修正後も同じ指摘で✅確認できる。
  try{
    var _mv=JSON.parse(localStorage.getItem('__ce_dcq_mv')||'null');
    if(_mv&&_mv.from&&_mv.from!==FILE&&(Date.now()-(_mv.ts||0))<600000){
      var _ma=JSON.parse(localStorage.getItem('__ce_dcq')||'{}')||{};
      var _mch=false;
      Object.keys(_ma).forEach(function(k){
        if(k.indexOf(_mv.from+'|')===0){
          var nk=FILE+k.slice(_mv.from.length);
          if(!_ma[nk]){ _ma[nk]=_ma[k]; _mch=true; }
        }
      });
      if(_mch) localStorage.setItem('__ce_dcq',JSON.stringify(_ma));
    }
    if(_mv) localStorage.removeItem('__ce_dcq_mv');
  }catch(_){}
  function dcqOpen(secEl){
    if(!secEl){ if(msg) msg.textContent='セクションの中で右クリックしてください'; return; }
    var kind=secEl.tagName.toLowerCase(), idx=0;
    if(kind==='section'){
      // サーバー側(shot_part)と同じ数え方＝「入れ子でないsection」の中での順番
      var secs=[].slice.call(document.querySelectorAll('section')).filter(function(s){ return !s.parentElement.closest('section'); });
      idx=secs.indexOf(secEl);
      if(idx<0){ if(msg) msg.textContent='このセクションは対象外です（入れ子セクション）'; return; }
    }
    // 前回の指摘の記憶（localStorage・セクションごと）＝うっかり閉じても再課金なしで見返せる
    var dcqKey=FILE+'|'+kind+'|'+idx;
    function dcqAll(){ try{ var a=JSON.parse(localStorage.getItem('__ce_dcq')||'{}'); return (a&&typeof a==='object')?a:{}; }catch(_){ return {}; } }
    function dcqStore(text,model){ try{ var a=dcqAll(); a[dcqKey]={t:text,m:model,d:new Date().toLocaleString('ja-JP')}; localStorage.setItem('__ce_dcq',JSON.stringify(a)); }catch(_){} }
    function dcqDel(){ try{ var a=dcqAll(); delete a[dcqKey]; localStorage.setItem('__ce_dcq',JSON.stringify(a)); }catch(_){} }
    var ov=document.createElement('div'); ov.id='__ce_pk';
    ov.innerHTML='<div class="bx" style="max-width:640px"><span class="cl" id="__ce_dcqx">×</span><h4>🧐 デザイン指摘（プロの目線・指摘4つ）</h4>'
      +(_dirty?'<div style="font-size:12px;color:#c00;background:#fde8e8;border-radius:7px;padding:6px 10px;margin-bottom:8px">⚠ 未保存の変更があります。スクショは保存済みの状態で撮るので、先に💾保存してから実行してください（今のままだと古い見た目に指摘が付きます）</div>':'')
      +'<div style="font-size:12px;color:#666;margin-bottom:8px">このセクションのスクショを撮ってAIに見せ、「どこを・なぜ・どう直すか（数値つき）」を効果の大きい順に4つ出します。余白・文字階層・色比率・構図・コピーの5観点で採点します。</div>'
      +'<button class="go2" id="__ce_dcqgo" style="background:#0b6bcb;margin:0">🧐 指摘をもらう（AI・約10円／20〜60秒）</button>'
      +'<div id="__ce_dcqout" style="display:none;white-space:pre-wrap;font-size:13px;line-height:1.9;background:#f2f7ff;border:1px solid #cfe0f7;border-radius:9px;padding:10px 12px;margin-top:8px"></div>'
      +'<div id="__ce_dcqchkout" style="display:none;white-space:pre-wrap;font-size:13px;line-height:1.9;background:#f2fbf4;border:1px solid #bfe6c8;border-radius:9px;padding:10px 12px;margin-top:8px"></div>'
      +'<div style="display:flex;align-items:center;gap:8px;margin-top:4px;flex-wrap:wrap">'
      +'<div id="__ce_dcqmdl" style="display:none;font-size:10.5px;color:#999;flex:1"></div>'
      +'<button id="__ce_dcqfix" style="display:none;border:none;background:#0b6bcb;color:#fff;border-radius:6px;padding:2px 10px;font-size:11px;cursor:pointer;font-weight:700">🔧 この指摘どおりに直して（AI・数円）</button>'
      +'<button id="__ce_dcqchk" style="display:none;border:1px solid #9ed0a8;background:#f2fbf4;color:#1a7a33;border-radius:6px;padding:2px 9px;font-size:11px;cursor:pointer;font-weight:700">✅ 直ったか確認（安いAI・約2円）</button>'
      +'<button id="__ce_dcqdel" style="display:none;border:1px solid #e0b4b4;background:#fff5f5;color:#c00;border-radius:6px;padding:2px 9px;font-size:11px;cursor:pointer">🗑 この指摘を削除</button>'
      +'</div></div>';
    // 指摘を見ながら後ろのページを直せるように：暗幕なし＋外側は素通し（クリックが下に届く）。
    // 閉じるのは✕だけ＝ドラッグや別要素の選択で消えない（h4掴みで移動は従来どおり）。
    var oldPk=document.getElementById('__ce_pk'); if(oldPk) oldPk.remove();  // 重なり防止
    ov.style.pointerEvents='none'; ov.style.background='transparent';
    document.body.appendChild(ov);
    var bx=ov.querySelector('.bx'); bx.style.pointerEvents='auto';
    ov.addEventListener('click',function(e){ if(e.target.id==='__ce_dcqx'){ ov.remove(); } });
    var go=ov.querySelector('#__ce_dcqgo'), out=ov.querySelector('#__ce_dcqout'),
        md=ov.querySelector('#__ce_dcqmdl'), delBtn=ov.querySelector('#__ce_dcqdel'),
        chkBtn=ov.querySelector('#__ce_dcqchk'), chkOut=ov.querySelector('#__ce_dcqchkout'),
        fixBtn=ov.querySelector('#__ce_dcqfix');
    // セクションHTMLのスナップショット（編集UIの断片を除いて送る）＝指摘・確認の両方で使う
    function dcqSecHtml(){
      try{
        var cl=secEl.cloneNode(true);
        [].slice.call(cl.querySelectorAll('script,style')).forEach(function(n){ n.remove(); });
        [].slice.call(cl.querySelectorAll('*')).forEach(function(n){ if(n.id&&n.id.indexOf('__ce')===0) n.remove(); });
        return cl.outerHTML;
      }catch(_){ return secEl.outerHTML; }
    }
    function dcqShow(text,label){
      out.style.display='block'; out.textContent=text;
      md.style.display='block'; md.textContent=label||'';
      delBtn.style.display='inline-block';
      chkBtn.style.display='inline-block';
      if(kind==='section') fixBtn.style.display='inline-block';  // AI修正は<section>だけ対象（サーバー仕様）
    }
    var dcqSaved=dcqAll()[dcqKey];
    if(dcqSaved&&dcqSaved.t){
      dcqShow(dcqSaved.t,'前回の指摘（'+(dcqSaved.d||'')+'・'+(dcqSaved.m||'')+'）※表示は無料');
      go.textContent='🧐 新しく指摘をもらい直す（AI・約10円）';
      if(dcqSaved.r){ chkOut.style.display='block'; chkOut.textContent='✅ 確認結果（'+(dcqSaved.rd||'')+'）\\n'+dcqSaved.r; }
    }
    delBtn.addEventListener('click',function(ev){
      ev.stopPropagation();
      dcqDel();
      out.style.display='none'; md.style.display='none'; delBtn.style.display='none';
      chkBtn.style.display='none'; chkOut.style.display='none'; fixBtn.style.display='none';
      go.textContent='🧐 指摘をもらう（AI・約10円／20〜60秒）';
      if(msg) msg.textContent='保存していた指摘を削除しました';
    });
    // 🔧この指摘どおりに直す＝指摘文をそのまま修正エンジンへ（px・色コードはAIには一番正確な指示）。
    // submit()が「今のDOMを保存→AI修正→進捗トースト→完了で開き直し」まで全部やる。
    // keep_text=ON＝✨おしゃれ化と同じテキスト保全ゲート（文章・画像が消えたら自動リトライ→中止）
    fixBtn.addEventListener('click',function(ev){
      ev.stopPropagation();
      var cur=dcqAll()[dcqKey];
      if(!cur||!cur.t){ if(msg) msg.textContent='先に🧐指摘をもらってください'; return; }
      if(!confirm('AIがこの指摘をまとめて直します（セクション修正・数円）。\\n終わったらページが開き直されます。実行しますか？')) return;
      var ins='以下のデザイン指摘リストのとおりに、このセクションを修正して。\\n'
        +'・指摘に書かれた数値（px・色コード・行間など）をそのまま正確に使う\\n'
        +'・指摘されていない部分のデザインは変えない\\n'
        +'・文章・画像は1つも消さない\\n\\n'+cur.t;
      ov.remove();
      submit(idx, ins, true, '', 'dcfix');  // 🔧指摘の修正は専用エンジン（⚙設定の3役・カンプ修正エンジンとは別）
    });
    // ✅直ったか確認＝前回の指摘だけを✅❌判定（新しい指摘は出ない）。安いモデル(Luna)で約2円
    chkBtn.addEventListener('click',function(ev){
      ev.stopPropagation();
      var cur=dcqAll()[dcqKey];
      if(!cur||!cur.t){ if(msg) msg.textContent='先に🧐指摘をもらってください'; return; }
      if(_dirty && !confirm('未保存の変更があります。スクショは保存済みの状態で撮るので、先に💾保存してから確認するのが正確です。このまま実行しますか？')) return;
      chkBtn.disabled=true; chkBtn.textContent='✅ AIが見比べています…（20〜40秒）';
      fetch('/api/design_critique',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE, kind:kind, idx:idx, html:dcqSecHtml(), mode:'recheck', prev:cur.t})})
      .then(function(r){return r.json();}).then(function(d){
        chkBtn.disabled=false; chkBtn.textContent='✅ 直ったか確認（安いAI・約2円）';
        chkOut.style.display='block';
        if(d.ok){
          var when=new Date().toLocaleString('ja-JP');
          chkOut.textContent='✅ 確認結果（'+when+'）\\n'+d.critique;
          try{ var a2=dcqAll(); if(a2[dcqKey]){ a2[dcqKey].r=d.critique; a2[dcqKey].rd=when; localStorage.setItem('__ce_dcq',JSON.stringify(a2)); } }catch(_){}
        } else { chkOut.textContent='失敗：'+(d.message||''); }
      }).catch(function(){
        chkBtn.disabled=false; chkBtn.textContent='✅ 直ったか確認（安いAI・約2円）';
        chkOut.style.display='block'; chkOut.textContent='通信エラー';
      });
    });
    go.addEventListener('click',function(){
      go.disabled=true; go.textContent='🧐 スクショを撮ってAIが見ています…（20〜60秒）';
      fetch('/api/design_critique',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:FILE, kind:kind, idx:idx, html:dcqSecHtml()})})
      .then(function(r){return r.json();}).then(function(d){
        go.disabled=false; go.textContent='🧐 もう一度指摘をもらう（AI・約10円）';
        if(d.ok){
          dcqStore(d.critique,d.model||'');           // 新しい指摘＝古い確認結果(r)もここでリセットされる
          chkOut.style.display='none';
          dcqShow(d.critique,'使ったAI: '+(d.model||''));
        }
        else{ out.style.display='block'; out.textContent='失敗：'+(d.message||''); }
      }).catch(function(){
        go.disabled=false; go.textContent='🧐 指摘をもらう（AI・約10円／20〜60秒）';
        out.style.display='block'; out.textContent='通信エラー';
      });
    });
  }
  // ===== 右クリックで、その要素に直接アニメ/指示/改善案を出す =====
  var curMenu=null, curEl=null, lastMenuPos=null;  // lastMenuPos=前回ドラッグで動かした位置を記憶
  // ===== 複数選択（Ctrl+右クリックで追加）＝サイズ・動き・削除をまとめて掛ける =====
  var selEls=[];  // 選択中の全要素（curEl=最後に選んだ主役。単独選択時は[curEl]と同じ）
  // ===== 🧩 グループ（Ctrl+G＝まとめる／Ctrl+Shift+G＝ほどく・2026-07-29）=====
  //   ★divで囲む方式は採らない：カンプの部品は自由配置(position:absolute)が多く、囲んだ瞬間に位置が崩れる。
  //   印（data-cegid）で結ぶだけ＝DOMは1ミリも変えずに「1つ掴めば仲間も一緒に動く」を実現する。
  // 画面まん中下に2秒だけ出る通知（編集バーの小さい文字だと気づかれないため・id は __ce 始まり＝保存に残らない）
  function ceFlash(txt){
    var old=document.getElementById('__ce_flash'); if(old) old.remove();
    var n=document.createElement('div'); n.id='__ce_flash'; n.textContent=txt;
    n.style.cssText='position:fixed;left:50%;bottom:96px;transform:translateX(-50%);z-index:2147483040;'
      +'background:#1d1d1f;color:#fff;border-radius:999px;padding:11px 22px;font:13.5px/1.5 system-ui,sans-serif;'
      +'font-weight:700;box-shadow:0 12px 32px rgba(0,0,0,.38);pointer-events:none;max-width:88vw;text-align:center';
    document.body.appendChild(n);
    setTimeout(function(){ if(n.parentElement) n.remove(); }, 2400);
  }
  // グループに入ったものを数秒だけ光らせる＝「どれが入ったか」を目で確かめられる
  function groupBlink(list){
    list.forEach(function(n){
      var had=n.style.outline;
      try{ n.style.setProperty('outline','2px dashed #1a7f37'); n.style.setProperty('outline-offset','2px'); }catch(_){}
      setTimeout(function(){ if(had) n.style.outline=had; else { n.style.removeProperty('outline'); n.style.removeProperty('outline-offset'); } }, 1800);
    });
  }
  // ★文字をドラッグでなぞった範囲（＝ブラウザのテキスト選択）からも拾う（2026-07-29）。
  //   アーチにした見出しなどを「なぞって選んで Ctrl+G」が一番自然なのに、ツールの複数選択(selEls)とは
  //   別物なので「2つ以上を選んでください」と言われてしまっていた（ユーザー報告）。
  function _selFromText(){
    var sel=null; try{ sel=window.getSelection(); }catch(_){ return []; }
    if(!sel||!sel.rangeCount||sel.isCollapsed) return [];
    var rg=sel.getRangeAt(0);
    var root=rg.commonAncestorContainer;
    if(root&&root.nodeType===3) root=root.parentElement;
    if(!root||!root.querySelectorAll) return [];
    var out=[];
    [].slice.call(root.querySelectorAll('*')).forEach(function(n){
      if(n.closest('[id^=__ce]')) return;
      if(n.children.length) return;                       // いちばん内側の要素だけ（入れ物は取らない）
      if(!(n.textContent||'').trim()) return;
      try{ if(rg.intersectsNode(n)) out.push(n); }catch(_){ }
    });
    // ★親子で両方拾ったら親だけ残す（子まで印を付けると移動量が二重に足されてバラける・実測）
    out=out.filter(function(n){ return !out.some(function(p){ return p!==n && p.contains&&p.contains(n); }); });
    return out;
  }
  function groupSel(){
    var list=(selEls&&selEls.length>1)?selEls.slice():[];
    if(list.length<2){
      var tl=_selFromText();
      if(tl.length>1) list=tl;
    }
    if(list.length<2){
      if(msg) msg.textContent='2つ以上を選んでから Ctrl+G を押してください（Ctrlを押しながらクリック／余白からドラッグで囲む／文字はドラッグでなぞる）';
      ceFlash('🧩 2つ以上を選んでから Ctrl+G を押してください');
      return;
    }
    // 既にグループの印を持つものが混ざっていたら、そのIDに合流する＝あとから1つ足せる
    var gid=null;
    list.some(function(n){ var g=n.getAttribute('data-cegid'); if(g){ gid=g; return true; } return false; });
    if(!gid) gid='g'+Date.now().toString(36)+Math.random().toString(36).slice(2,5);
    var n0=0;
    list.forEach(function(n){
      try{ pushUndo(n); }catch(_){ }
      n.setAttribute('data-cegid', gid); n0++;
      // ⚠子孫にも印を付けると、親子の両方に移動量が足されて二重に動く（実測でバラけた）。印は選んだ本人だけ。
    });
    markDirty();
    try{ var _s=window.getSelection(); if(_s&&_s.removeAllRanges) _s.removeAllRanges(); }catch(_){ }  // なぞった青を消す
    groupBlink(list);
    var m='🧩 '+list.length+'個をグループにしました（どれか1つを掴めば全部いっしょに動きます）';
    if(msg) msg.textContent=m+'／Ctrl+Shift+G でほどく';
    ceFlash(m);
  }
  function groupMates(el){
    var g=el&&el.getAttribute&&el.getAttribute('data-cegid');
    if(!g) return null;
    var l=[].slice.call(document.querySelectorAll('[data-cegid="'+g+'"]'));
    // ★親も同じグループにいる子は外す：親が動けば子も一緒に動くので、両方に足すと二重に動いてバラける。
    //   外した結果が1個（＝みんなを含む親だけ）でも、それを動かせば中身ごと動くので返す。
    l=l.filter(function(n){ return !l.some(function(p){ return p!==n && p.contains&&p.contains(n); }); });
    return l.length?l:null;
  }
  function ungroupSel(){
    var base=(selEls&&selEls.length)?selEls.slice():(curEl?[curEl]:[]);
    var ids={}, n=0;
    base.forEach(function(x){ var g=x&&x.getAttribute&&x.getAttribute('data-cegid'); if(g) ids[g]=1; });
    var keys=Object.keys(ids);
    if(!keys.length){
      if(msg) msg.textContent='グループになっているものを選んでから Ctrl+Shift+G を押してください';
      ceFlash('🧩 グループになっているものを選んでから Ctrl+Shift+G');
      return;
    }
    keys.forEach(function(g){
      [].slice.call(document.querySelectorAll('[data-cegid="'+g+'"]')).forEach(function(x){ try{ pushUndo(x); }catch(_){ } x.removeAttribute('data-cegid'); n++; });
    });
    markDirty();
    var m='🧩 グループを解除しました（'+n+'個・それぞれ別々に動かせます）';
    if(msg) msg.textContent=m;
    ceFlash(m);
  }
  document.addEventListener('keydown',function(e){
    if(!(e.ctrlKey||e.metaKey)||e.altKey) return;
    if((e.key||'').toLowerCase()!=='g') return;
    var ae=document.activeElement;
    if(ae&&(ae.tagName==='INPUT'||ae.tagName==='TEXTAREA'||ae.isContentEditable)) return;
    e.preventDefault(); e.stopPropagation();
    if(e.shiftKey) ungroupSel(); else groupSel();
  },true);
  var _forceEl=null;  // ⬆外側選択用：次のcontextmenuでpickTargetを使わずこの要素を選ぶ
  function eachSel(fn){ (selEls.length?selEls:(curEl?[curEl]:[])).forEach(fn); }
  try{ lastMenuPos=JSON.parse(localStorage.getItem('__ce_menupos')||'null'); }catch(_){}  // 再読込しても覚える
  function closeMenu(){
    // ★Ctrl+クリックで選んでいる最中は選択を消さない（2026-07-31）。
    //   「どこかをクリックしたらメニューを閉じる」処理がここを呼ぶため、2個目を選んだ瞬間に
    //   1個目が外れて **いつまでも2個にならない**＝Ctrl+G が使えなかった（実測で確認）。
    if(window.__ceCtrlSel && Date.now() < window.__ceCtrlSel){
      if(curMenu){ curMenu.remove(); curMenu=null; }
      return;
    }
    hideHandles(); window.__ceDblSel=null;
    // 赤枠はメニュー用＝背景パネルが開いている時だけ残す（★行末コメントにすると同じ行の後ろを丸ごと殺す）
    if(!document.getElementById('__ce_bgp')) grabHintHide();
    if(curMenu){curMenu.remove();curMenu=null;} if(curEl){ stopAnim(curEl); clearPreviewStyle(curEl); curEl.classList.remove('__ce_sel');curEl=null;} selEls.forEach(function(x){ stopAnim(x); clearPreviewStyle(x); x.classList.remove('__ce_sel'); }); selEls=[]; curAnim=null; curP={};
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
  // 複数選択に対応した削除：2個以上なら1回の確認でまとめて消す
  function removeSelected(){
    if(selEls.length>1){
      if(!confirm(selEls.length+'個の要素をまとめて消しますか？\\n※保存すると元に戻せません（保存前ならページ再読込でキャンセルできます）')) return;
      var list=selEls.slice(); closeMenu();
      list.forEach(function(x){ try{ x.remove(); }catch(_){} });
      markDirty(); msg.textContent=list.length+'個の要素を消しました（保存で確定）';
    } else { removeEl(curEl); }
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
  // ＼あしらい／・💬吹き出し用のCSSを1回だけページに注入（idに__ce系を使わない＝保存で残る）
  function ensureDecoCss(){
    // 太さは--ce-emphw（要素ごとに変えられる・既定3px）。古い保存ファイルは3px固定CSSが
    // 焼き込まれているので、--ce-emphwを知らない古いCSSを見つけたら新しい定義に差し替える。
    var css='.ce_emph{position:relative;display:inline-block;padding:0 1.5em;}'
      +'.ce_emph::before,.ce_emph::after{content:"";position:absolute;top:var(--ce-emphy,26%);width:var(--ce-emphw,3px);height:var(--ce-emphh,0.72em);background:var(--ce-emphc,#f0c14b);border-radius:2px;}'
      +'.ce_emph::before{left:.15em;transform:rotate(-30deg);box-shadow:0.5em 0.3em 0 var(--ce-emphc,#f0c14b);}'
      +'.ce_emph::after{right:.15em;transform:rotate(30deg);box-shadow:-0.5em 0.3em 0 var(--ce-emphc,#f0c14b);}'
      +'.ce_bubble{position:relative;display:inline-block;padding:.45em 1.1em;border:2px solid currentColor;border-radius:999px;}'
      +'.ce_bubble::before{content:"";position:absolute;left:1.4em;bottom:-0.64em;border:0.33em solid transparent;border-top-color:currentColor;}'
      +'.ce_bubble::after{content:"";position:absolute;left:1.4em;bottom:-0.36em;border:0.33em solid transparent;border-top-color:var(--ce-bubblebg,#fff);}'
      // 💬がやがや演出：要素に重ねた記号がポコポコ浮かんでは消えるループ（純CSS＝保存版でもJSなしで動く）
      +'.ce_gaya{position:absolute;inset:0;pointer-events:none;z-index:3;}/*ce_gaya2*/'
      +'.ce_gaya span{position:absolute;opacity:0;font-weight:700;background:rgba(255,255,255,.92);border-radius:999px;padding:.16em .34em;box-shadow:0 3px 10px rgba(0,0,0,.16);animation:ce_gaya_pop 3.6s ease-in-out infinite;}'
      +'@keyframes ce_gaya_pop{0%{opacity:0;transform:translateY(10px) scale(.6)}12%{opacity:1;transform:translateY(0) scale(1.12)}20%{transform:scale(1)}55%{opacity:1}75%{opacity:0;transform:translateY(-18px) scale(1.06)}100%{opacity:0}}';
    var st=document.getElementById('ce_deco_css');
    if(st){ if(st.textContent.indexOf('ce_gaya2')<0) st.textContent=css; return; }
    st=document.createElement('style'); st.id='ce_deco_css'; st.textContent=css;
    document.head.appendChild(st);
  }
  // 🌈 文字のならびをアーチ状に（AIなし）。1文字ずつspanに割り、真ん中を持ち上げ＋端を傾ける。
  // 元のHTMLは data-cearcorig に控える＝「戻す」で完全復元。強さ(px)は data-cearc。
  // ⚠中の装飾タグ（マーカー等）はアーチ中は外れる（戻すと復活する）。
  function arcApply(el, amp){
    var orig=el.getAttribute('data-cearcorig');
    if(amp<=0){
      if(orig!==null){ el.innerHTML=orig; el.removeAttribute('data-cearcorig'); el.removeAttribute('data-cearc'); markDirty(); }
      return;
    }
    if(orig===null){ el.setAttribute('data-cearcorig', el.innerHTML); orig=el.innerHTML; }
    // <br>入りでも行ごとにアーチをかける（改行と両立）
    var out=orig.split(/<br[^>]*>/i).map(function(ln){
      var d=document.createElement('div'); d.innerHTML=ln;
      var chars=[].slice.call(d.textContent);  // 空白も潰さない＝行頭スペースでの位置調整がアーチでも効く
      var N=chars.length; if(!N) return '';
      var html='';
      for(var i=0;i<N;i++){
        var t=(N>1)? i/(N-1) : 0.5;
        var y=-amp*Math.sin(Math.PI*t);                 // 真ん中ほど上へ
        var r=(t-0.5)*2*Math.min(14, amp*0.9);          // 端ほど傾く（上限14度）
        var ch=(chars[i]===' '||chars[i]==='\\u00a0')?'&nbsp;':esc(chars[i]);
        // background等の打ち消し＝ページCSSに「h2 span{背景}」のような付箋風ルールがあっても巻き込まれない
        html+='<span style="display:inline-block;background:transparent;box-shadow:none;padding:0;margin:0;transform:translateY('+y.toFixed(1)+'px) rotate('+r.toFixed(1)+'deg)">'+ch+'</span>';
      }
      return html;
    }).join('<br>');
    el.innerHTML=out;
    // グラデ文字(background-clip:text)は1文字ずつspanに割ると切り抜きが壊れ、背景が箱で見える
    // → 背景を外し、文字色は今見えている色で単色化する（透明のまま背景だけ外すと文字が消えるため）
    try{
      var _ac=getComputedStyle(el);
      if(((_ac.webkitBackgroundClip||_ac.backgroundClip||'')+'').indexOf('text')>=0){
        el.style.setProperty('background','none','important');
        var _fc=_ac.webkitTextFillColor||'';
        if(_fc.indexOf('transparent')>=0||_fc.indexOf('rgba(0, 0, 0, 0)')>=0) el.style.setProperty('-webkit-text-fill-color', _ac.color, 'important');
      }
    }catch(_){}
    el.setAttribute('data-cearc', amp);
    markDirty();
  }
  // ★「数字を直したら小さい文字になる」の対策（2026-07-21）。
  //   クローンの実績数字は <div class="result"><strong class="result-number">9</strong>
  //   <span class="result-label">開設から9年</span></div> のように、**大きさを持っているのは中のタグ**。
  //   従来は el.innerHTML=文字 で丸ごと書き換えていたので strong/span が消え、
  //   入れ物の16px明朝が出てしまっていた（実測で確認）。
  //   → 中の文字ノードだけを1対1で入れ替えれば、タグ＝見た目はそのまま残る。
  // ★入力した空白を画面にも出す（2026-07-29）。HTMLは「行頭・行末・連続した半角スペース」を潰すので、
  //   打っても消えたように見えていた。潰される分だけ NBSP（改行しない空白）に置き換えて見た目どおり残す。
  //   文中の1個の半角スペースはそのまま（NBSPにすると折り返せなくなるため）。
  function _keepSpaces(s){
    var NB=String.fromCharCode(160);
    function rep(m){ return Array(m.length+1).join(NB); }
    return String(s==null?'':s).split(/\\n/).map(function(ln){
      return ln.replace(/^ +/,rep).replace(/ +$/,rep).replace(/ {2,}/g,rep);
    }).join('\\n');
  }
  function _brTextNodes(el){
    var out=[], w=document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null), n;
    while((n=w.nextNode())){
      if(!(n.nodeValue||'').trim()) continue;
      if(n.parentElement && n.parentElement.closest('[id^="__ce"]')) continue;
      out.push(n);
    }
    return out;
  }
  function _brKeepTags(el, val){
    var nodes=_brTextNodes(el);
    if(nodes.length<2) return false;                       // 文字が1か所だけの要素は従来どおりでよい
    // ★trimしない：打った空白（行頭のインデント等）を消さずに、潰れる分だけNBSPにして残す
    var lines=_keepSpaces(val).split(/\\n/).filter(function(s){ return s.trim()!==''; });
    if(lines.length!==nodes.length) return false;          // 行数が変わったら1対1にできない
    nodes.forEach(function(t,i){
      t.nodeValue=lines[i];
      // 🔢カウントアップの目標値も一緒に直す（data-countが古いままだと再生で元の数字に戻る）
      var p=t.parentElement;
      while(p && p!==el.parentElement){
        if(p.hasAttribute && p.hasAttribute('data-count')){
          var num=(lines[i].match(/-?[\\d,.]*\\d/)||[''])[0].replace(/,/g,'');
          if(num) p.setAttribute('data-count',num);
          break;
        }
        p=p.parentElement;
      }
    });
    return true;
  }
  // ✏ 文字を編集：改行・大きさ・フォント・色をこの1枠でまとめて変える（すべてAIなし・即反映）。
  function openBreakEditor(el){
    if(!el){ msg.textContent='対象の要素がありません'; return; }
    // ★1文字ずつに割られた文字（スタッガー／タイプライター／にじみ出る等）を掴んだ状態で開くと、
    //   その1文字だけが編集対象になってしまう。文字列ごと直せるよう、まとめ役の親まで戻す
    //   （2026-07-30・要望）。判定はクラス名ではなく「短い兄弟が並ぶ」で動的に見るので、
    //   クローン元サイトが自前で割ったものにも効く。
    var _bg=0;
    while(el && _charFrag(el) && el.parentElement && el.parentElement!==document.body && _bg++<8){
      el=el.parentElement;
    }
    // Alt+ドラッグで文字を選択中なら「その選択文字だけ」を編集対象にする（spanで包む・2026-07-20）
    if(window.__ceSel&&window.__ceSel.has&&window.__ceSel.has()&&window.__ceSel.wrapSpan){
      var _w=window.__ceSel.wrapSpan();
      if(_w&&el.contains(_w)) el=_w;
    }
    // ➖区切り線（線span＋ラベルspanの入れ物）は、入れ物ごと書き換えると線が消える（実際に起きた）
    // → 編集対象をラベルspanに差し替える。ラベルが無い（線だけ）なら空ラベルを作ってそこを編集。
    if(el.getAttribute && el.getAttribute('data-cediv')!=null){
      var _lb=[].slice.call(el.querySelectorAll('span')).filter(function(s){ return (s.textContent||'').trim(); })[0];
      if(!_lb){ _lb=document.createElement('span'); _lb.setAttribute('style','font-size:13px;color:#888;letter-spacing:.05em'); _lb.textContent='ラベル'; el.appendChild(_lb); }
      el=_lb;
    }
    var cur=(el.innerText||el.textContent||'').replace(/\\u200b/g,'');
    var FONTS=FONT_LIST;  // 🅰 共通リスト（OS標準＋Google Fonts）を使う
    var opts=FONTS.map(function(f){return '<option value="'+f[0].replace(/"/g,'&quot;')+'">'+f[1]+'</option>';}).join('');
    var ov=document.createElement('div'); ov.id='__ce_pk';
    ov.innerHTML='<div class="bx"><span class="cl" id="__ce_pkx">×</span><h4>✏ 文字を編集（AIなし・即反映）</h4>'
      +'<div style="font-size:12px;color:#888;margin-bottom:8px">改行したい所で Enter を押して「改行を反映」。大きさ・フォント・色はその場で反映します。※1文字ずつの動きは外れます<br><b style="color:#1a7f37">閉じるのは右上の × だけ</b>（外側をクリックしても閉じません／タイトルを掴めば動かせます）</div>'
      +'<textarea id="__ce_brta" style="width:100%;height:120px;font-size:15px;padding:10px;border:1px solid #d0d0d5;border-radius:8px;font-family:inherit;resize:vertical;box-sizing:border-box"></textarea>'
      +'<button class="go2" id="__ce_brapply" style="background:#1a7f37;margin-top:8px">✅ 改行を反映</button>'
      +'<div style="border-top:1px solid #eee;margin:14px 0 0"></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:12px 0 6px">🔡 文字の大きさ <span id="__ce_brfsnow" style="font-weight:400;color:#666;font-size:11.5px"></span></div>'
      +'<div style="display:flex;gap:6px"><button class="go2" data-fs="1.1" style="background:#0b6bcb;margin:0;flex:1">＋ 大きく</button><button class="go2" data-fs="0.9" style="background:#0b6bcb;margin:0;flex:1">－ 小さく</button><button class="go2" data-fs="0" style="background:#888;margin:0">⟲</button></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">↕ 行間（ラインハイト） <span id="__ce_brlhnow" style="font-weight:400;color:#666;font-size:11.5px"></span></div>'
      +'<div style="display:flex;gap:6px"><button class="go2" data-lh="0.15" style="background:#0b6bcb;margin:0;flex:1">＋ 広く</button><button class="go2" data-lh="-0.15" style="background:#0b6bcb;margin:0;flex:1">－ 狭く</button><button class="go2" data-lhr="1" style="background:#888;margin:0">⟲</button></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">🅰 フォント</div>'
      +'<select id="__ce_brff" style="width:100%;font-size:13px;padding:9px;border:1px solid #d0d0d5;border-radius:8px;font-family:inherit">'+opts+'</select>'
      +'<div id="__ce_brffnow" style="font-size:11.5px;color:#555;margin-top:5px"></div>'
      +'<div style="display:flex;gap:6px;margin-top:5px"><button class="go2" id="__ce_brffall" style="background:#7c3aed;margin:0;flex:1">🅰 このフォントをページ全部に適用</button>'
      +'<button class="go2" id="__ce_brffallr" title="ページ全部への適用をやめる（個別に指定した分は戻りません）" style="background:#888;margin:0">⟲</button></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">🎨 文字の色</div>'
      +'<div style="display:flex;gap:8px;align-items:center"><input type="color" id="__ce_brcol" style="width:54px;height:38px;padding:2px;border:1px solid #d0d0d5;border-radius:8px;cursor:pointer"><button class="go2" id="__ce_brcolr" style="background:#888;margin:0;flex:1">⟲ 色を元に戻す</button></div>'
      +'<div id="__ce_brsw" style="margin-top:6px"></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">⇔ 字間（レタースペーシング）</div>'
      +'<div style="display:flex;gap:6px"><button class="go2" data-ls="0.5" style="background:#0b6bcb;margin:0;flex:1">＋ 広く</button><button class="go2" data-ls="-0.5" style="background:#0b6bcb;margin:0;flex:1">－ 狭く</button><button class="go2" data-lsr="1" style="background:#888;margin:0">⟲</button></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">〰 点線の下線（手描き風の演出）</div>'
      +'<div style="display:flex;gap:8px;align-items:center"><input type="color" id="__ce_brudot" style="width:54px;height:38px;padding:2px;border:1px solid #d0d0d5;border-radius:8px;cursor:pointer"><button class="go2" id="__ce_brudotb" style="background:#0b6bcb;margin:0;flex:1">〰 下線をつける／外す</button></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">⤴ 傾き（少し斜めに）</div>'
      +'<div style="display:flex;gap:6px"><button class="go2" data-rot="-2" style="background:#0b6bcb;margin:0;flex:1">↖ 左に傾く</button><button class="go2" data-rot="2" style="background:#0b6bcb;margin:0;flex:1">↗ 右に傾く</button><button class="go2" data-rotr="1" style="background:#888;margin:0">⟲</button></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">＼ あしらい ／（手書き風の飾り）</div>'
      +'<div style="display:flex;gap:6px;flex-wrap:wrap"><button class="go2" id="__ce_bremph" style="background:#b8860b;margin:0;flex:1">＼ 左右の強調線 ／</button><button class="go2" id="__ce_brbub" style="background:#0b6bcb;margin:0;flex:1">💬 吹き出しにする</button></div>'
      +'<div style="display:flex;gap:6px;align-items:center;margin-top:5px;font-size:11.5px;color:#555"><span>＼線の太さ</span><button class="go2" id="__ce_bremphm" style="background:#8a8a8e;margin:0;flex:1">−細く</button><button class="go2" id="__ce_bremphp" style="background:#8a8a8e;margin:0;flex:1">＋太く</button></div>'
      +'<div style="display:flex;gap:6px;align-items:center;margin-top:5px;font-size:11.5px;color:#555"><span>＼線の調整</span><input type="color" id="__ce_bremphc" value="#f0c14b" style="width:26px;height:22px;padding:0;border:none;border-radius:4px;cursor:pointer"><button class="go2" id="__ce_bremphyu" style="background:#8a8a8e;margin:0;flex:1">↑上へ</button><button class="go2" id="__ce_bremphyd" style="background:#8a8a8e;margin:0;flex:1">↓下へ</button><button class="go2" id="__ce_bremphhm" style="background:#8a8a8e;margin:0;flex:1">−短く</button><button class="go2" id="__ce_bremphhp" style="background:#8a8a8e;margin:0;flex:1">＋長く</button></div>'
      +'<div style="display:flex;gap:6px;align-items:center;margin-top:5px;font-size:11.5px;color:#555"><span>🌈 ならびをマルく</span><button class="go2" id="__ce_brarc" style="background:#0ea5a3;margin:0;flex:1.3">アーチにする/戻す</button><button class="go2" id="__ce_brarcm" style="background:#8a8a8e;margin:0;flex:1">−ゆるく</button><button class="go2" id="__ce_brarcp" style="background:#8a8a8e;margin:0;flex:1">＋つよく</button></div>'
      +'<div style="display:flex;gap:6px;align-items:center;margin-top:5px;font-size:11.5px;color:#555"><span>⬌ 要素の箱</span><button class="go2" id="__ce_brwm" style="background:#8a8a8e;margin:0;flex:1">−せまく</button><button class="go2" id="__ce_brwp" style="background:#8a8a8e;margin:0;flex:1">＋ひろく</button><button class="go2" id="__ce_brhr" style="background:#b45309;margin:0;flex:1.4">⬍高さを自動に戻す</button></div>'
      +'<div style="font-size:12.5px;font-weight:700;color:#2b6cb0;margin:14px 0 6px">📜 縦書き</div>'
      +'<button class="go2" id="__ce_brvert" style="background:#0b6bcb">📜 縦書きにする／戻す</button>'
      +'</div>';
    document.body.appendChild(ov);
    // 📍 編集パネルを「編集する文字の反対側」へ寄せる＝文字がパネルに隠れず、その場でプレビューを確認できる。
    //    幅・中身は元のまま（細くすると縦に伸びてフォント等が触りにくくなる）。中央寄せをやめて端に寄せるだけ。
    (function(){
      try{
        var r=el.getBoundingClientRect(), vw=window.innerWidth;
        ov.style.padding='12px';
        ov.style.justifyContent=((r.left+r.width/2) < vw/2) ? 'flex-end' : 'flex-start';
        ov.style.alignItems='flex-start';   // 縦は上寄せ＝縦長パネルでも上から全部見える
      }catch(_){}
    })();
    // 🎨 ページで実際に使われている文字色（頻度順10色）＝ここから選べば色が統一できる
    function _brApplyCol(v){
      el.style.setProperty('color', v, 'important');
      el.style.setProperty('-webkit-text-fill-color', v, 'important');
      [].forEach.call(el.querySelectorAll('.fxa_ch,.imp-char'), function(sp){ sp.style.setProperty('color', v, 'important'); sp.style.setProperty('-webkit-text-fill-color', v, 'important'); });
      markDirty();
    }
    (function(){
      var host=document.getElementById('__ce_brsw'); if(!host) return;
      var cnt={};
      [].slice.call(document.querySelectorAll('body *')).slice(0,1500).forEach(function(n){
        if(n.closest('[id^="__ce"]')) return;
        if(!(n.textContent||'').trim()) return;
        var c; try{ c=getComputedStyle(n).color; }catch(_){ return; }
        if(c) cnt[c]=(cnt[c]||0)+1;
      });
      var sws=Object.keys(cnt).sort(function(a,b){return cnt[b]-cnt[a];}).slice(0,10);
      host.innerHTML=sws.length?'<span style="font-size:11px;color:#888">ページで使用中：</span>'+sws.map(function(c){return '<button class="__ce_brswb" data-c="'+c+'" title="'+c+'（クリックでこの色に）" style="width:18px;height:18px;border:1px solid rgba(0,0,0,.15);border-radius:4px;background:'+c+';cursor:pointer;padding:0;vertical-align:middle;margin-right:3px"></button>';}).join(''):'';
    })();
    var ta=document.getElementById('__ce_brta'); ta.value=cur; ta.focus();
    // ★複数の文字が入った枠なら、先に「行数を変えなければ見た目は保たれる」と伝える（事故を未然に防ぐ）
    (function(){
      var _bn=_brTextNodes(el).length; if(_bn<2) return;
      var _h=document.createElement('div');
      _h.setAttribute('style','margin:6px 0 0;padding:6px 8px;background:#fffbe6;border:1px solid #f0d98c;border-radius:6px;font-size:11.5px;color:#7a5a00;line-height:1.7');
      _h.innerHTML='📌 この枠には文字が <b>'+_bn+'か所</b>（大きさの違う文字）入っています。<br>'
        +'<b>行数を変えなければ、大きさ・フォントはそのまま</b>入れ替わります。行を足す／消すと飾りが外れて小さい文字になります。';
      ta.parentNode.insertBefore(_h, ta.nextSibling);
    })();
    try{ document.getElementById('__ce_brcol').value=_rgbToHex(getComputedStyle(el).color); }catch(_){}
    try{ document.getElementById('__ce_brudot').value=_rgbToHex(getComputedStyle(el).color); }catch(_){}
    ov.addEventListener('click',function(e){
      if(e.target.id==='__ce_pkx'){ ov.remove(); return; }
      // ★暗幕（箱の外）クリックでは閉じない：書きかけの文章が一瞬で消える事故が多かった。
      //   textareaのリサイズや文字選択で手が箱の外に出るだけで閉じていたのが原因。閉じるのは × だけ。
      if(e.target.id==='__ce_pk') return;
      if(e.target.id==='__ce_brapply'){
        stopAnim(el); clearPreviewStyle(el);  // プレビュー途中の半透明・ズレたopacity/transformが残らないよう元へ戻す
        // ★まず「中のタグを残したまま」入れ替えられるか試す＝大きさ・フォントが変わらない
        if(_brKeepTags(el, ta.value)){
          markDirty(); msg.textContent='文字を入れ替えました（大きさ・フォントはそのまま／💾保存で確定）';
          return;
        }
        if(_brTextNodes(el).length>=2 && !confirm('この枠には文字が'+_brTextNodes(el).length+'か所あります。\\n行数を変えると中の飾り（大きさ・フォント）が外れて、小さい文字になります。\\n\\n行数をそのままにすれば見た目は変わりません。このまま進めますか？')){
          msg.textContent='やめました（行数を元と同じにすれば、大きさはそのまま入れ替わります）';
          return;
        }
        // 行頭の半角スペースはHTMLだと潰れて見えない → &nbsp;にして見た目どおり残す（全角スペースは元々残る）
        var _bht=esc(_keepSpaces(ta.value)).replace(/\\n/g,'<br>');
        _bht=_bht.replace(/(^|<br>)( +)/g,function(_m,_p,_sp){ return _p+Array(_sp.length+1).join('&nbsp;'); });
        el.innerHTML=_bht;
        // 🌈アーチ中なら、新しい文面を元テキストとして控え直してアーチをかけ直す（改行反映で平らに戻るのを防ぐ）
        var _arcA=+el.getAttribute('data-cearc')||0;
        if(_arcA>0){ el.setAttribute('data-cearcorig', el.innerHTML); arcApply(el,_arcA); }
        markDirty();
        msg.textContent='改行を反映しました。「💾 保存」で確定できます';
        return;
      }
      var fsb=e.target.closest('button[data-fs]');
      if(fsb){ _fontSize(el, +fsb.getAttribute('data-fs')); return; }
      var lhb=e.target.closest('button[data-lh]');
      if(lhb){ _lineHeight(el, +lhb.getAttribute('data-lh')); return; }
      if(e.target.closest('button[data-lhr]')){ _lineHeight(el, 0, true); return; }
      var swb=e.target.closest('.__ce_brswb');
      if(swb){ _brApplyCol(swb.getAttribute('data-c')); return; }
      var lsb=e.target.closest('button[data-ls]');
      if(lsb){
        var _lc=parseFloat(el.style.letterSpacing);
        if(isNaN(_lc)){ _lc=parseFloat(getComputedStyle(el).letterSpacing); if(isNaN(_lc)) _lc=0; }
        var _lv=Math.max(-2, _lc + (+lsb.getAttribute('data-ls')));
        el.style.setProperty('letter-spacing', _lv.toFixed(1)+'px', 'important');
        markDirty(); msg.textContent='字間: '+_lv.toFixed(1)+'px（保存で確定）';
        return;
      }
      if(e.target.closest('button[data-lsr]')){ el.style.removeProperty('letter-spacing'); markDirty(); return; }
      if(e.target.id==='__ce_brcolr'){ el.style.removeProperty('color'); el.style.removeProperty('-webkit-text-fill-color'); markDirty(); return; }
      if(e.target.id==='__ce_brudotb'){ toggleUnderlineDots(el, document.getElementById('__ce_brudot').value); return; }
      if(e.target.id==='__ce_brvert'){ toggleVertical(el); return; }
      var rb=e.target.closest('button[data-rot]');
      if(rb){ rotateBy(el, +rb.getAttribute('data-rot')); markDirty(); return; }
      if(e.target.closest('button[data-rotr]')){ var _cr=+el.getAttribute('data-cero')||0; if(_cr) rotateBy(el, -_cr); markDirty(); return; }
      if(e.target.id==='__ce_brwm'||e.target.id==='__ce_brwp'){
        adjustWidth(el, e.target.id==='__ce_brwp'?40:-40);
        msg.textContent='箱の幅を調整しました（保存で確定）'; return;
      }
      if(e.target.id==='__ce_brhr'){
        // 伸縮ハンドルの誤ドラッグ等で焼き込まれた高さ固定だけを解除（位置・回転は保つ）
        el.style.removeProperty('height'); el.style.removeProperty('min-height'); markDirty();
        msg.textContent='⬍高さの固定を解除しました（中身に合わせて自動になります・保存で確定）'; return;
      }
      if(e.target.id==='__ce_brarc'){
        var _a0=+el.getAttribute('data-cearc')||0;
        arcApply(el, _a0>0?0:8);
        msg.textContent=(_a0>0)?'アーチを戻しました（保存で確定）':'🌈 文字をアーチ状にしました（−ゆるく／＋つよくで調整・保存で確定）';
        return;
      }
      if(e.target.id==='__ce_brarcm'||e.target.id==='__ce_brarcp'){
        var _a1=+el.getAttribute('data-cearc')||0;
        if(!_a1){ msg.textContent='先に「アーチにする」を押してください'; return; }
        _a1=Math.max(2, Math.min(30, _a1+(e.target.id==='__ce_brarcp'?3:-3)));
        arcApply(el, _a1);
        msg.textContent='アーチの強さ: '+_a1+'（保存で確定）';
        return;
      }
      if(e.target.id==='__ce_bremphyu'||e.target.id==='__ce_bremphyd'){
        ensureDecoCss(); if(!el.classList.contains('ce_emph')) el.classList.add('ce_emph');
        var _ey=parseFloat(el.style.getPropertyValue('--ce-emphy')); if(isNaN(_ey)) _ey=26;
        _ey=Math.max(-20, Math.min(80, _ey+(e.target.id==='__ce_bremphyd'?6:-6)));
        el.style.setProperty('--ce-emphy', _ey+'%'); markDirty();
        msg.textContent='強調線の上下位置: '+_ey+'%（保存で確定）'; return;
      }
      if(e.target.id==='__ce_bremphhm'||e.target.id==='__ce_bremphhp'){
        ensureDecoCss(); if(!el.classList.contains('ce_emph')) el.classList.add('ce_emph');
        var _eh=parseFloat(el.style.getPropertyValue('--ce-emphh')); if(isNaN(_eh)) _eh=0.72;
        _eh=Math.max(0.3, Math.min(1.8, _eh+(e.target.id==='__ce_bremphhp'?0.12:-0.12)));
        el.style.setProperty('--ce-emphh', _eh.toFixed(2)+'em'); markDirty();
        msg.textContent='強調線の長さ: '+_eh.toFixed(2)+'em（保存で確定）'; return;
      }
      if(e.target.id==='__ce_bremphm'||e.target.id==='__ce_bremphp'){
        ensureDecoCss();
        if(!el.classList.contains('ce_emph')) el.classList.add('ce_emph');
        var _ew=parseFloat(el.style.getPropertyValue('--ce-emphw')); if(isNaN(_ew)) _ew=3;
        _ew=Math.max(1, Math.min(20, _ew+(e.target.id==='__ce_bremphp'?1:-1)));
        el.style.setProperty('--ce-emphw', _ew+'px'); markDirty();
        msg.textContent='強調線の太さ: '+_ew+'px（保存で確定）';
        return;
      }
      if(e.target.id==='__ce_bremph'){
        ensureDecoCss(); el.classList.toggle('ce_emph'); markDirty();
        msg.textContent=el.classList.contains('ce_emph')?'＼ 左右に強調線を付けました ／（保存で確定）':'強調線を外しました';
        return;
      }
      if(e.target.id==='__ce_brbub'){
        ensureDecoCss(); el.classList.toggle('ce_bubble'); markDirty();
        msg.textContent=el.classList.contains('ce_bubble')?'💬 吹き出しにしました（保存で確定）':'吹き出しを外しました';
        return;
      }
    });
    var _empc=document.getElementById('__ce_bremphc');
    if(_empc) _empc.addEventListener('input',function(){
      ensureDecoCss(); if(!el.classList.contains('ce_emph')) el.classList.add('ce_emph');
      el.style.setProperty('--ce-emphc', this.value); markDirty();
    });
    // 🅰 今かかっている値（フォント名・大きさ・行間）を出す。★「このフォント何？」を調べるのが目的なので、
    //    プルダウンも今のフォントを選んだ状態にする（一覧に無い名前なら先頭に足して選ぶ）。
    function _brSyncNow(){
      var cs; try{ cs=getComputedStyle(el); }catch(_){ return; }
      var f=(cs.fontFamily||'').split(',')[0].trim().replace(/["']/g,'');
      var n1=document.getElementById('__ce_brffnow');
      if(n1) n1.innerHTML='今のフォント：<b>'+esc(f||'（不明）')+'</b>'+(document.getElementById('__ce_fontall')?'　<span style="color:#7c3aed">※ページ全部に適用中</span>':'');
      var n2=document.getElementById('__ce_brfsnow');
      if(n2) n2.textContent='今 '+(Math.round((parseFloat(cs.fontSize)||0)*10)/10)+'px';
      var n3=document.getElementById('__ce_brlhnow');
      if(n3){ var lh=parseFloat(cs.lineHeight), fs=parseFloat(cs.fontSize)||16;
        n3.textContent=isFinite(lh)?('今 '+(Math.round(lh/fs*100)/100)+'倍（'+Math.round(lh)+'px）'):'今 標準'; }
    }
    (function(){
      var sel=document.getElementById('__ce_brff'); if(!sel) return;
      var cs; try{ cs=getComputedStyle(el); }catch(_){ return; }
      var first=(cs.fontFamily||'').split(',')[0].trim().replace(/["']/g,'').toLowerCase(), hit='';
      FONTS.forEach(function(f){
        if(!f[0]||hit) return;
        if(f[0].split(',')[0].trim().replace(/["']/g,'').toLowerCase()===first) hit=f[0];
      });
      if(hit){ sel.value=hit; }
      else if(cs.fontFamily){
        var op=document.createElement('option'); op.value=cs.fontFamily;
        op.textContent='今のフォント：'+cs.fontFamily.split(',')[0].replace(/["']/g,'');
        sel.insertBefore(op, sel.children[1]||null); sel.value=cs.fontFamily;
      }
    })();
    _brSyncNow();
    ov.addEventListener('click',function(){ setTimeout(_brSyncNow,0); });   // 何を押しても表示を追従させる
    document.getElementById('__ce_brff').addEventListener('change',function(){
      if(this.value){ ensureGoogleFont(this.value); el.style.setProperty('font-family', this.value, 'important'); }
      else el.style.removeProperty('font-family');
      markDirty(); setTimeout(_brSyncNow,0);
    });
    // 🅰 このフォントをページ全部に適用（＝気に入ったフォントを1クリックで全体に配る）
    //   1枚の<style>で当てる（要素ごとにインラインを書くとHTMLが膨らむため）。ただし
    //   ★インライン指定は<style>より強いので、先に個別のfont-family指定を外さないと効かない。
    document.getElementById('__ce_brffall').addEventListener('click',function(){
      var fam=''; try{ fam=getComputedStyle(el).fontFamily||''; }catch(_){}
      if(!fam){ msg.textContent='フォントが読み取れませんでした'; return; }
      var sel=document.getElementById('__ce_brff');
      if(sel&&sel.value) ensureGoogleFont(sel.value);
      var n=0;
      [].slice.call(document.querySelectorAll('[style*="font-family"]')).forEach(function(x){
        if(_inUI2(x)) return;
        x.style.removeProperty('font-family'); n++;
      });
      var st=document.getElementById('__ce_fontall');
      if(!st){ st=document.createElement('style'); st.id='__ce_fontall'; document.head.appendChild(st); }
      st.textContent='body,body *{font-family:'+fam+' !important}'
        +'#__ce,#__ce *,#__ce_cm,#__ce_cm *,#__ce_pk,#__ce_pk *,#__ce_toast,#__ce_toast *,#__ce_savebar,#__ce_savebar *{font-family:system-ui,"Segoe UI",sans-serif !important}';
      markDirty(); setTimeout(_brSyncNow,0);
      msg.textContent='🅰 ページ全部を「'+fam.split(',')[0].replace(/["']/g,'')+'」にしました'
        +(n?('（個別指定 '+n+'件は外しました）'):'')+'。戻すときは同じ所の ⟲。💾保存で残ります';
    });
    document.getElementById('__ce_brffallr').addEventListener('click',function(){
      var st=document.getElementById('__ce_fontall');
      if(!st){ msg.textContent='ページ全部への適用はしていません'; return; }
      st.remove(); markDirty(); setTimeout(_brSyncNow,0);
      msg.textContent='🅰 ページ全部への適用をやめました（元のフォントに戻ります）';
    });
    document.getElementById('__ce_brcol').addEventListener('input',function(){
      // color だけだと、グラデ文字(-webkit-text-fill-color:transparent)や1文字アニメで「透明のまま＝黒/消える」になる。
      // text-fill-color も同じ色で上書きし、子の文字span(fxa_ch)にも直接当てて確実に色を出す。
      el.style.setProperty('color', this.value, 'important');
      el.style.setProperty('-webkit-text-fill-color', this.value, 'important');
      // グラデ文字(background-clip:text)の要素は、単色にした時点で敷き背景が「ベタ塗りの箱」として見えてしまう→背景ごと外す
      try{ var _bc=getComputedStyle(el); if(((_bc.webkitBackgroundClip||_bc.backgroundClip||'')+'').indexOf('text')>=0) el.style.setProperty('background','none','important'); }catch(_){}
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
    if(window.__fxaSweepHl) window.__fxaSweepHl(span);
    else{ span.style.setProperty('--hlw',100); span.classList.add('fxa_in'); }
  }
  // 帯の太さ（--hlt0/--hlt1・中心は固定でそこから上下に広げ縮め）。delta%pt単位、8〜70%の範囲。
  function fxHlThick(span, delta){
    if(!span){ if(msg) msg.textContent='先にマーカーを引いてください'; return; }
    var t0=parseFloat(span.style.getPropertyValue('--hlt0'))||79;
    var t1=parseFloat(span.style.getPropertyValue('--hlt1'))||91;
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
  // ⏳待機時間（data-cedelay・ミリ秒）。画面に入ってから引き始めるまでの待ち。
  // 「先に文字の出現アニメ→あとから線」の順番づけ用。0.2秒刻み・0〜5秒。0なら属性ごと外す。
  function fxHlDelay(span, delta){
    if(!span){ if(msg) msg.textContent='先にマーカーを引いてください'; return; }
    var v=(+span.getAttribute('data-cedelay')||0)+delta;
    v=Math.max(0,Math.min(5000,Math.round(v/100)*100));
    if(v>0) span.setAttribute('data-cedelay',v); else span.removeAttribute('data-cedelay');
    // その場で「待って→引く」を体感できるようにプレビューも待ってから再生（連打時は前の待ちを取り消す）
    span.classList.remove('fxa_in'); span.style.setProperty('--hlw',0);
    if(span.__hlDelayT) clearTimeout(span.__hlDelayT);
    span.__hlDelayT=setTimeout(function(){ fxHlReplay(span); }, v);
    markDirty();
    if(msg) msg.textContent='マーカーの待機: '+(v/1000).toFixed(1)+'秒（画面に入ってから待って引く・保存で確定）';
  }
  // 帯の上下位置（--hlt0/--hlt1をまとめてずらす。太さは維持）。delta%pt単位、0〜100%からはみ出ないように収める。
  function fxHlPos(span, delta){
    if(!span){ if(msg) msg.textContent='先にマーカーを引いてください'; return; }
    var t0=parseFloat(span.style.getPropertyValue('--hlt0'))||79;
    var t1=parseFloat(span.style.getPropertyValue('--hlt1'))||91;
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
  // ===== 🖍マーカー／〰下線／＼あしらい／を「消す」共通処理（AIなし）=====
  // 飾りの付き方は5通りあるのに、今までは①しか「消す」対象として見ていなかった
  //   ①選択して包んだspan（.fxa_hl / .ceud）    ←これだけ消せた
  //   ②要素そのものに付いた走る下線（.fxa_ud）  ←選び直しても「下線を消す」が出ない
  //   ③⚙メニューの点線下線（data-ceudot＋インラインborder-bottom）
  //   ④＼あしらい／（.ce_emph＝疑似要素の黄色い斜め線）  ←DOMに実体が無いので余計に消せない
  //   ⑤💬吹き出し（.ce_bubble）
  // 全部同じ関数で拾う。②〜⑤のように「飾り専用の包みspanではない」相手は中身を出さず
  // クラスと色指定だけ落とす（見出しごと消えてしまうのを防ぐ）。
  // 🅰 フォント一覧（✏文字を編集／🅰まとめて文字調整で共用・2026-07-24）。
  //   value=完全なfont-family文字列。3要素目が有るもの=Google Fonts（選ぶと自動でWebフォントを読み込む）。
  var FONT_LIST=[
    ['','（フォントはそのまま）'],
    ["'Yu Gothic','Hiragino Kaku Gothic ProN',Meiryo,sans-serif",'ゴシック（標準）'],
    ["'Yu Mincho','Hiragino Mincho ProN',serif",'明朝（上品）'],
    ["'Hiragino Maru Gothic ProN','Rounded Mplus 1c',sans-serif",'丸ゴシック（OS標準）'],
    ["'BIZ UDPGothic',sans-serif",'BIZ UDゴシック（読みやすい）'],
    ["'BIZ UDPMincho',serif",'BIZ UD明朝'],
    ["'UD Digi Kyokasho NP-R','UDデジタル教科書体 NP-R',sans-serif",'UD教科書体（やわらか）'],
    ["'Zen Maru Gothic',sans-serif",'丸ゴシック Zen（Web）',1],
    ["'M PLUS Rounded 1c',sans-serif",'丸ゴシック M＋（Web）',1],
    ["'Kosugi Maru',sans-serif",'丸ゴシック 小杉（Web）',1],
    ["'RocknRoll One',sans-serif",'太丸ゴシック（Web）',1],
    ["'Zen Kaku Gothic New',sans-serif",'角ゴシック Zen（Web）',1],
    ["'Noto Sans JP',sans-serif",'角ゴシック Noto（Web）',1],
    ["'Noto Serif JP',serif",'明朝 Noto（Web）',1],
    ["'Shippori Mincho',serif",'明朝 しっぽり（Web）',1],
    ["'Zen Old Mincho',serif",'明朝 Zen Old（Web）',1],
    ["'Kaisei Decol',serif",'やわらか明朝 解星（Web）',1],
    ["'Klee One',cursive",'教科書体風 Klee（Web）',1],
    ["'Zen Kurenaido',sans-serif",'手書き風 紅道（Web）',1],
    ["'Yuji Syuku',serif",'筆文字 佑字祝（Web）',1],
    ["'Hachi Maru Pop',cursive",'ポップ丸 はち（Web）',1],
    ["'Dela Gothic One',sans-serif",'極太見出し（Web）',1],
    ["Georgia,'Times New Roman',serif",'英字セリフ'],
    ["Helvetica,Arial,sans-serif",'英字サンセリフ'],
    ["'Poppins',sans-serif",'英字 Poppins（Web）',1],
    ["'Playfair Display',serif",'英字 Playfair（Web）',1],
    ["'Courier New',monospace",'等幅（コード風）']
  ];
  // Google Fonts の家系名 → css2 の family クエリ。ここに載っている名前だけ Web から読み込む。
  var _GF_MAP={
    'Zen Maru Gothic':'Zen+Maru+Gothic:wght@400;500;700','M PLUS Rounded 1c':'M+PLUS+Rounded+1c:wght@400;500;700',
    'Kosugi Maru':'Kosugi+Maru','RocknRoll One':'RocknRoll+One','Zen Kaku Gothic New':'Zen+Kaku+Gothic+New:wght@400;500;700',
    'Noto Sans JP':'Noto+Sans+JP:wght@400;500;700','Noto Serif JP':'Noto+Serif+JP:wght@400;600;700',
    'Shippori Mincho':'Shippori+Mincho:wght@400;600;700','Zen Old Mincho':'Zen+Old+Mincho:wght@400;700',
    'Kaisei Decol':'Kaisei+Decol:wght@400;700','Klee One':'Klee+One:wght@400;600','Zen Kurenaido':'Zen+Kurenaido',
    'Yuji Syuku':'Yuji+Syuku','Hachi Maru Pop':'Hachi+Maru+Pop','Dela Gothic One':'Dela+Gothic+One',
    'Poppins':'Poppins:wght@400;500;700','Playfair Display':'Playfair+Display:wght@400;600;700'
  };
  // 選ばれた font-family から先頭のフォント名を取り出し、Google Fonts なら <link> を1度だけ<head>に足す。
  //   保存(cleanHtml)は<head>の<link rel=stylesheet>を残すので、保存後も効く。日本語名(OS標準)はマップに無く素通り。
  function ensureGoogleFont(fam){
    try{
      var m=(fam||'').match(/['"]?([A-Za-z0-9 -]+)/);
      if(!m) return;
      var name=m[1].trim();
      var q=_GF_MAP[name]; if(!q) return;
      var id='cegf-'+name.replace(/\\s+/g,'-');
      if(document.getElementById(id)) return;
      var l=document.createElement('link'); l.id=id; l.rel='stylesheet';
      l.href='https://fonts.googleapis.com/css2?family='+q+'&display=swap';
      (document.head||document.documentElement).appendChild(l);
    }catch(_){}
  }
  var DECO_SEL='.fxa_hl,.ceud,.fxa_ud,[data-ceudot],.ce_emph,.ce_bubble,.ce_txtbg';
  var UD_SEL='.ceud,.fxa_ud,[data-ceudot]';   // 〰下線だけ（マーカー・あしらいは含めない）
  var DECO_CLS=['fxa_hl','fxa_ud','ceud','fxa_in'];
  var DECO_VARS=['--hlc','--udc','--hlw','--hldur','--hlt0','--hlt1',
                 '--ce-emphc','--ce-emphw','--ce-emphh','--ce-emphy','--ce-bubblebg'];
  function _isDecoWrap(el){
    if(!el||el.tagName!=='SPAN'||!el.classList.length) return false;
    var ok=true;
    [].forEach.call(el.classList,function(c){ if(DECO_CLS.indexOf(c)<0) ok=false; });
    return ok;  // 装飾のためだけに作ったspan＝剥がして中身だけ残してよい
  }
  function decoStrip(el){
    if(!el) return;
    if(el.classList && el.classList.contains('ce_txtbg')){ textBgRemove(el); return; }  // 🖌文字の背景色は専用の外し処理へ
    if(_isDecoWrap(el)){
      var p=el.parentNode; if(!p) return;
      while(el.firstChild) p.insertBefore(el.firstChild, el);
      p.removeChild(el); markDirty(); return;
    }
    DECO_CLS.concat(['ce_emph','ce_bubble']).forEach(function(c){ el.classList.remove(c); });
    DECO_VARS.forEach(function(v){ el.style.removeProperty(v); });
    if(el.getAttribute('data-ceudot')!=null){
      el.removeAttribute('data-ceudot');
      el.style.removeProperty('border-bottom'); el.style.removeProperty('padding-bottom');
    }
    markDirty();
  }
  // 飾りの名前（ユーザーがメニューで見て「これだ」と分かる言葉で出す）
  function decoName(el){
    if(el.classList.contains('fxa_hl')) return '🖍マーカー';
    if(el.classList.contains('ce_emph')) return '＼あしらい／';
    if(el.classList.contains('ce_bubble')) return '💬吹き出し';
    return '〰下線';
  }
  var DECO_NAMED=[['fxa_hl','🖍マーカー'],['ceud','〰下線'],['fxa_ud','〰走る下線'],['ce_emph','＼あしらい／'],['ce_bubble','💬吹き出し'],['ce_txtbg','🖌文字の背景色']];
  var _SIDE_JA={top:'上',right:'右',bottom:'下',left:'左'};
  // ★右クリック位置から「見えている飾り」を全部集める（AIなし）。
  //   クラス名で決め打ちせず、疑似要素(::before/::after)とborderも同列に並べるのが肝：
  //   ユーザーには「マーカー」に見えていても中身は ce_emph の疑似要素だった、という事故が実際に起きた。
  //   疑似要素はDOMに実体が無く掴めない＝この一覧に出さないと素人には絶対に消せない。
  //   自分→外側4段まで遡る（飾りは親が持っていることが多い）＋中のマーカー/下線spanも拾う。
  function decoScan(el){
    var out=[], t=el;
    for(var i=0; t && i<5; i++, t=t.parentElement){
      if(!t || t===document.body || t.tagName==='HTML') break;
      var lb=(i===0)?'ここ':('外側'+i+'〈'+t.tagName.toLowerCase()+'〉');
      DECO_NAMED.forEach(function(p){ if(t.classList.contains(p[0])) out.push({el:t,kind:'cls',name:lb+'：'+p[1]}); });
      if(t.getAttribute('data-ceudot')!=null) out.push({el:t,kind:'cls',name:lb+'：〰点線下線'});
      ['::before','::after'].forEach(function(ps){
        var c='', pw=0, ph=0, bimg='';
        try{
          var p2=getComputedStyle(t,ps); c=p2.content; pw=parseFloat(p2.width)||0; ph=parseFloat(p2.height)||0;
          // ★疑似要素が「絵」を描いている場合はその画像を控える＝ボタンにサムネイルを出して
          //   「どれが気球か」を目で選べるようにする（実体が無いので名前だけでは絶対に区別できない）
          var bi=p2.backgroundImage||'';
          if(bi&&bi!=='none'&&bi.indexOf('gradient')<0){ var mu=bi.match(/url\\(["']?([^"')]+)/); if(mu) bimg=mu[1]; }
        }catch(_){}
        var k=(ps==='::before')?'cepsoff-b':'cepsoff-a', offd=t.classList.contains(k);
        // 幅も高さも0の疑似要素は見えていない（ホバー用の下線など）＝一覧に出すとノイズになるので除く
        if(offd||(c&&c!=='none'&&c!=='normal'&&pw>0&&ph>0))
          out.push({el:t,kind:k,img:bimg,
            name:lb+'：'+(bimg?'絵の飾り':'飾り')+'('+ps+')'+(pw?' '+Math.round(pw)+'×'+Math.round(ph):'')+(offd?'【消し済み】':'')});
      });
      var cs; try{ cs=getComputedStyle(t); }catch(_){ cs=null; }
      // border は「線が多すぎて一覧が埋まる」ので自分と1つ外側だけ（飾りの本命は疑似要素側）
      if(cs && i<2) ['top','right','bottom','left'].forEach(function(sd){
        var offd=(t.style.getPropertyValue('border-'+sd)==='none');
        if(offd||(cs.getPropertyValue('border-'+sd+'-style')!=='none'&&parseFloat(cs.getPropertyValue('border-'+sd+'-width'))>0))
          out.push({el:t,kind:'bd',side:sd,name:lb+'：'+_SIDE_JA[sd]+'の線'+(offd?'【消し済み】':'')});
      });
      if(/^(SECTION|HEADER|FOOTER|MAIN)$/.test(t.tagName)) break;  // ページの器まで来たら打ち切り
    }
    // 中のマーカー/下線span＝文字を選び直さなくても消せるように
    if(el && el.querySelectorAll && !/^(SECTION|HEADER|FOOTER|MAIN|BODY|HTML)$/.test(el.tagName)){
      [].slice.call(el.querySelectorAll(DECO_SEL)).slice(0,4).forEach(function(n){ out.push({el:n,kind:'cls',name:'中の'+decoName(n)}); });
    }
    return out.slice(0,8);
  }
  // 1件を消す／戻す（同じボタンでトグル＝押し間違えても怖くない）
  function decoToggle(it){
    if(it.kind==='cls'){ decoStrip(it.el); return '消しました'; }
    if(it.kind==='bd'){
      var pr='border-'+it.side;
      if(it.el.style.getPropertyValue(pr)==='none'){ it.el.style.removeProperty(pr); markDirty(); return '戻しました'; }
      it.el.style.setProperty(pr,'none','important'); markDirty(); return '消しました';
    }
    _psCss(); var on=it.el.classList.toggle(it.kind); markDirty(); return on?'消しました':'戻しました';
  }
  // ◽ 角丸の写真の「裏の四角(ケース)」を探す（AIなし・2026-07-24）。
  //   表に角丸があるのに裏の箱が直角だと、丸みの外側から直角の角がはみ出して見える。
  //   その裏の四角を「⌒角丸にする / ✕見た目を消す」で隠せるよう候補を集める（自分＋外側5段）。
  function radiusScan(el){
    if(!el||el.nodeType!==1) return [];
    function rad(n){ try{ return parseFloat(getComputedStyle(n).borderTopLeftRadius)||0; }catch(_){ return 0; } }
    // お手本の丸み＝自分・中の要素・親から拾った最大の角丸（表の写真の丸みに合わせる）
    var model=0, kids=el.querySelectorAll?[].slice.call(el.querySelectorAll('*')).slice(0,30):[];
    [el].concat(kids).forEach(function(n){ var r=rad(n); if(r>model) model=r; });
    var p=el; for(var k=0;k<5&&p&&p.nodeType===1;k++,p=p.parentElement){ var r=rad(p); if(r>model) model=r; }
    if(model<4) model=16;
    var out=[], t=el;
    for(var i=0; t && i<6; i++, t=t.parentElement){
      if(!t||t===document.body||t.tagName==='HTML') break;
      if(t.closest && t.closest('[id^="__ce"]')) continue;
      var cs; try{ cs=getComputedStyle(t); }catch(_){ continue; }
      var box=t.getBoundingClientRect();
      if(box.width>=24 && box.height>=24){
        // 背景/画像/枠/影 のどれかを持つ「見た目のある四角」で、角丸が表より小さい＝はみ出す犯人候補
        var bgc=cs.backgroundColor||'';
        var bg=bgc&&bgc!=='transparent'&&bgc!=='rgba(0, 0, 0, 0)';
        var bimg=cs.backgroundImage&&cs.backgroundImage!=='none';
        var bd=parseFloat(cs.borderTopWidth)>0&&cs.borderTopStyle!=='none';
        var sh=cs.boxShadow&&cs.boxShadow!=='none';
        var r=rad(t);
        if((bg||bimg||bd||sh) && r<model-1){
          var lb=(i===0)?'ここ〈'+t.tagName.toLowerCase()+'〉':'外側'+i+'〈'+t.tagName.toLowerCase()+'〉';
          out.push({el:t, model:Math.round(model), cur:Math.round(r), name:lb});
        }
      }
      if(/^(SECTION|HEADER|FOOTER|MAIN)$/.test(t.tagName)) break;
    }
    return out.slice(0,5);
  }
  // ⌒ 裏の四角に角丸をつける／外す（同じボタンでトグル）
  function radiusRound(it){
    var el=it.el;
    if(el.style.getPropertyValue('border-radius')){ el.style.removeProperty('border-radius'); el.style.removeProperty('overflow'); markDirty(); return false; }
    el.style.setProperty('border-radius', it.model+'px','important'); markDirty(); return true;
  }
  // ✕ 裏の四角の見た目（背景・枠・影）を消す／戻す（箱と中身は残す＝中の写真は消えない）
  function radiusFlat(it){
    var el=it.el;
    if(el.style.getPropertyValue('background-color')==='transparent'){
      ['background-color','background-image','border','box-shadow'].forEach(function(pr){ el.style.removeProperty(pr); });
      markDirty(); return false;
    }
    el.style.setProperty('background-color','transparent','important');
    el.style.setProperty('background-image','none','important');
    el.style.setProperty('border','none','important');
    el.style.setProperty('box-shadow','none','important');
    markDirty(); return true;
  }
  // ===== 🎨 背景・フチ・影を消す（AIなし・2026-07-28） =====
  // ★「色のついたカードを透明にしたい」が今までどこからも出来なかった。
  //   radiusFlat（◽裏の四角）は "角丸の写真の裏で直角がはみ出している箱" が見つかった時しか出ないので、
  //   ふつうの色つきカード（グラデ帯・薄い枠・影つき）には一生届かない＝別の入口として用意する。
  //   自分→外側4段の「見た目を持っている箱」を並べ、🎨背景 / ▭フチ / ☁影 を1つずつトグルで消す。
  // ★元のインラインstyleを data-ceflat* に控えてから上書きする＝「戻す」で元の値に戻る
  //   （消す時に removeProperty するだけだと、元から付いていた指定まで永久に失われる）
  var FLAT_P={bg:['background-color','background-image'],
              bd:['border-top-style','border-right-style','border-bottom-style','border-left-style'],
              sh:['box-shadow'], vis:['display']};
  // 飾りの代表色（ボタンに小さな色の四角を出す＝「このピンクのこと」と目で分かる）
  function _flatSw(cs){
    var c=cs.backgroundColor||'';
    if(c&&c!=='transparent'&&c.indexOf('rgba(0, 0, 0, 0)')<0) return c;
    var m=(cs.backgroundImage||'').match(/rgba?\([^)]+\)/);
    if(m) return m[0];
    var b=cs.borderTopColor||''; return (b&&b.indexOf('rgba(0, 0, 0, 0)')<0)?b:'';
  }
  function flatScan(el){
    var out=[], t=el;
    for(var i=0; t && i<5; i++, t=t.parentElement){
      if(!t||t===document.body||t.tagName==='HTML') break;
      if(t.closest&&t.closest('[id^="__ce"]')) continue;   // ツール自身のパネルは対象外
      var cs; try{ cs=getComputedStyle(t); }catch(_){ continue; }
      var r=t.getBoundingClientRect();
      if(r.width>=16&&r.height>=10){
        var bgc=cs.backgroundColor||'', bimg=cs.backgroundImage||'';
        var hasC=!!bgc&&bgc!=='transparent'&&bgc.indexOf('rgba(0, 0, 0, 0)')<0;
        var hasI=!!bimg&&bimg!=='none';
        var hasB=false;
        ['top','right','bottom','left'].forEach(function(sd){
          if(cs.getPropertyValue('border-'+sd+'-style')!=='none'&&parseFloat(cs.getPropertyValue('border-'+sd+'-width'))>0) hasB=true;
        });
        var hasS=!!cs.boxShadow&&cs.boxShadow!=='none';
        var oBg=t.getAttribute('data-ceflatbg')!=null, oBd=t.getAttribute('data-ceflatbd')!=null, oSh=t.getAttribute('data-ceflatsh')!=null;
        if(hasC||hasI||hasB||hasS||oBg||oBd||oSh){
          var lb=(i===0)?'ここ〈'+t.tagName.toLowerCase()+'〉':'外側'+i+'〈'+t.tagName.toLowerCase()+'〉';
          out.push({el:t, sw:_flatSw(cs), name:lb+' '+Math.round(r.width)+'×'+Math.round(r.height),
            bg:(hasC||hasI||oBg), bd:(hasB||oBd), sh:(hasS||oSh),
            tag:(hasC?' 🎨色':'')+(hasI?' 🖼絵':'')+(hasB?' ▭フチ':'')+(hasS?' ☁影':'')+((oBg||oBd||oSh)?' 【消し済み】':'')});
        }
      }
      if(/^(SECTION|HEADER|FOOTER|MAIN)$/.test(t.tagName)) break;  // ページの器まで再帰したら打ち切り
    }
    out=out.slice(0,4);
    // ★「後ろに浮いている飾り」（ピンクの丸・にじみ・リング等）は先祖ではないので上の輪では絶対に拾えない。
    //   さらに pointer-events:none なので右クリックでも掴めない＝ここに出さないと素人には消す手段が無い。
    //   ツール製(ce_bgdeco/ce_outlinedeco)もクローン元デザインの飾りも同じ扱いで並べる。
    try{
      var host=el.closest('section,header,footer,main')||document.body;
      var r0=el.getBoundingClientRect(), near=[], far=[];
      [].slice.call(host.querySelectorAll('div,span,i,b,em,figure,aside')).slice(0,600).forEach(function(n){
        if(n===el||n.contains(el)) return;
        if(n.closest&&n.closest('[id^="__ce"]')) return;
        if((n.textContent||'').trim()!=='') return;                          // 文字が入っていたら飾りではない
        if(n.querySelector&&n.querySelector('img,video,svg,picture,canvas,input,button,a')) return;
        var c2; try{ c2=getComputedStyle(n); }catch(_){ return; }
        var floaty=(c2.pointerEvents==='none')||(parseFloat(c2.zIndex)<0)||/ce_bgdeco|ce_outlinedeco/.test(n.className||'');
        if(!floaty) return;
        if(c2.position!=='absolute'&&c2.position!=='fixed') return;
        var off=(n.getAttribute('data-ceflatvis')!=null);
        var shape=(c2.backgroundImage&&c2.backgroundImage!=='none')
          ||(c2.backgroundColor&&c2.backgroundColor!=='transparent'&&c2.backgroundColor.indexOf('rgba(0, 0, 0, 0)')<0)
          ||(c2.borderStyle&&c2.borderStyle!=='none'&&parseFloat(c2.borderWidth)>0)||(c2.filter&&c2.filter!=='none');
        if(!shape&&!off) return;
        var r2=n.getBoundingClientRect();
        if(!off&&(r2.width<24||r2.height<24)) return;
        var rd=parseFloat(c2.borderTopLeftRadius)||0;
        var it={el:n, sw:_flatSw(c2), bg:false, bd:false, sh:false, deco:true,
          name:'🌸 後ろに浮いている飾り〈'+n.tagName.toLowerCase()+'〉'+Math.round(r2.width)+'×'+Math.round(r2.height)+(rd?'（丸み）':''),
          tag:off?' 【消し済み】':''};
        var hit=!(r2.right<r0.left||r2.left>r0.right||r2.bottom<r0.top||r2.top>r0.bottom);  // 選んだ要素と重なっているか
        (hit?near:far).push(it);
      });
      out=out.concat(near.slice(0,3));
      if(!near.length) out=out.concat(far.slice(0,2));   // 重なっていなくても同じセクション内なら候補に出す
    }catch(_){}
    return out.slice(0,7);
  }
  // 1種類を消す／戻す（同じボタンでトグル＝押し間違えても怖くない）。戻り値 true=今消した
  function flatOff(it,key){
    var el=it.el, a='data-ceflat'+key, props=FLAT_P[key]||[];
    var saved=el.getAttribute(a);
    if(saved!=null){                                   // ← 戻す
      props.forEach(function(p){ el.style.removeProperty(p); });
      if(key==='bd') el.style.removeProperty('border-style');
      try{ var o=JSON.parse(saved); Object.keys(o).forEach(function(p){ el.style.setProperty(p,o[p][0],o[p][1]); }); }catch(_){}
      el.removeAttribute(a); markDirty(); return false;
    }
    var sv={};                                         // ← 消す（先に元の値を控える）
    props.forEach(function(p){ var v=el.style.getPropertyValue(p); if(v) sv[p]=[v, el.style.getPropertyPriority(p)]; });
    el.setAttribute(a, JSON.stringify(sv));
    if(key==='bg'){ el.style.setProperty('background-color','transparent','important'); el.style.setProperty('background-image','none','important'); }
    else if(key==='bd'){ el.style.setProperty('border-style','none','important'); }
    else if(key==='vis'){ el.style.setProperty('display','none','important'); }   // 浮いてる飾りは消さずに隠す＝戻せる
    else { el.style.setProperty('box-shadow','none','important'); }
    markDirty(); return true;
  }
  // ===== 📏 余白を詰める（AIなし・2026-07-21） =====
  // ★「親を選んで高さを縮める」がうまくいかない理由＝カンプのセクションはたいてい
  //   min-height:720px(100vh) を持っていて、height をいくら縮めても min-height に負ける。
  //   さらに padding:120px 0 が乗る。だから「原因を名指しして、その場で外す」形にする。
  function padInfo(el){
    if(!el||el.nodeType!==1) return null;
    var cs; try{ cs=getComputedStyle(el); }catch(_){ return null; }
    var r=el.getBoundingClientRect();
    if(r.height<80) return null;
    var top=1e9, bot=-1e9, n=0;
    [].slice.call(el.children).forEach(function(c){
      if(c.closest&&c.closest('[id^="__ce"]')) return;
      var k=c.getBoundingClientRect(); if(!k.width||!k.height) return;
      n++; if(k.top<top) top=k.top; if(k.bottom>bot) bot=k.bottom;
    });
    var contentH=n?(bot-top):0;
    return {el:el, h:r.height, contentH:contentH, 余り:Math.round(r.height-contentH), 子数:n,
      minH:Math.round(parseFloat(cs.minHeight)||0), padT:Math.round(parseFloat(cs.paddingTop)||0),
      padB:Math.round(parseFloat(cs.paddingBottom)||0), 空:(n===0&&!(el.textContent||'').trim())};
  }
  // ★クリック地点から body まで全部辿って「余白の原因になっている指定」を持つ箱を全部集める。
  //   セクションで打ち切っていたのが致命傷だった：実際の真犯人は <main> の min-height:10278px で、
  //   これがある限り中のセクションをいくら縮めてもページは1pxも縮まない（実測で判明）。
  function padChain(el){
    var out=[], t=el;
    for(var i=0; t && i<14 && t.tagName!=='HTML'; i++, t=t.parentElement){
      if(t.closest && t.closest('[id^="__ce"]')) continue;
      var cs; try{ cs=getComputedStyle(t); }catch(_){ continue; }
      var r=t.getBoundingClientRect(); if(r.height<60) continue;
      var kids=[].slice.call(t.children).filter(function(c){
        if(c.closest && c.closest('[id^="__ce"]')) return false;
        var k=c.getBoundingClientRect(); return k.width>0&&k.height>0;
      });
      var top=1e9, bot=-1e9;
      kids.forEach(function(c){ var k=c.getBoundingClientRect(); if(k.top<top) top=k.top; if(k.bottom>bot) bot=k.bottom; });
      var contentH=kids.length?(bot-top):0;
      var minH=parseFloat(cs.minHeight)||0;
      var fixH=(cs.height!=='auto')?(parseFloat(cs.height)||0):0;
      var pad=(parseFloat(cs.paddingTop)||0)+(parseFloat(cs.paddingBottom)||0);
      var slack=Math.round(r.height-contentH);
      // ★自然な高さ（中身＋余白）＝min-heightを外した時に落ち着く高さ。
      //   これを超えるmin-heightだけが「本当の犯人」＝外せば縮む。中身＋余白がmin-heightを既に
      //   上回っているなら、min-heightを外しても1pxも縮まない（＝以前これで「詰められない」が再発した）。
      var naturalH=contentH+pad;
      var why='', kind='';
      if(minH>naturalH+20){ why='最小の高さ '+Math.round(minH)+'px'; kind='minh'; }
      else if(fixH>naturalH+20 && cs.position!=='absolute' && cs.position!=='fixed'){ why='固定の高さ '+Math.round(fixH)+'px'; kind='minh'; }
      else if(pad>=80 && slack>=80){ why='上下の余白 '+Math.round(pad)+'px'; kind='pad'; }
      else if(kids.length===0 && !(t.textContent||'').trim() && r.height>=100){ why='中身が空なのに '+Math.round(r.height)+'px'; kind='minh'; }
      if(!why) continue;
      out.push({el:t, kind:kind, pad:Math.round(pad),
        name:t.tagName.toLowerCase()+((t.className&&typeof t.className==='string'&&t.className.trim())?('.'+t.className.trim().split(/\\s+/)[0]):''),
        why:why, slack:slack, h:Math.round(r.height)});
    }
    out.sort(function(a,b){ return b.h-a.h; });   // 大きい箱＝影響が大きい順
    return out.slice(0,6);
  }
  // ★「まとめて詰める」は原因に応じて手を変える＝押して必ず縮む。
  //   min-heightが犯人→外す。余白(padding)が犯人→上下paddingを削る（min-height除去だけでは
  //   1pxも縮まないケースが実在した＝flex中央寄せセクションで中身＋余白がmin-heightを超えている時）。
  function padCrush(list){
    list.forEach(function(o){
      var el=o.el, h0=el.getBoundingClientRect().height;
      el.style.setProperty('min-height','0','important');
      el.style.setProperty('height','auto','important');
      // min-height除去で縮まなかった＝本当の原因は余白。上下paddingを詰めて確実に縮める。
      if(el.getBoundingClientRect().height > h0-20){
        var cs=getComputedStyle(el);
        var pt=parseFloat(cs.paddingTop)||0, pb=parseFloat(cs.paddingBottom)||0;
        if(pt>40) el.style.setProperty('padding-top','40px','important');
        if(pb>40) el.style.setProperty('padding-bottom','40px','important');
      }
    });
    markDirty();
  }
  // ★padTargetだけでは「箱の中のムダ」しか見つけられない。実際に多いのは
  //   「要素と要素のあいだ（margin/gap）」で、そこをクリックしても箱の余りは小さく出て
  //   何も出ない＝「押しても変わらない」になる。→ クリックしたY座標の上下にある実体を探して
  //   その隙間を実測し、原因（margin / gap / padding / min-height）をまとめて潰せるようにする。
  function gapAt(el,y){
    var t=el, hop=0;
    while(t && hop<8 && t!==document.body && t.tagName!=='HTML'){
      var kids=[].slice.call(t.children).filter(function(c){
        if(c.closest&&c.closest('[id^="__ce"]')) return false;
        var r=c.getBoundingClientRect(); return r.width>0&&r.height>0;
      });
      var tr=t.getBoundingClientRect();
      if(kids.length){
        var above=null,below=null,ab=-1e9,bt=1e9;
        kids.forEach(function(c){
          var r=c.getBoundingClientRect();
          if(r.bottom<=y+1 && r.bottom>ab){ ab=r.bottom; above=c; }
          if(r.top>=y-1 && r.top<bt){ bt=r.top; below=c; }
        });
        var topEdge=above?ab:tr.top, botEdge=below?bt:tr.bottom;
        var g=Math.round(botEdge-topEdge);
        if(g>=40 && y>=topEdge-1 && y<=botEdge+1) return {box:t, above:above, below:below, gap:g};
      } else if(tr.height>=40 && y>=tr.top && y<=tr.bottom){
        return {box:t, above:null, below:null, gap:Math.round(tr.height)};   // 中身が空の箱
      }
      t=t.parentElement; hop++;
    }
    return null;
  }
  function gapClose(info){
    if(!info) return 0;
    var before=info.gap;
    if(info.above) info.above.style.setProperty('margin-bottom','0','important');
    if(info.below) info.below.style.setProperty('margin-top','0','important');
    var cs; try{ cs=getComputedStyle(info.box); }catch(_){ cs=null; }
    if(cs){
      if(/(flex|grid)/.test(cs.display) && (parseFloat(cs.rowGap)||0)>0) info.box.style.setProperty('row-gap','0','important');
      if(!info.above && (parseFloat(cs.paddingTop)||0)>0) info.box.style.setProperty('padding-top','0','important');
      if(!info.below && (parseFloat(cs.paddingBottom)||0)>0) info.box.style.setProperty('padding-bottom','0','important');
    }
    info.box.style.setProperty('min-height','0','important');   // 最後の砦：これが残っていると全部無駄になる
    markDirty();
    return before;
  }
  function gapReset(info){
    if(!info) return;
    if(info.above) info.above.style.removeProperty('margin-bottom');
    if(info.below) info.below.style.removeProperty('margin-top');
    ['row-gap','padding-top','padding-bottom','min-height'].forEach(function(p){ info.box.style.removeProperty(p); });
    markDirty();
    if(msg) msg.textContent='この隙間の調整を元に戻しました';
  }
  function padTighten(el){
    el.style.setProperty('min-height','0','important');   // ★これが本命：高さを縮めても効かない原因
    el.style.setProperty('height','auto','important');
    markDirty();
    if(msg) msg.textContent='最小の高さを外して、中身の分だけの高さにしました（💾保存で確定・⟲戻すで取り消し）';
  }
  function padShrink(el,ratio){
    var cs=getComputedStyle(el);
    var t=Math.max(0,Math.round((parseFloat(cs.paddingTop)||0)*ratio));
    var b=Math.max(0,Math.round((parseFloat(cs.paddingBottom)||0)*ratio));
    el.style.setProperty('padding-top',t+'px','important');
    el.style.setProperty('padding-bottom',b+'px','important');
    markDirty();
    if(msg) msg.textContent='上下の余白を '+t+'px / '+b+'px にしました（続けて押すともっと詰まります）';
  }
  function padReset(el){
    ['min-height','height','padding-top','padding-bottom'].forEach(function(p){ el.style.removeProperty(p); });
    markDirty();
    if(msg) msg.textContent='余白を元に戻しました';
  }
  // ===== 🕳 ドラッグで空いた「穴」を埋める（AIなし・2026-07-21） =====
  // ★これが「余白が消せない」の本当の犯人だった（実測で判明）。
  //   ドラッグ移動は translate＝**見た目だけ**動かす仕組みなので、元いた場所に
  //   その要素の高さぶんの空白が残り続ける。padding も min-height も持っていないので
  //   📏でいくら押しても1pxも縮まない＝「押しても変わらない」の正体。
  //   → translate を margin に振り替えれば、見た目はそのままで穴だけ閉じる。
  // ★対象は「縦に積まれた大きいブロック」だけに絞る（実測での大事故防止）。
  //   小さい飾りや文字を margin に振り替えると、translate と違って**兄弟まで押し出す**ので
  //   ページ全体の見た目が変わってしまった（13個拾って別セクションが68pxズレた）。
  //   section/header/footer など「縦に1列で並ぶ箱」だけなら、margin＝translate と見た目が一致する。
  // ➡ 「ページの右側に上から下まで余白ができる」の原因＝横にはみ出している要素を探す（AIなし）。
  //   ★これが起きると犯人は画面の外（右）に居るので右クリックで選べない＝手では直せない。
  //     実例：ドラッグ事故でボタンが右に1545px飛び、ページ幅が1440→2090pxになっていた。
  //   親が犯人なら子は出さない（同じ原因で何十件も並ぶのを防ぐ）。
  function overflowScan(lim){
    var de=document.documentElement, vw=de.clientWidth;
    if(de.scrollWidth<=vw+4) return [];
    var out=[];
    [].slice.call(document.querySelectorAll('body *')).forEach(function(el){
      if(el.closest('[id^="__ce"]')) return;
      if(el.tagName==='SCRIPT'||el.tagName==='STYLE') return;
      var cs=getComputedStyle(el);
      if(cs.position==='fixed') return;              // 画面に貼り付く物は横スクロールを作らない
      var r=el.getBoundingClientRect();
      if(r.width<2||r.height<2) return;
      var over=Math.round(r.right-vw);
      if(over<=4) return;
      out.push({el:el, over:over, drag:!!(el.getAttribute('data-cetx')||el.getAttribute('data-cety')), wide:Math.round(r.width)>vw});
    });
    out.sort(function(a,b){ return b.over-a.over; });
    out=out.slice(0,120);
    return out.filter(function(o){ return !out.some(function(p){ return p!==o && p.el.contains(o.el); }); }).slice(0, lim||6);
  }
  // ★ウィンドウ幅に依存しない直し方（2026-07-25）。
  //   はみ出しは「今開いている幅」でしか判定できないので、1900pxで直しても1280pxではまた出る（実測）。
  //   横ズレの正体はほぼ「誤ドラッグ」なので、data-cetx（ドラッグの署名）が右向きに付いている物を
  //   幅に関係なく全部0に戻す＝どの画面幅で見ても再発しない。
  function ovDragXAll(){
    var list=[].slice.call(document.querySelectorAll('[data-cetx]')).filter(function(el){
      if(el.closest('[id^="__ce"]')) return false;
      return (parseFloat(el.getAttribute('data-cetx'))||0) > 2;   // 右へ動かされている物だけ
    });
    list.forEach(function(el){
      try{ pushUndo(el); }catch(_){}
      var ty=parseFloat(el.getAttribute('data-cety')||'0')||0;
      el.style.setProperty('translate','0px '+ty+'px');
      el.setAttribute('data-cetx','0');
    });
    markDirty();
    var de=document.documentElement;
    if(msg) msg.textContent='⟲ 横のドラッグズレを '+list.length+'箇所 戻しました（縦の調整は残しています）。ページ幅 '+de.scrollWidth+'px／画面 '+de.clientWidth+'px・💾保存で確定';
    return list.length;
  }
  // 🧢 画面に貼り付く帯（固定ヘッダー等）のズレ。★これは横スクロールを作らないので上のはみ出し検査に
  //   引っかからないが、見た目は「右にビヨーン」と一番目立つ。実例：CSSは width:calc(100% - 16px) なのに
  //   インラインへ width:1819px と translate:-305px が焼き込まれ、中央寄せのtransformも消えていた。
  function stickyScan(){
    var vw=document.documentElement.clientWidth, out=[];
    [].slice.call(document.querySelectorAll('body *')).forEach(function(el){
      if(el.closest('[id^="__ce"]')) return;
      var cs=getComputedStyle(el);
      if(cs.position!=='fixed'&&cs.position!=='sticky') return;
      var r=el.getBoundingClientRect();
      if(r.height<20||r.width<vw*0.35) return;
      var pxw=/px\s*$/.test(el.style.width||'');
      var tx=Math.abs(parseFloat(el.getAttribute('data-cetx')||'0'))||0;
      var off=(r.left<-8)||(r.right>vw+8);
      if(off||pxw||tx>2) out.push({el:el, left:Math.round(r.left), right:Math.round(r.right), w:Math.round(r.width), pxw:pxw});
    });
    return out.slice(0,4);
  }
  function stickyFix(){
    var list=stickyScan();
    list.forEach(function(o){
      try{ pushUndo(o.el); }catch(_){}
      // インラインに焼き込まれた「幅・横位置」だけ剥がす＝ページ自身のCSS（width:calc(100% - 16px)や
      // translateX(-50%)の中央寄せ）が復活する。色や影などの他の編集は触らない。
      ['translate','width','max-width','transform','left','right','margin-left'].forEach(function(p){ o.el.style.removeProperty(p); });
      o.el.removeAttribute('data-cetx'); o.el.removeAttribute('data-cety');
      // ★幅を戻したあとに「元のtransform」を測り直す。先に測ると、事故のpx幅で中央寄せ判定に失敗して
      //   matrix(px固定)のまま控えてしまい、別の画面幅でまた横ズレする（実測で判明）。
      try{ if(window.__fxaSetTf0) window.__fxaSetTf0(o.el, true); }catch(_){}
    });
    markDirty();
    var vw=document.documentElement.clientWidth;
    var after=stickyScan().length;
    if(msg) msg.textContent='🧢 画面に貼り付く帯を '+list.length+'箇所 元に戻しました'+(after?('（まだ'+after+'箇所ズレています）'):'（画面ぴったりに戻っています）')+'・💾保存で確定';
    return list.length;
  }
  function ovDragXCount(){
    var n=0;
    [].slice.call(document.querySelectorAll('[data-cetx]')).forEach(function(el){
      if(el.closest('[id^="__ce"]')) return;
      if((parseFloat(el.getAttribute('data-cetx'))||0) > 2) n++;
    });
    return n;
  }
  function ovName(el){
    var c=(el.className||'').toString().trim().split(/\\s+/)[0]||'';
    return el.tagName.toLowerCase()+(c?('.'+c):'');
  }
  // 見つけた犯人を直す：①ドラッグで横に動かされたもの＝横方向だけ0に戻す（縦の調整は残す）
  //                     ②画面より広いもの＝max-width:100% で画面内に収める
  function overflowFix(){
    var total=_ovPass(), tries=0;
    // ★1回で終わらせない：画像の遅れ読み込みや出現アニメで版が動き、直した直後は1440pxでも
    //   その後また1654pxへ戻ることがある（実測）。4秒ほど見張って、増えるたびに掃除し直す。
    var iv=setInterval(function(){
      total+=_ovPass(); tries++;
      var de=document.documentElement, left=de.scrollWidth-de.clientWidth;
      if(msg) msg.textContent = (left>4)
        ? ('➡ '+total+'箇所を直しました。まだ '+left+'px はみ出しています（見張り中…）')
        : ('➡ 右のはみ出しを直しました（'+total+'箇所）。ページ幅 '+de.scrollWidth+'px＝画面ぴったりです・💾保存で確定');
      if(tries>=8){
        clearInterval(iv);
        if(left>4 && msg) msg.textContent='➡ '+total+'箇所を直しましたが、まだ '+left+'px はみ出しています（もう一度押すか、右クリック→⟲位置・サイズをリセット）';
      }
    }, 500);
    markDirty();
    return total;
  }
  function _ovPass(){
    var n=0;
    for(var pass=0; pass<8; pass++){          // 1回では取り切れない（直すと次の犯人が顔を出す）ので繰り返す
      var list=overflowScan(40);
      if(!list.length) break;
      list.forEach(function(o){
        var el=o.el, vw=document.documentElement.clientWidth;
        try{ pushUndo(el); }catch(_){}
        var tx=parseFloat(el.getAttribute('data-cetx')||'0')||0;
        var ty=parseFloat(el.getAttribute('data-cety')||'0')||0;
        if(o.drag && tx>0){
          // ★右へドラッグされた事故＝横だけ元の位置に戻す（縦の調整は残す）
          el.style.setProperty('translate','0px '+ty+'px');
          el.setAttribute('data-cetx','0'); n++;
        }else{
          // ドラッグ以外（左へ動かした物・元から大きい飾り）は「はみ出した分だけ左へ寄せる」。
          //   ★ここで0に戻すと、左へドラッグしてある物は逆に右へ飛んで悪化する（実際に起きた）。
          var nx=Math.round(tx-o.over);
          el.style.setProperty('translate',nx+'px '+ty+'px');
          el.setAttribute('data-cetx',String(nx)); n++;
        }
        if(Math.round(el.getBoundingClientRect().width)>vw){
          el.style.setProperty('max-width','100%','important');
          el.style.setProperty('box-sizing','border-box','important');
          n++;
        }
      });
    }
    return n;
  }
  function dragHoles(){
    var out=[];
    [].slice.call(document.querySelectorAll('[data-cety],[data-cehole]')).forEach(function(t){
      if(t.closest && t.closest('[id^="__ce"]')) return;
      var pt=t.parentElement&&t.parentElement.tagName;
      var block=/^(SECTION|HEADER|FOOTER)$/.test(t.tagName) || pt==='MAIN' || pt==='BODY';
      if(!block) return;
      var ty=parseFloat(t.getAttribute('data-cety'))||0;
      if(t.hasAttribute('data-cehole')) ty=parseFloat((t.getAttribute('data-cehole')||'').split(',')[1])||0;
      if(Math.abs(ty)<40) return;                             // 微調整は穴にならないので出さない
      var r=t.getBoundingClientRect(); if(r.height<80) return;
      out.push({el:t, ty:Math.round(ty), h:Math.round(r.height), done:t.hasAttribute('data-cehole'),
        name:t.tagName.toLowerCase()+((t.className&&typeof t.className==='string'&&t.className.trim())?('.'+t.className.trim().split(/\\s+/)[0]):'')});
    });
    out.sort(function(a,b){ return Math.abs(b.ty)-Math.abs(a.ty); });
    return out.slice(0,6);
  }
  // ★穴を閉じても main に min-height:10278px のような「今の高さ」が焼き付いていると
  //   ページは1pxも縮まない（実測で判明）。入れ物系だけ最小の高さを外す。
  //   ★section/header/footer は外さない＝デザインの高さを勝手に潰さないため（それは📏の仕事）。
  function _holeUnpin(el){
    var t=el.parentElement, n=0;
    while(t && t.tagName!=='HTML' && n<8){
      if(!/^(SECTION|HEADER|FOOTER|ARTICLE|ASIDE)$/.test(t.tagName)){
        var cs; try{ cs=getComputedStyle(t); }catch(_){ cs=null; }
        if(cs && (parseFloat(cs.minHeight)||0)>0){
          t.style.setProperty('min-height','0','important');
          t.style.setProperty('height','auto','important');
        }
      }
      t=t.parentElement; n++;
    }
  }
  // ★transitionを一瞬だけ止めてから振り替える：カンプのクローン元CSSは transition:all を
  //   持っていることがあり、margin を書き換えた瞬間にアニメして位置がふらつくため。
  // ★縦(ty)だけを margin に振り替え、横(tx)は translate のまま残す。
  //   margin-left にすると幅が変わって見た目が動くため（縦は積み上げなので一致する）。
  function dragBake(o){
    var el=o.el; if(el.hasAttribute('data-cehole')) return;   // 二重に足さない
    var tx=parseFloat(el.getAttribute('data-cetx'))||0;
    var mt=parseFloat(getComputedStyle(el).marginTop)||0;
    el.style.setProperty('transition','none','important');
    el.style.setProperty('margin-top',Math.round(mt+o.ty)+'px','important');
    el.style.setProperty('translate',tx+'px 0','important');
    el.setAttribute('data-cety','0');
    el.setAttribute('data-cehole',tx+','+o.ty);              // ⟲で戻せるように元のズレを覚えておく
    _holeUnpin(el);
    requestAnimationFrame(function(){ try{ el.style.removeProperty('transition'); }catch(_){} });
    markDirty();
  }
  function dragUnbake(el){
    if(!el.hasAttribute('data-cehole')) return;              // まだ埋めていないものは触らない
    var v=(el.getAttribute('data-cehole')||'').split(','), tx=parseFloat(v[0])||0, ty=parseFloat(v[1])||0;
    el.style.setProperty('transition','none','important');
    el.style.removeProperty('margin-top');
    el.style.setProperty('translate',tx+'px '+ty+'px','important');
    el.setAttribute('data-cetx',tx); el.setAttribute('data-cety',ty);
    el.removeAttribute('data-cehole');
    requestAnimationFrame(function(){ try{ el.style.removeProperty('transition'); }catch(_){} });
    markDirty();
  }
  // ===== 🎞 スライドショーを止めて「見せたい1枚」に固定（AIなし・2026-07-21） =====
  // カンプでは「どんどん切り替わる」と困る（右クリックで掴めない・スクショが撮れない・
  // 見せたい絵が映らない）。ライブラリ(Splide/Swiper/slick)の停止APIには頼らず、
  // CSSの!importantで見た目を固定する＝インスタンスが取れないクローンでも確実に効く。
  // ★スタイルシート側で!important＝ライブラリが後からstyle.opacityを書いても勝てる
  //   （インラインに!importantを付けても、ライブラリの代入で優先度ごと消えるため）。
  function _sliderCss(){
    if(document.getElementById('ce-slidefix')) return;
    var st=document.createElement('style'); st.id='ce-slidefix';   // __ce系にしない＝保存に残す
    st.textContent='[data-cefreeze] .splide__slide,[data-cefreeze] .swiper-slide,[data-cefreeze] .slick-slide{display:none!important}'
      +'[data-cefreeze] .cefreeze-on{display:flex!important;opacity:1!important;visibility:visible!important;transform:none!important;z-index:2!important;position:relative!important}';
    (document.head||document.documentElement).appendChild(st);
  }
  function _sliderAt(el){
    if(!el||!el.closest) return null;
    var root=el.closest('.splide,.swiper,.swiper-container,.slick-slider');
    if(!root) return null;
    var items=[].slice.call(root.querySelectorAll('.splide__slide,.swiper-slide,.slick-slide')).filter(function(s){
      return !s.classList.contains('splide__slide--clone') && !s.classList.contains('swiper-slide-duplicate') && !s.classList.contains('slick-cloned');
    });
    if(items.length<2) return null;
    return {root:root, items:items, frozen:root.getAttribute('data-cefreeze')!=null};
  }
  // スライドの見た目（サムネ用の画像URL）を1枚ぶん取る
  function _slideThumb(s){
    var im=s.querySelector('img');
    if(im&&im.getAttribute('src')) return im.getAttribute('src');
    var n=s, hop=0;
    while(n&&hop<3){ var bi=''; try{ bi=getComputedStyle(n).backgroundImage; }catch(_){}
      if(bi&&bi!=='none'&&bi.indexOf('gradient')<0){ var m=bi.match(/url\\(["']?([^"')]+)/); if(m) return m[1]; }
      n=n.firstElementChild; hop++; }
    return '';
  }
  function sliderFreeze(info, idx){
    if(!info) return;
    _sliderCss();
    info.root.setAttribute('data-cefreeze','1');
    info.items.forEach(function(s,i){ s.classList.toggle('cefreeze-on', i===idx); });
    markDirty();
    if(msg) msg.textContent='スライドショーを止めて '+(idx+1)+'枚目に固定しました（💾保存で確定・「切り替わりに戻す」で解除）';
  }
  function sliderUnfreeze(info){
    if(!info) return;
    info.root.removeAttribute('data-cefreeze');
    info.items.forEach(function(s){ s.classList.remove('cefreeze-on'); });
    markDirty();
    if(msg) msg.textContent='スライドショーの切り替わりを元に戻しました（💾保存で確定）';
  }
  // ===== 文章の一部だけ色を変える：ドラッグで文字を選ぶ→小さな色ボタンが出る（AIなし）=====
  (function(){
    var pop=null, curSpan=null, savedRange=null, curHl=null, curUdot=null;
    function hidePop(){ if(pop){ pop.remove(); pop=null; } curSpan=null; savedRange=null; curHl=null; curUdot=null; }
    function inUI(node){ var el=node&&(node.nodeType===1?node:node.parentElement); return el&&el.closest&&(el.closest('[id^="__ce"]')||el.closest('#__ce_selc')||el.closest('#__ce_toast')); }
    // 選んだ範囲に色を当てる。中に色付きの子span（1文字ずつの.fxa_ch等の!important）があると
    // 囲むだけでは負けるので、子孫の色も全部この色で上書きする。
    function _forceColor(root, color){
      root.style.setProperty('color',color,'important'); root.style.setProperty('-webkit-text-fill-color',color,'important');
      // グラデ文字(background-clip:text)は単色化すると敷き背景が箱として見える→背景ごと外す
      try{ var _pc=getComputedStyle(root); if(((_pc.webkitBackgroundClip||_pc.backgroundClip||'')+'').indexOf('text')>=0) root.style.setProperty('background','none','important'); }catch(_){}
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
    // ⇔ 字間（letter-spacing）：選択文字をspanで囲んで0.5px刻みで広げ/狭める（AIなし・即反映）
    function spacing(step){
      var t=curSpan;
      if(!t && savedRange){
        try{
          t=document.createElement('span');
          try{ savedRange.surroundContents(t); }
          catch(_){ var frag=savedRange.extractContents(); t.appendChild(frag); savedRange.insertNode(t); }
          curSpan=t;
        }catch(err){ if(msg) msg.textContent='この範囲は字間を変えられませんでした（別々の要素にまたがっています）'; return; }
      }
      if(!t) return;
      var cur=parseFloat(t.style.letterSpacing);
      if(isNaN(cur)){ cur=parseFloat(getComputedStyle(t).letterSpacing); if(isNaN(cur)) cur=0; }
      var v=Math.max(-2, cur+step);
      t.style.setProperty('letter-spacing', v.toFixed(1)+'px', 'important');
      markDirty();
      if(msg) msg.textContent='字間: '+v.toFixed(1)+'px（保存で確定）';
    }
    // 🔠 文字サイズ：選択文字をspanで囲んで2px刻みで大きく/小さく（AIなし・即反映）
    function fontSize(step){
      var t=curSpan;
      if(!t && savedRange){
        try{
          t=document.createElement('span');
          try{ savedRange.surroundContents(t); }
          catch(_){ var frag=savedRange.extractContents(); t.appendChild(frag); savedRange.insertNode(t); }
          curSpan=t;
        }catch(err){ if(msg) msg.textContent='この範囲は文字サイズを変えられませんでした（別々の要素にまたがっています）'; return; }
      }
      if(!t) return;
      var cur=parseFloat(t.style.fontSize);
      if(isNaN(cur)){ cur=parseFloat(getComputedStyle(t).fontSize); if(isNaN(cur)) cur=16; }
      var v=Math.max(8, Math.min(200, cur+step));
      t.style.setProperty('font-size', Math.round(v)+'px', 'important');
      markDirty();
      if(msg) msg.textContent='文字サイズ: '+Math.round(v)+'px（保存で確定）';
    }
    // 🖍 蛍光ペン：選択文字を .fxa_hl で囲む→スクロールで線が伸びる（保存版でも自動再生）。
    function highlight(color){
      hlPushColorHistory(color);
      if(curHl){ curHl.style.setProperty('--hlc',color); fxHlReplay(curHl); markDirty(); return; }
      if(!savedRange) return;
      try{
        var span=document.createElement('span'); span.className='fxa_hl';
        span.style.setProperty('--hlc',color);
        try{ savedRange.surroundContents(span); }
        catch(_){ var frag=savedRange.extractContents(); span.appendChild(frag); savedRange.insertNode(span); }
        _unwrapAllIn(span,'.fxa_hl');  // 選択範囲に古いマーカーが混ざっていたら剥がして1枚に（二重防止）
        if(typeof ensureFxAssets==='function') ensureFxAssets();  // アニメCSS/監視JSを注入（保存版で動く）
        if(window.__fxaSweepHl) window.__fxaSweepHl(span); else{ span.style.setProperty('--hlw',100); span.classList.add('fxa_in'); }  // 今すぐ線を引く（プレビュー）
        curHl=span; markDirty();
        if(msg) msg.textContent='マーカーを引きました（スクロールで線がスーッと伸びます・保存で確定）';
      }catch(err){ if(msg) msg.textContent='この範囲はマーカーを引けませんでした（別々の要素にまたがっています）'; }
    }
    // 〰 点線の下線：選択文字だけをspanで囲んでborder-bottom:dottedを当てる（AIなし・即反映）。
    // anim=true（既定）：マーカーと同じ仕組み(.fxa_ud・--hlwスイープ)＝スクロールで左から走る。
    // anim=false：従来のborder点線＝最初から引かれた静止下線（走らせたくない場所用・2026-07-19）。
    function underline(color, anim){
      if(curUdot){
        // 旧方式(border)・新方式(--udc)どちらの下線でも色が変わるよう両方に当てる
        curUdot.style.setProperty('border-bottom-color',color,'important');
        curUdot.style.setProperty('--udc',color); markDirty(); return;
      }
      if(!savedRange) return;
      try{
        var span=document.createElement('span');
        if(anim===false){
          span.className='ceud';
          span.style.setProperty('border-bottom','3px dotted '+color,'important');
          span.style.setProperty('padding-bottom','0.15em','important');
        } else {
          span.className='ceud fxa_ud';
          span.style.setProperty('--udc',color);
          // 既定0.45sだと短い文は一瞬で引き終わり「走ってない」ように見える（実際に報告あり）→倍ゆっくりに
          span.style.setProperty('--hldur','0.9s');
        }
        try{ savedRange.surroundContents(span); }
        catch(_){ var frag=savedRange.extractContents(); span.appendChild(frag); savedRange.insertNode(span); }
        _unwrapAllIn(span,'.ceud');  // 選択範囲に古い下線が混ざっていたら剥がして1本に（二重防止）
        if(anim!==false){
          if(typeof ensureFxAssets==='function') ensureFxAssets();  // アニメCSS/監視JSを注入（保存版で動く）
          if(window.__fxaSweepHl) window.__fxaSweepHl(span); else{ span.style.setProperty('--hlw',100); span.classList.add('fxa_in'); }  // 今すぐ引く（プレビュー）
        }
        curUdot=span; markDirty();
        if(msg) msg.textContent=(anim===false)
          ?'点線の下線をつけました（静止・保存で確定）'
          :'走る下線をつけました（スクロールで左からスーッと・⏳動きの演出にも並びます・保存で確定）';
      }catch(err){ if(msg) msg.textContent='この範囲には下線をつけられませんでした（別々の要素にまたがっています）'; }
    }
    // root の中にある同種の装飾spanを全部剥がす（2026-07-19・二重マーカー/二重下線の根治）。
    // 既に装飾が付いた範囲を含めて選び直して追加すると「古いspanの上に新しいspan」の入れ子ができ、
    // (1)二重に見える (2)消しても外側1枚しか剥がれず内側が残る、が実際に起きた。
    // decoStrip＝包みspanなら剥がす／見出し等ならクラスだけ落とす（要素ごと消さない安全版）
    function _unwrapAllIn(root, sel){ [].slice.call(root.querySelectorAll(sel)).forEach(decoStrip); }
    // 選択範囲(savedRange)に重なる装飾spanを全部集める＝「またがって選んでも一発で消せる」用
    function _touching(sel){
      var out=[];
      [].slice.call(document.querySelectorAll(sel)).forEach(function(el){
        try{ if(savedRange && savedRange.intersectsNode(el)) out.push(el); }catch(_){}
      });
      return out;
    }
    function removeHl(){
      var list=_touching('.fxa_hl');
      if(curHl && list.indexOf(curHl)<0) list.push(curHl);
      if(!list.length){ if(msg) msg.textContent='ここにはマーカーがありません'; return; }
      list.forEach(function(el){ _unwrapAllIn(el,'.fxa_hl'); decoStrip(el); });  // 入れ子・複数もまとめて剥がす
      curHl=null;     // ★hidePopは呼ばない＝選択の記憶を残す（続けて下線ボタンも効く）
      if(msg) msg.textContent='マーカーを消しました（保存で確定）';
    }
    function removeUnderline(){
      // ★.ceud（選択して包んだ下線）だけでなく .fxa_ud（要素そのものに付いた走る下線）と
      //   data-ceudot（⚙メニューの点線下線）も対象にする＝「消す」が出ない/効かない事故の根治
      var list=_touching(UD_SEL);
      if(curUdot && list.indexOf(curUdot)<0) list.push(curUdot);
      if(!list.length){ if(msg) msg.textContent='ここには下線がありません'; return; }
      list.forEach(function(el){ _unwrapAllIn(el,'.ceud,.fxa_ud'); decoStrip(el); });  // 入れ子・複数もまとめて剥がす
      curUdot=null;   // ★hidePopは呼ばない＝選択の記憶を残す（続けてマーカーボタンも効く）
      if(msg) msg.textContent='点線の下線を消しました（保存で確定）';
    }
    document.addEventListener('mouseup', function(e){
      if(e.button!==0) return;  // ★右クリックのmouseupで覚えた選択(savedRange)を消さない＝「✂選択中」がメニューで効かなくなる事故の防止
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
          var exUd=ancEl.closest(UD_SEL); if(exUd) curUdot=exUd;
        }
        // ★2026-07-19：マーカーより「広い範囲」を選ぶと上の判定では見つからず、
        //   メニューが「消す」にならない実害があった → 選択範囲に重なるspanも検出する
        if(!curHl||!curUdot){
          try{
            [].slice.call(document.querySelectorAll(DECO_SEL)).forEach(function(el2){
              if(!rng.intersectsNode(el2)) return;
              if(!curHl && el2.classList.contains('fxa_hl')) curHl=el2;
              if(!curUdot && el2.matches(UD_SEL)) curUdot=el2;
            });
          }catch(_){}
        }
        // ★2026-07-11：ここに出していた黒い小ポップアップは廃止（操作が2箇所に割れて分かりにくいため）。
        //   選択はこの関数の変数(savedRange/curHl/curUdot)に覚えるだけにして、色・マーカー・下線の操作は
        //   右クリックメニューの「✂ 選択中の文字」ブロック（window.__ceSel経由）に一本化した。
        if(msg) msg.textContent='文字を選択中：そのまま右クリック→「✂ 選択中の文字」で色・サイズ・マーカー・下線（AIなし）';
      }, 10);
    }, true);
    // ※スクロールでは選択を消さない（選んでからスクロールして右クリックすることがある）
    // 右クリックメニューから使う窓口。選択の実体(savedRange等)はこの関数の中に閉じたまま外に出さない。
    window.__ceSel={
      has:function(){ return !!(savedRange||curHl||curUdot); },
      text:function(){ try{ return savedRange?String(savedRange):((curHl&&curHl.textContent)||(curUdot&&curUdot.textContent)||''); }catch(_){ return ''; } },
      hasHl:function(){ return !!curHl; },
      hasUd:function(){ return !!curUdot; },
      paint:paint, highlight:highlight, underline:underline, spacing:spacing, fontSize:fontSize,
      removeHl:removeHl, removeUd:removeUnderline,
      // 選択文字をspanで包んで返す（✏文字を編集の「選択文字だけ」対象用・2026-07-20）
      wrapSpan:function(){
        if(curSpan) return curSpan;
        if(curHl) return curHl;
        if(curUdot) return curUdot;
        if(!savedRange) return null;
        try{
          var t=document.createElement('span');
          try{ savedRange.surroundContents(t); }
          catch(_){ var f=savedRange.extractContents(); t.appendChild(f); savedRange.insertNode(t); }
          curSpan=t; return t;
        }catch(err){ return null; }
      },
      // 色ドラッグ中の追従用＝履歴を貯めずに色だけ変える（履歴はメニュー側がchangeで1回だけ入れる）
      recolorHl:function(c){ if(curHl){ curHl.style.setProperty('--hlc',c); fxHlReplay(curHl); markDirty(); } },
      clear:hidePop
    };
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
  // ★移動・回転・拡大を当てた瞬間、その要素は「中の浮いた部品(position:absolute/fixed)の基準」に変わる。
  //   基準が外側の大きな箱から自分に切り替わるため、中の部品が急に自分のサイズまで縮む
  //   （実例：丸いCTAの白文字を動かしたら青い丸が320px→160pxに縮んだ）。
  //   変形を当てる前に、中の浮いた部品を「今見えている大きさ・位置」でpx固定して見た目を保つ。
  function _freezeAbsKids(el){
    if(!el||el.__ceAbsFz) return;
    el.__ceAbsFz=1;
    var cs; try{ cs=getComputedStyle(el); }catch(_){ return; }
    var had=(cs.transform&&cs.transform!=='none')||(cs.translate&&cs.translate!=='none')
      ||(cs.rotate&&cs.rotate!=='none'&&parseFloat(cs.rotate)!==0)||(cs.scale&&cs.scale!=='none'&&cs.scale!=='1');
    if(had) return;                       // すでに基準になっている＝今さら変わらない
    var kids=[];
    try{
      [].slice.call(el.querySelectorAll('*')).forEach(function(n){
        if(n.closest&&n.closest('[id^="__ce"]')) return;
        var s; try{ s=getComputedStyle(n); }catch(_){ return; }
        if(s.position!=='absolute'&&s.position!=='fixed') return;
        var r=n.getBoundingClientRect();
        if(r.width>0&&r.height>0) kids.push({el:n, r:r});
      });
    }catch(_){ return; }
    if(!kids.length) return;
    var pr=el.getBoundingClientRect();
    var bl=parseFloat(cs.borderLeftWidth)||0, bt=parseFloat(cs.borderTopWidth)||0;
    kids.forEach(function(k){
      var st=k.el.style;
      st.setProperty('width', Math.round(k.r.width)+'px','important');
      st.setProperty('height', Math.round(k.r.height)+'px','important');
      st.setProperty('left', Math.round(k.r.left-pr.left-bl)+'px','important');
      st.setProperty('top', Math.round(k.r.top-pr.top-bt)+'px','important');
      st.setProperty('right','auto','important');
      st.setProperty('bottom','auto','important');
    });
  }
  function applyTf(el){
    var x=+el.getAttribute('data-cetx')||0, y=+el.getAttribute('data-cety')||0;
    var sx=+el.getAttribute('data-cesx')||1, sy=+el.getAttribute('data-cesy')||1;
    var ro=+el.getAttribute('data-cero')||0;
    // ★インライン要素(display:inline)は translate/rotate/scale を無視する＝ドラッグしても1pxも動かない。
    //   実際に縦書き見出しの文字spanで発生（span.__ce_selを掴んでも移動できない）。移動・変形がある時だけ
    //   inline-block へ上げて効くようにする（保存でinline styleは残るので移動位置も残る）。
    if((x||y||ro||sx!==1||sy!==1)){
      var _disp=''; try{ _disp=getComputedStyle(el).display; }catch(_){}
      if(_disp==='inline') el.style.setProperty('display','inline-block','important');
      _freezeAbsKids(el);   // 中の浮いた部品が縮まないよう、変形を当てる前に今の見た目で固定
    }
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
  // ===== 🧲 掴む枠（透明な枠・空きスペース）を見た目の位置に合わせる（2026-07-30） =====
  // ★このツールのドラッグは translate で動かす＝**レイアウト上の箱は元の場所に残る**。
  //   見た目と箱が離れると「白い所を掴むと離れた所の物が選ばれる」「白い空きが残る」になる。
  //   さらに厄介なのは、アニメ用ラッパー span[data-fxpw]（absolute）が枠で、中身だけ translate で
  //   飛んでいる型：枠と中身は**同じ1個の要素として選ばれる**ので、整列（2個必要）では直せない。
  // ここでは「ズレを箱そのものの位置（left/top・なければmargin）へ移し替える」。
  //   見た目は1pxも動かさず、掴む枠だけが見た目の場所へ来る。
  // 中身（子孫）が実際に描かれている範囲。ツールのUI・見えないものは数えない。
  function inkBoxOf(el){
    if(!el||!el.querySelectorAll) return null;
    var lo=[1e9,1e9], hi=[-1e9,-1e9], n=0;
    var list=[].slice.call(el.querySelectorAll('*')).slice(0,600);
    for(var i=0;i<list.length;i++){
      var c=list[i];
      if(c.id&&c.id.indexOf('__ce')===0) continue;
      if(c.closest&&c.closest('[id^=__ce]')) continue;
      if(c.classList&&c.classList.contains('__ce_hdl')) continue;
      var cs=null; try{ cs=getComputedStyle(c); }catch(_){ continue; }
      if(cs.display==='none'||cs.visibility==='hidden'||parseFloat(cs.opacity)===0) continue;
      var r=c.getBoundingClientRect();
      if(r.width<4||r.height<4) continue;
      if(r.left<lo[0])lo[0]=r.left; if(r.top<lo[1])lo[1]=r.top;
      if(r.right>hi[0])hi[0]=r.right; if(r.bottom>hi[1])hi[1]=r.bottom;
      n++;
    }
    if(!n) return null;
    return {l:lo[0], t:lo[1], w:hi[0]-lo[0], h:hi[1]-lo[1]};
  }
  // ①中身が箱の外へ飛んでいる型（子を1つずつドラッグしたカンプで起きる）。
  //   ★判定は「中身のまん中が箱の外にあるか」。px差で判定すると padding のぶんで毎回引っかかる。
  function frameFitInk(el){
    var ink=inkBoxOf(el); if(!ink) return null;
    var r=el.getBoundingClientRect(); if(!(r.width>0&&r.height>0)) return null;
    var icx=ink.l+ink.w/2, icy=ink.t+ink.h/2;
    if(icx>=r.left-4&&icx<=r.right+4&&icy>=r.top-4&&icy<=r.bottom+4) return null;   // 中に収まっている
    // 内側余白ぶんは残す＝箱の中で中身が端に張り付かず、元の見た目のまま収まる
    var cs=getComputedStyle(el);
    var pl=(parseFloat(cs.paddingLeft)||0)+(parseFloat(cs.borderLeftWidth)||0);
    var pt=(parseFloat(cs.paddingTop)||0)+(parseFloat(cs.borderTopWidth)||0);
    return {dx:ink.l-(r.left+pl), dy:ink.t-(r.top+pt), ink:ink};
  }
  // ②箱そのものがズレていて、枠（親のアニメ用ラッパー／自分のleft-top）が置いていかれている型
  function frameFitFrame(el){
    var t=null; try{ t=_txOf(el); }catch(_){ }
    if(!t||(Math.abs(t.x)<2&&Math.abs(t.y)<2)) return null;
    var pw=el.parentElement;
    if(pw&&pw.getAttribute&&pw.getAttribute('data-fxpw')){
      var cs=null; try{ cs=getComputedStyle(pw); }catch(_){ }
      if(cs&&(cs.position==='absolute'||cs.position==='fixed')) return {kind:'wrap', box:pw, t:t};
    }
    var cs2=null; try{ cs2=getComputedStyle(el); }catch(_){ }
    if(cs2&&(cs2.position==='absolute'||cs2.position==='fixed')) return {kind:'self', box:el, t:t};
    return {kind:'flow', box:el, t:t};   // 通常配置＝marginで箱ごと動かす（隣を押す可能性あり）
  }
  function frameFitInfo(el){
    if(!el||el===document.body) return null;
    var ik=null; try{ ik=frameFitInk(el); }catch(_){ }
    if(ik) return {kind:'ink', x:ik.dx, y:ik.dy};
    var fr=null; try{ fr=frameFitFrame(el); }catch(_){ }
    if(fr) return {kind:fr.kind, x:fr.t.x, y:fr.t.y};
    return null;
  }
  function frameFitApply(el){
    if(!el||el===document.body) return null;
    var done=[];
    // ①まず箱を「中身が飛んで行った先」へ持っていく。
    //   箱を動かすと中身も一緒に動くので、直下の子を同じ量だけ逆に戻す＝中身は1pxも動かない。
    var ik=null; try{ ik=frameFitInk(el); }catch(_){ }
    if(ik&&(Math.abs(ik.dx)>2||Math.abs(ik.dy)>2)){
      var kids=[].slice.call(el.children).filter(function(c){
        return c.nodeType===1 && !(c.id&&c.id.indexOf('__ce')===0) && c.tagName!=='SCRIPT' && c.tagName!=='STYLE';
      });
      setPos(el,(+el.getAttribute('data-cetx')||0)+ik.dx,(+el.getAttribute('data-cety')||0)+ik.dy);
      kids.forEach(function(c){
        setPos(c,(+c.getAttribute('data-cetx')||0)-ik.dx,(+c.getAttribute('data-cety')||0)-ik.dy);
      });
      done.push({kind:'ink', x:Math.round(ik.dx), y:Math.round(ik.dy), n:kids.length});
    }
    // ②そのうえで、透明な枠を箱の位置へ持ってくる（①で箱が動いた分もここで吸収される）
    var f=null; try{ f=frameFitFrame(el); }catch(_){ }
    if(f){
      var tx=f.t.x, ty=f.t.y, box=f.box;
      if(f.kind==='flow'){
        var cs=getComputedStyle(el);
        var ml=parseFloat(cs.marginLeft)||0, mt=parseFloat(cs.marginTop)||0;
        el.style.setProperty('margin-left',Math.round(ml+tx)+'px','important');
        el.style.setProperty('margin-top',Math.round(mt+ty)+'px','important');
      } else {
        var bs=getComputedStyle(box);
        var l=parseFloat(bs.left), tp=parseFloat(bs.top);
        if(!isFinite(l)) l=box.offsetLeft||0;
        if(!isFinite(tp)) tp=box.offsetTop||0;
        box.style.setProperty('left',Math.round(l+tx)+'px','important');
        box.style.setProperty('top',Math.round(tp+ty)+'px','important');
        box.style.setProperty('right','auto','important');    // right/bottom指定が残ると left が効かない
        box.style.setProperty('bottom','auto','important');
      }
      setPos(el,0,0);   // ズレを0に。上で枠を同じ量動かしてあるので見た目は変わらない
      done.push({kind:f.kind, x:Math.round(tx), y:Math.round(ty)});
    }
    return done.length?done:null;
  }
  // fx=横倍率, fy=縦倍率（1なら変えない）。横だけ長く/縦だけ長く/等倍を1関数で。
  function scaleBy(el,fx,fy){
    _cebt(el);
    var sx=(+el.getAttribute('data-cesx')||1)*fx, sy=(+el.getAttribute('data-cesy')||1)*fy;
    if(sx<0.2)sx=0.2; if(sx>5)sx=5; if(sy<0.2)sy=0.2; if(sy>5)sy=5;
    el.setAttribute('data-cesx',sx); el.setAttribute('data-cesy',sy); applyTf(el);
    growClipFrame(el,fx,fy);   // 中/外に「切り取り枠」があると見た目が変わらないので枠も一緒に広げる
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
    growClipFrame(el,fx,fy);
    markDirty();
  }
  // ★「サイズを何度変えても戻る（見た目が変わらない）」の正体＝間に "固定サイズ＋overflow:hidden の枠" が
  //   あり、そこで切り取られているため（実例：DIV.daily-image 646×932固定。画像を1.7倍にしても見た目は不変）。
  //   枠のサイズがインラインpx（＝このツールで付けた枠）なら、同じ倍率で枠も広げる＝見た目が実際に変わる。
  //   枠がページ側CSSで決まっている場合は触らない（切り取りが元デザインの意図なので）。
  //   選んだ要素の外側（親4段）と内側（子孫）の両方を見る＝どちらを選んでいても効く。
  function growClipFrame(el,fx,fy){
    if(!el||(fx===1&&fy===1)) return null;
    function clipSized(n){
      if(!n||n.nodeType!==1) return false;
      var cs=getComputedStyle(n);
      if(cs.overflow!=='hidden'&&cs.overflowX!=='hidden'&&cs.overflowY!=='hidden') return false;
      return (parseFloat(n.style.width)||0)>0 || (parseFloat(n.style.height)||0)>0 || (parseFloat(n.style.minHeight)||0)>0;
    }
    function grow(n){
      var pw=parseFloat(n.style.width)||0, ph=parseFloat(n.style.height)||0, pmh=parseFloat(n.style.minHeight)||0;
      if(pw>0) n.style.setProperty('width',Math.round(pw*fx)+'px','important');
      if(ph>0) n.style.setProperty('height',Math.round(ph*fy)+'px','important');
      if(pmh>0) n.style.setProperty('min-height',Math.round(pmh*fy)+'px','important');
    }
    var hit=null;
    var n=el, hops=0;
    while(n&&n.parentElement&&hops<4){ n=n.parentElement; hops++; if(clipSized(n)){ grow(n); hit=n; break; } }
    if(el.querySelectorAll){       // 内側の枠（選んだのが外側の飾りwrapperだった場合）
      [].slice.call(el.querySelectorAll('*')).slice(0,60).forEach(function(c){
        if(hit===c) return;
        if(clipSized(c)){ grow(c); hit=hit||c; }
      });
    }
    return hit;
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
  // ===== Excel風の伸縮ハンドル（選択中の要素の右端・下端・右下角に■が出る・AIなし） =====
  // ドラッグで adjustWidth/adjustMinH と同じ「歪まない方式」(width/min-height)を連続的に当てる。
  // 位置追従はrAFループ（スクロール・要素移動・アニメ中でも正確に付いてくる＝この環境の定石）。
  var _hdls=null, _hdlRaf=0, _hdlDrag=false;  // _hdlDrag=伸縮ドラッグ直後のclickで選択が閉じるのを防ぐ
  // 伸縮の前に「器の列割り」と「隣の要素のサイズ」を今の実寸で凍結する。
  // grid/flexの中の要素を広げると隣の列まで一緒に動いてしまうため（例：左の画像を広げると右の文章も広がる）、
  // 器のgrid-template-columnsと兄弟の幅を実測ピクセルで固定してから対象だけを伸縮する。
  function _freezeSiblings(x){
    var pa=x.parentElement; if(!pa || pa===document.body) return;
    var cs=getComputedStyle(pa);
    if(cs.display.indexOf('grid')>=0){
      if(!pa.getAttribute('data-cegfz')){
        pa.style.setProperty('grid-template-columns', cs.gridTemplateColumns, 'important');  // 実寸px列に固定
        pa.setAttribute('data-cegfz','1');
      }
      x.style.setProperty('justify-self','start','important');  // 広げた分は自分の枠からはみ出す（隣を押さない）
    } else if(cs.display.indexOf('flex')>=0){
      [].slice.call(pa.children).forEach(function(sib){
        if(sib===x || sib.nodeType!==1 || sib.getAttribute('data-cesfz')) return;
        var r=sib.getBoundingClientRect(); if(!r.width) return;
        sib.style.setProperty('flex','0 0 auto','important');
        sib.style.setProperty('width', Math.round(r.width)+'px','important');
        sib.setAttribute('data-cesfz','1');
      });
      x.style.setProperty('flex','0 0 auto','important');  // 自分のwidth指定がflex計算に負けないように
    }
  }
  // 📐 サイズそろえ用：「見えている箱」を返す。
  // 画像は枠(figure/div)に overflow:hidden で切り取られていることが多く、その場合は
  // 中の<img>をいくら伸縮しても見た目のサイズが1pxも変わらない（枠が見た目を決めている）。
  // ＝切り取っている枠があればそれを返す。切り取っていなければ画像自身が見た目そのもの。
  // 🖼 スライドショーの中を掴んだら「箱」を伸縮対象にする（2026-07-30・ユーザー要望）。
  //   重ねた画像は箱いっぱい(100%)なので、箱を変えれば**3枚まとめて**同じ大きさに変わる。
  //   1枚ずつ変えたいという要望が出たら、その時に選べるようにする（今は「3つ一緒でいい」）。
  function slBox(x){
    if(!x||!x.closest) return x;
    var w=x.closest('[data-slshow]');
    return w||x;
  }
  // 伸縮の前に、箱の今の大きさをpxで固定し、中の画像を全部「箱いっぱい」に敷き直す。
  // ★これをやらないと：1枚目だけ自分の幅を持ったまま残り、箱を縮めても1枚目が付いてこない
  //   （＝1枚目と2枚目以降で大きさが違う、という見え方になる）。
  function slFitAll(w){
    if(!w||!w.getAttribute||w.getAttribute('data-slshow')==null) return false;
    var r=w.getBoundingClientRect();
    if(r.width>4&&r.height>4){
      w.style.setProperty('width',Math.round(r.width)+'px','important');
      w.style.setProperty('height',Math.round(r.height)+'px','important');
      w.style.setProperty('min-height','0','important');
      w.style.setProperty('box-sizing','border-box','important');
    }
    [].slice.call(w.querySelectorAll('img')).forEach(function(im){
      im.style.setProperty('position','absolute','important');
      im.style.setProperty('inset','0','important');
      im.style.setProperty('width','100%','important');
      im.style.setProperty('height','100%','important');
      im.style.setProperty('object-fit','cover','important');
      im.style.removeProperty('margin');
    });
    return true;
  }
  function szBox(x){
    if(!x||x.tagName!=='IMG') return x;
    var p=x.parentElement; if(!p||p===document.body) return x;
    var cs; try{ cs=getComputedStyle(p); }catch(_){ return x; }
    var ov=cs.overflow+' '+cs.overflowX+' '+cs.overflowY;
    if(ov.indexOf('hidden')<0&&ov.indexOf('clip')<0) return x;
    var pr=p.getBoundingClientRect(), xr=x.getBoundingClientRect();
    if(!(pr.width>0&&pr.height>0)) return x;
    // 画像が枠と同じ大きさ以上＝枠いっぱいに敷かれている（＝枠が見た目）
    return (xr.width>=pr.width-2&&xr.height>=pr.height-2)? p : x;
  }
  // 📐 枠(figure/div)の中の「主役の画像」。枠だけ広げても中の画像が付いてこない作りが多いので、
  // 枠をそろえる時はこの画像も枠いっぱいに敷き直す。★右クリックは画像より枠を選ぶ仕様なのでほぼ毎回通る道。
  function szInnerImg(box){
    if(!box||box.tagName==='IMG'||!box.querySelectorAll) return null;
    var im=box.querySelectorAll('img'); if(im.length!==1) return null;
    var br=box.getBoundingClientRect(), ir=im[0].getBoundingClientRect();
    if(!(br.width>0&&br.height>0&&ir.width>0)) return null;
    return (ir.width>=br.width*0.6&&ir.height>=br.height*0.6)? im[0] : null;  // 枠の大半を占める＝画像枠とみなす
  }
  function showHandles(el){
    hideHandles();
    if(!el || el===document.body) return;
    // Excelと同じ8方向。左(w)・上(n)側は「引っ張った方向に伸びる」よう、幅/高さと一緒に位置(setPos)も補正する
    var defs=[
      {k:'e',  cur:'ew-resize',   t:'→ 右へ伸縮'},
      {k:'w',  cur:'ew-resize',   t:'← 左へ伸縮'},
      {k:'s',  cur:'ns-resize',   t:'↓ 下へ伸縮'},
      {k:'n',  cur:'ns-resize',   t:'↑ 上へ伸縮'},
      {k:'se', cur:'nwse-resize', t:'⤡ 右下へ伸縮'},
      {k:'nw', cur:'nwse-resize', t:'⤡ 左上へ伸縮'},
      {k:'ne', cur:'nesw-resize', t:'⤢ 右上へ伸縮'},
      {k:'sw', cur:'nesw-resize', t:'⤢ 左下へ伸縮'}
    ];
    // ★細い図形（線など）では8個の■が重なって、狙って掴めない（2026-07-29・ユーザー報告）。
    //   高さ4pxの線だと上/下/四隅がぜんぶ4px以内に密集し、横に伸ばしたいのに角を掴んで
    //   高さまで変わってしまう。細い時は角を出さず、上下の■は外側へ離して置く。
    var _r0=el.getBoundingClientRect();
    var _thinH=(el.offsetHeight||_r0.height)<24, _thinW=(el.offsetWidth||_r0.width)<24;
    if(_thinH||_thinW) defs=defs.filter(function(d){ return d.k.length===1; });
    // 複数選択なら選択した全員に■を出す（Excelと同じ＝どれが選択中か一目で分かる）
    // ★スライドショーの中を選んだ時は箱に■を出す＝箱を伸縮すれば3枚まとめて変わる
    var targets=(selEls.length?selEls:[el]).map(slBox);
    _hdls={list:[]};
    targets.forEach(function(tgt){
    defs.forEach(function(d){
      var h=document.createElement('div');
      h.className='__ce_hdl'; h.title=d.t+'（ドラッグ・💾保存で確定）';
      h.setAttribute('style','position:fixed;width:12px;height:12px;background:#0b6bcb;border:2px solid #fff;border-radius:3px;box-shadow:0 1px 4px rgba(0,0,0,.35);z-index:2147483646;cursor:'+d.cur+';user-select:none');
      h.addEventListener('mousedown',function(ev){
        // 要素の移動ドラッグ(_dDown)や文字選択にイベントを渡さない＝掴んだら伸縮だけ
        ev.preventDefault(); ev.stopPropagation(); _hdlDrag=true;
        var sx=ev.clientX, sy=ev.clientY;
        var _rz=(selEls.length?selEls:[tgt]).map(slBox);        // スライドショーは箱を伸縮する
        _rz.forEach(slFitAll);                                  // 中の画像を全部「箱いっぱい」に敷き直す
        _rz.forEach(_freezeSiblings);                           // 隣の列・兄弟が動かないよう先に凍結
        // 複数選択中は選択した全部に同じ量を掛ける（各要素の元サイズ・元位置を最初に控える）
        var bases=_rz.map(function(x){
          var rr=x.getBoundingClientRect();
          // ★大きさは offsetWidth/Height で測る（rectは回転や出現アニメのtransformを含む）。
          //   ／斜め線(rotate:-28deg)はrectだと高さが実際の3pxではなく百数十pxに見え、
          //   その値で高さを固定すると「線が太い板になる」（2026-07-29）。
          var ow=x.offsetWidth||rr.width, oh=x.offsetHeight||rr.height;
          // 細い図形（線など）は1pxまで縮められるようにする。★下限20px固定が
          //   「線の高さを縮められない／横に伸ばすと高さが出る」の正体だった（ユーザー報告）。
          var thin=(x.classList&&x.classList.contains('ce_shape'))||oh<20||ow<20;
          return {el:x, w:ow, h:oh, minw:(thin?2:40), minh:(thin?1:20),
                  tx:+x.getAttribute('data-cetx')||0, ty:+x.getAttribute('data-cety')||0};
        });
        // 横方向だけのドラッグ（e/w）では高さを今の実寸で固定する。
        // 中の画像は幅に合わせて縦横比で背が伸びる＝「全体がひろがって」レイアウトを押すため、
        // 高さを止めて「見える範囲が横に広がる」動き（Excelの図形に近い感覚）にする。
        if(d.k==='e'||d.k==='w'){
          bases.forEach(function(bs){
            bs.el.style.setProperty('height', Math.round(bs.h)+'px','important');
            if(bs.el.tagName==='IMG'){
              bs.el.style.setProperty('object-fit','cover','important');
            } else if(bs.el.querySelector('img')){
              bs.el.style.setProperty('overflow','hidden','important');  // 中の画像がはみ出す分は切り取り
            }
          });
        }
        function mv(e2){
          var dx=e2.clientX-sx, dy=e2.clientY-sy;
          bases.forEach(function(bs){
            var shx=0, shy=0;
            if(d.k.indexOf('e')>=0){
              var w=Math.max(bs.minw, bs.w+dx);
              bs.el.style.setProperty('width',Math.round(w)+'px','important');
              bs.el.style.setProperty('max-width','none','important');
            }
            if(d.k.indexOf('w')>=0){
              var w2=Math.max(bs.minw, bs.w-dx);
              bs.el.style.setProperty('width',Math.round(w2)+'px','important');
              bs.el.style.setProperty('max-width','none','important');
              shx=bs.w-w2;  // 左端がカーソルに付いてくるよう、増えた分だけ左へずらす
            }
            // ★高さは min-height ではなく height で当てる（2026-07-29）。
            //   min-height は「最小の高さ」なので、中身の高さより小さくできない＝
            //   伸ばせるのに縮められない（「下から上に行けない」の正体・ユーザー報告）。
            //   元CSSの min-height に負けないよう 0 を明示し、padding込みで測れるよう border-box にする。
            function _setH(elx, px){
              elx.style.setProperty('height', Math.round(px)+'px','important');
              elx.style.setProperty('min-height','0','important');
              elx.style.setProperty('box-sizing','border-box','important');
            }
            if(d.k.indexOf('s')>=0){ _setH(bs.el, Math.max(bs.minh, bs.h+dy)); }
            if(d.k.indexOf('n')>=0){
              var h2=Math.max(bs.minh, bs.h-dy);
              _setH(bs.el, h2);
              shy=bs.h-h2;  // 上端がカーソルに付いてくるよう、増えた分だけ上へずらす
            }
            if(shx||shy) setPos(bs.el, bs.tx+shx, bs.ty+shy);
          });
        }
        function up(){
          document.removeEventListener('mousemove',mv,true); document.removeEventListener('mouseup',up,true);
          setTimeout(function(){ _hdlDrag=false; }, 80);  // mouseup直後のclickをやり過ごしてから解除
          markDirty();
          if(msg) msg.textContent='サイズを変えました（⟲リセットで元に戻せる・💾保存で確定）';
        }
        document.addEventListener('mousemove',mv,true); document.addEventListener('mouseup',up,true);
      },true);
      document.body.appendChild(h);
      _hdls.list.push({d:d, node:h, el:tgt});
    });
    });
    (function loop(){
      if(!_hdls) return;
      _hdls.list.forEach(function(x){
        var n=x.node, k=x.d.k, r=x.el.getBoundingClientRect();
        // 細い図形は上下(左右)の■を外側へ逃がす＝真ん中の■と重ならず、狙って掴める
        var ox=(r.width<24)?11:0, oy=(r.height<24)?11:0;
        var lx=(k.indexOf('w')>=0)?(r.left-6-ox):((k.indexOf('e')>=0)?(r.right-6+ox):(r.left+r.width/2-6));
        var tp=(k.indexOf('n')>=0)?(r.top-6-oy):((k.indexOf('s')>=0)?(r.bottom-6+oy):(r.top+r.height/2-6));
        n.style.left=lx+'px'; n.style.top=tp+'px';
      });
      _hdlRaf=requestAnimationFrame(loop);
    })();
  }
  function hideHandles(){
    if(_hdlRaf){ cancelAnimationFrame(_hdlRaf); _hdlRaf=0; }
    if(_hdls){ _hdls.list.forEach(function(x){ x.node.remove(); }); _hdls=null; }
  }
  // ===== 動きプレビュー（RAFで毎フレーム手動描画＝この環境で確実）＋ 無料の焼き込み =====
  var curAnim=null, curP={};  // いまプレビュー中のアニメkと、その調整値
  // アニメごとに「前回いじった調整値」を記憶（再読込しても覚える＝次からのデフォルトにする）
  var _fxLast={}; try{ _fxLast=JSON.parse(localStorage.getItem('__ce_fxlast')||'{}')||{}; }catch(_){ _fxLast={}; }
  function _fxSaveLast(){ try{ localStorage.setItem('__ce_fxlast', JSON.stringify(_fxLast)); }catch(_){} }
  function fxDef(k){ for(var i=0;i<FX.length;i++){ if(FX[i].k===k) return FX[i]; } return null; }
  function fxParam(a,key){ if(curP[key]!=null) return curP[key]; for(var i=0;i<a.sl.length;i++){ if(a.sl[i].k===key) return a.sl[i].def; } return 0; }
  // プレビューで当てた一時styleを消し、確定状態（位置など）に戻す
  // プレビューが書き換えるインラインstyleのプロパティ一覧（開始時に控える・終了時に戻す）
  var _PREV_PROPS=['opacity','filter','clip-path','text-shadow','animation','transition','transform','translate','rotate','scale','transform-origin'];
  // プレビュー開始時：要素の「元のインラインstyle」を1回だけ控える（連続プレビューでも最初の状態を保つ）
  function snapPreviewStyle(el){
    if(!el||el.__cePrevSt) return;
    var o={};
    _PREV_PROPS.forEach(function(p){ var v=el.style.getPropertyValue(p); o[p]=v?{v:v,pri:el.style.getPropertyPriority(p)}:null; });
    el.__cePrevSt=o;
  }
  // ★2026-07-12 復元方式に変更：旧方式（無条件でopacity等をremoveProperty）は、昔の保存で
  //   焼き込まれた保険の opacity:1!important まで消してしまい、.reveal系の要素が右クリック→
  //   閉じるだけで隠れ状態へ落ちる＝「下に動いて消える」事故の原因だった。
  //   プレビューをしていない要素には何もしない。した要素は控えた元の値へ戻す。
  function clearPreviewStyle(el){
    if(!el) return;
    if(el.__cePrevSt){
      var snap=el.__cePrevSt; el.__cePrevSt=null;
      _PREV_PROPS.forEach(function(p){ var s=snap[p]; if(s){ el.style.setProperty(p, s.v, s.pri||''); } else { el.style.removeProperty(p); } });
    }
    // 位置・拡大・回転・退避のどれかが編集されていたら、その確定変形を当て直す（拡大だけでも消えないように）
    var edited=['data-cetx','data-cety','data-cesx','data-cesy','data-cero','data-cebt'].some(function(a){ return el.getAttribute(a)!=null; });
    if(edited){ applyTf(el); }
  }
  // 焼き込み前の完全掃除（旧clearPreviewStyle相当）：保険が焼き込んだ opacity:1!important 等の残骸も
  // 消してから付ける＝残すと隠れ状態(.fxa_pre)にならず「アニメを付けたのに動かない」になる。
  function purgeInlineFx(el){
    if(!el) return;
    el.__cePrevSt=null;
    // ★飾り（🌸グラデ/⭕リング/▢縁取り線）の ぼかし は残す：消すとアニメを付けた瞬間に見た目が変わる
    var _isDeco=el.classList&&(el.classList.contains('ce_bgdeco')||el.classList.contains('ce_ringdeco')||el.classList.contains('ce_outlinedeco'));
    var _keepFil=_isDeco?el.style.getPropertyValue('filter'):'';
    ['opacity','filter','clip-path','text-shadow','animation','transition'].forEach(function(p){ el.style.removeProperty(p); });
    if(_keepFil) el.style.setProperty('filter', _keepFil);
    var edited=['data-cetx','data-cety','data-cesx','data-cesy','data-cero','data-cebt'].some(function(a){ return el.getAttribute(a)!=null; });
    if(edited){ applyTf(el); } else { ['transform','translate','rotate','scale'].forEach(function(p){ el.style.removeProperty(p); }); }
  }
  function stopAnim(el){
    if(el&&el.__fxWipeOv){ el.__fxWipeOv.remove(); el.__fxWipeOv=null; }  // ワイププレビューの帯が残らないように
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
        var ch=s[i], cc=s.charCodeAt(i);
        // 絵文字（サロゲートペア）は2コード=1文字。真ん中で割ると表示が壊れる上、
        // 片割れがDOMに残ると💾保存がUTF-8エラー（surrogates not allowed）で必ず失敗する
        if(cc>=0xD800&&cc<=0xDBFF&&i+1<s.length){ ch+=s[i+1]; i++; }
        if(i+1<s.length&&s.charCodeAt(i+1)===0xFE0F){ ch+=s[i+1]; i++; }  // 絵文字の飾り指定(VS16)も同じ箱に
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
    // 行マスク(fxa_ln)の包みを外し、行の切れ目には<br>を復元（元のマークアップに戻す）
    var lns=[].slice.call(el.querySelectorAll('.fxa_ln'));
    lns.forEach(function(o,i){
      var inr=o.querySelector('.fxa_lni')||o;
      if(i>0) o.parentNode.insertBefore(document.createElement('br'), o);
      while(inr.firstChild) o.parentNode.insertBefore(inr.firstChild, o);
      o.remove();
    });
    var sp;
    while(sp=el.querySelector('.fxa_ch')){ sp.replaceWith(document.createTextNode(sp.textContent||'')); }
    try{ el.normalize(); }catch(_){}  // 隣り合うテキストを1つに結合＝元の文字列に戻す
  }
  // 行マスク用：<br>で行に分け、各行を「窓(.fxa_ln・overflow:hidden)＋中身(.fxa_lni)」で包む。
  // brが無い1行見出しは全体が1行扱い。戻すのはfxUnsplit（brも復元）。
  function splitLines(el){
    if(!el.textContent||!el.textContent.trim()) return [];
    var groups=[[]];
    [].slice.call(el.childNodes).forEach(function(n){
      if(n.nodeType===1&&n.tagName==='BR'){ groups.push([]); n.remove(); }
      else groups[groups.length-1].push(n);
    });
    var out=[];
    groups.forEach(function(g,i){
      if(!g.length) return;
      var o=document.createElement('span'); o.className='fxa_ln'; o.style.setProperty('--i', out.length);
      var inr=document.createElement('span'); inr.className='fxa_lni';
      el.insertBefore(o, g[0]);
      g.forEach(function(n){ inr.appendChild(n); });
      o.appendChild(inr); out.push(o);
    });
    return out;
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
  // ===== プレビュー用イージング＝保存後のCSSと同じカーブ =====
  // ★プレビュー(rAF手動描画)と本番(CSS transition/animation)は実装が別なので、
  //   カーブが違うと「保存したら突然速い/形が違う」になる（実際に苦情が出た）。
  //   本番CSSで使っている cubic-bezier をそのまま数値計算して完全一致させる。
  function cubicBezier(x1,y1,x2,y2){
    function cal(t,a1,a2){ return ((1-3*a2+3*a1)*t + (3*a2-6*a1))*t*t + 3*a1*t; }
    function der(t,a1,a2){ return 3*(1-3*a2+3*a1)*t*t + 2*(3*a2-6*a1)*t + 3*a1; }
    return function(x){
      if(x<=0) return 0; if(x>=1) return 1;
      var t=x;
      for(var i=0;i<5;i++){ var e=cal(t,x1,x2)-x; var d=der(t,x1,x2); if(Math.abs(e)<1e-4||!d) break; t-=e/d; }
      return cal(t,y1,y2);
    };
  }
  var EASE=cubicBezier(.25,.1,.25,1);          // CSSの'ease'＝出現系transitionと同じ
  var EASE_IO=cubicBezier(.42,0,.58,1);        // CSSの'ease-in-out'＝ループkeyframesと同じ
  var EASE_SPRING=cubicBezier(.34,1.56,.64,1); // 一文字ずつ(fxa_cpre)のばねカーブと同じ
  // 文字系プレビュー（stagger/typewriter/wave）
  function playChar(el,a){
    if(el.__fxHTML==null) el.__fxHTML=el.innerHTML;
    fxUnsplit(el);  // 既に割れていても一旦プレーンに戻してから割り直す
    var spans=splitChars(el);
    if(!spans.length){ el.innerHTML=el.__fxHTML; el.__fxHTML=null; if(msg)msg.textContent='⚠ ここには文字が無いので文字アニメは使えません。画像には「ふわっと出現」「ズームイン」「ぼやけて出現」などを選んでください'; return; }
    var stag=fxParam(a,'stag')||45, dur=fxParam(a,'dur')||(a.loop?1600:340), dist=fxParam(a,'dist')||16, amp=fxParam(a,'amp')||10, start=null;
    // 跳ね具合はスライダーで変わるので、プレビューのカーブも毎回作り直す（本番CSSと同じ形にそろえる）
    var SPRING=cubicBezier(.34, _fxBnc(a), .64, 1);
    function frame(ts){
      if(start===null)start=ts; var tt=ts-start;
      for(var i=0;i<spans.length;i++){ var sp=spans[i];
        if(a.loop){
          // 本番CSS(fxa_wave)＝上にだけ持ち上がる山型(ease-in-out)・開始は1文字90msずつ遅れる
          var lt0=tt-i*90, s=0;
          if(lt0>0){ var pp=(lt0%dur)/dur; s=pp<.5?EASE_IO(pp/.5):EASE_IO((1-pp)/.5); }
          sp.style.transform='translateY('+(-amp*s)+'px)';
        }
        else if(a.type){
          // 本番CSS(fxa_tw)＝1文字0.18秒(ease)でフェード＋下10px→定位置＋scale0.9→1
          var lt1=tt-i*stag, q1=lt1<=0?0:EASE(Math.min(1,lt1/180));
          sp.style.opacity=q1; sp.style.transform='translateY('+(10*(1-q1))+'px) scale('+(0.9+0.1*q1)+')';
        }
        else {
          // 本番CSS(fxa_cpre)＝ばねカーブ(少し行き過ぎて戻る)。opacityだけ0〜1に留める
          var lt=tt-i*stag, q=lt<=0?0:SPRING(Math.min(1,lt/dur));
          sp.style.opacity=Math.max(0,Math.min(1,q)); sp.style.transform='translateY('+(dist*(1-q))+'px)';
        }
      }
      var done=a.loop?false:(tt>spans.length*stag+(a.type?200:dur+20));
      if(!done){ el.__ceRAF=requestAnimationFrame(frame); }
      else { el.__ceRAF=null; el.innerHTML=el.__fxHTML; el.__fxHTML=null; }
    }
    el.__ceRAF=requestAnimationFrame(frame);
  }
  // 行マスクのプレビュー：一時的に行分割して、各行の中身を下からせり上げる（終わったら元HTMLへ戻す）
  function playLines(el,a){
    if(el.__fxHTML==null) el.__fxHTML=el.innerHTML;
    fxUnsplit(el);
    var lns=splitLines(el);
    if(!lns.length){ el.innerHTML=el.__fxHTML; el.__fxHTML=null; if(msg)msg.textContent='⚠ ここには文字が無いので行マスクは使えません'; return; }
    var dur=fxParam(a,'dur')||700, stag=fxParam(a,'stag')||130, start=null;
    var EOUT=cubicBezier(.22,1,.36,1);  // 本番CSSと同じカーブ
    function frame(ts){
      if(start===null)start=ts; var tt=ts-start, done=true;
      lns.forEach(function(o,i){
        var q=Math.max(0,Math.min(1,(tt-i*stag)/dur)); if(q<1) done=false;
        var inr=o.querySelector('.fxa_lni'); if(inr) inr.style.transform='translateY('+((1-EOUT(q))*112)+'%)';
      });
      if(!done){ el.__ceRAF=requestAnimationFrame(frame); }
      else { el.__ceRAF=null; el.innerHTML=el.__fxHTML; el.__fxHTML=null; }
    }
    el.__ceRAF=requestAnimationFrame(frame);
  }
  // カーテンワイプのプレビュー：テーマ色の帯（画面固定の重ね）を走らせつつ、後ろからclip-pathで開く
  function playWipe(el,a){
    var dur=fxParam(a,'dur')||800, start=null;
    var r=el.getBoundingClientRect();
    var col=(getComputedStyle(document.documentElement).getPropertyValue('--main')||'').trim()||'#334155';
    var wrap=document.createElement('div');
    wrap.setAttribute('style','position:fixed;left:'+r.left+'px;top:'+r.top+'px;width:'+r.width+'px;height:'+r.height+'px;overflow:hidden;z-index:2147482000;pointer-events:none');
    var band=document.createElement('div');
    band.setAttribute('style','position:absolute;inset:0;background:'+col+';transform:translateX(-101%)');
    wrap.appendChild(band); document.body.appendChild(wrap);
    el.__fxWipeOv=wrap;
    var E=cubicBezier(.65,0,.35,1);  // 本番keyframesと同じカーブ
    function frame(ts){
      if(start===null)start=ts; var p=Math.min(1,(ts-start)/dur);
      var bx=(p<.45? (-101+101*E(p/.45)) : (101*E((p-.45)/.55)));  // 帯：左外→中央→右外
      band.style.transform='translateX('+bx+'%)';
      var rv=Math.max(0,Math.min(1,(p-.1)/.6));                    // 中身：帯の少し後ろから開く
      el.style.setProperty('clip-path','inset(0 '+((1-E(rv))*100)+'% 0 0)','important');
      if(p<1){ el.__ceRAF=requestAnimationFrame(frame); }
      else { el.__ceRAF=null; wrap.remove(); el.__fxWipeOv=null; clearPreviewStyle(el); }
    }
    el.__ceRAF=requestAnimationFrame(frame);
  }
  // カウントアップのプレビュー：0→目標値（カンマ・小数の書式は元のまま）。終わったら元HTMLへ戻す
  function playCount(el,a){
    var m=(el.textContent||'').match(/[-+]?[\\d,]+(?:\\.\\d+)?/);
    if(!m){ if(msg)msg.textContent='⚠ ここには数字が無いのでカウントアップは使えません（例：120件・98%）'; return; }
    if(el.__fxHTML==null) el.__fxHTML=el.innerHTML;
    var raw=m[0], tgt=parseFloat(raw.replace(/,/g,'')), dec=(raw.split('.')[1]||'').length, com=raw.indexOf(',')>=0;
    var txt=el.textContent, pre=txt.slice(0,m.index), suf=txt.slice(m.index+raw.length);
    var dur=fxParam(a,'dur')||1200, start=null;
    function fmt(v){ var s=v.toFixed(dec); if(com){ var pp=s.split('.'); pp[0]=pp[0].replace(/\\B(?=(\\d{3})+(?!\\d))/g,','); s=pp.join('.'); } return s; }
    function frame(ts){
      if(start===null)start=ts; var p=Math.min(1,(ts-start)/dur), e=1-Math.pow(1-p,3);
      el.textContent=pre+fmt(tgt*e)+suf;
      if(p<1){ el.__ceRAF=requestAnimationFrame(frame); }
      else { el.__ceRAF=null; el.innerHTML=el.__fxHTML; el.__fxHTML=null; }
    }
    el.__ceRAF=requestAnimationFrame(frame);
  }
  function playAnim(el,k){
    if(!el){ if(msg)msg.textContent='⚠ 要素が選ばれていません（もう一度右クリックで選んでください）'; return; }
    var a=fxDef(k); if(!a){ if(msg)msg.textContent='⚠ 未対応の動き：'+k; return; }
    stopAnim(el);
    snapPreviewStyle(el);  // 元のインラインstyleを控える（終了・中断時にclearPreviewStyleが復元する）
    el.style.setProperty('animation','none','important');  // プレビュー中は要素自身のCSSアニメを止める（RAFのtransformが上書きされないように）
    // ★ページCSSのtransition（.reveal系の0.9s等）や焼き込み済みfxa_preのtransitionが生きていると、
    //   rAFの毎フレーム書き込みが1テンポ遅れて「試しても動かない」ように見える → プレビュー中だけ無効化
    el.style.setProperty('transition','none','important');
    var base=el.getAttribute('data-cebt')||'';  // 元の変形(回転など)は保つ。移動はtranslate個別プロパティ側に乗るのでここには含めない
    if(msg) msg.textContent='▶ 再生「'+a.b+'」（スライダーで調整→「付ける」で確定）';
    if(a.g==='char'){ playChar(el,a); return; }
    if(a.g==='lines'){ playLines(el,a); return; }
    if(a.g==='cnt'){ playCount(el,a); return; }
    if(a.dir==='wp'){ playWipe(el,a); return; }
    if(a.dir==='fl'){ el.__fxPrevTO=el.style.getPropertyValue('transform-origin')||''; el.style.setProperty('transform-origin','left center','important'); }  // 📖は左端が軸
    var dur=fxParam(a,'dur')||800, start=null;  // a.dは説明文なので使わない（速さスライダー無しは800msに）
    function frame(ts){
      if(start===null) start=ts;
      var p=(ts-start)/dur, o=1, tf='';
      if(a.g==='loop'){
        // 本番CSSのkeyframes(0%→50%→100%)と同じ「ease-in-outの山型」で往復（直線の三角波だとカクついて別物に見える）
        var pp=p%1, tri=pp<.5?EASE_IO(pp/.5):EASE_IO((1-pp)/.5);
        if(a.dir==='ps'){ tf='scale('+(1+(fxParam(a,'amp')/100)*tri)+')'; }
        else if(a.dir==='fy'){ tf='translateY('+(-fxParam(a,'amp')*tri)+'px)'; }
        else if(a.dir==='by'){
          // 本番CSS(fxa_bounce)＝0/30/60/80/100%の区分ごとに'ease'が掛かる
          var bp=pp<.3?EASE(pp/.3):(pp<.6?1-EASE((pp-.3)/.3):(pp<.8?.4*EASE((pp-.6)/.2):.4*(1-EASE((pp-.8)/.2))));
          tf='translateY('+(-fxParam(a,'amp')*Math.max(0,bp))+'px)';
        }
        else if(a.glow){ var g=Math.round(4+12*tri); el.style.setProperty('text-shadow','0 0 '+g+'px currentColor'+(tri>.35?(',0 0 '+Math.round(g*1.9)+'px currentColor'):''),'important'); el.style.setProperty('filter','brightness('+(1+.16*tri)+')','important'); }
      } else {
        var q=EASE(Math.min(1,p));  // 本番の transition ... ease と同じカーブ（最初速く・最後ゆっくり）
        o=q;
        if(a.dir==='y'){ tf='translateY('+(fxParam(a,'dist')*(1-q))+'px)'; }
        else if(a.dir==='yd'){ tf='translateY('+(-fxParam(a,'dist')*(1-q))+'px)'; }
        else if(a.dir==='xl'){ tf='translateX('+(-fxParam(a,'dist')*(1-q))+'px)'; }
        else if(a.dir==='xr'){ tf='translateX('+(fxParam(a,'dist')*(1-q))+'px)'; }
        else if(a.dir==='s'){ var sc=fxParam(a,'scale')/100; tf='scale('+(sc+(1-sc)*q)+')'; }
        else if(a.dir==='bl'){ el.style.setProperty('filter','blur('+(fxParam(a,'blur')*(1-q))+'px)','important'); }
        else if(a.dir==='ry'){ tf='perspective(800px) rotateY('+(fxParam(a,'deg')*(1-q))+'deg)'; }
        else if(a.dir==='fl'){ tf='perspective(1200px) rotateY('+(fxParam(a,'deg')*(1-q))+'deg)'; }  // 📖ページめくり（軸は左端＝上で設定済み）
        else if(a.dir==='cl'){ o=1; el.style.setProperty('clip-path','inset(0 '+((1-q)*100).toFixed(2)+'% 0 0)','important'); }  // カーテン開き＝フェードせず左から開く
        else if(a.dir==='cc'){ o=1; var _ci=((1-q)*50).toFixed(2); el.style.setProperty('clip-path','inset(0 '+_ci+'% 0 '+_ci+'%)','important'); }  // 真ん中から左右へ開く（舞台の幕）
        else if(a.dir==='clip'){ tf='translateY('+(fxParam(a,'dist')*(1-q))+'px)'; }  // せり上がり＝下からスッと上へ＋フェード（clip-pathは使わない＝半分で止まらない）
      }
      if(!a.glow){ el.style.setProperty('opacity',o,'important'); }
      if(tf) el.style.setProperty('transform',tf+' '+base,'important');
      if(a.g==='loop' || p<1){ el.__ceRAF=requestAnimationFrame(frame); }
      else {
        el.__ceRAF=null; clearPreviewStyle(el);
        // 📖用に変えた回転軸(transform-origin)を元へ戻す（他のアニメ・ドラッグに影響させない）
        if(a.dir==='fl'){ if(el.__fxPrevTO){ el.style.setProperty('transform-origin', el.__fxPrevTO, 'important'); } else { el.style.removeProperty('transform-origin'); } el.__fxPrevTO=null; }
      }
    }
    el.__ceRAF=requestAnimationFrame(frame);
  }
  // ===== 焼き込み（AIなし・無料）：永続CSS/JSをHTMLへ注入し、要素にクラス＋CSS変数を付ける =====
  // 命名は "fxa" 系（#__ce を含めない）＝保存時の掃除(cleanHtml)で消されず、そのまま残る。
  var FX_CSS='html.fxa-on .fxa_pre{opacity:0;transition:opacity var(--fxa-dur,.8s) ease,transform var(--fxa-dur,.8s) ease,filter var(--fxa-dur,.8s) ease,clip-path var(--fxa-dur,.8s) ease}'
    +'html.fxa-on .fxa_pre.fxa_y{transform:translateY(var(--fxa-dist,28px))}'
    +'html.fxa-on .fxa_pre.fxa_yd{transform:translateY(calc(-1*var(--fxa-dist,36px)))}'
    +'html.fxa-on .fxa_pre.fxa_xl{transform:translateX(calc(-1*var(--fxa-dist,48px)))}'
    +'html.fxa-on .fxa_pre.fxa_xr{transform:translateX(var(--fxa-dist,48px))}'
    +'html.fxa-on .fxa_pre.fxa_s{transform:scale(var(--fxa-scale,.86))}'
    +'html.fxa-on .fxa_pre.fxa_bl{filter:blur(var(--fxa-blur,14px))}'
    +'html.fxa-on .fxa_pre.fxa_ry{transform:perspective(800px) rotateY(var(--fxa-deg,90deg))}'
    +'html.fxa-on .fxa_pre.fxa_clip{transform:translateY(var(--fxa-dist,40px))}'
    // ★地雷（2026-07-25修正）：ここが clip-path:inset(0 0 0 0) だと「再生し終わった状態」でも
    //   要素のボックスちょうどで切り抜き続ける＝文字の上に出る部分（大きい文字の上端・アーチで持ち上げた字・
    //   回転させた見出し）が斜めの直線でスパッと切れる（実例：「楽しみを仕事で見つけよう！」の"事"の上が欠けた）。
    //   終わったら切らない＝none が正しい。カーテン系だけは transition の行き先が必要なので下で別に指定する。
    // ★transform は none ではなく「元々の transform（--fxa-tf0）」に戻す。
    //   none にすると固定ヘッダーの translateX(-50%) 等の中央寄せまで消えて横にビヨーンと飛ぶ（2026-07-25）。
    +'html.fxa-on .fxa_pre.fxa_in{opacity:1!important;transform:var(--fxa-tf0,none)!important;filter:none!important;clip-path:none!important}'
    // カーテン開き/ワイプは inset を animate する演出なので none に出来ない（noneは補間できず一瞬で終わる）。
    //   代わりに終点を少し外側（-25%）にして、文字のはみ出しは切らないようにする。
    +'html.fxa-on .fxa_pre.fxa_wp.fxa_in,html.fxa-on .fxa_pre.fxa_cl.fxa_in,html.fxa-on .fxa_pre.fxa_cc.fxa_in{clip-path:inset(-25% -1px -25% -1px)!important}'
    +'html.fxa-on .fxa_pre.fxa_cpre,html.fxa-on .fxa_pre.fxa_tw{opacity:1;transform:none;transition:none}'
    +'.fxa_ch{display:inline-block}'
    // 跳ね具合は --fxa-bnc（cubic-bezierの行き過ぎ量）で調整する。大きいほど上に飛び跳ねてから戻る
    +'html.fxa-on .fxa_cpre .fxa_ch{opacity:0;transform:translateY(var(--fxa-dist,26px));transition:opacity var(--fxa-dur,.34s) cubic-bezier(.34,var(--fxa-bnc,1.56),.64,1),transform var(--fxa-dur,.34s) cubic-bezier(.34,var(--fxa-bnc,1.56),.64,1)}'
    +'html.fxa-on .fxa_cpre.fxa_in .fxa_ch{opacity:1;transform:none;transition-delay:calc(var(--i,0)*var(--fxa-stag,32ms))}'
    +'html.fxa-on .fxa_tw .fxa_ch{opacity:0;transform:translateY(10px) scale(.9);transition:opacity .18s ease,transform .18s ease}'
    +'html.fxa-on .fxa_tw.fxa_in .fxa_ch{opacity:1;transform:none;transition-delay:calc(var(--i,0)*var(--fxa-stag,60ms))}'
    // にじみ出る：ぼかしを解きながら、ゆっくり浮かび上がる。1文字の時間(--fxa-dur)を文字の間隔
    // (--fxa-stag)より長く取るので隣同士が重なり、タイプライターのようなカタカタ感が出ない。
    +'html.fxa-on .fxa_pre.fxa_sk{opacity:1;transform:none;transition:none}'
    +'html.fxa-on .fxa_sk .fxa_ch{opacity:0;filter:blur(var(--fxa-blur,10px));transform:translateY(6px);'
    +'transition:opacity var(--fxa-dur,1.6s) cubic-bezier(.25,.46,.45,.94),filter var(--fxa-dur,1.6s) cubic-bezier(.25,.46,.45,.94),transform var(--fxa-dur,1.6s) cubic-bezier(.25,.46,.45,.94)}'
    +'html.fxa-on .fxa_sk.fxa_in .fxa_ch{opacity:1;filter:blur(0);transform:none;transition-delay:calc(var(--i,0)*var(--fxa-stag,140ms))}'
    +'@keyframes fxa_pulse{0%,100%{transform:scale(1)}50%{transform:scale(calc(1 + var(--fxa-amp,.06)))}}'
    +'@keyframes fxa_float{0%,100%{transform:translateY(0)}50%{transform:translateY(calc(-1*var(--fxa-amp,12px)))}}'
    +'@keyframes fxa_bounce{0%,100%{transform:translateY(0)}30%{transform:translateY(calc(-1*var(--fxa-amp,18px)))}60%{transform:translateY(0)}80%{transform:translateY(calc(-.4*var(--fxa-amp,18px)))}}'
    +'@keyframes fxa_glow{0%,100%{text-shadow:0 0 4px currentColor;filter:brightness(1)}50%{text-shadow:0 0 16px currentColor,0 0 30px currentColor;filter:brightness(1.16)}}'
    +'@keyframes fxa_wave{0%,100%{transform:translateY(0)}50%{transform:translateY(calc(-1*var(--fxa-amp,10px)))}}'
    // ▼2026-07-11追加：行マスク（.fxa_ln=行の窓・overflow:hiddenで刈る／.fxa_lni=中身が下からせり上がる）
    +'.fxa_ln{display:block;overflow:hidden}'
    +'.fxa_lni{display:block}'
    +'html.fxa-on .fxa_pre.fxa_lines{opacity:1;transform:none;transition:none}'
    +'html.fxa-on .fxa_lines .fxa_lni{transform:translateY(112%);transition:transform var(--fxa-dur,.7s) cubic-bezier(.22,1,.36,1)}'
    +'html.fxa-on .fxa_lines.fxa_in .fxa_lni{transform:none;transition-delay:calc(var(--i,0)*var(--fxa-stag,130ms))}'
    // ▼カーテンワイプ：中身はclip-pathで左→右に開き、::afterのテーマ色帯が一足先に走り抜ける
    +'html.fxa-on .fxa_pre.fxa_wp{opacity:1;position:relative;clip-path:inset(0 100% 0 0);transition:clip-path var(--fxa-dur,.8s) cubic-bezier(.65,0,.35,1) .1s}'
    +'html.fxa-on .fxa_wp.fxa_in::after{content:"";position:absolute;inset:0;background:var(--main,#334155);border-radius:inherit;transform:translateX(-101%);animation:fxa_wipeband var(--fxa-dur,.8s) cubic-bezier(.65,0,.35,1) forwards;pointer-events:none}'
    +'@keyframes fxa_wipeband{0%{transform:translateX(-101%)}45%{transform:translateX(0)}100%{transform:translateX(101%)}}'
    // ▼📖ページめくり：左端を軸に、立てたページ（rotateY）が開いて倒れてくる＋フェード
    +'html.fxa-on .fxa_pre.fxa_fl{transform-origin:left center;transform:perspective(1200px) rotateY(var(--fxa-deg,80deg))}'
    // ▼カーテン開き：色帯なしで、中身がclip-pathでスッと開く（フェードしない＝幕が開く見た目）
    //   cl=左端から右へ／cc=真ん中から左右へ（舞台の幕）。開き切りはどちらも共通の .fxa_in が担当
    +'html.fxa-on .fxa_pre.fxa_cl{opacity:1;clip-path:inset(0 100% 0 0)}'
    +'html.fxa-on .fxa_pre.fxa_cc{opacity:1;clip-path:inset(0 50% 0 50%)}'
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
    // ページ固有CSSに background:none!important があっても、ツールで付けたマーカーを確実に表示する。
    // ★box-decoration-breakは slice（既定）を使う：帯を「1本の長い帯を行でスライスした」扱いにする＝
    //   2行にまたがるマーカーは 1行目を引き終わってから2行目に続く（書き順の流れが出る）。
    //   clone だと各行が独立して同時に伸びる（実際に「一緒に出て流れを感じない」と苦情が出た）。
    +'html body .fxa_hl.fxa_hl{background-image:linear-gradient(transparent var(--hlt0,79%),var(--hlc,#ffe66d) var(--hlt0,79%),var(--hlc,#ffe66d) var(--hlt1,91%),transparent var(--hlt1,91%))!important;background-repeat:no-repeat!important;background-size:calc(var(--hlw,0) * 1%) 100%!important;padding:0 .06em;-webkit-box-decoration-break:slice;box-decoration-break:slice}'
    // 〰 点線下線のアニメ版（2026-07-19）：マーカーと同じ--hlw(0〜100)で左→右にスーッと引かれる。
    //   点線はborderでなくrepeating-gradientで描く＝マーカーの仕組み(sweepHl/--hldur/data-cedelay)を丸ごと共用できる。色は--udc。
    +'html body .fxa_ud.fxa_ud{background-image:repeating-linear-gradient(90deg,var(--udc,#0b6bcb) 0 6px,transparent 6px 11px)!important;background-repeat:no-repeat!important;background-position:left 100%!important;background-size:calc(var(--hlw,0) * 1%) 3px!important;padding-bottom:.15em;-webkit-box-decoration-break:slice;box-decoration-break:slice}'
    // ★★ ページ自前の出現アニメに負けない（2026-07-28・実測で判明した「動きが付かない」の正体）
    //   カンプ側CSSに `.reveal.is-visible{opacity:1!important;transform:none!important}` があり、
    //   ページ自身のIntersectionObserverがスクロール時に is-visible を付け直す。
    //   fxaの隠し状態(.fxa_pre)は!important無しなので必ず負ける＝`.reveal`を持つ要素だけ
    //   「動きを付けたのに一切動かない」になっていた（実例：p.lead.reveal「下記は9時30分…」）。
    //   ここだけ!importantで取り返す。★全部 html.fxa-on 配下＝JSが動かない環境では1行も効かないので
    //   「JSが無ければ中身が全部見える」という保険は壊れない。
    +'html.fxa-on .fxa_pre:not(.fxa_in):not(.fxa_cpre):not(.fxa_tw):not(.fxa_lines):not(.fxa_wp):not(.fxa_cl):not(.fxa_cc){opacity:0!important}'
    +'html.fxa-on .fxa_pre.fxa_y:not(.fxa_in){transform:translateY(var(--fxa-dist,28px))!important}'
    +'html.fxa-on .fxa_pre.fxa_yd:not(.fxa_in){transform:translateY(calc(-1*var(--fxa-dist,36px)))!important}'
    +'html.fxa-on .fxa_pre.fxa_xl:not(.fxa_in){transform:translateX(calc(-1*var(--fxa-dist,48px)))!important}'
    +'html.fxa-on .fxa_pre.fxa_xr:not(.fxa_in){transform:translateX(var(--fxa-dist,48px))!important}'
    +'html.fxa-on .fxa_pre.fxa_s:not(.fxa_in){transform:scale(var(--fxa-scale,.86))!important}'
    +'html.fxa-on .fxa_pre.fxa_ry:not(.fxa_in){transform:perspective(800px) rotateY(var(--fxa-deg,90deg))!important}'
    +'html.fxa-on .fxa_pre.fxa_fl:not(.fxa_in){transform:perspective(1200px) rotateY(var(--fxa-deg,80deg))!important}'
    +'html.fxa-on .fxa_pre.fxa_clip:not(.fxa_in){transform:translateY(var(--fxa-dist,40px))!important}'
    +'html.fxa-on .fxa_pre.fxa_bl:not(.fxa_in){filter:blur(var(--fxa-blur,14px))!important}'
    +'html.fxa-on .fxa_pre.fxa_cl:not(.fxa_in){clip-path:inset(0 100% 0 0)!important}'
    +'html.fxa-on .fxa_pre.fxa_cc:not(.fxa_in){clip-path:inset(0 50% 0 50%)!important}'
    +'html.fxa-on .fxa_pre.fxa_wp:not(.fxa_in){clip-path:inset(0 100% 0 0)!important}';
  // スクロールで画面に入ったら再生。JS無効なら全部表示（消えない保険）。"__ce"を含めない＝保存で残る。
  // ★時間トリガー(setTimeout)は使わない＝「スクロールで画面に入った時に1回だけ再生」に統一。
  //   IntersectionObserverだけで判定→発火したらunobserve（1回きり）。上部の要素は監視開始時に即発火＝読み込みで再生。
  var FX_RUN='(function(){var d=document,h=d.documentElement;'
    +'if(!d.querySelector(".fxa_pre,.fxa_hl,.fxa_cnt,.fxa_ud")){return;}h.classList.add("fxa-on");'
    +'[].slice.call(d.querySelectorAll(".fxa_pre")).forEach(function(el){if(el.style.transform)el.style.removeProperty("transform");});'  // 自動修復：出現アニメ要素に焼き込まれた古いtransform(プレビュー残骸)を消す＝過去に固まった分も開くだけで直る
    // ★地雷（2026-07-25修正）：再生後の .fxa_in は transform:none!important で「アニメ用のtransform」を
    //   消しているが、これは要素が元々持っているtransform（例：固定ヘッダーの translateX(-50%) 中央寄せ）も
    //   一緒に消してしまう＝再生し終わった瞬間にヘッダーが右へ705pxビヨーンと飛ぶ（実際に起きた）。
    //   そこで「アニメ前の素のtransform」を測って --fxa-tf0 に控え、.fxa_in はそれを書き戻す。
    //   測り方＝fxa_preクラスを一瞬外して計算値を読む（data-cebt＝ドラッグ前の控えがあればそれを優先）。
    //   ★matrixをそのまま控えると %指定が px に固定される（translateX(-50%)＝中央寄せが、その時の幅の
    //     px値で固まり、別の画面幅で見ると左右にズレる）。中央寄せと判る場合は -50% に読み替えて控える。
    // ★地雷：ここで正規表現は使わない。FX_RUNはJS文字列の中なので \\( と書いてもJS側で1段落ちて
    //   /^matrix(([^)]+))$/ になり、一致せず黙って何もしない（実際に半日ハマった）。indexOf/sliceで書く。
    +'function _tf50(el,t0){if(!t0||t0.indexOf("matrix(")!==0||t0.charAt(t0.length-1)!==")")return t0;'
    +'var v=t0.slice(7,-1).split(",").map(function(x){return parseFloat(x);});if(v.length<6)return t0;'
    +'if(!(Math.abs(v[0]-1)<.001&&Math.abs(v[1])<.001&&Math.abs(v[2])<.001&&Math.abs(v[3]-1)<.001))return t0;'
    +'var r=el.getBoundingClientRect();'
    +'var sx=(r.width>4&&Math.abs(v[4]+r.width/2)<=2)?"-50%":(v[4]+"px");'
    +'var sy=(r.height>4&&Math.abs(v[5]+r.height/2)<=2)?"-50%":(v[5]+"px");'
    +'return "translate("+sx+","+sy+")";}'
    +'function _setTf0(el,force){try{'
    +'if(!force&&el.style.getPropertyValue("--fxa-tf0"))return;'
    +'if(force)el.style.removeProperty("--fxa-tf0");'
    +'var bt=el.getAttribute("data-cebt");'
    +'if(bt&&bt!=="none"&&bt.indexOf("matrix")===0){el.style.setProperty("--fxa-tf0",_tf50(el,bt));return;}'
    +'var had=el.classList.contains("fxa_pre");if(had)el.classList.remove("fxa_pre");'
    +'var t0=getComputedStyle(el).transform;if(had)el.classList.add("fxa_pre");'
    +'if(t0&&t0!=="none")el.style.setProperty("--fxa-tf0",_tf50(el,t0));'
    +'}catch(_){}}'
    +'window.__fxaSetTf0=_setTf0;window.__fxaTf50=_tf50;'   // 🧢ヘッダー修復から呼ぶ（幅を直したあとに測り直す＝-50%として控えられる）
    +'[].slice.call(d.querySelectorAll(".fxa_pre")).forEach(function(el){_setTf0(el);});'
    // 🖍マーカーの帯を毎フレーム手動で描く（--hlw を0→100へ）。連打で二重に走らないよう世代番号(__hlGen)で古いループは自分で止める。
    // ★進捗は「実時間」でなく1フレーム最大64msの積算で進める（飛行ランタイムと同じ流儀）。
    //   実時間だと画像読み込み中のコマ落ちで線がワープし「設定より速く引かれた」ように見える。
    +'function sweepHl(el){var dur=parseFloat(el.style.getPropertyValue("--hldur"))||0.45;var gen=(el.__hlGen=(el.__hlGen||0)+1);var acc=0,lastTs=null;'
    +'function step(ts){if(el.__hlGen!==gen)return;if(lastTs!==null)acc+=Math.min(64,ts-lastTs);lastTs=ts;var p=Math.min(1,acc/(dur*1000));var e=1+2.70158*Math.pow(p-1,3)+1.70158*Math.pow(p-1,2);'
    +'el.style.setProperty("--hlw",e*100);if(p<1)requestAnimationFrame(step);else el.classList.add("fxa_in");}'
    +'requestAnimationFrame(step);}'
    +'window.__fxaSweepHl=sweepHl;'
    // 🔢カウントアップ：文字中の最初の数字を0→目標値へ（カンマ・小数の桁は元の書式を保つ）。
    // 進捗はマーカーと同じ「1フレーム最大64msの積算」＝コマ落ちでワープしない。終わったら元のHTMLへ戻す（中の装飾タグを壊さない）。
    +'function countUp(el){var m=(el.textContent||"").match(/[-+]?[\\d,]+(?:\\.\\d+)?/);if(!m){el.classList.add("fxa_in");return;}'
    +'var keep=el.innerHTML,raw=m[0],tgt=parseFloat(raw.replace(/,/g,"")),dec=(raw.split(".")[1]||"").length,com=raw.indexOf(",")>=0;'
    +'var pre=(el.textContent||"").slice(0,m.index),suf=(el.textContent||"").slice(m.index+raw.length);'
    +'var dur=parseFloat(el.style.getPropertyValue("--fxa-dur"))||1200,acc=0,last=null;'
    +'function fmt(v){var s=v.toFixed(dec);if(com){var pp=s.split(".");pp[0]=pp[0].replace(/\\B(?=(\\d{3})+(?!\\d))/g,",");s=pp.join(".");}return s;}'
    +'function st(ts){if(last!==null)acc+=Math.min(64,ts-last);last=ts;var p=Math.min(1,acc/dur),e=1-Math.pow(1-p,3);'
    +'el.textContent=pre+fmt(tgt*e)+suf;if(p<1)requestAnimationFrame(st);else{el.innerHTML=keep;el.classList.add("fxa_in");}}'
    +'requestAnimationFrame(st);}'
    +'function all(){return [].slice.call(d.querySelectorAll(".fxa_pre:not(.fxa_in),.fxa_hl:not(.fxa_in),.fxa_cnt:not(.fxa_in),.fxa_ud:not(.fxa_in)"));}'
    // 🔢グループ表示：data-cegrp="1/2/3"の要素は①→②→③の順にまとめて動く（グループ間0.3s・グループ内は0.15sずつ）
    +'function groupDelay(el){var g=+el.getAttribute("data-cegrp")||0;if(!g)return 0;'
    +'var mem=[].slice.call(d.querySelectorAll(\\'[data-cegrp="\\'+g+\\'"]\\'));var idx=mem.indexOf(el);'
    +'return (g-1)*300+Math.max(0,idx)*150;}'
    // data-cedelay="ミリ秒" を要素に直接付けると、グループ計算より優先してその通りの遅れで再生する（細かい手動演出用）
    // ★マーカーはページ読み込み中に始めるとコマ落ちして「設定より速く引かれた」ように見える
    //   （--hldurは正しく効いているのに、画像読み込みでrAFの描画が飛ぶ）。
    //   → 読み込み完了(load)まで待ってから引く。文字自体は見えているので遅らせても安全。
    // ★マーカー/下線/カウントは「親がまだ出現待ちで透明」の間は走らせない（透明のまま走り終わり
    //   「最初から引かれた状態」に見える実害があった）。親が現れるまで150ms間隔で待つ（上限20秒）
    +'function reveal(el){ function go(){ '
    +'if(el.classList.contains("fxa_cnt")||el.classList.contains("fxa_hl")||el.classList.contains("fxa_ud")){'
    +'var anc=el.parentElement&&el.parentElement.closest?el.parentElement.closest(".fxa_pre:not(.fxa_in)"):null;'
    +'if(anc&&(el.__fxaWait=(el.__fxaWait||0)+1)<130){setTimeout(go,150);return;}}'
    +'if(el.classList.contains("fxa_cnt")){countUp(el);} else if(el.classList.contains("fxa_hl")||el.classList.contains("fxa_ud")){'
    +'if(d.readyState==="complete") sweepHl(el); else{var done=false,start=function(){if(done)return;done=true;sweepHl(el);};window.addEventListener("load",function(){setTimeout(start,250);},{once:true});setTimeout(start,700);}'
    +'} else el.classList.add("fxa_in"); }'
    +'var cd=el.getAttribute("data-cedelay"); var gd=cd!=null?+cd:groupDelay(el); if(gd>0) setTimeout(go,gd); else go(); }'
    +'if(!("IntersectionObserver" in window)){all().forEach(reveal);return;}'
    +'var io=new IntersectionObserver(function(es){es.forEach(function(en){if(en.isIntersecting){var t=en.target;io.unobserve(t);requestAnimationFrame(function(){reveal(t);});}});},{threshold:0,rootMargin:"0px 0px -18% 0px"});'
    // only: true=オープニングの幕の中だけ / false=幕の外だけ / 省略=全部
    // ★幕の中身（ロゴ・文字）は幕が出ている今この瞬間に動かないと意味がない。
    //   ここを分けずに全部待たせていたため、ロゴに動きを付けても幕が消えた後（＝見えない所）で
    //   再生され、「付けたのに変な動きになる」状態だった（2026-07-29）。
    +'function obs(only){requestAnimationFrame(function(){all().forEach(function(el){'
    +'if(only!=null){var inOp=!!(el.closest&&el.closest("#__op_screen"));if(inOp!==only)return;}'
    +'io.observe(el);});});}'
    +'window.fxaObs=obs;'   // ★編集中に新しく動きを付けた要素も監視に入れる（付けた直後は監視対象外だった）  // 初回描画(fxa_pre隠れ状態)を1フレーム待ってから監視開始＝上部要素も一瞬で終わらずスライドする
    // ★オープニングの幕が出ている間は監視を始めない（幕の裏で出現アニメが終わってしまうのを防ぐ）。
    //   幕が消えたら "ce-op-done" が飛んでくる。保険で7秒後にも必ず動き出す（二重に呼んでも
    //   IntersectionObserver.observe は同じ要素なら無視されるので副作用なし）。
    +'function boot(){if(d.readyState==="loading")d.addEventListener("DOMContentLoaded",function(){obs();});else obs();}'
    +'if(window.__opWait){'
    +'if(d.readyState==="loading")d.addEventListener("DOMContentLoaded",function(){obs(true);});else obs(true);'  // 幕の中身は今すぐ動かす
    +'window.addEventListener("ce-op-done",boot,{once:true});setTimeout(boot,7000);}else boot();})();';
  // CSSは「消して足す」でなく内容だけ差し替える（一瞬スタイルが消えるチラつき・前のアニメへの干渉を防ぐ）
  function _fxInjCss(){ var st=document.getElementById('fxa-css'); if(st){ if(st.textContent!==FX_CSS) st.textContent=FX_CSS; return; } st=document.createElement('style'); st.id='fxa-css'; st.textContent=FX_CSS; (document.head||document.documentElement).appendChild(st); }
  // runは「無ければ足すだけ」＝既にあれば再実行しない（毎回の焼き込みで再実行→重複observer→前のアニメが乱れるのを防ぐ）
  function _fxInjRun(){ if(document.getElementById('fxa-run')) return; var sc=document.createElement('script'); sc.id='fxa-run'; sc.textContent=FX_RUN; (document.body||document.documentElement).appendChild(sc); }
  function ensureFxAssets(){ _fxInjCss(); _fxInjRun(); }  // applyBakeから毎回呼ばれても副作用が無い
  // 既存カンプを開いた瞬間に1回だけ：焼き込み済みの古いrunを最新版へ入れ替える（clip撤廃・マーカー再生方式の変更などを既存にも反映）
  if(document.querySelector('.fxa_pre,.fxa_wave,.fxa_ch,.fxa_hl,.fxa_cnt,.fxa_ud,[class*="fxa_lp_"]')){ var _or=document.getElementById('fxa-run'); if(_or) _or.remove(); ensureFxAssets(); }
  function fxClearClasses(el){
    [].slice.call(el.classList).forEach(function(c){ if(c.indexOf('fxa_')===0 && c!=='fxa_ch') el.classList.remove(c); });
    ['--fxa-dur','--fxa-dist','--fxa-scale','--fxa-blur','--fxa-deg','--fxa-amp','--fxa-stag','--fxa-bnc'].forEach(function(p){ el.style.removeProperty(p); });
  }
  function _fxDist(el,a){ el.style.setProperty('--fxa-dist', fxParam(a,'dist')+'px'); }
  // 跳ね具合 0〜100 を cubic-bezier の行き過ぎ量 1.0〜3.0 に変換する（0＝跳ねずになめらか）
  function _fxBnc(a){
    var v=fxParam(a,'bnc');
    if(v==null||isNaN(v)) v=30;                       // 既定30＝これまでの1.6相当
    return Math.round((1+v/50)*100)/100;
  }
  // 出現アニメを要素に直接かけると、その要素自身のCSSアニメ（ボタンのループ鼓動など）がtransformを毎フレーム
  // 上書きして「せり上がり等の動きが出ない」。→ そういう要素だけラッパーで包み、ラッパー側で出現させる
  // （中の要素は自分のアニメ・ホバーをそのまま保てる）。
  function fxUnwrap(el){
    var w=el&&el.parentElement;
    if(w&&w.classList&&w.classList.contains('fxa_wrap')){
      // position:absoluteだった要素を包んだ時は、包む際にel側へ強制したstatic化を元に戻す
      // （戻さないと、外した後も浮かぶイラスト等が本来の絶対配置に戻らず表示位置が壊れる）。
      if(w.dataset.fxpw==='1'){
        ['position','left','top','right','bottom','width','height','z-index'].forEach(function(p){ el.style.removeProperty(p); });
      }
      w.parentNode.insertBefore(el,w); w.remove();
    }
  }
  function fxWrap(el){
    var w=document.createElement('span'); w.className='fxa_wrap';
    var cs=null; try{ cs=getComputedStyle(el); }catch(_){}
    // position:absolute/fixedの装飾要素（浮かぶイラスト等）をそのまま素のspanで包むと、
    // 中身は元通り絶対配置で浮いたままラッパー自身は中身を持たない0サイズの箱になる。
    // →その0サイズの箱がクリック判定を奪ってしまい、以後その場所を右クリックしても
    //   別の（大きな）親要素が選ばれてしまう＝「付けたはずのアニメが触れなくなる」不具合の元。
    // 対策：ラッパー側に絶対配置と実寸を持たせ、中身はラッパーいっぱいのstaticに変える。
    if(cs && (cs.position==='absolute' || cs.position==='fixed') && el.parentNode===el.offsetParent){
      var ow=el.offsetWidth, oh=el.offsetHeight, ol=el.offsetLeft, ot=el.offsetTop, zi=cs.zIndex;
      w.dataset.fxpw='1';
      w.style.position=cs.position;
      w.style.left=ol+'px'; w.style.top=ot+'px';
      w.style.width=ow+'px'; w.style.height=oh+'px';
      if(zi && zi!=='auto') w.style.zIndex=zi;
      el.parentNode.insertBefore(w,el); w.appendChild(el);
      el.style.setProperty('position','static','important');
      el.style.setProperty('left','auto','important'); el.style.setProperty('top','auto','important');
      el.style.setProperty('right','auto','important'); el.style.setProperty('bottom','auto','important');
      el.style.setProperty('width','100%','important'); el.style.setProperty('height','100%','important');
      el.style.setProperty('z-index','auto','important');
      return w;
    }
    var disp=''; try{ disp=cs?cs.display:''; }catch(_){}
    w.style.display=(disp&&disp.indexOf('inline')===0)?'inline-block':'block';
    el.parentNode.insertBefore(w,el); w.appendChild(el); return w;
  }
  // 🧩 グループの1つに動きを付けたら、仲間ぜんぶに同じ動きを付ける（2026-07-31・要望
  //   「グループ化して、ちゃんとアニメーションと一緒になるように」）。
  // ★再入り防止(__fxGrp)が要る：中で自分をもう一度呼ぶので、無いと無限ループになる。
  var __fxGrp=false;
  function applyBake(el,k){
    if(!__fxGrp && el && el.getAttribute && el.getAttribute('data-cegid')){
      var mates=groupMates(el);
      if(mates && mates.length>1){
        __fxGrp=true;
        try{
          mates.forEach(function(m){ if(m!==el) applyBake(m,k); });
        } finally { __fxGrp=false; }
        if(msg) setTimeout(function(){ msg.textContent='🧩 グループ '+mates.length+'個に同じ動きを付けました（⏳一覧で順番・速さを調整できます）'; },30);
      }
    }
    var a=fxDef(k); if(!a){ if(msg)msg.textContent='⚠ まず動きを選んでください'; return; }
    var _fxShown=null;   // 付け終わったあとに1回だけ再生して見せる相手
    ensureFxAssets();
    fxUnwrap(el);  // 既存の出現ラッパーがあれば解除して素の要素に戻す（付け直し対応）
    stopAnim(el); purgeInlineFx(el); fxClearClasses(el); fxUnsplit(el); fxStripImpLetters(el);  // 2回目以降も必ずプレーン文字から＋一括改善の文字アニメを外す（上書き消え防止）
    // ★ドラッグ/拡大で付いた transition:none / animation:none を外す。これが残ると出現もループも一瞬で終わって「動かない」に見える。
    el.style.removeProperty('transition'); el.style.removeProperty('animation');
    // ★インラインの transform:none!important も外す（2026-07-28）。
    //   位置調整・余白そろえ等を通った要素には applyTf が transform:none!important を焼き込む。
    //   インラインの!importantはCSS側の!importantより強いので、動き（translateY等）が1pxも効かない
    //   ＝「薄く出るだけで動かない」になる。ズレの値は translate/rotate/scale が持つので位置は変わらない。
    if(el.style.getPropertyValue('transform')==='none') el.style.removeProperty('transform');
    // ★保険が付けた「見せるクラス」が過去の保存で焼き込まれていると、ページCSSの
    //   .inview{opacity:1!important;transform:none!important} 等に負けて出現アニメが一切効かない
    //   （実際に起きた：3D回転を付けても動かない）。reveal系の要素だけ、その場で外して主導権を取り戻す。
    if(el.matches && el.matches('[class*="reveal"],[class*="fade"],[class*="animate"],[class*="inview"],[class*="in-view"],[class*="stagger"],[class*="slide"],[class*="appear"],[data-reveal]')){
      ['in','show','is-visible','active','visible','in-view','inview','animated','revealed','aos-animate','is-inview','is-show','reveal-show','show-up','on','enter'].forEach(function(c){ el.classList.remove(c); });
    }
    // ★動きは常に「1つだけ」にする（2026-07-28）。ページ自身の出現アニメのトリガークラスを外す。
    //   残っているとページ側(.reveal等)とツール側(fxa)が同時に動き、右下→左上のような斜めの変な動きになる。
    //   外したクラスは data-cerevcls に覚えておき、動きを消した時に元へ戻す。
    ['reveal','stagger'].forEach(function(c){
      if(el.classList.contains(c)){
        el.classList.remove(c);
        var k=el.getAttribute('data-cerevcls')||'';
        if(k.split(' ').indexOf(c)<0) el.setAttribute('data-cerevcls',(k?k+' ':'')+c);
      }
    });
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
        el.classList.add('fxa_pre'); el.classList.add(a.type===2?'fxa_sk':(a.type?'fxa_tw':'fxa_cpre'));
        el.style.setProperty('--fxa-stag', fxParam(a,'stag')+'ms');
        if(a.type===2){  // にじみ出る：1文字の時間とぼかし量を持たせる（隣と重なって滑らかに見える）
          el.style.setProperty('--fxa-dur', (fxParam(a,'dur')||1600)+'ms');
          el.style.setProperty('--fxa-blur', (fxParam(a,'blur')||10)+'px');
        }
        if(!a.type){
          el.style.setProperty('--fxa-dist', fxParam(a,'dist')+'px');
          el.style.setProperty('--fxa-bnc', String(_fxBnc(a)));     // 跳ね具合
        }
        el.classList.add('fxa_in');  // 編集中はすぐ見えるように（保存時にfxa_inは外す＝再生に戻る）
        _fxShown=el;
      }
    } else if(a.g==='lines'){
      // 行マスク：<br>区切りで行に分割して包む（戻すのはfxUnsplit）。--iは行番号＝時差の元
      var lns=splitLines(el);
      if(!lns.length){ el.style.removeProperty('--fxa-dur'); if(msg)msg.textContent='⚠ ここには文字が無いので行マスクは付けられません'; return; }
      el.classList.add('fxa_pre'); el.classList.add('fxa_lines');
      el.style.setProperty('--fxa-stag', fxParam(a,'stag')+'ms');
      el.classList.add('fxa_in'); _fxShown=el;
    } else if(a.g==='cnt'){
      var mm=(el.textContent||'').match(/[-+]?[\\d,]+(?:\\.\\d+)?/);
      if(!mm){ el.style.removeProperty('--fxa-dur'); if(msg)msg.textContent='⚠ ここには数字が無いのでカウントアップは付けられません（例：120件・98%）'; return; }
      el.classList.add('fxa_cnt');  // 表示は完成状態のまま＝JSが無くても壊れない。開いた時にランタイムが0から回す
      el.classList.add('fxa_in');
    } else {
      // 出現(in)：要素自身にCSSアニメ(ボタンのループ等)がある時だけラッパーで包み、それに出現をかける
      // （transformの奪い合いを回避＝せり上がり等がちゃんと動く。中の要素は自分のアニメ・ホバーを保つ）。
      var host=el, an='none', _fixed=false;
      try{ an=getComputedStyle(el).animationName||'none'; }catch(_){}
      try{ _fixed=(getComputedStyle(el).position==='fixed'); }catch(_){}
      // 自分のCSSアニメ持ちだけラッパーに出現をかける（transformの奪い合い回避）。
      // 移動は translate 個別プロパティに乗っているので、出現アニメ(transform)と両立＝移動要素もそのまま付けてOK。
      // ★position:fixed だけは絶対に包まない（2026-07-30）：中身が浮くのでラッパーの高さが0になり、
      //   さらに画面の上に出てしまうため「見えたら再生」の合図が永久に来ない＝開始位置(-36px等)で
      //   固まったまま降りてこない。固定ヘッダーで実際に発生した。代わりに要素自身に直接かけ、
      //   ケンカする元アニメだけ止める（動きは常に1つだけ、の原則どおり）。
      if(an!=='none' && _fixed){ el.style.setProperty('animation','none','important'); }
      else if(an!=='none'){ host=fxWrap(el); }
      host.style.setProperty('--fxa-dur', (fxParam(a,'dur')||800)+'ms');
      host.classList.add('fxa_pre');
      if(a.dir==='y'){ host.classList.add('fxa_y'); host.style.setProperty('--fxa-dist', fxParam(a,'dist')+'px'); }
      else if(a.dir==='yd'){ host.classList.add('fxa_yd'); host.style.setProperty('--fxa-dist', fxParam(a,'dist')+'px'); }
      else if(a.dir==='xl'){ host.classList.add('fxa_xl'); host.style.setProperty('--fxa-dist', fxParam(a,'dist')+'px'); }
      else if(a.dir==='xr'){ host.classList.add('fxa_xr'); host.style.setProperty('--fxa-dist', fxParam(a,'dist')+'px'); }
      else if(a.dir==='s'){ host.classList.add('fxa_s'); host.style.setProperty('--fxa-scale', (fxParam(a,'scale')/100)); }
      else if(a.dir==='bl'){ host.classList.add('fxa_bl'); host.style.setProperty('--fxa-blur', fxParam(a,'blur')+'px'); }
      else if(a.dir==='ry'){ host.classList.add('fxa_ry'); host.style.setProperty('--fxa-deg', fxParam(a,'deg')+'deg'); }
      else if(a.dir==='fl'){ host.classList.add('fxa_fl'); host.style.setProperty('--fxa-deg', fxParam(a,'deg')+'deg'); }
      else if(a.dir==='wp'){ host.classList.add('fxa_wp'); }
      else if(a.dir==='cl'){ host.classList.add('fxa_cl'); }
      else if(a.dir==='cc'){ host.classList.add('fxa_cc'); }
      else if(a.dir==='clip'){ host.classList.add('fxa_clip'); host.style.setProperty('--fxa-dist', fxParam(a,'dist')+'px'); }
      host.classList.add('fxa_in');
      _fxShown=host;
    }
    // ★付けた直後に1回だけ再生して見せる（今までは「見えている状態」で終わるので
    //   本当に付いたのか分からず、「動かない」と誤解する原因になっていた）
    if(_fxShown){
      (function(n){
        try{
          n.classList.remove('fxa_in'); void n.offsetWidth;   // 隠し状態を1フレーム挟んでtransitionを走らせる
          requestAnimationFrame(function(){ n.classList.add('fxa_in'); });
          setTimeout(function(){ n.classList.add('fxa_in'); },500);  // 保険：rAFが来ない環境でも必ず見える状態で終わる
        }catch(_){ n.classList.add('fxa_in'); }
      })(_fxShown);
    }
    try{ if(window.fxaObs) window.fxaObs(); }catch(_){}   // 監視に入れる（保存し直さなくてもスクロール再生が効く）
    markDirty();
    if(msg) msg.textContent='✅ 付けました（今1回再生しました）。ヘッダの「💾 変更を保存」で残ります';
  }
  // 動きが実際に付いている要素（自分・外側・中）を集める。
  // ★右クリックした<img>ではなく、外側の<picture>や<p>に動きが付いていることが多い。
  //   自分だけ見ていたので「🚫動きを消すを押しても消えない」報告が出た（2026-07-29・実例：
  //   <p class="top02__tree"><picture class="fxa_y fxa_pre"><span><span><img …></span></span></picture></p>
  //   ＝imgのクラスは空なのに、動きは2つ外側のpictureに付いていた）。
  var FX_HOST_SEL='.fxa_pre,.fxa_cpre,.fxa_tw,.fxa_sk,.fxa_lines,.fxa_hl,.fxa_cnt,.fxa_ud,.fxa_wave,.fxa_wrap,'
    +'[class*="fxa_lp_"],[data-fxa-fly],[data-cefly]';
  function fxHosts(el){
    var out=[];
    function add(n){ if(n&&n.nodeType===1&&out.indexOf(n)<0) out.push(n); }
    add(el);
    // 外へ最大5段まで（セクションの器は越えない＝隣のブロックの動きまで巻き込まないため）
    var p=el.parentElement, hop=0;
    while(p && hop<5 && p!==document.body && !/^(SECTION|HEADER|FOOTER|MAIN|ARTICLE)$/.test(p.tagName)){
      try{ if(p.matches&&p.matches(FX_HOST_SEL)) add(p); }catch(_){}
      p=p.parentElement; hop++;
    }
    try{ [].slice.call(el.querySelectorAll(FX_HOST_SEL)).forEach(add); }catch(_){}
    return out;
  }
  // 付けた動き（出現/ループ/文字アニメ）を全部外して素の要素に戻す（AIなし・即反映）。
  function removeBake(el){
    if(!el) return;
    var hosts=fxHosts(el);
    if(hosts.length>1){                       // 自分以外にも動きの持ち主がいた＝まとめて外す
      hosts.forEach(function(n){ if(n!==el) removeBakeOne(n); });
    }
    removeBakeOne(el);
    // ★クローン元サイトのCSSで動いているアニメを止める（2026-07-29・実例：
    //   `.top02__tree img{animation:treeanime 2s infinite alternate}` ＝ ゆらゆら揺れ続ける木のイラスト）。
    //   これはクラスにもインラインにも現れない「CSSファイル側の指定」なので、
    //   クラスを外す・インラインのanimationを消すだけでは絶対に止まらない。
    //   しかも removeBakeOne の `style.removeProperty('animation')` が、以前止めていた
    //   inline の animation:none まで消してしまい「消したのにまた動き出す」原因になっていた。
    var stopped=0;
    try{
      [el].concat([].slice.call(el.querySelectorAll('*'))).slice(0,400).forEach(function(n){
        if(!n.style) return;
        var an=''; try{ an=getComputedStyle(n).animationName; }catch(_){ return; }
        if(an && an!=='none'){ n.style.setProperty('animation','none','important'); stopped++; }
      });
    }catch(_){}
    // クラスは外れたのに設定値(--fxa-*)だけ中の要素に残ることがある。害は無いが
    // 「消したのに残っている」ように見えるので一緒に掃除する（まだ動きが付いている要素は触らない）。
    try{
      var FXVARS=['--fxa-dur','--fxa-dist','--fxa-scale','--fxa-blur','--fxa-deg','--fxa-amp','--fxa-stag','--fxa-bnc'];
      [].slice.call(el.querySelectorAll('[style*="--fxa-"]')).concat([el]).forEach(function(n){
        if(!n.style) return;
        if(n.matches&&n.matches(FX_HOST_SEL)) return;
        FXVARS.forEach(function(p){ n.style.removeProperty(p); });
      });
    }catch(_){}
    markDirty();
    var tail=(hosts.length>1?('・外側/中の '+(hosts.length-1)+' 個ぶんも一緒に'):'')
      +(stopped?('・元サイトのCSSで動いていた '+stopped+' 個も止めました'):'');
    if(msg) msg.textContent='この要素の動きを消しました'+tail+'（💾保存で確定）';
    try{ ceFlash('🚫 動きを消しました（'+(hosts.length+stopped)+'箇所）'); }catch(_){}
  }
  function removeBakeOne(el){
    if(!el) return;
    fxUnwrap(el);  // 自分のCSSアニメ用に包んだラッパーがあれば解除
    // 🕊 飛行ルートも外す（属性を消すとランタイムのループは次フレームで自動停止する）
    if(el.getAttribute('data-fxa-fly')!=null||el.getAttribute('data-cefly')!=null){ el.removeAttribute('data-fxa-fly'); el.removeAttribute('data-cefly'); el.ceflyGen=(el.ceflyGen||0)+1; }
    stopAnim(el); clearPreviewStyle(el); fxClearClasses(el); fxUnsplit(el); fxStripImpLetters(el);
    // 焼き込み時に外したページ側のトリガークラス(.reveal等)を戻す＝元のページの動きに戻す
    var _rv=el.getAttribute('data-cerevcls');
    if(_rv){ _rv.split(' ').forEach(function(c){ if(c) el.classList.add(c); }); el.removeAttribute('data-cerevcls'); }
    el.style.removeProperty('transition'); el.style.removeProperty('animation');
  }
  // ===== ⏳ 演出タイミング（AIなし・無料）＝旧⏱順番モードを一覧型に統合（2026-07-12） =====
  function seqAnimKey(el){
    var c=el.classList;
    if(c.contains('fxa_hl')) return 'hl';
    if(c.contains('fxa_ud')) return 'ud';
    if(c.contains('fxa_cnt')) return 'count';
    if(c.contains('fxa_tw')) return 'typewriter';
    if(c.contains('fxa_sk')) return 'soak';
    if(c.contains('fxa_cpre')) return 'stagger';
    if(c.contains('fxa_lines')) return 'lines';
    if(c.contains('fxa_yd')) return 'fadedown';
    if(c.contains('fxa_xl')) return 'left';
    if(c.contains('fxa_xr')) return 'right';
    if(c.contains('fxa_s')) return 'zoom';
    if(c.contains('fxa_bl')) return 'blur';
    if(c.contains('fxa_ry')) return 'flip';
    if(c.contains('fxa_fl')) return 'pageflip';
    if(c.contains('fxa_wp')) return 'wipe';
    if(c.contains('fxa_cl')) return 'curtain';
    if(c.contains('fxa_cc')) return 'curtainc';
    if(c.contains('fxa_clip')) return 'rise';
    if(c.contains('fxa_y')) return 'fadeup';
    return 'fade';
  }
  // ===== ⏳ 動きの遅れ・速さ調整（AIなし・無料）：右クリック位置の動きを選んで遅らせ／速さを変える =====
  // 右クリックしたセクション内の全アニメ（🖍マーカー・出現・カウント）を一覧に並べ、
  // ↑↓で順番を入れ替え→「⏱上から順に刻む」でdata-cedelayを間隔刻みに自動割り当て。
  // ⏳遅らせ（data-cedelay）と🐢速さ（--hldur/--fxa-dur）は数値入力で即反映。✖で開いた時点に復元。
  var _dlyP=null;
  // 🕊 空飛ぶルート（紙飛行機など）も演出の一員なので、この一覧で順番・速さを扱えるようにする（2026-07-30・要望）。
  // ★速さの持ち方が他と違う：出現アニメは --fxa-dur / マーカーは --hldur だが、
  //   飛行は data-fxa-fly のJSONの d（ミリ秒）に入っている。読み書きを専用に分ける。
  var DLY_SEL='.fxa_hl,.fxa_pre,.fxa_cnt,.fxa_ud,[data-fxa-fly],[data-cefly]';
  function _isFly(c){ return !!(c&&c.getAttribute&&(c.getAttribute('data-fxa-fly')!=null||c.getAttribute('data-cefly')!=null)); }
  function _flyCfg(c){
    try{ return JSON.parse(c.getAttribute('data-fxa-fly')||c.getAttribute('data-cefly')||'null')||null; }catch(_){ return null; }
  }
  function _flyDur(c){ var g=_flyCfg(c); return (g&&+g.d)||4000; }
  function _flySetDur(c,v){
    var g=_flyCfg(c); if(!g) return;
    g.d=Math.max(200,Math.round(v));
    c.setAttribute(c.getAttribute('data-fxa-fly')!=null?'data-fxa-fly':'data-cefly', JSON.stringify(g));
  }
  function _flyReplay(c){
    c.ceflyGen=(c.ceflyGen||0)+1;                 // 走っている回を無効にする（ランタイムが世代で判定している）
    try{ ensureFlyRun(); }catch(_){}
    var d=+c.getAttribute('data-cedelay')||0;
    clearTimeout(c.__dlyT);
    c.__dlyT=setTimeout(function(){ if(window.ceflyArm) window.ceflyArm(c); }, d);
  }
  function dlyLabel(el){
    if(_isFly(el)){
      var tx0=(el.tagName==='IMG')?((el.getAttribute('alt')||'').slice(0,10)):((el.textContent||'').replace(/\\s+/g,' ').trim().slice(0,10));
      return '🕊 飛ぶ'+(tx0?('「'+tx0+'…」'):'');
    }
    var k=seqAnimKey(el), def=null;
    for(var i=0;i<FX.length;i++){ if(FX[i].k===k){ def=FX[i]; break; } }
    var nm=(k==='hl')?'🖍 マーカー':(k==='ud')?'〰 下線':(def?def.b:'動き');
    var tx=(el.textContent||'').replace(/\\s+/g,' ').trim().slice(0,10);
    return nm+(tx?('「'+tx+'…」'):'');
  }
  // 保険スクリプトが付けた「見せるクラス」(in/show/inview等)を外す＝これが残るとページCSSの
  // .reveal.in{transform:none!important}等が勝ち続けて「隠す」が効かず、⏳再生が見た目ゼロになる（実測）
  function stripShowCls(el){
    if(el.matches && el.matches('[class*="reveal"],[class*="fade"],[class*="animate"],[class*="inview"],[class*="in-view"],[class*="stagger"],[class*="slide"],[class*="appear"],[data-reveal]')){
      ['in','show','is-visible','active','visible','in-view','inview','animated','revealed','aos-animate','is-inview','is-show','reveal-show','show-up','on','enter'].forEach(function(c){ el.classList.remove(c); });
    }
  }
  function dlyPreview(el){
    clearTimeout(el.__dlyT);
    var d=+el.getAttribute('data-cedelay')||0;
    if(_isFly(el)){ _flyReplay(el); return; }
    if(el.classList.contains('fxa_hl')||el.classList.contains('fxa_ud')){
      el.classList.remove('fxa_in'); el.style.setProperty('--hlw',0);
      el.__dlyT=setTimeout(function(){ if(window.__fxaSweepHl) window.__fxaSweepHl(el); else { el.style.setProperty('--hlw',100); el.classList.add('fxa_in'); } }, d);
    } else {
      ensureFxAssets();
      purgeInlineFx(el);  // 昔の保険が焼き込んだopacity:1!important等が残ると「隠す」が効かず再生が見えない
      stripShowCls(el);
      el.style.setProperty('transition','none','important'); el.classList.remove('fxa_in');
      void el.offsetWidth; el.style.removeProperty('transition');
      el.__dlyT=setTimeout(function(){ el.classList.add('fxa_in'); }, d+60);
    }
  }
  function dlyClose(){ if(_dlyP){ _dlyP.remove(); _dlyP=null; } }
  // 🧢 ページ上部のヘッダー帯（ロゴ＋ナビの横長バー）を探す。丸ごとヒーローのheaderは高さで除外
  function findTopBar(){
    var cs=[].slice.call(document.querySelectorAll('header, nav, [class*="nav"], [class*="header"]'));
    for(var i=0;i<cs.length;i++){
      var el=cs[i];
      if(el.closest('#__ce')||(el.id&&el.id.indexOf('__ce')===0)) continue;
      var r=el.getBoundingClientRect(), top=r.top+(window.scrollY||0);
      if(top<260 && r.height>0 && r.height<200 && r.width>window.innerWidth*0.5){
        // 中にほぼ同じ大きさの「既にアニメ持ちのバー」があればそちらを使う（外側と二重にしない）
        if(!el.classList.contains('fxa_pre')){
          var inn=el.querySelector('.fxa_pre');
          if(inn){ var ri=inn.getBoundingClientRect(); if(ri.height>=r.height*0.6&&ri.width>=r.width*0.6) return inn; }
        }
        return el;
      }
    }
    return null;
  }
  // ▶ 全体の流れ：そのセクション内の全アニメを、各自のdata-cedelayどおりに一斉再生（本番と同じ見え方）
  function flowRun(scope){
    ensureFxAssets();
    var els=[].slice.call(scope.querySelectorAll(DLY_SEL));
    if(scope.matches&&scope.matches(DLY_SEL)) els.unshift(scope);
    if(!els.length) return;
    function _isSw(el){ return el.classList.contains('fxa_hl')||el.classList.contains('fxa_ud'); }  // --hlwスイープ系（マーカー/下線）
    // 🕊 飛行は「隠して→出す」ではなく最初から走らせ直す仕組みなので、通し再生では別扱いにする
    var flys=els.filter(_isFly); els=els.filter(function(e){ return !_isFly(e); });
    flys.forEach(_flyReplay);
    if(!els.length) return;
    els.forEach(function(el){
      clearTimeout(el.__dlyT);
      if(_isSw(el)){ el.classList.remove('fxa_in'); el.style.setProperty('--hlw',0); }
      else { purgeInlineFx(el); stripShowCls(el); el.style.setProperty('transition','none','important'); el.classList.remove('fxa_in'); }
    });
    void document.body.offsetWidth;
    els.forEach(function(el){ if(!_isSw(el)) el.style.removeProperty('transition'); });
    els.forEach(function(el){
      var d=+el.getAttribute('data-cedelay')||0;
      el.__dlyT=setTimeout(function(){
        if(_isSw(el)){
          // 親が出現待ちで透明の間は待ってから走る（透明のまま引き終わる事故防止・本番ランタイムと同じ）
          (function w(n){
            var anc=el.parentElement&&el.parentElement.closest?el.parentElement.closest('.fxa_pre:not(.fxa_in)'):null;
            if(anc&&n<130){ el.__dlyT=setTimeout(function(){ w(n+1); },150); return; }
            if(window.__fxaSweepHl) window.__fxaSweepHl(el); else { el.style.setProperty('--hlw',100); el.classList.add('fxa_in'); }
          })(0);
        }
        else el.classList.add('fxa_in');
      }, d+60);
    });
  }
  // wide=true でページ全体を対象にする（2026-07-30・報告「4つ入れているのに1件しか出ない」）。
  // ★原因：対象が「右クリックした場所のセクション」だけだった。ヒーローが <header> の中にあると
  //   scope が <header> になり、その中の1件しか出ない＝他のセクションに付けた動きが見えない。
  //   既定は今までどおりセクション。ただし**1件以下しか無い時は自動でページ全体へ広げる**
  //   （1行だけの一覧は順番も付けられず役に立たないため）。ボタンでいつでも切り替えられる。
  // forced=true＝ボタンで明示的に選んだ時。★これが無いと「このセクションだけ」に戻しても
  //   1件しか無い→また自動でページ全体に広がる＝ボタンが効かないように見える（実測）。
  function dlyOpen(el,x,y,wide,forced){
    dlyClose();
    var sec=(el&&el.closest&&el.closest('section,header,footer'))||document.body;
    var scope=wide?document.body:sec, auto=false;
    function collect(sc){
      var out=[].slice.call(sc.querySelectorAll(DLY_SEL));
      if(sc.matches&&sc.matches(DLY_SEL)) out.unshift(sc);
      // 🕊 飛行はセクションをまたいで置かれていることがある（紙飛行機はヒーローの外側に居がち）ので、
      //    そのセクションに1つも入っていない時だけ、縦位置が重なっている物を拾って一覧に加える。
      if(sc!==document.body && !out.some(_isFly)){
        [].slice.call(document.querySelectorAll('[data-fxa-fly],[data-cefly]')).forEach(function(f){
          if(f.closest&&f.closest('[id^="__ce"]')) return;
          var fr=f.getBoundingClientRect(), sr=sc.getBoundingClientRect?sc.getBoundingClientRect():null;
          if(!sr||(fr.bottom>sr.top&&fr.top<sr.bottom)) out.push(f);
        });
      }
      return out.filter(function(n){ return !(n.closest&&n.closest('[id^="__ce"]')); });
    }
    var items=collect(scope);
    if(!wide && !forced && items.length<2){ scope=document.body; items=collect(scope); wide=true; auto=true; }
    items=items.slice(0, wide?40:20);
    if(!items.length){ if(msg) msg.textContent='このページに動きが見つかりません（先に「✨動きを付ける」やマーカーを付けてください）'; return; }
    // 今の遅らせ順に並べる（同点はDOM順）＝一覧がそのまま再生順に見える
    items=items.map(function(n,i){ return {el:n,i:i}; }).sort(function(a,b){
      var da=+a.el.getAttribute('data-cedelay')||0, db=+b.el.getAttribute('data-cedelay')||0;
      return (da-db)||(a.i-b.i);
    }).map(function(o){ return o.el; });
    // 開いた時点の値を要素ごと控える＝✖閉じるで元に戻せる（✔決定なら控えを捨てるだけ）
    function _isSw2(c){ return c.classList.contains('fxa_hl')||c.classList.contains('fxa_ud'); }  // --hlwスイープ系（マーカー/下線）
    var snap=items.map(function(c){
      return {el:c, d:c.getAttribute('data-cedelay'), hl:_isSw2(c), fly:_isFly(c),
              dur:_isFly(c)?_flyDur(c):(_isSw2(c)?c.style.getPropertyValue('--hldur'):c.style.getPropertyValue('--fxa-dur'))};
    });
    var p=document.createElement('div'); p.id='__ce_dlyp';
    p.setAttribute('style','position:fixed;z-index:2147483647;background:#1d1d2b;color:#fff;border-radius:12px;padding:10px 14px;box-shadow:0 6px 24px rgba(0,0,0,.4);font:12.5px/1.6 sans-serif;width:440px;max-width:96vw;max-height:72vh;overflow:auto');
    function durOf(c){ return _isFly(c)?_flyDur(c):(_isSw2(c)?Math.round((parseFloat(c.style.getPropertyValue('--hldur'))||0.45)*1000):(parseInt(c.style.getPropertyValue('--fxa-dur'))||800)); }
    function rowsHtml(){
      return items.map(function(c,i){
        return '<div class="__dlyrow" data-i="'+i+'" style="display:flex;align-items:center;gap:5px;padding:4px 0;border-bottom:1px solid #34344a">'
          +'<button data-mv="-1" title="上へ" style="background:#34344a;color:#fff;border:none;border-radius:5px;width:22px;height:22px;cursor:pointer;padding:0">↑</button>'
          +'<button data-mv="1" title="下へ" style="background:#34344a;color:#fff;border:none;border-radius:5px;width:22px;height:22px;cursor:pointer;padding:0">↓</button>'
          +'<span class="__dlylbl" title="クリックでその要素まで移動" style="flex:1;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+(i+1)+'. '+esc(dlyLabel(c))+'</span>'
          +'⏳<input data-k="d" type="number" min="0" step="100" value="'+(+c.getAttribute('data-cedelay')||0)+'" style="width:62px;padding:2px 4px;border-radius:5px;border:none">'
          +'🐢<input data-k="s" type="number" min="100" step="100" value="'+durOf(c)+'" style="width:62px;padding:2px 4px;border-radius:5px;border:none">'
          +'<button data-pl="1" title="この動きだけ再生" style="background:#3b82f6;color:#fff;border:none;border-radius:5px;padding:2px 7px;cursor:pointer">▶</button>'
          +'</div>';
      }).join('');
    }
    // ★数字が読めない事故の対策：このパネルはカンプの中に差し込むので、カンプ側のCSS
    //   （input{background:transparent}や-webkit-text-fill-color等）が数字入力に当たって
    //   「黒地に黒文字」になることがあった。!important付きで白地・黒文字を固定する。
    //   color-scheme:light＝▲▼スピナーもダークモードに引きずられない。
    p.innerHTML='<style>#__ce_dlyp input[type=number]{background:#fff!important;color:#111!important;'
      +'-webkit-text-fill-color:#111!important;border:1px solid #6b6b8a!important;opacity:1!important;'
      +'font:700 13px/1.5 sans-serif!important;text-align:right!important;color-scheme:light!important;'
      +'box-shadow:none!important;text-shadow:none!important;-webkit-appearance:auto!important}</style>'
      +'<b>⏳ 動きの演出（'+(wide?'ページ全体':'このセクション')+'・'+items.length+'件）</b>'
      +'<button id="__ce_dlyscope" title="対象を切り替える" style="background:#374151;color:#fff;border:none;border-radius:6px;padding:2px 8px;margin-left:6px;cursor:pointer">'
      +(wide?'📄 このセクションだけ':'🌐 ページ全体を出す')+'</button>'
      +'<div style="opacity:.75;font-size:11px;margin-top:2px">数字はms・変えると即反映'
      +(auto?'／このセクションには1件しか無かったのでページ全体を出しています':'')+'</div>'
      +'<div id="__ce_dlyrows" style="margin-top:6px">'+rowsHtml()+'</div>'
      +'<div style="margin-top:8px;display:flex;align-items:center;gap:6px">間隔<input id="__ce_dlyiv" type="number" value="600" min="100" step="100" style="width:62px;padding:2px 4px;border-radius:5px;border:none">ms'
      +'<button id="__ce_dlystep" title="上の並び順どおりに遅らせを自動で刻む" style="background:#7c3aed;color:#fff;border:none;border-radius:6px;padding:4px 9px;cursor:pointer">⏱ 上から順に刻む</button>'
      +'<button id="__ce_dlyhdr" title="ページ上部のヘッダー帯に「上から降りる」を付けて、全部の後に出す" style="background:#0e7490;color:#fff;border:none;border-radius:6px;padding:4px 9px;cursor:pointer">🧢 最後にヘッダーを下ろす</button></div>'
      +'<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">'
      +'<button id="__ce_dlyflow" style="background:#6366f1;color:#fff;border:none;border-radius:7px;padding:5px 10px;cursor:pointer">▶ 全体の流れ</button>'
      +'<button id="__ce_dlyok" style="background:#22c55e;color:#fff;border:none;border-radius:7px;padding:5px 10px;cursor:pointer;font-weight:700">✔ 決定</button>'
      +'<button id="__ce_dlyx" style="background:#555;color:#fff;border:none;border-radius:7px;padding:5px 10px;cursor:pointer">✖ 閉じる（元に戻す）</button>'
      +'</div>';
    document.body.appendChild(p);
    p.style.left=Math.max(6,Math.min(x,window.innerWidth-p.offsetWidth-8))+'px';
    p.style.top=Math.max(6,Math.min(y,window.innerHeight-p.offsetHeight-8))+'px';
    _dlyP=p;
    var rowsBox=p.querySelector('#__ce_dlyrows');
    function redraw(){ rowsBox.innerHTML=rowsHtml(); }
    function setDelay(c,v){ v=Math.max(0,Math.round(v)); if(v>0) c.setAttribute('data-cedelay',v); else c.removeAttribute('data-cedelay'); markDirty(); }
    function setDur(c,v){
      v=Math.max(100,Math.round(v));
      if(_isFly(c)){ _flySetDur(c,v); _flyReplay(c); }                       // 🕊 飛行は所要時間そのもの＝変えたらすぐ飛び直す
      else if(_isSw2(c)) c.style.setProperty('--hldur',(v/1000)+'s');
      else c.style.setProperty('--fxa-dur',v+'ms');
      markDirty();
    }
    rowsBox.addEventListener('input',function(ev){
      var row=ev.target.closest('.__dlyrow'); if(!row) return;
      var c=items[+row.getAttribute('data-i')];
      if(ev.target.getAttribute('data-k')==='d') setDelay(c,+ev.target.value||0);
      if(ev.target.getAttribute('data-k')==='s') setDur(c,+ev.target.value||800);
    });
    rowsBox.addEventListener('click',function(ev){
      var row=ev.target.closest('.__dlyrow'); if(!row) return;
      var i=+row.getAttribute('data-i'), c=items[i];
      if(ev.target.getAttribute('data-pl')){ dlyPreview(c); return; }
      var mv=ev.target.getAttribute('data-mv');
      if(mv){ var j=i+(+mv); if(j<0||j>=items.length) return; items.splice(i,1); items.splice(j,0,c); redraw(); return; }
      if(ev.target.classList.contains('__dlylbl')){ try{ c.scrollIntoView({block:'center',behavior:'smooth'}); }catch(_){} c.classList.add('__ce_sel'); setTimeout(function(){ c.classList.remove('__ce_sel'); },1200); }
    });
    p.querySelector('#__ce_dlystep').addEventListener('click',function(){
      var iv=Math.max(100,+p.querySelector('#__ce_dlyiv').value||600);
      items.forEach(function(c,i){ setDelay(c,i*iv); });
      redraw();
      flowRun(scope);  // 刻んだ結果をすぐ通しで見せる
    });
    p.querySelector('#__ce_dlyhdr').addEventListener('click',function(){
      var bar=findTopBar();
      if(!bar){ if(msg) msg.textContent='ヘッダー帯が見つかりません（ページ上部の横長バーが対象です）'; return; }
      applyBake(bar,'fadedown');  // 「上から降りる」を付ける（既に動きがあれば付け替え）
      var host=bar.classList.contains('fxa_pre')?bar:(bar.closest('.fxa_pre')||bar);
      var mx=0; items.forEach(function(c){ mx=Math.max(mx,+c.getAttribute('data-cedelay')||0); });
      var iv=Math.max(100,+p.querySelector('#__ce_dlyiv').value||600);
      if(items.indexOf(host)<0) items.push(host);
      setDelay(host, mx+iv);  // いちばん最後＝全員が出そろってから降りてくる
      redraw();
      flowRun(scope); if(!scope.contains(host)) dlyPreview(host);
      if(msg) msg.textContent='🧢 ヘッダーが最後（'+(mx+iv)+'ms後）に上から降りるようにしました。💾保存で残ります';
    });
    p.querySelector('#__ce_dlyscope').addEventListener('click',function(){ dlyOpen(el,x,y,!wide,true); });
    p.querySelector('#__ce_dlyflow').addEventListener('click',function(){ flowRun(scope); if(msg){} });
    p.querySelector('#__ce_dlyok').addEventListener('click',function(){ dlyClose(); if(msg) msg.textContent='⏳ 演出を反映しました。💾保存で残ります'; });
    p.querySelector('#__ce_dlyx').addEventListener('click',function(){
      snap.forEach(function(s){
        if(s.d!=null) s.el.setAttribute('data-cedelay',s.d); else s.el.removeAttribute('data-cedelay');
        if(s.fly){ _flySetDur(s.el, s.dur); return; }
        var prop=s.hl?'--hldur':'--fxa-dur';
        if(s.dur) s.el.style.setProperty(prop,s.dur); else s.el.style.removeProperty(prop);
      });
      dlyClose(); if(msg) msg.textContent='⏳ 変更を取り消して閉じました';
    });
  }

  // ===== 📋 要素のコピペ（Ctrl+C / Ctrl+V・AIなし・2026-07-12） =====
  // 右クリックで選んだ要素(curEl)をCtrl+Cでコピー→Ctrl+Vでマウス位置に貼り付け（placeFree＝レスポンシブ%配置）。
  // セクション/ヘッダー/フッターを丸ごとコピーした時は、元の直後に複製を挿入（絶対配置にしない）。
  // 文字入力中(input/contenteditable)や文字を選択中は何もしない＝普通のコピペを邪魔しない。
  var _ceClip=null, _ceMX=0, _ceMY=0, _ceCX=0, _ceCY=0;
  document.addEventListener('mousemove',function(e){ _ceMX=e.pageX; _ceMY=e.pageY; _ceCX=e.clientX; _ceCY=e.clientY; });

  // ===== ⌨ ショートカットキー（マウス位置の要素に既存機能を発火・設定で変更／家↔会社同期） =====
  // 割り当ては localStorage['__ce_shortcuts']（{op:key}）＋サーバー(/api/shortcuts)で家↔会社共有。
  // g＝写真を加工（よく使うので押しやすいキーに・2026-07-29 要望）／画像を追加は i（image）へ移動
  var SC_DEF={ txt:'t', edit:'e', img:'i', photo:'g', fx:'a', fav:'o', shapes:'b' };
  // op=処理／label=設定パネルの説明／mid=対応する右クリックメニュー項目ID（[t]表示に使う・無い操作はnull）
  var SC_META=[
    {op:'txt',   label:'✏ 文字を追加（カーソル位置に新規）', mid:'__ce_q_txt'},
    {op:'edit',  label:'✏ 文字を編集（カーソル下の文字）',   mid:null},
    {op:'img',   label:'🖼 画像を追加（カーソル位置）',       mid:'__ce_q_img'},
    {op:'photo', label:'🖼 写真を加工（カーソル下の画像）',   mid:'__ce_cmdeco'},
    {op:'fx',    label:'✨ 動きを付ける（アニメを選ぶ）',      mid:'__ce_q_fx'},
    {op:'fav',   label:'⭐ お気に入りメニュー（保存・切替・追加）', mid:'__ce_q_fav'},
    {op:'shapes',label:'🔶 図形バーを出す／閉じる（〇・線・アイコン）', mid:'__ce_shapes'}
  ];
  var _scKeys=null;
  function scKeys(){
    if(_scKeys) return _scKeys;
    var m={}; for(var k in SC_DEF){ m[k]=SC_DEF[k]; }
    try{ var s=JSON.parse(localStorage.getItem('__ce_shortcuts')||'null');
      if(s&&typeof s==='object'){ for(var k2 in s){ if(typeof s[k2]==='string') m[k2]=s[k2].toLowerCase(); } } }catch(_){}
    _scKeys=m; return m;
  }
  function scKeyOf(mid){ if(!mid) return ''; var ks=scKeys(); for(var i=0;i<SC_META.length;i++){ if(SC_META[i].mid===mid) return ks[SC_META[i].op]||''; } return ''; }
  function scSave(m){
    _scKeys=m;
    try{ localStorage.setItem('__ce_shortcuts', JSON.stringify(m)); }catch(_){}
    try{ fetch('/api/shortcuts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keys:m})}); }catch(_){}
  }
  // 起動時に1回だけサーバー版を読んでlocalStorageへ流し込む（menu_layoutと同じ同期方式）
  (function _syncShortcuts(){
    try{ fetch('/api/shortcuts').then(function(r){return r.json();}).then(function(d){
      if(d&&d.keys&&typeof d.keys==='object'){ try{ localStorage.setItem('__ce_shortcuts',JSON.stringify(d.keys)); }catch(_){} _scKeys=null; }
    }).catch(function(){}); }catch(_){}
  })();
  // 文字を追加：空の要素を置いて即編集パネルを開く。何も入力せず閉じたら（Escape/×）空要素を残さず消す。
  function _scAddText(px,py){
    var nd=document.createElement('div'); nd.textContent='';
    nd.setAttribute('style','z-index:'+_freeZIndex()+';font-size:32px;font-weight:700;color:#333;font-family:inherit;line-height:1.4;padding:4px 8px;white-space:nowrap');
    placeFree(nd,px,py); markDirty(); openBreakEditor(nd);
    var w=new MutationObserver(function(){
      if(document.getElementById('__ce_pk')) return;          // 編集パネルはまだ開いている
      w.disconnect();
      if(!(nd.textContent||'').replace(/[\\s\\u200b]/g,'')){    // 何も入力されていない＝空要素を残さない
        if(nd.parentNode) nd.parentNode.removeChild(nd);
      }
    });
    w.observe(document.body,{childList:true,subtree:true});
  }
  function scRun(op){
    // 🔶図形バーは「どの要素を狙っているか」に関係なく出せる＝対象を決める前に処理する
    if(op==='shapes'){ try{ closeMenu(); }catch(_){} openShapeBar(); return; }
    var cx=_ceCX, cy=_ceCY;
    var under=document.elementFromPoint(cx,cy);
    var overUI = under && (under.closest('[id^="__ce"]'));
    // 右クリックで選択済みの要素(curEl)があればそれを最優先で対象にする＝マウスがメニューの上でも効く。
    // 無ければ従来どおりマウス直下の要素で発火する。
    var sel=(curEl&&document.body.contains(curEl)&&!curEl.closest('[id^="__ce"]'))?curEl:null;
    var el, px, py, uForEdit;
    if(sel){
      el=sel; uForEdit=sel;
      var r=sel.getBoundingClientRect();
      px=(window.scrollX||window.pageXOffset||0)+r.left+Math.min(24,r.width/2);
      py=(window.scrollY||window.pageYOffset||0)+r.top+Math.min(24,r.height/2);
    } else {
      if(!under||overUI) return;
      el=pickTarget(under); if(!el) return;
      px=_ceMX; py=_ceMY; uForEdit=under;
    }
    if(op==='txt'){ closeMenu(); _scAddText(px,py); return; }
    if(op==='edit'){
      // 編集は対象の「文字を持つ要素」を狙う（選択があればそれ／無ければカーソル直下。
      // pickTargetで入れ物まで登ると文字の無い箱を掴んで空欄＝追加のように見えるため）。
      // ★「中に文字があるか」で親をたどると、余白で押したときに大きな入れ物まで登って
      //   その中の関係ない文章が編集対象になる（実報告）。自分の中に直接ある文字だけを見る。
      var _own=function(n){
        if(!n||n.nodeType!==1) return false;
        for(var i=0;i<n.childNodes.length;i++){
          var c=n.childNodes[i];
          if(c.nodeType===3&&(c.nodeValue||'').replace(/[\\s\\u200b]/g,'')) return true;
        }
        return false;
      };
      var te=uForEdit, lim=3;
      while(te&&te!==document.body&&!_own(te)&&lim-->0&&!/^(SECTION|HEADER|FOOTER|MAIN)$/.test(te.tagName)){ te=te.parentElement; }
      var _big=false;
      try{ var _r=te?te.getBoundingClientRect():null;
        _big=!!_r&&(_r.width*_r.height)>(window.innerWidth*window.innerHeight*0.4); }catch(_){}
      if(!te||te===document.body||!_own(te)||_big){
        if(msg) msg.textContent='ここには編集できる文字がありません（直したい文字の上にマウスを置いて押してください）';
        return;
      }
      closeMenu(); openBreakEditor(te); return;
    }
    if(op==='img'){ closeMenu(); openAddImagePicker(px,py); return; }
    if(op==='photo'){
      var imgEl=(el.tagName==='IMG')?el:(el.querySelector?el.querySelector('img'):null);
      var si=secIndexOf(el); closeMenu(); openPhotoDecoPicker(el, imgEl, si); return;
    }
    if(op==='fx'){
      closeMenu(); curEl=el; el.classList.add('__ce_sel'); selEls=[el];
      _bigFull=true; _bigFxFocus=true; _forceEl=el;
      el.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:cx,clientY:cy})); return;
    }
    if(op==='fav'){ scFavMenu(el, cx, cy); return; }
  }
  function scFavMenu(el, cx, cy){
    var old=document.getElementById('__ce_scmenu'); if(old) old.remove();
    var sec=el.closest('section,header,footer')||el;
    var mn=document.createElement('div'); mn.id='__ce_scmenu';
    mn.setAttribute('style','position:fixed;z-index:2147483003;background:#fff;border:1px solid #e2c98a;border-radius:10px;box-shadow:0 12px 34px rgba(0,0,0,.28);padding:6px;font-family:system-ui,sans-serif;min-width:230px');
    var items=[
      ['⭐ このセクションをお気に入り保存', function(){ favSaveSection(sec); }],
      ['🔀 お気に入りから切り替え',        function(){ favSwapOpen(sec); }],
      ['➕ お気に入りから追加（場所を選ぶ）', function(){ var ab=document.getElementById('__ce_favadd'); if(ab) ab.click(); }]
    ];
    items.forEach(function(it){
      var b=document.createElement('div');
      b.setAttribute('style','padding:9px 11px;font-size:13px;color:#7a4f00;cursor:pointer;border-radius:7px;white-space:nowrap');
      b.textContent=it[0];
      b.addEventListener('mouseover',function(){ b.style.background='#fff5e0'; });
      b.addEventListener('mouseout',function(){ b.style.background='none'; });
      b.addEventListener('click',function(){ mn.remove(); it[1](); });
      mn.appendChild(b);
    });
    document.body.appendChild(mn);
    mn.style.left=Math.max(6,Math.min(cx, window.innerWidth-mn.offsetWidth-8))+'px';
    mn.style.top=Math.max(6,Math.min(cy, window.innerHeight-mn.offsetHeight-8))+'px';
    setTimeout(function(){ document.addEventListener('mousedown',function _off(ev){ if(!mn.contains(ev.target)){ mn.remove(); document.removeEventListener('mousedown',_off,true); } },true); },0);
  }
  function scOpenSettings(){
    var old=document.getElementById('__ce_scset'); if(old) old.remove();
    var keys=scKeys();
    var ov=document.createElement('div'); ov.id='__ce_scset';
    ov.setAttribute('style','position:fixed;inset:0;z-index:2147483004;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;font-family:system-ui,sans-serif');
    var rows=SC_META.map(function(x){
      return '<div style="display:flex;align-items:center;gap:10px;padding:8px 4px;border-bottom:1px solid #f0f0f3">'
        +'<span style="flex:1;font-size:13px;color:#1d1d1f">'+esc(x.label)+'</span>'
        +'<input class="__sck" data-op="'+x.op+'" maxlength="1" value="'+esc(keys[x.op]||'')+'" '
        +'style="width:44px;text-align:center;font-size:15px;font-weight:700;padding:6px;border:1px solid #cfd3da;border-radius:7px;text-transform:lowercase"></div>';
    }).join('');
    ov.innerHTML='<div style="background:#fff;border-radius:12px;padding:18px 20px;max-width:460px;width:92%;max-height:82vh;overflow:auto;box-shadow:0 18px 50px rgba(0,0,0,.35)">'
      +'<div style="display:flex;align-items:center;margin-bottom:6px"><b style="font-size:15px">⌨ ショートカットキー</b>'
      +'<span id="__ce_scsx" style="margin-left:auto;cursor:pointer;font-size:18px;color:#888">✕</span></div>'
      +'<div style="font-size:12px;color:#667;margin-bottom:12px">マウスを要素に載せてキーを押すと発火します。空欄にすると無効。1文字だけ（英字推奨）。家↔会社で同期されます。</div>'
      +rows
      +'<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">'
      +'<button id="__ce_screset" style="background:#eef0f4;border:1px solid #d7dae1;border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer">⟲ 初期に戻す</button>'
      +'<button id="__ce_scsave" style="background:#0b6bcb;color:#fff;border:none;border-radius:8px;padding:8px 18px;font-size:13px;font-weight:700;cursor:pointer">✔ 保存</button></div></div>';
    document.body.appendChild(ov);
    function close(){ ov.remove(); }
    ov.querySelector('#__ce_scsx').addEventListener('click',close);
    ov.addEventListener('mousedown',function(e){ if(e.target===ov) close(); });
    ov.querySelector('#__ce_screset').addEventListener('click',function(){
      [].slice.call(ov.querySelectorAll('.__sck')).forEach(function(inp){ inp.value=SC_DEF[inp.getAttribute('data-op')]||''; });
    });
    ov.querySelector('#__ce_scsave').addEventListener('click',function(){
      var m={}, seen={}, dup='';
      [].slice.call(ov.querySelectorAll('.__sck')).forEach(function(inp){
        var v=(inp.value||'').trim().toLowerCase().slice(0,1); m[inp.getAttribute('data-op')]=v;
        if(v){ if(seen[v]) dup=v; seen[v]=1; }
      });
      if(dup){ if(msg) msg.textContent='⚠ キー「'+dup+'」が重複しています（1つの操作にしてください）'; return; }
      scSave(m); close();
      if(msg) msg.textContent='⌨ ショートカットキーを保存しました（家↔会社で同期）';
    });
  }
  window.scOpenSettings=scOpenSettings;
  document.addEventListener('keydown',function(e){
    if(e.ctrlKey||e.metaKey||e.altKey) return;  // 修飾キー付きは対象外（Ctrl+C等と衝突させない）
    var ae=document.activeElement;
    if(ae&&(ae.tagName==='INPUT'||ae.tagName==='TEXTAREA'||ae.isContentEditable)) return;  // 入力中は無効
    if(document.getElementById('__ce_pk')||document.getElementById('__ce_dlyp')||document.getElementById('__ce_shp')||document.getElementById('__ce_scset')||document.getElementById('__ce_scmenu')) return;  // パネル・キー設定・お気に入りメニューを開いている時は無効
    var k=(e.key||'').toLowerCase(); if(k.length!==1) return;
    var keys=scKeys();
    for(var op in keys){ if(keys[op]===k){ e.preventDefault(); scRun(op); return; } }
  });
  document.addEventListener('keydown',function(e){
    if(!(e.ctrlKey||e.metaKey)) return;
    var k=(e.key||'').toLowerCase();
    if(k!=='c'&&k!=='v') return;
    var ae=document.activeElement;
    if(ae&&(ae.tagName==='INPUT'||ae.tagName==='TEXTAREA'||ae.isContentEditable)) return;
    var ts=window.getSelection&&window.getSelection();
    if(ts&&!ts.isCollapsed) return;  // 文字を選択している＝普通のテキストコピー優先
    if(k==='c'){
      // ★図形・線・飾り（🔶図形/⭕リング/▢縁取り/🌸グラデ）は「左クリックで掴む」仕組みなので
      //   curEl に入らず、Ctrl+C が効かなかった（2026-07-30・要望）。
      //   マウス位置にそれらがあれば優先し、無ければ直前に触った図形を使う＝図形全般がコピーできる。
      var src=curEl, hov=null;
      try{
        var us=document.elementsFromPoint(_ceCX,_ceCY);
        for(var ui=0;ui<us.length;ui++){
          var u=us[ui];
          if(!u||!u.closest) continue;
          if(u.closest('[id^="__ce"]')) continue;
          var dq=u.closest(DQ_SEL);
          if(dq){ hov=dq; break; }
        }
      }catch(_){}
      if(hov) src=hov;
      else if((!src||!document.contains(src)) && _lastShape && document.contains(_lastShape)) src=_lastShape;
      if(!src||!document.contains(src)){ if(msg) msg.textContent='先に右クリックでコピーしたい要素を選んでください（図形・線はその上にマウスを置いて Ctrl+C）'; return; }
      var n=src.cloneNode(true);
      n.classList.remove('__ce_sel','__ce_hl','__ce_sechl');
      _ceClip={html:n.outerHTML, tag:src.tagName};
      e.preventDefault();
      if(msg) msg.textContent='📋 コピーしました。貼り付けたい場所にマウスを置いて Ctrl+V';
      return;
    }
    // ★スクショ貼り付け（📋→セクション）と共存させる：内部コピーが無い時は、すぐ文句を言わずに
    //   paste イベント（この直後に来る）が画像を拾えるか待つ。拾えたら _psWait を false にされる。
    if(!_ceClip){
      _psWait=true;
      setTimeout(function(){ if(_psWait){ _psWait=false; if(msg) msg.textContent='先に要素を右クリックで選んで Ctrl+C（スクショなら Ctrl+V でセクションにできます）'; } },400);
      return;
    }
    e.preventDefault();
    var tpl=document.createElement('template'); tpl.innerHTML=_ceClip.html;
    var nd=tpl.content.firstElementChild; if(!nd) return;
    // ドラッグ移動の署名は複製に持ち込まない（貼った位置からさらにズレるのを防ぐ）
    ['data-cetx','data-cety','data-cero','data-cesx','data-cesy'].forEach(function(a){ nd.removeAttribute(a); });
    nd.style.removeProperty('translate');
    // 出現アニメ持ちは編集中すぐ見えるように（保存時にcleanHtmlがfxa_inを外す＝本番は再生に戻る）
    if(nd.classList.contains('fxa_pre')) nd.classList.add('fxa_in');
    [].slice.call(nd.querySelectorAll('.fxa_pre')).forEach(function(x){ x.classList.add('fxa_in'); });
    if(/^(SECTION|HEADER|FOOTER)$/.test(_ceClip.tag)){
      // 丸ごと複製＝元と同じ場所の直後に差し込む（コピー元が消えていたらページ末尾）
      var org=(curEl&&document.contains(curEl)&&curEl.tagName===_ceClip.tag)?curEl:null;
      if(org&&org.parentElement) org.parentElement.insertBefore(nd,org.nextSibling);
      else document.body.appendChild(nd);
      try{ nd.scrollIntoView({block:'center',behavior:'smooth'}); }catch(_){}
    } else {
      placeFree(nd,_ceMX,_ceMY);  // マウス位置のセクションへ%配置＝画面幅に追従
    }
    markDirty();
    if(msg) msg.textContent='📋 貼り付けました（もう一度Ctrl+Vで増やせます）。💾保存で残ります';
  });
  // ===== 📋 スクショを貼り付けて「セクション」にする（2026-07-25） =====
  // ⭐セクション保存はDOMからsectionを掴む方式なので、クローン元の作りが変だと取れないことが多い。
  // その逃げ道＝「見えている通りにスクショを撮って、カンプ上で Ctrl+V」。
  //   ①AIなし＝画像をそのまま1セクションにする（確実・無料・数秒）
  //   ②AIあり＝画像を見てHTML/CSSに作り直す（文字が本物のテキストになる／数十円）
  var _psWait=false;
  document.addEventListener('paste',function(e){
    var ae=document.activeElement;
    if(ae&&(ae.tagName==='INPUT'||ae.tagName==='TEXTAREA'||ae.isContentEditable)) return;  // 入力欄への貼り付けは邪魔しない
    var items=(e.clipboardData&&e.clipboardData.items)?[].slice.call(e.clipboardData.items):[];
    var f=null;
    items.forEach(function(it){ if(!f&&it.kind==='file'&&(it.type||'').indexOf('image/')===0) f=it.getAsFile(); });
    if(!f) return;                       // 画像じゃない＝従来どおり（要素の複製貼り付け等）に任せる
    _psWait=false; e.preventDefault();
    if(msg) msg.textContent='📋 スクショを受け取りました。保存中…';
    var fd=new FormData(); fd.append('image', f, 'paste.png');
    fetch('/api/paste_image',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(d){
      if(!d||!d.ok){ if(msg) msg.textContent='貼り付けた画像の保存に失敗しました'; return; }
      psOpen(d);
    }).catch(function(){ if(msg) msg.textContent='貼り付けた画像の保存に失敗しました（サーバーに届いていません）'; });
  });
  // 貼った直後のパネル：どう使うか（画像のまま／AIで作り直す）を選ばせる
  function psOpen(d){
    var old=document.getElementById('__ce_ps'); if(old) old.remove();
    var ov=document.createElement('div'); ov.id='__ce_ps';
    ov.setAttribute('style','position:fixed;inset:0;z-index:2147483600;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;font-family:system-ui,sans-serif');
    ov.innerHTML='<div style="background:#fff;border-radius:14px;box-shadow:0 18px 50px rgba(0,0,0,.35);padding:16px 18px;width:min(680px,92vw);max-height:88vh;overflow-y:auto">'
      +'<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px"><b style="font-size:15px">📋 このスクショをセクションにします</b>'
      +'<button id="__ce_psx" style="margin-left:auto;background:none;border:none;font-size:18px;color:#888;cursor:pointer">×</button></div>'
      +'<img src="'+esc(d.url)+'" style="display:block;width:100%;border:1px solid #e3e3e8;border-radius:8px;background:#fafafa">'
      +'<div style="font-size:11px;color:#888;margin:4px 0 12px">'+d.w+'×'+d.h+'px</div>'
      +'<button id="__ce_ps_raw" style="display:block;width:100%;background:#1a7f37;color:#fff;border:none;border-radius:9px;padding:11px;font-size:14px;font-weight:700;cursor:pointer">🖼 画像のまま入れる（AIなし・無料・数秒）</button>'
      +'<div style="font-size:11.5px;color:#777;margin:5px 0 14px">見た目は100%そのまま。あとで文字は直せません（位置調整・サイズ変更はできます）</div>'
      +'<input id="__ce_ps_hint" placeholder="AIへの追加指示（任意）例：見出しは「先生の声」にして" style="width:100%;box-sizing:border-box;border:1px solid #d7dae1;border-radius:8px;padding:8px 10px;font-size:13px;font-family:inherit;margin-bottom:6px">'
      +'<button id="__ce_ps_ai" style="display:block;width:100%;background:#0b6bcb;color:#fff;border:none;border-radius:9px;padding:11px;font-size:14px;font-weight:700;cursor:pointer">🤖 コードに作り直す（AI・数十円・20〜60秒）</button>'
      +'<div style="font-size:11.5px;color:#777;margin-top:5px">文字が本物のテキストになるので後から編集できます。写真は貼ったスクショを仮置きします（あとで差し替え）</div>'
      +'<div id="__ce_ps_msg" style="font-size:12px;color:#0b6bcb;margin-top:10px;min-height:16px"></div></div>';
    document.body.appendChild(ov);
    ov.querySelector('#__ce_psx').addEventListener('click',function(){ ov.remove(); });
    ov.addEventListener('click',function(e){ if(e.target===ov) ov.remove(); });
    ov.querySelector('#__ce_ps_raw').addEventListener('click',function(){
      ov.remove();
      psPickPos(function(insert){
        var sec=document.createElement('section');
        sec.setAttribute('data-cepaste','1');
        sec.setAttribute('style','margin:0;padding:0;line-height:0;font-size:0');
        var im=document.createElement('img');
        im.src=d.url; im.alt=''; im.setAttribute('data-cepasteimg','1');
        im.setAttribute('style','display:block;width:100%;height:auto');
        sec.appendChild(im);
        insert(sec);
        if(msg) msg.textContent='🖼 画像のセクションを追加しました。💾保存で確定してください';
      });
    });
    ov.querySelector('#__ce_ps_ai').addEventListener('click',function(){
      var pm=ov.querySelector('#__ce_ps_msg'), btn=ov.querySelector('#__ce_ps_ai');
      var hint=(ov.querySelector('#__ce_ps_hint').value||'').trim();
      btn.disabled=true; btn.style.opacity='.6';
      pm.textContent='🤖 AIがコードに作り直しています…（20〜60秒）';
      fetch('/api/paste_to_section',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({file:d.file,hint:hint})}).then(function(r){return r.json();}).then(function(res){
        btn.disabled=false; btn.style.opacity='1';
        if(!res||!res.ok){ pm.style.color='#c00'; pm.textContent='失敗：'+((res&&res.message)||'不明なエラー'); return; }
        ov.remove();
        psPickPos(function(insert){
          var tpl=document.createElement('template'); tpl.innerHTML=res.html;
          var el=tpl.content.firstElementChild;
          if(!el){ if(msg) msg.textContent='AIの返した中身が空でした（もう一度お試しください）'; return; }
          insert(el);
          if(msg) msg.textContent='🤖 AIがセクションを作りました（'+esc(res.model||'')+'）。💾保存で確定してください';
        });
      }).catch(function(){ btn.disabled=false; btn.style.opacity='1'; pm.style.color='#c00'; pm.textContent='通信に失敗しました'; });
    });
  }
  // どのセクションの下に入れるか選ばせる（➕お気に入り追加と同じ見た目・同じ考え方）
  function psPickPos(done){
    var secs=[].slice.call(document.querySelectorAll('section,header,footer')).filter(function(x){ return !x.closest('#__ce'); });
    if(!secs.length){ if(msg) msg.textContent='入れる目印になるセクションがページにありません'; return; }
    var _sn=0;   // ★番号は<section>だけで数える（ヘッダーを1に含めると「セクション2」から始まって混乱する）
    var rows='<div class="sit-pos" data-pos="-1">▲ 一番上（先頭）に入れる</div>'
      +secs.map(function(s,i){
        var hEl=s.querySelector('h1,h2,h3');
        var tag=(s.tagName==='HEADER')?'🧢ヘッダー':(s.tagName==='FOOTER')?'🦶フッター':('セクション'+(++_sn));
        var lbl=(hEl?hEl.textContent:'').replace(/\\s+/g,' ').trim().slice(0,24);
        return '<div class="sit-pos" data-pos="'+i+'">▼ '+esc(tag)+(lbl?'「'+esc(lbl)+'」':'')+' の下に入れる</div>';
      }).join('');
    var ovp=document.createElement('div'); ovp.id='__ce_pkpos';
    ovp.innerHTML='<div class="bx"><span class="cl" id="__ce_pkposx">×</span><h4>➕ どこに入れますか？</h4><div class="poslist">'+rows+'</div></div>';
    document.body.appendChild(ovp);
    ovp.addEventListener('click',function(e){
      if(e.target.id==='__ce_pkpos'||e.target.id==='__ce_pkposx'){ ovp.remove(); return; }
      var pit=e.target.closest('.sit-pos'); if(!pit) return;
      var pi=Number(pit.getAttribute('data-pos'));
      ovp.remove();
      done(function(newEl){
        if(pi<0){ secs[0].parentElement.insertBefore(newEl,secs[0]); }
        else { var a=secs[pi]; a.parentElement.insertBefore(newEl,a.nextSibling); }
        try{ markRevealed(newEl); }catch(_){}
        markDirty();
        try{ newEl.scrollIntoView({block:'center',behavior:'smooth'}); }catch(_){}
      });
    });
  }
  // ===== 🕊 空飛ぶルート（線を手描き→整えて飛ばす・AIなし・無料） =====
  // 使い方：右クリック→「🕊 線を描いて飛ばす」→キャラの上から線を描く→整え方を選ぶ→
  //         ○アンカーをドラッグで微修正（右クリックで削除）→▶試す→✅付ける→💾保存。
  // 保存はfxaと同じ焼き込み方式：要素に data-fxa-fly(JSONのルート) を刻み、ランタイム(#cefly-run)をHTMLへ注入。
  // data-ce* を削除する生成ページ固有の後処理と衝突しない名前にする。旧 data-cefly はランタイムで自動移行。
  // 座標は「要素の中心からの相対オフセット(px)」で持つ＝画面幅が変わってもルートの形は保たれる。
  // 移動は translate 個別プロパティ（ドラッグ移動と同じ流儀）＝出現アニメのtransformと奪い合わない。
  // ★ランタイム文字列に「アンダースコア2つ+ce」を含めないこと（cleanHtmlがその文字列入りscriptを保存時に消すため）。
  var FLY_RUN='(function(){if(window.ceflyOn)return;window.ceflyOn=true;var d=document;'
    +'[].slice.call(d.querySelectorAll("[data-cefly]")).forEach(function(el){if(!el.getAttribute("data-fxa-fly"))el.setAttribute("data-fxa-fly",el.getAttribute("data-cefly"));el.removeAttribute("data-cefly");});'
    +'function dense(p,m){var out=[],i,j;'
    +'if(m==="s"||p.length<3){for(i=0;i<p.length;i++)out.push([p[i][0],p[i][1]]);}'
    +'else{var P=[p[0]].concat(p,[p[p.length-1]]);'
    +'for(i=1;i<P.length-2;i++){for(j=0;j<32;j++){var t=j/32,t2=t*t,t3=t2*t;'
    +'out.push([0.5*((2*P[i][0])+((P[i+1][0]-P[i-1][0])*t)+((2*P[i-1][0]-5*P[i][0]+4*P[i+1][0]-P[i+2][0])*t2)+((3*P[i][0]-P[i-1][0]-3*P[i+1][0]+P[i+2][0])*t3)),'
    +'0.5*((2*P[i][1])+((P[i+1][1]-P[i-1][1])*t)+((2*P[i-1][1]-5*P[i][1]+4*P[i+1][1]-P[i+2][1])*t2)+((3*P[i][1]-P[i-1][1]-3*P[i+1][1]+P[i+2][1])*t3))]);}}'
    +'out.push([p[p.length-1][0],p[p.length-1][1]]);}'
    +'var L=[0];for(i=1;i<out.length;i++){var dx=out[i][0]-out[i-1][0],dy=out[i][1]-out[i-1][1];L.push(L[i-1]+Math.sqrt(dx*dx+dy*dy));}'
    +'return {p:out,L:L,total:L[L.length-1]||1};}'
    +'function at(pa,dist){var L=pa.L,p=pa.p,i=1;while(i<L.length-1&&L[i]<dist)i++;var seg=L[i]-L[i-1]||1,f=(dist-L[i-1])/seg;'
    +'return {x:p[i-1][0]+(p[i][0]-p[i-1][0])*f,y:p[i-1][1]+(p[i][1]-p[i-1][1])*f,dx:p[i][0]-p[i-1][0],dy:p[i][1]-p[i-1][1]};}'
    +'function fly(el){var cfg;try{cfg=JSON.parse(el.getAttribute("data-fxa-fly"));}catch(e){return;}'
    +'if(!cfg||!cfg.p||cfg.p.length<2)return;'
    +'var bx=+el.getAttribute("data-cetx")||0,by=+el.getAttribute("data-cety")||0,bro=+el.getAttribute("data-cero")||0;'
    +'var bsx=+el.getAttribute("data-cesx")||1,bsy=+el.getAttribute("data-cesy")||1;'
    +'var pa=dense(cfg.p,cfg.m),dur=cfg.d||4000,t0=null,gen=(el.ceflyGen=(el.ceflyGen||0)+1),cur=0,prevFl=false,lastTs=null;'
    +'function step(ts){if(el.ceflyGen!==gen)return;if(el.getAttribute("data-fxa-fly")==null)return;'
    +'if(t0===null)t0=ts;var dt=(lastTs==null)?16:Math.min(64,ts-lastTs);lastTs=ts;'
    +'var p=(ts-t0)/dur,back=false;'
    +'if(cfg.l){p=p%2;if(p>1){p=2-p;back=true;}}else if(p>1)p=1;'
    +'var e=0.5-0.5*Math.cos(Math.PI*p);var d1=e*pa.total,pt=at(pa,d1);'
    +'el.style.setProperty("translate",(bx+pt.x).toFixed(1)+"px "+(by+pt.y).toFixed(1)+"px","important");'
    +'if(cfg.r){var aF=at(pa,Math.min(pa.total,d1+8)),aB=at(pa,Math.max(0,d1-8));'
    +'var vx=back?(aB.x-aF.x):(aF.x-aB.x),vy=back?(aB.y-aF.y):(aF.y-aB.y),th=Math.atan2(vy,vx)*180/Math.PI;'
    +'var st=(cfg.t==null?100:cfg.t)/100,tgt,fl=false;'
    +'if(cfg.f){fl=Math.abs(th)>90;tgt=fl?(180-th):th;if(tgt>180)tgt-=360;}'
    +'else{tgt=Math.atan2(vy,Math.abs(vx))*180/Math.PI;}'
    +'tgt*=st;if(tgt>75)tgt=75;if(tgt<-75)tgt=-75;'
    +'if(!cfg.l&&p>0.85)tgt*=(1-p)/0.15;'
    +'if(fl!==prevFl){cur=tgt;}else{cur+=(tgt-cur)*(1-Math.exp(-dt/160));}'
    +'prevFl=fl;'
    +'if(cfg.f)el.style.setProperty("scale",(fl?-bsx:bsx)+" "+bsy,"important");'
    +'el.style.setProperty("rotate",(bro+(bsx<0?-cur:cur)).toFixed(1)+"deg","important");}'
    +'if(cfg.l||p<1)requestAnimationFrame(step);}'
    +'requestAnimationFrame(step);}'
    +'function start(el){var cd=el.getAttribute("data-cedelay");if(cd!=null&&+cd>0)setTimeout(function(){fly(el);},+cd);else fly(el);}'
    +'var io=("IntersectionObserver" in window)?new IntersectionObserver(function(es){es.forEach(function(en){if(en.isIntersecting){io.unobserve(en.target);start(en.target);}});},{threshold:0}):null;'
    +'window.ceflyArm=function(el){if(el.ceflyObs){start(el);return;}el.ceflyObs=1;if(io)io.observe(el);else start(el);};'
    +'function init(){[].slice.call(d.querySelectorAll("[data-fxa-fly]")).forEach(window.ceflyArm);}'
    +'if(d.readyState==="loading")d.addEventListener("DOMContentLoaded",init);else init();'
    +'})();';
  // ランタイム注入：既にあれば中身だけ最新版に差し替え（再実行はceflyOnガードで1回だけ＝二重再生しない）
  function ensureFlyRun(){
    var old=document.getElementById('cefly-run');
    if(old){ if(old.textContent!==FLY_RUN) old.textContent=FLY_RUN; return; }
    var sc=document.createElement('script'); sc.id='cefly-run'; sc.textContent=FLY_RUN;
    (document.body||document.documentElement).appendChild(sc);
  }
  // 手描き線の間引き（Ramer-Douglas-Peucker）：epsが大きいほどアンカーが減って単純な形になる
  function flyRdp(pts,eps){
    if(pts.length<3) return pts.slice();
    var out=[pts[0]];
    (function seg(a,b){
      var ax=pts[a][0],ay=pts[a][1],bx=pts[b][0],by=pts[b][1];
      var dx=bx-ax,dy=by-ay,len=Math.sqrt(dx*dx+dy*dy)||1,maxD=0,idx=-1;
      for(var i=a+1;i<b;i++){
        var dd=Math.abs((pts[i][0]-ax)*dy-(pts[i][1]-ay)*dx)/len;
        if(dd>maxD){maxD=dd;idx=i;}
      }
      if(maxD>eps){ seg(a,idx); seg(idx,b); } else { out.push(pts[b]); }
    })(0,pts.length-1);
    return out;
  }
  // アンカー列→なめらかな線（ランタイムと同じCatmull-Rom・16分割）。m==='s'なら直線つなぎ
  function flyDense(p,m){
    var out=[],i,j;
    if(m==='s'||p.length<3){ for(i=0;i<p.length;i++) out.push([p[i][0],p[i][1]]); }
    else{
      var P=[p[0]].concat(p,[p[p.length-1]]);
      for(i=1;i<P.length-2;i++){ for(j=0;j<32;j++){ var t=j/32,t2=t*t,t3=t2*t;
        out.push([0.5*((2*P[i][0])+((P[i+1][0]-P[i-1][0])*t)+((2*P[i-1][0]-5*P[i][0]+4*P[i+1][0]-P[i+2][0])*t2)+((3*P[i][0]-P[i-1][0]-3*P[i+1][0]+P[i+2][0])*t3)),
                  0.5*((2*P[i][1])+((P[i+1][1]-P[i-1][1])*t)+((2*P[i-1][1]-5*P[i][1]+4*P[i+1][1]-P[i+2][1])*t2)+((3*P[i][1]-P[i-1][1]-3*P[i+1][1]+P[i+2][1])*t3))]);
      }}
      out.push([p[p.length-1][0],p[p.length-1][1]]);
    }
    var L=[0];
    for(i=1;i<out.length;i++){ var dx=out[i][0]-out[i-1][0],dy=out[i][1]-out[i-1][1]; L.push(L[i-1]+Math.sqrt(dx*dx+dy*dy)); }
    return {p:out,L:L,total:L[L.length-1]||1};
  }
  function flyAt(pa,dist){
    var L=pa.L,p=pa.p,i=1;
    while(i<L.length-1&&L[i]<dist)i++;
    var seg=L[i]-L[i-1]||1,f=(dist-L[i-1])/seg;
    return {x:p[i-1][0]+(p[i][0]-p[i-1][0])*f, y:p[i-1][1]+(p[i][1]-p[i-1][1])*f, dx:p[i][0]-p[i-1][0], dy:p[i][1]-p[i-1][1]};
  }
  var _fly=null;  // 描画モードの状態（el/raw=手描き点列/anchors=整えた後の点列・すべてページ座標）
  function flyRedraw(){
    if(!_fly||!_fly.cv) return;
    var cv=_fly.cv, g=cv.getContext('2d');
    cv.width=window.innerWidth; cv.height=window.innerHeight;  // サイズ設定＝クリアも兼ねる
    var sx=window.scrollX||0, sy=window.scrollY||0;
    try{ var r=_fly.el.getBoundingClientRect();
      g.strokeStyle='rgba(2,132,199,.85)'; g.setLineDash([6,4]); g.lineWidth=2;
      g.strokeRect(r.left,r.top,r.width,r.height); g.setLineDash([]);
    }catch(_){}
    if(_fly.drawing&&_fly.raw.length>1){
      g.strokeStyle='rgba(100,116,139,.8)'; g.lineWidth=2; g.beginPath();
      g.moveTo(_fly.raw[0][0]-sx,_fly.raw[0][1]-sy);
      for(var i=1;i<_fly.raw.length;i++) g.lineTo(_fly.raw[i][0]-sx,_fly.raw[i][1]-sy);
      g.stroke();
    }
    if(_fly.anchors.length>1){
      var pa=flyDense(_fly.anchors,_fly.mode);
      g.strokeStyle='#0284c7'; g.lineWidth=3; g.lineJoin='round'; g.beginPath();
      g.moveTo(pa.p[0][0]-sx,pa.p[0][1]-sy);
      for(var k=1;k<pa.p.length;k++) g.lineTo(pa.p[k][0]-sx,pa.p[k][1]-sy);
      g.stroke();
      for(var a=0;a<_fly.anchors.length;a++){
        var px=_fly.anchors[a][0]-sx, py=_fly.anchors[a][1]-sy;
        g.beginPath(); g.arc(px,py,7,0,Math.PI*2);
        g.fillStyle=(a===0)?'#16a34a':(a===_fly.anchors.length-1)?'#dc2626':'#fff';
        g.fill(); g.strokeStyle='#0284c7'; g.lineWidth=2; g.stroke();
      }
    }
  }
  function flyHitAnchor(px,py){
    for(var i=0;i<_fly.anchors.length;i++){
      var dx=_fly.anchors[i][0]-px, dy=_fly.anchors[i][1]-py;
      if(dx*dx+dy*dy<=144) return i;  // 半径12px以内
    }
    return -1;
  }
  function _flyBtn(id,label,bg){ return '<button id="'+id+'" style="border:none;border-radius:8px;padding:7px 12px;font-weight:700;cursor:pointer;font-size:12.5px;color:#fff;background:'+bg+'">'+label+'</button>'; }
  function _flyTab(k,label){
    var on=(_fly.smooth===k);
    return '<button data-sm="'+k+'" style="border:1px solid #cfe0fb;border-radius:7px;padding:5px 10px;font-size:12px;cursor:pointer;font-weight:700;color:'+(on?'#fff':'#1d1d1f')+';background:'+(on?'#0284c7':'#eef3ff')+'">'+label+'</button>';
  }
  function flyPanelHint(t){
    if(!_fly) return;
    _fly.pn.innerHTML='<div style="font-weight:700;margin-bottom:4px">🕊 空飛ぶルート</div>'
      +'<div style="font-size:12.5px;color:#555;margin-bottom:8px">'+t+'</div>'
      +_flyBtn('__ce_fly_no','✕ やめる（Esc）','#888');
  }
  function flyPanelFull(){
    if(!_fly) return;
    _fly.pn.innerHTML='<div style="font-weight:700;margin-bottom:6px">🕊 空飛ぶルート　<span style="font-weight:400;color:#888;font-size:11px">○をドラッグ＝微調整／○を右クリック＝削除</span></div>'
      +'<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:8px">'
      +'<span style="font-size:12px;color:#555">整え方</span>'
      +_flyTab('raw','✍ そのまま')+_flyTab('smooth','〰 なめらか')+_flyTab('line','📏 直線')
      +'<span style="font-size:12px;color:#555;margin-left:8px">速さ</span>'
      // 速さは対数スケール（0.5〜12秒）：速い側ほどスライダーが細かく効く＝短時間の微調整がしやすい
      +'<input type="range" id="__ce_fly_dur" min="0" max="100" step="1" value="'+Math.round(100*Math.log(_fly.dur/0.5)/Math.log(24))+'" style="width:100px;vertical-align:middle;accent-color:#0284c7">'
      +'<span id="__ce_fly_durv" style="font-size:12px;color:#0369a1;font-weight:700;min-width:38px">'+_fly.dur+'秒</span>'
      +'<label style="font-size:12px;color:#555;margin-left:6px;cursor:pointer"><input type="checkbox" id="__ce_fly_rot"'+(_fly.rot?' checked':'')+'> 進行方向を向く</label>'
      +'<span style="font-size:12px;color:#555">傾き</span>'
      +'<input type="range" id="__ce_fly_tilt" min="0" max="100" step="5" value="'+_fly.tilt+'" style="width:80px;vertical-align:middle;accent-color:#0284c7">'
      +'<span id="__ce_fly_tiltv" style="font-size:12px;color:#0369a1;font-weight:700;min-width:34px">'+_fly.tilt+'%</span>'
      +'<label style="font-size:12px;color:#555;cursor:pointer" title="右向きの絵のキャラ用。左向きの絵ならチェックを外す"><input type="checkbox" id="__ce_fly_flip"'+(_fly.flip?' checked':'')+'> ⇄ 左に進む時は反転</label>'
      +'<label style="font-size:12px;color:#555;cursor:pointer"><input type="checkbox" id="__ce_fly_loop"'+(_fly.loop?' checked':'')+'> 🔁 往復ループ</label>'
      +'</div>'
      +'<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:8px">'
      +'<span style="font-size:12px;color:#555">キャラの向き（最初の姿勢）</span>'
      +'<button id="__ce_fly_rl" style="border:1px solid #cfe0fb;border-radius:7px;padding:5px 10px;font-size:12px;cursor:pointer;font-weight:700;background:#eef3ff;color:#1d1d1f">↺ 左に回す</button>'
      +'<button id="__ce_fly_rr" style="border:1px solid #cfe0fb;border-radius:7px;padding:5px 10px;font-size:12px;cursor:pointer;font-weight:700;background:#eef3ff;color:#1d1d1f">↻ 右に回す</button>'
      +'<button id="__ce_fly_mir" style="border:1px solid #cfe0fb;border-radius:7px;padding:5px 10px;font-size:12px;cursor:pointer;font-weight:700;background:#eef3ff;color:#1d1d1f">⇄ 左右反転</button>'
      +'<span style="font-size:11px;color:#888">飛ぶ時の傾きはこの姿勢が基準（保存で残る）</span>'
      +'</div>'
      +'<div style="display:flex;gap:6px;flex-wrap:wrap">'
      +_flyBtn('__ce_fly_prev','▶ 試す','#0b6bcb')
      +_flyBtn('__ce_fly_redraw','✏ 描き直す','#64748b')
      +_flyBtn('__ce_fly_ok','✅ 付ける（保存で残る）','#1a7f37')
      +_flyBtn('__ce_fly_no','✕ やめる','#888')
      +'</div>';
  }
  // 手ぶれならし（移動平均・端点は動かさない）。passesが多いほどツルツルになる
  function flySmoothRaw(pts,passes){
    var out=pts,k,i;
    for(k=0;k<passes;k++){
      if(out.length<3) break;
      var s=[out[0]];
      for(i=1;i<out.length-1;i++){ s.push([(out[i-1][0]+out[i][0]*2+out[i+1][0])/4,(out[i-1][1]+out[i][1]*2+out[i+1][1])/4]); }
      s.push(out[out.length-1]); out=s;
    }
    return out;
  }
  function flyRefit(){
    if(!_fly||_fly.raw.length<2) return;
    if(_fly.smooth==='raw'){ _fly.anchors=flyRdp(flySmoothRaw(_fly.raw,1),3); _fly.mode='c'; }
    else if(_fly.smooth==='line'){ _fly.anchors=flyRdp(flySmoothRaw(_fly.raw,2),30); _fly.mode='s'; }
    else{
      // なめらか：強めにならしてから、線の長さに比例した間引き＋アンカー最大8個
      // ＝細かい手ぶれは全部消して「大きなうねり」だけ残す（手描きでも自動できれいな弧になる）
      var base=flySmoothRaw(_fly.raw,4), total=0, i;
      for(i=1;i<base.length;i++){ var dx=base[i][0]-base[i-1][0], dy=base[i][1]-base[i-1][1]; total+=Math.sqrt(dx*dx+dy*dy); }
      var eps=Math.max(18, total*0.045);
      var an=flyRdp(base,eps);
      while(an.length>8&&eps<400){ eps*=1.5; an=flyRdp(base,eps); }
      _fly.anchors=an; _fly.mode='c';
    }
    flyRedraw();
  }
  function flyProcess(){
    // 描き始めがキャラの上（少し外もOK）なら、始点をキャラの中心にスナップ＝ズレずにつながる
    var r=_fly.el.getBoundingClientRect(), sx=window.scrollX||0, sy=window.scrollY||0;
    var rl=r.left+sx, rt=r.top+sy, p0=_fly.raw[0];
    if(p0[0]>=rl-14&&p0[0]<=rl+r.width+14&&p0[1]>=rt-14&&p0[1]<=rt+r.height+14){
      _fly.raw[0]=[rl+r.width/2, rt+r.height/2];
    }
    flyRefit(); flyPanelFull();
  }
  // ルートを「要素の中心からの相対オフセット」に変換した焼き込み用データ
  function flyCfg(){
    var el=_fly.el, r=el.getBoundingClientRect();
    var cx=r.left+(window.scrollX||0)+r.width/2, cy=r.top+(window.scrollY||0)+r.height/2;
    return {m:_fly.mode, d:Math.round(_fly.dur*1000), r:_fly.rot?1:0, f:_fly.flip?1:0, t:_fly.tilt, l:_fly.loop?1:0,
      p:_fly.anchors.map(function(a){ return [Math.round(a[0]-cx), Math.round(a[1]-cy)]; })};
  }
  function flyStopPrev(){
    if(_fly&&_fly.el){ _fly.el.ceflyGen=(_fly.el.ceflyGen||0)+1; purgeInlineFx(_fly.el); }
  }
  // キャラ絵そのものの左右向きを反転（data-cesxの符号を反転）＝左向きの絵を右向き基準にできる
  function flyMirror(el){
    _cebt(el);
    el.setAttribute('data-cesx', -((+el.getAttribute('data-cesx'))||1));
    if(el.getAttribute('data-cesy')==null) el.setAttribute('data-cesy', 1);
    applyTf(el);
  }
  // 編集画面でのプレビュー再生（ランタイムと同じ動き・rAF手動描画＝この環境で確実）
  function flyRunLocal(el,cfg){
    var pa=flyDense(cfg.p,cfg.m);
    var bx=+el.getAttribute('data-cetx')||0, by=+el.getAttribute('data-cety')||0, bro=+el.getAttribute('data-cero')||0;
    var bsx=+el.getAttribute('data-cesx')||1, bsy=+el.getAttribute('data-cesy')||1;
    var gen=(el.ceflyGen=(el.ceflyGen||0)+1), t0=null, cur=0, prevFl=false, lastTs=null;
    function step(ts){
      if(el.ceflyGen!==gen) return;
      if(t0===null)t0=ts;
      var dt=(lastTs==null)?16:Math.min(64,ts-lastTs); lastTs=ts;
      var p=(ts-t0)/cfg.d, back=false;
      if(cfg.l){ p=p%2; if(p>1){p=2-p;back=true;} } else if(p>1)p=1;
      var e2=0.5-0.5*Math.cos(Math.PI*p);
      var d1=e2*pa.total, pt=flyAt(pa,d1);
      el.style.setProperty('translate',(bx+pt.x).toFixed(1)+'px '+(by+pt.y).toFixed(1)+'px','important');
      if(cfg.r){
        // 進行方向（往復の戻りは接線を反転）へ「ジワッと」向く：
        // 接線に即スナップだとカーブでバタつく＝不自然だったので、0.16秒の慣性で追従させる。
        // ★接線は折れ線セグメントの向きでなく「前後±8px地点の中央差分」で取る＝区切りごとの段差が消える。
        var aF=flyAt(pa,Math.min(pa.total,d1+8)), aB=flyAt(pa,Math.max(0,d1-8));
        var vx=back?(aB.x-aF.x):(aF.x-aB.x), vy=back?(aB.y-aF.y):(aF.y-aB.y), th=Math.atan2(vy,vx)*180/Math.PI;
        var st=(cfg.t==null?100:cfg.t)/100, tgt, fl=false;
        if(cfg.f){ fl=Math.abs(th)>90; tgt=fl?(180-th):th; if(tgt>180)tgt-=360; }
        else { tgt=Math.atan2(vy,Math.abs(vx))*180/Math.PI; }
        tgt*=st; if(tgt>75)tgt=75; if(tgt<-75)tgt=-75;
        if(!cfg.l&&p>0.85) tgt*=(1-p)/0.15;  // 着地前は水平に戻す（傾いたまま止まらない）
        if(fl!==prevFl){ cur=tgt; }           // 鏡像切替の瞬間はスナップ（回転で補間すると一回転して見える）
        else { cur+=(tgt-cur)*(1-Math.exp(-dt/160)); }
        prevFl=fl;
        if(cfg.f) el.style.setProperty('scale',(fl?-bsx:bsx)+' '+bsy,'important');
        // ⇄でキャラ絵を反転している(bsx<0)場合、scaleが回転の後に掛かるため傾きが鏡写しになる→符号を戻す
        el.style.setProperty('rotate',(bro+(bsx<0?-cur:cur)).toFixed(1)+'deg','important');
      }
      if(cfg.l||p<1){ requestAnimationFrame(step); }
      else{ setTimeout(function(){ if(el.ceflyGen===gen){ purgeInlineFx(el); flyRedraw(); } },450); }
    }
    requestAnimationFrame(step);
  }
  function flyBake(){
    flyStopPrev();
    var el=_fly.el, cfg=flyCfg();
    if(cfg.p.length<2){ if(msg)msg.textContent='⚠ ルートが短すぎます（もう少し長く描いてください）'; return; }
    el.setAttribute('data-fxa-fly', JSON.stringify(cfg));
    el.removeAttribute('data-cefly');
    ensureFlyRun();
    markDirty(); flyEnd();
    if(window.ceflyArm) window.ceflyArm(el);  // その場で1回飛んで見せる（保存後はスクロールで画面に入った時に再生）
    if(msg) msg.textContent='✅ 飛行ルートを付けました（💾変更を保存で残る・開くと画面に入った時に飛びます）';
  }
  function flyDown(e){
    if(e.button!==0||!_fly) return;
    e.preventDefault(); e.stopPropagation();
    var px=e.clientX+(window.scrollX||0), py=e.clientY+(window.scrollY||0);
    if(_fly.anchors.length){ var ai=flyHitAnchor(px,py); if(ai>=0){ _fly.dragIdx=ai; } return; }
    _fly.drawing=true; _fly.raw=[[px,py]]; flyRedraw();
  }
  function flyMove(e){
    if(!_fly) return;
    var px=e.clientX+(window.scrollX||0), py=e.clientY+(window.scrollY||0);
    if(_fly.dragIdx>=0){ _fly.anchors[_fly.dragIdx]=[px,py]; flyRedraw(); return; }
    if(_fly.drawing){
      var lp=_fly.raw[_fly.raw.length-1], dx=px-lp[0], dy=py-lp[1];
      if(dx*dx+dy*dy>9){ _fly.raw.push([px,py]); flyRedraw(); }
    }
  }
  function flyUp(){
    if(!_fly) return;
    if(_fly.dragIdx>=0){ _fly.dragIdx=-1; return; }
    if(_fly.drawing){
      _fly.drawing=false;
      if(_fly.raw.length>=4){ flyProcess(); }
      else{ _fly.raw=[]; flyPanelHint('線が短すぎました。もう一度、マウスをドラッグして線を描いてください'); }
      flyRedraw();
    }
  }
  function flyCtx(e){
    e.preventDefault(); e.stopPropagation();
    if(!_fly||!_fly.anchors.length) return;
    var px=e.clientX+(window.scrollX||0), py=e.clientY+(window.scrollY||0);
    var ai=flyHitAnchor(px,py);
    if(ai>=0&&_fly.anchors.length>2){ _fly.anchors.splice(ai,1); flyRedraw(); }
  }
  function flyKey(e){ if(e.key==='Escape'){ flyStopPrev(); flyEnd(); } }
  function flyEnd(){
    if(!_fly) return;
    document.removeEventListener('mousemove',flyMove,true);
    document.removeEventListener('mouseup',flyUp,true);
    document.removeEventListener('keydown',flyKey,true);
    window.removeEventListener('scroll',flyRedraw,true);
    window.removeEventListener('resize',flyRedraw);
    _fly.cv.remove(); _fly.pn.remove();
    _fly=null; window.__ceFlyMode=false;
  }
  function startFlightDraw(el){
    if(!el){ if(msg)msg.textContent='⚠ 要素が選ばれていません（もう一度右クリックで選んでください）'; return; }
    if(_undraggable(el)){ if(msg)msg.textContent='⚠ ページ全体の器は飛ばせません（キャラの画像など小さめの要素を右クリックしてください）'; return; }
    if(_fly) flyEnd();
    var cv=document.createElement('canvas'); cv.id='__ce_flyov';
    cv.setAttribute('style','position:fixed;left:0;top:0;width:100vw;height:100vh;z-index:2147483004;cursor:crosshair;background:rgba(15,23,42,.10)');
    var pn=document.createElement('div'); pn.id='__ce_flypn';
    pn.setAttribute('style','position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:2147483005;background:#fff;border:1px solid #ddd;border-radius:12px;box-shadow:0 12px 36px rgba(0,0,0,.28);padding:10px 14px;font-family:system-ui,sans-serif;font-size:13px;color:#1d1d1f;max-width:94vw');
    document.body.appendChild(cv); document.body.appendChild(pn);
    _fly={el:el, raw:[], anchors:[], smooth:'smooth', mode:'c', dur:4, rot:true, flip:true, tilt:60, loop:false, drawing:false, dragIdx:-1, cv:cv, pn:pn};
    window.__ceFlyMode=true;
    // パネルの操作（作り直しても効くよう委譲で1回だけ張る）
    pn.addEventListener('click',function(ev){
      var sm=ev.target.closest('[data-sm]');
      if(sm){ flyStopPrev(); _fly.smooth=sm.getAttribute('data-sm'); flyRefit(); flyPanelFull(); return; }
      var b=ev.target.closest('button'); if(!b) return;
      // キャラの向き調整：反転中は回転が鏡写しになるので、ボタンの見た目の向きに合わせて符号を補正
      if(b.id==='__ce_fly_rl'||b.id==='__ce_fly_rr'){
        flyStopPrev();
        var d15=(b.id==='__ce_fly_rr')?15:-15;
        if(((+_fly.el.getAttribute('data-cesx'))||1)<0) d15=-d15;
        rotateBy(_fly.el,d15); return;
      }
      if(b.id==='__ce_fly_mir'){ flyStopPrev(); flyMirror(_fly.el); return; }
      if(b.id==='__ce_fly_prev'){ flyStopPrev(); flyRunLocal(_fly.el, flyCfg()); return; }
      if(b.id==='__ce_fly_redraw'){ flyStopPrev(); _fly.raw=[]; _fly.anchors=[]; flyPanelHint('もう一度、マウスをドラッグして線を描いてください'); flyRedraw(); return; }
      if(b.id==='__ce_fly_ok'){ flyBake(); return; }
      if(b.id==='__ce_fly_no'){ flyStopPrev(); flyEnd(); return; }
    });
    pn.addEventListener('input',function(ev){
      if(ev.target.id==='__ce_fly_dur'){ _fly.dur=Math.round(5*Math.pow(24,ev.target.value/100))/10; var v=document.getElementById('__ce_fly_durv'); if(v)v.textContent=_fly.dur+'秒'; }
      if(ev.target.id==='__ce_fly_rot'){ _fly.rot=!!ev.target.checked; }
      if(ev.target.id==='__ce_fly_flip'){ _fly.flip=!!ev.target.checked; }
      if(ev.target.id==='__ce_fly_tilt'){ _fly.tilt=+ev.target.value; var tv=document.getElementById('__ce_fly_tiltv'); if(tv)tv.textContent=_fly.tilt+'%'; }
      if(ev.target.id==='__ce_fly_loop'){ _fly.loop=!!ev.target.checked; }
    });
    cv.addEventListener('mousedown',flyDown,true);
    cv.addEventListener('contextmenu',flyCtx,true);
    document.addEventListener('mousemove',flyMove,true);
    document.addEventListener('mouseup',flyUp,true);
    document.addEventListener('keydown',flyKey,true);
    window.addEventListener('scroll',flyRedraw,true);
    window.addEventListener('resize',flyRedraw);
    flyPanelHint('マウスをドラッグして、飛ばしたいルートの線を描いてください（キャラの上から描き始めるとつながります）。画面はホイールでスクロールできます');
    flyRedraw();
  }
  // 既に飛行が焼き込まれたカンプを開いたら、古いランタイムを外して最新版を強制再実行
  // （二重再生はceflyGen世代番号で自動解決＝後から動いた最新版だけが生き残る）
  if(document.querySelector('[data-fxa-fly],[data-cefly]')){
    var _ofr=document.getElementById('cefly-run');
    if(_ofr){ _ofr.remove(); window.ceflyOn=false; }
    ensureFlyRun();
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
      sl.oninput=function(e){ var inp2=e.target.closest('input'); if(!inp2) return; var kk=inp2.getAttribute('data-k'); curP[kk]=+inp2.value; if(!_fxLast[curAnim]) _fxLast[curAnim]={}; _fxLast[curAnim][kk]=+inp2.value; _fxSaveLast(); var sd=null; for(var i=0;i<a.sl.length;i++){ if(a.sl[i].k===kk) sd=a.sl[i]; } var lb=inp2.parentNode.querySelector('span'); if(lb) lb.textContent=inp2.value+((sd&&sd.u)||'px'); eachSel(function(x){ playAnim(x,curAnim); }); };
    }
    var ctl=document.getElementById('__fx_ctl'); if(ctl) ctl.style.display='block';
    // 🧩複数選択中は全員で再生（主役だけ動くと「他が動かない」ように見えるため）
    eachSel(function(x){ playAnim(x,k); });
  }
  function resetPos(el){
    el.style.removeProperty('transform'); el.style.removeProperty('transform-origin'); el.style.removeProperty('animation'); el.style.removeProperty('transition');
    el.style.removeProperty('translate'); el.style.removeProperty('rotate'); el.style.removeProperty('scale');  // 個別プロパティ方式の移動も戻す
    if(el.getAttribute('data-cew')!=null){ // 画像サイズを変えていたら、それも元に戻す（元からの幅指定は触らない）
      el.style.removeProperty('width'); el.style.removeProperty('height'); el.style.removeProperty('object-fit'); el.style.removeProperty('max-width');
    }
    el.style.removeProperty('min-height'); el.style.removeProperty('width'); el.style.removeProperty('max-width');  // 縦の余白・横幅の増減も戻す
    // 伸縮ハンドルが横ドラッグ時に固定した高さ・切り取り・flex/grid凍結も戻す
    el.style.removeProperty('height'); el.style.removeProperty('object-fit'); el.style.removeProperty('overflow');
    el.style.removeProperty('flex'); el.style.removeProperty('justify-self');
    el.style.removeProperty('margin-top'); el.style.removeProperty('z-index');  // ⬆食い込み・🔼重なり順も戻す（inline分だけ＝元のCSSは無傷）
    el.removeAttribute('data-cetx'); el.removeAttribute('data-cety'); el.removeAttribute('data-cesx'); el.removeAttribute('data-cesy'); el.removeAttribute('data-cero'); el.removeAttribute('data-cebt');
    el.removeAttribute('data-cew'); el.removeAttribute('data-ceh');
    markDirty();
  }
  // ===== 📌 画面への貼り付き（position:fixed）を解除する（2026-07-20・AIなし） =====
  // 「画面に貼り付いて全ページを付いてくる」要素を、ページと一緒にスクロールする普通の要素に変える。
  // 見た目の位置は変えない（fixedは常に同じ画面位置に出るので、その画面座標＝ページ最上部での位置）。
  // 2通り：①要素そのものが貼り付いている（固定ヘッダー等）②貼り付く器の中に取り残されている（追加した画像等）
  function _stuckAncestor(el){
    var n=el;
    while(n && n!==document.body && n.nodeType===1){
      try{ if(getComputedStyle(n).position==='fixed') return n; }catch(_){ return null; }
      n=n.parentElement;
    }
    return null;
  }
  // 貼り付いている要素そのものを、その場で「普通の要素」に変える（親子関係は動かさない＝元CSSが崩れない）
  function _unfixSelf(el){
    // 貼り付いている＝画面のどこに出るかが常に同じ＝その画面座標がそのまま「ページ最上部での位置」。
    // ★合わせ方は「absoluteにしてみて、ズレた分だけleft/topを直す」差分方式にする。
    //   変形(ドラッグのtranslate・FXのtransform等)は前後で同じだけかかるので差分で打ち消し合い、
    //   二重掛けが起きない（引き算で直そうとすると、CSSクラス由来の変形を消せず二重にずれる＝実際に起きた）。
    var before=el.getBoundingClientRect();
    el.style.setProperty('position','absolute','important');
    var after=el.getBoundingClientRect();
    var cs=getComputedStyle(el);
    var cl=parseFloat(cs.left)||0, ct=parseFloat(cs.top)||0;
    // スクロール量を引くのは「今見えている位置」ではなく「ページ最上部での位置」に置くため
    // （スクロール中に実行しても結果が同じになる）
    el.style.setProperty('left',Math.round(cl+(before.left-after.left-(window.scrollX||0)))+'px','important');
    el.style.setProperty('top', Math.round(ct+(before.top -after.top -(window.scrollY||0)))+'px','important');
    markDirty();
    return true;
  }
  // ツールで後から置いた飾りを、貼り付く器の外へ出す（器の中身ではないので出しても崩れない）
  function _unfixOut(el, stuck){
    var r=el.getBoundingClientRect();
    var sz=parseInt(getComputedStyle(stuck).zIndex,10);
    if(!isNaN(sz) && (parseInt(el.style.zIndex,10)||0)<=sz) el.style.zIndex=String(sz+1);
    var sr=stuck.getBoundingClientRect();
    var moved=(+el.getAttribute('data-cetx')||0)||(+el.getAttribute('data-cety')||0);
    // 器が画面いっぱい・左上ぴったりなら座標系が同じ＝そのまま引っ越すだけで left:22% のような%指定を保てる
    if(!moved && Math.abs(sr.left)<2 && Math.abs(sr.top)<2
       && Math.abs(sr.width-document.documentElement.clientWidth)<3){
      document.body.appendChild(el);
    } else {
      el.style.position='absolute';
      el.style.left=Math.round(r.left)+'px';
      el.style.top=Math.round(r.top)+'px';
      el.style.removeProperty('translate'); el.style.removeProperty('transform');
      el.removeAttribute('data-cetx'); el.removeAttribute('data-cety');
      document.body.appendChild(el);
    }
    markDirty();
    return true;
  }
  // どれを「普通」に変えるか決める（★ここを間違えるとロゴが消える：下の3つ目の分岐が命）
  function _unfixPlan(el){
    var stuck=el&&_stuckAncestor(el);
    if(!stuck) return null;
    if(stuck===el) return {kind:'self', target:el};              // ①自分が貼り付いている
    if(el.__ceFree) return {kind:'out', target:el, host:stuck};  // ②ツールで置いた飾り＝器から出す
    // ③元のサイトの中身（ロゴ・ナビ等）＝器そのものを普通にする。
    //   ★中身だけ器から引きずり出すと、効いていたCSSから外れてページ最下部へ飛ぶ＝「ロゴが消えた」になる
    //   （実際にh1.header-logo__ttlが 0,8647 へ飛んだ）。中身は絶対に動かさない。
    return {kind:'host', target:stuck};
  }
  // ===== 📌 逆：この要素を画面に貼り付ける（スクロールしても付いてくる固定ヘッダー等） =====
  // ★狙い＝「セクション（ヘッダー行など）を、スクロールしても上に残る固定バーにする」。
  //   position:fixed だと元いた場所に穴が空く（🕳と同じ）ので、既定は sticky を使う＝
  //   その要素の高さぶんは今までどおり場所を取りつつ、スクロールで画面の縁に貼り付く。
  //   固定バーは背景が透けると下の文字と重なって読めないので、透明なら薄い白を敷く。
  function pinFixTarget(el){
    // 右クリックした所から外へ辿り、最初に出会う section/header/footer を貼り付け対象にする
    //   （中の小さな要素だけ貼り付けると崩れるため。無ければその要素自身）。
    var t=el;
    for(var i=0;t&&i<6&&t.tagName!=='BODY'&&t.tagName!=='HTML';i++,t=t.parentElement){
      if(/^(HEADER|FOOTER|SECTION|NAV)$/.test(t.tagName)) return t;
    }
    return el;
  }
  // 🧹 焼き込まれてしまった「桁外れの重なり順」を開いた時に直す（2026-07-30）。
  //   既存カンプには 2147480000 が z-index として保存済み＝上に何を置いても裏に潜る。
  //   ツール自身のUI(__ce*)とオープニングの幕(__op_screen)は本当にその値が必要なので触らない。
  function zRepair(){
    var hit=[];
    try{
      [].slice.call(document.querySelectorAll('[style*="z-index"]')).forEach(function(n){
        if(n.id&&(n.id.indexOf('__ce')===0||n.id.indexOf('__op')===0)) return;
        if(n.closest&&(n.closest('[id^="__ce"]')||n.closest('#__op_screen'))) return;
        var v=parseInt(n.style.zIndex,10);
        if(!isNaN(v)&&Math.abs(v)>=1000000) hit.push(n);
      });
    }catch(_){}
    if(!hit.length) return 0;
    var z=_pinZ();
    hit.forEach(function(n){ n.style.setProperty('z-index',String(z),'important'); });
    return hit.length;
  }
  (function(){
    function go(){
      var n=0; try{ n=zRepair(); }catch(_){ }
      if(n&&msg){
        markDirty();
        msg.textContent='🧹 重なり順が桁外れだった '+n+' 箇所を直しました（'+
          '上に画像や文字を乗せても裏に潜らなくなります・💾保存で確定・⟲で戻せます）';
      }
    }
    if(document.readyState==='complete') setTimeout(go,600); else window.addEventListener('load',function(){ setTimeout(go,600); });
  })();
  function pinIsFixed(el){
    var cs; try{ cs=getComputedStyle(el); }catch(_){ return false; }
    return cs.position==='fixed'||cs.position==='sticky'||el.hasAttribute('data-cepin');
  }
  // 📌貼り付け用の重なり順。「ページの中身より前」であれば十分で、大きすぎてはいけない。
  // ★地雷（2026-07-30・実害）：ここは 2147480000（ほぼ最大値）を直書きしていた。
  //   貼り付けたセクションが最強になり、あとから乗せた画像・文字が**必ず裏に潜って掴めない**
  //   （実測：<section class="fv"> が2147480000／乗せた鳥は49で永久に見えない）。
  //   ページ内の実際の最大値を測って、そのすぐ上に置く。
  function _pinZ(){
    var mx=0;
    try{
      [].slice.call(document.querySelectorAll('body *')).slice(0,1500).forEach(function(n){
        if(n.id&&(n.id.indexOf('__ce')===0||n.id.indexOf('__op')===0)) return;
        if(n.closest&&(n.closest('[id^="__ce"]')||n.closest('#__op_screen'))) return;
        var v=parseInt(getComputedStyle(n).zIndex,10);
        if(!isNaN(v)&&v>mx&&v<100000) mx=v;      // 桁外れの値は「壊れた値」なので基準にしない
      });
    }catch(_){}
    return Math.max(100, Math.min(mx+10, 99999));
  }
  function pinFix(el, edge){
    edge=edge||'top';
    var cs; try{ cs=getComputedStyle(el); }catch(_){ cs=null; }
    el.style.setProperty('position','sticky','important');
    el.style.setProperty(edge,'0','important');
    el.style.setProperty(edge==='top'?'bottom':'top','auto','important');
    el.style.setProperty('z-index',String(_pinZ()),'important');   // ページの中身より前・でも常識的な値
    // 背景が透けていると貼り付いた時に下の文字と重なって読めない → 薄い白を保険で敷く
    var bg=cs&&cs.backgroundColor;
    if(!bg || bg==='rgba(0, 0, 0, 0)' || bg==='transparent'){
      el.style.setProperty('background-color','rgba(255,255,255,.96)','important');
      el.setAttribute('data-cepinbg','1');
    }
    el.setAttribute('data-cepin',edge);
    markDirty();
  }
  function pinUnfix(el){
    ['position','top','bottom','z-index'].forEach(function(p){ el.style.removeProperty(p); });
    if(el.getAttribute('data-cepinbg')){ el.style.removeProperty('background-color'); el.removeAttribute('data-cepinbg'); }
    el.removeAttribute('data-cepin');
    markDirty();
  }
  function unfixEl(el){
    var p=_unfixPlan(el);
    if(!p) return false;
    // ★履歴は末尾のmarkDirty()に任せる（このツールの流儀＝「変えてからmarkDirty」で1手前が積まれる）。
    //   ここで先にpushUndoすると同じキーで2回積むことになり、⟲で戻らなくなる（実際に戻らなかった）。
    return (p.kind==='out') ? _unfixOut(p.target,p.host) : _unfixSelf(p.target);
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
  // ===== 📏 整列ガイド線＋吸着（パワポ/Figma風・AIなし） =====
  // ドラッグ中、同セクション内の他要素やセクション自身と「端・中央」が6px以内に近づいたら
  // ピンクのガイド線を表示してピタッと吸着。Shiftを押しながらドラッグで吸着OFF（自由に置ける）。
  var __gd=null,__gdV=null,__gdH=null;
  function _gdCollect(el){
    var sec=(el.closest&&el.closest('section,header,footer'))||document.body;
    var v=[], h=[];
    function addRect(r){ v.push(r.left, r.left+r.width/2, r.right); h.push(r.top, r.top+r.height/2, r.bottom); }
    var sr=sec.getBoundingClientRect(); addRect(sr);
    [].slice.call(sec.querySelectorAll('*')).slice(0,400).forEach(function(n){
      if(n===el||el.contains(n)||n.contains(el)) return;      // 自分と中身・先祖は除外（吸着の自己参照防止）
      if(n.closest('[id^="__ce"]')) return;
      var cs; try{ cs=getComputedStyle(n); }catch(_){ return; }
      if(cs.display==='none'||cs.visibility==='hidden') return;
      var r=n.getBoundingClientRect();
      if(r.width<24||r.height<12) return;                     // 小さすぎる断片は線だらけになるので無視
      addRect(r);
    });
    return {v:v,h:h};
  }
  function _gdLine(ax,pos){
    var el=(ax==='v')?__gdV:__gdH;
    if(pos===null){ if(el) el.style.display='none'; return; }
    if(!el){
      el=document.createElement('div'); el.id='__ce_gd'+ax;
      el.style.cssText='position:fixed;z-index:2147483646;background:#ff2d9b;pointer-events:none;'+(ax==='v'?'width:1px;height:100vh;top:0':'height:1px;width:100vw;left:0');
      document.body.appendChild(el);
      if(ax==='v') __gdV=el; else __gdH=el;
    }
    el.style.display='block';
    if(ax==='v') el.style.left=pos+'px'; else el.style.top=pos+'px';
  }
  function _gdEnd(){ if(__gdV){__gdV.remove();__gdV=null;} if(__gdH){__gdH.remove();__gdH=null;} __gd=null; }
  function _gdSnap(el,tx,ty){
    if(!__gd) return {tx:tx,ty:ty};
    var r=el.getBoundingClientRect();
    var curX=+el.getAttribute('data-cetx')||0, curY=+el.getAttribute('data-cety')||0;
    var dx=tx-curX, dy=ty-curY;
    var eV=[r.left+dx, r.left+r.width/2+dx, r.right+dx];
    var eH=[r.top+dy, r.top+r.height/2+dy, r.bottom+dy];
    var TH=6, bV=null, bH=null;
    __gd.v.forEach(function(g){ eV.forEach(function(x){ var d=g-x; if(Math.abs(d)<=TH&&(bV===null||Math.abs(d)<Math.abs(bV.d))) bV={d:d,p:g}; }); });
    __gd.h.forEach(function(g){ eH.forEach(function(y){ var d=g-y; if(Math.abs(d)<=TH&&(bH===null||Math.abs(d)<Math.abs(bH.d))) bH={d:d,p:g}; }); });
    _gdLine('v', bV?bV.p:null);
    _gdLine('h', bH?bH.p:null);
    return {tx:tx+(bV?bV.d:0), ty:ty+(bH?bH.d:0)};
  }
  function _dDown(e){
    if(e.altKey) return;  // ★地雷：ドラッグモードが固定でONの要素は、Altを押しても文字選択に譲らず飲み込んでしまっていた。ここで先に手放す。
    if(_undraggable(dragEl)){ return; }  // 器は動かさない（保険）
    dActive=true; dSX=e.clientX; dSY=e.clientY;
    dOX=+dragEl.getAttribute('data-cetx')||0; dOY=+dragEl.getAttribute('data-cety')||0;
    __gd=_gdCollect(dragEl);  // 📏 吸着候補（他要素の端・中央）をドラッグ開始時に1回だけ集める
    document.body.style.userSelect='none'; e.preventDefault(); e.stopPropagation();
  }
  document.addEventListener('mousemove',function(e){
    if(!(dActive&&dragEl)) return;
    _asStart(e.clientX, e.clientY);
    var tx=dOX+(e.clientX-dSX), ty=dOY+(e.clientY-dSY);
    if(e.shiftKey){ _gdLine('v',null); _gdLine('h',null); }  // Shift＝吸着せず自由に動かす
    else { var sn=_gdSnap(dragEl,tx,ty); tx=sn.tx; ty=sn.ty; }
    setPos(dragEl, tx, ty);
  },true);
  document.addEventListener('mouseup',function(e){ _asStop(); if(dActive){ dActive=false; document.body.style.userSelect=''; _gdEnd(); pushUndo(dragEl);
    e.stopPropagation(); _hdlDrag=true; setTimeout(function(){ _hdlDrag=false; },150);  // ドラッグ直後のclickでページJSが発火（リンク遷移等）しないようやり過ごす
  } },true);
  // ★普通にクリックしてつかむと、ボタンを押さなくてもその場で即ドラッグできる（既定の動き）。
  //   文字を選んで下線/マーカー/文字色を付けたい時だけ、Altキーを押しながら選ぶ
  //   （Alt無しだとドラッグが割り込むので、Alt有りの時だけ従来通り文字選択に譲る）。
  var _altEl=null, _altActive=false, _aSX=0,_aSY=0,_aOX=0,_aOY=0;
  function _inUI2(node){ if(window.__ceFlyMode||window.__ceInspOn) return true; var el=node&&(node.nodeType===1?node:node.parentElement); return el&&el.closest&&(el.closest('[id^="__ce"]')||el.closest('#__ce_selc')||el.closest('#__ce_toast')||el.closest('.__ce_hdl')||el.closest('#__ce_dlyp')||el.closest('#__ce_shp')||el.closest('#__ce_pskill')||el.closest('#__ce_sbgp')||el.closest('#__ce_scset')||el.closest('#__ce_scmenu')||el.closest('#__ce_bgp')||el.closest('#__ce_ruler')||el.closest('#__ce_grab')); }
  var _aGrp=null;  // 🧩一括移動用：複数選択中に掴んだら、選択全員の開始位置を控えて同じ移動量を足す
  // 🔲 ドラッグ範囲選択（マーキー・2026-07-19）：セクション余白など「ドラッグしても何も起きない場所」から
  // ドラッグすると青い点線枠が出て、枠に完全に入った要素をまとめて複数選択（Ctrl+クリックと同じselEls状態）。
  // 4px未満の動きはただのクリック扱い＝誤発動しない。枠のUIは.__ce_hdl＝保存に紛れない。
  var _mq=null, _mqBox=null;
  function _mqStart(e){
    _mq={x:e.clientX,y:e.clientY,on:false,l:0,t:0,r:0,b:0};
    document.addEventListener('mousemove',_mqMove,true);
    document.addEventListener('mouseup',_mqUp,true);
  }
  function _mqMove(e){
    if(!_mq) return;
    var dx=Math.abs(e.clientX-_mq.x), dy=Math.abs(e.clientY-_mq.y);
    if(!_mq.on){
      if(dx<4&&dy<4) return;
      _mq.on=true;
      if(!_mqBox){
        _mqBox=document.createElement('div'); _mqBox.className='__ce_hdl';
        _mqBox.style.cssText='position:fixed;z-index:2147483000;border:1.5px dashed #0b6bcb;background:rgba(11,107,203,.08);pointer-events:none;display:none';
        document.body.appendChild(_mqBox);
      }
      _mqBox.style.display='block'; document.body.style.userSelect='none';
    }
    e.preventDefault();
    var l=Math.min(_mq.x,e.clientX), t=Math.min(_mq.y,e.clientY);
    _mq.l=l; _mq.t=t; _mq.r=l+dx; _mq.b=t+dy;
    _mqBox.style.left=l+'px'; _mqBox.style.top=t+'px';
    _mqBox.style.width=dx+'px'; _mqBox.style.height=dy+'px';
  }
  function _mqUp(){
    document.removeEventListener('mousemove',_mqMove,true);
    document.removeEventListener('mouseup',_mqUp,true);
    var m=_mq; _mq=null;
    document.body.style.userSelect='';
    if(_mqBox) _mqBox.style.display='none';
    if(!m||!m.on) return;  // 動かしていない＝ただのクリック（何もしない）
    // 枠に「完全に入った」見える要素だけ拾う（交差判定だと巨大な親まで拾って事故るため）
    var inside=[];
    [].slice.call(document.body.querySelectorAll('*')).forEach(function(n){
      if(_inUI2(n)) return;
      if(/^(SECTION|HEADER|FOOTER|MAIN|SCRIPT|STYLE|BR|HR)$/.test(n.tagName)) return;
      if(_undraggable(n)) return;
      var r=n.getBoundingClientRect();
      if(r.width<6||r.height<6) return;
      // ★判定は「要素の箱」でなく「実際に文字が描かれている範囲」で行う（Rangeで実測）。
      //   見出し等はブロック要素で箱が右端まで伸びるため、箱基準だと見た目どおりに囲んでも
      //   選べない（実際に起きた）。文字なし要素（画像・図形）は従来どおり箱で判定
      var rr=r;
      if((n.textContent||'').trim()){
        try{
          var rg=document.createRange(); rg.selectNodeContents(n);
          var tr=rg.getBoundingClientRect();
          if(tr&&tr.width>=2&&tr.height>=2) rr=tr;
        }catch(_){ }
      }
      // 文字要素＝「文字の範囲が枠にほぼ全部入った」時だけ選ぶ（少しのはみ出しは6pxまで許す）。
      //   中心判定だと、囲んだ列の隣まで伸びる長い説明文が巻き込まれる（実際に起きた）。
      // 文字なし要素（画像・図形）＝「箱が全部入った」または「中心が枠内」で選ぶ
      var isText=(rr!==r), pad=isText?6:1;
      var full=(rr.left>=m.l-pad && rr.right<=m.r+pad && rr.top>=m.t-pad && rr.bottom<=m.b+pad);
      var cx=(rr.left+rr.right)/2, cy=(rr.top+rr.bottom)/2;
      var center=(cx>=m.l && cx<=m.r && cy>=m.t && cy<=m.b);
      // ★傾けた文字（アーチ等）は箱が斜めに膨らんで枠からはみ出すため、"全部入った"判定だけだと
      //   囲んだのに選ばれない（実報告：13文字のつもりが5個）。中心が枠内＋6割以上重なっていれば拾う。
      //   （枠の外へ長く伸びる説明文は重なりが小さいので、これまでどおり巻き込まれない）
      var ix=Math.max(0, Math.min(rr.right,m.r)-Math.max(rr.left,m.l));
      var iy=Math.max(0, Math.min(rr.bottom,m.b)-Math.max(rr.top,m.t));
      var cover=(ix*iy)/Math.max(1, rr.width*rr.height);
      if(full || (!isText&&center) || (isText&&center&&cover>=0.6)) inside.push(n);
    });
    // ★「入れ物」は選ばない（2026-07-29）：枠いっぱいに広がる箱を掴むと、中身ではなく器が動いて
    //   ページ全体が斜めにずれる事故になる（実報告）。枠の9割を覆う箱・画面級の箱は候補から外し、
    //   その中身のほうを選ぶ。大きな箱そのものを動かしたい時は右クリック→🖱掴んで動かす。
    var _mqW=Math.max(1,m.r-m.l), _mqH=Math.max(1,m.b-m.t);
    inside=inside.filter(function(n){
      var r=n.getBoundingClientRect();
      if(r.width>=_mqW*0.9 && r.height>=_mqH*0.9) return false;
      if(r.width>=window.innerWidth*0.9 && r.height>=window.innerHeight*0.7) return false;
      return true;
    });
    // 親も枠内なら親だけ残す（1文字span等の破片でなく「まとまり」を選ぶ）
    var set=new Set(inside);
    var picked=inside.filter(function(n){
      var p=n.parentElement;
      while(p&&p!==document.body){ if(set.has(p)) return false; p=p.parentElement; }
      return true;
    });
    // 既存の選択を置き換え
    selEls.forEach(function(x){ x.classList.remove('__ce_sel'); });
    selEls=picked.slice();
    selEls.forEach(function(x){ x.classList.add('__ce_sel'); });
    curEl=selEls.length?selEls[selEls.length-1]:null;
    // ★mouseup直後に発生するclickが「外クリック＝選択解除」を踏んで選択が即消える（実際に起きた）。
    //   既存ドラッグと同じ「やり過ごしガード」(_hdlDrag)で1拍だけclickを無効化する
    _hdlDrag=true; setTimeout(function(){ _hdlDrag=false; },150);
    if(msg) msg.textContent=picked.length
      ?('🧩 '+picked.length+'個を範囲選択しました（そのままドラッグ＝まとめて移動／右クリック＝まとめて操作）')
      :'範囲内に選べる要素がありませんでした';
  }
  // ★Ctrl+クリックの複数選択（2026-07-31）。案内文には昔から「Ctrlを押しながらクリック」と
  //   書いてあったのに、**実装が無かった**（selEls に足す場所が範囲選択と図形設置しか無かった）。
  //   そのため Ctrl+G を押しても「2つ以上を選んでください」で弾かれ、
  //   ユーザーからは「グループ化しても一緒に動かない」に見えていた（実測で確認）。
  document.addEventListener('mousedown',function(e){
    if(!(e.ctrlKey||e.metaKey) || e.altKey || e.shiftKey || e.button!==0) return;
    if(_inUI2(e.target)) return;
    var t=_realTarget(e), el=pickTarget(t);
    if(!el||_undraggable(el)) return;
    e.preventDefault(); e.stopPropagation();
    _hdlDrag=true; setTimeout(function(){ _hdlDrag=false; },150);   // 直後のclickを1拍だけ無効化
    window.__ceCtrlSel=Date.now()+600;                              // この間は closeMenu に選択を消させない
    var i=selEls.indexOf(el);
    if(i>=0){ selEls.splice(i,1); el.classList.remove('__ce_sel'); }  // もう一度Ctrl+クリック＝選択を外す
    else { selEls.push(el); el.classList.add('__ce_sel'); }
    curEl=selEls.length?selEls[selEls.length-1]:null;
    if(msg) msg.textContent=selEls.length
      ?('🧩 '+selEls.length+'個を選択中'+(selEls.length>1?'（Ctrl+G でグループにすると、まとめて動かせます）':'（Ctrlを押しながら他もクリック）'))
      :'選択を解除しました';
  },true);
  // ★Ctrl+クリックの click / mouseup は必ず止める（2026-07-31）。止めないと、この後ろで動く
  //   「どこかをクリックしたらメニューを閉じる」処理が closeMenu() を呼び、selEls が空になる＝
  //   2個目を選んだ瞬間に1個目が外れて、いつまでも2個にならない（実測でここに引っかかった）。
  ['click','mouseup'].forEach(function(ev){
    document.addEventListener(ev,function(e){
      if(!(e.ctrlKey||e.metaKey) || e.altKey || e.shiftKey || e.button!==0) return;
      if(_inUI2(e.target)) return;
      e.preventDefault(); e.stopPropagation();
    },true);
  });
  document.addEventListener('mousedown',function(e){
    if(e.altKey || e.button!==0 || _inUI2(e.target)) return;
    if(e.ctrlKey||e.metaKey) return;   // Ctrl+クリックは上の複数選択が担当＝ドラッグを始めない
    // Shift+ドラッグ＝要素の上からでも範囲選択（行が横幅いっぱいで余白が無いレイアウト用）
    if(e.shiftKey){ _mqStart(e); e.preventDefault(); return; }
    var _mt=_realTarget(e);   // 追加した飾り画像の透明部分から始めた時は下の要素を掴む
    var el=pickTarget(_mt); if(!el||_undraggable(el)){ if(el) _mqStart(e); return; }
    _aGrp=null;
    var _hitSel=null;
    if(selEls.length){
      // 選択中（青点線）の「見た目の範囲内」を掴んだら選択中のものを動かす。
      // DOM的に無関係な要素（文字分割アニメのspan等）が前面にかぶっていても、選択を優先して掴めるように矩形でも判定する
      for(var i=0;i<selEls.length;i++){
        var _sr=selEls[i].getBoundingClientRect();
        if(selEls[i]===el || selEls[i].contains(el) ||
           (e.clientX>=_sr.left && e.clientX<=_sr.right && e.clientY>=_sr.top && e.clientY<=_sr.bottom)){ _hitSel=selEls[i]; break; }
      }
    }
    if(_hitSel){
      el=_hitSel;
      // 複数選択中はその全員／単体でも🧩グループの印があれば仲間ぜんぶを同じ量だけ動かす
      var _gl=(selEls.length>1)?selEls:groupMates(el);
      if(_gl&&_gl.length) _aGrp=_gl.map(function(x){ return {el:x, ox:+x.getAttribute('data-cetx')||0, oy:+x.getAttribute('data-cety')||0}; });
    } else if(el.tagName==='IMG'){
      // 未選択の画像も、ぴったり包む枠があれば枠ごと掴む（右クリックの自動親選択と同じルール）
      var _pw2=el.parentElement;
      if(_pw2 && !/^(SECTION|HEADER|FOOTER|MAIN|BODY|HTML)$/.test(_pw2.tagName) && !_undraggable(_pw2)){
        var _ri2=el.getBoundingClientRect(), _rp2=_pw2.getBoundingClientRect();
        if(_rp2.width<=_ri2.width*1.5+40 && _rp2.height<=_ri2.height*1.5+40) el=_pw2;
      }
    }
    // 選択していない状態でいきなり掴んだ時も、🧩グループの印があれば仲間ぜんぶを動かす
    if(!_aGrp){
      var _gm=groupMates(el);
      if(_gm&&_gm.length) _aGrp=_gm.map(function(x){ return {el:x, ox:+x.getAttribute('data-cetx')||0, oy:+x.getAttribute('data-cety')||0}; });
    }
    // 🧩グループを掴んだことが分かるように知らせる（効いていない時は出ない＝原因の切り分けになる）
    if(_aGrp&&_aGrp.length>1&&msg) msg.textContent='🧩 グループ '+_aGrp.length+'個をまとめて動かします';
    // ★入れ物の「余白」スタート＝範囲選択（2026-07-19）：クリック直下(e.target)が
    //   「子要素2個以上・自分は文字を直接持たない・そこそこ大きい」＝リストやグリッドの背景なら、
    //   入れ物ごと掴まずに範囲選択を始める（Figmaと同じ感覚）。中身（文字・画像）を掴んだ時は従来どおり即ドラッグ。
    //   これが無いと、タイムラインの時刻を囲みたいのに行間から始めるとリスト全体が選択されてしまう（実際に起きた）
    if(!_hitSel){
      var _bt=_mt, _bok=false;
      if(_bt && _bt.nodeType===1 && _bt.children.length>=2 && _bt.tagName!=='IMG'){
        var _btr=_bt.getBoundingClientRect();
        if(_btr.width>=240 && _btr.height>=120){
          _bok=true;
          for(var _bi=0;_bi<_bt.childNodes.length;_bi++){
            var _bc=_bt.childNodes[_bi];
            if(_bc.nodeType===3 && _bc.nodeValue.replace(/\s+/g,'')){ _bok=false; break; }
          }
        }
      }
      if(_bok){ _mqStart(e); return; }
    }
    // ★セクション/ヘッダー/フッター丸ごとは「普通のドラッグ」では動かさない（2026-07-11ガード）。
    //   余白部分を掴んでスクロール/選択したつもりが、セクション全体に translate が付いて保存で焼き込まれ
    //   「全ブロックが約200pxずれたカンプ」が実際にできてしまった。動かしたい時は右クリック→🖱 掴んで動かす。
    if(!_hitSel && /^(SECTION|HEADER|FOOTER|MAIN|BODY|HTML)$/.test(el.tagName)){ _aGrp=null; _mqStart(e); return; }
    // ★ページ丸ごと級の入れ物（クローンの全体ラッパーdiv等）も普通ドラッグ禁止。
    //   これが動くと「body全体が一気に左に寄る」事故になる（実際に発生）。動かしたい時は右クリック→🖱。
    if(!_hitSel){ var _gr=el.getBoundingClientRect(); if(_gr.width>=window.innerWidth*0.95 && _gr.height>=window.innerHeight*1.2){ _aGrp=null; _mqStart(e); return; } }
    _altEl=el; _altActive=true; _aSX=e.clientX; _aSY=e.clientY;
    _aOX=+el.getAttribute('data-cetx')||0; _aOY=+el.getAttribute('data-cety')||0;
    __gd=_gdCollect(el);  // 📏 整列ガイドの吸着候補を集める（普通ドラッグ＝この経路が本命）
    document.body.style.userSelect='none'; e.preventDefault(); e.stopPropagation();
  },true);
  var _aMoved=false;  // 実際に動かしたか（3px超）。動かした時だけ選択を残す＝クリックだけなら従来通り解除
  // ===== ドラッグ中のオートスクロール（2026-07-29）=====
  //   ★これが無いと「画面に収まらない距離」を運べない＝下にあるものを上へ持って行けない（実報告）。
  //   画面の上端/下端に寄せている間だけ自動でスクロールし、掴んだ時の基準も同じだけずらす
  //   （ずらさないとスクロールぶん要素が置いていかれる）。
  var _asRAF=null, _asMX=0, _asMY=0;
  function _asTick(){
    _asRAF=null;
    if(!_altActive && !dActive) return;
    var edge=80, sp=0, y=_asMY;
    if(y<edge) sp=-Math.max(6, Math.round((edge-y)/2));
    else if(y>window.innerHeight-edge) sp=Math.max(6, Math.round((y-(window.innerHeight-edge))/2));
    if(sp){
      var b=window.pageYOffset;
      window.scrollBy(0, sp);
      var moved=window.pageYOffset-b;
      if(moved){
        _aSY-=moved; dSY-=moved;
        if(_altActive&&_altEl){
          var dx=_asMX-_aSX, dy=_asMY-_aSY;
          if(_aGrp) _aGrp.forEach(function(g){ setPos(g.el, g.ox+dx, g.oy+dy); });
          else setPos(_altEl, _aOX+dx, _aOY+dy);
        } else if(dActive&&dragEl){ setPos(dragEl, dOX+(_asMX-dSX), dOY+(_asMY-dSY)); }
      }
    }
    _asRAF=requestAnimationFrame(_asTick);
  }
  function _asStart(x,y){ _asMX=x; _asMY=y; if(!_asRAF) _asRAF=requestAnimationFrame(_asTick); }
  function _asStop(){ if(_asRAF){ cancelAnimationFrame(_asRAF); _asRAF=null; } }
  document.addEventListener('mousemove',function(e){
    if(!_altActive||!_altEl) return;
    _asStart(e.clientX, e.clientY);
    var dx=e.clientX-_aSX, dy=e.clientY-_aSY;
    if(Math.abs(dx)+Math.abs(dy)>3) _aMoved=true;
    var tx=_aOX+dx, ty=_aOY+dy;
    if(e.shiftKey){ _gdLine('v',null); _gdLine('h',null); }  // Shift＝吸着OFFで自由に動かす
    else { var sn=_gdSnap(_altEl,tx,ty); dx+=(sn.tx-tx); dy+=(sn.ty-ty); tx=sn.tx; ty=sn.ty; }
    if(_aGrp){ _aGrp.forEach(function(g){ setPos(g.el, g.ox+dx, g.oy+dy); }); }
    else setPos(_altEl, tx, ty);
  },true);
  document.addEventListener('mouseup',function(){
    _asStop();
    if(_altActive){
      var _pk=_altEl; _altActive=false; document.body.style.userSelect=''; _altEl=null; _gdEnd(); pushUndo(_pk);
      // 動かした直後はmouseup後のclickで選択が閉じてしまうので、やり過ごす（伸縮ハンドルのガードを共用）
      // ＝ドラッグ後も選択が残り、続けて同じ「まとまり」を掴み直せる（Excelと同じ感覚）
      if(_aGrp||_aMoved){ _hdlDrag=true; setTimeout(function(){ _hdlDrag=false; }, 80); }
      _aGrp=null; _aMoved=false;
    }
  },true);
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
    // ★クラスだけの編集UIも本文から除く（2026-07-20・⟲が効かなかった真犯人）。
    //   伸縮ハンドル(div.__ce_hdl)はidが無くbody直下に出るため「本文」と誤認されていた。
    //   右クリックで8個(計2692字)増え、メニューを閉じると消える＝ユーザーは何もしていないのに
    //   本文が変わったと判定され、変更後の状態が余分な履歴として積まれる。
    //   その結果⟲が「今と同じ状態」を復元し、"戻したのに戻らない"になっていた（実測で確認）。
    var c=(n.className||'').toString();
    if(c.indexOf('__ce')>=0) return false;
    if(n.tagName==='SCRIPT'||n.tagName==='STYLE') return false;
    return true;
  }
  function _contentNodes(){ return [].slice.call(document.body.children).filter(_isContent); }
  function snapContent(){ return _contentNodes().map(function(n){ return n.outerHTML; }).join(''); }
  // ★履歴の「変化あり」判定に使うノイズ除去（2026-07-20）＝⟲が効かなかった原因への対策。
  //   スナップショットは本文のHTML丸ごとなので、ユーザーが何もしていなくても中身が変わる：
  //     ①__ce_sel … 選択中の青点線。メニューを閉じるだけで付け外しされる
  //     ②出現アニメの表示クラス … スクロールしただけで監視(IntersectionObserver)が勝手に付ける
  //   これを「変化」と数えると、変更していないのに履歴が積まれ、⟲がその偽の履歴＝
  //   今と同じ状態を復元して「戻したのに何も変わらない」になる（実測で再現）。
  //   ★消すのは比較のときだけ。保存する中身は忠実なまま（クラス名を消すと見た目が変わるため）。
  var _SNAP_NOISE=/\s*\b(__ce_sel|fxa_in|aos-animate|is-visible|is-inview|is-show|reveal-show|revealed|inview|in-view|animated)\b/g;
  function _snapKey(s){ return String(s||'').replace(_SNAP_NOISE,''); }
  function updUndoBtn(){ var u=document.getElementById('__ce_undo'); if(u) u.style.opacity=_undoStack.length?'1':'.4'; }
  // 変更後に呼ぶ：直前の状態(_lastSnap)を積んで、現在を新しい基準にする（実質変化なしなら積まない）
  // key（対象要素）付きで呼ぶと、同じ要素への連続操作（20秒以内）は1つの履歴にまとまる
  // ＝ちょい動かし10回でも⟲1回で操作前まで戻る（「数センチずつしか戻らない」対策）
  var _lastPushKey=null,_lastPushT=0;
  var _groupOpen=false;  // 今の編集グループの「直前の状態」を既に積んだか（1操作1履歴にするための目印）
  function pushUndo(key){
    var cur=snapContent();
    if(cur===_lastSnap) return;
    // 見た目に関係ない差（選択の青点線・出現アニメの表示クラス）だけなら履歴にしない。
    // ＝基準だけ更新して抜ける。これをしないと偽の履歴が積まれ⟲が「今と同じ状態」を復元してしまう
    if(_snapKey(cur)===_snapKey(_lastSnap)){ _lastSnap=cur; return; }
    var now=Date.now();
    // 同じ要素をいじり続けている間は何分たっても1履歴（別の要素に移った時点で区切り）
    // ＝「ちょい動かし20回→⟲1回で操作前へ」。旧仕様の20秒制限は「少しずつしか戻らない」の犯人だった
    // ★履歴の積み方（2026-07-20に作り直し。⟲が効かない原因はここだった）
    //   ルールは2つだけ：
    //     ①その編集の「直前の状態」は必ず1回積む（積む前に基準だけ進めると永久に戻れなくなる）
    //     ②同じ編集の続き（同じ対象・対象不明の追い呼び出し）は積まずに基準だけ進める＝1操作1履歴
    //   以前は key が null（closeMenu後は curEl=null）だと「別の対象＝新しい履歴」と誤判定し、
    //   **変更後の状態**を余分に積んでいた。その結果⟲が今と同じ状態を復元し
    //   「戻したのに戻らない」になっていた（⟲2回押すと戻ることで確認）。
    var newGroup=key && _lastPushKey && key!==_lastPushKey;   // 別の対象に移った＝新しい履歴
    var mustPush=(!_groupOpen)||newGroup;                     // まだ直前の状態を積んでいない or 対象が変わった
    var same=!mustPush;
    if(_lastSnap!==null&&!same){ _undoStack.push(_lastSnap); if(_undoStack.length>25) _undoStack.shift(); _groupOpen=true; }
    _lastSnap=cur; if(key) _lastPushKey=key; _lastPushT=now; updUndoBtn();
  }
  function _restoreContent(html){
    _contentNodes().forEach(function(n){ n.remove(); });
    var tpl=document.createElement('template'); tpl.innerHTML=html;
    document.body.insertBefore(tpl.content, document.body.firstChild);
    // 復元で opacity:0 等のまま隠れる本文が出ないよう強制表示（保険・_SERVE_SAFETYと同じ考え）
    [].slice.call(document.body.querySelectorAll('*')).forEach(function(e){
      if(e.id && e.id.indexOf('__ce')===0) return;
      // ローダー/プリローダーの幕は「隠れているのが正常」＝強制表示しない（緑パズルが復活する事故防止）
      if(e.closest&&e.closest('[class*="loader"],[class*="loading"],[class*="preload"],[class*="splash"]')) return;
      var cs; try{ cs=getComputedStyle(e); }catch(_){ return; }
      if(parseFloat(cs.opacity)===0){ e.style.setProperty('opacity','1','important'); e.style.transform='none'; }
      if(cs.visibility==='hidden'){ e.style.setProperty('visibility','visible','important'); }
    });
    // 復元でDOMが作り直される＝「ツールで置いた飾り」の目印(__ceFree)が消えるので付け直す
    // （これが無いと復元後の📌が「器から出す」ではなく別の動きになる・透明部分の素通りも効かなくなる）
    try{ _scanPlaced(); }catch(_){}
  }
  function undoStep(){
    if(!_undoStack.length){ msg.textContent='これ以上戻せません'; return; }
    closeMenu();
    if(dragEl){ dragEl=null; dActive=false; }  // 復元で対象要素が入れ替わるため掴み状態は解除
    // ★「戻したつもりで戻っていない」を見逃さない（2026-07-20）：
    //   原因（余分な履歴）は上のpushUndoで直したが、成功表示だけは実際に変化を確かめてから出す。
    //   履歴は捨てない＝1回押したら1つだけ戻る（勝手に複数戻ると別の事故になる）。
    var before=_snapKey(snapContent());
    _restoreContent(_undoStack.pop());
    var moved=(_snapKey(snapContent())!==before);
    _lastSnap=snapContent(); _lastPushKey=null; _groupOpen=false;  // 戻した後の操作は新しい履歴として積む
    updUndoBtn();
    if(!moved){ msg.textContent='⟲ 戻せる変更が見つかりませんでした（この操作は履歴に残っていないようです）'; return; }
    _dirty=true; var b=document.getElementById('__ce_save'); if(b){ b.textContent='💾 変更を保存'; b.classList.add('saved'); }
    msg.textContent='ひとつ前に戻しました（さらに戻せます／保存で確定）';
  }
  // Ctrl+Z＝⟲ボタンと同じ「ひとつ戻す」／Shift+Ctrl+Z＝5手まとめて戻す（2026-07-20）。
  // 文字入力中だけはブラウザ標準のundoに任せる。
  document.addEventListener('keydown',function(e){
    if(!(e.ctrlKey||e.metaKey)||e.altKey) return;
    if((e.key||'').toLowerCase()!=='z') return;
    var ae=document.activeElement;
    if(ae&&(ae.isContentEditable||ae.tagName==='INPUT'||ae.tagName==='TEXTAREA')){
      // 標準undoは編集UIの断片（掴みハンドル等の小さいボタン）まで復活させることがある→直後に掃除
      setTimeout(function(){ [].slice.call(document.querySelectorAll('.__ce_hdl,#__ce_selc,.__ce_ipui')).forEach(function(n){ n.remove(); }); },0);
      return;
    }
    e.preventDefault(); e.stopPropagation();
    if(e.shiftKey){ for(var i=0;i<5&&_undoStack.length;i++) undoStep(); }
    else undoStep();
  },true);
  // 🔗 編集中はリンクに飛ばない（誤クリックでカンプから離脱して編集が迷子になるのを防ぐ・2026-07-20）
  // ★ページ自前のJSがdocumentのcaptureで先にクリックを拾う作り（クローン系）に順序で負けないよう、
  //   ①windowのcapture（一番先に走る）＋②押した瞬間にhrefを一時無効化、の二段構え。
  var _lkT=null;
  window.addEventListener('pointerdown',function(e){
    var a=e.target&&e.target.closest&&e.target.closest('a[href]');
    if(!a||_inUI2(a)) return;
    if((a.getAttribute('href')||'')==='javascript:void(0)') return;
    a.setAttribute('data-cehref', a.getAttribute('href'));
    a.setAttribute('href','javascript:void(0)');  // 離した後600msで元に戻す＝ホバー装飾等は保たれる
    clearTimeout(_lkT);
    _lkT=setTimeout(function(){ [].slice.call(document.querySelectorAll('a[data-cehref]')).forEach(function(x){ x.setAttribute('href', x.getAttribute('data-cehref')); x.removeAttribute('data-cehref'); }); },600);
  },true);
  window.addEventListener('click',function(e){
    if(e.target&&e.target.closest&&(e.target.closest('[id^="__ce"]')||e.target.closest('.__ce_hdl'))) return;  // ツールUIは素通し
    // ドラッグで動かした直後のclickは丸ごと無効化＝ページ自前のJS（カードクリックで遷移等）も発火させない
    if(_hdlDrag){ e.preventDefault(); e.stopPropagation(); return; }
    var a=e.target.closest&&e.target.closest('a[href]');
    if(a){
      e.preventDefault();
      if(msg) msg.textContent='🔗 編集中はリンクに飛びません（リンク先: '+(a.getAttribute('data-cehref')||a.getAttribute('href')||'')+'）';
      return;
    }
    // onclick属性でページ遷移する作り（location/href/window.open入り）もブロック
    var oc=e.target.closest&&e.target.closest('[onclick]');
    if(oc&&/location|\\bhref\\b|window\\.open/.test(oc.getAttribute('onclick')||'')){
      e.preventDefault(); e.stopPropagation();
      if(msg) msg.textContent='🔗 編集中はページ移動しません（onclick遷移をブロック）';
    }
  },true);
  // 🔗 リンク/画像のブラウザ標準ドラッグ（リンクを掴んで運ぶ・ゴースト画像）を編集中は止める
  // ＝<a>の行やリンク付きカードも、普通の要素と同じようにドラッグ移動できるようになる
  document.addEventListener('dragstart',function(e){
    if(e.target&&e.target.closest&&(e.target.closest('[id^="__ce"]'))) return;
    e.preventDefault();
  },true);
  // 位置/大きさを変えたら、ヘッダの保存ボタンを「💾 変更を保存」に変えて緑で目立たせる（ボタンは1つに統一）
  var _dirty=false;
  function markDirty(){
    _dirty=true;
    var b=document.getElementById('__ce_save');
    if(b){ b.textContent='💾 変更を保存'; b.classList.add('saved'); }
    if(!dActive) pushUndo(dragEl||curEl);  // ドラッグ中は積まず、離した時(mouseup)に1回だけ積む。矢印ナッジ等の連打は同一要素なら1履歴
  }
  function cleanHtml(){
    // ★保存前に：画面に貼り付く器（position:fixed）の中に取り残された追加物を救出（2026-07-20）。
    // 固定ヘッダーはページ座標での上端が常に今のスクロール位置＝どこをクリックしても「ここに入る」と
    // 判定されるため、置き場所を決める処理がそこを選んでしまっていた（今は選ばないよう修正済み）。
    // その中に入った要素はページと一緒にスクロールせず全ページに居座るので、body直下へ出す。
    // ＝当時のカンプも「開いて💾保存し直すだけ」で直る。位置は今の見た目のまま（ページ最上部基準）。
    (function(){
      [].slice.call(document.querySelectorAll('body>*,header>*,section>*,footer>*')).forEach(function(el){
        if(el.id && el.id.indexOf('__')===0) return;
        if(el.tagName==='SCRIPT'||el.tagName==='STYLE') return;
        var st=el.style;
        if(st.position!=='absolute' || !st.left || !st.zIndex) return;   // ツールが置いた要素の目印(3点セット)
        // 画面に貼り付く親（fixed）の中にいるか。stickyは大半の範囲で一緒に流れるので触らない
        var stuck=null, p=el.parentElement;
        while(p && p!==document.body){
          if(getComputedStyle(p).position==='fixed'){ stuck=p; break; }
          p=p.parentElement;
        }
        if(!stuck) return;
        // 元は器（z-index:1000等）の中にいて前面だったので、外へ出しても重なり順が変わらないよう持ち上げる
        var sz=parseInt(getComputedStyle(stuck).zIndex,10);
        if(!isNaN(sz) && (parseInt(st.zIndex,10)||0)<=sz) st.zIndex=String(sz+1);
        // 器が「画面いっぱい・左上ぴったり」なら、body直下と座標系が同じ＝left/topはそのまま通用する。
        // この道なら left:22% のような%指定を%のまま残せる（px化すると画面幅に追従しなくなる）。
        var sr=stuck.getBoundingClientRect();
        var moved=(+el.getAttribute('data-cetx')||0)||(+el.getAttribute('data-cety')||0)||st.translate||st.transform;
        if(!moved && Math.abs(sr.left)<2 && Math.abs(sr.top)<2
           && Math.abs(sr.width-document.documentElement.clientWidth)<3){
          document.body.appendChild(el); return;                         // そのまま引っ越すだけ＝見た目も指定も無傷
        }
        // それ以外は実測値で置き直す。固定要素の中なので画面座標＝ページ最上部での位置。
        // ドラッグ分(translate)は測定値に含まれるのでここで織り込み、data-cetx側は0に戻す（残すと二重にずれる）。
        var r=el.getBoundingClientRect();
        if(!r.width && !r.height) return;
        st.left=Math.round(r.left)+'px';
        st.top=Math.round(r.top)+'px';
        st.removeProperty('translate'); st.removeProperty('transform');
        el.removeAttribute('data-cetx'); el.removeAttribute('data-cety');
        document.body.appendChild(el);   // 直後の引っ越し処理がセクション相対%へ変換する
      });
    })();
    // 保存前に：bodyへ固定px(left:473px等)で置かれた旧方式の追加物（文字/画像）を、その場所の
    // セクション相対（left%）へ自動で引っ越し＝古いカンプも保存し直すだけで画面幅に追従するようになる。
    // ★座標計算は生DOMでしかできないので、クローンを取る前にここでやる（見た目は変わらない）。
    (function(){
      [].slice.call(document.body.children).forEach(function(el){
        if(el.id && el.id.indexOf('__')===0) return;                       // 編集UI・オープニング幕は対象外
        if(el.tagName==='SCRIPT'||el.tagName==='STYLE') return;
        var st=el.style;
        if(st.position!=='absolute') return;
        if(!/px$/.test(st.left||'') || !/px$/.test(st.top||'')) return;    // px直置きの旧方式だけ
        var x=parseFloat(st.left), y=parseFloat(st.top), host=null;
        // ドラッグで動かしたぶん(translate)も実位置に織り込む（残すと固定pxのままで、狭い画面で再びはみ出す）
        x+=(+el.getAttribute('data-cetx')||0); y+=(+el.getAttribute('data-cety')||0);
        [].slice.call(document.querySelectorAll('header,section,footer')).some(function(s){
          if(s.closest('#__ce')) return false;
          var r=s.getBoundingClientRect(), top=r.top+(window.scrollY||0);
          if(y>=top && y<=top+r.height){ host=s; return true; }
          return false;
        });
        if(!host) return;                                                  // どのセクションにも属さない＝そのまま
        var r=host.getBoundingClientRect(), hx=r.left+(window.scrollX||0), hy=r.top+(window.scrollY||0);
        if(getComputedStyle(host).position==='static') host.style.position='relative';
        // 左%は「要素の幅を引いた最大値」まで＝右端で切れて見えなくなるのを防ぐ
        var maxPct=Math.max(0,(1-Math.min(el.offsetWidth||0,r.width*0.96)/r.width)*100);
        var pct=Math.max(0,Math.min(maxPct,(x-hx)/r.width*100));
        st.left=pct.toFixed(1)+'%';
        st.top=Math.round(y-hy)+'px';
        // どの画面幅でも右にはみ出さない形へ：画像＝幅も%（画面と一緒に伸び縮み）／
        // 文字＝折り返し解禁＋「置いた位置から右端まで」のmax-width
        if(el.tagName==='IMG'){ st.width=Math.min(96,(el.offsetWidth||260)/r.width*100).toFixed(1)+'%'; st.height='auto'; }
        else { st.whiteSpace='normal'; st.maxWidth=Math.max(10,99-pct).toFixed(1)+'%'; }
        el.removeAttribute('data-cetx'); el.removeAttribute('data-cety');  // 織り込み済み＝移動オフセットは0に戻す
        st.removeProperty('translate');
        host.appendChild(el);
      });
    })();
    var doc=document.documentElement.cloneNode(true);
    ['#__ce','#__ce_cm','#__ce_pk','#__ce_toast','#__ce_savebar','#__ce_selc','.__ce_hdl','#__ce_flyov','#__ce_flypn','#__ce_dlyp','#__ce_shp','#__ce_secout','.__ce_ipui','#__ce_pskill','#__ce_sbgp','#__ce_scset','#__ce_scmenu','#__ce_tbgp','#__ce_vlp','#__ce_dqp','#__ce_secp','#__ce_pkpos','#__ce_bgp','#__ce_ruler','#__ce_grab','#__ce_noanimcss','#__ce_opbar'].forEach(function(sel){
      [].slice.call(doc.querySelectorAll(sel)).forEach(function(n){n.remove();});
    });
    // 飾りを選択中の青い点線（編集用の目印）が焼き込まれないように必ず外す
    [].slice.call(doc.querySelectorAll('.ce_bgdeco,.ce_ringdeco,.ce_outlinedeco,[data-celine]')).forEach(function(n){ n.style.removeProperty('outline'); });
    // ★保険：↑の一覧に書き足し忘れたパネルを開いたまま💾保存すると、パネルがHTMLに焼き込まれて
    //   「開くたびに出るのに閉じられない板」になる（実例：🖌文字の背景パレット __ce_tbgp）。
    //   id が __ce で始まる要素はツールのUIだけなので、丸ごと掃除する。
    //   ただし <style> は例外＝保存した見た目を支えるCSS（__ce_ringcss の回転リング等）を消さない。
    [].slice.call(doc.querySelectorAll('[id^="__ce"]')).forEach(function(n){
      if(/^(STYLE|LINK|SCRIPT)$/.test(n.tagName)) return;
      n.remove();
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
    // 🧯 保険の「強制表示」(data-cesafe)を剥がす（2026-07-29）。
    //   カンプ内の保険は2.5秒後に「透明のままの要素」をopacity:1!important＋animation:noneで見せる。
    //   これが保存で焼き込まれると、その要素は次に開いた時から永久に動かない（保存のたびに増える）。
    //   開き直せば保険がまた効く＝見た目は変わらないので、保存版からは必ず落とす。
    //   印が付く前に保存されたぶんは、保険が書く3点セット（opacity:1!important＋animation-name:none）
    //   で見分ける。デザインの意図では有り得ない組み合わせなので誤爆しない。
    (function(){
      var list=[].slice.call(doc.querySelectorAll('[data-cesafe],[style*="opacity"]'));
      list.forEach(function(n){
        if(!n.style) return;
        if(n.classList&&n.classList.contains('fxa_pre')) return;     // ツールで付けた動きは触らない
        var marked=n.hasAttribute&&n.hasAttribute('data-cesafe');
        var sig=(n.style.getPropertyValue('opacity')==='1'
                 && n.style.getPropertyPriority('opacity')==='important'
                 && n.style.animationName==='none');
        if(!marked&&!sig) return;
        ['opacity','animation','visibility'].forEach(function(p){ n.style.removeProperty(p); });
        if(n.style.getPropertyValue('transform')==='none') n.style.removeProperty('transform');
        n.removeAttribute('data-cesafe');
      });
    })();
    // カンプ内の保険スクリプトがスクロール時に付ける「見せるクラス」16種も外す（保険は開き直せばまた動く）。
    // ★焼き込まれると (1)開き直しても出現アニメが「最初から表示済み」になる
    //   (2)ページCSSに .inview{opacity:1!important} 等があると、あとから付けたfxaの動きが永久に効かない
    //   （実際に起きた：3D回転を付けても動かないカンプの犯人）。--hlw:100の焼き込み事故と同じ家系。
    (function(){
      var SHOW=['in','show','is-visible','active','visible','in-view','inview','animated','revealed','aos-animate','is-inview','is-show','reveal-show','show-up','on','enter'];
      var SEL='[class*="reveal"],[class*="fade"],[class*="animate"],[class*="inview"],[class*="in-view"],[class*="stagger"],[class*="slide"],[class*="appear"],[data-reveal]';
      [].slice.call(doc.querySelectorAll(SEL)).forEach(function(n){ if(n.classList) SHOW.forEach(function(k){ n.classList.remove(k); }); });
    })();
    // 🧩 ローダー/プリローダーの幕（2026-07-19）：保険スクリプトの「強制表示」が焼き込まれると、
    //   本来すぐ消える読み込み幕がページ全体を覆ったまま保存される（実際に起きた：緑パズルの
    //   ローダーだけが見える白紙カンプ）。「hidden系クラスで隠す設計」のローダーから強制表示の
    //   inline styleを外し、CSS本来の「隠れた状態」に戻す。
    (function(){
      var HIDDENC=['is-hidden','hidden','is-loaded','loaded','done','is-done','hide'];
      [].slice.call(doc.querySelectorAll('[class*="loader"],[class*="loading"],[class*="preload"],[class*="splash"]')).forEach(function(n){
        if(!n.classList) return;
        var hid=HIDDENC.some(function(k){ return n.classList.contains(k); });
        if(!hid) return;
        ['opacity','visibility','display','transform'].forEach(function(p){ n.style.removeProperty(p); });
        [].slice.call(n.querySelectorAll('*')).forEach(function(c){ if(c.style){ ['opacity','visibility','display'].forEach(function(p){ c.style.removeProperty(p); }); } });
      });
    })();
    // 🖍マーカーは--hlw(0〜100)で伸び具合を持っているので、fxa_inを外すだけでは戻らない。
    // ★これを忘れると「再生し終わった状態(--hlw:100)」がそのまま保存され、次に開いた時に
    //   アニメせず最初から引かれた状態になってしまう（実際に起きたバグ）。必ず0に戻す。
    [].slice.call(doc.querySelectorAll('.fxa_hl,.fxa_ud')).forEach(function(n){ n.style.setProperty('--hlw',0); });
    // 🖼 スライドショー：保存の瞬間に何枚目が表示中でも、保存版は必ず1枚目から始める
    // （--hlw焼き込み事故と同じ家系の予防。スクリプトが動かない場所＝サムネ等でも1枚目が見える）
    [].slice.call(doc.querySelectorAll('[data-slshow]')).forEach(function(w){
      [].slice.call(w.querySelectorAll('img')).forEach(function(im,i){ im.style.setProperty('opacity', i===0?'1':'0', 'important'); });
    });
    // 🕊 飛行アニメ：飛んでる途中のtranslate/rotateが保存に焼き付かないよう、確定位置（data-cetx等）へ戻す。
    // ★これを忘れるとマーカーの--hlwと同じ事故（飛び終わった位置で固まった状態が保存される）が起きる。
    [].slice.call(doc.querySelectorAll('[data-fxa-fly],[data-cefly]')).forEach(function(n){
      if(n.getAttribute('data-cefly')!=null&&n.getAttribute('data-fxa-fly')==null) n.setAttribute('data-fxa-fly',n.getAttribute('data-cefly'));
      n.removeAttribute('data-cefly');
      if(n.getAttribute('data-cetx')!=null||n.getAttribute('data-cety')!=null){ n.style.setProperty('translate',((+n.getAttribute('data-cetx'))||0)+'px '+((+n.getAttribute('data-cety'))||0)+'px','important'); }
      else{ n.style.removeProperty('translate'); }
      if(n.getAttribute('data-cero')!=null){ n.style.setProperty('rotate',((+n.getAttribute('data-cero'))||0)+'deg','important'); }
      else{ n.style.removeProperty('rotate'); }
      // ⇄反転で付いたscaleも確定値へ戻す（左向きで止まった状態が保存で固まらないように）
      if(n.getAttribute('data-cesx')!=null||n.getAttribute('data-cesy')!=null){ n.style.setProperty('scale',((+n.getAttribute('data-cesx'))||1)+' '+((+n.getAttribute('data-cesy'))||1),'important'); }
      else{ n.style.removeProperty('scale'); }
    });
    // ドラッグモード中だけの目印(cursor:move)は編集中の一時状態。保存に残ると次に開いた時も
    // 十字カーソルのままになり、しかもAltを押しても文字選択に譲らず固まって見える不具合の元になる。
    [].slice.call(doc.querySelectorAll('[style*="cursor: move"],[style*="cursor:move"]')).forEach(function(n){ n.style.removeProperty('cursor'); });
    // オープニングの幕：編集用に「止めて表示」していた状態(data-paused/インライン)を解除＝保存版は開いた時に自動再生に戻す
    var _op=doc.querySelector('#__op_screen');
    // ★display は消してはいけない：幕の中央そろえは display:flex で成り立っているので、
    //   消すと block に落ちてロゴ/文字が左上に寄る（実際に起きた・2026-07-29）。編集用の
    //   display:none / flex どちらの状態からでも、保存版は必ず flex に戻す。
    if(_op){ _op.removeAttribute('data-paused'); _op.style.display='flex'; _op.style.removeProperty('opacity'); _op.style.removeProperty('transition'); }
    // 「幕が出ている間はページのアニメを止める」印は編集中の一時状態＝保存に残すと次に開いた時ずっと止まる
    doc.classList.remove('op-wait');
    [].slice.call(doc.querySelectorAll('script')).forEach(function(s){ if(/__ce/.test(s.textContent)) s.remove(); });
    [].slice.call(doc.querySelectorAll('style')).forEach(function(s){ if(/#__ce/.test(s.textContent)) s.remove(); });
    // 🔗 リンク無効化の一時退避(data-cehref)が保存に紛れないよう必ず元のhrefへ戻す
    [].slice.call(doc.querySelectorAll('a[data-cehref]')).forEach(function(n){ n.setAttribute('href', n.getAttribute('data-cehref')); n.removeAttribute('data-cehref'); });
    // 過去の文字分割（修正前のsplitChars）で割れた絵文字の片割れ（孤立サロゲート）が残っていても
    // 保存をUTF-8エラーで失敗させない：ペアが揃った正常な絵文字は残し、孤立した片割れだけ捨てる
    var __out=doc.outerHTML.replace(/([\\uD800-\\uDBFF][\\uDC00-\\uDFFF])|[\\uD800-\\uDFFF]/g,function(m,p){ return p||''; });
    return '<!doctype html>\\n'+__out;
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
  // 「文字を持たない飾りspan」（浮遊する丸・図形など）は文字の断片ではなく1つの部品＝親へ上らず本人を掴む
  function _isGraphicInline(el){
    if((el.textContent||'').trim()) return false;             // 文字がある＝文字の断片扱いのまま
    var st; try{ st=getComputedStyle(el); }catch(_){ return false; }
    if(st.position==='absolute'||st.position==='fixed') return true;   // 浮かせてある飾り
    if(st.backgroundColor!=='rgba(0, 0, 0, 0)'||st.backgroundImage!=='none') return true;  // 色/画像を持つ飾り
    return false;
  }
  // <span>等でも「見た目は独立した箱」ならそれ自体を掴ませる（親へ上らない）。
  // ★縦書きラベルが掴めない報告(2026-07-21)の原因＝タグ名だけで判定していたため、
  //   display:block の <span class="vertical-label"> でも「文字の断片」とみなして
  //   親の巨大なレイアウトdivまで上っていた＝クリックしても永久に選べない。
  function _isStandaloneBox(el){
    if(!el||el.nodeType!==1) return false;
    var cl=el.classList;
    if(cl&&(cl.contains('ce_psel')||cl.contains('ce_shape'))) return true;   // ツールが置いた部品は単体で掴める
    // ツールが1文字ずつ包んだ断片は今までどおり親へ上る（アニメ用のinline-blockに引っかからないように）
    if(cl&&(cl.contains('fxa_ch')||cl.contains('imp-char')||cl.contains('ch')||cl.contains('fxa_ln')||cl.contains('fxa_lni'))) return false;
    // ★1〜2文字は基本「分割された文字の断片」＝親へ上る。ただし“アイコンの箱”だけは例外（2026-07-28）。
    //   例：NEWSの行末の丸い「→」＝<i>36×36・display:grid・border 1px・border-radius:50%。
    //   今までは断片扱いで親のリンク全体が選ばれ、矢印そのものを掴めなかった。
    if((el.textContent||'').trim().length<=2){
      var si; try{ si=getComputedStyle(el); }catch(_){ return false; }
      if(si.display==='inline') return false;
      var w=el.offsetWidth, h=el.offsetHeight;
      if(w<16||h<16) return false;
      var hasBd=(parseFloat(si.borderTopWidth)>0&&si.borderTopStyle!=='none');
      var hasBg=(si.backgroundColor&&si.backgroundColor!=='rgba(0, 0, 0, 0)'&&si.backgroundColor!=='transparent')
                ||(si.backgroundImage&&si.backgroundImage!=='none');
      var rad=parseFloat(si.borderTopLeftRadius)||0;
      var square=(w>=24&&h>=24&&Math.max(w,h)/Math.min(w,h)<=1.6);
      return !!(hasBd||hasBg||rad>=4||square);
    }
    var st; try{ st=getComputedStyle(el); }catch(_){ return false; }
    if((st.writingMode||'').indexOf('vertical')===0) return true;              // 縦書き＝意図して作った塊
    if(st.position==='absolute'||st.position==='fixed') return true;           // 浮かせて置いてある
    // 親がflex/gridで自分がその「並びの1コマ」＝レイアウト部品なので独立した箱として掴ませる。
    // ★見出しの中の <span style="display:block"> は親がflexではないので巻き込まない
    //   （見出しは今までどおり丸ごと掴める＝既存の操作感を壊さないための線引き）
    if(st.display==='block'||st.display==='flex'||st.display==='grid'){
      var pa=el.parentElement, ps; if(!pa) return false;
      try{ ps=getComputedStyle(pa); }catch(_){ return false; }
      if(/(flex|grid)/.test(ps.display)) return true;
    }
    return false;
  }
  // 🔼🔽 重なり順を「相手より確実に上（下）」にする（2026-07-28）。
  //   ★z-indexを1ずつ足すだけでは勝てない：①相手の数字が大きい ②自分が別の"重なりの部屋"
  //   (stacking context：transform/opacity/isolation/z-index持ちの親)の中にいると、中でいくら数字を
  //   上げても部屋ごと後ろのまま。実際「🔼手前を何度押してもキャプションカードの下から出てこない」報告あり。
  //   → 重なっている相手の数字を実測して跳び越し、それでも上に出なければ親を1段ずつ一緒に上げる（最大4段）。
  function _zNum(n){ var v=parseInt(getComputedStyle(n).zIndex,10); return isNaN(v)?null:v; }
  function _zRivals(el){
    var r=el.getBoundingClientRect(), out=[];
    if(!r.width||!r.height) return out;
    [].slice.call(document.body.querySelectorAll('div,section,figure,article,aside,p,span,a,img,h1,h2,h3,ul,li')).slice(0,2500).forEach(function(n){
      if(n===el||el.contains(n)||n.contains(el)) return;
      // ★ツール自身のUI（青い■ハンドル・メニュー・パネル）は相手に数えない。
      //   これを外すと z-index 2147483646 のハンドルを相手だと思って、数字が一気に最大値まで飛ぶ（実際に起きた）
      if(n.id&&n.id.indexOf('__ce')===0) return;
      if(String(n.className||'').indexOf('__ce')>=0) return;
      if(n.closest&&n.closest('[id^="__ce"]')) return;
      var b=n.getBoundingClientRect();
      if(b.width<6||b.height<6) return;
      if(b.right<=r.left||b.left>=r.right||b.bottom<=r.top||b.top>=r.bottom) return;   // 重なっていない
      out.push(n);
    });
    return out;
  }
  // 重なっている場所で実際に一番上に出ているか。画面の外にあって判定できない時は null を返す
  //   （nullを「まだ下」と誤解すると、親を無駄に4段まで持ち上げてしまう）
  function _zOnTop(el){
    var r=el.getBoundingClientRect();
    if(r.bottom<0||r.top>window.innerHeight||r.right<0||r.left>window.innerWidth) return null;
    var x=Math.round(Math.max(2,Math.min(r.left+r.width/2, window.innerWidth-3)));
    var y=Math.round(Math.max(2,Math.min(r.top+r.height/2, window.innerHeight-3)));
    var hit=document.elementFromPoint(x,y);
    if(!hit) return null;
    if(hit.closest&&hit.closest('[id^="__ce"]')) return null;   // ツールのメニューが上に乗っている＝判定できない
    return (hit===el||el.contains(hit));
  }
  function zStack(el, back){
    if(!el) return {z:0,lifted:0};
    var best=null;
    _zRivals(el).forEach(function(n){        // 相手側の"部屋"の数字を拾う（自分に一番近い祖先の値）
      var t=n;
      while(t&&t!==document.body){ var z=_zNum(t); if(z!==null){ if(best===null||(back?z<best:z>best)) best=z; break; } t=t.parentElement; }
    });
    if(best===null) best=0;
    var z=Math.max(-999, Math.min(9999, back?best-1:best+1));   // 数字が青天井に飛ばないよう上限を付ける
    function put(n){
      if(getComputedStyle(n).position==='static') n.style.setProperty('position','relative','important');
      n.style.setProperty('z-index', z, 'important');
    }
    put(el);
    var lifted=0, p=el.parentElement;
    // まだ上に出ていなければ、親の"部屋"ごと一緒に上げる（セクションまで・最大4段）
    for(var i=0;i<4 && p && p!==document.body && !/^(SECTION|HEADER|FOOTER|MAIN)$/.test(p.tagName); i++){
      if(back) break;                       // 後ろへ送る時は親を触らない（他が巻き添えになる）
      var top=_zOnTop(el);
      if(top===true||top===null) break;     // 出ている／判定できない時はここで止める
      put(p); lifted++; p=p.parentElement;
    }
    return {z:z, lifted:lifted};
  }
  // 🖼 スライドショーの中で「いま見えている1枚」を返す（2026-07-30・ユーザー要望）。
  //   3枚が同じ場所にピッタリ重なっているので、どれが掴まれるかは重なり順まかせだった。
  //   赤が出ている時は赤、緑が出ている時は緑を触りたい＝不透明度がいちばん高い1枚を選ぶ。
  function slFront(w){
    if(!w||!w.querySelectorAll) return null;
    var best=null, bo=-1;
    [].slice.call(w.querySelectorAll('img')).forEach(function(im){
      var o=0; try{ o=parseFloat(getComputedStyle(im).opacity); }catch(_){ }
      if(!isFinite(o)) o=0;
      if(o>bo){ bo=o; best=im; }
    });
    return best;
  }
  function pickTarget(el){
    // 🖼 スライドショーの「スライド自身」を掴んだ時だけ、今見えている1枚に付け替える。
    //   ★上に乗せた別の画像（鳥など）や文字は付け替えない（2026-07-30・ユーザー要望）。
    //     ここを「入れ物の中なら全部」にすると、スライドの上に置いた鳥が永久に掴めなくなる。
    var _slw=(el&&el.parentElement&&el.parentElement.getAttribute
              &&el.parentElement.getAttribute('data-slshow')!=null&&el.tagName==='IMG')?el.parentElement
             :((el&&el.getAttribute&&el.getAttribute('data-slshow')!=null)?el:null);
    if(_slw){ var _sf=slFront(_slw); if(_sf) return _sf; }
    // 🧩グループの一員は、それ自身を掴む（親に吸い上げると印が見つからず仲間が付いてこない）
    if(el&&el.getAttribute&&el.getAttribute('data-cegid')) return el;
    // ★ツールが置いた部品（🔓実体化した飾り・🔶図形）は、それ自身を掴む＝親に吸い上げない。
    //   「01」のような1〜2文字のspanは「分割された文字の断片」と見なされて親が選ばれ、
    //   ハンドルは出るのに掴んでも動かない、という報告が出た（2026-07-29）。
    if(el&&el.classList&&(el.classList.contains('ce_psel')||el.classList.contains('ce_shape')
       ||el.classList.contains('ce_tnode'))) return el;   // ce_tnode＝裸の文字を包んだ入れ物（親に吸い上げない）
    var INLINE={SPAN:1,B:1,I:1,EM:1,STRONG:1,SMALL:1,MARK:1,U:1,FONT:1,WBR:1,BR:1};
    var cur=el, hops=0;
    while(cur && cur.parentElement && cur!==document.body && hops<10 && INLINE[cur.tagName]
          && !_isGraphicInline(cur) && !_isStandaloneBox(cur)){
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
  // 後から乗せた飾り画像の「透明な部分」か判定（絵のある所だけ掴ませ、透過部分は下へ通す）。
  // 対象は__ceFree印＝ツールで追加した画像だけ。カンプ本体の画像の掴み方は今までどおり。
  var _pxCv=null;
  // 切り抜いた「背景の絵」の透明な部分もクリックを素通しにする（imgと同じ考え方）。
  //   ★これが無いと、透過にした瞬間その要素の箱ぜんぶが当たり判定になり、
  //     透明な余白を右クリックしただけで大きな箱が選ばれる（＝全体がグループ化されたように見える）。
  var _bgNatCache={};
  function _bgNat(url){
    var im=_bgNatCache[url];
    if(!im){ im=new Image(); im.src=url; _bgNatCache[url]=im; }
    return (im.complete&&im.naturalWidth)?im:null;
  }
  function _clearPixelBg(el, x, y){
    if(!el||el.nodeType!==1||!el.getAttribute||el.getAttribute('data-cecutbg')!=='1') return false;
    var cs; try{ cs=getComputedStyle(el); }catch(_){ return false; }
    if((cs.backgroundRepeat||'').indexOf('no-repeat')<0) return false;   // 敷き詰めは判定しない（安全側）
    var m=(cs.backgroundImage||'').match(/url\\(["']?(.*?)["']?\\)/); if(!m) return false;
    var im=_bgNat(m[1]); if(!im) return false;
    var r=el.getBoundingClientRect();
    var bl=parseFloat(cs.borderLeftWidth)||0, bt=parseFloat(cs.borderTopWidth)||0;
    var bw=r.width-bl-(parseFloat(cs.borderRightWidth)||0), bh=r.height-bt-(parseFloat(cs.borderBottomWidth)||0);
    if(!(bw>0&&bh>0)) return false;
    var iw=im.naturalWidth, ih=im.naturalHeight, sw, sh, bs=cs.backgroundSize||'auto';
    if(bs==='cover'||bs==='contain'){
      var sc=(bs==='cover')?Math.max(bw/iw,bh/ih):Math.min(bw/iw,bh/ih);
      sw=iw*sc; sh=ih*sc;
    }else{
      var ps=bs.split(' ');
      var pv=function(v,base){ if(/%$/.test(v)) return parseFloat(v)/100*base; if(/px$/.test(v)) return parseFloat(v); return null; };
      sw=pv(ps[0]||'',bw); sh=(ps.length>1)?pv(ps[1],bh):null;
      if(sw==null&&sh==null){ sw=iw; sh=ih; }
      else if(sh==null){ sh=sw*ih/iw; }
      else if(sw==null){ sw=sh*iw/ih; }
    }
    if(!(sw>0&&sh>0)) return false;
    var one=function(s, base, span){
      var mm=/^calc[(](.+?)\\s*([+-])\\s*(-?[0-9.]+)px[)]$/.exec(s||''), off=0, b=s||'0%';
      if(mm){ b=mm[1]; off=(mm[2]==='-'?-1:1)*parseFloat(mm[3]); }
      return (/%$/.test(b)? parseFloat(b)/100*(base-span) : (parseFloat(b)||0))+off;
    };
    var pos=cs.backgroundPosition||'0% 0%', out=[], d=0, cur2='';
    for(var i=0;i<pos.length;i++){ var ch=pos.charAt(i);
      if(ch==='(') d++; else if(ch===')') d--;
      if(ch===' '&&d===0){ if(cur2){ out.push(cur2); cur2=''; } continue; }
      cur2+=ch; }
    if(cur2) out.push(cur2);
    var px=x-(r.left+bl)-one(out[0],bw,sw), py=y-(r.top+bt)-one(out[1],bh,sh);
    if(px<0||py<0||px>=sw||py>=sh) return true;         // 絵の外＝ここには何も描かれていない
    try{
      if(!_pxCv){ _pxCv=document.createElement('canvas'); _pxCv.width=1; _pxCv.height=1; }
      var g=_pxCv.getContext('2d',{willReadFrequently:true});
      g.clearRect(0,0,1,1);
      g.drawImage(im, Math.floor(px/sw*iw), Math.floor(py/sh*ih), 1, 1, 0, 0, 1, 1);
      return g.getImageData(0,0,1,1).data[3]<8;
    }catch(_){ return false; }
  }
  // ⬆手前に出すで持ち上げた「入れ物」は、中身が無い場所でも箱ぜんぶが当たり判定になり、
  //   その下の文字が掴めなくなる（実報告）。何も描いていない場所ではクリックを素通しにする。
  function _zliftSkip(c, x, y){
    if(!c||!c.getAttribute||c.getAttribute('data-cezlift')!=='1') return false;
    var cs; try{ cs=getComputedStyle(c); }catch(_){ return false; }
    if(cs.backgroundImage&&cs.backgroundImage!=='none') return false;         // 自分で絵を描いている
    var m=(cs.backgroundColor||'').match(/rgba?\\(([^)]+)\\)/);
    if(m){ var p=m[1].split(','); var a=(p.length>3)?parseFloat(p[3]):1; if(a>0.05) return false; }  // 色で塗っている
    var ls=[]; try{ ls=document.elementsFromPoint(x,y); }catch(_){ return false; }
    for(var i=0;i<ls.length;i++){
      if(ls[i]===c) break;
      if(c.contains(ls[i])) return false;      // その点に自分の中身がある＝掴んでよい
    }
    return true;                                // 空っぽの場所＝下へ通す
  }
  function _clearPixel(el, x, y){
    if(_zliftSkip(el, x, y)) return true;
    if(el&&el.nodeType===1&&el.tagName!=='IMG') return _clearPixelBg(el, x, y);
    if(!el||el.tagName!=='IMG'||!el.__ceFree) return false;
    if(!el.naturalWidth||!el.naturalHeight) return false;
    try{
      var r=el.getBoundingClientRect();
      if(!r.width||!r.height) return false;
      var sx=Math.floor((x-r.left)/r.width*el.naturalWidth);
      var sy=Math.floor((y-r.top)/r.height*el.naturalHeight);
      if(sx<0||sy<0||sx>=el.naturalWidth||sy>=el.naturalHeight) return true;
      if(!_pxCv){ _pxCv=document.createElement('canvas'); _pxCv.width=1; _pxCv.height=1; }
      var g=_pxCv.getContext('2d',{willReadFrequently:true});
      g.clearRect(0,0,1,1);
      g.drawImage(el, sx, sy, 1, 1, 0, 0, 1, 1);
      return g.getImageData(0,0,1,1).data[3]<8;   // ほぼ透明＝ここは絵が無い
    }catch(_){ return false; }                     // 読めない画像は従来どおり「掴める」扱い（安全側）
  }
  // 👆 その座標に「実際に文字が描かれている」なら、その文字の要素を返す（AIなし）。
  //   ★上に透明な入れ物がかぶっていると、文字を右クリックしても入れ物が掴まれる（実報告）。
  //   キャレット位置＝ブラウザが「ここに文字がある」と認めた位置なので、覆われていても本物の文字に届く。
  function _textAt(x,y){
    var ls=[]; try{ ls=document.elementsFromPoint(x,y); }catch(_){ return null; }
    for(var i=0;i<ls.length;i++){
      var n=ls[i];
      if(!n||n.nodeType!==1) continue;
      if(n.closest&&n.closest('[id^="__ce"]')) continue;
      if(n===document.body||n.tagName==='HTML') break;
      for(var k=0;k<n.childNodes.length;k++){
        var c=n.childNodes[k];
        if(c.nodeType!==3||!(c.nodeValue||'').replace(/[\\s\\u200b]/g,'')) continue;
        var rg=document.createRange(); rg.selectNode(c);
        var rs=[]; try{ rs=rg.getClientRects(); }catch(_){ continue; }
        for(var j=0;j<rs.length;j++){
          var q=rs[j];
          if(x>=q.left-1&&x<=q.right+1&&y>=q.top-1&&y<=q.bottom+1) return n;   // ここに文字が描かれている
        }
      }
    }
    return null;
  }
  // クリック地点の「本当に触りたい要素」＝生き返らせた画像の透明部分なら下の要素を返す。
  function _realTarget(e){
    var t=e.target;
    if(!_clearPixel(t, e.clientX, e.clientY)) return t;
    var under=document.elementsFromPoint(e.clientX, e.clientY);
    for(var i=0;i<under.length;i++){
      var c=under[i];
      if(c.closest('[id^="__ce"]')) continue;
      if(_clearPixel(c, e.clientX, e.clientY)) continue;
      return c;
    }
    return t;
  }
  // 右クリック地点で、膜を貫通して「実体のある要素」まで潜る（膜が何枚重なっていてもOK）。
  function _descendOverlay(el, x, y){
    if(!_seeThrough(el)) return el;
    var under=document.elementsFromPoint(x, y);
    for(var i=0;i<under.length;i++){
      var c=under[i];
      if(c.closest('[id^="__ce"]')) continue;
      if(_clearPixel(c, x, y)) continue;  // 追加した飾り画像の透明部分は素通り
      var pc=pickTarget(c);
      if(!_seeThrough(pc)) return pc;   // 実体のある要素が見つかったらそこを選ぶ
    }
    return el;                           // 全部が膜なら元のまま（膜自体を消せるように）
  }
  // ★クリックがすり抜ける飾り（pointer-events:none）を座標の当たり判定で拾う（2026-07-21）。
  //   document.elementsFromPoint は pointer-events:none の要素を返さないので、
  //   「見えているのに右クリックしても後ろの器が選ばれる」＝掴めない絵になっていた（気球の絵で発覚）。
  //   小さい順＝一番具体的な絵が先頭。飾りらしいもの（画像・図形・背景画像持ち）だけに絞ってノイズを防ぐ。
  // root=探す範囲（省略でページ全体）／mustHit=true なら「クリック地点が絵の中」に限る。
  // クリックが少しズレても見つかるよう、選んだ器の中を mustHit=false で舐める使い方もする。
  function _peScan(root,x,y,mustHit){
    var out=[];
    var all=(root||document.body).querySelectorAll('img,svg,canvas,video,picture,div,span,i,figure');
    for(var i=0;i<all.length && i<6000;i++){
      var e=all[i];
      if(e.closest('[id^="__ce"]')) continue;
      var cs; try{ cs=getComputedStyle(e); }catch(_){ continue; }
      if(cs.pointerEvents!=='none') continue;                      // すり抜ける設定のものだけ
      if(cs.visibility==='hidden'||parseFloat(cs.opacity||'1')<0.05) continue;
      var r=e.getBoundingClientRect();
      if(r.width<5||r.height<5) continue;
      if(r.bottom<0||r.top>window.innerHeight) continue;            // 画面の外にあるものは出さない
      if(mustHit && (x<r.left||x>r.right||y<r.top||y>r.bottom)) continue;
      // ★「絵」だけに限る（文字を持つ箱や空のdivは拾わない）＝文字の上を右クリックした時に
      //   飾りが横取りする誤爆を防ぐ。実測で空divに横取りされたので条件を絞った。
      var isArt=/^(IMG|SVG|CANVAS|VIDEO|PICTURE)$/.test(e.tagName)
        || (cs.backgroundImage&&cs.backgroundImage!=='none'&&cs.backgroundImage.indexOf('gradient')<0);
      if(!isArt) continue;
      if(r.width*r.height > window.innerWidth*window.innerHeight*0.6) continue;  // 画面いっぱいの膜は掴まない
      if(mustHit && e.tagName==='IMG' && _clearPixel(e,x,y)) continue;           // 透明な部分は掴まない
      var cx=(r.left+r.right)/2-x, cy=(r.top+r.bottom)/2-y;
      out.push({el:e, a:r.width*r.height, d:Math.sqrt(cx*cx+cy*cy)});
    }
    out.sort(function(p,q){ return (p.d-q.d)||(p.a-q.a); });        // クリックに近い順→小さい順
    return out.map(function(o){ return o.el; });
  }
  function _peNoneAt(x,y){ return _peScan(null,x,y,true); }
  // すり抜ける設定の絵を選んだら、掴んで動かせるようにpointer-eventsを戻す（黙って直さず必ず知らせる）
  function _peWake(el){
    if(!el||el.nodeType!==1) return false;
    var cs; try{ cs=getComputedStyle(el); }catch(_){ return false; }
    if(cs.pointerEvents!=='none') return false;
    el.style.setProperty('pointer-events','auto','important');
    markDirty();
    return true;
  }
  // ===== ➖ 線・飾りを消す（border/疑似要素・AIなし・2026-07-20） =====
  // 疑似要素(::before/::after)はDOMに実体が無く掴んで移動できない（既知の限界）ため、「消す/戻す」だけ提供。
  // 消し方＝要素にcepsoff-b/aクラスを付け、content:none!importantのCSS（#ce-psoff・保存に残る）で殺す。
  function _psCss(){
    if(document.getElementById('ce-psoff')) return;
    var st=document.createElement('style'); st.id='ce-psoff';
    st.textContent='.cepsoff-b::before{content:none!important}.cepsoff-a::after{content:none!important}';
    (document.head||document.documentElement).appendChild(st);
  }
  // ===== 🔓 疑似要素(::before/::after)を「本物の要素」に作り替えて掴めるようにする（2026-07-29・要望）=====
  //   ★「01」のような大きな飾り数字はCSSの content で描かれていてDOMに実体が無い＝掴めない・動かせない・
  //   🎯レイヤー選択にも出てこない。見た目をそのままコピーした span を置き、元の疑似要素は消す
  //   （＝見た目は変わらないまま、普通の要素として掴める・色も大きさも変えられる）。
  var PS_COPY=['position','top','right','bottom','left','width','height','display','box-sizing',
    'padding','margin','font-family','font-size','font-weight','font-style','line-height','letter-spacing',
    'text-align','white-space','color','background-color','background-image','background-size','background-position',
    'background-repeat','border-radius','box-shadow','opacity','z-index','transform','transform-origin',
    'writing-mode','text-shadow','-webkit-text-fill-color','align-items','justify-content'];
  function psList(el){
    var out=[];
    if(!el||!el.tagName) return out;
    ['before','after'].forEach(function(w){
      if(el.classList&&el.classList.contains(w==='after'?'cepsoff-a':'cepsoff-b')) return;   // 作り替え済み
      var cs; try{ cs=getComputedStyle(el,'::'+w); }catch(_){ return; }
      if(!cs) return;
      var ct=cs.content||'';
      if(!ct||ct==='none'||ct==='normal') return;
      var txt=ct.replace(/^["']|["']$/g,'');
      var w0=parseFloat(cs.width)||0, h0=parseFloat(cs.height)||0;
      if(!txt && (w0<2||h0<2)) return;                       // 中身も大きさも無い＝見えていない
      out.push({which:w, text:txt.slice(0,10), img:/^url\(/.test(ct)});
    });
    return out;
  }
  function psMaterialize(el, which){
    var cs; try{ cs=getComputedStyle(el,'::'+which); }catch(_){ return null; }
    if(!cs) return null;
    var ct=cs.content||'';
    if(!ct||ct==='none'||ct==='normal') return null;
    var sp=document.createElement('span');
    sp.className='ce_psel'; sp.setAttribute('data-cepsel', which);
    PS_COPY.forEach(function(pr){
      var v=cs.getPropertyValue(pr);
      if(!v||v==='auto'||v==='none'||v==='normal') return;
      sp.style.setProperty(pr, v);
    });
    if(cs.display==='inline') sp.style.setProperty('display','inline-block');   // inlineのままだとドラッグで1pxも動かない
    if(/^url\(/.test(ct)) sp.style.setProperty('background-image', ct);         // 画像contentは背景として持たせる
    else sp.textContent=ct.replace(/^["']|["']$/g,'');
    _psCss();
    el.classList.add(which==='after'?'cepsoff-a':'cepsoff-b');                  // 元の飾りは消す（二重に見えないように）
    if(which==='after') el.appendChild(sp); else el.insertBefore(sp, el.firstChild);
    markDirty();
    return sp;
  }
  // 🎨 セクションの背景色を変える（AIなし・即反映）＝右クリックから直接開く浮動パネル。
  //   ①このページのセクション色 ②ページで使用中の色（頻度順） ③自由な色 から選ぶ。
  //   ⚙大メニューにも同じ機能があるが、右クリックからすぐ届くようにこちらへ切り出した。
  function openSecBg(el,x,y){
    var old=document.getElementById('__ce_sbgp'); if(old) old.remove();
    var target=(el&&el.closest)?el.closest('section,header,footer'):null;
    if(!target){ if(msg) msg.textContent='セクション（またはヘッダー/フッター）の中を右クリックしてから使ってください'; return; }
    function _colOk(c){ return c && c!=='transparent' && !/rgba\\(\\s*\\d+,\\s*\\d+,\\s*\\d+,\\s*0\\)/.test(c); }
    function applySecBg(c){
      target.style.setProperty('background-color', c, 'important');
      // グラデが上に被って色が見えない時は外す（写真背景url は残す）
      try{ var bi=getComputedStyle(target).backgroundImage; if(bi && bi.indexOf('gradient')>=0 && bi.indexOf('url(')<0) target.style.setProperty('background-image','none','important'); }catch(_){}
      markDirty();
      if(msg) msg.textContent='セクションの背景色を '+c+' にしました（💾保存で確定・⟲戻すで取り消し）';
    }
    function sw(c,t){ return '<button class="__ce_sbgsw" data-c="'+c+'" title="'+esc(t||c)+'" style="width:24px;height:24px;border:1px solid rgba(0,0,0,.28);border-radius:5px;cursor:pointer;background:'+c+';padding:0;margin:2px;vertical-align:middle"></button>'; }
    var secSw=[], seen={};
    [].slice.call(document.querySelectorAll('header,section,footer')).forEach(function(s,i){
      if(s.closest('[id^="__ce"]')) return;
      var c=''; try{ c=getComputedStyle(s).backgroundColor; }catch(_){ return; }
      if(!_colOk(c)||seen[c]) return; seen[c]=1;
      secSw.push(sw(c,(i+1)+'番目('+s.tagName.toLowerCase()+')の背景 '+c));
    });
    var cnt={};
    [].slice.call(document.querySelectorAll('body *')).slice(0,1500).forEach(function(n){
      if(n.closest('[id^="__ce"]')) return;
      var cs; try{ cs=getComputedStyle(n); }catch(_){ return; }
      [cs.backgroundColor, cs.color].forEach(function(c){ if(_colOk(c)) cnt[c]=(cnt[c]||0)+1; });
    });
    var pgSw=Object.keys(cnt).filter(function(c){return !seen[c];})
      .sort(function(a,b){return cnt[b]-cnt[a];}).slice(0,14).map(function(c){return sw(c,c+'（ページ内で使用中）');});
    var p=document.createElement('div'); p.id='__ce_sbgp';
    p.setAttribute('style','position:fixed;z-index:2147483647;background:#fff;color:#1d1d1f;border:1px solid #dbe4ee;border-radius:11px;padding:10px 12px;font:12px/1.6 sans-serif;box-shadow:0 8px 28px rgba(0,0,0,.28);max-width:320px');
    p.innerHTML='<b>🎨 セクションの背景色（〈'+target.tagName.toLowerCase()+'〉）</b>'
      +'<div style="opacity:.7;margin:6px 0 2px">このページのセクション色</div><div>'+(secSw.join('')||'<span style="color:#999">（なし）</span>')+'</div>'
      +'<div style="opacity:.7;margin-top:6px">ページで使われている色</div><div>'+(pgSw.join('')||'<span style="color:#999">（なし）</span>')+'</div>'
      +'<div style="opacity:.7;margin-top:6px">新しい色（選ぶと即反映）</div><input type="color" id="__ce_sbgc" value="#f5f7fa" style="width:44px;height:26px;padding:0;border:1px solid #ccc;border-radius:5px;cursor:pointer;vertical-align:middle">'
      +'<button data-x="1" style="margin-left:8px;background:#555;color:#fff;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;vertical-align:middle">✖ 閉じる</button>';
    document.body.appendChild(p);
    p.style.left=Math.max(6,Math.min(x,window.innerWidth-p.offsetWidth-8))+'px';
    p.style.top=Math.max(6,Math.min(y,window.innerHeight-p.offsetHeight-8))+'px';
    p.addEventListener('click',function(ev){
      ev.stopPropagation();
      if(ev.target.getAttribute('data-x')){ p.remove(); return; }
      var b=ev.target.closest('.__ce_sbgsw'); if(b){ applySecBg(b.getAttribute('data-c')); return; }
    });
    p.querySelector('#__ce_sbgc').addEventListener('input',function(){ applySecBg(this.value); });
  }
  // 🖌 文字の「行の背景」に色ボックスを敷く（AIなし・2026-07-24）。
  //   マーカー(蛍光ペン)と違い、文字のまわりに余白を付けたベタ塗りのボックス。
  //   中身を span.ce_txtbg で包み、box-decoration-break:clone で複数行でも行ごとに背景が付く。
  function textBgApply(el,color){
    if(!el||el.nodeType!==1) return;
    var sp;
    if(el.classList.contains('ce_txtbg')){ sp=el; }
    else {
      // ★既に背景spanが（子孫でも祖先でも）あれば使い回す＝色を塗り直すだけ。
      //   これをしないと内側の文字を選んで塗るたびにspanが入れ子になり「2重に色が付いて1枚消せない」事故になる。
      var ex=(el.querySelector&&el.querySelector('.ce_txtbg'))||(el.closest&&el.closest('.ce_txtbg'));
      if(ex){ sp=ex; }
      else {
        sp=document.createElement('span'); sp.className='ce_txtbg';
        while(el.firstChild) sp.appendChild(el.firstChild);
        el.appendChild(sp);
      }
    }
    // 縦書き(vertical-rl等)は行の向きが横＝余白を入れ替えないと隣の列と背景が重なる（報告あり）
    var vert=false; try{ vert=/vertical/.test(getComputedStyle(sp).writingMode||''); }catch(_){}
    sp.style.setProperty('background-color',color,'important');
    sp.style.setProperty('padding', vert?'.4em .1em':'.12em .4em','important');
    sp.style.setProperty('border-radius','3px','important');
    sp.style.setProperty('box-decoration-break','clone','important');
    sp.style.setProperty('-webkit-box-decoration-break','clone','important');
    markDirty();
  }
  // 包みspanを剥がして中身を戻す（背景を消す）
  function textBgRemove(el){
    if(!el) return;
    // ★2重・3重に付いていても一度で全部はがす。範囲＝祖先に背景があればその親、無ければ自分。
    //   その配下（＋自分自身）の .ce_txtbg を全部はがす＝「下の1枚が消せない」を根絶。
    var scope=el, anc=el.closest?el.closest('.ce_txtbg'):null;
    if(anc && anc.parentElement) scope=anc.parentElement;
    var list=scope.querySelectorAll?[].slice.call(scope.querySelectorAll('.ce_txtbg')):[];
    if(scope.classList && scope.classList.contains('ce_txtbg')) list.unshift(scope);
    if(!list.length) return;
    list.forEach(function(sp){
      if(sp.parentNode && sp!==scope){ var pa=sp.parentNode; while(sp.firstChild) pa.insertBefore(sp.firstChild, sp); pa.removeChild(sp); }
      else { ['background-color','padding','border-radius','box-decoration-break','-webkit-box-decoration-break'].forEach(function(pr){ sp.style.removeProperty(pr); }); sp.classList.remove('ce_txtbg'); }
    });
    markDirty();
  }
  // 🖌 文字の背景ボックスのパレット（右クリックから直接開く浮動パネル・openSecBg と同じ作り）
  function openTextBg(el,x,y){
    var old=document.getElementById('__ce_tbgp'); if(old) old.remove();
    if(!el||!(el.textContent||'').trim()){ if(msg) msg.textContent='文字のある要素を右クリックしてから使ってください'; return; }
    function _colOk(c){ return c && c!=='transparent' && !/rgba\\(\\s*\\d+,\\s*\\d+,\\s*\\d+,\\s*0\\)/.test(c); }
    function sw(c,t){ return '<button class="__ce_tbgsw" data-c="'+c+'" title="'+esc(t||c)+'" style="width:24px;height:24px;border:1px solid rgba(0,0,0,.28);border-radius:5px;cursor:pointer;background:'+c+';padding:0;margin:2px;vertical-align:middle"></button>'; }
    var cnt={};
    [].slice.call(document.querySelectorAll('body *')).slice(0,1500).forEach(function(n){
      if(n.closest('[id^="__ce"]')) return;
      var cs; try{ cs=getComputedStyle(n); }catch(_){ return; }
      [cs.backgroundColor, cs.color].forEach(function(c){ if(_colOk(c)) cnt[c]=(cnt[c]||0)+1; });
    });
    var pgSw=Object.keys(cnt).sort(function(a,b){return cnt[b]-cnt[a];}).slice(0,14).map(function(c){return sw(c,c+'（ページ内で使用中）');});
    var soft=['#fdf6e3','#fff3d6','#fde8e8','#e8f3ee','#e7f0fb','#f3e8fb','#fbeee0','#eef2f6','#fff7cc','#ffe4ec'];
    var softSw=soft.map(function(c){return sw(c,c+'（淡い定番）');});
    var p=document.createElement('div'); p.id='__ce_tbgp';
    p.setAttribute('style','position:fixed;z-index:2147483647;background:#fff;color:#1d1d1f;border:1px solid #dbe4ee;border-radius:11px;padding:10px 12px;font:12px/1.6 sans-serif;box-shadow:0 8px 28px rgba(0,0,0,.28);max-width:330px');
    p.innerHTML='<b>🖌 文字の背景に色を塗る（〈'+el.tagName.toLowerCase()+'〉の行）</b>'
      +'<div style="opacity:.7;margin:6px 0 2px">淡い定番色（画像のようなクリーム等）</div><div>'+softSw.join('')+'</div>'
      +'<div style="opacity:.7;margin-top:6px">ページで使われている色</div><div>'+(pgSw.join('')||'<span style="color:#999">（なし）</span>')+'</div>'
      +'<div style="opacity:.7;margin-top:6px">好きな色（選ぶと即反映）</div><input type="color" id="__ce_tbgc" value="#fff3d6" style="width:44px;height:26px;padding:0;border:1px solid #ccc;border-radius:5px;cursor:pointer;vertical-align:middle">'
      +'<button data-flat="1" style="margin-left:8px;background:#eee;color:#333;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;vertical-align:middle">✕ 背景を消す</button>'
      +'<button data-x="1" style="margin-left:6px;background:#555;color:#fff;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;vertical-align:middle">閉じる</button>';
    document.body.appendChild(p);
    p.style.left=Math.max(6,Math.min(x,window.innerWidth-p.offsetWidth-8))+'px';
    p.style.top=Math.max(6,Math.min(y,window.innerHeight-p.offsetHeight-8))+'px';
    p.addEventListener('click',function(ev){
      ev.stopPropagation();
      if(ev.target.getAttribute('data-x')){ p.remove(); return; }
      if(ev.target.getAttribute('data-flat')){ textBgRemove(el); if(msg) msg.textContent='文字の背景を消しました（💾保存で確定）'; return; }
      var b=ev.target.closest('.__ce_tbgsw'); if(b){ textBgApply(el,b.getAttribute('data-c')); if(msg) msg.textContent='文字の背景に色を塗りました（💾保存で確定・⟲戻すで取り消し）'; return; }
    });
    p.querySelector('#__ce_tbgc').addEventListener('input',function(){ textBgApply(el,this.value); });
  }
  // ▎文字の左に縦線を引く（引用ブロック風・AIなし・即反映）。
  //   ★要素を増やさず border-left ＋ padding-left だけで作る＝掴んで移動もそのまま効き、保存でも壊れない。
  //   カンプの文字は自由配置(position:absolute)が多くて margin が効かないことがあるが、border/paddingは効く。
  var VL_COLORS=[['#5ec8c8','ミント'],['#4aa3e0','水色'],['#2f6fd0','青'],['#f6a94a','オレンジ'],['#e46a8b','ピンク'],['#8a7ce0','むらさき'],['#2b2b30','黒'],['#c9d3e0','グレー']];
  function vlineApply(el, opt){
    if(!el||!(el.textContent||'').trim()){ if(msg) msg.textContent='文字のある要素を右クリックしてから使ってください'; return null; }
    opt=opt||{};
    var col=opt.color||el.getAttribute('data-cevlc')||VL_COLORS[0][0];
    var w=(opt.w!=null)?opt.w:parseFloat(el.getAttribute('data-cevlw')||'4');
    var gap=(opt.gap!=null)?opt.gap:parseFloat(el.getAttribute('data-cevlg')||'18');
    w=Math.max(1,Math.min(24,w)); gap=Math.max(0,Math.min(90,gap));
    pushUndo(el);
    el.setAttribute('data-cevl','1');
    el.setAttribute('data-cevlc',col); el.setAttribute('data-cevlw',String(w)); el.setAttribute('data-cevlg',String(gap));
    el.style.setProperty('border-left', w+'px solid '+col,'important');
    el.style.setProperty('padding-left', gap+'px','important');
    markDirty();
    return el;
  }
  function vlineRemove(el){
    if(!el) return;
    pushUndo(el);
    ['data-cevl','data-cevlc','data-cevlw','data-cevlg'].forEach(function(a){ el.removeAttribute(a); });
    el.style.removeProperty('border-left'); el.style.removeProperty('padding-left');
    markDirty();
  }
  function openVLine(el,x,y){
    var old=document.getElementById('__ce_vlp'); if(old) old.remove();
    if(!el||!(el.textContent||'').trim()){ if(msg) msg.textContent='文字のある要素を右クリックしてから使ってください'; return; }
    var BS='background:#eef2f7;color:#333;border:1px solid #d7e0ea;border-radius:6px;padding:4px 9px;cursor:pointer';
    function sw(c,t){ return '<button class="__ce_vlsw" data-c="'+c+'" title="'+esc(t)+'" style="width:26px;height:26px;border:1px solid rgba(0,0,0,.28);border-radius:5px;cursor:pointer;background:'+c+';padding:0;margin:2px;vertical-align:middle"></button>'; }
    var cur=el.getAttribute('data-cevl');
    var p=document.createElement('div'); p.id='__ce_vlp';
    p.setAttribute('style','position:fixed;z-index:2147483647;background:#fff;color:#1d1d1f;border:1px solid #dbe4ee;border-radius:11px;padding:10px 12px;font:12px/1.6 sans-serif;box-shadow:0 8px 28px rgba(0,0,0,.28);max-width:330px');
    p.innerHTML='<b>▎ 文字の左に縦線を引く（〈'+el.tagName.toLowerCase()+'〉）</b>'
      +'<div style="opacity:.7;margin:6px 0 2px">色（押すと即つきます）</div>'
      +'<div>'+VL_COLORS.map(function(c){return sw(c[0],c[1]);}).join('')
      +'<input type="color" id="__ce_vlc" value="'+(el.getAttribute('data-cevlc')||'#5ec8c8')+'" title="好きな色" style="width:34px;height:26px;padding:0;border:1px solid #ccc;border-radius:5px;cursor:pointer;vertical-align:middle;margin-left:4px"></div>'
      +'<div style="opacity:.7;margin-top:8px">線の太さ</div>'
      +'<div style="display:flex;gap:5px;margin-top:3px"><button data-w="-1" style="'+BS+'">－ 細く</button><button data-w="1" style="'+BS+'">＋ 太く</button>'
      +'<button data-w="0" data-wset="4" style="'+BS+'">⟲ 4px</button></div>'
      +'<div style="opacity:.7;margin-top:8px">文字との間隔</div>'
      +'<div style="display:flex;gap:5px;margin-top:3px"><button data-g="-4" style="'+BS+'">← 詰める</button><button data-g="4" style="'+BS+'">→ 離す</button></div>'
      +'<div style="display:flex;gap:6px;margin-top:10px">'
      +'<button data-off="1" style="background:#c0392b;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer">✕ 縦線を消す</button>'
      +'<button data-x="1" style="background:#555;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer">閉じる</button></div>'
      +(cur?'':'<div style="opacity:.6;margin-top:6px;font-size:11px">色を押すとこの行の左に縦線が付きます（💾保存で確定）</div>');
    document.body.appendChild(p);
    p.style.left=Math.max(6,Math.min(x,window.innerWidth-p.offsetWidth-8))+'px';
    p.style.top=Math.max(6,Math.min(y,window.innerHeight-p.offsetHeight-8))+'px';
    p.addEventListener('click',function(ev){
      ev.stopPropagation();
      if(ev.target.getAttribute('data-x')){ p.remove(); return; }
      if(ev.target.getAttribute('data-off')){ vlineRemove(el); if(msg) msg.textContent='縦線を消しました（💾保存で確定）'; return; }
      var b=ev.target.closest('.__ce_vlsw');
      if(b){ vlineApply(el,{color:b.getAttribute('data-c')}); if(msg) msg.textContent='文字の左に縦線を引きました（💾保存で確定・⟲戻すで取り消し）'; return; }
      var ws=ev.target.getAttribute('data-wset');
      if(ws){ vlineApply(el,{w:parseFloat(ws)}); if(msg) msg.textContent='線の太さ：'+ws+'px'; return; }
      var w=ev.target.getAttribute('data-w');
      if(w!=null){ var nw=parseFloat(el.getAttribute('data-cevlw')||'4')+parseFloat(w); vlineApply(el,{w:nw}); if(msg) msg.textContent='線の太さ：'+Math.max(1,Math.min(24,nw))+'px'; return; }
      var g=ev.target.getAttribute('data-g');
      if(g!=null){ var ng=parseFloat(el.getAttribute('data-cevlg')||'18')+parseFloat(g); vlineApply(el,{gap:ng}); if(msg) msg.textContent='文字との間隔：'+Math.max(0,Math.min(90,ng))+'px'; return; }
    });
    p.querySelector('#__ce_vlc').addEventListener('input',function(){ vlineApply(el,{color:this.value}); });
  }
  function openDecoKill(el,x,y){
    var old=document.getElementById('__ce_pskill'); if(old) old.remove();
    var items=[];
    [el, el&&el.parentElement].forEach(function(t,ti){
      if(!t||t===document.body||t.tagName==='HTML') return;
      var cs; try{ cs=getComputedStyle(t); }catch(_){ return; }
      var nm=ti?('親〈'+t.tagName.toLowerCase()+'〉'):'この要素';
      ['top','right','bottom','left'].forEach(function(sd){
        var off=(t.style.getPropertyValue('border-'+sd)==='none');
        if(off||(cs.getPropertyValue('border-'+sd+'-style')!=='none'&&parseFloat(cs.getPropertyValue('border-'+sd+'-width'))>0))
          items.push({t:t,kind:'bd',side:sd,label:nm+'：'+({top:'上',right:'右',bottom:'下',left:'左'})[sd]+'の線(border)'+(off?'【消し済み】':'')});
      });
      ['::before','::after'].forEach(function(ps){
        var c=''; try{ c=getComputedStyle(t,ps).content; }catch(_){}
        var kcls=(ps==='::before')?'cepsoff-b':'cepsoff-a';
        if(t.classList.contains(kcls)||(c&&c!=='none'&&c!=='normal'))
          items.push({t:t,kind:(ps==='::before')?'pb':'pa',label:nm+'：飾り('+ps+')'+(t.classList.contains(kcls)?'【消し済み】':'')});
      });
    });
    if(!items.length){ if(msg) msg.textContent='この要素（と親）にborder線・疑似要素の飾りは見つかりませんでした（⬆外側を選ぶで一段上も試せます）'; return; }
    var p=document.createElement('div'); p.id='__ce_pskill';
    p.setAttribute('style','position:fixed;z-index:2147483647;background:#1d1d2b;color:#fff;border-radius:10px;padding:10px 12px;font:12.5px/1.7 sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.4);max-width:340px');
    p.innerHTML='<b>➖ 線・飾りを消す</b><div style="font-size:11px;opacity:.7;margin:2px 0 6px">疑似要素は掴んで移動できないので「消す/戻す」だけできます（同じボタンでトグル）</div>'
      +items.map(function(it,i){ return '<button data-i="'+i+'" style="display:block;width:100%;text-align:left;margin:3px 0;background:#34344a;color:#fff;border:none;border-radius:6px;padding:5px 8px;cursor:pointer">'+it.label+'</button>'; }).join('')
      +'<button data-x="1" style="margin-top:6px;background:#555;color:#fff;border:none;border-radius:6px;padding:4px 10px;cursor:pointer">✖ 閉じる</button>';
    document.body.appendChild(p);
    p.style.left=Math.max(6,Math.min(x,window.innerWidth-p.offsetWidth-8))+'px';
    p.style.top=Math.max(6,Math.min(y,window.innerHeight-p.offsetHeight-8))+'px';
    p.addEventListener('click',function(ev){
      ev.stopPropagation();
      if(ev.target.getAttribute('data-x')){ p.remove(); return; }
      var i=ev.target.getAttribute('data-i'); if(i==null) return;
      var it=items[+i];
      if(it.kind==='bd'){
        var pr='border-'+it.side;
        if(it.t.style.getPropertyValue(pr)==='none') it.t.style.removeProperty(pr);
        else it.t.style.setProperty(pr,'none','important');
      } else {
        _psCss();
        it.t.classList.toggle(it.kind==='pb'?'cepsoff-b':'cepsoff-a');
      }
      markDirty(); pushUndo(it.t);
      if(msg) msg.textContent='➖ 切り替えました（同じボタンで戻る・💾保存で確定）';
    });
  }
  // capture:true＝キャプチャ段階で先取りする。忠実クローン(元JS保持)の中に元サイト自前の
  // ===== ブラウザ風クイックメニュー（右クリックの瞬間にカーソル位置へ・よく使う操作だけ） =====
  // 大メニュー（従来のパネル）は「⚙ すべての編集メニュー…」から開く二段構え。
  var _bigFull=false;  // trueのとき、次のcontextmenuは従来の大メニューを開く
  var _bigFxFocus=false;  // trueのとき、大メニューを開いた直後に「✨動きを選ぶ」グリッドへ自動スクロール＋ハイライト
  function selectParent(fromFull){
    var pa=curEl&&curEl.parentElement;
    if(!pa || pa===document.body || pa.tagName==='HTML'){ if(msg) msg.textContent='これ以上外側はありません'; return; }
    if(_undraggable(pa)){ if(msg) msg.textContent='これ以上外側はページ全体の器なので選べません'; return; }
    if(fromFull) _bigFull=true;  // 大メニューから押した時は大メニューのまま親を開く
    _forceEl=pa;
    var r=pa.getBoundingClientRect();
    pa.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,
      clientX:Math.max(10,Math.min(r.left+r.width/2, window.innerWidth-20)),
      clientY:Math.max(10,Math.min(r.top+24, window.innerHeight-20))}));
  }
  // ===== ▦ セクションの境目オーバーレイ（表示中だけ・保存には残らない・pointer-events:noneで編集の邪魔をしない） =====
  function __ceSecoutSync(){
    var ov=document.getElementById('__ce_secout'); if(!ov) return;
    var list=[].slice.call(document.querySelectorAll('section,header,footer'));
    var html='', n=0, W=window.innerWidth, H=window.innerHeight;
    list.forEach(function(el){
      if(el.closest('[id^="__ce"]')) return;
      var r=el.getBoundingClientRect();
      if(r.width<2||r.height<2) return;
      var tag=el.tagName, col='#2f6bff', lbl;
      if(tag==='HEADER'){ col='#16a34a'; lbl='ヘッダー'; }
      else if(tag==='FOOTER'){ col='#ea580c'; lbl='フッター'; }
      else { n++; lbl='セクション '+n; }
      var top=Math.max(0,r.top), h=Math.min(H,r.bottom)-top;
      if(h<2 || r.bottom<0 || r.top>H) return;
      html+='<div style="position:absolute;left:'+r.left+'px;top:'+top+'px;width:'+r.width+'px;height:'+h+'px;outline:2px dashed '+col+';outline-offset:-2px;background:'+col+'0f;box-sizing:border-box"></div>';
      if(r.bottom>18 && r.top<H){
        var _ly=Math.min(Math.max(0,r.top), H-22);  // 縦長セクションでも画面内上端に貼り付く＝スクロール中も番号が消えない
        html+='<div style="position:absolute;left:'+r.left+'px;top:'+_ly+'px;background:'+col+';color:#fff;font:700 11px/1.35 system-ui,sans-serif;padding:2px 8px;border-radius:0 0 7px 0;white-space:nowrap">'+lbl+'</div>';
      }
    });
    ov.innerHTML=html;
  }
  function toggleSecOutline(){
    var ex=document.getElementById('__ce_secout');
    if(ex){ ex.remove(); window.removeEventListener('scroll',__ceSecoutSync,true); window.removeEventListener('resize',__ceSecoutSync); if(msg) msg.textContent='セクションの境目を隠しました'; return; }
    var ov=document.createElement('div'); ov.id='__ce_secout';
    ov.setAttribute('style','position:fixed;inset:0;pointer-events:none;z-index:2147482000');
    document.body.appendChild(ov);
    __ceSecoutSync();
    window.addEventListener('scroll',__ceSecoutSync,true);
    window.addEventListener('resize',__ceSecoutSync);
    if(msg) msg.textContent='セクションの境目を表示中（もう一度押すと消えます・保存には残りません）';
  }
  // ===== クイックメニューの並び（ユーザーが自分で並べ替えできる・2026-07-12） =====
  // 並びは localStorage(__ce_qmenu_layout) に ['id','sep','id'...] で保存（'sep'=区切り線）。
  // 新しい機能IDはレイアウト未登録でも自動で末尾に出る＝機能追加してもメニューから消えない。
  var QM_DEFS=[
    ['__ce_q_up','⬆ 外側を選ぶ（枠ごと動かす）'],
    ['__ce_q_txt','✏ 文字を追加（編集）'],
    ['__ce_q_img','🖼 画像を追加（ここに置く）'],
    ['__ce_q_imgswap','🔄 この画像を差し替え（AIなし・一瞬）'],
    ['__ce_q_bgsz','🖼 背景画像の大きさ・位置（AIなし）'],
    ['__ce_q_photo','🖼 写真を加工（フチ・カード・背景など）'],
    ['__ce_q_slide','🖼 スライドショー（画像が次々切り替わる）'],
    ['__ce_q_fx','✨ 動きを付ける（アニメを選ぶ）'],
    ['__ce_q_fly','🕊 線を描いて飛ばす（空飛ぶルート）'],
    ['__ce_q_dly','⏳ 動きの演出（順番・遅らせ・速さ）'],
    ['__ce_q_secout','▦ セクションの境目を表示/隠す（AIなし）'],
    ['__ce_q_ref','📚 お手本を見る（ベース・似た例・アドバイス）'],
    ['__ce_q_dcq','🧐 デザイン指摘をもらう（プロの目・AI数円）'],
    ['__ce_q_brush','🌙 自動磨き（指摘→修正を自動で数周・AI課金）'],
    ['__ce_q_fav','⭐ このセクションをお気に入り（部品保存）'],
    ['__ce_q_secadd','➕ セクションを追加（お気に入りから）'],
    ['__ce_q_secswap','🔀 このセクションを入れ替え'],
    ['__ce_q_secdel','🗑 セクションを削除（一覧から選ぶ）'],
    ['__ce_q_pickov','🎯 重なっている要素から選ぶ（下の層）'],
    ['__ce_q_del','🗑 この要素を削除'],
    ['__ce_q_ovup','⬆ 上に食い込ませる（重ねる・60px）'],
    ['__ce_q_ovdn','⬇ 食い込みを戻す（60px）'],
    ['__ce_q_gaya','💬 がやがや演出（にぎやか吹き出しループ）'],
    ['__ce_q_edge','〰 セクションの境目の形（波・カーブ・斜め）'],
    ['__ce_q_ovshow','📤 切れてる画像を全部見せる（はみ出し許可）'],
    ['__ce_q_zup','🔼 重なりを手前に'],
    ['__ce_q_zdn','🔽 重なりを後ろに'],
    ['__ce_q_align','⁝ 兄弟の行をそろえる（ズレ掃除）'],
    ['__ce_q_frmfit','🧲 掴む枠を見た目の位置に合わせる'],
    ['__ce_q_pskill','➖ 線・飾りを消す（border・疑似要素）'],
    ['__ce_q_addline','➕ 線を追加（実要素・掴んで動かせる）'],
    ['__ce_q_fxrm','🚫 動きを消す'],
    ['__ce_q_rst','⟲ 位置・サイズをリセット'],
    ['__ce_q_unfix','📌 画面への貼り付きを解除（一緒にスクロール）'],
    ['__ce_q_pin','📌 スクロールしても画面に貼り付ける（固定ヘッダー等）'],
    ['__ce_q_secbg','🎨 セクションの背景色を変える（AIなし・即反映）'],
    ['__ce_q_txtbg','🖌 文字の背景に色を塗る（行ごと・AIなし）'],
    ['__ce_q_vline','▎ 文字の左に縦線を引く（引用風・AIなし）'],
    ['__ce_q_deco','🎨 この飾りの色・形・傾きを変える'],
    ['__ce_q_psgrab','🔓 数字や飾り（01など）を掴めるようにする']
  ];
  // ★既定の並び＝見出し(sep:ラベル)入り。この見出しがそのまま「親メニュー」になり、
  //   中身はホバー／クリックで開くサブメニューに畳まれる（項目30個で縦に長すぎた対策・2026-07-21）。
  var QM_DEF_LAYOUT=[
    'sep:➕ 要素を足す・変える','__ce_q_txt','__ce_q_img','__ce_q_imgswap','__ce_q_bgsz','__ce_q_photo','__ce_q_slide','__ce_q_addline','__ce_q_txtbg','__ce_q_vline','__ce_q_deco','__ce_q_psgrab',
    'sep:✨ 動き・演出','__ce_q_fx','__ce_q_fly','__ce_q_dly','__ce_q_gaya',
    'sep:🧩 セクション','__ce_q_secbg','__ce_q_fav','__ce_q_secadd','__ce_q_secswap','__ce_q_secdel','__ce_q_secout','__ce_q_edge',
    'sep:🎯 選ぶ・重なり','__ce_q_up','__ce_q_pickov','__ce_q_zup','__ce_q_zdn','__ce_q_ovup','__ce_q_ovdn','__ce_q_ovshow','__ce_q_pin','__ce_q_unfix',
    'sep:🧹 整える・消す','__ce_q_frmfit','__ce_q_align','__ce_q_pskill','__ce_q_fxrm','__ce_q_rst','__ce_q_del',
    'sep:🤖 AIに頼む','__ce_q_ref','__ce_q_dcq','__ce_q_brush'
  ];
  function qmDefMap(){ var m={}; QM_DEFS.forEach(function(d){ m[d[0]]=d[1]; }); return m; }
  function qmLayoutLoad(){
    var lay=null;
    try{ var a=JSON.parse(localStorage.getItem('__ce_qmenu_layout')||'null'); if(Array.isArray(a)&&a.length) lay=a.slice(); }catch(_){}
    if(!lay) lay=QM_DEF_LAYOUT.slice();
    var m=qmDefMap();
    // 廃止した機能IDは飛ばす。sep=区切り線 / sep:名前=畳むグループ / sepf:名前=畳まない見出し / off:=隠し項目 は残す
    lay=lay.filter(function(k){ return k==='sep'||k.indexOf('sep:')===0||k.indexOf('sepf:')===0||m[k]||(k.indexOf('off:')===0&&m[k.slice(4)]); });
    // 新機能は末尾へ（隠し済みは復活させない）。★末尾が見出しの続きだと新機能がサブメニューに
    //   埋もれて気づけないので、素の区切り線を1本挟んでトップレベルに出す。
    var _add=QM_DEFS.filter(function(d){ return lay.indexOf(d[0])<0&&lay.indexOf('off:'+d[0])<0; });
    if(_add.length){ lay.push('sep'); _add.forEach(function(d){ lay.push(d[0]); }); }
    return lay;
  }
  function qmLayoutSave(a){
    try{ localStorage.setItem('__ce_qmenu_layout',JSON.stringify(a)); }catch(_){}  // まずローカルに即反映（同期）
    // サーバーの共有ファイルにも保存＝Gitで家↔会社が揃う（区切り線・グループが会社でも出る）
    try{
      fetch('/api/menu_layout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({layout:a})})
        .then(function(r){return r.json();})
        .then(function(d){ if(!(d&&d.ok)&&msg) msg.textContent='メニューの並びは保存しましたが、共有ファイルへの書き込みに失敗しました'; })
        .catch(function(){});
    }catch(_){}
  }
  // ★起動時に1回だけ、共有ファイル(サーバー)の並び順をローカルのキャッシュへ流し込む。
  //   qmLayoutLoadは同期で毎回呼ばれるので、非同期fetchはここで済ませてlocalStorageに橋渡しする。
  //   ＝家で作った区切り線が、Git同期後の会社PCでも最初の右クリックから出る。
  (function _syncMenuLayout(){
    try{
      fetch('/api/menu_layout').then(function(r){return r.json();}).then(function(d){
        if(d&&d.ok&&Array.isArray(d.layout)&&d.layout.length){
          try{ localStorage.setItem('__ce_qmenu_layout',JSON.stringify(d.layout)); }catch(_){}
        }
      }).catch(function(){});
    }catch(_){}
  })();
  // ▸ 見出し(sep:ラベル)〜次の見出しまでを1つの「親メニュー」に畳む。
  //   ★サブメニューは position:fixed ＝ メニュー本体(overflow-y:auto)にハサミ切られない。
  //     入れ子のDOMのままなので、既存のクリック処理・_inUI2・closeMenu は一切いじらなくて済む。
  //   ★中身が1個だけの見出しは畳まない（開く手間のほうが大きいので、そのまま並べる）。
  function qmBuildList(m,row){
    var lay=qmLayoutLoad(), out=[], grp=null, gi=0;
    function flush(){
      if(!grp) return;
      var items=grp.items;
      if(items.length===0){ grp=null; return; }
      if(items.length===1){ out.push(items[0]); grp=null; return; }
      out.push('<div class="__ce_grp">'
        +'<button class="__ce_qi __ce_gbtn" data-g="'+gi+'" style="display:flex;width:100%;align-items:center;gap:8px;text-align:left;background:none;border:none;padding:7px 10px;border-radius:7px;cursor:pointer;font-size:13px;font-family:inherit;color:#1d1d1f">'
        +'<span style="flex:1">'+esc(grp.name)+'</span>'
        +'<span style="color:#8a8a90;font-size:11px">'+items.length+'　▸</span></button>'
        +'<div class="__ce_sub" data-g="'+gi+'" style="display:none;position:fixed;left:0;top:0;min-width:250px;max-height:calc(100vh - 20px);overflow-y:auto;background:#f2f2f7;border:1px solid #b9b9c4;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.22);padding:4px;z-index:2147483647">'
        +'<div style="padding:5px 10px 5px;font-size:10.5px;font-weight:700;color:#5b6472;border-bottom:1px solid #dcdce2;margin-bottom:3px">'+esc(grp.name)+'</div>'
        +items.join('')+'</div></div>');
      gi++; grp=null;
    }
    lay.forEach(function(k){
      if(k.indexOf('off:')===0) return;                       // 🙈で隠した項目は出さない
      if(k==='sep'){ flush(); out.push('<div style="border-top:1px solid #b9b9c4;margin:4px 6px"></div>'); return; }
      if(k.indexOf('sepf:')===0){                             // 畳まない見出し＝そのまま並べる（従来の見え方）
        flush();
        out.push('<div style="display:flex;align-items:center;gap:6px;margin:5px 8px;font-size:10.5px;font-weight:700;color:#5b6472">'
          +'<span style="flex:1;border-top:1px solid #b9b9c4"></span><span>'+esc(k.slice(5))+'</span>'
          +'<span style="flex:1;border-top:1px solid #b9b9c4"></span></div>');
        return;
      }
      if(k.indexOf('sep:')===0){ flush(); grp={name:k.slice(4), items:[]}; return; }
      var r=m[k]?row(k,m[k]):'';                              // 今の状況で出さない項目は空文字で返る
      if(!r) return;
      if(grp) grp.items.push(r); else out.push(r);
    });
    flush();
    return out.join('');
  }
  // ▸ 親メニューにホバー／クリックでサブメニューを開く配線
  function qmWireGroups(qm){
    var openG=null, tmr=null;
    function closeAll(){
      [].slice.call(qm.querySelectorAll('.__ce_sub')).forEach(function(s){ s.style.display='none'; });
      [].slice.call(qm.querySelectorAll('.__ce_gbtn')).forEach(function(b){ b.style.background='none'; });
      openG=null;
    }
    function open(g){
      var btn=qm.querySelector('.__ce_gbtn[data-g="'+g+'"]'), sub=qm.querySelector('.__ce_sub[data-g="'+g+'"]');
      if(!btn||!sub) return;
      closeAll();
      var r=btn.getBoundingClientRect();
      sub.style.display='block';
      var w=sub.offsetWidth, h=sub.offsetHeight;
      var left=r.right+2; if(left+w>innerWidth-6) left=Math.max(6, r.left-w-2);   // 右が狭ければ左に出す
      var top=r.top-4;    if(top+h>innerHeight-6) top=Math.max(6, innerHeight-h-6);
      sub.style.left=left+'px'; sub.style.top=top+'px';
      btn.style.background='#e3e3ea';
      openG=g;
    }
    [].slice.call(qm.querySelectorAll('.__ce_gbtn')).forEach(function(b){
      b.addEventListener('mouseenter',function(){ clearTimeout(tmr); open(b.getAttribute('data-g')); });
      b.addEventListener('click',function(ev){
        ev.stopPropagation(); clearTimeout(tmr);              // クリックでも開ける（ホバーが苦手な人向け）
        if(openG===b.getAttribute('data-g')) closeAll(); else open(b.getAttribute('data-g'));
      });
    });
    // サブメニューはDOM上はメニューの子なので、そこへ移動しても qm の mouseleave は起きない
    [].slice.call(qm.querySelectorAll('.__ce_sub')).forEach(function(s){
      s.addEventListener('mouseenter',function(){ clearTimeout(tmr); });
      s.addEventListener('mouseleave',function(){ clearTimeout(tmr); tmr=setTimeout(closeAll,280); });
    });
    qm.addEventListener('mouseenter',function(){ clearTimeout(tmr); });
    qm.addEventListener('mouseleave',function(){ clearTimeout(tmr); tmr=setTimeout(closeAll,280); });
  }
  // ⚙並べ替えモード：↑↓で移動・─区切り線の追加/削除・⟲初期に戻す・✔完了で保存
  function qmEditMode(qm){
    var lay=qmLayoutLoad(), m=qmDefMap();
    // ★グループ（サブメニュー）を目で分かるようにする：グループ名の下にぶら下がる項目は
    //   左に線を引いて字下げする＝「どれがどのグループに入っているか」が一目で分かる。
    function html(){
      var open=null, cnt=[];                                  // 各行が属するグループ名（先に数えて件数を出す）
      lay.forEach(function(k){
        if(k==='sep'||k.indexOf('sepf:')===0){ open=null; cnt.push(null); return; }
        if(k.indexOf('sep:')===0){ open={n:0}; cnt.push(open); return; }
        if(open&&k.indexOf('off:')!==0) open.n++;
        cnt.push(open);
      });
      // ★先頭に「掴んで動かせるヘッダー」＝パネルが縦に長く画面外に出た時、これを掴んで上へ動かせる。
      //   position:sticky で中をスクロールしても常に見える＝どこにいても掴める。
      return '<div id="__ce_qe_bar" style="position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:6px;'
        +'padding:7px 10px;margin:-4px -4px 4px;background:#e9eef7;border-bottom:1px solid #c7d2e2;border-radius:9px 9px 0 0;cursor:grab;user-select:none">'
        +'<span style="color:#7a8aa0;font-size:14px">⠿</span><b style="flex:1;font-size:12.5px;color:#2a3550">📋 メニューの設定</b>'
        +'<span style="font-size:10px;color:#8a94a8">↕ ここを掴んで動かせます</span></div>'
        +'<div style="padding:0 10px 6px;font-size:11px;color:#666;line-height:1.75">'
        +'<b>📁 グループ</b>を作ると、その下の項目が<b>サブメニュー</b>（▸ホバーで開く）にまとまります。<br>'
        +'次のグループ名（または区切り線）までが中身です。⠿ドラッグ／↑↓で入れ替え・🙈で隠せます。</div>'
        +lay.map(function(k,i){
          var isGrp=(k.indexOf('sep:')===0), isFlat=(k.indexOf('sepf:')===0), isLine=(k==='sep');
          var isSep=(isGrp||isFlat||isLine);
          var isOff=(k.indexOf('off:')===0);  // 🙈隠し中の項目＝薄く＋取り消し線で出す（👁で戻せる）
          var inGrp=(!isSep&&cnt[i]);
          var lbl;
          if(isLine){
            lbl='<input data-seplb="1" value="" placeholder="─ 区切り線（名前を書くとグループになります）" style="flex:1;min-width:0;font-size:11px;color:#666;border:1px dashed #bbb;border-radius:5px;padding:2px 6px;font-family:inherit">';
          } else if(isSep){
            lbl='<input data-seplb="1" value="'+esc(isGrp?k.slice(4):k.slice(5))+'" placeholder="グループ名" style="flex:1;min-width:0;font-size:11.5px;font-weight:700;color:#33415c;border:1px solid #c9d3e0;background:#fff;border-radius:5px;padding:3px 6px;font-family:inherit">'
              +'<button data-gk="1" title="'+(isGrp?'今：サブメニューに畳む（押すと開いたまま並べる）':'今：畳まず並べる（押すとサブメニューにする）')+'" style="height:22px;border:none;background:'+(isGrp?'#dbeafe':'#f0f0f2')+';color:#33415c;border-radius:5px;cursor:pointer;padding:0 7px;font-size:11px;white-space:nowrap">'
              +(isGrp?('▸ 畳む'+(cnt[i]&&cnt[i].n?'（'+cnt[i].n+'）':'')):'▾ 畳まない')+'</button>';
          } else {
            lbl='<span style="flex:1;font-size:12px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;'+(isOff?'opacity:.38;text-decoration:line-through':'')+'">'+m[isOff?k.slice(4):k]+'</span>';
          }
          return '<div data-i="'+i+'" style="display:flex;align-items:center;gap:4px;padding:3px 8px 3px '+(inGrp?'22px':'8px')+';'+(inGrp?'border-left:3px solid #c9d3e0;margin-left:14px':'')+'">'
            +'<span data-dh="1" draggable="true" title="ドラッグで移動" style="cursor:grab;color:#aaa;font-size:13px;padding:0 2px;user-select:none">⠿</span>'
            +'<button data-mv="-1" style="width:22px;height:20px;border:none;background:#f0f0f2;border-radius:5px;cursor:pointer;padding:0">↑</button>'
            +'<button data-mv="1" style="width:22px;height:20px;border:none;background:#f0f0f2;border-radius:5px;cursor:pointer;padding:0">↓</button>'
            +lbl
            +(isSep
              ?'<button data-del="1" title="この区切り／グループ名を消す（中の項目は残ります）" style="width:22px;height:20px;border:none;background:#fde8e8;color:#c00;border-radius:5px;cursor:pointer;padding:0">✕</button>'
              :'<button data-hide="1" title="'+(isOff?'メニューに戻す':'メニューから隠す')+'" style="width:26px;height:20px;border:none;background:'+(isOff?'#e8f4e8':'#f0f0f2')+';border-radius:5px;cursor:pointer;padding:0">'+(isOff?'👁':'🙈')+'</button>')
            +'</div>';
        }).join('')
        +'<div style="padding:2px 10px 6px;font-size:10.5px;color:#8a8a90">※ 中身が1個だけのグループは、開く手間が増えるだけなので畳まずに出します。</div>'
        // ★ボタン列は下に貼り付け（sticky）＝いくらスクロールしても「✔完了」が常に見える＝押せずに迷子にならない。
        +'<div style="position:sticky;bottom:0;display:flex;gap:6px;padding:7px 8px;flex-wrap:wrap;background:#eef1f7;border-top:1px solid #c7d2e2;margin:0 -4px -4px;border-radius:0 0 9px 9px">'
        +'<button id="__ce_qe_grp" style="border:none;background:#2f6bff;color:#fff;border-radius:6px;padding:3px 9px;font-size:12px;cursor:pointer;font-weight:700">📁 グループを追加</button>'
        +'<button id="__ce_qe_sep" style="border:1px solid #ccc;background:#fff;border-radius:6px;padding:3px 8px;font-size:12px;cursor:pointer">─ 区切り線を追加</button>'
        +'<button id="__ce_qe_rst" style="border:1px solid #ccc;background:#fff;border-radius:6px;padding:3px 8px;font-size:12px;cursor:pointer">⟲ 初期に戻す</button>'
        +'<button id="__ce_qe_ok" style="border:none;background:#22c55e;color:#fff;border-radius:6px;padding:3px 12px;font-size:12px;cursor:pointer;font-weight:700;margin-left:auto">✔ 完了</button>'
        +'</div>';
    }
    qm.innerHTML=html();
    // ★編集パネルは縦に長い＝右クリック位置によっては下端（✔完了）が画面外に出る。
    //   固定表示にして画面上部へ寄せ、高さも画面に収める（中はスクロール）＝必ずボタンに届く。
    qm.style.position='fixed';
    qm.style.left=Math.max(6,Math.min(parseFloat(qm.style.left)||60, window.innerWidth-360))+'px';
    qm.style.top='8px';
    qm.style.maxHeight=(window.innerHeight-16)+'px';
    qm.style.overflowY='auto';
    if(!qm.__qeBound){
      qm.__qeBound=true;
      // ⠿ヘッダーを掴んでパネルごと移動（画面外に出た時に上へ引っぱれる）。position:fixed基準で動かす。
      //   ★document側のリスナーは「開くたびに増やさない」＝1回だけ束ねて、動かす対象(_qmDrag.qm)を差し替える。
      qm.addEventListener('mousedown',function(ev){
        if(!qm.__qeOn) return;
        var bar=ev.target.closest&&ev.target.closest('#__ce_qe_bar'); if(!bar) return;
        ev.preventDefault();
        var r=qm.getBoundingClientRect();
        window.__qmDrag={qm:qm, dx:ev.clientX-r.left, dy:ev.clientY-r.top};
        bar.style.cursor='grabbing';
      });
      if(!window.__qmDragBound){
        window.__qmDragBound=true;
        document.addEventListener('mousemove',function(ev){
          var d=window.__qmDrag; if(!d||!d.qm||!d.qm.isConnected) return;
          var x=Math.max(6,Math.min(ev.clientX-d.dx, window.innerWidth-d.qm.offsetWidth-6));
          var y=Math.max(6,Math.min(ev.clientY-d.dy, window.innerHeight-40));   // 少し残して掴み直せる
          d.qm.style.left=x+'px'; d.qm.style.top=y+'px';
        });
        document.addEventListener('mouseup',function(){
          var d=window.__qmDrag; if(!d) return;
          if(d.qm){ var bar=d.qm.querySelector('#__ce_qe_bar'); if(bar) bar.style.cursor='grab'; }
          window.__qmDrag=null;
        });
      }
      // ⠿ドラッグで並べ替え（↑↓ボタンと併用可）。落とす位置は行の上半分＝前、下半分＝後ろ。
      var _dragI=-1;
      qm.addEventListener('dragstart',function(ev){
        if(!qm.__qeOn) return;
        var h=ev.target; if(!h.getAttribute||!h.getAttribute('data-dh')){ ev.preventDefault(); return; }
        var rowEl=h.closest('[data-i]'); if(!rowEl){ ev.preventDefault(); return; }
        _dragI=+rowEl.getAttribute('data-i');
        try{ ev.dataTransfer.setData('text/plain',''+_dragI); ev.dataTransfer.effectAllowed='move'; }catch(_){}
      });
      qm.addEventListener('dragover',function(ev){
        if(!qm.__qeOn||_dragI<0) return;
        var rowEl=ev.target.closest?ev.target.closest('[data-i]'):null; if(!rowEl) return;
        ev.preventDefault(); try{ ev.dataTransfer.dropEffect='move'; }catch(_){}
        [].forEach.call(qm.querySelectorAll('[data-i]'),function(x){ x.style.borderTop=''; x.style.borderBottom=''; });
        var r=rowEl.getBoundingClientRect();
        if(ev.clientY>r.top+r.height/2) rowEl.style.borderBottom='2px solid #2f6bff'; else rowEl.style.borderTop='2px solid #2f6bff';
      });
      qm.addEventListener('drop',function(ev){
        if(!qm.__qeOn||_dragI<0) return;
        var rowEl=ev.target.closest?ev.target.closest('[data-i]'):null; if(!rowEl) return;
        ev.preventDefault();
        var j=+rowEl.getAttribute('data-i');
        var r=rowEl.getBoundingClientRect();
        if(ev.clientY>r.top+r.height/2) j+=1;
        var it=lay.splice(_dragI,1)[0];
        if(j>_dragI) j-=1;
        lay.splice(j,0,it);
        _dragI=-1; qm.innerHTML=html();
      });
      qm.addEventListener('dragend',function(){ _dragI=-1; [].forEach.call(qm.querySelectorAll('[data-i]'),function(x){ x.style.borderTop=''; x.style.borderBottom=''; }); });
      // 区切り線の見出し入力＝打った瞬間にlayへ同期（↑↓で再描画されても消えないように）
      qm.addEventListener('input',function(ev){
        if(!qm.__qeOn) return;
        var t=ev.target; if(!t.getAttribute||!t.getAttribute('data-seplb')) return;
        var rowEl=t.closest('[data-i]'); if(!rowEl) return;
        var i=+rowEl.getAttribute('data-i'), v=(t.value||'').trim();
        var flat=(lay[i].indexOf('sepf:')===0);              // 「畳まない」設定は名前を打ち替えても保つ
        lay[i]= v ? ((flat?'sepf:':'sep:')+v) : 'sep';       // 名前を消したら ただの区切り線に戻る
      });
      qm.addEventListener('click',function(ev){
        if(!qm.__qeOn) return;
        ev.stopPropagation();
        var t=ev.target;
        if(t.id==='__ce_qe_sep'){ lay.push('sep'); qm.innerHTML=html(); return; }
        if(t.id==='__ce_qe_grp'){
          lay.push('sep:新しいグループ'); qm.innerHTML=html();
          // 追加した名前欄をすぐ書き換えられるように選択状態で待つ（名前を決めるのが最初の作業なので）
          var ins=qm.querySelectorAll('input[data-seplb]'); var last=ins[ins.length-1];
          if(last){ last.focus(); last.select(); last.scrollIntoView({block:'nearest'}); }
          return;
        }
        if(t.id==='__ce_qe_rst'){
          try{ localStorage.removeItem('__ce_qmenu_layout'); }catch(_){}
          // 共有ファイルも既定に戻す（既定レイアウトをそのまま保存＝会社PCでも初期化が揃う）
          qmLayoutSave(QM_DEF_LAYOUT.slice());
          try{ localStorage.removeItem('__ce_qmenu_layout'); }catch(_){}  // 保存直後に消して「既定」を読ませる
          lay=qmLayoutLoad(); qm.innerHTML=html(); return;
        }
        if(t.id==='__ce_qe_ok'){ qmLayoutSave(lay); qm.__qeOn=false; closeMenu(); if(msg) msg.textContent='メニューの並びを保存しました（次の右クリックから反映）'; return; }
        var rowEl=t.closest('[data-i]'); if(!rowEl) return;
        var i=+rowEl.getAttribute('data-i');
        if(t.getAttribute('data-gk')){                        // ▸畳む ⇄ ▾畳まない の切り替え
          lay[i]=(lay[i].indexOf('sepf:')===0)?('sep:'+lay[i].slice(5)):('sepf:'+lay[i].slice(4));
          qm.innerHTML=html(); return;
        }
        if(t.getAttribute('data-del')){ lay.splice(i,1); qm.innerHTML=html(); return; }
        if(t.getAttribute('data-hide')){ lay[i]=(lay[i].indexOf('off:')===0)?lay[i].slice(4):('off:'+lay[i]); qm.innerHTML=html(); return; }
        var mv=t.getAttribute('data-mv');
        if(mv){ var j=i+(+mv); if(j<0||j>=lay.length) return; var it=lay.splice(i,1)[0]; lay.splice(j,0,it); qm.innerHTML=html(); }
      });
    }
    qm.__qeOn=true;
  }
  function openQuickMenu(e){
    // ★右クリック座標は関数の先頭で確保する。以前は下のほうで var qx=… していたため、
    //   上のメニュー組み立てで使うと undefined のまま elementsFromPoint に渡って
    //   「非有限の値」で例外→**右クリックメニューが丸ごと出ない**事故になった（2026-07-21）。
    var qx=e.clientX, qy=e.clientY;
    var multi=selEls.length>1;
    // 文字の「追加/編集」の自動分岐：中に文字があれば編集、余白（文字なし・大きな器・画像）なら追加。
    // 文字はアニメ用ラッパーdivや1文字ずつのspanに包まれていることがあるので、子孫込み(textContent)で判定する
    var _hasTxt=!!((curEl.textContent||'').trim());
    var _rq=curEl.getBoundingClientRect();
    var _tooBig=(_rq.width*_rq.height)>(window.innerWidth*window.innerHeight*0.5);  // 画面の半分超の箱＝余白扱い
    var addMode=!_hasTxt || _tooBig || /^(SECTION|MAIN|HEADER|FOOTER|BODY|HTML|IMG)$/.test(curEl.tagName);
    var qm=document.createElement('div'); qm.id='__ce_cm';
    qm.setAttribute('style','width:auto;min-width:215px;padding:4px;max-height:calc(100vh - 16px);overflow-y:auto');  // 項目が増えて画面より長い時はメニュー内スクロール
    function row(id,label){
      var kk=scKeyOf(id);  // この項目にショートカットキーが割り当ててあれば末尾に [t] を出す（見つけやすく）
      var badge=kk?' <span style="float:right;margin-left:8px;font-size:11px;font-weight:700;color:#6b7280;background:#eef0f4;border:1px solid #d7dae1;border-radius:4px;padding:0 5px">'+esc(kk)+'</span>':'';
      return '<button class="__ce_qi" id="'+id+'" style="display:block;width:100%;text-align:left;background:none;border:none;padding:7px 10px;border-radius:7px;cursor:pointer;font-size:13px;font-family:inherit;color:#1d1d1f">'+label+badge+'</button>'; }
    // ✂ Alt+ドラッグで文字を選択してから右クリック＝選択への操作（色・マーカー・下線）を最上部に出す
    // （以前は選択直後に黒い小ポップアップが出ていた→2026-07-11にこのメニューへ一本化）
    var selApiQ=window.__ceSel, selRowQ='';
    if(selApiQ && selApiQ.has()){
      var _stq=(selApiQ.text()||'').replace(/\\s+/g,' ').trim();
      qm.style.minWidth='268px';
      // ページで実際に使われている文字色（頻度順10色）＝ここから選べば色がバラバラにならない
      var _qsw='';
      try{
        var _qcnt={};
        [].slice.call(document.querySelectorAll('body *')).slice(0,1500).forEach(function(el){
          if(el.closest('[id^="__ce"]')) return;
          if(!(el.textContent||'').trim()) return;
          var c; try{ c=getComputedStyle(el).color; }catch(_){ return; }
          if(c) _qcnt[c]=(_qcnt[c]||0)+1;
        });
        _qsw=Object.keys(_qcnt).sort(function(a,b){return _qcnt[b]-_qcnt[a];}).slice(0,10)
          .map(function(c){ return '<button class="__ce_q_selsw" data-c="'+c+'" title="'+c+'（ページで使用中）" style="width:16px;height:16px;border:1px solid rgba(0,0,0,.15);border-radius:4px;background:'+c+';cursor:pointer;padding:0;vertical-align:middle;margin-right:3px"></button>'; }).join('');
      }catch(_){ }
      selRowQ='<div style="background:#fff7d6;border-bottom:1px solid #f3e2a0;padding:6px 10px 7px;font-size:12px;line-height:2.1;border-radius:7px">'
        +'<b>✂ 選択中「'+esc(_stq.slice(0,10))+(_stq.length>10?'…':'')+'」</b>（AIなし）<br>'
        +(_qsw?'<span style="opacity:.8">ページの色</span> '+_qsw+'<br>':'')
        +'<span style="opacity:.8">サイズ</span> <button id="__ce_q_fsm" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:2px 8px;cursor:pointer">−小さく</button> <button id="__ce_q_fsp" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:2px 8px;cursor:pointer">＋大きく</button><br>'
        +'<span style="opacity:.8">字間</span> <button id="__ce_q_spm" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:2px 8px;cursor:pointer">−せまく</button> <button id="__ce_q_spp" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:2px 8px;cursor:pointer">＋ひろく</button><br>'
        +'<span style="opacity:.8">文字色</span><input type="color" id="__ce_q_selc" value="#e05656" style="width:28px;height:21px;padding:0;border:none;border-radius:4px;vertical-align:middle;cursor:pointer"> '
        +'🖍<input type="color" id="__ce_q_selhlc" value="'+hlDefaultColor()+'" style="width:28px;height:21px;padding:0;border:none;border-radius:4px;vertical-align:middle;cursor:pointer"><button id="__ce_q_selhlb" style="background:#eab308;border:none;border-radius:5px;padding:2px 8px;cursor:pointer;font-weight:700">'+(selApiQ.hasHl()?'マーカーを消す':'マーカー')+'</button> '
        +'〰<input type="color" id="__ce_q_seludc" value="#e07856" style="width:28px;height:21px;padding:0;border:none;border-radius:4px;vertical-align:middle;cursor:pointer"><button id="__ce_q_seludb" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:2px 8px;cursor:pointer">'+(selApiQ.hasUd()?'下線を消す':'下線')+'</button>'
        +'<label title="ON=スクロールで左からスーッと走る／OFF=最初から引かれた静止下線" style="font-size:11px;margin-left:3px;cursor:pointer;user-select:none"><input type="checkbox" id="__ce_q_seluda"'+((localStorage.getItem('__ce_ud_anim')||'1')!=='0'?' checked':'')+' style="vertical-align:middle;cursor:pointer">走る</label>'
        +'</div>';
    }
    // 🖍/〰 右クリックした場所にマーカー・下線があれば「消す」を最上部に出す（AIなし）。
    // ★文字を選び直さなくても消せるのが肝：選択に頼ると「選び方によっては消すボタンが出ない」
    //   （実際に「マーカーがあるのに消せない」報告あり）
    var decoQ=decoScan(curEl), decoRowQ='';
    if(decoQ.length){
      decoRowQ='<div style="background:#fff1e6;border-bottom:1px solid #f4d5bb;padding:6px 10px 7px;font-size:12px;border-radius:7px">'
        +'<b>➖ 見えている飾りを消す</b>（'+decoQ.length+'件・AIなし）<br>'
        +'<span style="font-size:10.5px;color:#8a7a6a">ボタンに触れると赤枠でどれか分かります／押すと消える・もう一度押すと戻る</span>'
        +decoQ.map(function(it,i){
          // 絵の飾りはサムネイルを出す＝「どれが気球か」が一目で分かる（疑似要素は実体が無く名前では選べない）
          var th=it.img?('<img src="'+esc(it.img)+'" style="width:26px;height:26px;object-fit:contain;vertical-align:middle;margin-right:6px;background:#fff;border:1px solid #e6c8ae;border-radius:4px">'):'';
          return '<button class="__ce_dcz" data-i="'+i+'" style="display:flex;align-items:center;width:100%;text-align:left;margin:3px 0 0;background:#fff;border:1px solid #e6c8ae;border-radius:6px;padding:3px 8px;cursor:pointer;font-size:11.5px;font-family:inherit;color:#1d1d1f">'+th+'<span>'+esc(it.name)+'</span></button>';
        }).join('')
        +'</div>';
    }
    // 🎨 背景・フチ・影を消す（AIなし・2026-07-28）＝色つきカードをその場で透明にする。
    // ★「見つかったこと」の中ではなく本命メニュー側に置く：⑲の教訓（発見できない機能は無い機能と同じ）
    var flatQ=flatScan(curEl), flatRowQ='';
    if(flatQ.length){
      flatRowQ='<div style="background:#f6f0ff;border-bottom:1px solid #ddd0f2;padding:6px 10px 7px;font-size:12px;border-radius:7px">'
        +'<b>🎨 背景・フチを消す</b>（'+flatQ.length+'件・AIなし）<br>'
        +'<span style="font-size:10.5px;color:#7a6a8a">触れると赤枠でどれか分かります／押すと透明になる・もう一度押すと戻る。🌸は後ろに浮いている飾り（ピンクの丸など右クリックで掴めないもの）</span>'
        +flatQ.map(function(it,i){
          function bt(k,t,on){
            return on?('<button class="__ce_flz" data-i="'+i+'" data-k="'+k+'" style="background:#fff;border:1px solid #d3c2ee;border-radius:6px;padding:2px 7px;cursor:pointer;font-size:11px;font-family:inherit;color:#1d1d1f;white-space:nowrap">'+t+'</button>'):'';
          }
          // 色の四角＝「このピンクのこと」が言葉なしで分かる（浮いてる飾りは実体を指せないので特に重要）
          var sw=it.sw?('<span style="flex:none;width:13px;height:13px;border-radius:3px;border:1px solid rgba(0,0,0,.2);background:'+it.sw+'"></span>'):'';
          return '<div style="display:flex;align-items:center;gap:4px;margin:4px 0 0">'
            +sw
            +'<span style="flex:1;font-size:11px;color:#1d1d1f;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(it.name+it.tag)+'">'+esc(it.name+it.tag)+'</span>'
            +bt('bg','🎨背景',it.bg)+bt('bd','▭フチ',it.bd)+bt('sh','☁影',it.sh)
            +(it.deco
              ?'<button class="__ce_flz" data-i="'+i+'" data-k="vis" style="background:#d6455c;border:none;border-radius:6px;padding:2px 7px;cursor:pointer;font-size:11px;font-family:inherit;color:#fff;white-space:nowrap">🚫この飾りを消す</button>'
              :'<button class="__ce_flz" data-i="'+i+'" data-k="all" style="background:#7c5cd6;border:none;border-radius:6px;padding:2px 7px;cursor:pointer;font-size:11px;font-family:inherit;color:#fff;white-space:nowrap">✨全部</button>')
            +'</div>';
        }).join('')+'</div>';
    }
    // ◽ 角丸の写真の「裏の四角」＝直角のケースが角からはみ出している時、⌒丸く/✕消すで隠す（AIなし）
    var radQ=radiusScan(curEl), radRowQ='';
    if(radQ.length){
      radRowQ='<div style="background:#eef6ff;border-bottom:1px solid #c9dcf5;padding:6px 10px 7px;font-size:12px;border-radius:7px">'
        +'<b>◽ 裏の四角のカドを隠す</b>（'+radQ.length+'件・AIなし）<br>'
        +'<span style="font-size:10.5px;color:#6a7a8a">角丸の写真の裏で直角の角がはみ出している箱です。触れると赤枠／⌒丸くする か ✕見た目を消す（もう一度で戻る）</span>'
        +radQ.map(function(it,i){
          return '<div style="display:flex;align-items:center;gap:5px;margin:4px 0 0">'
            +'<span style="flex:1;font-size:11px;color:#1d1d1f;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(it.name)+(it.cur?'（今 '+it.cur+'px）':'')+'</span>'
            +'<button class="__ce_rrz" data-i="'+i+'" style="background:#fff;border:1px solid #bcd0ee;border-radius:6px;padding:3px 8px;cursor:pointer;font-size:11px;font-family:inherit;color:#1d1d1f;white-space:nowrap">⌒ 角丸 '+it.model+'px</button>'
            +'<button class="__ce_rhz" data-i="'+i+'" style="background:#fff;border:1px solid #e6bcbc;border-radius:6px;padding:3px 8px;cursor:pointer;font-size:11px;font-family:inherit;color:#b23;white-space:nowrap">✕ 消す</button>'
            +'</div>';
        }).join('')
        +'</div>';
    }
    // 🫥 クリックがすり抜ける絵（pointer-events:none）がこの位置にあるなら「選ぶ」ボタンを出す。
    // ★これが無いと「見えているのに右クリックでは絶対に掴めない絵」になる（気球のイラストで発覚）。
    //   勝手に掴み替えると文字クリックを横取りするので、必ずユーザーに選ばせる。
    // ★ピッタリ絵の上で右クリックしなくても見つかるように、選んだ器の中も舐める（「どこ？」対策）。
    //   ①クリック地点の真上にある絵 ②選んだ器の中にある絵 を、クリックに近い順で並べる。
    var peQ=_peNoneAt(e.clientX, e.clientY);
    if(curEl && curEl.querySelectorAll){
      _peScan(curEl, e.clientX, e.clientY, false).forEach(function(n){ if(peQ.indexOf(n)<0) peQ.push(n); });
    }
    peQ=peQ.filter(function(n){ return n!==curEl; }).slice(0,4);
    var peRowQ='';
    if(peQ.length){
      peRowQ='<div style="background:#eef2ff;border-bottom:1px solid #c9d2f5;padding:6px 10px 7px;font-size:12px;border-radius:7px">'
        +'<b>🫥 「クリックがすり抜ける絵」があります</b><br>'
        +'<span style="font-size:10.5px;color:#6b7280">普通の右クリックでは掴めない設定の絵です。触れると赤枠でどれか分かります／押すと掴めるようになります</span>'
        +peQ.map(function(n,i){
          var src=(n.getAttribute&&n.getAttribute('src')||'').split('/').pop();
          var r2=n.getBoundingClientRect();
          return '<button class="__ce_pez" data-i="'+i+'" style="display:block;width:100%;text-align:left;margin:3px 0 0;background:#fff;border:1px solid #c3cdf0;border-radius:6px;padding:3px 8px;cursor:pointer;font-size:11.5px;font-family:inherit;color:#1d1d1f">'
            +esc(n.tagName.toLowerCase()+(src?'（'+src+'）':'')+' '+Math.round(r2.width)+'×'+Math.round(r2.height))+'</button>';
        }).join('')
        +'</div>';
    }
    // 🕳 ドラッグで空いた「穴」＝📏では絶対に消せない空白。見つけたら最優先で出す
    var dgQ=dragHoles(), dgRowQ='';
    if(dgQ.length){
      dgRowQ='<div style="background:#fff7ed;border-bottom:1px solid #f2d0a4;padding:6px 10px 7px;font-size:12px;border-radius:7px">'
        +'<b>🕳 ドラッグで空いた「穴」が '+dgQ.length+'個 あります</b>（AIなし）<br>'
        +'<span style="font-size:10.5px;color:#8a6a4a">動かした跡に空白が残っています。📏では消せません</span>'
        +'<div style="margin:3px 0 4px;font-size:10.5px;color:#7a5a3a;line-height:1.8">'
        +dgQ.map(function(o,i){ return '<div class="__ce_dgz" data-i="'+i+'" style="cursor:default">'+(i===0?'<b>▶ ':'　')
            +esc(o.name)+'</b>：縦に '+o.ty+'px（高さ'+o.h+'px）'
            +(o.done?' <span style="color:#15803d;font-weight:700">✅埋め済み</span>':'')+'</div>'; }).join('')
        +'</div>'
        +'<button id="__ce_dg_fix" style="background:#ea580c;color:#fff;border:none;border-radius:5px;padding:3px 10px;cursor:pointer;font-size:11.5px;font-weight:700">🕳 穴を埋める（見た目はそのまま）</button> '
        +'<button id="__ce_dg_rst" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:3px 8px;cursor:pointer;font-size:11.5px">⟲ 元に戻す</button>'
        +'<span id="__ce_dg_now" style="margin-left:6px;font-size:11px;color:#8a6a4a"></span>'
        +'</div>';
    }
    // ➡ 横はみ出し（＝ページの右側に上から下まで余白ができる）。犯人は画面外に居て掴めないので一覧で名指しする
    var owQ=overflowScan(), owDX=ovDragXCount(), owSK=stickyScan(), owRowQ='';
    if(owQ.length || owDX || owSK.length){
      var _de=document.documentElement, _owGap=_de.scrollWidth-_de.clientWidth;
      owRowQ='<div style="background:#fef2f2;border-bottom:1px solid #f0c4c4;padding:6px 10px 7px;font-size:12px;border-radius:7px">'
        +'<b>➡ 横のズレ・はみ出し</b>（AIなし）<br>'
        +'<span style="font-size:10.5px;color:#8a6a6a">「右側に上から下まで余白ができる」の正体です。犯人は画面の外に居るので手では選べません</span>'
        +'<div style="margin:3px 0 4px;font-size:10.5px;color:#7a5a5a;line-height:1.8">'
        +'<div>今の画面（'+_de.clientWidth+'px）でのはみ出し：<b>'+(_owGap>0?_owGap:0)+'px</b>'
        +(owDX?('　／　右へドラッグされたまま：<b>'+owDX+'箇所</b>'):'')+'</div>'
        +owSK.map(function(o){ return '<div>🧢 <b>'+esc(ovName(o.el))+'</b>（画面に貼り付く帯）が左'+o.left+'〜右'+o.right+'px'
            +(o.pxw?'・幅が'+o.w+'pxで固定されています':'')+'</div>'; }).join('')
        +owQ.map(function(o,i){ return '<div class="__ce_owz" data-i="'+i+'" style="cursor:default">'+(i===0?'<b>▶ ':'　')
            +esc(ovName(o.el))+'</b>：右に '+o.over+'px'+(o.drag?'（ドラッグで動かされています）':(o.wide?'（画面より広い）':''))+'</div>'; }).join('')
        +'</div>'
        +(owSK.length?'<button id="__ce_ow_sk" style="background:#0e7490;color:#fff;border:none;border-radius:5px;padding:3px 10px;cursor:pointer;font-size:11.5px;font-weight:700" title="焼き込まれた幅・横位置だけ剥がして、ページ本来のCSSに戻す">🧢 ヘッダー等を元の位置に戻す（'+owSK.length+'）</button> ':'')
        +(owDX?'<button id="__ce_ow_dx" style="background:#b91c1c;color:#fff;border:none;border-radius:5px;padding:3px 10px;cursor:pointer;font-size:11.5px;font-weight:700" title="どの画面幅で見ても再発しない直し方">⟲ 横ドラッグのズレを全部戻す（'+owDX+'）</button> ':'')
        +(owQ.length?'<button id="__ce_ow_fix" style="background:#dc2626;color:#fff;border:none;border-radius:5px;padding:3px 10px;cursor:pointer;font-size:11.5px;font-weight:700">➡ 今の幅のはみ出しを直す</button> ':'')
        +'<button id="__ce_ow_rst" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:3px 8px;cursor:pointer;font-size:11.5px">⟲ 元に戻す</button>'
        +'<div style="font-size:10px;color:#9a8a8a;margin-top:3px">※「はみ出しを直す」は今の画面幅が基準です。狭い画面でも直したいときは、先に<b>横ドラッグのズレを全部戻す</b>を押してください</div>'
        +'</div>';
    }
    // 📏 ムダな余白がある箱で右クリックしたら「詰める」を出す（数字つきで原因を名指し）
    var pdQ=padChain(curEl), pdRowQ='';
    if(pdQ.length){
      pdRowQ='<div style="background:#fff5f5;border-bottom:1px solid #f3cccc;padding:6px 10px 7px;font-size:12px;border-radius:7px">'
        +'<b>📏 余白の原因が '+pdQ.length+'個 見つかりました</b>（AIなし）<br>'
        +'<span style="font-size:10.5px;color:#8a6a6a">大きい箱ほど効きます。押すと全部まとめて外します</span>'
        +'<div style="margin:3px 0 4px;font-size:10.5px;color:#7a5a5a;line-height:1.8">'
        +pdQ.map(function(o,i){ return '<div>'+(i===0?'<b>▶ ':'　')+esc(o.name)+'</b>：'+esc(o.why)+'（高さ'+o.h+'px）'+(i===0?'':'')+'</div>'; }).join('')
        +'</div>'
        +'<button id="__ce_pd_fit" style="background:#dc2626;color:#fff;border:none;border-radius:5px;padding:3px 10px;cursor:pointer;font-size:11.5px;font-weight:700">⬍ まとめて詰める</button> '
        +'<button id="__ce_pd_half" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:3px 8px;cursor:pointer;font-size:11.5px">▁ 上下の余白も半分</button> '
        +'<button id="__ce_pd_rst" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:3px 8px;cursor:pointer;font-size:11.5px">⟲ 元に戻す</button>'
        +'<span id="__ce_pd_now" style="margin-left:6px;font-size:11px;color:#8a6a6a"></span>'
        +'</div>';
    }
    // 🎞 スライドショーの中で右クリックしたら「止めて1枚に固定」を出す（サムネで選べる）
    var slQ=_sliderAt(curEl), slRowQ='';
    if(slQ){
      slRowQ='<div style="background:#eefaf3;border-bottom:1px solid #bfe6d3;padding:6px 10px 7px;font-size:12px;border-radius:7px">'
        +'<b>🎞 スライドショー（'+slQ.items.length+'枚・AIなし）</b>'+(slQ.frozen?'<span style="color:#0f766e;font-weight:700"> 固定中</span>':'')+'<br>'
        +'<span style="font-size:10.5px;color:#6b7280">切り替わりを止めて、見せたい1枚に固定できます（クリックで選ぶ）</span><br>'
        +'<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:4px">'
        +slQ.items.map(function(s,i){
          var th=_slideThumb(s), on=s.classList.contains('cefreeze-on');
          return '<button class="__ce_slz" data-i="'+i+'" title="'+(i+1)+'枚目に固定" style="width:46px;height:36px;padding:0;border:2px solid '+(on?'#0f766e':'#cfe6db')+';border-radius:5px;background:#fff;cursor:pointer;overflow:hidden">'
            +(th?'<img src="'+esc(th)+'" style="width:100%;height:100%;object-fit:contain">':'<span style="font-size:11px">'+(i+1)+'</span>')+'</button>';
        }).join('')
        +'</div>'
        +(slQ.frozen?'<button id="__ce_slzoff" style="margin-top:5px;background:#64748b;color:#fff;border:none;border-radius:5px;padding:3px 10px;cursor:pointer;font-size:11.5px">▶ 切り替わりに戻す</button>':'')
        +'</div>';
    }
    // 🎯 ここに重なっているものを右クリックの一番上に並べる（AIなし）。
    //   ★重なりを前後させて掴みに行くのは素人には難しい。「その場で選べる」ほうが早い、というユーザーの指摘で新設。
    //   同じ一覧は⚙メニューの「重なっている要素から選ぶ」にもあるが、奥にあって見つけてもらえなかった。
    var ovQ=[], ovRowQ='';
    (function(){
      var ls=[]; try{ ls=document.elementsFromPoint(qx,qy); }catch(_){ return; }
      ls=ls.filter(function(c){ return c!==document.documentElement&&c!==document.body&&!(c.closest&&c.closest('[id^="__ce"]')); });
      var pe=[]; try{ pe=_peNoneAt(qx,qy)||[]; }catch(_){}          // クリックがすり抜ける飾りも並べる
      var raw=pe.concat(ls.filter(function(c){ return pe.indexOf(c)<0; }));
      // ★同じ中身の入れ物が何重にも並ぶと選べない（実報告：7個すべて同じ文字）。
      //   中の要素とほぼ同じ大きさの外側は「増えていない」ので畳む。残すのは意味の違うものだけ。
      var area=function(n){ var r=n.getBoundingClientRect(); return Math.max(1,r.width*r.height); };
      ovQ=[];
      raw.forEach(function(c){
        if(ovQ.length>=5) return;
        var dup=ovQ.some(function(k){ return c.contains(k) && area(c)<=area(k)*1.25; });   // 外側だが実質同じ
        if(!dup) ovQ.push(c);
      });
      if(ovQ.length<2) { ovQ=[]; return; }
      // ★入れ子の親子は文字が同じで見分けが付かない＝大きさを添える（小さい＝文字そのもの／大きい＝入れ物）
      var nameOf2=function(c){
        var tx=(c.textContent||'').replace(/\\s+/g,' ').trim();
        var r2=c.getBoundingClientRect(), sz=' '+Math.round(r2.width)+'×'+Math.round(r2.height);
        if(c.tagName==='IMG') return '🖼 画像'+sz;
        var cs2=''; try{ cs2=getComputedStyle(c).backgroundImage; }catch(_){}
        if(tx) return '「'+tx.slice(0,8)+'」'+sz;
        if(cs2&&cs2!=='none') return '🖼 絵の箱'+sz;
        return '⬜ 箱'+sz;
      };
      ovRowQ='<div style="background:#f3f0ff;border-bottom:1px solid #d5cdf0;padding:6px 10px 7px;border-radius:7px">'
        +'<b style="font-size:12px">🎯 ここに重なっているもの（'+ovQ.length+'個・AIなし）</b><br>'
        +'<span style="font-size:10.5px;color:#6a5f8a">触れると赤枠／押すとそれを掴みます（上ほど手前）</span>'
        +'<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:4px">'
        +ovQ.map(function(c,i){
          return '<button class="__ce_ovz" data-i="'+i+'" title="'+esc(c.tagName.toLowerCase()+(c.className&&typeof c.className==='string'?'.'+c.className.split(' ')[0]:''))+'" style="background:#fff;border:1px solid #cfc6ea;border-radius:6px;padding:3px 8px;cursor:pointer;font:11.5px/1.3 inherit;color:#1d1d1f;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
            +(i===0?'▲ ':'')+esc(nameOf2(c))+'</button>';
        }).join('')
        +'</div></div>';
    })();
    // 🖼 ここにある背景の絵・飾りを「サムネで」右クリックにそのまま出す（押さなくても分かる）。
    //   ★板そのものは出さない：スライダー＋9マスは背が高く、メニューが画面外まで押し下がるため。
    //   サムネに触れるとページ側が赤枠＝どれのことか目で確かめてから押せる。
    var bgQ=bgCandsAt(qx,qy,curEl), bgRowQ='';
    // ★1枚しかない時に選ばせるのは無駄（ユーザー指摘）。1枚なら1行だけ、複数の時だけサムネを並べる。
    if(bgQ.length===1){
      var _b1=bgQ[0], _t1=_b1.el.tagName;
      var _n1=(_t1==='BODY'||_t1==='HTML')?'⚠ ページ全体の背景':(_b1.ps?'飾りの絵':'背景の絵');
      bgRowQ='<div style="background:#eef2ff;border-bottom:1px solid #ccd6f5;padding:5px 10px 6px;border-radius:7px">'
        +'<button class="__ce_bgz" data-i="0" style="display:flex;align-items:center;gap:7px;width:100%;text-align:left;background:none;border:none;padding:2px;cursor:pointer;font:12px/1.4 inherit;color:#1d1d1f">'
        +'<img src="'+esc(_b1.url)+'" style="width:34px;height:24px;object-fit:contain;background:#fff;border:1px solid #ccd6f5;border-radius:4px">'
        +'<span>🖼 '+esc(_n1)+'を直す（大きさ・位置・前後・消す）</span></button></div>';
    }
    else if(bgQ.length){
      bgRowQ='<div style="background:#eef2ff;border-bottom:1px solid #ccd6f5;padding:6px 10px 7px;border-radius:7px">'
        +'<b style="font-size:12px">🖼 ここにある背景の絵・飾り（'+bgQ.length+'枚・AIなし）</b><br>'
        +'<span style="font-size:10.5px;color:#5b6a8a">触れるとページ側が赤枠／押すと大きさ・位置を直せます</span>'
        +'<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:4px">'
        +bgQ.map(function(c,i){
          var t=c.el.tagName, nm=(t==='BODY'||t==='HTML')?'⚠全体':(c.ps?'飾り':'背景');
          return '<button class="__ce_bgz" data-i="'+i+'" title="'+esc(nm)+'" style="display:flex;flex-direction:column;align-items:center;gap:1px;width:52px;padding:2px;background:#fff;border:1px solid #ccd6f5;border-radius:6px;cursor:pointer;font:10.5px/1.2 inherit;color:#1d1d1f">'
            +'<img src="'+esc(c.url)+'" style="width:44px;height:30px;object-fit:contain;background:#fff;border-radius:3px">'
            +'<span>'+esc(nm)+'</span></button>';
        }).join('')
        +'</div>'
        // ★「白い所＝余白」だと思って余白パネルをいくら触っても直らない事故が起きる。
        //   正体は「絵が箱まで届いていない」こと。届いていない量を出して1クリックで直せるようにする。
        +(function(){
          var out='';
          bgQ.forEach(function(c,i){
            if(out||c.ps) return;
            if(/^(BODY|HTML)$/.test(c.el.tagName)) return;
            var cs; try{ cs=getComputedStyle(c.el); }catch(_){ return; }
            var bs=cs.backgroundSize||'auto';
            if(bs==='cover') return;
            var r=c.el.getBoundingClientRect();
            if(!(r.width>60&&r.height>40)) return;
            var pv=function(v,base){ if(/%$/.test(v)) return parseFloat(v)/100*base; if(/px$/.test(v)) return parseFloat(v); return null; };
            var t2=bs.split(' '), sw=pv(t2[0]||'',r.width);
            if(sw==null) return;                       // auto/contain は絵の実寸が要るのでここでは出さない
            var gw=Math.round(r.width-sw);
            if(gw<12) return;
            out='<div style="margin-top:5px;background:#fff4f4;border:1px solid #f0c9c9;border-radius:6px;padding:5px 8px;font-size:11px;color:#a33;line-height:1.5">'
              +'⚠ 絵が箱まで届いていません（左右に'+gw+'px）。ここは余白ではないので余白パネルでは詰まりません<br>'
              +'<button class="__ce_bgfill" data-i="'+i+'" style="margin-top:3px;background:#fff;border:1px solid #d99;border-radius:5px;padding:2px 9px;cursor:pointer;font-size:11px;font-family:inherit;color:#a33">箱いっぱいにする（cover）</button></div>';
          });
          return out;
        })()
        +'</div>';
    }
    var _qmM=qmDefMap();
    // 🔄画像の差し替えは「そこに画像がある時」だけ出す（無い場所で押しても何も起きない項目は隠す）。
    // 何枚重なっているかも出す＝どれを選ぶ画面が出るのか予想できる。
    (function(){
      var _c=imgCandsAt(qx,qy,curEl), _n=_c.length;
      // 🖼背景の大きさ・位置は「背景画像がそこにある時」だけ出す（<img>だけの場所では意味がない）
      var _bg=bgQ;
      if(!_bg.length) delete _qmM['__ce_q_bgsz'];
      else if(_bg.length>1) _qmM['__ce_q_bgsz']='🖼 背景画像の大きさ・位置（'+_bg.length+'枚・AIなし）';
      if(!_n){ delete _qmM['__ce_q_imgswap']; return; }
      if(_n>1) _qmM['__ce_q_imgswap']='🔄 この画像を差し替え（'+_n+'枚が重なっています・AIなし）';
    })();
    // 📌 貼り付き解除は「実際に画面に貼り付いている時」だけ出す（普通の要素には無関係な項目なので隠す）。
    // 自分自身が固定なのか、固定の器の中に取り残されているのかで文言を変える＝何が起きるか分かるように。
    (function(){
      var _p=_unfixPlan(curEl);
      // ツールで貼り付けた(data-cepin sticky)ものは_stuckAncestor(=fixedだけ検出)に映らないので、先に見る
      var _pinned=curEl&&curEl.closest&&curEl.closest('[data-cepin]');
      if(_pinned){ _qmM['__ce_q_unfix']='📌 画面への貼り付きを解除（〈'+_pinned.tagName.toLowerCase()+'〉をページと一緒に動かす）'; return; }
      if(!_p){ delete _qmM['__ce_q_unfix']; return; }
      _qmM['__ce_q_unfix']=
        (_p.kind==='self') ? '📌 画面への貼り付きを解除（この要素をページと一緒に動かす）'
      : (_p.kind==='out')  ? '📌 画面への貼り付きを解除（貼り付く枠から出す）'
      : '📌 画面への貼り付きを解除（〈'+_p.target.tagName.toLowerCase()+'〉ごとページと一緒に動かす）';
    })();
    // 📌 逆：貼り付ける は「まだ貼り付いていない時」だけ出す（既に固定なら上の解除が出るので二重で出さない）。
    //   貼り付ける対象のセクション名を出す＝どこが貼り付くか予想できる。
    (function(){
      var _pt=pinFixTarget(curEl);
      if(!_pt || pinIsFixed(_pt)){ delete _qmM['__ce_q_pin']; return; }
      var _nm=_pt.tagName.toLowerCase();
      _qmM['__ce_q_pin']='📌 スクロールしても画面に貼り付ける（〈'+_nm+'〉を上に固定）';
    })();
    // 🎨 背景色は「セクション/ヘッダー/フッターの中」でだけ出す（塗る相手が無い所では隠す）
    (function(){
      var _sb=(curEl&&curEl.closest)?curEl.closest('section,header,footer'):null;
      if(!_sb){ delete _qmM['__ce_q_secbg']; return; }
      _qmM['__ce_q_secbg']='🎨 セクションの背景色を変える（〈'+_sb.tagName.toLowerCase()+'〉・AIなし）';
    })();
    // 🖌 文字の背景色は「文字のある要素」でだけ出す（section等の大きな器や画像では隠す）
    (function(){
      var _te=curEl;
      var ok=_te && (_te.textContent||'').trim().length>0 && !/^(SECTION|HEADER|FOOTER|MAIN|BODY|HTML|IMG|SVG)$/.test(_te.tagName);
      if(!ok){ delete _qmM['__ce_q_txtbg']; delete _qmM['__ce_q_vline']; return; }
      _qmM['__ce_q_txtbg']='🖌 文字の背景に色を塗る（〈'+_te.tagName.toLowerCase()+'〉の行・AIなし）';
      _qmM['__ce_q_vline']=(_te.getAttribute('data-cevl')?'▎ 左の縦線を調整する（色・太さ・間隔）':'▎ 文字の左に縦線を引く（引用風・AIなし）');
    })();
    // 🎨 右クリックした場所に飾り（丸・リング・縁取り線・図形/線）があるときだけ出す
    (function(){
      var _dq=null;
      try{ _dq=dqHitAt(qx,qy,curEl); }catch(_){ }
      if(_dq){ _qmM['__ce_q_deco']='🎨 '+(DQ_NAME[dqKind(_dq)]||'飾り')+'の色・形・傾きを変える'; return; }
      var _bd=null; try{ _bd=dqBorderAt(qx,qy); }catch(_){ }
      if(_bd){ _qmM['__ce_q_deco']='➖ この線（'+(BD_SIDE[_bd.side]||'')+'の線）の色・太さ・種類を変える'; return; }
      delete _qmM['__ce_q_deco'];
    })();
    // 🖼 すでにスライドショーになっている所では「選び直す」と分かる文字にする
    //   （同じ文字のままだと、設定を変えようと押した人が気づかずに解除していた・実報告）
    (function(){
      var _sw=(curEl&&curEl.closest)?curEl.closest('[data-slshow]'):null;
      if(!_sw) return;   // 作っていない所は既定の文字のまま
      // ★どの1枚を掴んでいるかを必ず出す（3枚が同じ場所に重なっていて目では区別できないため）
      var _sl=[].slice.call(_sw.querySelectorAll('img')), _si=_sl.indexOf(curEl);
      _qmM['__ce_q_slide']='🖼 スライドショーを選び直す・解除（全'+_sl.length+'枚'
        +(_si>=0?('・今つかんでいるのは '+(_si+1)+'枚目'):'')+'）';
    })();
    // 🧲 掴む枠が見た目からズレている時だけ出す＝どっちへ何pxズレているかを文字で見せる
    //   （透明な枠は目で見えないので、数字で言わないと「何を直すのか」が伝わらない）
    (function(){
      var _ff=null; try{ _ff=frameFitInfo(curEl); }catch(_){ }
      if(!_ff){ delete _qmM['__ce_q_frmfit']; return; }
      // ズレの向きは「枠から見てどっちに動かすか」で言う（数字だけだと符号の意味が分からない）
      var _fd=(_ff.x<0?'左':'右')+Math.abs(Math.round(_ff.x))+'px・'
             +(_ff.y<0?'上':'下')+Math.abs(Math.round(_ff.y))+'px';
      _qmM['__ce_q_frmfit']=(_ff.kind==='ink')
        ? ('🧲 空っぽの箱を中身の位置へ持ってくる（中身が '+_fd+' に飛び出しています）')
        : ('🧲 掴む枠を見た目の位置に合わせる（枠を '+_fd+' 動かして重ねます）');
    })();
    // 🔓 CSSで描かれた飾り（01などの疑似要素）がある時だけ出す＝掴める形に作り替える
    (function(){
      var _ps=[];
      try{ _ps=psList(curEl); }catch(_){ }
      if(!_ps.length){ delete _qmM['__ce_q_psgrab']; return; }
      var _nm=_ps[0].text||(_ps[0].img?'画像の飾り':'飾り');
      _qmM['__ce_q_psgrab']='🔓 「'+_nm+'」を掴めるようにする（動かす・色を変える）';
    })();
    // 🅰 まとめて文字調整（複数選択時のみ）：フォント種はページで使用中のものを頻度順に出す＋定番を後ろに
    var mfRow='';
    if(multi){
      var _mfF=[];
      try{
        var _fc={};
        [].slice.call(document.querySelectorAll('h1,h2,h3,h4,p,a,li,span,dt,dd,button')).slice(0,800).forEach(function(n){
          if(!(n.textContent||'').trim()||_inUI2(n)) return;
          var f=(getComputedStyle(n).fontFamily||'').split(',')[0].trim().replace(/"/g,'');
          if(f) _fc[f]=(_fc[f]||0)+1;
        });
        _mfF=Object.keys(_fc).sort(function(a,b){return _fc[b]-_fc[a];}).slice(0,6);
      }catch(_){ }
      ['游明朝','游ゴシック','ヒラギノ角ゴ ProN','メイリオ','serif','sans-serif'].forEach(function(f){ if(_mfF.indexOf(f)<0) _mfF.push(f); });
      // ★今かかっている値を読んで、そのまま画面に出す（＝「何が選ばれているか」が一目で分かる）。
      //   選んだもので値が違えば「バラバラ」と出す。値が1つなら選択肢もその値を選んだ状態にする。
      var _mfNow=function(get){ var s=[]; selEls.forEach(function(x){ var v; try{ v=get(getComputedStyle(x)); }catch(_){ return; } if(v!=null&&v!==''&&s.indexOf(v)<0) s.push(v); }); return s; };
      var _curF=_mfNow(function(cs){ return (cs.fontFamily||'').split(',')[0].trim().replace(/["']/g,''); });
      var _curS=_mfNow(function(cs){ return Math.round((parseFloat(cs.fontSize)||0)*10)/10; });
      var _curW=_mfNow(function(cs){ return cs.fontWeight; });
      var _curC=_mfNow(function(cs){ return cs.color; });
      var _mfHex=function(c){ var m=/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/.exec(c||''); return m?('#'+[1,2,3].map(function(i){ return ('0'+(+m[i]).toString(16)).slice(-2); }).join('')):''; };
      var _one=function(a,suf){ return a.length===1?(a[0]+(suf||'')):(a.length?'バラバラ':'—'); };
      var _bS='background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:2px 8px;cursor:pointer';
      var _nowS='font-size:10.5px;color:#5b7396;background:#dbe9fb;border-radius:4px;padding:0 5px';
      mfRow='<div style="background:#e8f2ff;border-bottom:1px solid #c9def5;padding:6px 10px 7px;font-size:12px;line-height:2.1;border-radius:7px;max-width:295px;box-sizing:border-box">'
        +'<b>🅰 まとめて文字調整（'+selEls.length+'個・AIなし）</b><br>'
        +'<span style="opacity:.8">フォント</span> <select id="__ce_mf_f" style="max-width:185px;font-size:11px;padding:2px;border:1px solid #ccd;border-radius:5px">'
        +'<option value="">'+(_curF.length===1?'（変えない）':'（変えない・今はバラバラ）')+'</option>'
        +(_curF.length===1?('<optgroup label="今のフォント"><option value="'+esc(_curF[0])+'" selected>'+esc(_curF[0])+'</option></optgroup>'):'')
        +'<optgroup label="ページで使用中">'+_mfF.map(function(f){ return '<option value="'+esc(f)+'">'+esc(f)+'</option>'; }).join('')+'</optgroup>'
        +'<optgroup label="おすすめ（Web＝要ネット）">'+FONT_LIST.filter(function(f){return f[0];}).map(function(f){ return '<option value="'+esc(f[0])+'">'+esc(f[1])+'</option>'; }).join('')+'</optgroup></select><br>'
        +'<span style="opacity:.8">大きさ</span> <input type="number" id="__ce_mf_s" value="'+(_curS.length===1?_curS[0]:'')+'" placeholder="'+(_curS.length?'バラバラ':'')+'" title="今の文字サイズ（px）。直接入れてEnterでも変えられます" style="width:56px;font-size:11px;padding:2px 4px;border:1px solid #ccd;border-radius:5px">px '
        +'<button id="__ce_mf_m" style="'+_bS+'">−</button> <button id="__ce_mf_p" style="'+_bS+'">＋</button>'
        +(_curS.length>1?(' <span id="__ce_mf_sn" style="'+_nowS+'">今 '+_curS.slice(0,4).join(' / ')+'px</span>'):'')+'<br>'
        +'<span style="opacity:.8">太さ</span> <button id="__ce_mf_b" style="'+_bS+';font-weight:700">太く</button> <button id="__ce_mf_n" style="'+_bS+'">標準</button> '
        +'<span id="__ce_mf_wn" style="'+_nowS+'">今 '+_one(_curW)+'</span><br>'
        +'<span style="opacity:.8">文字色</span> <input type="color" id="__ce_mf_c" value="'+(_curC.length===1?(_mfHex(_curC[0])||'#222222'):'#222222')+'" style="width:28px;height:21px;padding:0;border:none;border-radius:4px;vertical-align:middle;cursor:pointer"> '
        +'<span id="__ce_mf_cn" style="'+_nowS+'">今 '+(_curC.length===1?(_mfHex(_curC[0])||_curC[0]):'バラバラ')+'</span>'
        +'</div>';
    }
    // 📐 まとめてサイズをそろえる（複数選択時のみ・Figmaの「サイズを合わせる」相当・AIなし）
    // 大きい方／中央値／小さい方の「お手本の1個」を選び、その寸法を全員に配る。
    // 比率が違う画像を両方そろえると普通は潰れるので、IMGは object-fit:cover＝切り取りにして潰さない。
    var szRow='';
    if(multi){
      var _szW=[], _szH=[], _szImg=0;
      selEls.forEach(function(x){
        var r=szBox(x).getBoundingClientRect();   // 画像は切り取り枠＝見えている箱で測る
        if(r.width>0&&r.height>0){ _szW.push(r.width); _szH.push(r.height); }
        if(x.tagName==='IMG'||(x.querySelector&&x.querySelector('img'))) _szImg++;
      });
      if(_szW.length>1){
        var _szSm=Math.round(Math.min.apply(null,_szW))+'×'+Math.round(Math.min.apply(null,_szH));
        var _szBg=Math.round(Math.max.apply(null,_szW))+'×'+Math.round(Math.max.apply(null,_szH));
        var _szB='background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:2px 8px;cursor:pointer';
        szRow='<div style="background:#eaf7ee;border-bottom:1px solid #c3e6cd;padding:6px 10px 7px;font-size:12px;line-height:2.1;border-radius:7px">'
          +'<b>📐 サイズをそろえる（'+selEls.length+'個'+(_szImg?'・うち画像'+_szImg+'個':'')+'・AIなし）</b><br>'
          +'<span style="opacity:.8">そろえる先</span> '
          +'<button id="__ce_sz_max" title="いちばん大きい1個に全部を合わせる" style="'+_szB+'">🔼 大きい方</button> '
          +'<button id="__ce_sz_mid" title="まん中の大きさの1個に合わせる（偶数個のときは小さい方寄り）" style="'+_szB+'">◼ 中央値</button> '
          +'<button id="__ce_sz_min" title="いちばん小さい1個に全部を合わせる" style="'+_szB+'">🔽 小さい方</button><br>'
          +'<span style="opacity:.8">そろえる所</span> '
          +'<select id="__ce_sz_ax" style="font-size:11px;padding:2px;border:1px solid #ccd;border-radius:5px">'
          +'<option value="wh">両方（幅と高さ）</option><option value="w">幅だけ</option><option value="h">高さだけ</option></select> '
          +'<span style="font-size:10.5px;color:#888">今 '+_szSm+' 〜 '+_szBg+'px</span>'
          +'</div>';
      }
    }
    // 📐 位置をそろえる（複数選択時のみ・パワポ/Figmaの「配置」相当・AIなし）
    // 既にある📏吸着ガイドは「ドラッグ中に近づいたら吸い付く」＝1個ずつ手で寄せる道具。
    // 掴む相手を間違えると別の要素が動く（複数選択・🧩グループが残っていると特に）ので、
    // ここは「選んだもの全部の端／中央を数値でピタリ合わせる」ボタンにする＝手ブレ0で揃う。
    var alRow='';
    if(multi){
      var _alN=0, _alFix=0;
      selEls.forEach(function(x){
        if(!x||_undraggable(x)) return;
        var r; try{ r=x.getBoundingClientRect(); }catch(_){ return; }
        if(!(r.width>0&&r.height>0)) return;
        _alN++;
        try{ if(getComputedStyle(x).position==='static') _alFix++; }catch(_){}
      });
      if(_alN>1){
        var _alB='background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:2px 8px;cursor:pointer';
        alRow='<div style="background:#f1eefc;border-bottom:1px solid #d8cff2;padding:6px 10px 7px;font-size:12px;line-height:2.1;border-radius:7px;max-width:295px;box-sizing:border-box">'
          +'<b>🧲 位置をそろえる（'+_alN+'個・AIなし）</b><br>'
          +'<span style="opacity:.8">よこ</span> '
          +'<button class="__ce_alb" data-k="l" title="左端をそろえる" style="'+_alB+'">⬅ 左</button> '
          +'<button class="__ce_alb" data-k="cx" title="左右の中心をそろえる（縦一列に並ぶ）" style="'+_alB+'">⬄ 中央</button> '
          +'<button class="__ce_alb" data-k="r" title="右端をそろえる" style="'+_alB+'">➡ 右</button><br>'
          +'<span style="opacity:.8">たて</span> '
          +'<button class="__ce_alb" data-k="t" title="上端をそろえる（横一列に並ぶ）" style="'+_alB+'">⬆ 上</button> '
          +'<button class="__ce_alb" data-k="cy" title="上下の中心をそろえる" style="'+_alB+'">⬍ 中</button> '
          +'<button class="__ce_alb" data-k="b" title="下端をそろえる" style="'+_alB+'">⬇ 下</button><br>'
          +'<span style="opacity:.8">等間隔</span> '
          +'<button class="__ce_alb" data-k="dx" title="左右の隙間を全部同じにする（両端は動かさない・3個以上）" style="'+_alB+'">⇹ よこ</button> '
          +'<button class="__ce_alb" data-k="dy" title="上下の隙間を全部同じにする（両端は動かさない・3個以上）" style="'+_alB+'">⇳ たて</button><br>'
          +'<span style="opacity:.8">そろえる先</span> '
          +'<select id="__ce_al_ref" style="font-size:11px;padding:2px;border:1px solid #ccd;border-radius:5px;max-width:180px">'
          +'<option value="edge">いちばん端のもの（おすすめ）</option>'
          +'<option value="first">最初に選んだもの（動かさない）</option></select>'
          +'<div style="font-size:10.5px;color:#6b5b95;line-height:1.45;margin-top:3px;max-width:275px">'
          +'ドラッグと違って掴み間違いが起きません。⟲で戻せます'
          +(_alFix?('<br>⚠ 通常配置が'+_alFix+'個：ずらし（translate）で寄せるので、他の行と重なることがあります'):'')
          +'</div></div>';
      }
    }
    // 📏 余白の定番（自分で決めた値をボタンにして当てる・AIなし）
    // ブロックごとにアキがバラバラなのを、同じ数値を配って一発でそろえる用。値は自分で編集でき、
    // localStorage に持つのでカンプをまたいで使い回せる（＝自分の"余白ルール"になる）。
    var spRow='', spFit=null;
    (function(){
      var tgtN=(selEls.length?selEls.length:1);
      // ★数値＋単位だけを通す。引用符などが混じるとボタンの中身が空になって「押せない空箱」に見える
      //   （実際に空ボタンの報告あり）。通らない値は捨てて、全滅なら既定値に戻す。
      var vals=(localStorage.getItem('__ce_sp_presets')||'5rem,10rem,14rem').split(',')
        .map(function(s){ return s.trim(); }).filter(function(s){ return /^[0-9]*[.]?[0-9]+(rem|em|px|%)?$/.test(s); }).slice(0,6);
      if(!vals.length) vals=['5rem','10rem','14rem'];
      var rootPx=16; try{ rootPx=parseFloat(getComputedStyle(document.documentElement).fontSize)||16; }catch(_){}
      var _pbS='background:#fff;border:1px solid #dcc6a6;border-radius:5px;padding:2px 9px;cursor:pointer;font-weight:700';
      var btns=vals.map(function(v){
        var px=/rem$/.test(v)?(Math.round(parseFloat(v)*rootPx)+'px'):(/em$/.test(v)?'':v);
        return '<button class="__ce_spv" data-v="'+esc(v)+'" title="'+esc(v)+(px?('（約'+px+'）'):'')+'" style="'+_pbS+'">'+esc(v)+'</button>';
      }).join(' ');
      // ドラッグで縦にずらした跡があると、余白をそろえてもその分だけアキが狂う＝一緒に戻せるようにする
      var ty=0, free=0;
      (selEls.length?selEls:[curEl]).forEach(function(x){
        if(!x) return;
        ty+=Math.abs(+x.getAttribute('data-cety')||0);
        try{ delete x.__ceSpRef; delete x.__ceSpRefB; }catch(_){}   // 隙間の基準はメニューを開き直したら選び直す（古い基準を持ち越さない）
        var ps=''; try{ ps=getComputedStyle(x).position; }catch(_){}
        if(ps==='absolute'||ps==='fixed') free++;   // 自由配置＝margin/paddingでは1pxも動かない
      });
      // 🤏 箱が中身よりずっと高い（過去のリサイズで height が !important で焼き込まれた等）＝
      //   padding や min-height をいくら下げても縮まないので、1発で文字ぴったりに戻すボタンを出す
      var fitInfo=null;
      try{
        var _fe=selEls.length? (selEls.length===1?selEls[0]:null) : curEl;   // 複数選択時は出さない
        if(_fe&&_fe.textContent&&_fe.textContent.trim()&&!/^(SECTION|HEADER|FOOTER|MAIN|IMG)$/.test(_fe.tagName)){
          var _rg=document.createRange(); _rg.selectNodeContents(_fe);
          var _ch=_rg.getBoundingClientRect().height, _bh=_fe.offsetHeight;
          if(_ch>0&&_bh-_ch>24) fitInfo={h:Math.round(_bh), c:Math.round(_ch)};
        }
      }catch(_){}
      var mode=localStorage.getItem('__ce_sp_mode')||'gap';
      var opt=function(v,t){ return '<option value="'+v+'"'+(mode===v?' selected':'')+'>'+t+'</option>'; };
      // ★max-width＝この板の説明文でメニュー全体が横に広がるのを止める（widthはauto＝中身なりのため）
      spRow='<div style="background:#fdf3e6;border-bottom:1px solid #ecd9bb;padding:6px 10px 7px;font-size:12px;line-height:2.1;border-radius:7px;max-width:295px;box-sizing:border-box">'
        +'<b>📏 余白の定番（'+tgtN+'個に当てる・AIなし）</b><br>'
        +btns+' <button id="__ce_sp_ed" title="定番の数値を自分で決め直す（カンマ区切り・カンプをまたいで残る）" style="background:none;border:none;color:#8a6d3b;font-size:11px;cursor:pointer;text-decoration:underline">✎ 値を変える</button>'
        +' <button id="__ce_sp_rl" title="ページ中の今の隙間を全部その場に数字で表示（もう一度押すと消える・保存には写らない）" style="background:none;border:none;color:#8a6d3b;font-size:11px;cursor:pointer;text-decoration:underline">📐ものさし</button><br>'
        +'<span style="opacity:.8">そろえ方</span> '
        +'<select id="__ce_sp_md" style="font-size:11px;padding:2px;border:1px solid #ccd;border-radius:5px;max-width:185px">'
        +opt('gap','上との隙間＝結果その値（おすすめ）')
        +opt('gapb','下との隙間＝結果その値')
        +opt('mt','margin-top をその値に')+opt('mb','margin-bottom をその値に')+opt('ptb','padding上下 をその値に')
        +'</select><br><span id="__ce_sp_now" style="font-size:10.5px;color:#888"></span>'
        +(mode==='gap'?('<div style="font-size:10.5px;color:#8a6d3b;line-height:1.45;margin-top:3px;max-width:275px">'
            +(tgtN>1?'選んだもの同士の隙間が<b>結果その値ちょうど</b>になります<br>（一番上は動かしません）'
                    :'すぐ上にあるものとの隙間が<b>結果その値ちょうど</b>に<br>なります（2個以上選ぶとより確実）')+'</div>'):'')
        +(mode==='gapb'?('<div style="font-size:10.5px;color:#8a6d3b;line-height:1.45;margin-top:3px;max-width:275px">'
            +'すぐ下にあるものとの隙間が<b>結果その値ちょうど</b>に<br>なります（下にあるものを動かして作ります）</div>'):'')
        +((free&&mode!=='gap'&&mode!=='gapb')?('<div style="font-size:10.5px;color:#c0392b;line-height:1.45;margin-top:3px;max-width:275px">'
            +'⚠ 自由配置が'+free+'個：margin/padding では1pxも動きません。そろえ方は「隙間＝結果その値」を使ってください</div>'):'')
        +(fitInfo?('<button id="__ce_sp_fit" title="固定サイズ(height)の焼き込みを外して、箱の高さを中身ぴったりに戻す" style="display:block;margin-top:4px;background:#fff;border:1px solid #dcc6a6;border-radius:6px;padding:3px 9px;cursor:pointer;font-size:11.5px;font-family:inherit;color:#1d1d1f">🤏 箱を中身にぴったり縮める（今 '+fitInfo.h+'px → 約'+fitInfo.c+'px）</button>'):'')
        +(ty>8?('<label title="ドラッグで動かした縦のズレを0に戻してから余白を当てる" style="display:block;font-size:11px;line-height:1.5;margin-top:4px;cursor:pointer;user-select:none"><input type="checkbox" id="__ce_sp_zy" style="vertical-align:middle;margin-right:4px;cursor:pointer">ドラッグの縦ズレ（計'+Math.round(ty)+'px）も0に戻す</label>'):'')
        +'</div>';
      spFit=fitInfo;
    })();
    // 📏 板は畳んで1行にする（最初に使う機能ではないので、本命メニューを押し下げない）。
    //   開閉はこのPCに記憶＝よく使う人は開いたままにできる。中身はDOMに残す＝下の配線はそのまま効く。
    if(spRow){
      var _spOpen=(localStorage.getItem('__ce_sp_open')==='1');
      spRow='<button id="__ce_sp_tg" style="display:flex;width:100%;align-items:center;gap:8px;text-align:left;background:none;border:none;padding:7px 10px;border-radius:7px;cursor:pointer;font-size:13px;font-family:inherit;color:#1d1d1f">'
        +'<span style="flex:1">📏 余白・間隔をそろえる'+(spFit?' <span style="font-size:10.5px;color:#c1801f">🤏縮められます</span>':'')+'</span>'
        +'<span class="__ce_sp_ar" style="color:#8a8a90;font-size:11px">'+(_spOpen?'▾':'▸')+'</span></button>'
        +'<div id="__ce_sp_body" style="'+(_spOpen?'':'display:none')+'">'+spRow+'</div>';
    }
    // ★並び順（2026-07-25）：気づき系パネル（🕳穴・📏余白・🎞スライド・🫥すり抜け・➖飾り・◽カド）は
    //   縦に長く、3つ重なると本命のメニューが画面外まで押し下げられる。よく使うグループメニューを先に出し、
    //   パネル群はその下にまとめる（選択中パネル selRowQ / まとめて文字調整 mfRow は今の操作の続きなので上のまま）。
    //   ★パネルは他のグループと同じ「▸ 1つの畳んだメニュー」にまとめる（data-g="n"＝数字のgiと衝突しない）。
    //     中身はDOM上 qm の子のままなので、下の #__ce_dg_fix 等の配線・closeMenu はそのまま効く。
    // ― 薄い区切り線は細すぎて右クリックで掴めない＝クリック位置の上下12pxから線を拾って
    //   「この線を選ぶ」ボタンを出す（AIなし）。線そのもの(hr・高さ8px以下)と、大きい要素の
    //   上下フチのborder線（持ち主を選ぶ）の両方を拾う。
    var lineQ=[];
    (function(){
      var seen=[];
      var push=function(el,name){ if(el!==curEl&&seen.indexOf(el)<0&&lineQ.length<4){ seen.push(el); lineQ.push({el:el,name:name}); } };
      var stack=[];
      [-12,-8,-4,0,4,8,12].forEach(function(dy){
        var y=qy+dy; if(y<0||y>window.innerHeight) return;
        var ls; try{ ls=document.elementsFromPoint(qx,y)||[]; }catch(_){ ls=[]; }
        ls.forEach(function(n){ if(stack.indexOf(n)<0) stack.push(n); });
      });
      stack.forEach(function(n){
        if(!n||n.nodeType!==1||n===document.body||n.tagName==='HTML'||_inUI2(n)) return;
        var r=n.getBoundingClientRect(); if(!(r.width>=40)) return;
        var cs; try{ cs=getComputedStyle(n); }catch(_){ return; }
        if(cs.visibility==='hidden'||(parseFloat(cs.opacity)||0)<0.05) return;
        var tg=n.tagName.toLowerCase();
        var hasBg=(cs.backgroundColor&&cs.backgroundColor!=='transparent'&&cs.backgroundColor!=='rgba(0, 0, 0, 0)')||(cs.backgroundImage&&cs.backgroundImage!=='none');
        var bt=parseFloat(cs.borderTopWidth)>0&&cs.borderTopStyle!=='none';
        var bb=parseFloat(cs.borderBottomWidth)>0&&cs.borderBottomStyle!=='none';
        if(tg==='hr'||(r.height<=8&&(hasBg||bt))){ push(n,'― 線そのもの〈'+tg+'〉幅'+Math.round(r.width)+'px'); return; }
        if(bt&&Math.abs(r.top-qy)<=12) push(n,'￣ 上フチに線を持つ〈'+tg+'〉');
        if(bb&&Math.abs(r.bottom-qy)<=12) push(n,'＿ 下フチに線を持つ〈'+tg+'〉');
      });
    })();
    var lineRowQ='';
    if(lineQ.length){
      lineRowQ='<div style="background:#eefbf1;border-bottom:1px solid #bfe3c8;padding:6px 10px 7px;font-size:12px;border-radius:7px">'
        +'<b>― 近くの薄い線を選ぶ</b>（'+lineQ.length+'件・AIなし）<br>'
        +'<span style="font-size:10.5px;color:#5f7a66">触れると赤く光ります／押すとその線が選択されます（動かす・消す・⚙もOK）</span>'
        +lineQ.map(function(it,i){
          return '<button class="__ce_lnz" data-i="'+i+'" style="display:block;width:100%;text-align:left;margin:3px 0 0;background:#fff;border:1px solid #b5d9bf;border-radius:6px;padding:3px 8px;cursor:pointer;font-size:11.5px;font-family:inherit;color:#1d1d1f">'+esc(it.name)+'</button>';
        }).join('')+'</div>';
    }
    var noticeL=[owRowQ,dgRowQ,pdRowQ,slRowQ,peRowQ,decoRowQ,radRowQ].filter(function(s){ return !!s; });
    var noticeQ = noticeL.length
      ? '<div style="border-top:1px solid #b9b9c4;margin:4px 6px"></div>'
        +'<div class="__ce_grp"><button class="__ce_qi __ce_gbtn" data-g="n" style="display:flex;width:100%;align-items:center;gap:8px;text-align:left;background:none;border:none;padding:7px 10px;border-radius:7px;cursor:pointer;font-size:13px;font-family:inherit;color:#1d1d1f">'
        +'<span style="flex:1">🔎 このページで見つかったこと</span>'
        +'<span style="color:#8a8a90;font-size:11px">'+noticeL.length+'　▸</span></button>'
        +'<div class="__ce_sub" data-g="n" style="display:none;position:fixed;left:0;top:0;min-width:300px;max-width:380px;max-height:calc(100vh - 20px);overflow-y:auto;background:#f2f2f7;border:1px solid #b9b9c4;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.22);padding:4px;z-index:2147483647">'
        +'<div style="padding:5px 10px;font-size:10.5px;font-weight:700;color:#5b6472;border-bottom:1px solid #dcdce2;margin-bottom:3px">🔎 このページで見つかったこと（全部AIなし）</div>'
        +noticeL.join('')+'</div></div>'
      : '';
    // 背景が複数ある時のサムネ一覧は縦に長いので、本命メニューの下へ回す（1枚の時の1行は上のまま）
    var bgTop=(bgQ.length===1)?bgRowQ:'', bgBottom=(bgQ.length>1)?bgRowQ:'';
    // 🔎 やりたいことで探す（言い換え表＝一瞬・無料／当たらない時だけAI）
    var searchRowQ='<div style="padding:6px 6px 2px">'
      +'<input id="__ce_qsearch" type="text" placeholder="🔎 やりたいことで探す（例：余白を取りたい）" '
      +'style="width:100%;font-size:12.5px;padding:6px 9px;border:1px solid #d0d0d5;border-radius:8px;font-family:inherit;box-sizing:border-box">'
      +'<div id="__ce_qsres"></div></div>';
    qm.innerHTML=searchRowQ+selRowQ+mfRow+szRow+alRow+ovRowQ+spRow+bgTop+flatRowQ+lineRowQ+(multi?'<div style="padding:5px 10px 2px;font-size:11px;color:#888">🧩 '+selEls.length+'個を選択中（全部に効く）</div>':'')
      +qmBuildList(_qmM,row)   // 見出しごとに畳んで「親メニュー ▸ サブメニュー」にする
      +bgBottom
      +noticeQ
      +'<div style="border-top:1px solid #b9b9c4;margin:4px 6px"></div>'
      +row('__ce_q_vspace','⇕ ここの縦の空間を広げる（余白を作る・AIなし）')
      +row('__ce_q_full','⚙ すべての編集メニュー…')
      +'<div style="display:flex;justify-content:flex-end;gap:10px;padding:0 8px 3px">'
      +'<button id="__ce_q_sckey" style="background:none;border:none;color:#aaa;font-size:11px;cursor:pointer">⌨ キー設定</button>'
      +'<button id="__ce_q_edit" style="background:none;border:none;color:#aaa;font-size:11px;cursor:pointer">⚙ 並べ替え</button></div>';
    document.body.appendChild(qm);
    // 🔎 メニューの曖昧検索（2026-07-30）
    //   項目が増えて「どこにあるか分からない」を解消する。まず言い換え表でローカル検索（一瞬・無料）、
    //   1件も当たらなかった時だけ Gemini に聞く＝ふだんは費用ゼロ。
    //   ★結果は本物のボタンを押す代理ボタンで出す：畳んだサブメニューの中の項目にも届く。
    (function(){
      var inp=qm.querySelector('#__ce_qsearch'), res=qm.querySelector('#__ce_qsres');
      if(!inp||!res) return;
      // 言い換え表：入力にこの行のどれかが含まれていたら、その行の語ぜんぶで探す
      var SYN=[
        '余白 すきま 隙間 間隔 マージン パディング 詰める 空ける あける スペース ゆとり 広げる 狭める',
        '大きさ サイズ 拡大 縮小 大きく 小さく 幅 高さ 太く 細く 伸ばす 縮める',
        '動き アニメ アニメーション 出現 演出 タイミング 遅延 ディレイ 順番',
        '色 カラー 背景 塗る 文字色 グラデ グラデーション 透明 薄く',
        '位置 場所 動かす 移動 ドラッグ ずらす そろえる 揃える 整列 中央 寄せる',
        '文字 テキスト 書体 フォント 行間 字間 太字 見出し',
        '画像 写真 img 差し替え 切り抜き トリミング 背景画像',
        '線 ボーダー 枠 罫線 下線 区切り マーカー',
        '影 シャドウ ぼかし',
        '角丸 丸み 角',
        '消す 削除 非表示 隠す 戻す 元に戻す やり直す',
        '保存 書き出し 出力 ダウンロード 本番 コーディング',
        // ★「ヘッダー」で探しても当たらなかった実例（2026-07-30）。ヘッダーの入れ替えは
        //   ボタン名に「ヘッダー」が入っておらず「🔀 お気に入りからセクションを切り替え」なので、
        //   言い換えで橋渡しする。フッター・ナビ・ロゴも同じ導線。
        'ヘッダー ヘッダ フッター ふったー ナビ ナビゲーション メニューバー 上のバー ロゴ 切り替え 入れ替え 差し替え お気に入り 部品',
        'セクション 節 ブロック 並べ替え 順番 追加 増やす 複製'
      ].map(function(s){ return s.split(' '); });
      function norm(s){
        s=(s||'').toLowerCase();
        // カタカナ→ひらがな（「マージン」と「まーじん」を同じ扱いにする）
        s=s.replace(/[\\u30a1-\\u30f6]/g,function(c){ return String.fromCharCode(c.charCodeAt(0)-0x60); });
        return s.replace(/[\\s\\u3000・、。！？…]/g,'');
      }
      var pool=[].slice.call(qm.querySelectorAll('.__ce_qi')).filter(function(b){
        return !b.classList.contains('__ce_gbtn');
      });
      // ★全体メニュー(#__ce)の項目も対象にする。右クリック側に出ていない機能（余白をそろえる等）は
      //   ここにしかなく、含めないと「余白を取りたい」がローカルで当たらずAI行きになってしまう。
      var full=document.getElementById('__ce');
      if(full){
        pool=pool.concat([].slice.call(full.querySelectorAll('button')).filter(function(b){
          return (b.textContent||'').trim().length>1;
        }));
      }
      var items=pool.map(function(b){
        var lb=(b.textContent||'').replace(/\\s+/g,' ').trim();
        return { el:b, label:lb, key:norm(lb) };
      });
      function expand(q){
        var nq=norm(q), terms=[nq];
        SYN.forEach(function(row){
          if(row.some(function(w){ return nq.indexOf(norm(w))>=0; })) row.forEach(function(w){ terms.push(norm(w)); });
        });
        return terms.filter(function(t){ return t.length>=1; });
      }
      function render(list,note){
        res.innerHTML='';
        if(note){ var d=document.createElement('div'); d.style.cssText='font-size:11px;color:#8a8a90;padding:4px 4px 0'; d.textContent=note; res.appendChild(d); }
        list.slice(0,8).forEach(function(it){
          var b=document.createElement('button');
          b.style.cssText='display:block;width:100%;text-align:left;background:#f4f8ff;border:1px solid #d6e4fb;border-radius:7px;padding:6px 9px;margin:3px 0 0;cursor:pointer;font-size:12.5px;font-family:inherit;color:#1d1d1f';
          b.textContent=it.label;
          b.addEventListener('click',function(ev){ ev.stopPropagation(); it.el.click(); });
          res.appendChild(b);
        });
      }
      var tmr=null;
      inp.addEventListener('mousedown',function(ev){ ev.stopPropagation(); });
      inp.addEventListener('input',function(){
        var q=inp.value.trim();
        if(tmr){ clearTimeout(tmr); tmr=null; }
        if(!q){ res.innerHTML=''; return; }
        var terms=expand(q);
        var hits=items.filter(function(it){ return terms.some(function(t){ return t && it.key.indexOf(t)>=0; }); });
        if(hits.length){ render(hits,'見つかった項目（押すとその機能が開きます）'); return; }
        render([],'🤖 AIに聞いています…');
        tmr=setTimeout(function(){
          fetch('/api/menu_search',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({q:q,labels:items.map(function(it){ return it.label; })})})
          .then(function(r){ return r.json(); })
          .then(function(d){
            if(d.message){ render([],d.message); return; }
            var got=(d.idx||[]).map(function(i){ return items[i]; }).filter(Boolean);
            render(got, got.length?('🤖 AIが選んだ候補'):'該当が見つかりませんでした');
          }).catch(function(){ render([],'AI検索に届きませんでした'); });
        },450);
      });
    })();
    // 🖱 メニュー自体を掴んで動かせるようにする（下に隠れた要素を見たい時に邪魔になるため）。
    //   ボタン・入力の上では動かさない＝押す操作は今まで通り。位置はこのPCに記憶する。
    (function(){
      var dg=false, sx=0, sy=0, ol=0, ot=0;
      qm.addEventListener('mousedown',function(ev){
        if(ev.button!==0) return;
        if(qm.__qeOn) return;                                     // 並べ替えモードは従来の仕組みを使う
        var t=ev.target;
        if(t.closest&&t.closest('button,input,select,textarea,label,a,.__ce_qi,.__ce_sub')) return;
        var r=qm.getBoundingClientRect();
        dg=true; sx=ev.clientX; sy=ev.clientY; ol=r.left; ot=r.top;
        qm.style.left=ol+'px'; qm.style.top=ot+'px'; qm.style.right='auto'; qm.style.bottom='auto';
        ev.preventDefault(); ev.stopPropagation();
        var mv=function(e2){
          if(!dg) return;
          var nl=Math.max(0,Math.min(ol+(e2.clientX-sx), window.innerWidth-60));
          var nt=Math.max(0,Math.min(ot+(e2.clientY-sy), window.innerHeight-40));
          qm.style.left=nl+'px'; qm.style.top=nt+'px';
        };
        var up=function(){
          dg=false;
          document.removeEventListener('mousemove',mv,true); document.removeEventListener('mouseup',up,true);
          var rr=qm.getBoundingClientRect();
          lastMenuPos={left:rr.left, top:rr.top};
          try{ localStorage.setItem('__ce_menupos',JSON.stringify(lastMenuPos)); }catch(_){}
        };
        document.addEventListener('mousemove',mv,true);
        document.addEventListener('mouseup',up,true);
      },true);
      qm.style.cursor='default';
    })();
    qmWireGroups(qm);
    qm.querySelector('#__ce_q_edit').addEventListener('click',function(ev){ ev.stopPropagation(); qmEditMode(qm); });
    qm.querySelector('#__ce_q_sckey').addEventListener('click',function(ev){ ev.stopPropagation(); closeMenu(); scOpenSettings(); });
    // 🅰 まとめて文字調整の配線（メニューを閉じずにその場で効く・インラインstyle!important＝どのCSSにも勝つ）
    if(multi&&qm.querySelector('#__ce_mf_f')){
      var mfEach=function(fn){ selEls.forEach(function(x){ try{ pushUndo(x); fn(x); }catch(_){} }); try{ markDirty(); }catch(_){} };
      // 当てたあと、今かかっている値を読み直して表示に反映する（＝押した結果がその場で分かる）
      var mfSync=function(){
        var g=function(get){ var s=[]; selEls.forEach(function(x){ var v; try{ v=get(getComputedStyle(x)); }catch(_){ return; } if(v!=null&&v!==''&&s.indexOf(v)<0) s.push(v); }); return s; };
        var hx=function(c){ var m=/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/.exec(c||''); return m?('#'+[1,2,3].map(function(i){ return ('0'+(+m[i]).toString(16)).slice(-2); }).join('')):''; };
        var s=g(function(cs){ return Math.round((parseFloat(cs.fontSize)||0)*10)/10; });
        var w=g(function(cs){ return cs.fontWeight; });
        var c=g(function(cs){ return cs.color; });
        var f=g(function(cs){ return (cs.fontFamily||'').split(',')[0].trim().replace(/["']/g,''); });
        var si=qm.querySelector('#__ce_mf_s');
        if(si&&document.activeElement!==si){ si.value=(s.length===1?s[0]:''); si.placeholder=(s.length>1?'バラバラ':''); }
        var sn=qm.querySelector('#__ce_mf_sn'); if(sn) sn.textContent=(s.length>1?('今 '+s.slice(0,4).join(' / ')+'px'):'');
        var wn=qm.querySelector('#__ce_mf_wn'); if(wn) wn.textContent='今 '+(w.length===1?w[0]:(w.length?'バラバラ':'—'));
        var cn=qm.querySelector('#__ce_mf_cn'); if(cn) cn.textContent='今 '+(c.length===1?(hx(c[0])||c[0]):'バラバラ');
        var ci=qm.querySelector('#__ce_mf_c'); if(ci&&c.length===1&&hx(c[0])) ci.value=hx(c[0]);
        // フォント選択も今の値に合わせる（無い名前ならリストに足してから選ぶ）
        var fi=qm.querySelector('#__ce_mf_f');
        if(fi&&f.length===1){
          var found=false;
          [].slice.call(fi.options).forEach(function(o){ if(o.value===f[0]) found=true; });
          if(!found){ var og=document.createElement('optgroup'); og.label='今のフォント';
            var op=document.createElement('option'); op.value=f[0]; op.textContent=f[0]; og.appendChild(op);
            fi.insertBefore(og, fi.children[1]||null); }
          fi.value=f[0];
        }
      };
      qm.querySelector('#__ce_mf_f').addEventListener('change',function(ev){ ev.stopPropagation();
        var v=this.value; if(!v) return;
        ensureGoogleFont(v);
        // 完全なfont-family(カンマ/引用符入り＝おすすめリスト)はそのまま／フォント名1個(ページ使用)はsans-serifを添える
        var fam=(v.indexOf(',')>=0||v.indexOf("'")>=0||v.indexOf('"')>=0)? v : ('"'+v+'", sans-serif');
        mfEach(function(x){ x.style.setProperty('font-family', fam,'important'); });
        setTimeout(mfSync,0);
      });
      qm.querySelector('#__ce_mf_f').addEventListener('click',function(ev){ ev.stopPropagation(); });
      qm.querySelector('#__ce_mf_m').addEventListener('click',function(ev){ ev.stopPropagation();
        mfEach(function(x){ var fs=parseFloat(getComputedStyle(x).fontSize)||16; x.style.setProperty('font-size',Math.max(8,fs-2)+'px','important'); });
        mfSync();
      });
      qm.querySelector('#__ce_mf_p').addEventListener('click',function(ev){ ev.stopPropagation();
        mfEach(function(x){ var fs=parseFloat(getComputedStyle(x).fontSize)||16; x.style.setProperty('font-size',(fs+2)+'px','important'); });
        mfSync();
      });
      // px直接入力（Enterでも入力中でも即反映）＝「今いくつか」を見ながら数値で決められる
      var _mfs=qm.querySelector('#__ce_mf_s');
      _mfs.addEventListener('click',function(ev){ ev.stopPropagation(); });
      _mfs.addEventListener('keydown',function(ev){ ev.stopPropagation(); if(ev.key==='Enter') this.blur(); });
      _mfs.addEventListener('change',function(ev){ ev.stopPropagation();
        var n=parseFloat(this.value); if(!(n>0)) return;
        mfEach(function(x){ x.style.setProperty('font-size', Math.max(6,n)+'px','important'); });
        mfSync();
      });
      qm.querySelector('#__ce_mf_b').addEventListener('click',function(ev){ ev.stopPropagation();
        mfEach(function(x){ x.style.setProperty('font-weight','700','important'); });
        mfSync();
      });
      qm.querySelector('#__ce_mf_n').addEventListener('click',function(ev){ ev.stopPropagation();
        mfEach(function(x){ x.style.setProperty('font-weight','400','important'); });
        mfSync();
      });
      var _mfc=qm.querySelector('#__ce_mf_c');
      _mfc.addEventListener('click',function(ev){ ev.stopPropagation(); });
      _mfc.addEventListener('input',function(){ var v=this.value; selEls.forEach(function(x){ x.style.setProperty('color',v,'important'); }); mfSync(); });
      _mfc.addEventListener('change',function(){ try{ markDirty(); }catch(_){} });
    }
    // 📐 サイズをそろえるの配線（メニューは閉じない＝大きい方→小さい方と試し比べできる）
    if(multi&&qm.querySelector('#__ce_sz_max')){
      // 1回当てて実測し、ズレていたら比率で当て直す（★これが要）。
      // px指定どおりの見た目にならない原因は現場に山ほどある：枠のscale縮小・box-sizing・
      // 元CSSのmin-height/max-width・元から付いているtransform。原因を1つずつ潰すより
      // 「当てる→測る→ズレた分だけ直す」を数回まわすほうが確実に揃う。
      var szFit=function(box,tw,th,ax,isImg){
        // ★測るのは offsetWidth/offsetHeight（レイアウトの箱）。getBoundingClientRect は
        //   出現アニメで transform が動いている最中の値を返すことがあり、その動いている数字で
        //   計算すると、アニメが終わった瞬間に55px小さい…という取りこぼしになる（実測で確認）。
        var pw=-1, ph=-1;
        var mw=function(){ return box.offsetWidth||box.getBoundingClientRect().width; };
        var mh=function(){ return box.offsetHeight||box.getBoundingClientRect().height; };
        for(var pass=0; pass<4; pass++){
          var rw=mw(), rh=mh();
          if(!(rw>0&&rh>0)) return null;
          var okW=(ax==='h')||Math.abs(rw-tw)<=1, okH=(ax==='w')||Math.abs(rh-th)<=1;
          if(pass&&okW&&okH) break;
          if(pass&&Math.abs(rw-pw)<0.5&&Math.abs(rh-ph)<0.5) break;   // 動かなくなった＝中身が支えている
          pw=rw; ph=rh;
          if(ax!=='h'){
            var wv=pass?((parseFloat(box.style.width)||rw)+(tw-rw)):tw;   // ズレた分だけ足し引き
            box.style.setProperty('width', Math.max(20,Math.round(wv*100)/100)+'px','important');
          }
          if(ax!=='w'){
            var drv=isImg?(parseFloat(box.style.height)||rh):(parseFloat(box.style.minHeight)||rh);
            var hv=Math.max(20,Math.round((pass?drv+(th-rh):th)*100)/100);
            if(isImg){ box.style.setProperty('height', hv+'px','important'); box.style.setProperty('min-height','0','important'); }
            else { box.style.setProperty('min-height', hv+'px','important'); if(box.style.height) box.style.setProperty('height', hv+'px','important'); }
          }
        }
        return {width:mw(), height:mh()};
      };
      var szDo=function(mode){
        var axs=qm.querySelector('#__ce_sz_ax'), ax=(axs&&axs.value)||'wh';
        var list=[], seen=[];
        selEls.forEach(function(x){
          var box=szBox(x);                       // 画像は切り取り枠＝見えている箱をそろえる
          if(seen.indexOf(box)>=0) return;        // 画像と枠を両方選んでいても1回だけ
          var r=box.getBoundingClientRect(); if(!(r.width>0&&r.height>0)) return;
          seen.push(box);
          list.push({el:box, inner:(box!==x?x:szInnerImg(box)), w:r.width, h:r.height,
                     img:(box.tagName==='IMG'||!!(box.querySelector&&box.querySelector('img')))});
        });
        if(list.length<2) return;
        // 「幅だけ」なら幅で、「高さだけ」なら高さで、「両方」なら面積でお手本の1個を選ぶ
        var key=(ax==='w')?function(o){return o.w;}:(ax==='h')?function(o){return o.h;}:function(o){return o.w*o.h;};
        var srt=list.slice().sort(function(a,b){ return key(a)-key(b); });
        var ref=(mode==='max')?srt[srt.length-1]:((mode==='min')?srt[0]:srt[(srt.length-1)>>1]);
        var tw=Math.round(ref.w), th=Math.round(ref.h), ng=0;
        list.forEach(function(o){
          var box=o.el;
          try{ _freezeSiblings(box); }catch(_){}   // 隣の列・兄弟が一緒に動かないよう先に凍結
          // 枠が縮小(scale)されていると「幅300px」と書いても見た目は300pxにならない＝等倍に戻す
          if(((+box.getAttribute('data-cesx'))||1)!==1||((+box.getAttribute('data-cesy'))||1)!==1){
            box.setAttribute('data-cesx',1); box.setAttribute('data-cesy',1); try{ applyTf(box); }catch(_){}
          }
          box.style.setProperty('box-sizing','border-box','important');   // 枠線・内側余白ぶん太らないように
          if(ax!=='h') box.style.setProperty('max-width','none','important');
          if(ax!=='w') box.style.setProperty('max-height','none','important');
          if(o.inner){   // 枠をそろえた時は、中の画像を枠いっぱいに敷き直す
            o.inner.style.setProperty('width','100%','important');
            o.inner.style.setProperty('height','100%','important');
            o.inner.style.setProperty('max-width','none','important');
            o.inner.style.setProperty('max-height','none','important');
            o.inner.style.setProperty('object-fit','cover','important');
          }
          if(box.tagName==='IMG'){ box.style.setProperty('object-fit','cover','important'); }   // 比率が違っても潰さず切り取り
          else if(o.img){ box.style.setProperty('overflow','hidden','important'); }
          var got=szFit(box,tw,th,ax,o.img);
          if(got&&(((ax!=='h')&&Math.abs(got.width-tw)>2)||((ax!=='w')&&Math.abs(got.height-th)>2))) ng++;
        });
        try{ markDirty(); }catch(_){}
        if(msg) msg.textContent='📐 '+list.length+'個を'+((mode==='max')?'大きい方':((mode==='min')?'小さい方':'中央値'))
          +'にそろえました（'+((ax==='w')?('幅'+tw+'px'):((ax==='h')?('高さ'+th+'px'):(tw+'×'+th+'px')))+'）。'
          +(ng?('⚠'+ng+'個は中身が入りきらず揃いませんでした（文字が多い等）。'):'')+'💾保存で残ります・⟲で戻せます';
      };
      qm.querySelector('#__ce_sz_ax').addEventListener('click',function(ev){ ev.stopPropagation(); });
      qm.querySelector('#__ce_sz_max').addEventListener('click',function(ev){ ev.stopPropagation(); szDo('max'); });
      qm.querySelector('#__ce_sz_mid').addEventListener('click',function(ev){ ev.stopPropagation(); szDo('mid'); });
      qm.querySelector('#__ce_sz_min').addEventListener('click',function(ev){ ev.stopPropagation(); szDo('min'); });
    }
    // 📐 位置をそろえるの配線（メニューは閉じない＝左→中央と当て比べできる）
    if(multi&&qm.querySelector('.__ce_alb')){
      // ★位置は getBoundingClientRect（＝見た目の箱）で読む。
      //   このツールが要素を動かすのに使うのは単体プロパティ translate なので offsetLeft には
      //   移動ぶんが入らない（§7 ㉕の地雷）。rect には入るので、rectで読んで
      //   「足りない差のぶんだけ translate を足す」なら座標の原点が何であっても合う。
      // ★translate はレイアウトを動かさない＝1個動かしても他の要素の位置は変わらない。
      //   だから「全員の今の位置を先に測る→順に寄せる」で破綻しない（reflowの心配がない）。
      var alBox=function(el){
        var r=el.getBoundingClientRect();
        return {l:r.left,t:r.top,r:r.right,b:r.bottom,w:r.width,h:r.height,cx:r.left+r.width/2,cy:r.top+r.height/2};
      };
      var alVal=function(b,k){
        return (k==='l'||k==='dx')?b.l:(k==='r')?b.r:(k==='cx')?b.cx:(k==='t'||k==='dy')?b.t:(k==='b')?b.b:b.cy;
      };
      var alMove=function(el,dx,dy){
        var tx=(+el.getAttribute('data-cetx')||0)+dx, ty=(+el.getAttribute('data-cety')||0)+dy;
        setPos(el, Math.round(tx*100)/100, Math.round(ty*100)/100);
      };
      var alList=function(){
        var out=[], seen=[];
        selEls.forEach(function(x){
          if(!x) return;
          var box=szBox(x);                        // 画像は切り取り枠＝見えている箱をそろえる（サイズそろえと同じ流儀）
          if(seen.indexOf(box)>=0||_undraggable(box)) return;
          var b; try{ b=alBox(box); }catch(_){ return; }
          if(!(b.w>0&&b.h>0)) return;
          seen.push(box); out.push({el:box,b:b});
        });
        return out;
      };
      var alDo=function(k){
        var list=alList();
        if(list.length<2){ if(msg) msg.textContent='そろえるには2個以上を選んでください（Ctrl+クリックで足せます）'; return; }
        var vert=(k==='t'||k==='cy'||k==='b'||k==='dy');
        var refm='edge'; var _rs=qm.querySelector('#__ce_al_ref'); if(_rs&&_rs.value) refm=_rs.value;
        var want=[], label='', extra='';
        if(k==='dx'||k==='dy'){
          // 等間隔：両端は動かさず、間のものを「隙間が全部同じ」になる位置へ配る
          if(list.length<3){ if(msg) msg.textContent='等間隔は3個以上を選んでください（今 '+list.length+'個）'; return; }
          var srt=list.slice().sort(function(a,b){ return vert?(a.b.t-b.b.t):(a.b.l-b.b.l); });
          var span=vert?(srt[srt.length-1].b.b-srt[0].b.t):(srt[srt.length-1].b.r-srt[0].b.l);
          var used=0; srt.forEach(function(o){ used+=vert?o.b.h:o.b.w; });
          var gap=(span-used)/(srt.length-1);
          var cur=vert?srt[0].b.b:srt[0].b.r;
          for(var i=1;i<srt.length-1;i++){
            want.push({el:srt[i].el, tgt:cur+gap});
            cur=cur+gap+(vert?srt[i].b.h:srt[i].b.w);
          }
          label=(vert?'⇳ たて':'⇹ よこ')+'に等間隔で並べ'; extra='隙間は '+Math.round(gap)+'px。';
        } else {
          // 端そろえ：目標の座標を1つ決めて、そこへ全員を寄せる
          var g;
          if(refm==='first'){
            g=alVal(list[0].b,k);                 // 最初に選んだものは動かさない＝そこが基準
          } else if(k==='cx'||k==='cy'){
            // 中央は「選んだもの全体のまん中」に寄せる。どれか1個の中心に寄せると全部が片側へ動いて見える
            var lo=Math.min.apply(null,list.map(function(o){ return vert?o.b.t:o.b.l; }));
            var hi=Math.max.apply(null,list.map(function(o){ return vert?o.b.b:o.b.r; }));
            g=(lo+hi)/2;
          } else {
            var vs=list.map(function(o){ return alVal(o.b,k); });
            g=(k==='l'||k==='t')?Math.min.apply(null,vs):Math.max.apply(null,vs);
          }
          list.forEach(function(o){ want.push({el:o.el, tgt:g}); });
          label={l:'⬅ 左',cx:'⬄ 中央',r:'➡ 右',t:'⬆ 上',cy:'⬍ 中',b:'⬇ 下'}[k]+'にそろえ';
        }
        // ★2周する：1周目で寄せたあと測り直し、残ったズレを詰める。
        //   親が縮小(scale)されていると translate 100px が見た目100pxにならないため、
        //   「当てる→測る→残りを足す」でしか最後の数pxは揃わない（サイズそろえと同じ考え方）。
        var moved=0;
        for(var pass=0;pass<2;pass++){
          want.forEach(function(o){
            var b; try{ b=alBox(o.el); }catch(_){ return; }
            var d=o.tgt-alVal(b,k);
            if(Math.abs(d)<0.5) return;
            if(vert) alMove(o.el,0,d); else alMove(o.el,d,0);
            if(!pass) moved++;
          });
        }
        try{ markDirty(); }catch(_){}
        if(msg) msg.textContent='🧲 '+label+'ました（'+list.length+'個のうち '+moved+'個を動かしました'
          +(refm==='first'&&k!=='dx'&&k!=='dy'?'・基準＝最初に選んだもの':'')+'）。'+extra+'💾保存で残ります・⟲で戻せます';
      };
      if(qm.querySelector('#__ce_al_ref')) qm.querySelector('#__ce_al_ref').addEventListener('click',function(ev){ ev.stopPropagation(); });
      [].slice.call(qm.querySelectorAll('.__ce_alb')).forEach(function(b){
        b.addEventListener('click',function(ev){ ev.stopPropagation(); alDo(this.getAttribute('data-k')); });
      });
    }
    // 📏 余白の定番の配線（メニューは閉じない＝5rem→10remと当て比べできる）
    if(qm.querySelector('#__ce_sp_ed')){
      var _spFixT=0;   // アニメ落ち着き待ちの当て直しタイマー（当て直す前に別の値を押されたら取り消す）
      var spEls=function(){ return selEls.length?selEls:(curEl?[curEl]:[]); };
      var spMd=function(){ var s=qm.querySelector('#__ce_sp_md'); return (s&&s.value)||'mt'; };
      var spNow=function(){
        var box=qm.querySelector('#__ce_sp_now'); if(!box) return;
        var m=spMd(), seen=[];
        if(m==='gap'){
          // 「今できている隙間」を実測で出す（CSSの値ではなく、目で見えているアキ）
          var ls=spEls().slice().sort(function(a,b){ return a.getBoundingClientRect().top-b.getBoundingClientRect().top; });
          if(ls.length>1){
            for(var i=1;i<ls.length;i++) seen.push(Math.round(ls[i].getBoundingClientRect().top-ls[i-1].getBoundingClientRect().bottom));
          } else if(ls.length===1){
            var rf=spRefAbove(ls[0]);
            if(rf) seen.push(Math.round(ls[0].getBoundingClientRect().top-rf.getBoundingClientRect().bottom));
          }
          box.textContent = seen.length? ('今の隙間 '+seen.slice(0,5).join(' / ')+'px') : '';
          return;
        }
        if(m==='gapb'){
          spEls().forEach(function(x){
            var rb=spRefBelow(x); if(!rb) return;
            seen.push(Math.round(rb.getBoundingClientRect().top-x.getBoundingClientRect().bottom));
          });
          box.textContent = seen.length? ('今の下の隙間 '+seen.slice(0,5).join(' / ')+'px') : '';
          return;
        }
        var prop=(m.charAt(0)==='m')?'margin':'padding', side=(m==='mb')?'Bottom':'Top';
        spEls().forEach(function(x){
          var v; try{ v=Math.round(parseFloat(getComputedStyle(x)[prop+side])||0); }catch(_){ return; }
          if(seen.indexOf(v)<0) seen.push(v);
        });
        box.textContent = seen.length? ('今 '+seen.slice(0,5).join(' / ')+'px'+(seen.length>1?'（バラバラ）':'（そろっています）')) : '';
      };
      // 指定値(10rem/24px/2em)を px にする
      var spPx=function(v,el){
        var n=parseFloat(v)||0;
        if(/rem\\s*$/.test(v)){ var rp=16; try{ rp=parseFloat(getComputedStyle(document.documentElement).fontSize)||16; }catch(_){} return n*rp; }
        if(/em\\s*$/.test(v)){ var ep=16; try{ ep=parseFloat(getComputedStyle(el).fontSize)||16; }catch(_){} return n*ep; }
        return n;
      };
      // ★自由配置(position:absolute/fixed)の要素は margin/padding を入れても1pxも動かない
      //   （値は入るのに見た目が変わらない＝一番気づけない失敗）。この場合は「すぐ上にあるものとの
      //   隙間」を指定値にするよう、要素自体を縦に動かす＝ユーザーの狙い（間隔をそろえる）を満たす。
      // ★基準（上にあるもの）はパネルを開いている間ずっと固定する。毎回選び直すと、下へ動かした拍子に
      //   別の要素を追い越して「その追い越した相手」が新しい基準になり、10rem→5remで上がらず下がる
      //   （実測で再現）。＝1回選んだ相手を覚えて、当て直しでも同じ相手との隙間を作る。
      // すぐ上にあるものを探す。★一度選んだ相手は覚えておく（パネルを開いている間は固定）。
      //   毎回選び直すと、下へ動かした拍子に別の要素を追い越して基準が入れ替わり、10rem→5remで
      //   上がらず下がる（実測で再現）。
      var spRefAbove=function(x){
        var ref=x.__ceSpRef; if(ref&&ref.isConnected) return ref;
        var r=x.getBoundingClientRect();
        var host=(x.closest&&x.closest('section,header,footer,main'))||document.body;
        var best=null;
        [].slice.call(host.querySelectorAll('*')).forEach(function(n){
          if(n===x||x.contains(n)||n.contains(x)||_inUI2(n)) return;
          var nr=n.getBoundingClientRect();
          if(!(nr.width>4&&nr.height>4)||nr.bottom>r.top+1) return;      // 上にあるものだけ
          var cs; try{ cs=getComputedStyle(n); }catch(_){ return; }
          if(cs.visibility==='hidden'||(parseFloat(cs.opacity)||0)<0.05) return;
          var ov=Math.min(nr.right,r.right)-Math.max(nr.left,r.left);
          if(ov<Math.min(nr.width,r.width)*0.25) return;                 // 横がある程度重なっているものだけ
          if(!best||nr.bottom>best.b) best={el:n,b:nr.bottom};
        });
        if(!best) return null;
        return (x.__ceSpRef=best.el);
      };
      // すぐ下にあるものを探す（spRefAboveの下方向版）。★同じく一度選んだ相手は覚えて固定する。
      var spRefBelow=function(x){
        var ref=x.__ceSpRefB; if(ref&&ref.isConnected) return ref;
        var r=x.getBoundingClientRect();
        var host=(x.closest&&x.closest('section,header,footer,main'))||document.body;
        var best=null;
        [].slice.call(host.querySelectorAll('*')).forEach(function(n){
          if(n===x||x.contains(n)||n.contains(x)||_inUI2(n)) return;
          var nr=n.getBoundingClientRect();
          if(!(nr.width>4&&nr.height>4)||nr.top<r.bottom-1) return;      // 下にあるものだけ
          var cs; try{ cs=getComputedStyle(n); }catch(_){ return; }
          if(cs.visibility==='hidden'||(parseFloat(cs.opacity)||0)<0.05) return;
          var ov=Math.min(nr.right,r.right)-Math.max(nr.left,r.left);
          if(ov<Math.min(nr.width,r.width)*0.25) return;                 // 横がある程度重なっているものだけ
          if(!best||nr.top<best.t) best={el:n,t:nr.top};
        });
        if(!best) return null;
        // ★見つかったのが「01」のような小さな中身なら、行の器まで遡って器ごと動かす
        //   （中身だけ動かすと行の残り（見出し・本文）が置いていかれてバラける）。
        //   親の上端が選択側と縦に重なったら器を超えたのでそこで止める。
        var b=best.el;
        while(b.parentElement){
          var pE=b.parentElement;
          if(/^(SECTION|HEADER|FOOTER|MAIN|BODY|HTML)$/.test(pE.tagName)||_inUI2(pE)) break;
          if(pE.contains(x)) break;
          if(pE.getBoundingClientRect().top<r.bottom-1) break;
          b=pE;
        }
        return (x.__ceSpRefB=b);
      };
      // ★「上のもの(prev)との隙間が、結果的にちょうど px になる」ようにする。
      //   足し算(＋10rem)でも「margin:10rem」でもなく、実測 → 足りない分だけ動かす → もう一度実測。
      //   自由配置は margin が1pxも効かないので移動で、それ以外は margin-top を差分で増減して作る。
      var spSetGap=function(prev,x,px){
        var from=null, usedTr=false;
        for(var pass=0; pass<3; pass++){
          var gap=x.getBoundingClientRect().top-prev.getBoundingClientRect().bottom;
          if(from===null) from=gap;
          var d=px-gap;
          if(Math.abs(d)<0.6) break;
          var pos=''; try{ pos=getComputedStyle(x).position; }catch(_){}
          if(pos==='absolute'||pos==='fixed'||usedTr){
            try{ setPos(x,(+x.getAttribute('data-cetx')||0),(+x.getAttribute('data-cety')||0)+d); }catch(_){ return null; }
          }else{
            var pt=prev.getBoundingClientRect().top;
            var mt=0; try{ mt=parseFloat(getComputedStyle(x).marginTop)||0; }catch(_){}
            x.style.setProperty('margin-top', Math.round((mt+d)*100)/100+'px','important');
            // ★marginを足した反動で基準(prev)側まで動く親がある（高さ固定のflex等＝実報告：
            //   下を広げたら上の要素が上に逃げた）。動いたらmarginを取り消して、以後は
            //   translate（レイアウトに響かない移動）で隙間を作る。
            if(Math.abs(prev.getBoundingClientRect().top-pt)>1){
              x.style.setProperty('margin-top', Math.round(mt*100)/100+'px','important');
              usedTr=true;
            }
          }
        }
        var got=x.getBoundingClientRect().top-prev.getBoundingClientRect().bottom;
        return {from:Math.round(from), to:Math.round(got), ok:Math.abs(got-px)<=1.5};
      };
      var spDo=function(v){
        var m=spMd();
        var zy=qm.querySelector('#__ce_sp_zy'), doZy=!!(zy&&zy.checked);
        var els=spEls(); if(!els.length) return;
        if(doZy) els.forEach(function(x){   // ドラッグの縦ズレを先に0へ戻してから隙間を作る
          if(+x.getAttribute('data-cety')||0){ try{ setPos(x,(+x.getAttribute('data-cetx')||0),0); }catch(_){} }
        });
        var res=[], ng=0, isGap=(m==='gap'||m==='gapb');
        var spRun=function(){
          res=[]; ng=0;
          if(!isGap){
            els.forEach(function(x){
              if(m==='mt') x.style.setProperty('margin-top', v,'important');
              else if(m==='mb') x.style.setProperty('margin-bottom', v,'important');
              else { x.style.setProperty('padding-top', v,'important'); x.style.setProperty('padding-bottom', v,'important'); }
            });
            return;
          }
          var ls=els.slice().sort(function(a,b){ return a.getBoundingClientRect().top-b.getBoundingClientRect().top; });
          var px=spPx(v,ls[0]);
          if(m==='gapb'){
            // 下との隙間＝下にあるものを動かして作る（marginが効かない自由配置でも確実に空く）
            ls.forEach(function(x){
              var rb=spRefBelow(x); if(!rb){ ng++; return; }
              // ★同じセクション内で「選択より下にある自由配置」を先に控える。器(rb)を動かしても
              //   絶対配置の迷子（過去ドラッグの焼き込み）は付いてこないので、動かなかった子は
              //   後から同じ量だけ一緒に動かす（＝行がバラけない）。
              var host=(x.closest&&x.closest('section,header,footer,main'))||document.body;
              var xb=x.getBoundingClientRect().bottom;
              var strays=[];
              [].slice.call(host.querySelectorAll('*')).forEach(function(n){
                if(n===x||n===rb||n.contains(x)||x.contains(n)||n.contains(rb)||_inUI2(n)) return;
                var ps=''; try{ ps=getComputedStyle(n).position; }catch(_){ return; }
                if(ps!=='absolute'&&ps!=='fixed') return;
                var nr=n.getBoundingClientRect();
                if(nr.top>=xb-1&&nr.width>4&&nr.height>4) strays.push({el:n,t0:nr.top});
              });
              var g=spSetGap(x,rb,px);
              if(g){ res.push(g.from+'→'+g.to+'px'); if(!g.ok) ng++;
                var d2=g.to-g.from;
                if(Math.abs(d2)>0.5){
                  var un=strays.filter(function(s){ return Math.abs(s.el.getBoundingClientRect().top-s.t0)<0.5; });
                  un=un.filter(function(s){ return !un.some(function(o){ return o!==s&&o.el.contains(s.el); }); });  // 入れ子は外側だけ
                  un.forEach(function(s){ try{ setPos(s.el,(+s.el.getAttribute('data-cetx')||0),(+s.el.getAttribute('data-cety')||0)+d2); }catch(_){} });
                }
              } else ng++;
            });
            return;
          }
          if(ls.length>1){
            for(var i=1;i<ls.length;i++){
              var g=spSetGap(ls[i-1],ls[i],px);
              if(g){ res.push(g.from+'→'+g.to+'px'); if(!g.ok) ng++; } else ng++;
            }
          }else{
            var rf=spRefAbove(ls[0]);
            if(!rf){ ng++; return; }
            var g1=spSetGap(rf,ls[0],px);
            if(g1){ res.push(g1.from+'→'+g1.to+'px'); if(!g1.ok) ng++; } else ng++;
          }
        };
        // ★margin/padding系は「値は入るのに1pxも動かない」事故が起きうる（自由配置・親の高さ固定等）。
        //   当てる前後で実際に動いたかを測って、動いていなければ⚠で正直に言う（黙って成功と言わない）。
        var freeN=0, h0=0, t0=[];
        if(!isGap){
          els.forEach(function(x){ var ps=''; try{ ps=getComputedStyle(x).position; }catch(_){} if(ps==='absolute'||ps==='fixed') freeN++; });
          h0=document.scrollingElement.scrollHeight;
          t0=els.map(function(x){ return x.getBoundingClientRect().top; });
        }
        spRun();
        var spWarn='';
        if(!isGap){
          var moved=(Math.abs(document.scrollingElement.scrollHeight-h0)>0.5);
          els.forEach(function(x,i){ if(Math.abs(x.getBoundingClientRect().top-t0[i])>0.5) moved=true; });
          if(freeN) spWarn='　⚠'+freeN+'個は自由配置＝margin/paddingでは動きません。そろえ方を「下との隙間＝結果その値」にすると空きます';
          else if(!moved) spWarn='　⚠見た目は1pxも動いていません（元からその値だったか、親の高さが固定等）。動かないときは「隙間＝結果その値」が確実です';
        }
        // ★出現アニメでtransformが動いている最中に測ると1回目だけズレる（実測）。
        //   0.7秒後＝アニメが落ち着いてからもう一度合わせ直して、必ず指定どおりの隙間にする。
        //   ★前の値の当て直しが残っていると次の値を上書きしてしまう（5rem→10remが80pxに戻る）ので必ず取り消す。
        if(_spFixT){ clearTimeout(_spFixT); _spFixT=0; }
        if(isGap) _spFixT=setTimeout(function(){ _spFixT=0; try{ spRun(); markDirty(); spNow(); }catch(_){} },700);
        try{ markDirty(); }catch(_){}
        setTimeout(spNow,0);
        if(document.getElementById('__ce_ruler')) setTimeout(spRulerDraw,750);   // ものさし表示中なら測り直す
        if(msg){
          msg.textContent = isGap
            ? ('📏 '+(m==='gapb'?'下との':'上との')+'隙間を '+v+'（'+Math.round(spPx(v,els[0]))+'px）にしました：'+(res.join(' / ')||'（対象なし）')
               +(ng?('　⚠'+ng+'件は届きませんでした（'+(m==='gapb'?'下':'上')+'に基準が無い等）'):'')+'。💾保存で残ります・⟲で戻せます')
            : ('📏 '+els.length+'個の '+({mt:'margin-top',mb:'margin-bottom',ptb:'padding上下'}[m])+' を '+v+' にしました'+spWarn+'。💾保存で残ります・⟲で戻せます');
        }
      };
      // 📐 余白ものさし：今の隙間を全部その場に数字で出す。役割で色分け＝「全部同じ値」にしない前提
      //   （🟣セクション間 / 🟠見出し下 / 🔵本文間。同じ色の中でバラついている所だけそろえる、が使い方）
      var spRulerOff=function(){ var o=document.getElementById('__ce_ruler'); if(o) o.remove(); };
      var spRulerDraw=function(){
        spRulerOff();
        var ov=document.createElement('div'); ov.id='__ce_ruler';
        ov.style.cssText='position:absolute;left:0;top:0;width:100%;height:0;pointer-events:none;z-index:2147483600';
        var rootPx=16; try{ rootPx=parseFloat(getComputedStyle(document.documentElement).fontSize)||16; }catch(_){}
        var sy=window.scrollY||0, sx=window.scrollX||0;
        var isHead=function(n){ if(/^H[1-6]$/.test(n.tagName)) return true;
          var cs; try{ cs=getComputedStyle(n); }catch(_){ return false; }
          return (parseFloat(cs.fontSize)||0)>=24&&(parseInt(cs.fontWeight,10)||400)>=600; };
        var chip=function(prev,next,kind){
          var a=prev.getBoundingClientRect(), b=next.getBoundingClientRect();
          var g=Math.round(b.top-a.bottom); if(g<2||g>800) return;
          var col=(kind==='sec')?'#8e44ad':(kind==='head')?'#e67e22':'#2980b9';
          var x=Math.max(a.left,b.left)+Math.min(a.width,b.width)/2;
          var ln=document.createElement('div');
          ln.style.cssText='position:absolute;width:0;border-left:2px dashed '+col+';opacity:.55';
          ln.style.left=Math.round(sx+x)+'px'; ln.style.top=Math.round(sy+a.bottom)+'px'; ln.style.height=g+'px';
          var d=document.createElement('div');
          d.style.cssText='position:absolute;transform:translate(-50%,-50%);background:'+col+';color:#fff;font:700 11px/1.7 sans-serif;padding:0 7px;border-radius:9px;white-space:nowrap;box-shadow:0 1px 4px rgba(0,0,0,.35)';
          d.style.left=Math.round(sx+x)+'px'; d.style.top=Math.round(sy+(a.bottom+b.top)/2)+'px';
          d.textContent=g+'px（'+(Math.round(g/rootPx*10)/10)+'rem）';
          ov.appendChild(ln); ov.appendChild(d);
        };
        var vis=function(n){ var r=n.getBoundingClientRect(); if(!(r.width>30&&r.height>8)) return null;
          var cs; try{ cs=getComputedStyle(n); }catch(_){ return null; }
          if(cs.visibility==='hidden'||(parseFloat(cs.opacity)||0)<0.05) return null; return r; };
        var secs=[].slice.call(document.querySelectorAll('section,header,footer')).filter(function(s){ return !_inUI2(s)&&vis(s); });
        secs.sort(function(a,b){ return a.getBoundingClientRect().top-b.getBoundingClientRect().top; });
        for(var i=1;i<secs.length;i++) if(!secs[i-1].contains(secs[i])&&!secs[i].contains(secs[i-1])) chip(secs[i-1],secs[i],'sec');
        secs.forEach(function(sec){
          var els=[].slice.call(sec.querySelectorAll('h1,h2,h3,h4,h5,h6,p,img,ul,ol,figure,blockquote,table')).filter(function(n){ return !_inUI2(n)&&vis(n); });
          els=els.filter(function(n){ return !els.some(function(o){ return o!==n&&o.contains(n); }); });
          els.sort(function(a,b){ return a.getBoundingClientRect().top-b.getBoundingClientRect().top; });
          for(var j=1;j<els.length;j++){
            var ra=els[j-1].getBoundingClientRect(), rb2=els[j].getBoundingClientRect();
            if(Math.min(ra.right,rb2.right)-Math.max(ra.left,rb2.left)<Math.min(ra.width,rb2.width)*0.25) continue;
            chip(els[j-1],els[j], isHead(els[j-1])?'head':'flow');
          }
        });
        document.body.appendChild(ov);
      };
      // 📏 1行←→板の開閉（このPCに記憶）
      var _spTg=qm.querySelector('#__ce_sp_tg');
      if(_spTg){
        _spTg.addEventListener('mouseenter',function(){ this.style.background='#eef4ff'; });
        _spTg.addEventListener('mouseleave',function(){ this.style.background='none'; });
        _spTg.addEventListener('click',function(ev){ ev.stopPropagation();
          var bd=qm.querySelector('#__ce_sp_body'); if(!bd) return;
          var on=(bd.style.display==='none');
          bd.style.display=on?'':'none';
          try{ localStorage.setItem('__ce_sp_open', on?'1':'0'); }catch(_){}
          var ar=this.querySelector('.__ce_sp_ar'); if(ar) ar.textContent=on?'▾':'▸';
          if(on) spNow();
        });
      }
      // 🤏 箱を中身にぴったり縮める：焼き込まれた height/min-height を外す（paddingは残す）
      var _fitB=qm.querySelector('#__ce_sp_fit');
      if(_fitB) _fitB.addEventListener('click',function(ev){ ev.stopPropagation();
        var x=selEls.length===1?selEls[0]:curEl; if(!x) return;
        try{ pushUndo(x); }catch(_){}
        var h0=x.offsetHeight;
        x.style.setProperty('height','auto','important');
        x.style.setProperty('min-height','0','important');
        try{ markDirty(); }catch(_){}
        var h1=x.offsetHeight;
        this.textContent='✓ 縮めました（'+h0+'px → '+h1+'px）';
        if(msg) msg.textContent='🤏 箱を中身ぴったりに縮めました：'+h0+'px → '+h1+'px（💾保存で確定・⟲で戻せます）';
        if(document.getElementById('__ce_ruler')) setTimeout(spRulerDraw,300);
        setTimeout(spNow,0);
      });
      qm.querySelector('#__ce_sp_rl').addEventListener('click',function(ev){ ev.stopPropagation();
        if(document.getElementById('__ce_ruler')){ spRulerOff(); if(msg) msg.textContent='📐 ものさしを消しました'; }
        else { spRulerDraw(); if(msg) msg.textContent='📐 今の隙間を表示中：🟣セクション間 / 🟠見出し下 / 🔵本文間。もう一度📐かEscで消えます（💾保存には写りません）'; }
      });
      spNow();
      [].slice.call(qm.querySelectorAll('.__ce_spv')).forEach(function(b){
        b.addEventListener('click',function(ev){ ev.stopPropagation(); spDo(b.getAttribute('data-v')); });
      });
      qm.querySelector('#__ce_sp_md').addEventListener('click',function(ev){ ev.stopPropagation(); });
      qm.querySelector('#__ce_sp_md').addEventListener('change',function(ev){ ev.stopPropagation();
        try{ localStorage.setItem('__ce_sp_mode', this.value); }catch(_){}
        spNow();
      });
      if(qm.querySelector('#__ce_sp_zy')) qm.querySelector('#__ce_sp_zy').addEventListener('click',function(ev){ ev.stopPropagation(); });
      qm.querySelector('#__ce_sp_ed').addEventListener('click',function(ev){ ev.stopPropagation();
        var cur=localStorage.getItem('__ce_sp_presets')||'5rem,10rem,14rem';
        var v=prompt('余白の定番をカンマ区切りで入れてください（例：5rem,10rem,14rem）。px・em でもOK・最大6個', cur);
        if(v==null) return;
        var _bad=[];
        v=v.split(',').map(function(s){ return s.trim(); }).filter(function(s){
          if(!s) return false;
          if(/^[0-9]*[.]?[0-9]+(rem|em|px|%)?$/.test(s)) return true;
          _bad.push(s); return false;                       // 引用符などが混じると空ボタンになるので弾く
        }).slice(0,6).join(',');
        if(!v){ if(msg) msg.textContent='📏 数値で入れてください（例：5rem,10rem,14rem）。使えなかった値：'+_bad.join(' / '); return; }
        try{ localStorage.setItem('__ce_sp_presets', v); }catch(_){}
        closeMenu();
        if(msg) msg.textContent='📏 余白の定番を「'+v+'」にしました（このPCに記憶・どのカンプでも使えます）。もう一度右クリックで出ます';
      });
    }
    // 🕳 穴を埋めるの配線（ホバーで対象を赤枠＝どれのことか目で分かる）
    if(dgQ.length && qm.querySelector('#__ce_dg_fix')){
      var _dgH0=document.scrollingElement.scrollHeight, _dgNow=qm.querySelector('#__ce_dg_now');
      var _dgSync=function(){
        var h=document.scrollingElement.scrollHeight;
        if(_dgNow) _dgNow.textContent='ページ '+_dgH0+'px → '+h+'px';
        if(msg) msg.textContent='ドラッグの跡の空白を埋めました：ページの高さ '+_dgH0+'px → '+h+'px（💾保存で確定・⟲戻すで取り消し）';
      };
      [].slice.call(qm.querySelectorAll('.__ce_dgz')).forEach(function(b){
        var n=dgQ[+b.getAttribute('data-i')].el;
        b.addEventListener('mouseenter',function(){ try{ n.style.setProperty('outline','2px solid #ff3b30','important'); }catch(_){} });
        b.addEventListener('mouseleave',function(){ try{ n.style.removeProperty('outline'); }catch(_){} });
      });
      qm.querySelector('#__ce_dg_fix').addEventListener('click',function(ev){ ev.stopPropagation();
        dgQ.forEach(function(o){ try{ o.el.style.removeProperty('outline'); dragBake(o); }catch(_){} });
        setTimeout(_dgSync,60); });
      qm.querySelector('#__ce_dg_rst').addEventListener('click',function(ev){ ev.stopPropagation();
        dgQ.forEach(function(o){ try{ dragUnbake(o.el); }catch(_){} }); setTimeout(_dgSync,60); });
    }
    // ➡ 右のはみ出しを直すの配線（直した瞬間にページ幅の変化を出す＝効いたのが目で分かる）
    if((owQ.length||owDX||owSK.length) && qm.querySelector('#__ce_ow_rst')){
      var _owW0=document.documentElement.scrollWidth;
      // 戻す用の控えは「今はみ出している物」＋「右へドラッグされている物」＋「貼り付く帯」
      var _owEls=owQ.map(function(o){ return o.el; }).concat(owSK.map(function(o){ return o.el; }));
      [].slice.call(document.querySelectorAll('[data-cetx]')).forEach(function(el){
        if(el.closest('[id^="__ce"]')) return;
        if((parseFloat(el.getAttribute('data-cetx'))||0)>2 && _owEls.indexOf(el)<0) _owEls.push(el);
      });
      var _owSnap=_owEls.map(function(el){ return {el:el, tr:el.style.translate||'', tx:el.getAttribute('data-cetx'),
        ty:el.getAttribute('data-cety'), mw:el.style.maxWidth||'', w:el.style.width||'', tf:el.style.transform||''}; });
      var _fixB=qm.querySelector('#__ce_ow_fix');
      if(_fixB) _fixB.addEventListener('click',function(ev){ ev.stopPropagation();
        overflowFix();
        if(msg) msg.textContent=msg.textContent+'（ページ幅 '+_owW0+'px → '+document.documentElement.scrollWidth+'px）';
      });
      var _dxB=qm.querySelector('#__ce_ow_dx');
      if(_dxB) _dxB.addEventListener('click',function(ev){ ev.stopPropagation(); ovDragXAll(); });
      var _skB=qm.querySelector('#__ce_ow_sk');
      if(_skB) _skB.addEventListener('click',function(ev){ ev.stopPropagation(); stickyFix(); });
      qm.querySelector('#__ce_ow_rst').addEventListener('click',function(ev){ ev.stopPropagation();
        _owSnap.forEach(function(s){
          if(s.tr) s.el.style.translate=s.tr; else s.el.style.removeProperty('translate');
          if(s.tf) s.el.style.transform=s.tf; else s.el.style.removeProperty('transform');
          if(s.w) s.el.style.width=s.w; else s.el.style.removeProperty('width');
          if(s.tx!=null) s.el.setAttribute('data-cetx',s.tx); else s.el.removeAttribute('data-cetx');
          if(s.ty!=null) s.el.setAttribute('data-cety',s.ty); else s.el.removeAttribute('data-cety');
          if(s.mw) s.el.style.maxWidth=s.mw; else s.el.style.removeProperty('max-width');
        });
        markDirty();
        if(msg) msg.textContent='➡ はみ出しの修正を取り消しました（ページ幅 '+document.documentElement.scrollWidth+'px）';
      });
    }
    // 📏 余白を詰めるの配線。★ページ全体の高さの変化を出す＝「効いたのか分からない」を無くす
    if(pdQ.length && qm.querySelector('#__ce_pd_fit')){
      var _pdH0=document.scrollingElement.scrollHeight, _pdNow=qm.querySelector('#__ce_pd_now');
      var _pdSync=function(){
        var h=document.scrollingElement.scrollHeight;
        if(_pdNow) _pdNow.textContent='ページ '+_pdH0+'px → '+h+'px（'+(_pdH0-h)+'px 縮んだ）';
        if(msg) msg.textContent='余白を詰めました：ページの高さ '+_pdH0+'px → '+h+'px（💾保存で確定・⟲戻すで取り消し）';
      };
      qm.querySelector('#__ce_pd_fit').addEventListener('click',function(ev){ ev.stopPropagation(); padCrush(pdQ); _pdSync(); });
      qm.querySelector('#__ce_pd_half').addEventListener('click',function(ev){ ev.stopPropagation();
        pdQ.forEach(function(o){ padShrink(o.el,0.5); }); _pdSync(); });
      qm.querySelector('#__ce_pd_rst').addEventListener('click',function(ev){ ev.stopPropagation();
        pdQ.forEach(function(o){ padReset(o.el); }); _pdSync(); });
    }
    // 🎞 スライドショーの配線（サムネを押すとその1枚に固定・メニューは開いたまま）
    [].slice.call(qm.querySelectorAll('.__ce_slz')).forEach(function(b){
      b.addEventListener('click',function(ev){
        ev.stopPropagation();
        sliderFreeze(slQ, +this.getAttribute('data-i'));
        [].slice.call(qm.querySelectorAll('.__ce_slz')).forEach(function(x){ x.style.borderColor='#cfe6db'; });
        this.style.borderColor='#0f766e';
      });
    });
    var _slOff=qm.querySelector('#__ce_slzoff');
    if(_slOff) _slOff.addEventListener('click',function(ev){ ev.stopPropagation(); sliderUnfreeze(slQ); closeMenu(); });
    // 🫥「すり抜ける絵」を選ぶ配線：pointer-eventsを戻してから、その要素で右クリックを開き直す
    [].slice.call(qm.querySelectorAll('.__ce_pez')).forEach(function(b){
      var n=peQ[+b.getAttribute('data-i')];
      b.addEventListener('mouseenter',function(){ try{ n.__pez=n.style.getPropertyValue('outline'); n.style.setProperty('outline','2px solid #ff3b30','important'); }catch(_){} });
      b.addEventListener('mouseleave',function(){ try{ if(n.__pez) n.style.setProperty('outline',n.__pez,'important'); else n.style.removeProperty('outline'); }catch(_){} });
      b.addEventListener('click',function(ev){
        ev.stopPropagation();
        try{ n.style.removeProperty('outline'); }catch(_){}
        _peWake(n);
        if(msg) msg.textContent='この絵を掴めるようにしました（移動・削除・差し替えができます／💾保存で確定・⟲戻すで取り消し）';
        closeMenu();
        _forceEl=n;
        n.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:qx,clientY:qy}));
      });
    });
    // 🎯 重なっているものの配線：触れると赤枠／押すとその要素を掴んで開き直す
    [].slice.call(qm.querySelectorAll('.__ce_ovz')).forEach(function(b){
      var c=ovQ[+b.getAttribute('data-i')]; if(!c) return;
      b.addEventListener('mouseenter',function(){
        try{ c.__ovz=c.style.getPropertyValue('outline'); c.style.setProperty('outline','2px solid #ff3b30','important'); }catch(_){}
      });
      b.addEventListener('mouseleave',function(){
        try{ if(c.__ovz) c.style.setProperty('outline',c.__ovz,'important'); else c.style.removeProperty('outline'); }catch(_){}
      });
      b.addEventListener('click',function(ev){ ev.stopPropagation();
        try{ c.style.removeProperty('outline'); }catch(_){}
        try{ if(_peWake(c)&&msg) msg.textContent='この飾りはクリックがすり抜ける設定でした→掴めるようにしました'; }catch(_){}
        _forceEl=c;
        c.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:qx,clientY:qy}));
      });
    });
    // 🖼 背景の絵・飾りのサムネ配線：触れると赤枠／押すと大きさ・位置パネルをその絵で開く
    [].slice.call(qm.querySelectorAll('.__ce_bgz')).forEach(function(b){
      var i=+b.getAttribute('data-i'), c=bgQ[i]; if(!c) return;
      b.addEventListener('mouseenter',function(){
        var t=c.el.tagName, nm=(t==='BODY'||t==='HTML')?'⚠ ページ全体の背景':(c.ps?'飾りの絵':'背景の絵');
        var r=c.el.getBoundingClientRect(), sz=Math.round(r.width)+'×'+Math.round(r.height);
        if(c.ps){ try{ var cs=getComputedStyle(c.el,c.ps); sz=Math.round(parseFloat(cs.width))+'×'+Math.round(parseFloat(cs.height)); }catch(_){} }
        grabHintShow(c.el, nm, c.url, sz, c.ps);
      });
      b.addEventListener('mouseleave',function(){ grabHintHide(); });
      b.addEventListener('click',function(ev){ ev.stopPropagation(); grabHintHide(); closeMenu(); openBgSizePanel(bgQ, i); });
    });
    // ⚠「絵が箱まで届いていません」→ 1クリックで箱いっぱいにする
    [].slice.call(qm.querySelectorAll('.__ce_bgfill')).forEach(function(b){
      var c=bgQ[+b.getAttribute('data-i')]; if(!c) return;
      b.addEventListener('mouseenter',function(){
        var r=c.el.getBoundingClientRect();
        grabHintShow(c.el, '背景の絵', c.url, Math.round(r.width)+'×'+Math.round(r.height), c.ps);
      });
      b.addEventListener('mouseleave',function(){ grabHintHide(); });
      b.addEventListener('click',function(ev){ ev.stopPropagation();
        try{ pushUndo(c.el); }catch(_){}
        bgpApply(c,'background-size','cover'); bgpApply(c,'background-repeat','no-repeat');
        try{ markDirty(); }catch(_){}
        this.textContent='✓ 箱いっぱいにしました';
        if(msg) msg.textContent='🖼 絵を箱いっぱいに広げました（はみ出た分は切れます・⟲で戻せます・💾保存で確定）';
      });
    });
    // ― 近くの薄い線の配線：ホバーで赤く光らせ、押したらその要素を選択して開き直す
    [].slice.call(qm.querySelectorAll('.__ce_lnz')).forEach(function(b){
      var it=lineQ[+b.getAttribute('data-i')]; if(!it) return;
      b.addEventListener('mouseenter',function(){ try{ it.el.__lnz=it.el.style.getPropertyValue('outline'); it.el.style.setProperty('outline','2px solid #ff3b30','important'); }catch(_){} });
      b.addEventListener('mouseleave',function(){ try{ if(it.el.__lnz) it.el.style.setProperty('outline',it.el.__lnz,'important'); else it.el.style.removeProperty('outline'); }catch(_){} });
      b.addEventListener('click',function(ev){ ev.stopPropagation();
        try{ it.el.style.removeProperty('outline'); }catch(_){}
        _forceEl=it.el;
        var r=it.el.getBoundingClientRect();
        it.el.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,
          clientX:Math.max(10,Math.min(r.left+r.width/2, window.innerWidth-20)),
          clientY:Math.max(10,Math.min(r.top+Math.min(12,r.height/2), window.innerHeight-20))}));
        if(msg) msg.textContent='― 線を選択しました（🖱掴んで動かす・削除・⚙メニューが使えます）';
      });
    });
    // ➖「見えている飾りを消す」の配線（選択の有無に関係なく、1件ずつトグルで消せる）
    [].slice.call(qm.querySelectorAll('.__ce_dcz')).forEach(function(b){
      var it=decoQ[+b.getAttribute('data-i')];
      // ホバー中だけ赤枠＝「どの飾りの話か」を目で確かめてから押せる（疑似要素は親に枠が出る）
      b.addEventListener('mouseenter',function(){ try{ it.el.__dcz=it.el.style.getPropertyValue('outline'); it.el.style.setProperty('outline','2px solid #ff3b30','important'); }catch(_){} });
      b.addEventListener('mouseleave',function(){ try{ if(it.el.__dcz) it.el.style.setProperty('outline',it.el.__dcz,'important'); else it.el.style.removeProperty('outline'); }catch(_){} });
      b.addEventListener('click',function(ev){
        ev.stopPropagation();
        try{ it.el.style.removeProperty('outline'); }catch(_){}
        var r=decoToggle(it);
        var sp=this.querySelector('span'); if(sp) sp.textContent=(r==='消しました'?'✓ 消した：':'↩ 戻した：')+it.name;
        if(msg) msg.textContent='飾りを'+r+'（💾保存で確定・⟲戻すで取り消し）';
      });
    });
    // 🎨 背景・フチ・影を消すの配線＝ホバーで赤枠→押すとトグル（何度でも戻せる）
    [].slice.call(qm.querySelectorAll('.__ce_flz')).forEach(function(b){
      var it=flatQ[+b.getAttribute('data-i')]; if(!it) return;
      var k=b.getAttribute('data-k'), lb0=b.textContent;
      b.addEventListener('mouseenter',function(){ try{ it.el.__flz=it.el.style.getPropertyValue('outline'); it.el.style.setProperty('outline','2px solid #ff3b30','important'); }catch(_){} });
      b.addEventListener('mouseleave',function(){ try{ if(it.el.__flz) it.el.style.setProperty('outline',it.el.__flz,'important'); else it.el.style.removeProperty('outline'); }catch(_){} });
      b.addEventListener('click',function(ev){
        ev.stopPropagation();
        try{ it.el.style.removeProperty('outline'); }catch(_){}
        var on;
        if(k==='all'){
          var want=(it.el.getAttribute('data-ceflatbg')==null);   // 1つも消していなければ「全部消す」
          ['bg','bd','sh'].forEach(function(k2){ if((it.el.getAttribute('data-ceflat'+k2)!=null)!==want) flatOff(it,k2); });
          on=want;
        } else on=flatOff(it,k);
        this.textContent=on?'↩ 戻す':lb0;
        if(msg) msg.textContent='🎨 '+(on?'透明にしました':'戻しました')+'（💾保存で確定・⟲戻すで取り消し）';
      });
    });
    // ◽ 裏の四角のカドを隠す（⌒角丸／✕見た目消す）の配線＝ホバーで赤枠→押すとトグル
    [].slice.call(qm.querySelectorAll('.__ce_rrz,.__ce_rhz')).forEach(function(b){
      var it=radQ[+b.getAttribute('data-i')]; if(!it) return;
      b.addEventListener('mouseenter',function(){ try{ it.el.__rrz=it.el.style.getPropertyValue('outline'); it.el.style.setProperty('outline','2px solid #ff3b30','important'); }catch(_){} });
      b.addEventListener('mouseleave',function(){ try{ if(it.el.__rrz) it.el.style.setProperty('outline',it.el.__rrz,'important'); else it.el.style.removeProperty('outline'); }catch(_){} });
      b.addEventListener('click',function(ev){
        ev.stopPropagation();
        try{ it.el.style.removeProperty('outline'); }catch(_){}
        var round=b.classList.contains('__ce_rrz');
        var on=round?radiusRound(it):radiusFlat(it);
        this.textContent=round?(on?'↩ 戻す':'⌒ 角丸 '+it.model+'px'):(on?'↩ 戻す':'✕ 消す');
        if(msg) msg.textContent=(round?'裏の四角を角丸に':'裏の四角の見た目を')+(on?(round?'しました':'消しました'):'戻しました')+'（💾保存で確定・⟲戻すで取り消し）';
      });
    });
    // ✂ 選択中の文字の配線（ボタンはメニューを閉じずにその場で効く）
    if(selApiQ && qm.querySelector('#__ce_q_selc')){
      qm.querySelector('#__ce_q_selc').addEventListener('input',function(){ selApiQ.paint(this.value); });
      [].slice.call(qm.querySelectorAll('.__ce_q_selsw')).forEach(function(b){
        b.addEventListener('click',function(ev){ ev.stopPropagation(); selApiQ.paint(this.getAttribute('data-c')); });
      });
      var _fsm=qm.querySelector('#__ce_q_fsm'), _fsp=qm.querySelector('#__ce_q_fsp');
      if(_fsm) _fsm.addEventListener('click',function(ev){ ev.stopPropagation(); selApiQ.fontSize(-2); });
      if(_fsp) _fsp.addEventListener('click',function(ev){ ev.stopPropagation(); selApiQ.fontSize(2); });
      var _spm=qm.querySelector('#__ce_q_spm'), _spp=qm.querySelector('#__ce_q_spp');
      if(_spm) _spm.addEventListener('click',function(ev){ ev.stopPropagation(); selApiQ.spacing(-0.5); });
      if(_spp) _spp.addEventListener('click',function(ev){ ev.stopPropagation(); selApiQ.spacing(0.5); });
      qm.querySelector('#__ce_q_selhlb').addEventListener('click',function(ev){ ev.stopPropagation();
        if(selApiQ.hasHl()){ selApiQ.removeHl(); this.textContent='マーカー'; }
        else{ selApiQ.highlight(qm.querySelector('#__ce_q_selhlc').value); this.textContent='マーカーを消す'; }
      });
      qm.querySelector('#__ce_q_selhlc').addEventListener('input',function(){ selApiQ.recolorHl(this.value); });                      // ドラッグ中は色だけ追従
      qm.querySelector('#__ce_q_selhlc').addEventListener('change',function(){ if(selApiQ.hasHl()) hlPushColorHistory(this.value); }); // 履歴は決定時に1回だけ
      qm.querySelector('#__ce_q_seludb').addEventListener('click',function(ev){ ev.stopPropagation();
        if(selApiQ.hasUd()){ selApiQ.removeUd(); this.textContent='下線'; }
        else{
          var _an=qm.querySelector('#__ce_q_seluda'), _anOn=(!_an||_an.checked);
          try{ localStorage.setItem('__ce_ud_anim', _anOn?'1':'0'); }catch(_){}  // 前回の選択を記憶
          selApiQ.underline(qm.querySelector('#__ce_q_seludc').value, _anOn); this.textContent='下線を消す';
        }
      });
      qm.querySelector('#__ce_q_seludc').addEventListener('input',function(){ if(selApiQ.hasUd()) selApiQ.underline(this.value); });
    }
    // メニューは「いじりたい要素にかぶらない場所」に出す（2026-07-30・ユーザー報告で作り直し）。
    // ★困っていたこと：右クリックした真上にメニューが出るので、重なっている要素を選び直す・
    //   赤枠のハイライトを見る、といった操作がメニューで塞がれて先に進めない。
    // ★旧実装は「420px以下の小さい要素」だけ横へ逃がしていた＝大きい箱（セクション・カード）では
    //   毎回かぶる。サイズで線を引くのをやめ、**実際にかぶる面積で決める**方式にした。
    //   候補（要素の右/左/下/上・画面の四隅・カーソルの右下）を並べ、かぶりが最小の所を選ぶ。
    //   同じなら**カーソルに近い方**を選ぶ（遠くへ飛ぶと目線が迷子になる）。
    (function(){
      var gapX=24, gapY=18, M=8;
      var w=qm.offsetWidth, h=qm.offsetHeight, W=window.innerWidth, H=window.innerHeight;
      var er=null; try{ er=(curEl&&curEl.getBoundingClientRect)?curEl.getBoundingClientRect():null; }catch(_){ }
      if(er&&!(er.width>0&&er.height>0)) er=null;
      var clampX=function(v){ return Math.max(M, Math.min(v, W-w-M)); };
      var clampY=function(v){ return Math.max(M, Math.min(v, H-h-M)); };
      var ovOf=function(x,y){
        if(!er) return 0;
        var iw=Math.min(x+w,er.right)-Math.max(x,er.left);
        var ih=Math.min(y+h,er.bottom)-Math.max(y,er.top);
        return (iw>0&&ih>0)?(iw*ih):0;
      };
      // ★手で動かした場所があり、そこがかぶらないなら最優先＝ユーザーの選んだ置き場所を尊重する
      if(lastMenuPos){
        var lx=clampX(lastMenuPos.left), ly=clampY(lastMenuPos.top);
        if(ovOf(lx,ly)===0){ qm.style.left=lx+'px'; qm.style.top=ly+'px'; return; }
      }
      var cand=[[e.clientX+gapX, e.clientY+gapY]];      // 今までの場所（カーソルの右下）
      if(er){
        cand.push([er.right+gapX, er.top]);             // 要素の右
        cand.push([er.left-gapX-w, er.top]);            // 要素の左
        cand.push([er.left, er.bottom+gapY]);           // 要素の下
        cand.push([er.left, er.top-gapY-h]);            // 要素の上
      }
      cand.push([M,M],[W-w-M,M],[M,H-h-M],[W-w-M,H-h-M]);   // 画面の四隅（大きい要素の逃げ場）
      var best=null;
      cand.forEach(function(c){
        var x=clampX(c[0]), y=clampY(c[1]);
        var ov=ovOf(x,y), d=Math.abs(x-e.clientX)+Math.abs(y-e.clientY);
        if(!best||ov<best.ov-1||(ov<=best.ov+1&&d<best.d)) best={x:x,y:y,ov:ov,d:d};
      });
      qm.style.left=best.x+'px'; qm.style.top=best.y+'px';
    })();
    curMenu=qm;
    qm.addEventListener('mouseover',function(ev){ var b2=ev.target.closest('.__ce_qi'); [].slice.call(qm.querySelectorAll('.__ce_qi')).forEach(function(x){ x.style.background=(x===b2)?'#eef4ff':'none'; }); });
    qm.addEventListener('click',function(ev){
      var t=ev.target.closest('.__ce_qi'); if(!t) return;
      if(t.id==='__ce_q_vspace'){ var vse=curEl, vsy=qy; closeMenu(); openSpacer(vse, vsy); return; }
      if(t.id==='__ce_q_up'){ selectParent(false); return; }
      if(t.id==='__ce_q_txt'){
        if(addMode){
          // 余白（文字を持たない要素）で押した＝右クリックしたその場所に新しい文字を置いて、すぐ編集開始
          // （空のまま Escape/× で閉じたら _scAddText が空要素を残さず消す）
          closeMenu(); _scAddText((window.scrollX||window.pageXOffset||0)+qx, (window.scrollY||window.pageYOffset||0)+qy);
        } else {
          var tgt=curEl; closeMenu(); openBreakEditor(tgt);
        }
        return;
      }
      if(t.id==='__ce_q_img'){ closeMenu(); openAddImagePicker((window.scrollX||window.pageXOffset||0)+qx, (window.scrollY||window.pageYOffset||0)+qy); return; }
      if(t.id==='__ce_q_imgswap'){
        var _ic=imgCandsAt(qx,qy,curEl);
        if(_ic.length>1){ pickWhichImg(_ic); return; }        // 重なっている→どれを差し替えるかサムネで選ぶ
        if(!_ic.length){ if(msg) msg.textContent='ここには差し替えられる画像がありません（画像の上で右クリックしてください）'; closeMenu(); return; }
        closeMenu(); openPicker(_ic[0]); return;
      }
      if(t.id==='__ce_q_bgsz'){
        var _bc=bgCandsAt(qx,qy,curEl);
        if(!_bc.length){ if(msg) msg.textContent='ここには背景画像がありません（青い形などの上で右クリックしてください）'; closeMenu(); return; }
        closeMenu(); openBgSizePanel(_bc); return;
      }
      // 🖼 写真を加工：⚙大メニュー(__ce_cmdeco)にしか無かったのでクイックにも出す（2026-07-25）
      if(t.id==='__ce_q_photo'){
        var _pe=curEl, _pim=(_pe&&_pe.tagName==='IMG')?_pe:((_pe&&_pe.querySelector)?_pe.querySelector('img'):null);
        var _psi=secIndexOf(_pe); closeMenu(); openPhotoDecoPicker(_pe,_pim,_psi); return;
      }
      if(t.id==='__ce_q_slide'){ var sle=curEl, sx=qx, sy=qy; closeMenu(); slideMake(sle,sx,sy); return; }
      if(t.id==='__ce_q_fx'){ _bigFull=true; _bigFxFocus=true; _forceEl=curEl; curEl.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:qx,clientY:qy})); return; }
      if(t.id==='__ce_q_fly'){ var ft=curEl; closeMenu(); startFlightDraw(ft); return; }
      if(t.id==='__ce_q_dly'){ var dle=curEl; closeMenu(); dlyOpen(dle,qx,qy); return; }
      if(t.id==='__ce_q_secout'){ toggleSecOutline(); closeMenu(); return; }
      if(t.id==='__ce_q_ref'){ var rse=curEl&&curEl.closest?curEl.closest('section,header,footer'):null; closeMenu(); refOpen(rse); return; }
      if(t.id==='__ce_q_dcq'){ var dse=curEl&&curEl.closest?curEl.closest('section,header,footer'):null; closeMenu(); dcqOpen(dse); return; }
      if(t.id==='__ce_q_brush'){ var bsi=curEl?secIndexOf(curEl):-1; closeMenu(); brushOpen(bsi); return; }
      if(t.id==='__ce_q_fav'){
        var fs=curEl.closest('section,header,footer');
        if(!fs){
          // <section>が無いページ（忠実クローン等）：右クリック位置から上って「1画面ぶんの塊」を部品にする。
          // 親がページ丸ごと級（画面の高さの3倍超）になる手前で止める＝全ページ保存の事故防止。
          fs=curEl;
          while(fs.parentElement && fs.parentElement!==document.body && fs.parentElement.tagName!=='MAIN'){
            var _ph=fs.parentElement.getBoundingClientRect().height;
            if(_ph>window.innerHeight*3) break;
            fs=fs.parentElement;
          }
          if(fs===document.body||fs.tagName==='HTML') fs=null;
        }
        closeMenu(); favSaveSection(fs); return; }
      if(t.id==='__ce_q_pickov'){
        closeMenu();
        // 右クリック地点に重なっている要素を全部並べる。
        // ★elementsFromPointは pointer-events:none の飾りを返さない（＝今まで一覧に出ず「掴めない絵」になっていた）
        //   ので、当たり判定で拾ったぶんを先頭に足す。
        var cands=document.elementsFromPoint(qx,qy).filter(function(c){ return c!==document.documentElement&&c!==document.body&&!c.closest('[id^="__ce"]'); });
        var _peList=_peNoneAt(qx,qy);
        cands=_peList.concat(cands.filter(function(c){ return _peList.indexOf(c)<0; }));
        if(!cands.length){ msg.textContent='ここに重なっている要素はありません'; return; }
        var pk=document.createElement('div'); pk.id='__ce_pkpos';
        pk.innerHTML='<div class="bx"><span class="cl" id="__ce_pkposx">×</span><h4>🎯 どの要素？（上ほど手前の層・行に載せると赤枠で確認）</h4><div class="poslist">'+cands.map(function(c,i){
          var tx=((c.textContent||'').replace(/\\s+/g,' ').trim().slice(0,18));
          var lb=c.tagName.toLowerCase()+((c.className&&typeof c.className==='string'&&c.className.trim())?'.'+c.className.trim().split(/\\s+/)[0]:'')+(tx?'「'+tx+'」':'');
          if(_peList.indexOf(c)>=0) lb='🫥 '+lb+'（クリックがすり抜ける飾り）';
          return '<div class="sit-pos" data-oi="'+i+'">'+esc(lb)+'</div>';
        }).join('')+'</div></div>';
        document.body.appendChild(pk);
        function _pco(){ cands.forEach(function(c){ c.style.outline=''; }); }
        pk.addEventListener('mouseover',function(ev){ _pco(); var it=ev.target.closest('.sit-pos'); if(it){ var c=cands[+it.getAttribute('data-oi')]; if(c) c.style.outline='3px solid #e05656'; } });
        pk.addEventListener('click',function(ev){
          if(ev.target.id==='__ce_pkpos'||ev.target.id==='__ce_pkposx'){ _pco(); pk.remove(); return; }
          var it=ev.target.closest('.sit-pos'); if(!it) return;
          var c=cands[+it.getAttribute('data-oi')]; _pco(); pk.remove(); if(!c) return;
          if(_peWake(c) && msg) msg.textContent='この飾りはクリックがすり抜ける設定でした→掴めるようにしました（💾保存で確定・⟲戻すで取り消し）';
          _forceEl=c; c.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:qx,clientY:qy}));  // その要素で右クリックメニューを開き直す
        });
        return;
      }
      if(t.id==='__ce_q_del'){
        if(!confirm('この要素を削除しますか？\\n（💾保存で確定。保存する前なら開き直せば戻ります）')) return;
        curEl.remove(); closeMenu(); markDirty();
        msg.textContent='🗑 削除しました（💾保存で確定・保存前なら開き直しで復活）';
        return;
      }
      if(t.id==='__ce_q_ovup'||t.id==='__ce_q_ovdn'){
        // 上の要素に重ねる＝margin-topをマイナスに（メニューは閉じない＝連打で調整できる）
        var mt=parseFloat(curEl.style.marginTop); if(isNaN(mt)) mt=parseFloat(getComputedStyle(curEl).marginTop)||0;
        mt+=(t.id==='__ce_q_ovup'?-60:60);
        curEl.style.setProperty('margin-top', Math.round(mt)+'px','important');
        markDirty(); msg.textContent='⬆ 食い込み: '+Math.round(mt)+'px（マイナスほど上に重なる・保存で確定）';
        return;
      }
      if(t.id==='__ce_q_gaya'){
        ensureDecoCss();
        var gh=(curEl.tagName==='IMG')?curEl.parentElement:curEl;   // 画像そのものなら親に重ねる
        var gex=gh.querySelector('.ce_gaya');
        closeMenu();
        if(!gex){
          if(getComputedStyle(gh).position==='static') gh.style.setProperty('position','relative','important');
          gex=document.createElement('div'); gex.className='ce_gaya';
          [['♪','6%','14%','0s','34px','#12a37f'],['💬','80%','8%','0.6s','38px','#f0a13b'],['！','28%','4%','1.2s','32px','#e05656'],['✨','58%','2%','1.8s','34px','#d9a80c'],['♪','90%','34%','2.4s','28px','#12a37f'],['💭','10%','48%','3.0s','32px','#5b8fd6']].forEach(function(g){
            var sp=document.createElement('span'); sp.textContent=g[0];
            sp.setAttribute('style','left:'+g[1]+';top:'+g[2]+';animation-delay:'+g[3]+';font-size:'+g[4]+';color:'+g[5]);
            gex.appendChild(sp);
          });
          gh.appendChild(gex); markDirty();
          msg.textContent='💬 がやがや演出を付けました（範囲パネルで位置調整・💾保存で確定）';
        }
        gayaPanel(gex);  // 付いている時は範囲調整パネル（外すのもここから）
        return;
      }
      if(t.id==='__ce_q_edge'){
        var ege=curEl.closest('section,header,footer');
        if(!ege){
          // ★背景を塗っている「見えているブロック」を最優先（2026-07-19）：透明な入れ物や外の大枠に
          //   かけると、見た目の境目とズレて「設定しても変わらない」が起きる（ヒーローの緑の帯＝
          //   実は透明な絵の後ろの.messageが塗っていた実例）。クリック地点に重なる要素の中から
          //   「背景色/背景画像あり・横幅ほぼいっぱい・高さ260px以上」のものを探して使う。
          try{
            var _egc=document.elementsFromPoint(qx,qy)||[];
            for(var _egi=0;_egi<_egc.length;_egi++){
              var _egn=_egc[_egi];
              if(_egn.closest && _egn.closest('[id^="__ce"]')) continue;
              if(_egn===document.body||_egn===document.documentElement) break;
              var _egs=getComputedStyle(_egn), _egr2=_egn.getBoundingClientRect();
              var _bgok=_egn.tagName==='IMG'||(_egs.backgroundColor!=='rgba(0, 0, 0, 0)'&&_egs.backgroundColor!=='transparent')||(_egs.backgroundImage&&_egs.backgroundImage!=='none');
              if(_bgok && _egr2.height>=260 && _egr2.width>=window.innerWidth*0.7){ ege=_egn; break; }
            }
          }catch(_){}
        }
        if(!ege){
          // <section>が無いページ（クローン等）＝右クリック位置から「1画面ぶんの塊」を対象にする（⭐と同じ流儀）
          ege=curEl;
          while(ege.parentElement && ege.parentElement!==document.body && ege.parentElement.tagName!=='MAIN'){
            // ★「見えている境目」でけずる（2026-07-19）：横幅ほぼいっぱい＆十分な高さの要素なら
            //   そこで止める。外の大枠まで登ると、絵より下の透明な余白をけずることになり
            //   「設定しても見た目が変わらない」が起きる（ヒーローの緑の帯で実際に発生）。
            var _egr=ege.getBoundingClientRect();
            if(_egr.height>=260 && _egr.width>=window.innerWidth*0.7) break;
            var _egh=ege.parentElement.getBoundingClientRect().height;
            if(_egh>window.innerHeight*3) break;
            ege=ege.parentElement;
          }
          if(ege===document.body||ege.tagName==='HTML') ege=null;
        }
        closeMenu(); edgeOpen(ege); return; }
      if(t.id==='__ce_q_ovshow'){
        // 親のoverflow:hidden（枠からはみ出た部分を刈り取る設定）をセクションまで遡って解除。
        // 画像の上下が切れて見える時の定番原因。重なり順(z-index)では直らないタイプ。
        var oe=curEl, ofn=0;
        while(oe&&oe!==document.body){
          try{
            var os=getComputedStyle(oe);
            if(/(hidden|clip|auto|scroll)/.test(''+os.overflow+os.overflowX+os.overflowY)){ oe.style.setProperty('overflow','visible','important'); ofn++; }
          }catch(_){}
          if(/^(SECTION|HEADER|FOOTER)$/.test(oe.tagName)) break;
          oe=oe.parentElement;
        }
        markDirty(); closeMenu();
        msg.textContent=ofn?('📤 '+ofn+'箇所の「はみ出し刈り取り」を解除しました（💾保存で確定・ダメなら保存せず開き直し）'):'はみ出しを隠す設定は見つかりませんでした（🔼重なりを手前に、を試してください）';
        return;
      }
      if(t.id==='__ce_q_zup'||t.id==='__ce_q_zdn'){
        var _zr=zStack(curEl, t.id==='__ce_q_zdn');
        markDirty();
        msg.textContent=(t.id==='__ce_q_zup'?'🔼 手前に出しました':'🔽 後ろに送りました')
          +'（重なり順 '+_zr.z+(_zr.lifted?'／親の箱も'+_zr.lifted+'個いっしょに調整':'')+'・💾保存で確定）';
        return;
      }
      if(t.id==='__ce_q_secadd'){ closeMenu(); var ab=document.getElementById('__ce_favadd'); if(ab) ab.click(); return; }
      if(t.id==='__ce_q_secswap'){ var sw=curEl.closest('section,header,footer'); closeMenu(); favSwapOpen(sw); return; }
      if(t.id==='__ce_q_secdel'){ var de=curEl.closest('section,header,footer'); closeMenu(); secDeleteOpen(de); return; }
      if(t.id==='__ce_q_frmfit'){
        // 見た目は動かさず、掴む枠（透明な枠・空きスペース）だけを見た目の位置へ持ってくる。
        // ★見た目が1pxでも動いたら失敗なので、当てる前後の rect を測って結果に出す（黙って崩さない）。
        // ★測るのは「中身が描かれている場所」。箱そのものは動かすのが目的なので、
        //   箱の rect で見張ると必ず「動いた」判定になってしまう。
        var _fmeas=function(x){
          var i=null; try{ i=inkBoxOf(x); }catch(_){ }
          if(i) return [i.l,i.t];
          var r=x.getBoundingClientRect(); return [r.left,r.top];
        };
        var _fl=(selEls.length?selEls.slice():[curEl]), _fdone=[], _fmove=0, _fink=0, _fflow=0;
        _fl.forEach(function(x){
          if(!x) return;
          var _b4=_fmeas(x);
          var steps=frameFitApply(x);
          if(!steps) return;
          var _af=_fmeas(x);
          steps.forEach(function(s){ if(s.kind==='ink') _fink++; if(s.kind==='flow') _fflow++; });
          _fdone.push(steps);
          if(Math.abs(_af[0]-_b4[0])>1.5||Math.abs(_af[1]-_b4[1])>1.5) _fmove++;
        });
        closeMenu();
        if(!_fdone.length){ msg.textContent='この要素の枠はズレていません（動かした跡がありません）'; return; }
        markDirty();
        msg.textContent='🧲 枠と中身の位置をそろえました（'+_fdone.length+'個'
          +(_fink?('・うち'+_fink+'個は空っぽの箱を中身の所へ移動'):'')+'）。'
          +(_fmove?('⚠'+_fmove+'個は中身も動いてしまいました＝⟲で戻せます。'):'中身の見た目は変わっていません。')
          +(_fflow?('⚠'+_fflow+'個は通常配置なので余白（margin）で動かしました＝隣がずれたら⟲で戻してください。'):'')
          +'これで白い所を掴んでも狙ったものが選べます・💾保存で確定';
        return;
      }
      if(t.id==='__ce_q_align'){
        // 右クリックした行と「同じ形の兄弟」（同じ親・同じタグ・同じクラス）を探し、
        // 各行に個別に焼き込まれたズレの元（移動・回転・インライン余白・寄せ）を掃除→共通CSSの位置に戻る＝そろう。
        var ar=(function(x){
          var cur=x;
          while(cur&&cur.parentElement&&cur.parentElement!==document.body){
            var pa=cur.parentElement, same=0;
            [].forEach.call(pa.children,function(sib){ if(sib!==cur&&sib.tagName===cur.tagName&&(sib.className||'')===(cur.className||'')) same++; });
            if(same>=1) return cur;
            cur=cur.parentElement;
          }
          return null;
        })(curEl);
        closeMenu();
        if(!ar){ msg.textContent='そろえる相手（同じ形の兄弟の行）が見つかりませんでした'; return; }
        var an=0;
        [].forEach.call(ar.parentElement.children,function(sib){
          if(sib.tagName!==ar.tagName||(sib.className||'')!==(ar.className||'')) return;
          ['translate','rotate','scale','transform','transform-origin','left','top','margin-left','margin-top','text-align','padding-left'].forEach(function(p){ sib.style.removeProperty(p); });
          ['data-cetx','data-cety','data-cero','data-cesx','data-cesy','data-cebt'].forEach(function(a){ sib.removeAttribute(a); });
          [].forEach.call(sib.children,function(kid){
            ['translate','transform','margin-left','text-align','padding-left'].forEach(function(p){ kid.style.removeProperty(p); });
            ['data-cetx','data-cety','data-cero','data-cesx','data-cesy','data-cebt'].forEach(function(a){ kid.removeAttribute(a); });
          });
          an++;
        });
        markDirty();
        msg.textContent='⁝ '+an+'行のズレ（個別の移動・余白・寄せ）を掃除してそろえました（💾保存で確定・ダメなら保存せず開き直し）';
        return;
      }
      if(t.id==='__ce_q_pskill'){ var _pke=curEl; closeMenu(); openDecoKill(_pke,qx,qy); return; }
      if(t.id==='__ce_q_secbg'){ var _sbe=curEl; closeMenu(); openSecBg(_sbe,qx,qy); return; }
      if(t.id==='__ce_q_txtbg'){ var _tbe=curEl; closeMenu(); openTextBg(_tbe,qx,qy); return; }
      if(t.id==='__ce_q_vline'){ var _vle=curEl; closeMenu(); openVLine(_vle,qx,qy); return; }
      if(t.id==='__ce_q_psgrab'){
        var _pge=curEl; closeMenu();
        var _lst=[]; try{ _lst=psList(_pge); }catch(_){ }
        if(!_lst.length){ if(msg) msg.textContent='ここには作り替えられる飾りがありませんでした'; return; }
        pushUndo(_pge);
        var _made=null;
        _lst.forEach(function(o){ var s=psMaterialize(_pge, o.which); if(s&&!_made) _made=s; });
        if(_made){
          curEl=_made; selEls=[_made];
          try{ _made.classList.add('__ce_sel'); showHandles(_made); }catch(_){}
          if(msg) msg.textContent='🔓 掴めるようにしました（そのままドラッグで移動／右クリックで色・大きさ・動きも付けられます）';
        } else if(msg) msg.textContent='作り替えに失敗しました';
        return;
      }
      if(t.id==='__ce_q_deco'){
        var _dqe=null,_bde=null;
        try{ _dqe=dqHitAt(qx,qy,curEl); }catch(_){ }
        if(!_dqe){ try{ _bde=dqBorderAt(qx,qy); }catch(_){ } }
        var _dx=qx,_dy=qy; closeMenu();
        if(_dqe) openDecoQuick(_dqe,_dx,_dy);
        else if(_bde) openLineQuick(_bde,_dx,_dy);
        else if(msg) msg.textContent='ここには飾り・線が見つかりませんでした';
        return;
      }
      if(t.id==='__ce_q_addline'){
        // ➕ 実要素の線：疑似要素と違い、掴んで移動・右クリック→要素削除・色変え（写真加工の背景色）が全部効く
        var _ln=document.createElement('div');
        _ln.setAttribute('style','height:2px;background:#d9d9d9;width:100%;margin:16px 0');
        _ln.setAttribute('data-celine','1');
        if(curEl&&curEl.parentElement&&curEl!==document.body) curEl.parentElement.insertBefore(_ln,curEl);
        else document.body.appendChild(_ln);
        markDirty(); pushUndo(_ln); closeMenu();
        if(msg) msg.textContent='➕ 右クリックした要素のすぐ上に線（実要素・2px）を置きました。掴んで移動・右クリックで削除/調整できます（💾保存で確定）';
        return;
      }
      if(t.id==='__ce_q_fxrm'){ eachSel(removeBake); closeMenu(); return; }
      if(t.id==='__ce_q_rst'){ eachSel(resetPos); markDirty(); closeMenu(); return; }
      if(t.id==='__ce_q_pin'){
        var _pt=pinFixTarget(curEl);
        pinFix(_pt,'top');
        closeMenu();
        msg.textContent='📌 〈'+_pt.tagName.toLowerCase()+'〉を画面の上に貼り付けました（スクロールしても残ります／解除は同じ所を右クリック→📌解除・💾保存で確定）';
        return;
      }
      if(t.id==='__ce_q_unfix'){
        // ツールで貼り付けた(data-cepin)ものは、器を動かす_unfixPlanでなく専用の解除で確実に外す
        if(curEl&&curEl.closest&&curEl.closest('[data-cepin]')){
          pinUnfix(curEl.closest('[data-cepin]')); closeMenu();
          msg.textContent='📌 貼り付けを解除しました（ページと一緒にスクロールします・💾保存で確定）';
          return;
        }
        var _uf=unfixEl(curEl);
        closeMenu();
        // ⚠️⟲は当てにしない：この操作は要素の入れ物ごと移すので、⟲（DOM丸ごと復元）で戻らない場合がある。
        //   戻したい時は「保存せずに開き直す」のが確実なので、そう案内する。
        msg.textContent=_uf
          ? '📌 貼り付きを解除しました（スクロールすると一緒に動きます／元に戻すなら保存せずに開き直してください・💾保存で確定）'
          : 'この要素は画面に貼り付いていません';
        return;
      }
      if(t.id==='__ce_q_full'){ _bigFull=true; _forceEl=curEl; curEl.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:qx,clientY:qy})); return; }
    });
  }
  // 右クリック禁止スクリプトが残っていても、こちらを優先させて確実にメニューを開く。
  // ★「たまに右クリックが出なくなる」対策＝Escで全部の状態を強制リセットする最後の砦。
  //   モード（🕊線飛ばし・🔍インスペクト）が抜け残っていたり、パネルが閉じ損ねていると
  //   右クリックが効かなくなる。各モードは自前のEsc処理を持つが、それが取りこぼした時の保険。
  document.addEventListener('keydown',function(e){
    if(e.key!=='Escape') return;
    var recovered=(window.__ceInspOn||window.__ceFlyMode);
    // まずは各モードの正規の終了処理（後片付けごとやってくれる）
    try{ if(window.__ceInspOn && window.__ceInspExit) window.__ceInspExit(); }catch(_){}
    try{ if(window.__ceFlyMode && typeof flyEnd==='function'){ flyStopPrev(); flyEnd(); } }catch(_){}
    // ★正規終了が例外で途中まで＝フラグが残ると右クリックが死んだままになるので、最後に必ず強制で落とす
    window.__ceInspOn=false; window.__ceFlyMode=false;
    try{ document.documentElement.style.cursor=''; }catch(_){}
    // 閉じ損ねた各種パネル（暗幕クリックで閉じないもの含む）を掃除
    ['__ce_pk','__ce_dlyp','__ce_shp','__ce_sbgp','__ce_pskill','__ce_scset','__ce_scmenu','__ce_tbgp','__ce_vlp','__ce_dqp','__ce_secp','__ce_pkpos','__ce_ruler','__ce_bgp','__ce_grab','__ce_grab2'].forEach(function(id){ var p=document.getElementById(id); if(p){ if(p.__close) p.__close(); else { if(p.__off) p.__off(); p.remove(); } recovered=true; } });
    try{ if(typeof closeMenu==='function') closeMenu(); }catch(_){}
    if(recovered && msg) msg.textContent='元の状態に戻しました（右クリックが使えます）';
  },true);
  // 🎯 カーソルの真下にある「文字を直接持つ一番小さい要素」を探す（2026-07-30）。
  //   見出しの中に「NEWS」と「お知らせ」が兄弟で並ぶ形だと、塊（h2）が選ばれて片方だけ掴めない。
  //   ★子孫の文字ではなく「自分が直接持っているテキスト」で判定するのがミソ。
  //     子孫込み(textContent)で見ると入れ物まで該当してしまい、また塊が選ばれる。
  // ★1文字ずつに割られた文字（アニメ用の分割）は、1文字だけ掴めても意味がない。
  //   ツールが割った .fxa_ch だけでなく、クローン元サイトが自前で割ったもの
  //   （<span>N</span><span>E</span>… animation-delay付き）も同じなので、クラス名では判定しない。
  //   「自分の文字が1〜2文字で、兄弟も短いものが並んでいる」＝分割の断片、と動的に見分ける。
  function _charFrag(el){
    if(!el||!el.parentElement) return false;
    if(el.classList&&el.classList.contains('fxa_ch')) return true;
    var own=(el.textContent||'').trim();
    // ★文字を持たない要素（画像・図形など）は「文字の断片」ではない。
    //   ここを空文字で通すと、画像が親に吸い上げられて「画像の上で右クリックして」と弾かれる
    //   ＝スライドショーや写真加工が作れなくなる（2026-07-30・実報告）。
    if(!own) return false;
    if(own.length>2) return false;
    var sibs=el.parentElement.children, n=0, short=0;
    for(var i=0;i<sibs.length;i++){
      var s=(sibs[i].textContent||'').trim();
      if(!s) continue;
      n++; if(s.length<=2) short++;
    }
    return n>=2 && short>=Math.max(2, Math.floor(n*0.7));
  }
  function _deepTextAt(root,x,y){
    if(!root||!root.querySelectorAll) return null;
    var best=null, bestArea=Infinity;
    var all=[].slice.call(root.querySelectorAll('*'));
    for(var i=0;i<all.length && i<3000;i++){
      var el=all[i];
      if(el.closest&&el.closest('[id^="__ce"]')) continue;
      var own=false;
      for(var n=el.firstChild;n;n=n.nextSibling){
        if(n.nodeType===3 && (n.nodeValue||'').trim()){ own=true; break; }
      }
      if(!own) continue;
      var r=el.getBoundingClientRect();
      if(r.width<1||r.height<1) continue;
      if(x<r.left-2||x>r.right+2||y<r.top-2||y>r.bottom+2) continue;
      var a=r.width*r.height;
      if(a<bestArea){ best=el; bestArea=a; }
    }
    // ★1文字ずつの断片に当たったら「まとめ役」の親まで戻す＝文字列ごと選べる（1文字だけ掴めても使えない）
    var guard=0;
    while(best && _charFrag(best) && best.parentElement && best!==root && guard++<8){
      best=best.parentElement;
    }
    return best;
  }
  // 🔓 「その文字だけを指す入れ物」がDOMに無い裸のテキストノードを、その場で span に包んで掴めるようにする。
  //   実例：<h2><span>NEWS</span>お知らせ</h2> の「お知らせ」は要素ではないので、どんな選び方をしても
  //   h2（＝NEWS込み）しか掴めなかった（2026-07-30・実報告。3回直しを外した真犯人）。
  //   ★包むだけなので見た目は変わらない（display も触らない）。§7㉗の疑似要素「実体化」と同じ考え方。
  //   ★テキストノードの位置は Range.getClientRects で測る（§7㉖で既に使っている手法）。
  function _wrapTextNodeAt(el,x,y){
    if(!el||!el.firstChild||el.closest&&el.closest('[id^="__ce"]')) return null;
    // ★1文字ずつに割られた断片の中では包まない。包むと「N」だけ選ばれて文字列ごと掴めなくなる
    //   （スタッガー系アニメを付けた文字で実際に起きた・2026-07-30）。
    if(_charFrag(el)) return null;
    for(var n=el.firstChild;n;n=n.nextSibling){
      if(n.nodeType!==3) continue;
      if(!(n.nodeValue||'').trim()) continue;
      var rg=null; try{ rg=document.createRange(); rg.selectNodeContents(n); }catch(_){ continue; }
      var rs=rg.getClientRects(), hit=false;
      for(var i=0;i<rs.length;i++){
        var r=rs[i];
        if(x>=r.left-2&&x<=r.right+2&&y>=r.top-2&&y<=r.bottom+2){ hit=true; break; }
      }
      if(!hit) continue;
      var sp=document.createElement('span');
      sp.className='ce_tnode'; sp.setAttribute('data-cetnode','1');
      try{ n.parentNode.insertBefore(sp,n); sp.appendChild(n); }catch(_){ return null; }
      return sp;
    }
    return null;
  }
  // 🎯 Figma式ダブルクリック：1回目＝右クリックと同じ塊を選択、以降ダブルクリックのたびに
  //   クリック位置の子へ1段ずつ潜る（文字そのものまで届く）。メニューは開かず選択だけ＝
  //   潜り終わったら右クリックでメニュー（下のcontextmenuが __ceDblSel を優先して拾う）。
  document.addEventListener('dblclick',function(e){
    if(window.__ceFlyMode||window.__ceInspOn) return;
    var t=e.target;
    if(!t||t.nodeType!==1) return;
    if(t.closest&&t.closest('[id^="__ce"]')) return;
    // ★選択中に出る伸縮ハンドル(.__ce_hdl)が文字の上に重なっていると、ダブルクリックがハンドルに
    //   吸われて何も起きない（「画面下に何も出ない」＝ここで止まっていた・2026-07-30）。
    //   ハンドルは操作用の小さな四角なので、ここでは貫通して下の中身を対象にする。
    if(t.closest&&t.closest('.__ce_hdl')){
      var _uh=document.elementsFromPoint(e.clientX,e.clientY), _t2=null;
      for(var _k=0;_k<_uh.length;_k++){
        var _e2=_uh[_k];
        if(!_e2||!_e2.closest) continue;
        if(_e2.closest('.__ce_hdl')||_e2.closest('[id^="__ce"]')) continue;
        _t2=_e2; break;
      }
      if(!_t2) return;
      t=_t2;
    }
    if(t.isContentEditable) return;                     // 文字編集中の単語選択はそのまま
    // 🎨 Shift+ダブルクリック＝その場で「色を置き換える」を開く（ヘッダーの色替えの最短ルート）。
    //   素のダブルクリックはFigma式の潜り込み選択に使われているので、修飾キーで住み分ける。
    if(e.shiftKey){
      e.preventDefault();
      var _ct=null; try{ _ct=pickTarget(_realTarget(e)); }catch(_){}
      openColorReplace(_ct||t);
      if(msg) msg.textContent='🎨 色の置き換えを開きました（範囲は「この中／セクション／ページ全体」で切り替え）';
      return;
    }
    var next=null;
    if(curEl&&curEl===t){ if(msg) msg.textContent='🎯 ここが一番奥です（右クリックでメニュー・Escで解除）'; return; }
    if(curEl&&curEl.contains(t)){
      // 1段だけ潜る：今の選択の子のうち、クリックした場所を含むもの
      var c=t; while(c&&c.parentElement!==curEl) c=c.parentElement;
      next=c||t;
    }else{
      next=pickTarget(_realTarget(e));                  // 最初は右クリックと同じ掴み方
      // ★枠だけの図形（背景が透明な ce_shape）が上に乗っていると、その中の文字を永久に掴めない
      //   （NEWSの飾り枠の中の「お知らせ」で発覚・2026-07-30）。右クリックでは図形自身を選べる必要が
      //   あるので貫通させないが、ダブルクリックは「中身へ潜る」操作なので、ここでは貫通させる。
      if(next && next.classList && next.classList.contains('ce_shape')){
        var _bg=''; try{ _bg=getComputedStyle(next).backgroundColor||''; }catch(_){}
        if(_bg==='transparent' || /,\\s*0\\)\\s*$/.test(_bg)){
          var _u=document.elementsFromPoint(e.clientX,e.clientY);
          for(var _i=0;_i<_u.length;_i++){
            var _c=_u[_i];
            if(!_c || !_c.closest) continue;
            if(_c.closest('[id^="__ce"]')) continue;
            if(_c.classList && _c.classList.contains('ce_shape')) continue;
            if(!(_c.textContent||'').trim()) continue;   // 文字を持つものまで潜る
            var _p=pickTarget(_c); if(_p){ next=_p; break; }
          }
        }
      }
      if(next) next=_descendOverlay(next,e.clientX,e.clientY);
      // ★塊ではなく「カーソル下の文字そのもの」まで一気に降りる（2026-07-30・要望）。
      //   見出しの中に文字が2つ並ぶ形（NEWS＋お知らせ）で、片方だけ掴めない報告があったため。
      //   塊のほうを選びたい時は右クリック→「⬆ 外側を選ぶ」で戻れる。
      if(next){
        var _dt=_deepTextAt(next, e.clientX, e.clientY);
        if(_dt && _dt!==next && next.contains(_dt)) next=_dt;
        // ★それでも塊のままなら「入れ物を持たない裸の文字」なので、その場で包んで単体で掴めるようにする
        var _wn=_wrapTextNodeAt(next, e.clientX, e.clientY);
        if(_wn) next=_wn;
      }
    }
    // 1段ずつ潜る側でも同じ手当てをする（既に何か選択している状態から「お知らせ」を掴む流れ）
    if(next && next!==document.body){
      var _wn2=_wrapTextNodeAt(next, e.clientX, e.clientY);
      if(_wn2) next=_wn2;
    }
    // ★どの経路を通っても、最後に「1文字ずつの断片」ならまとめ役の親まで戻す＝文字列ごと選べる。
    //   1文字だけ選べても色も動きも付けられず使えないため（スタッガー系アニメで実報告・2026-07-30）。
    var _cg=0;
    while(next && _charFrag(next) && next.parentElement && next.parentElement!==document.body && _cg++<8){
      next=next.parentElement;
    }
    if(!next||next===document.body||next.tagName==='HTML') return;
    if(next.closest&&next.closest('[id^="__ce"]')) return;
    closeMenu();
    curEl=next; next.classList.add('__ce_sel'); selEls=[next];
    window.__ceDblSel=next;
    // ★選んだらそのまま動かせるようにする（2026-07-30・要望）。
    //   右クリックで自動ドラッグONにするのは §7⑨ で文字選択ができなくなり撤回した経緯があるが、
    //   ダブルクリックは「これを触る」と明示した操作なので、ここでONにしても文字選択の邪魔にならない。
    // ★display:inline のままだと translate が効かず1pxも動かない（§7㉗と同じ罠）。
    //   文字だけの span を掴めるようにしたので、ここで必ず inline-block に直しておく。
    try{
      if(getComputedStyle(next).display==='inline') next.style.setProperty('display','inline-block','important');
    }catch(_){}
    var _dragOK=false; try{ setDragOn(next); _dragOK=true; }catch(_){}
    if(msg){
      var d=next.tagName.toLowerCase()+(next.className&&typeof next.className==='string'?'.'+next.className.split(' ')[0]:'');
      msg.textContent='🎯 '+d+' を選択'+(_dragOK?'／そのままドラッグで移動できます':'')
        +'（もう一度ダブルクリック＝1段奥へ／右クリック＝メニュー／Esc＝解除）';
    }
  },true);
  document.addEventListener('contextmenu',function(e){
    if(window.__ceFlyMode){ e.preventDefault(); if(msg) msg.textContent='🕊 線を飛ばすモード中です。右クリックメニューに戻すには Esc を押してください'; return; }  // 🕊ルート描画中は右クリック＝アンカー削除
    var _wasForced=_forceEl;
    var el=_forceEl||pickTarget(_realTarget(e)); _forceEl=null;
    // 🎯ダブルクリックで潜って選んだ要素の上で右クリック＝その選択を保ってメニューを出す
    if(!_wasForced&&window.__ceDblSel&&window.__ceDblSel.isConnected&&(window.__ceDblSel===e.target||window.__ceDblSel.contains(e.target))){ el=window.__ceDblSel; }
    window.__ceDblSel=null;
    if(!el||el.closest('[id^="__ce"]')) return;
    // ★ツールで置いた部品（🔶図形・🔓実体化した飾り・背景の飾り）は「透明な膜」ではないので
    //   貫通させない。ここを通すと、丸を右クリックしたのに下の文章ブロックが選ばれ、
    //   動きを付けると文章のほうに付いてしまう（実報告・2026-07-29）。
    var _isToolPart=!!(el&&el.classList&&(el.classList.contains('ce_shape')||el.classList.contains('ce_psel')
      ||el.classList.contains('ce_bgdeco')||el.classList.contains('ce_ringdeco')||el.classList.contains('ce_outlinedeco')
      ||(el.getAttribute&&el.getAttribute('data-celine'))));
    if(!_wasForced && !_isToolPart) el=_descendOverlay(el, e.clientX, e.clientY);  // 透明な膜は貫通して下の実体を掴む（⬆外側選択のときは貫通させない）
    // 別の枝の入れ物が上にかぶっている時は、実際に見えている文字のほうを掴む（重なりを直さなくても選べる）
    if(!_wasForced && !_isToolPart){
      var _tx=_textAt(e.clientX, e.clientY);
      if(_tx && _tx!==el && !el.contains(_tx) && !_tx.contains(el)) el=_tx;
    }
    // ★1文字ずつに割られた文字（スタッガー等）を右クリックした時は、まとめ役の親まで戻す（2026-07-30・要望）。
    //   1文字だけに色や動きが付くと使えないため、選択・編集・右クリックで挙動をそろえる。
    //   ⬆外側選択(_wasForced)の時は触らない＝意図して親を選んでいる操作を邪魔しない。
    if(!_wasForced){
      var _rg=0;
      while(el && _charFrag(el) && el.parentElement && el.parentElement!==document.body && _rg++<8){
        el=el.parentElement;
      }
    }
    // ※「クリックがすり抜ける絵」は勝手に掴み替えない（文字の上を右クリックした時に横取りする誤爆が
    //   実測で出たため）。代わりにクイックメニューの先頭に「この絵を選ぶ」を出して選ばせる（_peRowQ）。
    // 🖼画像は「枠（親）ごと」がほぼ常に正解：親が画像をぴったり包むラッパー（figure/div等）なら
    //   自動で親を選ぶ＝1回のドラッグで画像も裏の枠も一緒に動く。セクション等の大きな器は選ばない。
    if(!_wasForced && el.tagName==='IMG'){
      var _pw=el.parentElement;
      // ★スライドショーの入れ物には吸い上げない（2026-07-30・ユーザー要望）。
      //   3枚が同じ場所に重なっているので、入れ物を選ぶと「今見えている1枚」を触れなくなる。
      //   ＝赤が出ている時は赤、緑の時は緑を掴めるようにする。大きさ変更だけは箱側で受ける(slBox)。
      if(_pw && _pw.getAttribute && _pw.getAttribute('data-slshow')!=null) _pw=null;
      // ★セクション級の器は「枠」ではないので絶対に選ばない（大きい画像だと寸法比だけでは弾けず、
      //   ヘッダー丸ごとが選ばれて動かせてしまった）。
      if(_pw && !/^(SECTION|HEADER|FOOTER|MAIN|BODY|HTML)$/.test(_pw.tagName) && !_undraggable(_pw)){
        var _ri=el.getBoundingClientRect(), _rp=_pw.getBoundingClientRect();
        if(_rp.width<=_ri.width*1.5+40 && _rp.height<=_ri.height*1.5+40) el=_pw;
      }
    }
    e.preventDefault(); e.stopPropagation(); if(e.stopImmediatePropagation) e.stopImmediatePropagation();
    if(e.ctrlKey && selEls.length){
      // Ctrl+右クリック＝今の選択を保ったまま追加/解除（トグル）。メニューだけ作り直す
      if(curMenu){ curMenu.remove(); curMenu=null; }
      var ix=selEls.indexOf(el);
      if(ix>=0){
        el.classList.remove('__ce_sel'); selEls.splice(ix,1);
        if(!selEls.length){ closeMenu(); return; }
        curEl=selEls[selEls.length-1];
      } else {
        selEls.push(el); el.classList.add('__ce_sel'); curEl=el;
      }
    } else if(selEls.length>1 && (selEls.indexOf(el)>=0 || selEls.some(function(s){ return s.contains(e.target); }))){
      // 複数選択中に「選択している要素の上」で右クリック＝選択をそのまま保ってメニューを出す
      // （🔲範囲選択→右クリックでまとめて操作、の本命経路。⚙大メニュー開き直し(_wasForced)も同じ扱い）
      if(curMenu){ curMenu.remove(); curMenu=null; }
      if(selEls.indexOf(el)<0){ for(var _i2=0;_i2<selEls.length;_i2++){ if(selEls[_i2].contains(e.target)){ el=selEls[_i2]; break; } } }
      curEl=el;
    } else {
      closeMenu();
      curEl=el; el.classList.add('__ce_sel'); selEls=[el];
    }
    showHandles(curEl);  // Excel風：選択したら右端・下端・右下角に伸縮ハンドル■を出す
    // まずはブラウザ風のクイックメニューを出す（⚙から従来の大メニューへ）
    if(!_bigFull){ openQuickMenu(e); return; }
    _bigFull=false;
    var sIdx=secIndexOf(curEl), d=descEl(curEl);
    el=curEl;  // 以降の処理は主役（最後に選んだ要素）を基準にする
    // 画像なら「AIなしの即差し替え」を最優先で出す（画像のAI指示は不安定なため）
    var imgEl = (el.tagName==='IMG') ? el : (el.querySelector ? el.querySelector('img') : null);
    var cands=imgCandsAt(e.clientX, e.clientY, el);
    var swapH = (cands.length
      ? '<button class="go2" id="__ce_cmswap" style="background:#1a7f37;margin-bottom:6px">🖼 この画像を差し替え（AIなし・一瞬）</button>'
        +'<div class="cap" style="margin:0 0 8px">画像はこれが確実です（差し替えは一瞬）</div>'
      : '')
      // 白フチ／はみ出しカード／背景の飾り／背景に設定／水彩(AI) をまとめて1つの入口に統合（ボタン数を増やさない）
      + '<button class="go2" id="__ce_cmdeco" style="background:#0b6bcb;margin-bottom:8px">🖼 写真を加工（フチ・カード・背景など）'+(scKeyOf('__ce_cmdeco')?' ['+esc(scKeyOf('__ce_cmdeco'))+']':'')+'</button>';
    // 右クリックのAIセクションは「無料の焼き込みで出来ないもの」だけに絞る（背景装飾=bg / AI専用=ai）。
    // 単純な出現/ループ系は上の「動きを選ぶ→付ける」に一本化したのでここには出さない。
    var aiList=PRESETS.filter(function(p){return p.bg||p.ai;});
    var agh=aiList.map(function(p,i){return '<button class="ag2" data-i="'+i+'"><b>'+esc(p.b)+'</b><span>'+esc(p.d)+'</span></button>';}).join('');
    // ※文字選択（✂）・文字編集・画像追加・飛ばす・動きを消す・外側を選ぶは
    //   クイックメニュー（openQuickMenu）だけに置く＝同じ機能を2箇所に出さない（2026-07-11整理）
    var m=document.createElement('div'); m.id='__ce_cm';
    var mTitle=(selEls.length>1)?('🧩 '+selEls.length+'個を選択中（サイズ・動き・削除は全部に効く）'):d;
    m.innerHTML='<div class="h"><span class="t">'+esc(mTitle)+'</span><span class="c" id="__ce_cmx">✕</span></div>'
      +'<div class="bd2">'+swapH
      +'<div class="cap">🧩 Ctrl+右クリック＝まとめて選択に追加（もう一度で外す）</div>'
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
      +'<button class="go2" id="__ce_cmhl" style="background:#eab308;color:#1d1d1f;margin-bottom:4px">🖍 この文字にマーカー（スクロールで線が伸びる）</button>'
      +'<div class="cap" style="margin-bottom:8px">🖍 色 <input type="color" id="__ce_cmhlc" value="'+hlDefaultColor()+'" style="width:34px;height:22px;padding:0;border:none;border-radius:4px;vertical-align:middle;cursor:pointer"><span id="__ce_cmhlsw" style="display:inline-flex;gap:3px;flex-wrap:wrap;vertical-align:middle;margin-right:4px">'+hlSwatchesHtml()+'</span> 太さ<button id="__ce_cmhlthm" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:1px 7px;cursor:pointer">－</button><button id="__ce_cmhlthp" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:1px 7px;cursor:pointer">＋</button> 位置<button id="__ce_cmhlpu" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:1px 7px;cursor:pointer" title="上へ">▲</button><button id="__ce_cmhlpd" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:1px 7px;cursor:pointer" title="下へ">▼</button> 速さ<button id="__ce_cmhldm" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:1px 7px;cursor:pointer" title="遅く">🐢</button><button id="__ce_cmhldp" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:1px 7px;cursor:pointer" title="速く">🐇</button> 待機<button id="__ce_cmhlwm" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:1px 7px;cursor:pointer" title="待ちを短く（-0.2秒）">－</button><button id="__ce_cmhlwp" style="background:#f2f2f4;border:1px solid #ddd;border-radius:5px;padding:1px 7px;cursor:pointer" title="待ちを長く（+0.2秒）＝文字が動いたあとに引きたい時">＋</button><br>／ Alt+ドラッグで文字を選んで右クリック＝「一部だけ」に引ける ／<a href="#" id="__ce_cmhlrm" style="color:#0b6bcb">🚫 マーカーを消す</a></div>'
      +'<button class="go2" id="__ce_cmsecbg" style="background:#0e7490;margin-bottom:4px">🎨 セクションの背景色を変える（AIなし・即反映）</button>'
      +'<div id="__ce_cmsecbgp" style="display:none;background:#f7fafc;border:1px solid #dbe4ee;border-radius:9px;padding:8px 10px;margin:0 0 8px;font-size:12px;line-height:1.6"></div>'
      +'<button class="go2" id="__ce_cmstyle" style="background:#c026a6;margin-bottom:8px">✨ このセクションをおしゃれに（AIが一括）</button>'
      +'<div class="cap">✨ 動きを選ぶ（クリックで試す→調整→付ける・AIなし・無料）</div>'
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
    // ✨クイックメニューの「動きを付ける」から来た時＝動きグリッドまで自動スクロール＋一瞬光らせて場所を教える
    if(_bigFxFocus){
      _bigFxFocus=false;
      var _fbd=m.querySelector('.bd2'), _fg=m.querySelector('#__fx_grid');
      if(_fbd && _fg){
        var _fcap=_fg.previousElementSibling||_fg;  // 見出し「✨動きを選ぶ」ごと見せる
        _fbd.scrollTop=Math.max(0, _fcap.getBoundingClientRect().top - _fbd.getBoundingClientRect().top + _fbd.scrollTop - 6);
        _fg.style.outline='3px solid #c026a6'; _fg.style.outlineOffset='2px'; _fg.style.borderRadius='8px';
        setTimeout(function(){ try{ _fg.style.outline=''; _fg.style.outlineOffset=''; }catch(_){} },1600);
      }
    }
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
      if(nb){ if(nb.getAttribute('data-rst')) eachSel(resetPos); else eachSel(function(x){ nudge(x, +nb.getAttribute('data-nx'), +nb.getAttribute('data-ny')); }); return; }
      var mhb=ev.target.closest('button[data-mh]');  // 高さ(min-height)の増減＝スケールと違い歪まない
      if(mhb){ eachSel(function(x){ adjustMinH(x, +mhb.getAttribute('data-mh')); }); return; }
      var mwb=ev.target.closest('button[data-mw]');  // 幅(width)の増減＝歪まない横伸ばし
      if(mwb){ eachSel(function(x){ adjustWidth(x, +mwb.getAttribute('data-mw')); }); return; }
      var sb=ev.target.closest('.__ce_size button');
      if(sb){
        eachSel(function(x){
          if(sb.hasAttribute('data-ro')) rotateBy(x, +sb.getAttribute('data-ro'));
          else if(x.tagName==='IMG') sizeImg(x, +sb.getAttribute('data-sx'), +sb.getAttribute('data-sy'));  // 画像は歪まない方式で
          else scaleBy(x, +sb.getAttribute('data-sx'), +sb.getAttribute('data-sy'));
        });
        return;
      }
      var ak=ev.target.closest('#__fx_grid button');
      if(ak){ selectFx(ak.getAttribute('data-ak'), ak); return; }
      var apl=ev.target.closest('#__fx_apply');
      if(apl){
        if(!curAnim){ msg.textContent='まず上から動きを選んでください'; return; }
        // ★入れ子は外側だけに付ける（2026-07-31・報告「文字と青い箱を一緒に選ぶと動きが固まる」）。
        //   親と子の両方に .fxa_pre を付けると、子は親の opacity:0 の中でさらに自分も透明＝
        //   親が現れるまで動き出せず「固まった」ように見える。transformも二重にかかって位置がずれる。
        //   親が動けば中身も一緒に動くので、外側だけに付けるのが正しい（🧩グループと同じ考え方）。
        var _fxT=(selEls.length?selEls.slice():(curEl?[curEl]:[]));
        var _fxN=_fxT.length;
        _fxT=_fxT.filter(function(n){ return !_fxT.some(function(p){ return p!==n && p.contains && p.contains(n); }); });
        _fxT.forEach(function(x){ applyBake(x, curAnim); });
        if(msg&&_fxN>1) msg.textContent='✅ '+_fxT.length+'個に付けました'
          +(_fxN>_fxT.length?('（中に入っている'+(_fxN-_fxT.length)+'個は外側と一緒に動くので除きました）'):'')+'／💾保存で残る';
        return;
      }
      var gb=ev.target.closest('#__ce_grp button');
      if(gb){
        var gv=gb.getAttribute('data-grp');
        if(gv==='0'){ eachSel(function(x){ x.removeAttribute('data-cegrp'); }); if(msg) msg.textContent='グループを解除しました（保存で確定）'; }
        else{ eachSel(function(x){ x.setAttribute('data-cegrp', gv); }); if(msg) msg.textContent='グループ'+gv+'に入れました（①→②→③の順で表示・保存で確定）'; }
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
    // 🖍 この文字にマーカー：選択がいらない版。右クリックした要素の中身を .fxa_hl で丸ごと囲む
    //   →スクロールで線がスーッと伸びる（保存版でも再生）。※選択できない箇所でも確実に引ける。
    function hlWhole(el,color){
      if(!el) return;
      hlPushColorHistory(color);
      // 既に丸ごとマーカー済みなら色だけ更新
      if(el.children.length===1 && el.firstChild && el.firstChild.classList && el.firstChild.classList.contains('fxa_hl')){
        el.firstChild.style.setProperty('--hlc',color); fxHlReplay(el.firstChild); markDirty();
        if(msg) msg.textContent='マーカーの色を変えました（保存で確定）'; return;
      }
      var span=document.createElement('span'); span.className='fxa_hl'; span.style.setProperty('--hlc',color);
      while(el.firstChild){ span.appendChild(el.firstChild); }
      el.appendChild(span);
      if(typeof ensureFxAssets==='function') ensureFxAssets();  // アニメCSS/監視JSを注入
      if(window.__fxaSweepHl) window.__fxaSweepHl(span); else{ span.style.setProperty('--hlw',100); span.classList.add('fxa_in'); }  // 今すぐ線を引く（プレビュー）
      markDirty();
      if(msg) msg.textContent='マーカーを引きました（スクロールで線が伸びます・保存で確定／⟲戻すで取り消し）';
    }
    var hlB=m.querySelector('#__ce_cmhl');
    if(hlB) hlB.addEventListener('click',function(){ hlWhole(curEl, m.querySelector('#__ce_cmhlc').value); });
    var hlC=m.querySelector('#__ce_cmhlc');
    // ドラッグ中(input)は色だけ追従。履歴は決定時(change)に1回だけ＝ドラッグ中に毎回貯めると
    // ほぼ同じ色で履歴10枠が埋まり、色見本が「同じ色の四角の列」になってしまう（実際になった）
    if(hlC) hlC.addEventListener('input',function(){ var s=curEl&&curEl.querySelector&&curEl.querySelector('.fxa_hl'); if(s){ s.style.setProperty('--hlc',this.value); markDirty(); } });
    if(hlC) hlC.addEventListener('change',function(){
      hlPushColorHistory(this.value);
      var w=m.querySelector('#__ce_cmhlsw');
      if(w){ w.innerHTML=hlSwatchesHtml(); hlBindSwatches(w, hlC, function(c){ var s=curEl&&curEl.querySelector&&curEl.querySelector('.fxa_hl'); if(s){ s.style.setProperty('--hlc',c); markDirty(); } }); }
    });
    hlBindSwatches(m.querySelector('#__ce_cmhlsw'), m.querySelector('#__ce_cmhlc'), function(c){ var s=curEl&&curEl.querySelector&&curEl.querySelector('.fxa_hl'); if(s){ s.style.setProperty('--hlc',c); markDirty(); } });
    function _cmHlSpan(){ return curEl&&curEl.querySelector&&curEl.querySelector('.fxa_hl'); }
    var hlThm=m.querySelector('#__ce_cmhlthm'); if(hlThm) hlThm.addEventListener('click',function(){ fxHlThick(_cmHlSpan(),-6); });
    var hlThp=m.querySelector('#__ce_cmhlthp'); if(hlThp) hlThp.addEventListener('click',function(){ fxHlThick(_cmHlSpan(),6); });
    var hlPu=m.querySelector('#__ce_cmhlpu'); if(hlPu) hlPu.addEventListener('click',function(){ fxHlPos(_cmHlSpan(),-4); });
    var hlPd=m.querySelector('#__ce_cmhlpd'); if(hlPd) hlPd.addEventListener('click',function(){ fxHlPos(_cmHlSpan(),4); });
    var hlDm=m.querySelector('#__ce_cmhldm'); if(hlDm) hlDm.addEventListener('click',function(){ fxHlSpeed(_cmHlSpan(),0.2); });
    var hlDp=m.querySelector('#__ce_cmhldp'); if(hlDp) hlDp.addEventListener('click',function(){ fxHlSpeed(_cmHlSpan(),-0.2); });
    var hlWm=m.querySelector('#__ce_cmhlwm'); if(hlWm) hlWm.addEventListener('click',function(){ fxHlDelay(_cmHlSpan(),-200); });
    var hlWp=m.querySelector('#__ce_cmhlwp'); if(hlWp) hlWp.addEventListener('click',function(){ fxHlDelay(_cmHlSpan(),200); });
    var hlRm=m.querySelector('#__ce_cmhlrm');
    if(hlRm) hlRm.addEventListener('click',function(ev){
      ev.preventDefault();
      var s=curEl&&curEl.querySelector&&curEl.querySelector('.fxa_hl');
      if(!s){ msg.textContent='ここにはマーカーがありません'; return; }
      var p=s.parentNode; while(s.firstChild) p.insertBefore(s.firstChild, s); p.removeChild(s);
      markDirty(); msg.textContent='マーカーを消しました（保存で確定）';
    });
    // 🎨 セクションの背景色（AIなし）：右クリックした場所のセクションに、
    //   ①各セクションの背景色 ②ページで使っている色（頻度順） ③自由な色 のどれかを塗る
    var sbgB=m.querySelector('#__ce_cmsecbg'), sbgP=m.querySelector('#__ce_cmsecbgp');
    function _colOk(c){ return c && c!=='transparent' && !/rgba\\(\\s*\\d+,\\s*\\d+,\\s*\\d+,\\s*0\\)/.test(c); }
    function applySecBg(c){
      var t=(curEl&&curEl.closest)?curEl.closest('section,header,footer'):null;
      if(!t){ msg.textContent='セクション（またはヘッダー/フッター）の中を右クリックしてから使ってください'; return; }
      t.style.setProperty('background-color', c, 'important');
      // グラデーション背景が上に被っていると色が見えないので外す（写真背景(url)は残す）
      try{ var bi=getComputedStyle(t).backgroundImage; if(bi && bi.indexOf('gradient')>=0 && bi.indexOf('url(')<0) t.style.setProperty('background-image','none','important'); }catch(_){}
      markDirty();
      msg.textContent='セクションの背景色を '+c+' にしました（💾保存で確定・⟲戻すで取り消し）';
    }
    if(sbgB) sbgB.addEventListener('click',function(){
      if(sbgP.style.display!=='none'){ sbgP.style.display='none'; return; }
      if(!sbgP.innerHTML){
        function sw(c,t){ return '<button class="__ce_sbgsw" data-c="'+c+'" title="'+esc(t||c)+'" style="width:24px;height:24px;border:1px solid rgba(0,0,0,.28);border-radius:5px;cursor:pointer;background:'+c+';padding:0;margin:2px;vertical-align:middle"></button>'; }
        // ①各セクション（ヘッダー/フッター含む）の背景色＝「このHPの色」から選べる
        var secSw=[], seen={};
        [].slice.call(document.querySelectorAll('header,section,footer')).forEach(function(s,i){
          if(s.closest('[id^="__ce"]')) return;
          var c=''; try{ c=getComputedStyle(s).backgroundColor; }catch(_){ return; }
          if(!_colOk(c)||seen[c]) return; seen[c]=1;
          secSw.push(sw(c,(i+1)+'番目('+s.tagName.toLowerCase()+')の背景 '+c));
        });
        // ②ページで使われている色（文字色・背景色を頻度順に・上と重複しない色だけ）
        var cnt={};
        [].slice.call(document.querySelectorAll('body *')).slice(0,1500).forEach(function(el){
          if(el.closest('[id^="__ce"]')) return;
          var cs; try{ cs=getComputedStyle(el); }catch(_){ return; }
          [cs.backgroundColor, cs.color].forEach(function(c){ if(_colOk(c)) cnt[c]=(cnt[c]||0)+1; });
        });
        var pgSw=Object.keys(cnt).filter(function(c){return !seen[c];})
          .sort(function(a,b){return cnt[b]-cnt[a];}).slice(0,14).map(function(c){return sw(c,c+'（ページ内で使用中）');});
        sbgP.innerHTML='<div style="opacity:.75">このページのセクション色</div><div>'+(secSw.join('')||'<span style="color:#999">（取得できませんでした）</span>')+'</div>'
          +'<div style="opacity:.75;margin-top:6px">ページで使われている色</div><div>'+(pgSw.join('')||'<span style="color:#999">（なし）</span>')+'</div>'
          +'<div style="opacity:.75;margin-top:6px">新しい色（選ぶと即反映）</div><input type="color" id="__ce_sbgc" value="#f5f7fa" style="width:44px;height:26px;padding:0;border:1px solid #ccc;border-radius:5px;cursor:pointer;vertical-align:middle">';
        sbgP.addEventListener('click',function(ev){ var b=ev.target.closest('.__ce_sbgsw'); if(b) applySecBg(b.getAttribute('data-c')); });
        sbgP.querySelector('#__ce_sbgc').addEventListener('input',function(){ applySecBg(this.value); });
      }
      sbgP.style.display='block';
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
    m.querySelector('#__ce_cmdel').addEventListener('click',function(){ removeSelected(); });
    // 右クリック直後に自動でドラッグONにすると、cursor:moveがmousedownを奪ってしまい
    // 「文字を選んでから下線/マーカー/色」の選択操作ができなくなる（選べない＝消せない）。
    // そのため移動は「🖱 ドラッグで動かす」ボタンを押した時だけONにする（toggleDrag）。
  }, true);
  // メニュー外をクリックしたら閉じる＆選択マーカー(青点線)も消す（枠が残らないように）
  document.addEventListener('click',function(e){ if(_hdlDrag) return; if((curMenu||curEl) && !e.target.closest('#__ce_cm') && !e.target.closest('.__ce_hdl')) closeMenu(); }, true);
  // 右クリックで選んだ要素（青点線）は、キーボードのDeleteキーでも消せる（確認ダイアログ付き・保存で確定）
  document.addEventListener('keydown',function(e){
    if(e.key!=='Delete') return;
    var a=document.activeElement;
    if(a && (a.tagName==='INPUT' || a.tagName==='TEXTAREA' || a.isContentEditable)) return;  // 文字入力・文字編集中は誤爆させない
    e.preventDefault();
    // ★選択が外れていると何も起きず「壊れている」と見える（実報告）。選択が無いときは
    //   マウスの下のものを対象にする（他のショートカットと同じ考え方）。それも無ければ理由を言う。
    if(!curEl || !document.body.contains(curEl)){
      var u=null; try{ u=document.elementFromPoint(_ceCX,_ceCY); }catch(_){}
      var el2=null;
      try{ el2=_textAt(_ceCX,_ceCY); }catch(_){}      // 上に入れ物がかぶっていても、見えている文字を優先
      if(!el2 && u && !_inUI2(u)){
        try{ el2=pickTarget(_realTarget({target:u, clientX:_ceCX, clientY:_ceCY})); }catch(_){ el2=u; }
      }
      if(!el2 || _undraggable(el2)){
        if(msg) msg.textContent='消したいものにマウスを置くか、右クリックで選んでから Delete を押してください';
        return;
      }
      closeMenu();
      curEl=el2; el2.classList.add('__ce_sel'); selEls=[el2];
    }
    removeSelected();
  }, true);
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
      function show(t){ if(t.classList.contains('fxa_hl')||t.classList.contains('fxa_ud')) t.style.setProperty('--hlw',100); t.classList.add('fxa_in'); }
      function all(){ return [].slice.call(document.querySelectorAll('.fxa_pre:not(.fxa_in),.fxa_hl:not(.fxa_in),.fxa_ud:not(.fxa_in)')); }
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
  /* ★「再生されなかった出現アニメ」を拾う（2026-07-30）。
     .fxa_pre は監視(IntersectionObserver)が .fxa_in を付けて初めて見える。監視が張られる前に
     描き終わっていた・保険の焼き込みを剥がした後だった等で取りこぼすと **永久に透明のまま**になる。
     下の掃除は fxa を触らない約束なので誰も助けない＝ヘッダーやヒーローが出ないまま（実報告）。
     ★実際に踏んだ形：ヒーローのスライドショーが <header> の中にあり、そのヘッダーが再生されず
       opacity:0 → 中身ごと丸ごと真っ白。子を調べても opacity:1 なので原因が見えない。
     ここでは opacity を殴らず .fxa_in を付ける＝本来のアニメが遅れて再生されるだけ。
     画面に入っているものだけが対象＝下の方はスクロールした時に普通に再生される。
     early=true（読み込み直後）は **遅らせ設定が無いものだけ**。遅らせ/グループは「わざと後から出す」
     設定なので、ここで横取りすると順番に出る演出が一斉に出てしまう（camp.py に実報告あり）。 */
  function wakeFxa(early){
    try{
      [].slice.call(document.querySelectorAll('.fxa_pre:not(.fxa_in)')).forEach(function(el){
        var r=el.getBoundingClientRect();
        if(!(r.bottom>0 && r.top<(window.innerHeight||0))) return;
        var cd=+el.getAttribute('data-cedelay')||0, grp=el.getAttribute('data-cegrp');
        if(!cd && !grp){ el.classList.add('fxa_in'); return; }   /* 遅らせ無し＝もう出ているはず＝取りこぼし */
        if(early) return;                                        /* 遅らせ有りは監視に任せる（早すぎる横取りを避ける） */
        /* 最後の砦：待ち時間は**ページを開いた時から**数える。cd をまるまる待つと、既に2秒以上
           たっているのに更に数秒待つ＝白い時間が延びる（実測で悪化した）。 */
        var el0=el, left=cd-((window.performance&&performance.now)?performance.now():0);
        if(left>0) setTimeout(function(){ el0.classList.add('fxa_in'); }, left);
        else el.classList.add('fxa_in');
      });
    }catch(_){}
  }
  /* 従来の保険：透明/非表示のまま残った要素を強制表示（fxaは上の監視(IntersectionObserver)が担当するので触らない）。 */
  function sweep(){
    wakeFxa(false);
    var all=document.querySelectorAll('body *');
    for(var i=0;i<all.length;i++){
      var e=all[i];
      if(e.closest('[id^="__ce"]')) continue;
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
      /* ★polygonは対象外：〰境目の形やデザインの多角形は「隠す」用途ではないのに、
         角の座標に必ず100%を含むため誤爆して消していた（保存後に境目の形が消えるバグの犯人）。
         隠れっぱなし事故はカーテン系のinset()だけなので、そちらだけ復活させる。 */
      if(cp && cp!=='none' && cp.indexOf('polygon')<0 && /100%|inset\\(1/.test(cp)){ e.style.setProperty('clip-path','none','important'); e.style.setProperty('-webkit-clip-path','none','important'); }
    }
  }
  /* ★取りこぼしは「早い段階」で拾う（2026-07-30・ユーザー報告「開くと白い／出るまで4秒かかる」）。
     監視(FX_RUN)は DOMContentLoaded で張られ、画面内なら次のフレームで再生される。
     260ms たっても遅らせ設定なしの .fxa_pre が透明のままなら取りこぼし確定＝ここで起こす。
     2.2秒の掃除だけに任せていた頃は、ヘッダー配下のヒーローが出るまで4秒かかっていた。 */
  function run(){
    fxaStart();
    setTimeout(function(){ wakeFxa(true); }, 260);
    setTimeout(function(){ wakeFxa(true); }, 900);   /* 画像の読み込みでレイアウトが動いた分をもう一度 */
    setTimeout(sweep, 2200);
  }
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
_OP_RUN_RE = re.compile(r'<script id="__op_run">.*?</script>', re.DOTALL)
_OP_EARLY_RE = re.compile(r'<script id="__op_early">.*?</script>', re.DOTALL)
_OP_CSS_RE = re.compile(r'<style id="__op_css">.*?</style>', re.DOTALL)
# <html class="… op-wait …"> が焼き込まれていた場合に、そのクラスだけ抜く
_OP_WAIT_CLS_RE = re.compile(r'(<html\b[^>]*\bclass="[^"]*?)\s*\bop-wait\b', re.IGNORECASE)
# オープニングの幕：先出しスクリプト（ページの中身が描かれる前に「待て」の合図を出すだけ）と、
# 幕の再生スクリプトの最新版。既存カンプにも配信時に当てる＝開き直しただけで順番が直る。
_OP_CSS_TAG = (
    '<style id="__op_css">#__op_screen{position:fixed;inset:0;display:flex;align-items:center;'
    'justify-content:center;margin:0;padding:0;text-align:center}'
    '#__op_screen>*{position:static;margin:0;max-width:92vw}'
    'html.op-wait body>*:not(#__op_screen):not([id^="__ce"]),'
    'html.op-wait body>*:not(#__op_screen):not([id^="__ce"]) *{animation-play-state:paused!important}</style>')
_OP_EARLY_TAG = ('<script id="__op_early">if(document.readyState==="loading")'
                 '{document.documentElement.classList.add("op-wait");window.__opWait=1;}</script>')
_OP_RUN_TAG = (
    '<script id="__op_run">(function(){if(window.__opRan)return;window.__opRan=1;'
    'var d=document,s=d.getElementById("__op_screen");if(!s)return;var h=d.documentElement;'
    'function release(){h.classList.remove("op-wait");window.__opWait=0;'
    'try{window.dispatchEvent(new Event("ce-op-done"));}catch(_){}}'
    'if(s.getAttribute("data-paused")==="1"){release();return;}'
    'if(getComputedStyle(s).display==="none"){release();return;}'
    'h.classList.add("op-wait");window.__opWait=1;'
    's.style.transition="opacity .6s ease";s.style.opacity="0";'
    'requestAnimationFrame(function(){requestAnimationFrame(function(){s.style.opacity="1";});});'
    'setTimeout(function(){if(s.getAttribute("data-paused")==="1"){release();return;}s.style.opacity="0";'
    'setTimeout(function(){s.style.display="none";release();},650);},1800);'
    'setTimeout(release,8000);})();</script>')

# ★編集中（ツールで開いている時）は幕を流さない版（2026-07-30・ユーザー報告「開くと2〜3秒真っ白」）。
#   幕の既定色は radial-gradient(#eafff6→#eef4ff→#ffffff)＝ほぼ白。その約2.5秒のあいだ op-wait が
#   ページ側のアニメを止めるので、開くたびに「白い画面が2〜3秒」続いていた（実測：幕591ms→2822ms、
#   中身が出そろうのは3.7s）。編集は何度も開き直すので、待ち時間がそのまま作業の邪魔になる。
#   ＝編集中だけ即座に中身を出す。保存版・👁プレビューは今までどおり流れる（cleanHtmlがdisplayを戻す）。
_OP_SKIP_RUN_TAG = (
    '<script id="__op_run">(function(){window.__opRan=1;window.__opWait=0;'
    'var d=document,s=d.getElementById("__op_screen"),h=d.documentElement;'
    'if(s)s.style.display="none";h.classList.remove("op-wait");'
    'try{window.dispatchEvent(new Event("ce-op-done"));}catch(_){}})();</script>')
# 先出しスクリプトも無効化する。★これを残すと head の時点で op-wait が付き、
#   幕を消しても「アニメだけ止まったまま」になる（保険の8秒タイマーまで待つ）。
#   DOMContentLoaded の処理は、__op_run を持たない古いカンプ用の保険。
_OP_SKIP_EARLY_TAG = (
    '<script id="__op_early">window.__opRan=1;window.__opWait=0;'
    'document.addEventListener("DOMContentLoaded",function(){'
    'var s=document.getElementById("__op_screen");if(s)s.style.display="none";'
    'document.documentElement.classList.remove("op-wait");'
    'try{window.dispatchEvent(new Event("ce-op-done"));}catch(_){}});</script>')


def _upgrade_opening(html: str, editing: bool = False) -> str:
    """幕(#__op_screen)を持つ既存カンプに、最新の再生スクリプトと先出しスクリプトを当てる。

    ★狙い：オープニングより先にヒーローのアニメが動き出してしまう順番の崩れを、
      開き直しただけで直す（幕そのものをbody先頭へ動かすのは編集バー側 opUpgrade が行い、💾保存で確定する）。
    editing=True（ツールで開いた時）は幕を流さない版を当てる＝開いた瞬間から編集できる。
    幕は消さずに display:none にするだけなので、👁/▶ でいつでも確認でき、💾保存すれば元に戻る。
    """
    if 'id="__op_screen"' not in html:
        # ★幕だけ消えて「待て」の合図(__op_early)が残った“みなしご”を掃除する（2026-07-30）。
        #   __op_early は読み込み時に html.op-wait を付けるだけの役で、外すのは幕の再生スクリプト
        #   (__op_run)。幕を手で消したカンプは外す役がいないので **op-wait が永久に残り、
        #   ページのアニメが全部止まったまま＝開いても中身が出ない**（実測：4秒たっても出ない）。
        #   🗑ボタン(removeOpening)は3つまとめて外すが、それ以前に作られたカンプが壊れたまま残る。
        #   ここで丸ごと消す＝この状態で💾保存すれば、単体HTMLでも二度と起きない。
        if 'id="__op_early"' in html or 'id="__op_css"' in html:
            html = _OP_EARLY_RE.sub("", html, count=1)
            html = _OP_RUN_RE.sub("", html, count=1)
            html = _OP_CSS_RE.sub("", html, count=1)
            html = _OP_WAIT_CLS_RE.sub(r"\1", html, count=1)
        return html
    run_tag = _OP_SKIP_RUN_TAG if editing else _OP_RUN_TAG
    early_tag = _OP_SKIP_EARLY_TAG if editing else _OP_EARLY_TAG
    html = _OP_RUN_RE.sub(run_tag, html, count=1)
    if 'id="__op_early"' in html:
        html = _OP_EARLY_RE.sub(early_tag, html, count=1)
    head = ""
    if 'id="__op_css"' not in html:
        head += _OP_CSS_TAG
    if 'id="__op_early"' not in html:
        head += early_tag
    if head:
        low = html.lower()
        i = low.find("</head>")
        if i >= 0:
            html = html[:i] + head + html[i:]
    return html
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
    html = _upgrade_opening(html, editing=True)   # 幕→ヒーローの順番を当てる＋編集中は幕を流さない
    bar = _SERVE_SAFETY + _EDIT_BAR.replace("%FILE_JSON%", _json.dumps(filename)).replace(
        "%EDIT_PROVIDER_JSON%", _json.dumps(config.CONFIG.htmlgen.edit_provider))
    low = html.lower()
    if "</body>" in low:
        i = low.rfind("</body>")
        return html[:i] + bar + html[i:]
    return html + bar


@app.route("/api/menu_search", methods=["POST"])
def api_menu_search():
    """🔎 メニューの曖昧検索（AI担当・2026-07-30）。

    ブラウザ側でまず言い換え表を使ったローカル検索を行い、**当たらなかった時だけ**ここへ来る。
    ＝ふだんは無料・一瞬で終わり、AIの費用が出るのは「言葉が表に無かった時」だけ。

    受け取り: {"q": "余白を取りたい", "labels": ["＋高くする", "🖱 掴んで動かす", ...]}
    返す:     {"idx": [3, 7], "by": "gemini"}  ← labels の何番目が近いか（近い順）

    ★モデルは既定で gemini-3.5-flash-lite（Lite系の最新・入力$0.30/出力$2.50 per 1M）。
      生成用のモデル設定とは別に持つ：カンプ生成は高品質モデル、こちらは短文なので最安で足りる。
    """
    import urllib.request

    data = request.get_json(silent=True) or {}
    q = (data.get("q") or "").strip()
    labels = [str(x) for x in (data.get("labels") or [])][:400]
    if not q or not labels:
        return jsonify({"idx": [], "by": "none"})

    gcfg = config.CONFIG.gemini
    if not getattr(gcfg, "enabled", False):
        return jsonify({"idx": [], "by": "nokey",
                        "message": "AI検索を使うには ⚙設定 で Gemini のAPIキーを入れてください"})

    model = os.environ.get("DESIGN_STOCK_GEMINI_MENU_MODEL", "gemini-3.5-flash-lite")
    numbered = "\n".join(f"{i}: {s}" for i, s in enumerate(labels))
    prompt = (
        "あなたはデザインツールのメニュー検索です。\n"
        "利用者のやりたいことに合うメニュー項目を、下の一覧から選んでください。\n"
        "・近い順に最大5件\n"
        "・番号だけをJSON配列で返す（例: [12,3]）\n"
        "・該当が無ければ []\n"
        "・説明や前置きは書かない\n\n"
        f"【やりたいこと】{q}\n\n【メニュー一覧】\n{numbered}\n"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 200, "temperature": 0},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={gcfg.api_key}")
    try:
        req = urllib.request.Request(
            url, data=_json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = _json.loads(resp.read().decode("utf-8"))
        text = "".join(p.get("text", "") for p in res["candidates"][0]["content"]["parts"])
    except Exception as exc:  # ネット断・キー間違い等でもUIは止めない
        log.warning("メニューAI検索に失敗: %s", exc)
        return jsonify({"idx": [], "by": "error", "message": "AI検索に届きませんでした（通信かキーを確認）"})

    m = re.search(r"\[[^\]]*\]", text)          # 前置きが付いても配列だけ拾う
    idx: list[int] = []
    if m:
        try:
            idx = [int(v) for v in _json.loads(m.group(0)) if 0 <= int(v) < len(labels)]
        except Exception:
            idx = []
    return jsonify({"idx": idx[:5], "by": "gemini", "model": model})


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


@app.route("/camp_preview/<path:filename>")
def camp_preview(filename: str):
    """履歴一覧のホバープレビュー用：編集バーを注入せず生のHTMLをそのまま返す（軽量・読み取り専用）。"""
    path = config.CAMP_DIR / filename
    if not path.exists() or not path.is_file() or path.suffix != ".html" or path.parent != config.CAMP_DIR:
        abort(404)
    return send_file(path)


@app.route("/camp_figma/<path:filename>")
def camp_figma(filename: str):
    """🎨 Figmaキャプチャ用ページ：掃除＋アニメ潰し（完成状態で固定）した素のHTMLを返す。
    画像はサーバー配信のまま（軽い）＝ツール起動中に開いて html.to.design 拡張でCaptureする用。"""
    path = config.CAMP_DIR / filename
    if not path.exists() or not path.is_file() or path.suffix != ".html" or path.parent != config.CAMP_DIR:
        abort(404)
    return Response(figmakit.capture_ready(path.read_text(encoding="utf-8")), mimetype="text/html")


# ── 🆚 Before/After比較ビュー（AIなし・営業デモ用） ──────────────────────────
# 2つのカンプを重ねてスライダーで見比べる／左右並べ。スクロールは比率で同期。
# iframeは /camp_preview/（編集バー無しの素のHTML）を使う＝表示が軽く事故らない。
_COMPARE_PAGE = """<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<title>🆚 Before/After 比較</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:'Hiragino Sans','Yu Gothic',sans-serif;background:#1c2230}
.bar{display:flex;gap:8px;align-items:center;padding:8px 12px;height:52px;color:#dde}
.bar select{max-width:340px;padding:6px;border-radius:6px;border:1px solid #445;background:#232b3d;color:#dde;font-size:12.5px}
.bar button{padding:6px 12px;border:0;border-radius:6px;background:#3a4763;color:#fff;font-size:12.5px;cursor:pointer}
.bar button:hover{background:#4a5a7d}
.bar label{font-size:12px;display:flex;align-items:center;gap:4px}
.bar .hint{font-size:11px;color:#89a;margin-left:auto}
.wrap{position:relative;height:calc(100vh - 52px);background:#fff}
.wrap iframe{position:absolute;inset:0;width:100%;height:100%;border:0;background:#fff}
#ifB{clip-path:inset(0 0 0 var(--x,50%))}
#divider{position:absolute;top:0;bottom:0;left:var(--x,50%);width:0;border-left:3px solid #e91e63;z-index:6;cursor:ew-resize}
#divider .knob{position:absolute;top:50%;left:-17px;width:32px;height:32px;border-radius:50%;background:#e91e63;color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:14px;transform:translateY(-50%);box-shadow:0 2px 8px rgba(0,0,0,.4)}
.badge{position:absolute;top:10px;z-index:5;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:700;color:#fff;pointer-events:none}
#bA{left:10px;background:rgba(180,60,60,.9)}
#bB{right:10px;background:rgba(30,140,80,.9)}
body.sbs .wrap{display:grid;grid-template-columns:1fr 1fr;gap:6px;background:#1c2230}
body.sbs .wrap iframe{position:relative;inset:auto;clip-path:none!important}
body.sbs #divider{display:none}
</style></head><body>
<div class="bar">
  <span style="font-size:14px">🆚</span>
  <select id="selA"></select>
  <button id="swap" title="AとBを入れ替え">⇄</button>
  <select id="selB"></select>
  <button id="mode">◧ 左右に並べる</button>
  <label><input type="checkbox" id="sync" checked>スクロール同期</label>
  <span class="hint">スライダーの◉を左右にドラッグ／左＝Before・右＝After</span>
</div>
<div class="wrap" id="wrap">
  <iframe id="ifA"></iframe>
  <iframe id="ifB"></iframe>
  <div class="badge" id="bA">Before</div>
  <div class="badge" id="bB">After</div>
  <div id="divider"><div class="knob">◉</div></div>
</div>
<script>
var qs=new URLSearchParams(location.search);
var selA=document.getElementById('selA'), selB=document.getElementById('selB');
var ifA=document.getElementById('ifA'), ifB=document.getElementById('ifB');
var wrap=document.getElementById('wrap'), divider=document.getElementById('divider');
var lock=false;
function label(c){ return (c.fav?'⭐':'')+(c.name?c.name+' ':'')+(c.title||c.file); }
fetch('/api/camps').then(function(r){return r.json();}).then(function(d){
  var camps=d.camps||[];
  [selA,selB].forEach(function(sel){
    camps.forEach(function(c){ var o=document.createElement('option'); o.value=c.file; o.textContent=label(c); sel.appendChild(o); });
  });
  selA.value=qs.get('a')||(camps[1]?camps[1].file:(camps[0]&&camps[0].file)||'');
  selB.value=qs.get('b')||(camps[0]&&camps[0].file)||'';
  load();
});
function load(){
  if(selA.value) ifA.src='/camp_preview/'+encodeURIComponent(selA.value);
  if(selB.value) ifB.src='/camp_preview/'+encodeURIComponent(selB.value);
  try{ history.replaceState(null,'','/compare?a='+encodeURIComponent(selA.value)+'&b='+encodeURIComponent(selB.value)); }catch(e){}
}
selA.addEventListener('change',load); selB.addEventListener('change',load);
document.getElementById('swap').addEventListener('click',function(){
  var t=selA.value; selA.value=selB.value; selB.value=t; load();
});
document.getElementById('mode').addEventListener('click',function(){
  document.body.classList.toggle('sbs');
  this.textContent=document.body.classList.contains('sbs')?'◫ 重ねてスライダー':'◧ 左右に並べる';
});
// スクロール同期（高さが違っても比率で合わせる）
function hook(me,other){
  me.addEventListener('load',function(){
    try{
      me.contentWindow.addEventListener('scroll',function(){
        if(!document.getElementById('sync').checked||lock) return;
        lock=true;
        try{
          var w=me.contentWindow, d=w.document.documentElement;
          var r=w.scrollY/Math.max(1,d.scrollHeight-w.innerHeight);
          var ow=other.contentWindow, od=ow.document.documentElement;
          ow.scrollTo(0, r*Math.max(0,od.scrollHeight-ow.innerHeight));
        }catch(e){}
        requestAnimationFrame(function(){ lock=false; });
      });
    }catch(e){}
  });
}
hook(ifA,ifB); hook(ifB,ifA);
// スライダーのドラッグ
var knob=divider.querySelector('.knob');
knob.addEventListener('pointerdown',function(ev){
  ev.preventDefault(); knob.setPointerCapture(ev.pointerId);
  ifA.style.pointerEvents='none'; ifB.style.pointerEvents='none';
  function mv(e){
    var rc=wrap.getBoundingClientRect();
    var x=Math.min(98,Math.max(2,(e.clientX-rc.left)/rc.width*100));
    wrap.style.setProperty('--x',x+'%');
  }
  function up(e){
    knob.releasePointerCapture(ev.pointerId);
    knob.removeEventListener('pointermove',mv); knob.removeEventListener('pointerup',up);
    ifA.style.pointerEvents=''; ifB.style.pointerEvents='';
  }
  knob.addEventListener('pointermove',mv); knob.addEventListener('pointerup',up);
});
</script></body></html>"""


@app.route("/compare")
def compare_page():
    """🆚 Before/After比較ビュー（?a=<file>&b=<file> で初期選択・省略時は最新2つ）。"""
    return Response(_COMPARE_PAGE, mimetype="text/html")


@app.route("/img/<site_id>/<which>")
def img(site_id: str, which: str):
    """スクショ画像を返す。which は firstview / fullpage。"""
    column = "firstview_path" if which != "fullpage" else "fullpage_path"
    with db.connect() as conn:
        row = db.get_site(conn, site_id)
    if not row or not row[column]:
        abort(404)
    path = config.resolve_data_path(row[column])
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
    path = config.resolve_data_path(row["animation_video_path"])
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="video/webm")


def serve(host: str = "127.0.0.1", port: int = 5000, preload: bool = True, dev: bool = False) -> None:
    """ビューアを起動する。preload=True で起動時にモデルを読み込んでおく。

    preload=False（--no-preload・このPCの推奨）でも、起動して少し経ったら
    バックグラウンドでこっそり読み込む＝初回検索の「遅い」を体感ゼロに近づける。
    読み込みがメモリ不足で失敗しても握りつぶす（従来どおり初回検索時に再挑戦される）。

    dev=True（--dev）＝開発モード：.py を保存すると自動でサーバが再起動する。
    手で Ctrl+C → 起動し直しが不要になる。モデル先読みは再起動のたびに重くなるので
    しない（初回検索時に読む）。templates/viewer.html は毎リクエスト読み直す作りなので
    元々ブラウザのF5だけで反映される＝再起動対象はPythonだけでよい。
    """
    def _boot():
        db.init_db()
        if dev:
            pass  # 開発モードは先読みなし（再起動が速いことを優先）
        elif preload:
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

    if dev:
        # werkzeug（Flask同梱）のリローダで包む＝importしている全.pyを監視し、
        # 保存を検知したらプロセスごと再起動する。waitressのまま使えるのが利点
        # （Flask開発サーバに切り替えると Failed to fetch 問題が再発するため）。
        try:
            from werkzeug._reloader import run_with_reloader
        except ImportError:  # 古いwerkzeugは公開APIにある
            from werkzeug.serving import run_with_reloader
        log.info("開発モード: .py を保存すると自動で再起動します")
        run_with_reloader(_boot)
    else:
        _boot()
