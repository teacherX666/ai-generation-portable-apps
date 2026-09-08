#!/bin/bash
# 部署防呆：重启 RedCraft 前检查各子应用运行中任务（2026-09-04）。
# - 有运行中任务时默认拒绝（--force 跳过），避免更新把同事正在跑的任务打断。
# - 排队任务会由 jobs_backlog.json 自动恢复，无需等待；运行中任务重启后
#   会标记「服务更新中断」并给一键重试按钮。
# 用法：./tools/restart_safe.sh            # 检查，安全则重启
#       ./tools/restart_safe.sh --check    # 只检查不重启
#       ./tools/restart_safe.sh --force    # 有任务也重启
set -u

FORCE=0
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --check) CHECK_ONLY=1 ;;
  esac
done

APPS=(9090:Portal 8787:Seedance 8797:NanoBanana 8888:Dreamina 8891:Portrait 8893:Canvas 8896:Previz 8900:RAG)
ACTIVE_TOTAL=0
ACTIVE_DETAIL=""

for entry in "${APPS[@]}"; do
  port="${entry%%:*}"
  name="${entry#*:}"
  count=$(curl -s -m 4 "http://127.0.0.1:${port}/api/jobs" 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    jobs = d.get('jobs', d if isinstance(d, list) else [])
    if not isinstance(jobs, list):
        jobs = d.get('items', [])
    active = [j for j in jobs if isinstance(j, dict) and str(j.get('status', '')).lower()
              in ('queued', 'pending', 'running', 'querying', 'waiting_provider', 'uploading', 'submitting')]
    print(len(active))
except Exception:
    print(0)
" 2>/dev/null)
  count="${count:-0}"
  count=$((count + 0))
  ACTIVE_TOTAL=$((ACTIVE_TOTAL + count))
  if [ "$count" -gt 0 ]; then
    ACTIVE_DETAIL="${ACTIVE_DETAIL}  - ${name}: ${count} 个运行中\n"
  fi
done

if [ "$ACTIVE_TOTAL" -gt 0 ]; then
  echo "⚠️  当前有 ${ACTIVE_TOTAL} 个任务正在运行："
  printf "%b" "$ACTIVE_DETAIL"
  echo ""
  echo "重启会把它们标记为「服务更新中断」（可在任务卡上点重试）。"
  if [ "$FORCE" -eq 1 ]; then
    echo "→ --force：跳过确认，继续重启。"
  else
    echo "→ 已取消。等任务跑完再重启，或用 --force 强制重启。"
    exit 1
  fi
else
  echo "✅ 没有运行中的任务，可以安全重启。"
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  exit 0
fi

echo "→ 重启 com.ai-portal ..."
launchctl kickstart -k gui/$(id -u)/com.ai-portal
echo "→ 重启 com.feishu-generation-agent ..."
launchctl kickstart -k gui/$(id -u)/com.feishu-generation-agent
echo "→ 完成。等待几秒后访问 https://<局域网IP>:9090 验证。"
