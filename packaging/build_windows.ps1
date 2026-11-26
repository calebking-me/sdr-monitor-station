<#
.SYNOPSIS
    Build SDRScanner.exe with PyInstaller.

.DESCRIPTION
    Creates a clean virtual environment (.venv-build), installs the app's
    dependencies plus PyInstaller, builds dist\SDRScanner\SDRScanner.exe,
    runs its self-test and zips the result as dist\SDRScanner-<version>-win64.zip.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
    powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 -OneFile
#>
param(
    [switch]$OneFile,       # single self-extracting .exe instead of a folder
    [switch]$SkipSelfTest,
    [string]$Python = "py"  # Python launcher or path to python.exe (3.10+)
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

$venv = Join-Path $root ".venv-build"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "Creating build environment in $venv"
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create virtual environment with '$Python'" }
}
$py = Join-Path $venv "Scripts\python.exe"

Write-Host "Installing dependencies"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements.txt "pyinstaller>=6.3"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$version = (& $py -c "import sdrscanner; print(sdrscanner.__version__)").Trim()
Write-Host "Building SDR Scanner $version"

if ($OneFile) { $env:SDRSCANNER_ONEFILE = "1" } else { Remove-Item Env:SDRSCANNER_ONEFILE -ErrorAction SilentlyContinue }
& $py -m PyInstaller packaging\sdrscanner.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

if ($OneFile) {
    $exe = Join-Path $root "dist\SDRScanner.exe"
    $payload = $exe
} else {
    $exe = Join-Path $root "dist\SDRScanner\SDRScanner.exe"
    $payload = Join-Path $root "dist\SDRScanner"
    Copy-Item README.md, LICENSE -Destination $payload
}

if (-not $SkipSelfTest) {
    Write-Host "Running self-test"
    $report = Join-Path $root "dist\selftest.txt"
    $env:QT_QPA_PLATFORM = "offscreen"
    $p = Start-Process -FilePath $exe -ArgumentList "--self-test", "`"$report`"" -Wait -PassThru
    Remove-Item Env:QT_QPA_PLATFORM
    if (Test-Path $report) { Get-Content $report }
    if ($p.ExitCode -ne 0) { throw "Self-test failed (exit code $($p.ExitCode))" }
}

$zip = Join-Path $root "dist\SDRScanner-$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $payload -DestinationPath $zip
Write-Host ""
Write-Host "Done: $exe"
Write-Host "      $zip"
