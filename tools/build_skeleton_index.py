"""クローンHTMLからセクションの型辞書を作る（候補パネルの素材）。

使い方:
    python tools/build_skeleton_index.py            # 画像がそろっているクローン全部
    python tools/build_skeleton_index.py --all      # 画像が無いクローンも含める
    python tools/build_skeleton_index.py --limit 5  # 動作確認用に5本だけ

★画像フォルダ（clone_*_files）が無いクローンは既定で除外する。
  .gitignore で画像を同期していないので、別PCから来たクローンは画像が全部欠ける
  （CLAUDE.md 9.7）。サムネが壊れた候補を並べても選べないため。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config, skeleton  # noqa: E402


def pick_files(include_all: bool, limit: int) -> list[Path]:
    files = sorted(config.CAMP_DIR.glob("clone_*.html"))
    files = [f for f in files if not f.name.startswith("_")]
    if not include_all:
        keep = []
        for f in files:
            if (f.parent / f"{f.stem}_files").is_dir():
                keep.append(f)
        print(f"画像がそろっているクローン: {len(keep)} / {len(files)} 本")
        files = keep
    if limit > 0:
        files = files[:limit]
    return files


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="画像が無いクローンも含める")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-shots", action="store_true", help="サムネを作らない（速い）")
    args = ap.parse_args()

    files = pick_files(args.all, args.limit)
    if not files:
        print("対象がありません")
        return

    index = skeleton.build_index(files, shots=not args.no_shots)

    print("\n==== 型は何種類に収束したか ====")
    rows = skeleton.type_summary(index)
    for t, c in rows:
        print(f"{c:4d}  {t}")
    print(f"\n型の種類: {len(rows)} 種類 / セクション {index['n_sections']}件")

    roles: dict = {}
    for s in index["sections"]:
        roles[s["role"]] = roles.get(s["role"], 0) + 1
    print("役割ごと:", roles)


if __name__ == "__main__":
    main()
