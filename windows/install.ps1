# Dictate for Windows installer.
#
#   irm https://raw.githubusercontent.com/rohanpitla/dictate-mac/main/windows/install.ps1 | iex
#
# Installs everything into %LOCALAPPDATA%\Dictate for this Windows account
# only. No administrator rights; nothing system-wide. Running it again
# updates Dictate and keeps your settings, history and API key.
#
# Every download is checked against a SHA-256 checksum written in this
# file, and every Python package against the hashes in
# requirements-windows.txt, so a tampered download is refused.
#
# Options (when running the file directly):
#   -WithLocal      also install the on-laptop speech engine (~200 MB more)
#   -Source <dir>   install from a local copy of the code (testing)
#   -NoLaunch       don't start Dictate at the end
#   -StartAtLogin / -NoStartAtLogin   skip the question

param(
    [switch]$WithLocal,
    [string]$Source = "",
    [switch]$NoLaunch,
    [switch]$StartAtLogin,
    [switch]$NoStartAtLogin
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # PowerShell 5's progress bar slows downloads 10x
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---- pinned downloads ---------------------------------------------------
$UvVersion = "0.13.6"
$UvUrl = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
$UvSha256 = "a2a8820d5eea32f08ef467ef3b7f66883a9b076d5737a2fc2fbb5f458d1bb50f"
# Set when a release is published (windows/make_release.py prints them).
$ReleaseUrl = ""
$ReleaseSha256 = ""
# x64 Python on every PC, including ARM laptops (Windows runs it under
# emulation; several audio/AI packages have no ARM build).
$PythonSpec = "cpython-3.13-windows-x86_64-none"

$Base = Join-Path $env:LOCALAPPDATA "Dictate"
$App = Join-Path $Base "app"
$Venv = Join-Path $Base "venv"
$Tools = Join-Path $Base "tools"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Base "python"
$env:UV_CACHE_DIR = Join-Path $Base "cache"
$env:UV_NO_MODIFY_PATH = "1"

function Say($text) { Write-Host "  $text" }
function Fail($text) {
    Write-Host ""
    Write-Host "  Dictate could not be installed: $text" -ForegroundColor Red
    Write-Host "  Nothing else on your PC was changed." -ForegroundColor Red
    throw $text
}

function Get-Verified($url, $sha256, $out) {
    Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
    $actual = (Get-FileHash -Algorithm SHA256 -Path $out).Hash.ToLower()
    if ($actual -ne $sha256.ToLower()) {
        Remove-Item $out -Force
        Fail "the download from $url did not match its checksum (it may have been tampered with)."
    }
}

function Stop-Dictate {
    $running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*dictate_win*" -and $_.ExecutablePath -like "$Base*" }
    foreach ($p in $running) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    if ($running) { Start-Sleep -Milliseconds 800 }
}

Write-Host ""
Write-Host "Installing Dictate for Windows" -ForegroundColor Cyan
if (-not [Environment]::Is64BitOperatingSystem) { Fail "Dictate needs 64-bit Windows." }
$build = [Environment]::OSVersion.Version.Build
if ($build -lt 17763) { Fail "Dictate needs Windows 10 (version 1809) or newer." }
New-Item -ItemType Directory -Force -Path $Base, $Tools | Out-Null

# ---- 1. uv (installs Python and the packages) ---------------------------
$uv = Join-Path $Tools "uv.exe"
$haveUv = (Test-Path $uv) -and ((& $uv --version 2>$null) -like "uv $UvVersion*")
if (-not $haveUv) {
    Say "Downloading the installer tool (uv $UvVersion)..."
    $zip = Join-Path $env:TEMP "dictate-uv-$UvVersion.zip"
    Get-Verified $UvUrl $UvSha256 $zip
    Expand-Archive -Path $zip -DestinationPath $Tools -Force
    Remove-Item $zip -Force
}

# ---- 2. the app ----------------------------------------------------------
Stop-Dictate
$staging = Join-Path $Base "app-new"
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Force -Path $staging | Out-Null
if ($Source) {
    Say "Copying Dictate from $Source..."
    foreach ($item in @("dictate", "dictate_win", "windows", "requirements-windows.txt",
                        "requirements-windows-local.txt", "LICENSE")) {
        $from = Join-Path $Source $item
        if (-not (Test-Path $from)) { Fail "missing $item in $Source" }
        Copy-Item $from -Destination $staging -Recurse -Force
    }
} else {
    if (-not $ReleaseUrl) { Fail "no release has been published yet." }
    Say "Downloading Dictate..."
    $zip = Join-Path $env:TEMP "dictate-release.zip"
    Get-Verified $ReleaseUrl $ReleaseSha256 $zip
    Expand-Archive -Path $zip -DestinationPath $staging -Force
    Remove-Item $zip -Force
}
Get-ChildItem $staging -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
if (Test-Path $App) { Remove-Item $App -Recurse -Force }
Move-Item $staging $App

# ---- 3. Python and packages ---------------------------------------------
Say "Installing Python and the libraries Dictate uses (first time: a few minutes)..."
& $uv python install $PythonSpec --quiet
if ($LASTEXITCODE -ne 0) { Fail "Python could not be installed (uv python install)." }
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    & $uv venv $Venv --python $PythonSpec --quiet
    if ($LASTEXITCODE -ne 0) { Fail "could not create Dictate's Python environment." }
}
$python = Join-Path $Venv "Scripts\python.exe"
$pythonw = Join-Path $Venv "Scripts\pythonw.exe"
$reqs = Join-Path $App "requirements-windows.txt"
if ($WithLocal) { $reqs = Join-Path $App "requirements-windows-local.txt" }
& $uv pip sync $reqs --python $python --require-hashes --quiet
if ($LASTEXITCODE -ne 0) { Fail "the libraries could not be installed (a package failed its hash check or could not be downloaded)." }
Remove-Item $env:UV_CACHE_DIR -Recurse -Force -ErrorAction SilentlyContinue   # saves disk space

# Run Python quietly. (PowerShell 5 turns any text a program writes to
# stderr into an error when it is redirected, so "Stop" is relaxed here and
# the exit code decides.)
function Invoke-Python($code) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    Push-Location $App
    & $python -c $code *> $null
    $exit = $LASTEXITCODE
    Pop-Location
    $ErrorActionPreference = $saved
    return ($exit -eq 0)
}

# Quick check that everything loads before creating shortcuts.
if (-not (Invoke-Python "import dictate_win.app")) { Fail "Dictate's files did not load correctly." }

# ---- 4. shortcuts ---------------------------------------------------------
$icon = Join-Path $Base "dictate.ico"
Invoke-Python "from dictate_win.app import _icon_image; _icon_image('idle').save(r'$icon', sizes=[(16,16),(32,32),(48,48),(64,64)])" | Out-Null

function New-Shortcut($path) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $pythonw
    $link.Arguments = "-m dictate_win"
    $link.WorkingDirectory = $App
    $link.Description = "Dictate: tap a key, speak, and it types for you"
    if (Test-Path $icon) { $link.IconLocation = $icon }
    $link.Save()
}

$startMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Dictate.lnk"
New-Shortcut $startMenu
Say "Added Dictate to the Start menu."

$startup = Join-Path ([Environment]::GetFolderPath("Startup")) "Dictate.lnk"
$atLogin = $StartAtLogin
if (-not $StartAtLogin -and -not $NoStartAtLogin) {
    $answer = Read-Host "  Start Dictate automatically when you sign in to Windows? (Y/n)"
    $atLogin = ($answer -eq "" -or $answer -match "^[Yy]")
}
if ($atLogin) {
    New-Shortcut $startup
    Say "Dictate will start when you sign in."
} elseif (Test-Path $startup) {
    Remove-Item $startup -Force
}

Write-Host ""
Write-Host "Dictate is installed." -ForegroundColor Green
Say "Look for the microphone icon next to the clock (it may be under the ^ arrow)."
Say "Tap Right Ctrl to start dictating, tap it again to stop."
Say "To uninstall: tray icon menu > Uninstall Dictate."
if (-not $NoLaunch) {
    Start-Process -FilePath $pythonw -ArgumentList "-m dictate_win" -WorkingDirectory $App
}
