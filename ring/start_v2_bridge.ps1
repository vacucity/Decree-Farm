param(
    [string]$Mac = "DA:2A:F8:9B:FE:44",
    [int]$Port = 8520,
    [float]$CommandTimeout = 20,
    [switch]$NoAutoConnect
)

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = "C:\Users\23017\anaconda3\python.exe"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pythonExe = (Get-Command python -ErrorAction Stop).Source
}

Set-Location -LiteralPath $projectRoot
Write-Host "Ring Sound SDK V2 bridge"
Write-Host "SDK: 0.4.1 | MAC: $Mac | Web: http://127.0.0.1:$Port"
$bridgeArgs = @(
    "-m", "ring_bridge.server",
    "--mac", $Mac,
    "--port", [string]$Port,
    "--timeout", [string]$CommandTimeout
)
if ($NoAutoConnect) {
    $bridgeArgs += "--no-auto-connect"
}
& $pythonExe @bridgeArgs
