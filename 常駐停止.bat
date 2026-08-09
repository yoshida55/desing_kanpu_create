@echo off
rem === Stop the resident hotkey launcher ===
rem  Keep this file ASCII-only (see the note in the launcher .bat).
rem  Japanese messages are printed by tools\quick_stop.ps1.

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\quick_stop.ps1"
