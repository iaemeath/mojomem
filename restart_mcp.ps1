$processes = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'mcp_server.py|codebase-memory-mcp' }
foreach ($p in $processes) {
    Write-Host "Killing Process ID: $($p.ProcessId) - $($p.CommandLine)"
    Stop-Process -Id $p.ProcessId -Force
}
Write-Host "Done."
