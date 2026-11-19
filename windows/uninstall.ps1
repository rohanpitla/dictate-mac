# Remove Dictate for Windows.
#
# Removes the app, its Python, its shortcuts and the saved API key. Asks
# whether to also delete your settings and dictation history (kept by
# default, so a reinstall picks them up).
#
#   -Purge     delete settings and history too, without asking
#   -KeepData  keep them, without asking

param([switch]$Purge, [switch]$KeepData)

$ErrorActionPreference = "Continue"
$Base = Join-Path $env:LOCALAPPDATA "Dictate"
$App = Join-Path $Base "app"
$python = Join-Path $Base "venv\Scripts\python.exe"

Write-Host ""
Write-Host "Uninstalling Dictate" -ForegroundColor Cyan

# 1. Stop Dictate (and its microphone process).
$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*dictate_win*" -and $_.ExecutablePath -like "$Base*" }
foreach ($p in $running) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 800

# 2. If Dictate was stopped while it had the sound muted, turn it back on;
#    and remove the saved API key from Credential Manager.
if ((Test-Path $python) -and (Test-Path $App)) {
    Push-Location $App
    & $python -c "from dictate_win.mute import OutputMuter; OutputMuter().recover_after_crash()" 2>&1 | Out-Null
    & $python -m dictate_win.credentials --forget api.groq.com 2>&1 | Out-Null
    Pop-Location
}
# Any other saved Dictate keys (a different provider address)
cmdkey /list | Select-String "Dictate/" | ForEach-Object {
    $target = ($_ -replace ".*Target:\s*(LegacyGeneric:target=)?", "").Trim()
    if ($target -like "Dictate/*") { cmdkey /delete:$target | Out-Null }
}

# 3. Shortcuts.
foreach ($folder in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Startup"))) {
    $link = Join-Path $folder "Dictate.lnk"
    if (Test-Path $link) { Remove-Item $link -Force }
}

# 4. The app, its Python and tools.
foreach ($item in @("app", "app-new", "venv", "python", "tools", "cache", "dictate.ico")) {
    $path = Join-Path $Base $item
    if (Test-Path $path) { Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue }
}

# 5. Settings, history, logs, recordings.
$deleteData = $Purge
if (-not $Purge -and -not $KeepData) {
    $answer = Read-Host "  Also delete your settings and dictation history? (y/N)"
    $deleteData = $answer -match "^[Yy]"
}
if ($deleteData) {
    Remove-Item $Base -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "  Settings and history deleted."
} else {
    Write-Host "  Your settings and history are kept in $Base"
}

Write-Host ""
Write-Host "Dictate has been removed." -ForegroundColor Green
