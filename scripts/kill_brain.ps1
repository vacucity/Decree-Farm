$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*brain.py*' }
if (-not $procs) { Write-Output 'NO_BRAIN_RUNNING'; exit 0 }
foreach ($p in $procs) {
    Write-Output ("killing PID " + $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Write-Output 'DONE'
