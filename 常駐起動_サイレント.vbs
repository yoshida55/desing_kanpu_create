' デザインストック常駐起動（黒い窓なし・ブラウザも開かない）
' 家PC・会社PCどちらでも、下の候補から実在するフォルダを自動で選んで起動する。
' 会社PCのパスが違う場合は candidates の Array に1行追記するだけでOK。
Option Explicit
Dim fso, sh, base, p, candidates
candidates = Array( _
  "D:\99_AIソフト\86_デザインカンプ作成ツール_claude", _
  "C:\Users\guest04\Desktop\高橋研三\99_AIソフト\86_デザインカンプ作成ツール_claude", _
  "C:\Users\guest04\Desktop\高橋研三\86_デザインカンプ作成ツール_claude" )
Set fso = CreateObject("Scripting.FileSystemObject")
base = ""
For Each p In candidates
  If fso.FileExists(p & "\venv\Scripts\python.exe") Then
    base = p
    Exit For
  End If
Next
If base = "" Then WScript.Quit
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = base
sh.Run """" & base & "\venv\Scripts\python.exe"" cli.py serve --no-preload --no-open", 0, False
