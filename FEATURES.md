# FEATURES — このツールで「できること」の全一覧

> ⚠ **このファイルは自動生成です。手で書き換えないでください。**
> 生成: `python tools/gen_features.py` ／ 元ネタ: `src/viewer.py` と `src/*.py`
> 機能を足すと、次に生成し直した時点でここに自動で載ります。

読む人へ：`CLAUDE.md` は**設計の約束と落とし穴**、`docs/実装履歴.md` は**なぜそう作ったか・試してダメだった案**の記録で、
どちらも機能一覧ではありません。「何ができるか」を知りたいならこのファイルだけで足ります。

規模: 右クリック 35項目 / 編集バー 33項目 / APIパス 93本 / モジュール 30本

💰=AIを呼ぶ（お金がかかる） ／ 無料=AIを使わない。ほとんどの操作は無料です。

---

## 1. 右クリックメニュー（要素を選んでその場で直す）

カンプ上で要素を右クリックすると出るメニュー。ラベルは実際の画面表示そのまま。

| 機能 | ID | AI課金 |
|---|---|---|
| ⬆ 外側を選ぶ（枠ごと動かす） | `__ce_q_up` | 無料 |
| ✏ 文字を追加（編集） | `__ce_q_txt` | 無料 |
| 🖼 画像を追加（ここに置く） | `__ce_q_img` | 無料 |
| 🖼 写真を加工（差し替え・大きさ・フチ・背景など） | `__ce_q_photo` | 無料 |
| 🖼 スライドショー（画像が次々切り替わる） | `__ce_q_slide` | 無料 |
| ✨ 動きを付ける（アニメを選ぶ） | `__ce_q_fx` | 無料 |
| 🕊 線を描いて飛ばす（空飛ぶルート） | `__ce_q_fly` | 無料 |
| ⏳ 動きの演出（順番・遅らせ・速さ） | `__ce_q_dly` | 無料 |
| 📚 お手本を見る（ベース・似た例・アドバイス） | `__ce_q_ref` | 無料 |
| 🧐 デザイン指摘をもらう（プロの目・AI数円） | `__ce_q_dcq` | 💰 |
| 🌙 自動磨き（指摘→修正を自動で数周・AI課金） | `__ce_q_brush` | 💰 |
| 🧩 セクション（保存・追加・入れ替え・削除・背景色・境目） | `__ce_q_sec` | 無料 |
| 🔀 このセクションの形を変える（実サイトの候補を次々・AIなし） | `__ce_q_secswap` | 無料 |
| 🎯 重なっている要素から選ぶ（下の層） | `__ce_q_pickov` | 無料 |
| 🗑 この要素を削除 | `__ce_q_del` | 無料 |
| 🔼 重なり・食い込みを調整（手前/後ろ・はみ出し） | `__ce_q_stack` | 無料 |
| 💬 がやがや演出（にぎやか吹き出しループ） | `__ce_q_gaya` | 無料 |
| ⇔ 横のズレを直す（右の余りを詰める） | `__ce_q_hfix` | 無料 |
| ⁝ 兄弟の行をそろえる（ズレ掃除） | `__ce_q_align` | 無料 |
| 🧲 掴む枠を見た目の位置に合わせる | `__ce_q_frmfit` | 無料 |
| ➖ 線・飾りを消す（border・疑似要素） | `__ce_q_pskill` | 無料 |
| ➕ 線を追加（実要素・掴んで動かせる） | `__ce_q_addline` | 無料 |
| ✏ この文字を編集（改行や文言を直す） | `__ce_q_edit` | 無料 |
| 📤 ヘッダーの中の大きな中身を外に出す（位置はそのまま） | `__ce_q_heroout` | 無料 |
| 🚫 動きを消す | `__ce_q_fxrm` | 無料 |
| ⟲ 位置・サイズをリセット | `__ce_q_rst` | 無料 |
| 📌 画面への貼り付きを解除（一緒にスクロール） | `__ce_q_unfix` | 無料 |
| 📌 スクロールしても画面に貼り付ける（固定ヘッダー等） | `__ce_q_pin` | 無料 |
| 🏞 背景を留めて、次のセクションを上に通す（AIなし） | `__ce_q_overpass` | 無料 |
| 🏞 このセクションの下に写真の帯を足す（留められる背景・AIなし） | `__ce_q_photoband` | 無料 |
| 🏞 帯の中身を選ぶ（文字のレイアウト・AIなし） | `__ce_q_bandpat` | 無料 |
| 🎨 この飾りの色・形・傾きを変える | `__ce_q_deco` | 無料 |
| 🔓 数字や飾り（01など）を掴めるようにする | `__ce_q_psgrab` | 無料 |
| 📌 パレットに入れる（コピー／切り取りは板で選ぶ） | `__ce_q_pal` | 無料 |
| 🧹 囲んだ範囲の余白を詰める | `__ce_q_gapfix` | 無料 |

## 2. 編集バー（画面右上・ページ全体に効く操作）

| 機能 | ID | AI課金 |
|---|---|---|
| 🏠 ツール（ホーム）に戻る | `__ce_home` | 無料 |
| ⟲ 元の色に戻す | `__ce_baser` | 無料 |
| 🚫 背景の飾り（わっか等）を消す | `__ce_nodeco` | 無料 |
| 🧹 余白・見出しを一律に揃える | `__ce_normalize` | 無料 |
| 📍 動かした跡を一覧で見る | `__ce_unshift` | 無料 |
| 🎨 全ボタンをテーマ色に統一 | `__ce_btncolor` | 無料 |
| 🎨 ヘッダー・文字・背景の色を置き換える（Shift+ダブルクリックでも開く） | `__ce_colrepbtn` | 無料 |
| ➖ 全セクションの先頭に区切り線を入れる | `__ce_divline` | 無料 |
| 📷 見本画像を付ける（構図を寄せる） | `__ce_refimg_btn` | 無料 |
| 📐 横幅をそろえる（AIなし・無料） | `__ce_align` | 無料 |
| 🚀 ページ全体を今風に（一括改善） | `__ce_improve` | 無料 |
| 🔶 図形バーを出す（画面の下） | `__ce_shapes` | 無料 |
| 🎬 フェードのオープニングを付ける | `__ce_op_add` | 無料 |
| 👁 オープニングを出す／隠す（▶で本番の動きを確認・文字は右クリックで差し替え） | `__ce_op_edit` | 無料 |
| 🗑 オープニングを外す（もう出さない） | `__ce_op_del` | 無料 |
| 🔀 お気に入りからセクションを切り替え | `__ce_favlist` | 無料 |
| ➕ お気に入りからセクションを追加（場所を選ぶ） | `__ce_favadd` | 無料 |
| 🧢 ヘッダーの種類を選ぶ（標準6種＋⭐保存分） | `__ce_hdpick` | 無料 |
| 🦶 フッターの種類を選ぶ（⭐保存分） | `__ce_ftpick` | 無料 |
| 🎨 おしゃれ度をチェック | `__ce_stylecheck` | 無料 |
| 🎯 チェックして自動で磨く（採点→改善を一括・AI） | `__ce_autopolish` | 💰 |
| 📦 分割エクスポート（zipで保存） | `__ce_export` | 無料 |
| 📐 仕様書を作る（コーディング担当に渡す用） | `__ce_spec` | 無料 |
| 📱 レスポンシブ検査（スマホ/タブレットで崩れないか） | `__ce_resp` | 無料 |
| 📱 スマホ版を作る（おおよそ変換・AIなし） | `__ce_sp` | 無料 |
| 🔍 インスペクト（コーダーに数値を渡す） | `__ce_insp` | 無料 |
| 🎬 アニメ実装キット（動きをコードで渡す） | `__ce_kit` | 無料 |
| 📦 本番化キット（AIに本番コードを書かせる下ごしらえ） | `__ce_prod` | 無料 |
| 🎨 Figma用に書き出す（取り込んでデザイン化） | `__ce_figma` | 無料 |
| 🔃 セクション並べ替え（順番を入れ替える） | `__ce_secswap` | 無料 |
| 🧹 大掃除（分割span・残骸を消してソースを軽く） | `__ce_bigclean` | 無料 |
| 🗂 バックアップを取る（今の保存状態を複製） | `__ce_bk` | 無料 |
| 🛑 アニメを全部止める | `__ce_stopanim` | 無料 |

## 3. モジュール構成（どのファイルが何をしているか）

| ファイル | 役割 |
|---|---|
| `src/anim.py` | アニメーションの抜き出し（Feature：登録サイトから再利用できるアニメ素材を集める）。 |
| `src/animkit.py` | 🎬 アニメ実装キットの書き出し（カンプ → コーダーがそのまま使える汎用コード）。 |
| `src/assets.py` | 画像の抜き出し（Feature：登録サイトから実画像を集める）。 |
| `src/bgremove.py` | アップロード画像の背景を除去して透過PNGにする（onnxruntime + U2-Net・ローカル・無料）。 |
| `src/camp.py` | カンプ生成（仕様 4.7 / Phase 4 ＝ 最終目標）。 |
| `src/camp_patch.py` | Codex 用パッチの読み書きと検証（共同編集 Phase 2）。 |
| `src/clone.py` | 実サイトの忠実クローン（DOMスナップショット方式）。 |
| `src/config.py` | 設定値を1か所に集約するモジュール。 |
| `src/db.py` | SQLite アクセス層。仕様 4.2 の `site` テーブルをそのまま実装する。 |
| `src/embed.py` | 埋め込み（embed）パイプライン。仕様 4.3 を実装する。 |
| `src/export_split.py` | 納品用の「分割エクスポート」（HTML / CSS / JS を別ファイルに切り出し＋画像ローカル化）。 |
| `src/figmaimport.py` | 🎨 Figma → カンプHTML の取り込み（REST API・AIなし＝無料）。 |
| `src/figmakit.py` | 🎨 Figma取り込み用の書き出し（AIなし＝無料・一瞬）。 |
| `src/ingest.py` | 取り込み（ingest）。仕様 4.1 を実装する。 |
| `src/model.py` | SigLIP-2 のラッパー。仕様 4.3 の方針： |
| `src/motion.py` | 録画（スクロール動画）から「動きの仕様書」をAIに書かせる（Phase 4 / mix & match B案）。 |
| `src/pricing.py` | AI料金の実費記録と見積もり（🌙自動磨きなどの「いくらかかる？」の頭脳）。 |
| `src/prodkit.py` | 📦 本番化キット書き出し（AIなし＝無料・一瞬）。 |
| `src/quality.py` | 生成カンプの仕上がりチェック（薄い出力＝ハズレ回アラート）。 |
| `src/recipes.py` | 業種別デザインレシピ。 |
| `src/respcheck.py` | レスポンシブ自動監査（カンプHTML → 3画面幅の実測レポート）。 |
| `src/search.py` | 検索（search）。仕様 4.4 / 4.5 を実装する。 |
| `src/skeleton.py` | セクションの「骨格」を実測して型（レイアウトの種類）に正規化する。 |
| `src/sp_convert.py` | スマホ版のおおよそ自動変換（カンプHTML → SP用 @media を注入した1ファイル）。 |
| `src/spec.py` | コーディング仕様書の生成（カンプHTML → 寸法・色・フォント・動き入りの1枚HTML）。 |
| `src/style_check.py` | おしゃれ度チェック（納品前の最終QC）。 |
| `src/tokens.py` | デザイントークン抽出（仕様 Phase 3 / カンプ生成の"効き"を強くする）。 |
| `src/utils.py` | 小さな共有ユーティリティ。 |
| `src/vibe.py` | 雰囲気描写文ハイブリッド（仕様 Phase 3）。 |
| `src/viewer.py` | （説明なし＝docstringを1行足すとここに出ます） |

## 4. APIエンドポイント

<details><summary>全93本（クリックで展開）</summary>

- `/`
- `/anim/<site_id>/<path:filename>`
- `/api/anim_kit`
- `/api/auto_brushup`
- `/api/base_stats`
- `/api/base_stats_all`
- `/api/brush_apply`
- `/api/brushup_estimate`
- `/api/camp_backup`
- `/api/camp_delete`
- `/api/camp_fav`
- `/api/camp_name`
- `/api/camp_rate`
- `/api/camp_sections`
- `/api/camp_suggest`
- `/api/camps`
- `/api/clone_site`
- `/api/clone_site/status`
- `/api/delete`
- `/api/describe`
- `/api/describe_all`
- `/api/design_critique`
- `/api/edit_camp`
- `/api/edit_element`
- `/api/export_split`
- `/api/extract_anim`
- `/api/extract_images`
- `/api/figma_import`
- `/api/figma_kit`
- `/api/generate_camp`
- `/api/generate_camp/status`
- `/api/import_folder`
- `/api/improve_camp`
- `/api/improve_camp/status`
- `/api/make_spec`
- `/api/make_spec/status`
- `/api/menu_layout`
- `/api/menu_search`
- `/api/pair_fit`
- `/api/pair_fit_all`
- `/api/paste_image`
- `/api/paste_to_section`
- `/api/prod_kit`
- `/api/read_motion`
- `/api/record_animation`
- `/api/register`
- `/api/register/status`
- `/api/remove_bg`
- `/api/remove_bg_url`
- `/api/resp_check`
- `/api/resp_check/status`
- `/api/save_camp_html`
- `/api/save_favorite`
- `/api/save_spec_html`
- `/api/search`
- `/api/sec_cands`
- `/api/sec_part`
- `/api/sec_thumb/<path:name>`
- `/api/section_advice`
- `/api/section_fav/delete`
- `/api/section_fav/list`
- `/api/section_fav/save`
- `/api/settings`
- `/api/shape_ai`
- `/api/shape_lib`
- `/api/shape_lib_add`
- `/api/shape_lib_del`
- `/api/shortcuts`
- `/api/similar`
- `/api/site/<site_id>`
- `/api/sites`
- `/api/sp_convert`
- `/api/sp_convert/status`
- `/api/style_check`
- `/api/swap_image`
- `/api/test_key`
- `/api/upload`
- `/api/upload_delete`
- `/api/uploads`
- `/assets/<site_id>/<path:filename>`
- `/camp/<path:filename>`
- `/camp_figma/<path:filename>`
- `/camp_preview/<path:filename>`
- `/check/<path:filename>`
- `/compare`
- `/exports/<path:filename>`
- `/figma/<path:sub>`
- `/img/<site_id>/<which>`
- `/kit/<path:filename>`
- `/sp/<path:filename>`
- `/spec/<path:filename>`
- `/uploads/<path:filename>`
- `/video/<site_id>`

</details>

