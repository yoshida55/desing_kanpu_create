"""機能一覧（FEATURES.md）をソースコードから自動生成する。

なぜ手書きしないか：
  機能を足すたびに手で書き足すと必ず陳腐化する（README.mdが実際にPhase1のまま化石化した）。
  幸いこのツールはメニューのラベルが最初から一般向けの日本語なので、そのまま一覧にできる。

使い方：
  python tools/gen_features.py          # FEATURES.md を書き出す
  python tools/gen_features.py --check  # 中身が最新かだけ確認（CI/差分チェック用・書き換えない）

出力先: プロジェクト直下の FEATURES.md
"""

from __future__ import annotations

import ast
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWER = ROOT / "src" / "viewer.py"
OUT = ROOT / "FEATURES.md"

# viewer.py は1万行近く、しかも中にJSが埋まっている。構文解析ではなく素直に正規表現で拾う。
RE_QUICK = re.compile(r"\['(__ce_q_[a-z]+)','([^']+)'\]")
RE_BAR = re.compile(r'<button class="im" id="(__ce_[a-z_]+)"[^>]*>([^<]+)')
RE_ROUTE = re.compile(r"@app\.route\(\s*[\"']([^\"']+)[\"']")

# ラベルからの「AI課金かどうか」推測が外れるものだけ明示的に上書きする。
PAID_OVERRIDE: dict[str, bool] = {
    # 書き出すだけなので無料。ラベルの「AIに書かせる」は"渡した後"の話であってこの操作の費用ではない
    "__ce_prod": False,
}


def _read(p: Path) -> str:
    return io.open(p, encoding="utf-8").read()


def _modules() -> list[tuple[str, str]]:
    """src/*.py の1行目docstringを「そのファイルの役割」として集める。"""
    out: list[tuple[str, str]] = []
    for f in sorted((ROOT / "src").glob("*.py")):
        if f.name == "__init__.py":
            continue
        doc = ""
        try:
            mod = ast.parse(_read(f))
            lines = (ast.get_docstring(mod) or "").strip().splitlines()
            doc = lines[0].strip() if lines else ""
        except Exception:  # noqa: BLE001  壊れていても一覧は出す
            doc = ""
        out.append((f.name, doc or "（説明なし＝docstringを1行足すとここに出ます）"))
    return out


def build() -> str:
    src = _read(VIEWER)

    quick = RE_QUICK.findall(src)
    bar = RE_BAR.findall(src)
    routes = sorted({r for r in RE_ROUTE.findall(src)})
    mods = _modules()

    # AIを使う＝お金がかかるものはラベルに書いてある（「AI」「課金」「数円」）。読む側の判断材料になる。
    # ★「AIなし」「AI不要」は"AI"を含むが無料の意味＝先に弾く（部分一致で💰と誤判定していた）
    # ★それでも外れるものは PAID_OVERRIDE で明示（ラベル推測の限界。増えたらそこに足す）
    def paid(mid: str, label: str) -> bool:
        if mid in PAID_OVERRIDE:
            return PAID_OVERRIDE[mid]
        if "AIなし" in label or "AI不要" in label:
            return False
        return any(k in label for k in ("AI", "課金", "数円", "有料"))

    L: list[str] = []
    a = L.append
    a("# FEATURES — このツールで「できること」の全一覧")
    a("")
    a("> ⚠ **このファイルは自動生成です。手で書き換えないでください。**")
    a("> 生成: `python tools/gen_features.py` ／ 元ネタ: `src/viewer.py` と `src/*.py`")
    a("> 機能を足すと、次に生成し直した時点でここに自動で載ります。")
    a("")
    a("読む人へ：`CLAUDE.md` は**時系列の開発ログ**（なぜ作ったか・バグの原因）であって機能一覧ではありません。")
    a("「何ができるか」を知りたいならこのファイルだけで足ります。")
    a("")
    a(f"規模: 右クリック {len(quick)}項目 / 編集バー {len(bar)}項目 / APIパス {len(routes)}本 / モジュール {len(mods)}本")
    a("")
    a("💰=AIを呼ぶ（お金がかかる） ／ 無料=AIを使わない。ほとんどの操作は無料です。")
    a("")
    a("---")
    a("")
    a("## 1. 右クリックメニュー（要素を選んでその場で直す）")
    a("")
    a("カンプ上で要素を右クリックすると出るメニュー。ラベルは実際の画面表示そのまま。")
    a("")
    a("| 機能 | ID | AI課金 |")
    a("|---|---|---|")
    for mid, label in quick:
        a(f"| {label} | `{mid}` | {'💰' if paid(mid, label) else '無料'} |")
    a("")
    a("## 2. 編集バー（画面右上・ページ全体に効く操作）")
    a("")
    a("| 機能 | ID | AI課金 |")
    a("|---|---|---|")
    for mid, label in bar:
        a(f"| {label.strip()} | `{mid}` | {'💰' if paid(mid, label) else '無料'} |")
    a("")
    a("## 3. モジュール構成（どのファイルが何をしているか）")
    a("")
    a("| ファイル | 役割 |")
    a("|---|---|")
    for name, doc in mods:
        a(f"| `src/{name}` | {doc} |")
    a("")
    a("## 4. APIエンドポイント")
    a("")
    a("<details><summary>全" + str(len(routes)) + "本（クリックで展開）</summary>")
    a("")
    for r in routes:
        a(f"- `{r}`")
    a("")
    a("</details>")
    a("")
    return "\n".join(L) + "\n"


def main() -> int:
    text = build()
    if "--check" in sys.argv:
        cur = _read(OUT) if OUT.exists() else ""
        if cur == text:
            print("FEATURES.md は最新です")
            return 0
        print("FEATURES.md が古いです → python tools/gen_features.py で更新してください")
        return 1
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(text)
    print(f"書き出しました: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
