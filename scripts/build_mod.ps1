$env:GAME_PATH = 'C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley'
Set-Location 'd:\AdventureX2026\vendor\smapi-mod'
Write-Output '=== dotnet build (Release) ==='
dotnet build -c Release 2>&1
Write-Output ('=== EXITCODE=' + $LASTEXITCODE + ' ===')
