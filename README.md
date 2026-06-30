# デザイン参照ストック & 曖昧検索システム（Phase 1）

良いと感じたWebサイトの「見た目・雰囲気」を集めて、**言葉や画像で曖昧検索**できる土台。
いわば「デザイン版のRAG」。Phase 1 は **取り込み → 埋め込み → 静的HTML検索** までを実装している。

```
URL ─▶ [ingest] 撮影(firstview/fullpage) ─▶ [embed] SigLIP-2でベクトル化 ─▶ [search] 言葉/画像で検索 → results.html
```

---

## 1. できること（コマンド一覧）

| コマンド | 役割 |
|----------|------|
| `python cli.py ingest <URL...>` | URLを撮影してDBに保存（firstview + fullpage） |
| `python cli.py ingest --list urls.txt` | リストファイルから一括取り込み |
| `python cli.py embed` | 撮影済みを SigLIP-2 で埋め込み（未処理だけ） |
| `python cli.py search "落ち着いた青系のサイト"` | 言葉で曖昧検索（text→image） |
| `python cli.py search --image path/to.png` | 画像で似たもの検索（image→image） |
| `python cli.py serve` | **検索ボックス付きビューアを起動**（モデル常駐・反復検索が一瞬） |
| `python cli.py status` | 登録件数 / 埋め込み済み件数を表示 |

`--force` を付けると、ingest は撮り直し・embed は全件再埋め込みになる。

---

## 2. セットアップ（重いDLは初回のみ）

> ⚠ コードは用意済み。下記は **実行環境を作る手順**。
> SigLIP-2モデル（約3.5GB）と Playwright のブラウザは初回だけDLが走る。

### 2-1. 仮想環境（venv）を作る

```bash
cd "d:/99_AIソフト/86_デザインカンプ作成ツール_claude"
python -m venv venv
source venv/Scripts/activate    # Git Bash の場合（PowerShellは venv\Scripts\Activate.ps1）
```

### 2-2. PyTorch を入れる（GPU/CPUで分岐）

**GPUあり（RTX 3070Ti / CUDA 12.x）:**
```bash
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

**CPUのみ:**
```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

### 2-3. 残りの依存 + Playwright ブラウザ

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

---

## 3. 使い方（最短ルート）

```bash
# ① 取り込み（1件 or リスト）
python cli.py ingest https://stripe.com/jp
python cli.py ingest --list urls.sample.txt

# ② 埋め込み（初回はモデルDLで数分）
python cli.py embed

# ③-A 1回だけ検索（results.html が開く）
python cli.py search "上品で余白の広い、明るいトーンのサイト"

# ③-B 何度も検索したいとき → ビューア起動（おすすめ）
python cli.py serve
#   → http://127.0.0.1:5000 が開く。検索ボックスに言葉を入れて Enter。
#     モデルが常駐しているので2回目以降は一瞬。Ctrl+C で停止。
```

検索結果カードの画像を押すと **実物のサイトへ飛ぶ** ので、動き・アニメはそこで確認する。
カードの「似てるのを探す」を押すと、そのサイトに近いものを芋づる式にたどれる（image→image）。

---

## 4. 設定の変え方（ハードコードしない方針）

`src/config.py` で集中管理。環境変数でも上書きできる。

| 変えたいもの | 方法 |
|--------------|------|
| 埋め込みモデル | `DESIGN_STOCK_MODEL=google/siglip2-base-patch16-384`（軽量版など） |
| 実行デバイス | `DESIGN_STOCK_DEVICE=cuda` / `cpu`（既定は `auto`） |
| 撮影viewport / UA | `src/config.py` の `CaptureConfig` |
| 検索の表示件数 | `search --top 12` または `SearchConfig.top_n` |

次元数（SigLIP2-SO400M は1152）は**実測してDBに記録**するので、モデルを差し替えても破綻しない。

---

## 5. データの中身

```
data/
├─ design_stock.sqlite      … site テーブル（仕様4.2）
└─ screenshots/             … firstview / fullpage の元画像（再埋め込み用に必ず保存）
results.html                … 直近の検索結果（毎回上書き）
```

- **元画像は必ず残す** → モデルを変えても何度でも再埋め込みできる。
- `embed_model_name` / `embed_version` / `embed_dim` を持つ → 古いレコードだけ再処理できる。

---

## 6. Phase 1 のゴール（検証の進め方）

このフェーズの目的は見た目ではなく **「SigLIP-2がちゃんと効くか測れること」**。

1. 検証前に「このクエリを打ったら、このサイトが上位に来てほしい」を **5〜10個メモ** しておく
2. 3観点を別々に見る：①色・トーン ②抽象的な雰囲気（高級感等）③image→imageの納得感
3. クエリやモデルを変えたとき「良くなったか」を **差分で判定** できる状態を作る

---

## 7. 構成

```
cli.py              … CLI入口（ingest / embed / search / status）
src/
├─ config.py        … 設定の集中管理（モデルID・撮影条件・パス）
├─ db.py            … SQLite（site テーブル / 仕様4.2）
├─ ingest.py        … Playwright撮影（firstview + fullpage / 重複排除）
├─ model.py         … SigLIP-2 ラッパー（画像/テキスト→L2正規化ベクトル・次元実測）
├─ embed.py         … 埋め込みパイプライン（未処理だけ / --forceで全件）
├─ search.py        … text→image / image→image / results.html 出力
└─ utils.py         … ハッシュID・URL正規化・BLOB変換・ログ
```

---

## 8. 既知の割り切り（仕様より）

- 同意バナーの自動クローズは完璧ではない（文言が合わなければ `src/ingest.py` の `_CONSENT_TEXTS` に追加）
- headlessをブロックするサイトがある（`CaptureConfig.headless = False` で回避を試す）
- 抽象的な雰囲気はSigLIP単体だと弱い → Phase 3 の「雰囲気描写文の埋め込み」で補う予定
- 件数が万単位になったら numpy総当たり → sqlite-vec へ（保存と検索を分離してあるので差し替え可能）
