"""📦 本番化キット書き出し（AIなし＝無料・一瞬）。

カンプを「Claude Code / Codex に渡して本番用HTML/CSS/JSに書き起こさせる」ための
フォルダ一式を作る。狙いは **AIが読んだ瞬間に取りかかれる状態**：
  1. カンプ見本.html … ツールの痕跡（保険スクリプト・data-ce*等）をそうじ済み＝AIを迷わせるゴミゼロ
  2. anim.css / anim.js … 検証済みアニメ部品（AIに自作させない＝壊れない）
  3. 変換指示.md … 毎回打っていたプロンプトの完成形（規約準拠・テキスト全保持・対応表つき）
  4. コーディング規約.md … ナレッジからコピーして同梱（フォルダだけで自己完結）

使い方：カンプの編集バー「📦 本番化キット」→ 出来たフォルダでClaude Code/Codexを開き
「変換指示.mdどおりにやって」と言うだけ。
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import camp, config
from .animkit import KIT_CSS, KIT_JS, scan_camp
from .utils import get_logger

log = get_logger(__name__)

# コーディング規約の場所（会社PCなど別パスの場合は .env の DESIGN_STOCK_CODING_RULES で差し替え）
RULES_PATH = Path(os.environ.get(
    "DESIGN_STOCK_CODING_RULES",
    r"D:\50_knowledge\01_コーディングルール・命名規約.md",
))

PROD_DIR = config.CAMP_DIR / "prod"


def _clean(html: str) -> str:
    """ツールの痕跡を機械そうじ（見た目は一切変えない）。"""
    # 保険スクリプト（真っ黒対策の強制表示）＝本番コードに混入させない
    html = re.sub(re.escape(camp._SAFE_START) + r".*?" + re.escape(camp._SAFE_END),
                  "", html, flags=re.DOTALL)
    # ドラッグ編集などの内部記録属性
    html = re.sub(r'\s+data-ce[a-z0-9_]*="[^"]*"', "", html)
    html = re.sub(r"\s+data-ce[a-z0-9_]*='[^']*'", "", html)
    # ベースひも付けメタ（ツール内部用）
    html = re.sub(r'<meta[^>]+name="ce-base"[^>]*>\s*', "", html)
    return html


def build_prodkit(filename: str, out_dir: str | None = None) -> dict:
    """カンプ1本ぶんの本番化キットフォルダを書き出す。戻り値 {dir, rows, rules}。

    out_dir を渡すと「そのフォルダ／カンプ名／」に出力（未指定なら従来どおり data/camps/prod/）。
    """
    src = config.CAMP_DIR / filename
    if not src.exists():
        raise FileNotFoundError(f"カンプが見つかりません: {filename}")
    html = src.read_text(encoding="utf-8")
    rows = scan_camp(html)  # そうじ前に読む（data-cedelayを対応表へ写すため）

    base = Path(out_dir).expanduser() if (out_dir or "").strip() else PROD_DIR
    if not base.is_absolute() and base is not PROD_DIR:
        raise ValueError("出力フォルダはフルパス（例 D:\\web\\kit）で指定してください")
    out = base / src.stem
    out.mkdir(parents=True, exist_ok=True)
    (out / "カンプ見本.html").write_text(_clean(html), encoding="utf-8")
    (out / "anim.css").write_text(KIT_CSS, encoding="utf-8")
    (out / "anim.js").write_text(KIT_JS, encoding="utf-8")

    rules_ok = RULES_PATH.exists()
    if rules_ok:
        shutil.copy(RULES_PATH, out / "コーディング規約.md")

    table = "\n".join(
        f"| {r['sec']} | `{r['el']}` {r['text'][:24]} | `{r['kit']}` |"
        for r in rows
    ) or "| - | アニメ付き要素なし | - |"

    ins = _INSTRUCTION.replace("%TABLE%", table)
    ins = ins.replace("%DATE%", datetime.now().strftime("%Y-%m-%d"))
    ins = ins.replace("%RULESNOTE%", "" if rules_ok else
                      f"\n> ⚠ コーディング規約.mdの同梱に失敗（見つからない: {RULES_PATH}）。規約は口頭指示で補うこと。\n")
    (out / "変換指示.md").write_text(ins, encoding="utf-8")

    log.info("本番化キットを書き出し: %s（アニメ%d件・規約同梱=%s）", out, len(rows), rules_ok)
    return {"dir": str(out), "rows": len(rows), "rules": rules_ok}


# AIコーディングエージェントへの指示書。%TABLE%/%DATE%/%RULESNOTE%を差し込む
_INSTRUCTION = """# 変換指示 — カンプを本番用HTML/CSS/JSに書き起こす（%DATE%生成）

あなた（AIコーディングエージェント）への指示。このフォルダの **カンプ見本.html** を見本として、
本番用のコードを新規に書き起こすこと。
%RULESNOTE%
## 作るファイル

```
index.html
css/reset.css      … リセットCSS（標準的なものでよい）
css/style.css      … 共通（html{font-size:62.5%}＝1rem=10px を定義）
css/index.css      … このページのスタイル
css/anim.css       … 同梱のものをコピー（改変禁止）
js/anim.js         … 同梱のものをコピー（改変禁止）
```
読み込み順は reset → style → index → anim。anim.jsは</body>直前。

## 大前提（違反したら失敗とみなす）

1. **同梱の「コーディング規約.md」に完全準拠**。特に：
   - クラス名＝英語2単語・アンスコ区切り（`service_area`）。タグ名・手法名（_flex等）を入れない
   - メインビジュアルのクラスは `main`（hero/mvとは書かない）
   - インラインstyle禁止・remで指定（1rem=10px）・余白はmargin-bottom統一
   - line-height/letter-spacingはem換算（規約の計算式どおり）
   - PCファースト、メディアクエリはCSS末尾にまとめる
   - コメントはボックス型区切り＋人間らしい表現（`/* ===== */`はAIっぽいので禁止）
2. **文章は一字一句そのまま**。要約・言い換え・省略は禁止。画像も全部使う（URLは見本のまま）
3. **カンプ見本.htmlのクラス名やHTML構造は真似しない**。あれはツールの内部表現。
   見た目だけを忠実に再現し、コードは規約に沿ってゼロから設計する。
   特に見本内の`<script>`と`fxa_*`クラスは**プレビュー再生用の機械**なので、1行もコピーしない
   （動きの再現はすべて同梱のanim.css/anim.js＋下の対応表で行う）
4. レイアウトはflex/gridで組む。position:absoluteは飾りの後乗せだけ（親にrelative）

## アニメーション

anim.css / anim.js は検証済みの完成品。**自作せず**、下の対応表どおり要素にクラスを付けるだけ。
（`rv`＝出現の合図、`rv-up`等＝動きの種類、`data-delay="ミリ秒"`＝遅らせ。
JSが`<html>`に`anim-on`を付けたときだけ動く＝JS無効でも消えない設計）

| 場所 | 見本での要素（目印テキスト） | 付けるクラス・属性 |
|---|---|---|
%TABLE%

## 完了チェック（必ず実行して結果を報告）

1. コーディング規約.md末尾の「コーディング完了チェックリスト」を全項目確認
2. 見本の全テキストが出力に残っているか機械照合（欠落ゼロ）
3. ブラウザ（Playwright可）で表示し、PC幅/スマホ幅で崩れ・横スクロールがないか確認
"""
