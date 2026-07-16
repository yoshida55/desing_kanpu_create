@echo off
rem === デザインカンプ作成ツール 起動（開発モード） ===
rem .py を保存すると自動でサーバが再起動する＝修正のたびに手で起動し直す必要なし。
rem HTMLの修正はブラウザのF5だけで反映（再起動すら不要）。

cd /d "%~dp0"

rem 既に5000番で動いている古いサーバーを止める（ゾンビ対策・二重起動防止）
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1

if not exist "venv\Scripts\python.exe" (
  echo venv が見つかりません。先に環境構築をしてください（README参照）。
  pause
  exit /b 1
)

echo 開発モードで起動します... http://127.0.0.1:5000
echo （.py を保存すると自動で再起動します。この黒い画面は閉じないでください）
venv\Scripts\python.exe cli.py serve --no-preload --dev

pause
