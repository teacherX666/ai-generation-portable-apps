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
    $env:LISTEN = "127.0.0.1"

    New-Item -ItemType Directory -Path $env:CUDA_CACHE_PATH -Force | Out-Null
}

function Test-ServiceHealth {
    param([hashtable]$Service)

    foreach ($url in $Service.HealthUrls) {
        try {
            $null = & curl.exe --noproxy "*" --silent --show-error --max-time 4 $url 2>$null
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
