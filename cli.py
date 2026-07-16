"""
デザイン参照ストック & 曖昧検索システム — CLI入口（Phase 1）。

サブコマンド：
  ingest   URL（または--listのファイル）を撮影してDBに保存
  embed    撮影済みレコードを SigLIP-2 で埋め込み
  search   text または image で検索 → results.html を出力して開く
  status   現在の件数（全件 / 埋め込み済み）を表示

使い方の例：
  python cli.py ingest https://example.com
  python cli.py ingest --list urls.txt
  python cli.py embed
  python cli.py search "落ち着いた上品な青系のサイト"
  python cli.py search --image data/screenshots/foo__firstview.png
  python cli.py status
"""

from __future__ import annotations

# ★ 何よりも先に設定する（numpy/torch等が読まれる前）。
#   torch同梱のOpenMP(libiomp5md.dll)が numpy/MKL の別OpenMPと二重ロードされると
#   セグメンテーション違反で落ちる。プロセス先頭で許可しておくと確実に回避できる。
import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

# .env を読み込む（ANTHROPIC_API_KEY 等）。無くても動く（その機能だけ使えない）。
# override=True：.env を最優先にする（システム環境に古い/無効なキーが残っていても
# .env の値で上書きされる。これが無いと無効な環境変数が勝ってしまう）。
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

import argparse
import sys
from pathlib import Path

from src import db, embed, ingest, search, vibe
from src.utils import setup_logging, get_logger

log = get_logger("cli")


def cmd_ingest(args: argparse.Namespace) -> int:
    if args.list:
        urls = ingest.read_url_list(Path(args.list))
        if not urls:
            log.error("リストにURLがありません: %s", args.list)
            return 1
    elif args.urls:
        urls = args.urls
    else:
        log.error("URL を渡すか --list でファイルを指定してください")
        return 1
    summary = ingest.capture_many(urls, force=args.force)
    print(f"\n取り込み完了: 保存={summary['saved']} / スキップ={summary['skipped']} / 失敗={summary['failed']}")
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    summary = embed.embed_all(force=args.force)
    print(f"\n埋め込み完了: 完了={summary['embedded']} / 失敗={summary['failed']}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    if not args.query and not args.image:
        log.error("検索ワード または --image を指定してください")
        return 1
    search.search_and_show(
        query=args.query,
        image=args.image,
        top_n=args.top,
        open_browser=not args.no_open,
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    db.init_db()
    with db.connect() as conn:
        total, embedded = db.count_sites(conn)
    print(f"登録サイト: {total} 件 / 埋め込み済み: {embedded} 件 / 未埋め込み: {total - embedded} 件")
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    summary = vibe.describe_all()
    print(f"\n雰囲気の言語化 完了: 生成={summary['described']} / 失敗={summary['failed']}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    # viewer は flask/torch を読むので、必要になったここで初めて import する
    from src import viewer

    url = f"http://{args.host}:{args.port}"
    if not args.no_open:
        # 起動と同時にブラウザを開く（サーバ起動前に予約だけ入れる）
        import threading
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    viewer.serve(host=args.host, port=args.port, preload=not args.no_preload, dev=args.dev)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="design-stock",
        description="デザイン参照ストック & 曖昧検索システム（Phase 1）",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="ログを控えめにする")
    sub = parser.add_subparsers(dest="command", required=True)

    # ingest
    p_ingest = sub.add_parser("ingest", help="URLを撮影してDBに保存")
    p_ingest.add_argument("urls", nargs="*", help="1件以上のURL")
    p_ingest.add_argument("--list", help="1行1URLのファイルパス")
    p_ingest.add_argument("--force", action="store_true", help="撮影済みでも撮り直す")
    p_ingest.set_defaults(func=cmd_ingest)

    # embed
    p_embed = sub.add_parser("embed", help="撮影済みレコードを埋め込み")
    p_embed.add_argument("--force", action="store_true", help="全件を再埋め込み")
    p_embed.set_defaults(func=cmd_embed)

    # search
    p_search = sub.add_parser("search", help="検索してresults.htmlを開く")
    p_search.add_argument("query", nargs="?", help="検索ワード（雰囲気を言葉で）")
    p_search.add_argument("--image", help="参照画像のパス（image→image検索）")
    p_search.add_argument("--top", type=int, default=None, help="表示件数（既定24）")
    p_search.add_argument("--no-open", action="store_true", help="ブラウザを自動で開かない")
    p_search.set_defaults(func=cmd_search)

    # status
    p_status = sub.add_parser("status", help="件数を表示")
    p_status.set_defaults(func=cmd_status)

    # describe（雰囲気を言語化）
    p_desc = sub.add_parser("describe", help="未処理サイトの雰囲気をClaudeで言語化（ハイブリッド検索用）")
    p_desc.set_defaults(func=cmd_describe)

    # serve（ローカルビューア）
    p_serve = sub.add_parser("serve", help="検索ボックス付きビューアを起動（モデル常駐）")
    p_serve.add_argument("--host", default="127.0.0.1", help="待ち受けホスト")
    p_serve.add_argument("--port", type=int, default=5000, help="待ち受けポート")
    p_serve.add_argument("--no-open", action="store_true", help="ブラウザを自動で開かない")
    p_serve.add_argument("--no-preload", action="store_true", help="起動時のモデル先読みをしない")
    p_serve.add_argument("--dev", action="store_true", help="開発モード（.py保存で自動再起動）")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(verbose=not args.quiet)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
