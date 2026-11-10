# 注册/更新「每日自动清理」计划任务（幂等，重复跑就是覆盖）
#
# 背景：仓库里的 tools/cleanup_daily.py 原本是 macOS 的 launchd 作业
# （com.ai-portal-cleanup，每日 03:47）。这台 Windows 服务机**从来没挂过它**，
# 所以 outputs 一路涨到 41GB —— 这正是"有没有自动清理的设定"的答案：
# 有设定，但没生效。本脚本把它补成 Windows 计划任务。

$ErrorActionPreference = 'Stop'

$TaskName = 'AI Generation Daily Cleanup'
$Wrapper  = 'C:\Users\123\Documents\LTX\ai-generation-portable-apps\_diag\run_daily_cleanup.ps1'

if (-not (Test-Path -LiteralPath $Wrapper)) {
    throw "找不到包装脚本：$Wrapper"
}

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $Wrapper)

# 每日 03:47（与原来 launchd 的口径一致；服务机整夜在线）
$trigger = New-ScheduledTaskTrigger -Daily -At '03:47'

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

# 与仓库现有任务（AI Generation Portal Watchdog）同一套主体：当前用户 + 交互式令牌
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description '每日 03:47 清理：outputs 超 7 天进回收站、回收站超 7 天彻底删、草稿素材超 30 天清、日志截断、失效下载 token 剪枝、history 超 30 天剪枝。' `
    -Force | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
Write-Host "已注册：$($t.TaskName)  状态=$($t.State)"
$t.Triggers | ForEach-Object { Write-Host "  触发：$($_.StartBoundary)" }
$t.Actions  | ForEach-Object { Write-Host "  动作：$($_.Execute) $($_.Arguments)" }
