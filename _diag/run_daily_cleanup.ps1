# 每日自动清理的包装脚本（由计划任务 "AI Generation Daily Cleanup" 调用）
#
# 为什么要有包装：计划任务里直接写 python 的话输出没人看、出错了也不知道。
# 这里把 stdout/stderr 追加到 _diag\daily_cleanup.log，方便回溯"哪天删了什么"。
#
# 策略（用户 2026-10-21 口径："7 天自动清理"；2026-11-04 收紧 workspaces 到 14 天）：
#   --outputs-retention 7  outputs 产物超 7 天进入回收站
#   --trash-days 7         回收站里的备份再留 7 天后彻底删除
#   --workspace-days 14    草稿参考素材：preset.json 超 14 天未编辑才清（原 30 天）
#   --apply                真执行（不加就是 dry-run）
#
# 注：C 盘剩余 < 25GB 时工具会自动把上面两项收紧到 outputs 3 天 / 回收站 1 天
#     （cleanup_daily.py 的 tighten_for_low_disk），2026-11-04 起长期处于该水位。
#
# 安全底线（工具自带）：不需要的文件才删；未与飞书同步的先进回收站；
#   usage.json / activity_log.json / users.json 一律不碰；今天的产物一律跳过。
#
# ⚠️ 本文件必须存为 UTF-8 **带 BOM**：计划任务用 powershell.exe(5.1) 执行，
#    无 BOM 时它会按 GBK 解码中文注释，双字节字符会吞掉紧跟的 } 或引号 → 整脚本解析失败
#    （2026-11-04 实际踩过：加了中文注释后报 "Missing closing '}'"）。

$ErrorActionPreference = 'Continue'

$RepoRoot = 'C:\Users\123\Documents\LTX\ai-generation-portable-apps'
$Python   = 'C:\Users\123\AppData\Local\Programs\Python\Python312\python.exe'
$Tool     = Join-Path $RepoRoot 'tools\cleanup_daily.py'
$Log      = Join-Path $RepoRoot '_diag\daily_cleanup.log'

# 控制台/重定向都用 UTF-8，避免 GBK 把中文打成乱码（也避免 emoji 触发 UnicodeEncodeError）
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

if (-not (Test-Path -LiteralPath $Tool)) {
    Add-Content -LiteralPath $Log -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] 找不到 $Tool，跳过" -Encoding UTF8
    exit 1
}

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -LiteralPath $Log -Value "`n===== $stamp 开始 =====" -Encoding UTF8

& $Python $Tool --apply --outputs-retention 7 --trash-days 7 --workspace-days 14 2>&1 |
    ForEach-Object { $_.ToString() } |
    Add-Content -LiteralPath $Log -Encoding UTF8

Add-Content -LiteralPath $Log -Value "[$((Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))] 结束（exit=$LASTEXITCODE）" -Encoding UTF8

# 日志自身也做大小保护：超过 5MB 只留尾部 1MB
try {
    $item = Get-Item -LiteralPath $Log -ErrorAction Stop
    if ($item.Length -gt 5MB) {
        $tail = Get-Content -LiteralPath $Log -Tail 2000
        Set-Content -LiteralPath $Log -Value $tail -Encoding UTF8
    }
} catch { }