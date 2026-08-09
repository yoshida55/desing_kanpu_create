# 常駐（ホットキー）を止める。
# ★止めるのは quick_launcher だけ。作業中のサーバ（5000番）には触らない。
#   「venvのpythonを全部kill」は絶対にやらないこと（本番のサーバを巻き添えで落とすため）。
# ★-Filter で python 系だけに絞る理由：絞らないと、この命令を実行している PowerShell 自身の
#   コマンドラインにも "quick_launcher" の文字が入っているので、自分自身を止めてしまう。

$targets = @(
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*quick_launcher*' }
)

if ($targets.Count -gt 0) {
    $targets | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host ("常駐を停止しました（" + $targets.Count + "件）")
    Write-Host "※ 検索サーバ（5000番）はそのまま動いています。"
} else {
    Write-Host "常駐していませんでした。"
}
Start-Sleep -Seconds 2
