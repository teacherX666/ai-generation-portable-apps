<#
  打包迁移工具：把当前仓库打成一个 git bundle（**不依赖 GitHub**）。

  用途：GitHub 连不上 / 不想等推送时，把代码搬到另一台机器。
  产物只有一个文件（本仓库代码约 20MB，不含 state/outputs 等生成数据）。

  用法：
      powershell -ExecutionPolicy Bypass -File tools\make_migration_bundle.ps1

  目标机器上（二选一）：
      # A) 该机器已有仓库（最常见：B 上已经在跑）
      git fetch "D:\portal-<日期>-<sha>.bundle" "refs/heads/*:refs/remotes/kit/*"
      git checkout -B main-local-20261013 kit/main-local-20261013

      # B) 全新克隆
      git clone "D:\portal-<日期>-<sha>.bundle" ai-generation-portable-apps
#>
param(
    [string]$Branch = "main-local-20261013",
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutDir) { $OutDir = Join-Path $repo "_migration" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Push-Location $repo
try {
    $head = (git rev-parse --short HEAD).Trim()
    $stamp = Get-Date -Format "yyyyMMdd-HHmm"
    $bundle = Join-Path $OutDir "portal-$stamp-$head.bundle"

    Write-Host "仓库: $repo"
    Write-Host "分支: $Branch   HEAD: $head"
    Write-Host "打包中（--all，自包含，目标机器不需要有历史）..."

    git bundle create $bundle --all 2>&1 | Out-Null
    git bundle verify $bundle 2>&1 | Out-Null

    $mb = [Math]::Round((Get-Item $bundle).Length / 1MB, 1)
    Write-Host ""
    Write-Host "[OK] 迁移包已生成: $bundle"
    Write-Host "     大小: $mb MB"
    Write-Host ""
    Write-Host "拷到目标机器后，在那台机器上执行："
    Write-Host "  git fetch `"<bundle 路径>`" `"refs/heads/*:refs/remotes/kit/*`""
    Write-Host "  git checkout -B $Branch kit/$Branch"
    Write-Host ""
    Write-Host "（全新机器：git clone `"<bundle 路径>`" ai-generation-portable-apps）"
    Write-Host ""
    Write-Host "迁移后三件事（详见 docs/migrate-to-another-machine.md）："
    Write-Host "  1) 配角色：DSH_LOCAL_LABEL / DSH_CENTER_URL（环境变量最省事）"
    Write-Host "  2) 确认本机没有任务在跑再重启（重启会重新入队重跑）"
    Write-Host "  3) 重启后核对 /healthz、端口、使用统计的来源列"
}
finally {
    Pop-Location
}
