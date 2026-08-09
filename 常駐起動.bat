@echo off
rem === Start the resident hotkey launcher (design search) ===
rem  NOTE: Keep this file ASCII-only.
rem  Japanese text in a Shift-JIS .bat can break cmd parsing, because some
rem  characters have 0x5C / 0x5E as their 2nd byte (same as \ and ^).
rem  Japanese messages are shown by the tray notification instead.
rem
rem  Hotkey : Ctrl+Alt+S       -> show / hide the search window (default)
rem           Ctrl+Alt+Shift+D -> quit the resident launcher
rem  To change the key, add one line to .env :
rem      DESIGN_STOCK_HOTKEY=ctrl+shift+h
rem  If the key is taken by another app, an alternative is used automatically.
rem  The key actually in use is written to:
rem      data\quick_launcher.log

cd /d "%~dp0"

if not exist "venv\Scripts\pythonw.exe" (
  echo venv not found. Please set up the environment first. See README.
  pause
  exit /b 1
)

start "" "venv\Scripts\pythonw.exe" "tools\quick_launcher.py"
