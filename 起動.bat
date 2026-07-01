@echo off
rem === デザイン参照ストック＆カンプ生成ツール 起動 ===
rem このバッチをダブルクリックすると、サーバが起動し、正しいURLでブラウザが開きます。
rem （HTMLファイルを直接ダブルクリックすると file:// になりボタンが動かないので注意）

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
  echo venv が見つかりません。先に環境構築をしてください（README参照）。
  pause
  exit /b 1
)

echo ツールを起動します... ブラウザで http://127.0.0.1:5000 が開きます。
echo （この黒い画面は閉じないでください。閉じるとサーバも止まります）
rem --no-preload: メモリの厳しいPCでも起動できるよう、モデルは初回検索時に読む
venv\Scripts\python.exe cli.py serve --no-preload

pause
