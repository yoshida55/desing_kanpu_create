# Codex 作業ルール（カンプの見た目を直すとき）

このファイルは Codex 向け。カンプHTMLを**直接編集しない**ため、必ずここに書いた手順で作業する。
仕組みの全体像は `Codexと編集ツールの共同編集仕様書.md`。

## 絶対に守ること

- カンプHTML（`data/camps/*.html`）を直接開いて書き換えない
- `viewer.py` を案件ごとに変更しない
- 変更は `python tools/camp_patch.py` だけで作る
- 構造変更（div の追加・削除・並べ替え）は行わない。必要なら「未対応」として報告する

## 手順

```powershell
# 1. 前提がズレていないか確認（stale:true なら作業しない。ユーザーに開き直してもらう）
python tools/camp_patch.py status --file <name>.html

# 2. 対象を探す（必ず絞る。全部出すと数百件になる）
python tools/camp_patch.py inspect --file <name>.html --section 3
python tools/camp_patch.py inspect --file <name>.html --find "軽作業"
python tools/camp_patch.py inspect --file <name>.html --images

# 3. 変更を積む
python tools/camp_patch.py set-style --file <name>.html --id ce_xxx --property margin-top --value 40px --important
python tools/camp_patch.py set-text  --file <name>.html --id ce_xxx --value "新しい文章"

# 4. 使える状態か確かめる（ここが ok:true になって初めて作業完了）
python tools/camp_patch.py validate --file <name>.html --live
```

## 前提と落とし穴

- **IDはユーザーが一度保存しないと存在しない。** `inspect` が「data-ceid が1つもありません」と言ったら、
  ユーザーにツールで開いて💾保存してもらう。それまで作業できない。
- **順番が決まっている。** パッチを作ったら、次に開くのはユーザー。
  ユーザーが先に保存すると `stale:true` になり、パッチは作り直しになる。
- **書けても効かないことがある。** クローン元CSSは `!important` の塊で、指定しても見た目が変わらない場合がある。
  ユーザーが開いた時に「◯件は元のCSSに負けて見た目が変わっていません」と出るので、その報告を見て別の方法を考える。
  （例：実測で `opacity` は効かなかった。ツール側が管理しているため）
- **文字色は `color` だけでは変わらないことがある。** `-webkit-text-fill-color` が別に指定されていると
  そちらが勝ち、「computed の color はピンクなのに画面は黒」になる（2026-08-02 実測）。
  → ツール側で `color` を指定すると自動で両方に当てるようにしてある。Codexは `color` を指定するだけでよい。
- **`set_text` は子要素が無い要素だけ。** 1文字ずつ span に割れた見出しには使えない（中身が壊れる）。
- **位置を動かすときは `set_style: transform` を使わない。** 専用の `set_transform_state` を使う
  （CSSだけ変えるとツールの内部状態とズレて、次のドラッグで位置が飛ぶ）。
- **画像はサイト内の相対URLだけ**（`/uploads/xxx.png`）。Windowsの絶対パスと外部URLは拒否される。

## 変更できるもの / できないもの

できる：位置・寸法・余白・文字（サイズ/行間/字間/色/整列）・背景色・枠線・角丸・影・透明度・
画像のsrc/alt/object-fit・alt/title/aria-*

できない：`position` `display` `z-index` などの広く影響するスタイル、`class` の付け外し、
リンク先、`on*` 属性、任意のHTML挿入、`data-ce*` の直接変更
