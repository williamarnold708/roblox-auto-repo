<#
.SYNOPSIS
  Register RobloxAutoPromo with Windows Task Scheduler (current user, no admin needed).

.DESCRIPTION
  Creates two tasks:
    "RobloxAutoPromo Service"        - at logon, runs `pythonw -m app service` (no console window) from the
                                        project folder. Re-checked every 15 minutes so it is
                                        restarted if it ever exits; never runs twice at once.
    "RobloxAutoPromo Daily Summary"  - every day at -SummaryTime, runs `python -m app summary --notify`.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -SummaryTime 21:30 -StartNow
#>
[CmdletBinding()]
param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$Python = "",
    [string]$SummaryTime = "21:00",
    [int]$IntervalSeconds = 60,
    [switch]$StartNow
)
$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path $ProjectDir).Path

# --- find pythonw.exe (prefer the project's virtualenv)
if (-not $Python) {
    $candidates = @(
        (Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"),
        (Join-Path $ProjectDir "venv\Scripts\pythonw.exe")
    )
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    $Python = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $Python -or -not (Test-Path $Python)) {
    throw "pythonw.exe not found. Install Python 3.11+ (winget install Python.Python.3.11) or pass -Python C:\path\to\pythonw.exe"
}
Write-Host "Project : $ProjectDir"
Write-Host "Python  : $Python"

$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

# --- service task
$svcName = "RobloxAutoPromo Service"
$svcAction = New-ScheduledTaskAction -Execute $Python `
    -Argument "-m app service --interval $IntervalSeconds" -WorkingDirectory $ProjectDir
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
# watchdog: re-trigger every 15 min; MultipleInstances IgnoreNew means it only starts if not running
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)
$svcSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $svcName -Action $svcAction -Trigger @($logon, $watchdog) `
    -Settings $svcSettings -Principal $principal `
    -Description "RobloxAutoPromo: watches the inbox, makes TikTok videos, posts on schedule. Stop: python -m app kill (or create a KILL file)." `
    -Force | Out-Null
Write-Host "Registered '$svcName' (at logon + 15-min watchdog; pythonw = no window)."

# --- daily summary task
$sumName = "RobloxAutoPromo Daily Summary"
$sumAction = New-ScheduledTaskAction -Execute $Python -Argument "-m app summary --notify" -WorkingDirectory $ProjectDir
$daily = New-ScheduledTaskTrigger -Daily -At $SummaryTime
$sumSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName $sumName -Action $sumAction -Trigger $daily `
    -Settings $sumSettings -Principal $principal `
    -Description "RobloxAutoPromo daily summary -> logs\summary-YYYY-MM-DD.md + notification." -Force | Out-Null
Write-Host "Registered '$sumName' (daily at $SummaryTime)."

if ($StartNow) {
    Start-ScheduledTask -TaskName $svcName
    Write-Host "Service started."
}
Write-Host ""
Write-Host "Check:    python -m app status"
Write-Host "Logs:     $ProjectDir\logs\autopromo.log"
Write-Host "Remove:   powershell -ExecutionPolicy Bypass -File scripts\uninstall_windows_task.ps1"
