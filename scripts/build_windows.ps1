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
$MinGitVersion = "2.55.0.4"
$MinGitArchive = "MinGit-$MinGitVersion-64-bit.zip"
$MinGitUrl = "https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.4/$MinGitArchive"
$MinGitSha256 = "4e03f94c2ffbf70be337e005cee02661c732dbfc81031a078bda9299b9a7d644"
$VendorRoot = Join-Path $Root "build/vendor"
$MinGitRoot = Join-Path $VendorRoot "mingit"
$MinGitZip = Join-Path $VendorRoot $MinGitArchive

Write-Host "Building RockCore $Version for Windows x64"
Invoke-Native "Generate branding assets" $Python @("scripts/make_brand_assets.py")
Invoke-Native "Generate version metadata" $Python @("build/make_version_info.py")

Write-Host "==> Prepare verified bundled MinGit $MinGitVersion"
New-Item -ItemType Directory -Force $VendorRoot | Out-Null
if (-not (Test-Path $MinGitZip -PathType Leaf) -or `
    (Get-FileHash $MinGitZip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $MinGitSha256) {
    if (Test-Path $MinGitZip) { Remove-Item $MinGitZip -Force }
    Invoke-WebRequest -Uri $MinGitUrl -OutFile $MinGitZip
}
$DownloadedHash = (Get-FileHash $MinGitZip -Algorithm SHA256).Hash.ToLowerInvariant()
if ($DownloadedHash -ne $MinGitSha256) {
    throw "MinGit checksum mismatch: expected $MinGitSha256, got $DownloadedHash"
}
if (Test-Path $MinGitRoot) { Remove-Item $MinGitRoot -Recurse -Force }
New-Item -ItemType Directory -Force $MinGitRoot | Out-Null
Expand-Archive -Path $MinGitZip -DestinationPath $MinGitRoot -Force
$BundledGit = Join-Path $MinGitRoot "cmd/git.exe"
if (-not (Test-Path $BundledGit -PathType Leaf)) {
    throw "MinGit archive did not contain cmd/git.exe"
}
Invoke-Native "Verify downloaded MinGit" $BundledGit @("--version")

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
$PackagedGit = Join-Path $AppDir "_internal/runtime/git/cmd/git.exe"
if (-not (Test-Path $PackagedGit -PathType Leaf)) {
    throw "PyInstaller completed but bundled Git was not included: $PackagedGit"
}
Invoke-Native "Verify packaged MinGit" $PackagedGit @("--version")

Write-Host "==> Run packaged startup smoke test"
$PreviousQtPlatform = $env:QT_QPA_PLATFORM
$PreviousPath = $env:PATH
$env:QT_QPA_PLATFORM = "offscreen"
try {
    # Prove the app does not accidentally rely on Git installed on the runner.
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $Smoke = Start-Process -FilePath $AppExe `
        -ArgumentList "--startup-smoke-test" -Wait -PassThru
    if ($Smoke.ExitCode -ne 0) {
        throw "Packaged startup smoke test failed with exit code $($Smoke.ExitCode)"
    }
    $PythonSmoke = Start-Process -FilePath $AppExe `
        -ArgumentList "--python-validation-smoke-test" -Wait -PassThru
    if ($PythonSmoke.ExitCode -ne 0) {
        throw "Packaged Python validation smoke test failed with exit code $($PythonSmoke.ExitCode)"
    }
} finally {
    $env:QT_QPA_PLATFORM = $PreviousQtPlatform
    $env:PATH = $PreviousPath
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
