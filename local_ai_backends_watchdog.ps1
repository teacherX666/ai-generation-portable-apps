[CmdletBinding()]
param(
    [switch]$Stop
)

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StateDir = Join-Path $RootDir "state\local-ai-backends"
$WatchdogLog = Join-Path $StateDir "watchdog.log"
$StopFile = Join-Path $StateDir "watchdog.stop"
$PollIntervalSeconds = 5

# Keep 127.0.0.1 by default. Set LOCAL_AI_LISTEN=0.0.0.0 only when
# another server on the LAN needs to reach the AI Port gateway.
$localAiConfig = Join-Path $PSScriptRoot "config\local_ai.env"
if (-not $env:LOCAL_AI_LISTEN -and (Test-Path -LiteralPath $localAiConfig)) {
    foreach ($line in [System.IO.File]::ReadAllLines($localAiConfig)) {
        $line = $line.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        $key, $value = $line -split "=", 2
        if ($key -eq "LOCAL_AI_LISTEN" -and $value) {
            $env:LOCAL_AI_LISTEN = $value.Trim().Trim('"')
            break
        }
    }
}
$ListenAddress = if ($env:LOCAL_AI_LISTEN) { $env:LOCAL_AI_LISTEN } else { "0.0.0.0" }

$ComfyRoot = "E:\AI Tool\ComfyUI_windows_portable"
$ComfyPython = Join-Path $ComfyRoot "python_embeded\python.exe"
$StudioRoot = "E:\AI Tool\projects\work\ai-portable-studio"
$StudioPython = "C:\Users\123\AppData\Local\Programs\Python\Python312\python.exe"

$Services = [ordered]@{
    "comfyui" = [ordered]@{
        Label = "ComfyUI"
        Port = 8188
        HealthUrls = @("http://127.0.0.1:8188/system_stats")
        FilePath = $ComfyPython
        Arguments = @(
            "-s",
            "ComfyUI\main.py",
            "--windows-standalone-build",
            "--listen", "127.0.0.1",
            "--port", "8188",
            "--preview-method", "none",
            "--reserve-vram", "2",
            "--use-sage-attention"
        )
        WorkingDirectory = $ComfyRoot
        PidFile = Join-Path $StateDir "comfyui.pid"
        StdoutLog = Join-Path $StateDir "comfyui.out.log"
        StderrLog = Join-Path $StateDir "comfyui.err.log"
        StartupTimeoutSeconds = 300
        RecoveryTimeoutSeconds = 60
        RestartBackoffSeconds = 5
        # "Busy" probe: /queue is a plain in-memory read and stays responsive while
        # /system_stats (which queries GPU/VRAM) stalls under heavy load. If neither
        # answers, fall back to the GPU.
        QueueUrl = "http://127.0.0.1:8188/queue"
        BusyGpuMemMb = 4000
        # Only treat "busy but unresponsive" as wedged after this long. One measured
        # 15s 720p upscale took 30 minutes, so leave plenty of headroom.
        BusyMaxSeconds = 5400
    }
    "aiport" = [ordered]@{
        Label = "AI Port"
        Port = 8801
        HealthUrls = @("http://127.0.0.1:8801/api/modules")
        FilePath = $StudioPython
        Arguments = @("-u", "app.py")
        WorkingDirectory = $StudioRoot
        PidFile = Join-Path $StateDir "aiport.pid"
        StdoutLog = Join-Path $StateDir "aiport.out.log"
        StderrLog = Join-Path $StateDir "aiport.err.log"
        StartupTimeoutSeconds = 60
        RecoveryTimeoutSeconds = 30
        RestartBackoffSeconds = 3
        # The gateway also runs jobs: never kill it while its queue is non-empty,
        # that is exactly how an in-flight job gets destroyed.
        QueueUrl = "http://127.0.0.1:8801/api/queue"
        BusyMaxSeconds = 5400
    }
}

function Write-WatchdogLog {
    param([string]$Message)

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $WatchdogLog -Value "[$timestamp] $Message"
}

function Initialize-BackendEnvironment {
    $ffmpegBin = "E:\AI Tool\tools\ffmpeg\bin"
    if (Test-Path -LiteralPath $ffmpegBin) {
        $env:PATH = "$ffmpegBin;$env:PATH"
    }

    $env:HF_ENDPOINT = "https://hf-mirror.com"
    $env:HF_HOME = "E:\AI Tool\models\huggingface-cache"
    $env:HF_HUB_DISABLE_TELEMETRY = "1"
    $env:TRUST_REMOTE_CODE = "1"
    $env:PYTORCH_CUDA_ALLOC_CONF = "expandable_segments:True,max_split_size_mb:128"
    $env:CUDA_CACHE_PATH = "$ComfyRoot\.cuda_cache"
    $env:CUDA_CACHE_MAXSIZE = "2147483648"
    $env:CUBLAS_WORKSPACE_CONFIG = ":4096:8"
    $env:PORT = "8801"
    $env:LISTEN = $ListenAddress

    New-Item -ItemType Directory -Path $env:CUDA_CACHE_PATH -Force | Out-Null
}

function Test-ServiceHealth {
    param([hashtable]$Service)

    foreach ($url in $Service.HealthUrls) {
        try {
            $null = & curl.exe --noproxy "*" --silent --show-error --fail --max-time 4 $url 2>$null
            if ($LASTEXITCODE -eq 0) {
                return $true
            }
        }
        catch {
            # Fall through to Invoke-WebRequest for hosts without curl.exe.
        }

        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 4 -ErrorAction Stop
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
                return $true
            }
        }
        catch {
            # Keep checking the remaining health URLs.
        }
    }

    return $false
}

# When a health probe fails, decide whether the service is BUSY or DEAD.
# Why (measured 2026-09-20): a 15s 720p upscale ran for 30 minutes; during the
# final decode /system_stats stopped answering for 60s (it queries GPU/VRAM and
# gets stuck under load), so the watchdog taskkilled ComfyUI and 30 minutes of
# work were lost. A failing probe is NOT proof the service is dead.
function Test-ServiceBusy {
    param([hashtable]$Service)

    # 1) A non-empty queue means it is working. ComfyUI exposes
    #    queue_running/queue_pending, AI Port exposes count.
    if ($Service.QueueUrl) {
        $raw = & curl.exe --noproxy "*" --silent --max-time 4 $Service.QueueUrl 2>$null
        if ($LASTEXITCODE -eq 0 -and $raw) {
            try {
                $queue = $raw | ConvertFrom-Json
                if ($null -ne $queue.queue_running -or $null -ne $queue.queue_pending) {
                    return ((@($queue.queue_running).Count + @($queue.queue_pending).Count) -gt 0)
                }
                if ($null -ne $queue.count) {
                    return ([int]$queue.count -gt 0)
                }
            }
            catch {
                # Queue answered but the body was unreadable; fall through to the GPU.
            }
        }
    }

    # 2) Queue unreachable: if the GPU is still loaded it is computing, not dead.
    #    Sample a few times and take the peak: a single reading is far too jumpy
    #    (measured 1% -> 57% -> 12% within 4 seconds while a job was running).
    #    NOTE: do NOT pipe nvidia-smi into Select-Object -First 1 -- cutting the
    #    pipeline short makes $LASTEXITCODE -1 and silently skips the check.
    #    Capture the whole output first, then index it.
    if ($Service.BusyGpuMemMb -gt 0) {
        $maxUtil = 0
        $maxMem = 0
        foreach ($attempt in 1..3) {
            $out = @(& nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits 2>$null)
            $line = if ($out.Count -gt 0) { [string]$out[0] } else { "" }
            if ($line) {
                $parts = $line -split ","
                if ($parts.Count -ge 2) {
                    $util = 0; $mem = 0
                    [void][int]::TryParse($parts[0].Trim(), [ref]$util)
                    [void][int]::TryParse($parts[1].Trim(), [ref]$mem)
                    if ($util -gt $maxUtil) { $maxUtil = $util }
                    if ($mem -gt $maxMem) { $maxMem = $mem }
                }
            }
            if ($attempt -lt 3) {
                Start-Sleep -Milliseconds 600
            }
        }
        if ($maxUtil -ge 10 -or $maxMem -ge $Service.BusyGpuMemMb) {
            return $true
        }
    }

    return $false
}

function Get-PortOwnerProcessId {
    param([int]$Port)

    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @("127.0.0.1", "0.0.0.0", "::", "::1") }
    $connection = $connections | Select-Object -First 1
    if ($null -eq $connection) {
        return 0
    }

    return [int]$connection.OwningProcess
}

function Stop-ProcessTree {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return
    }

    $null = & taskkill.exe /PID $ProcessId /T /F 2>$null
    if ($LASTEXITCODE -ne 0) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
}

function Read-PidFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return 0
    }

    $value = (Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($value -match "^\d+$") {
        return [int]$value
    }

    return 0
}

function Start-LocalService {
    param(
        [string]$Key,
        [hashtable]$Service,
        [hashtable]$Runtime
    )

    $Runtime.Pid = 0
    Set-Content -LiteralPath $Service.PidFile -Value "" -ErrorAction SilentlyContinue

    try {
        $process = Start-Process `
            -FilePath $Service.FilePath `
            -ArgumentList $Service.Arguments `
            -WorkingDirectory $Service.WorkingDirectory `
            -WindowStyle Hidden `
            -RedirectStandardOutput $Service.StdoutLog `
            -RedirectStandardError $Service.StderrLog `
            -PassThru
    }
    catch {
        Write-WatchdogLog "$($Service.Label) failed to start: $($_.Exception.Message)"
        $Runtime.RestartNotBefore = (Get-Date).AddSeconds($Service.RestartBackoffSeconds)
        return $false
    }

    $Runtime.Pid = $process.Id
    $Runtime.LastStartAttempt = Get-Date
    $Runtime.RestartNotBefore = (Get-Date).AddSeconds($Service.RestartBackoffSeconds)
    $Runtime.EverHealthy = $false
    $Runtime.HealthFailures = 0
    Set-Content -LiteralPath $Service.PidFile -Value $process.Id
    Write-WatchdogLog "started $($Service.Label) process $($process.Id)"
    return $true
}

New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
Remove-Item -LiteralPath $StopFile -Force -ErrorAction SilentlyContinue

$Runtime = @{}
foreach ($key in $Services.Keys) {
    $Runtime[$key] = @{
        Pid = 0
        EverHealthy = $false
        HealthFailures = 0
        LastStartAttempt = [datetime]::MinValue
        RestartNotBefore = [datetime]::MinValue
        BusySince = [datetime]::MinValue
    }
}

if ($Stop) {
    Write-WatchdogLog "stop requested"
    foreach ($key in $Services.Keys) {
        $service = $Services[$key]
        $pid = Read-PidFile -Path $service.PidFile
        if ($pid -gt 0) {
            Stop-ProcessTree -ProcessId $pid
        }
        $owner = Get-PortOwnerProcessId -Port $service.Port
        if ($owner -gt 0) {
            Stop-ProcessTree -ProcessId $owner
        }
        Write-WatchdogLog "stopped $($service.Label)"
    }
    exit 0
}

$mutex = New-Object System.Threading.Mutex($false, "Local\AI.Generation.LocalBackends.Watchdog")
try {
    if (-not $mutex.WaitOne(0)) {
        Write-WatchdogLog "another local backend watchdog instance is already running"
        exit 0
    }
}
catch {
    Write-WatchdogLog "failed to acquire mutex: $($_.Exception.Message)"
    exit 1
}

Write-WatchdogLog "watchdog started (pid $PID)"
Initialize-BackendEnvironment

try {
    while ($true) {
        if (Test-Path -LiteralPath $StopFile) {
            Write-WatchdogLog "stop file detected"
            break
        }

        foreach ($key in $Services.Keys) {
            $service = $Services[$key]
            $state = $Runtime[$key]

            if ((Get-Date) -lt $state.RestartNotBefore) {
                continue
            }

            $healthy = Test-ServiceHealth -Service $service
            $portOwner = Get-PortOwnerProcessId -Port $service.Port

            if ($healthy) {
                if ($portOwner -gt 0) {
                    if ($state.Pid -ne $portOwner) {
                        Write-WatchdogLog "adopted existing healthy $($service.Label) process $portOwner"
                    }
                    $state.Pid = $portOwner
                    Set-Content -LiteralPath $service.PidFile -Value $portOwner
                }

                if (-not $state.EverHealthy) {
                    Write-WatchdogLog "$($service.Label) is healthy"
                }
                $state.EverHealthy = $true
                $state.HealthFailures = 0
                $state.LastStartAttempt = Get-Date
                $state.BusySince = [datetime]::MinValue
                continue
            }

            $state.HealthFailures++
            $trackedProcess = $null
            if ($state.Pid -gt 0) {
                $trackedProcess = Get-Process -Id $state.Pid -ErrorAction SilentlyContinue
            }

            $timeoutSeconds = $service.StartupTimeoutSeconds
            if ($state.EverHealthy) {
                $timeoutSeconds = $service.RecoveryTimeoutSeconds
            }

            $elapsedSeconds = ((Get-Date) - $state.LastStartAttempt).TotalSeconds
            $processAlive = ($portOwner -gt 0) -or ($null -ne $trackedProcess)
            $inGracePeriod = ($state.LastStartAttempt -ne [datetime]::MinValue) -and
                ($elapsedSeconds -lt $timeoutSeconds)

            if ($processAlive -and $inGracePeriod) {
                if (($state.HealthFailures % 12) -eq 0) {
                    Write-WatchdogLog "$($service.Label) is still starting (elapsed $([int]$elapsedSeconds)s)"
                }
                continue
            }

            # Probe failing but the service is demonstrably working (queue non-empty /
            # GPU loaded) -> busy, not dead. Never taskkill it here. Only a service
            # that stays busy AND unresponsive past BusyMaxSeconds is treated as wedged.
            if (Test-ServiceBusy -Service $service) {
                if ($state.BusySince -eq [datetime]::MinValue) {
                    $state.BusySince = Get-Date
                }
                $busySeconds = ((Get-Date) - $state.BusySince).TotalSeconds
                $busyMax = if ($service.BusyMaxSeconds) { $service.BusyMaxSeconds } else { 1800 }
                if ($busySeconds -lt $busyMax) {
                    if (($state.HealthFailures % 12) -eq 0) {
                        Write-WatchdogLog "$($service.Label) health probe failing but busy ($([int]$busySeconds)s) - not restarting"
                    }
                    continue
                }
                Write-WatchdogLog "$($service.Label) busy and unresponsive for $([int]$busySeconds)s - forcing restart"
            }
            $state.BusySince = [datetime]::MinValue

            if ($portOwner -gt 0) {
                Write-WatchdogLog "$($service.Label) is unhealthy; stopping process $portOwner"
                Stop-ProcessTree -ProcessId $portOwner
            }
            elseif ($null -ne $trackedProcess) {
                Write-WatchdogLog "$($service.Label) process $($state.Pid) is unhealthy; restarting"
                Stop-ProcessTree -ProcessId $state.Pid
            }
            else {
                Write-WatchdogLog "$($service.Label) is down; restarting"
            }

            $null = Start-LocalService -Key $key -Service $service -Runtime $state
        }

        Start-Sleep -Seconds $PollIntervalSeconds
    }
}
finally {
    foreach ($key in $Services.Keys) {
        $service = $Services[$key]
        $state = $Runtime[$key]
        if ($state.Pid -gt 0) {
            Stop-ProcessTree -ProcessId $state.Pid
        }
        $owner = Get-PortOwnerProcessId -Port $service.Port
        if ($owner -gt 0) {
            Stop-ProcessTree -ProcessId $owner
        }
    }
    Write-WatchdogLog "watchdog stopped"
    $mutex.ReleaseMutex()
}
