@echo off
chcp 65001 >nul
rem === 変更をGitHubに上げる（作業が終わったらこれ） ===
cd /d "%~dp0"

rem コミット者の名前/メールが未設定なら入れておく（どのPCでも commit できるように）
git config user.name  >nul 2>&1 || git config user.name  "yoshida55"
git config user.email >nul 2>&1 || git config user.email "yoshida55@users.noreply.github.com"

echo まず最新を取得（git pull）...
git pull origin main
echo.

echo 変更をコミットしてプッシュします。
git add -A
set /p msg="コミットメッセージ（空Enterで日時にします）: "
if "%msg%"=="" set msg=更新 %date% %time%
git commit -m "%msg%"
git push origin main
echo.
echo 完了しました。何かキーを押すと閉じます。
pause >nul
