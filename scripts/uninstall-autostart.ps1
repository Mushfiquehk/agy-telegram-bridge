$taskName = "agy-telegram-bridge"
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*agy_bridge*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host "Removed '$taskName'."
