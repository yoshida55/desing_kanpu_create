# PCを起動したら自動で常駐するように、スタートアップへショートカットを置く／外す。
#   登録: powershell -File tools\register_startup.ps1
#   解除: powershell -File tools\register_startup.ps1 -Remove
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup 'デザイン検索_常駐.lnk'

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link -Force; Write-Host "解除しました: $link" }
    else { Write-Host '登録されていませんでした' }
    return
}

$pythonw = Join-Path $root 'venv\Scripts\pythonw.exe'
$script = Join-Path $root 'tools\quick_launcher.py'
if (-not (Test-Path $pythonw)) { Write-Host "venv が見つかりません: $pythonw"; return }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $pythonw
$sc.Arguments = '"' + $script + '"'
$sc.WorkingDirectory = $root
$sc.WindowStyle = 7          # 最小化で起動（pythonw なので実際は何も出ない）
$sc.Description = 'デザイン検索を常駐させる（Ctrl+Alt+D）'
$sc.Save()
Write-Host "登録しました: $link"
Write-Host '次にPCを起動したときから、自動で常駐します。'
