@echo off
chcp 65001 >nul
rem === GitHubから最新を取得（作業を始める前にこれ） ===
cd /d "%~dp0"
echo GitHubから最新を取得します（git pull）...
echo.
git pull origin main
echo.
echo 完了しました。何かキーを押すと閉じます。
pause >nul
