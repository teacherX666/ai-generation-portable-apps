[CmdletBinding()]
param(
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$TaskName = "AI Generation Local AI Backends Watchdog"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WatchdogScript = Join-Path $ScriptDir "local_ai_backends_watchdog.ps1"

if (-not (Test-Path -LiteralPath $WatchdogScript)) {
    throw "watchdog script not found: $WatchdogScript"
}

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Unregistered task: $TaskName"
    exit 0
}

$action = New-ScheduledTaskAction `
    -Execute "C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$WatchdogScript`"" `
    -WorkingDirectory $ScriptDir

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Start and supervise ComfyUI (8188) and AI Port (8801); restart them on crash or failed health checks."

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Write-Output "Registered task: $TaskName"
Write-Output "Watchdog: $WatchdogScript"
