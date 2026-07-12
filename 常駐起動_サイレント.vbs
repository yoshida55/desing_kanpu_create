' デザインストック常駐起動（黒い窓なし・ブラウザも開かない）
' Windowsスタートアップに置くとPC起動時に自動でサーバーが立ち上がり、
' Chrome拡張の右クリック保存がいつでも使える。
' 手動で画面を見たいときは今までどおり 起動.bat（こちらはブラウザが開く）。
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "D:\99_AIソフト\86_デザインカンプ作成ツール_claude"
sh.Run """D:\99_AIソフト\86_デザインカンプ作成ツール_claude\venv\Scripts\python.exe"" cli.py serve --no-preload --no-open", 0, False
