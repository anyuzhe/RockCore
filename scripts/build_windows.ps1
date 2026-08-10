$ErrorActionPreference = "Stop"

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )

    Write-Host "==> $Label"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root
$Version = (Get-Content (Join-Path $Root "VERSION") -Raw).Trim()
$Python = (Get-Command python).Source

Write-Host "Building RockCore $Version for Windows x64"
Invoke-Native "Generate branding assets" $Python @("scripts/make_brand_assets.py")
Invoke-Native "Generate version metadata" $Python @("build/make_version_info.py")

if (Test-Path dist) { Remove-Item dist -Recurse -Force }
if (Test-Path build/pyinstaller) { Remove-Item build/pyinstaller -Recurse -Force }
if (Test-Path release) { Remove-Item release -Recurse -Force }
New-Item -ItemType Directory -Force release | Out-Null

Invoke-Native "Run PyInstaller" $Python @(
    "-m", "PyInstaller", "--noconfirm", "--clean",
    "--distpath", "dist", "--workpath", "build/pyinstaller",
    "build/RockCore.spec"
)

$AppDir = Join-Path $Root "dist/RockCore"
$AppExe = Join-Path $AppDir "RockCore.exe"
if (-not (Test-Path $AppExe -PathType Leaf)) {
    throw "PyInstaller completed but expected executable was not created: $AppExe"
}

Write-Host "==> Run packaged startup smoke test"
$PreviousQtPlatform = $env:QT_QPA_PLATFORM
$env:QT_QPA_PLATFORM = "offscreen"
try {
    $Smoke = Start-Process -FilePath $AppExe `
        -ArgumentList "--startup-smoke-test" -Wait -PassThru
    if ($Smoke.ExitCode -ne 0) {
        throw "Packaged startup smoke test failed with exit code $($Smoke.ExitCode)"
    }
} finally {
    $env:QT_QPA_PLATFORM = $PreviousQtPlatform
}

$Portable = Join-Path $Root "release/RockCore-$Version-Windows-x64-portable.zip"
Compress-Archive -Path (Join-Path $AppDir "*") -DestinationPath $Portable -Force
if (-not (Test-Path $Portable -PathType Leaf)) {
    throw "Portable package was not created: $Portable"
}

$Iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if ($Iscc) {
    Invoke-Native "Build Inno Setup installer" $Iscc.Source @(
        "/DAppVersion=$Version", "installer/RockCore.iss"
    )
} else {
    Write-Warning "Inno Setup is not installed; portable ZIP was created, installer skipped."
}

Get-ChildItem release -File | ForEach-Object {
    $Hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$Hash  $($_.Name)"
} | Set-Content release/SHA256SUMS.txt -Encoding ascii

Write-Host "Build output: $((Resolve-Path release).Path)"
