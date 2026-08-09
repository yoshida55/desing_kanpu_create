@echo off
rem === Register the resident launcher to Windows startup (run once) ===
rem  Keep this file ASCII-only (see the note in the launcher .bat).
rem  To remove it later: Win+R -> shell:startup -> delete the shortcut.

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\register_startup.ps1"
pause
