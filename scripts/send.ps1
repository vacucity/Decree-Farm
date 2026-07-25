param(
  [Parameter(Mandatory=$true)][string]$Action,
  [Nullable[int]]$X,
  [Nullable[int]]$Y,
  [string]$Mode = '',
  [string]$Target = '',
  [string]$Companion = '',
  [string]$Tool = '',
  [string]$Location = '',
  [Nullable[int]]$Direction,
  [string]$Message = ''
)
$actions = 'C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley\Mods\StardewMCPBridge\actions'
New-Item -ItemType Directory -Force -Path $actions | Out-Null

$obj = [ordered]@{ actionType = $Action }
if ($Target -ne '')    { $obj.target = $Target }
if ($Mode -ne '')      { $obj.mode = $Mode }
if ($Companion -ne '') { $obj.companion = $Companion }
if ($Location -ne '')  { $obj.location = $Location }
if ($Tool -ne '')      { $obj.tool = $Tool }
if ($null -ne $X)      { $obj.x = $X }
if ($null -ne $Y)      { $obj.y = $Y }
if ($null -ne $Direction) { $obj.direction = $Direction }
if ($Message -ne '')   { $obj.metadata = @{ message = $Message } }

$json = $obj | ConvertTo-Json -Compress
$ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$fname = "{0}-{1}.json" -f $ms, (Get-Random -Minimum 100000 -Maximum 999999)
$final = Join-Path $actions $fname
$tmp = "$final.tmp"
Set-Content -LiteralPath $tmp -Value $json -NoNewline -Encoding UTF8
Rename-Item -LiteralPath $tmp -NewName $fname
Write-Output ("SENT " + $fname + " -> " + $json)
