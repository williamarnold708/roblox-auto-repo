<#
.SYNOPSIS
  Remove the RobloxAutoPromo scheduled tasks (stops the running service first).
  Your inbox, videos, database and logs are NOT touched.
#>
$ErrorActionPreference = "Continue"
foreach ($name in @("RobloxAutoPromo Service", "RobloxAutoPromo Daily Summary")) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($t) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed '$name'."
    } else {
        Write-Host "'$name' not installed."
    }
}
# pythonw may outlive the task if started manually; stop only our service processes
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "-m app service" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; Write-Host "Stopped PID $($_.ProcessId)." }
