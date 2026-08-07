$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root
$Version = (Get-Content (Join-Path $Root "VERSION") -Raw).Trim()
$Python = (Get-Command python).Source

Write-Host "Building RockCore $Version for Windows x64"
& $Python scripts/make_brand_assets.py
& $Python build/make_version_info.py

if (Test-Path dist) { Remove-Item dist -Recurse -Force }
if (Test-Path build/pyinstaller) { Remove-Item build/pyinstaller -Recurse -Force }
if (Test-Path release) { Remove-Item release -Recurse -Force }
New-Item -ItemType Directory -Force release | Out-Null

& $Python -m PyInstaller --noconfirm --clean --distpath dist --workpath build/pyinstaller build/RockCore.spec

$Portable = Join-Path $Root "release/RockCore-$Version-Windows-x64-portable.zip"
Compress-Archive -Path (Join-Path $Root "dist/RockCore/*") -DestinationPath $Portable -Force

if (Get-Command iscc -ErrorAction SilentlyContinue) {
    & iscc "/DAppVersion=$Version" installer/RockCore.iss
} else {
    Write-Warning "Inno Setup is not installed; portable ZIP was created, installer skipped."
}

Get-ChildItem release -File | ForEach-Object {
    $Hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$Hash  $($_.Name)"
} | Set-Content release/SHA256SUMS.txt -Encoding ascii

Write-Host "Build output: $((Resolve-Path release).Path)"
