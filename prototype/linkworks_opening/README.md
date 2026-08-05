# LinkWorks Opening Prototype

一人の人物が巨大な扉へ歩き、自分の手で開きます。カメラはその人物を肩越しに追い越し、左のITブース、右の梱包ライン、中央の発光樹、遠景の塔が重なる「仕事の宇宙港」へ入ったあと、通常のホームページへつながります。デザインカンプツールは使わず、Three.jsによる本物の3DとCSSフォールバックを組み合わせています。

## 確認方法

Three.jsのES Modulesを読み込むため、次のローカルサーバーで確認します。このフォルダで実行してください。

```powershell
python -m http.server 4173
```

確認URL：`http://localhost:4173/`

## 技術構成

- HTML：オープニングと通常ページの意味構造
- CSS：通常ページ、文字演出、WebGLが使えない場合の3D錯視フォールバック
- Three.js：人物の歩行と腕、扉、照明、カメラ移動、発光軌道、ITブース、梱包ライン、発光樹、粒子空間
- JavaScript：約5秒後の切り替え、スキップ、再生
- 外部フォント・画像：なし
- 外部ライブラリ：Three.js `0.185.1`のみ（CDN固定）

## なぜSplineを使っていないか

今回はカメラの通過と扉の蝶番をコードで細かく同期し、通常ページへの接続まで一つの時間軸で制御するため、Three.jsを選びました。Splineはデザイナーが画面上で3Dモデルを継続編集する完成版に向きますが、今回の試作では外部シーン管理と埋め込みが増えるため採用していません。

## WordPressへ移す場合

1. `index.html`内の`.intro`をテンプレートの`body`直下へ置く
2. 通常ページ部分はテーマの既存HTMLに置き換える
3. `styles.css`を`wp_enqueue_style()`、`script.js`を`wp_enqueue_script()`で読み込む
4. WordPress 6.5以降では、ES Modulesの`scene.js`を`wp_enqueue_script_module()`で読み込む
5. 毎回再生しない場合は、`sessionStorage`で「同一訪問中は1回だけ」に変更する

演出は通常ページの上に重なる独立レイヤーなので、既存テーマへの影響を限定できます。

```php
add_action( 'wp_enqueue_scripts', 'linkworks_enqueue_opening' );

function linkworks_enqueue_opening() {
  $theme_version = wp_get_theme()->get( 'Version' );

  wp_enqueue_style(
    'linkworks-opening',
    get_theme_file_uri( 'assets/linkworks/styles.css' ),
    array(),
    $theme_version
  );

  wp_enqueue_script(
    'linkworks-opening-control',
    get_theme_file_uri( 'assets/linkworks/script.js' ),
    array(),
    $theme_version,
    array( 'strategy' => 'defer', 'in_footer' => true )
  );

  wp_enqueue_script_module(
    'linkworks-opening-scene',
    get_theme_file_uri( 'assets/linkworks/scene.js' ),
    array(),
    $theme_version
  );
}
```

## 表示速度への対策

- テクスチャ画像と動画を使わず、単純な形状とマテリアルだけで構成
- 画面の描画密度を最大`1.5`に制限し、高解像度ディスプレイのGPU負荷を抑制
- ポストプロセスを使わず、単純な照明と粒子760個に限定
- オープニング終了後はThree.jsの描画ループを停止
- `prefers-reduced-motion`では3D演出を再生せず通常ページを表示
- Three.jsの読み込み失敗時も、HTML/CSS版の扉演出で続行

プロトタイプはCDNを使っていますが、本番ではThree.jsをテーマ内へビルドして配置すると、外部CDN障害の影響を避けられます。
