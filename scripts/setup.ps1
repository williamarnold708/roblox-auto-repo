<#
.SYNOPSIS
  One-time setup for RobloxAutoPromo on Windows. Run from PowerShell in the project folder: .\scripts\setup.ps1

.DESCRIPTION
  1. Installs Python 3.12 and FFmpeg with winget if they are missing.
  2. Creates a virtual environment (.venv) and installs requirements.txt.
  3. Runs the demo to prove the pipeline works on this PC (skip with -SkipDemo).
  4. Asks for your game details and writes inbox\<game>\game.json.
  5. Optionally points the inbox at a Google Drive / OneDrive / Dropbox folder,
     so recordings uploaded from your phone are picked up automatically.
  6. Registers the auto-start tasks (scripts\install_windows_task.ps1).

  Safe to re-run: every step checks what is already done.
#>
[CmdletBinding()]
param(
    [switch]$SkipDemo,
    [switch]$SkipTask
)
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectDir

function Step($msg) { Write-Host "`n=== $msg" -ForegroundColor Cyan }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Find-Python {
    # Avoid the Microsoft Store "python.exe" alias, which is not a real interpreter.
    $candidates = @()
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $exe = & $py.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe) { $candidates += $exe.Trim() }
    }
    $candidates += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
                   Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
    foreach ($c in $candidates) {
        $v = & $c -c "import sys; print(sys.version_info >= (3, 11))" 2>$null
        if ($v -eq "True") { return $c }
    }
    return $null
}

function Winget-Install($id) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "winget is not available. Install '$id' manually, then re-run setup."
    }
    winget install --id $id -e --accept-source-agreements --accept-package-agreements --silent
    Refresh-Path
}

function Slugify($s) {
    $slug = ($s.ToLower() -replace "[^a-z0-9]+", "-").Trim("-")
    if (-not $slug) { $slug = "my-game" }
    return $slug
}

# ---------------------------------------------------------------- 1. tools
Step "Checking Python and FFmpeg"
$python = Find-Python
if (-not $python) {
    Write-Host "Installing Python 3.12..."
    Winget-Install "Python.Python.3.12"
    $python = Find-Python
    if (-not $python) { throw "Python installed but not found. Close this window and run .\scripts\setup.ps1 again." }
}
Write-Host "Python : $python"

if (-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue)) {
    Write-Host "Installing FFmpeg..."
    Winget-Install "Gyan.FFmpeg"
    if (-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue)) {
        throw "FFmpeg installed but not on PATH yet. Close this window and run .\scripts\setup.ps1 again."
    }
}
Write-Host "FFmpeg : $((Get-Command ffmpeg.exe).Source)"

# ---------------------------------------------------------------- 2. venv
Step "Installing Python packages"
$venvPy = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { & $python -m venv .venv }
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

# ---------------------------------------------------------------- 3. demo
if (-not $SkipDemo) {
    Step "Running the demo (synthetic footage, about 1-2 minutes)"
    & $venvPy -m app demo
    if ($LASTEXITCODE -ne 0) { throw "Demo failed - see logs\autopromo.log." }
}

# ---------------------------------------------------------------- 4. inbox location
Step "Where will your recordings arrive?"
Write-Host "If you record on your phone, upload to a Google Drive / OneDrive / Dropbox folder"
Write-Host "that syncs to this PC, and enter that folder here (e.g. C:\Users\you\Google Drive\AutoPromo)."
$inboxInput = Read-Host "Synced folder path (press Enter to use the project's inbox folder)"
$settingsFile = Join-Path $ProjectDir "config\settings.toml"
if ($inboxInput) {
    $inbox = $inboxInput.Trim('"').Trim()
    New-Item -ItemType Directory -Force -Path $inbox | Out-Null
    $tomlPath = $inbox -replace "\\", "/"   # forward slashes avoid TOML escaping
    $content = Get-Content $settingsFile -Raw
    $content = [regex]::Replace($content, '(?m)^inbox\s*=.*$', "inbox = `"$tomlPath`"")
    Set-Content -Path $settingsFile -Value $content -NoNewline -Encoding UTF8
    Write-Host "Inbox set to $inbox"
} else {
    $inbox = Join-Path $ProjectDir "inbox"
}

# ---------------------------------------------------------------- 5. game.json
Step "Describe your game (used for captions and hashtags)"
$name = Read-Host "Game name"
if ($name) {
    $slug = Slugify $name
    $gameDir = Join-Path $inbox $slug
    $gameFile = Join-Path $gameDir "game.json"
    if ((Test-Path $gameFile) -and ((Read-Host "$gameFile exists. Overwrite? (y/N)") -ne "y")) {
        Write-Host "Kept existing game.json"
    } else {
        $url   = Read-Host "Roblox game link (https://www.roblox.com/games/...)"
        $desc  = Read-Host "One-sentence description"
        $genre = Read-Host "Genre (obby, tycoon, simulator, horror, ...)"
        $aud   = Read-Host "Target audience (e.g. 8-14, likes hard obbies)"
        $cta   = Read-Host "Call to action (Enter for 'Play $name on Roblox - link in bio')"
        if (-not $cta) { $cta = "Play $name on Roblox - link in bio" }
        $avoid = Read-Host "Words to never use, comma-separated (Enter for none)"
        $game = [ordered]@{
            name        = $name
            url         = $url
            description = $desc
            genre       = $genre
            audience    = $aud
            cta         = $cta
            hashtags    = @("roblox") + @($(if ($genre) { ($genre.ToLower() -replace "[^a-z0-9]", "") }) | Where-Object { $_ })
            avoid_words = @($avoid -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        }
        New-Item -ItemType Directory -Force -Path $gameDir | Out-Null
        $json = $game | ConvertTo-Json
        [IO.File]::WriteAllText($gameFile, $json, (New-Object Text.UTF8Encoding $false))
        Write-Host "Wrote $gameFile"
    }
    Write-Host "Put recordings for this game in: $gameDir" -ForegroundColor Green
} else {
    Write-Host "Skipped. Create inbox\<game>\game.json later (see README)."
}

# ---------------------------------------------------------------- 6. auto-start
if (-not $SkipTask) {
    Step "Registering auto-start tasks"
    & (Join-Path $PSScriptRoot "install_windows_task.ps1") -StartNow
}

Step "Done"
Write-Host "Check status : .venv\Scripts\python -m app status"
Write-Host "Dashboard    : .venv\Scripts\python -m app dashboard"
Write-Host "Finished videos + captions appear in queue\ready\ - post them, then run:"
Write-Host "               .venv\Scripts\python -m app posted N --url <tiktok link>"
