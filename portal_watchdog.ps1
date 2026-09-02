[CmdletBinding()]
param(
    [switch]$Stop
)

$ErrorActionPreference = "Stop"

$PortalDir = Join-Path $PSScriptRoot "portal"
$StateDir = Join-Path $PortalDir "state"
$WatchdogLog = Join-Path $StateDir "portal-watchdog.log"
$ChildOutLog = Join-Path $StateDir "portal.out.log"
$ChildErrLog = Join-Path $StateDir "portal.err.log"
$ChildPidFile = Join-Path $StateDir "portal-watchdog.child.pid"
$StopFile = Join-Path $StateDir "portal-watchdog.stop"
$HealthUrls = @(
    "https://127.0.0.1:9090/api/platform/status",
    "http://127.0.0.1:9090/api/platform/status"
)
$HealthIntervalSeconds = 10
$HealthFailuresToRestart = 3
$StartupTimeoutSeconds = 90
$RestartDelaySeconds = 5

function Write-WatchdogLog {
    param([string]$Message)

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $WatchdogLog -Value "[$timestamp] $Message"
}

function Find-PortalPython {
    $candidates = @(
        @{ Name = "py.exe"; Arguments = @("-3", "-c", "import sys; print(sys.executable)") },
        @{ Name = "python.exe"; Arguments = @("-c", "import sys; print(sys.executable)") },
        @{ Name = "python3.exe"; Arguments = @("-c", "import sys; print(sys.executable)") }
    )

    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Name -ErrorAction SilentlyContinue
        if (-not $command) {
            continue
        }

        try {
            $output = & $command.Source $candidate.Arguments 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $output) {
                continue
            }
        }
        catch {
            continue
        }

        $python = ($output | Select-Object -First 1).Trim()
        if (-not (Test-Path -LiteralPath $python)) {
            continue
        }

        $versionOutput = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $versionOutput) {
            continue
        }

        $version = ($versionOutput | Select-Object -First 1).Trim().Split(".")
        if ([int]$version[0] -ne 3 -or [int]$version[1] -lt 9 -or [int]$version[1] -gt 12) {
            continue
        }

        return $python
    }

    return $null
}

function Test-PortalHealth {
    foreach ($url in $HealthUrls) {
        $output = & cmd.exe /d /c "curl.exe -k -sS --max-time 5 `"$url`" 2>nul"
        if ($LASTEXITCODE -eq 0) {
            return $true
        }
    }
    return $false
}

function Stop-PortalTree {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return
    }

    $output = & cmd.exe /d /c "taskkill.exe /PID $ProcessId /T /F >nul 2>&1"
    if ($LASTEXITCODE -ne 0) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Request-PortalStop {
    $currentPid = [System.Diagnostics.Process]::GetCurrentProcess().Id
    if (-not (Test-Path -LiteralPath $StopFile)) {
        Set-Content -LiteralPath $StopFile -Value "stop requested by pid $currentPid"
    }

    if (Test-Path -LiteralPath $ChildPidFile) {
        $childPid = (Get-Content -LiteralPath $ChildPidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
        if ($childPid -match "^\d+$") {
            Stop-PortalTree -ProcessId ([int]$childPid)
        }
    }
}

if ($Stop) {
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    Request-PortalStop
    Write-WatchdogLog "stop requested"
    Write-Output "Portal watchdog stop requested."
    exit 0
}

New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
Remove-Item -LiteralPath $StopFile -Force -ErrorAction SilentlyContinue

$mutex = New-Object System.Threading.Mutex($false, "Local\AI.Generation.Portal.Watchdog")
try {
    if (-not $mutex.WaitOne(0)) {
        Write-WatchdogLog "another watchdog instance is already running"
        exit 0
    }
}
catch {
    Write-WatchdogLog "failed to acquire mutex: $($_.Exception.Message)"
    exit 1
}

$watchdogPid = [System.Diagnostics.Process]::GetCurrentProcess().Id
Set-Content -LiteralPath (Join-Path $StateDir "portal-watchdog.pid") -Value $watchdogPid
Write-WatchdogLog "watchdog started (pid $watchdogPid)"

$python = Find-PortalPython
if (-not $python) {
    Write-WatchdogLog "compatible Python 3.9-3.12 was not found"
    $mutex.ReleaseMutex()
    exit 1
}

Write-WatchdogLog "using Python: $python"

# Windows stdlib stubs for these two apps only support the FastAPI engine.
$env:INFINITE_CANVAS_ENGINE = "fastapi"
$env:RAG_ASSISTANT_ENGINE = "fastapi"

$child = $null
$healthFailures = 0

try {
    while ($true) {
        if (Test-Path -LiteralPath $StopFile) {
            Write-WatchdogLog "stop file detected"
            if ($null -ne $child -and -not $child.HasExited) {
                Stop-PortalTree -ProcessId $child.Id
                $child.WaitForExit()
            }
            break
        }

        if ($null -eq $child -or $child.HasExited) {
            if ($null -ne $child) {
                $exitCode = $child.ExitCode
                Write-WatchdogLog "portal exited with code $exitCode; restarting in ${RestartDelaySeconds}s"
                Start-Sleep -Seconds $RestartDelaySeconds
            }

            Set-Content -LiteralPath $ChildOutLog -Value "" -ErrorAction SilentlyContinue
            Set-Content -LiteralPath $ChildErrLog -Value "" -ErrorAction SilentlyContinue

            try {
                $child = Start-Process `
                    -FilePath $python `
                    -ArgumentList @("-u", "app.py") `
                    -WorkingDirectory $PortalDir `
                    -WindowStyle Hidden `
                    -RedirectStandardOutput $ChildOutLog `
                    -RedirectStandardError $ChildErrLog `
                    -PassThru
            }
            catch {
                Write-WatchdogLog "failed to start portal: $($_.Exception.Message)"
                Start-Sleep -Seconds $RestartDelaySeconds
                continue
            }

            Set-Content -LiteralPath $ChildPidFile -Value $child.Id
            Write-WatchdogLog "started portal process $($child.Id)"

            $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
            $ready = $false
            while ((Get-Date) -lt $deadline) {
                if (Test-Path -LiteralPath $StopFile) {
                    break
                }
                if ($child.HasExited) {
                    break
                }
                if (Test-PortalHealth) {
                    $ready = $true
                    break
                }
                Start-Sleep -Seconds 1
            }

            if (-not $ready) {
                if (-not $child.HasExited) {
                    Write-WatchdogLog "portal did not become healthy in ${StartupTimeoutSeconds}s"
                    Stop-PortalTree -ProcessId $child.Id
                    $child.WaitForExit()
                }
                $healthFailures = 0
                Start-Sleep -Seconds $RestartDelaySeconds
                continue
            }

            $healthFailures = 0
            Write-WatchdogLog "portal is healthy"
        }
        elseif (-not (Test-PortalHealth)) {
            $healthFailures++
            Write-WatchdogLog "health check failed ($healthFailures/$HealthFailuresToRestart)"
            if ($healthFailures -ge $HealthFailuresToRestart) {
                Write-WatchdogLog "restarting unresponsive portal"
                Stop-PortalTree -ProcessId $child.Id
                $child.WaitForExit()
                $healthFailures = 0
                Start-Sleep -Seconds $RestartDelaySeconds
                continue
            }
        }
        else {
            $healthFailures = 0
        }

        Start-Sleep -Seconds $HealthIntervalSeconds
    }
}
finally {
    if ($null -ne $child -and -not $child.HasExited) {
        Stop-PortalTree -ProcessId $child.Id
    }

    Remove-Item -LiteralPath $ChildPidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $StateDir "portal-watchdog.pid") -Force -ErrorAction SilentlyContinue
    Write-WatchdogLog "watchdog stopped"
    $mutex.ReleaseMutex()
}
