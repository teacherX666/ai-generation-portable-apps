#!/usr/bin/env bash
# 服务器初始化脚本 — Ubuntu 24.04 LTS，root 执行，幂等可重跑
# 前置：仓库已就位 /opt/ai-portal（deploy/sync-data.sh 或 git clone）
# 执行：scp deploy/init-server.sh root@<IP>:/tmp/ && ssh root@<IP> 'bash /tmp/init-server.sh'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

REPO_ROOT=/opt/ai-portal

echo "== 1/7 基础包 + 时区（飞书日报调度依赖本地时间）"
apt-get update -qq
apt-get install -y -qq git rsync curl ufw wget software-properties-common
timedatectl set-timezone Asia/Shanghai

# Ubuntu 22.04 不自带 python3.12 → deadsnakes PPA；24.04 自带则跳过
if ! command -v python3.12 >/dev/null 2>&1; then
  echo "== 1b python3.12（deadsnakes PPA，针对 Ubuntu 22.04）"
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -qq
  apt-get install -y -qq python3.12 python3.12-venv
fi

echo "== 2/7 Caddy（官方源，systemd 托管）"
apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https ca-certificates
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt-get update -qq
apt-get install -y -qq caddy

echo "== 3/7 仓库根 venv（portal 经 .venv/bin/uvicorn 拉起各子应用，必须在仓库根）"
python3.12 -m venv "$REPO_ROOT/.venv"
"$REPO_ROOT/.venv/bin/pip" install -q --upgrade pip
"$REPO_ROOT/.venv/bin/pip" install -q -r "$REPO_ROOT/requirements.txt"

echo "== 4/7 feishu-generation-agent 独立 venv（pyproject 可编辑安装）"
python3.12 -m venv "$REPO_ROOT/feishu-generation-agent/.venv"
"$REPO_ROOT/feishu-generation-agent/.venv/bin/pip" install -q --upgrade pip
"$REPO_ROOT/feishu-generation-agent/.venv/bin/pip" install -q -e "$REPO_ROOT/feishu-generation-agent"

echo "== 5/7 dreamina CLI（Linux 版）—— 手动步骤"
# Mac 上 ~/.local/bin/dreamina 是 Go 二进制（bytedance/cloudwego 系），Linux 需对应构建。
# 从 Mac 版下载出处找 linux-x64 包，放到 /usr/local/bin/dreamina 并 chmod +x。
echo "   TODO: 手动安装 dreamina CLI 的 Linux 版（见 deploy/README.md）"

echo "== 6/7 防火墙（安全组之外的第二层；安全组同样只放 22/80/443）"
ufw default deny incoming
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "== 7/7 systemd 单元 + Caddy"
cp "$REPO_ROOT/deploy/ai-portal.service" /etc/systemd/system/
cp "$REPO_ROOT/deploy/feishu-generation-agent.service" /etc/systemd/system/
cp "$REPO_ROOT/deploy/feishu-output-sync.service" /etc/systemd/system/
cp "$REPO_ROOT/deploy/Caddyfile" /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable ai-portal feishu-generation-agent feishu-output-sync caddy

echo "✓ 初始化完成。下一步："
echo "  1) 核对 $REPO_ROOT/portal/state/users.json 的 signup_enabled 为 false"
echo "  2) systemctl start caddy ai-portal feishu-generation-agent feishu-output-sync"
echo "  3) systemctl status ai-portal 确认启动；journalctl -u ai-portal -f 看日志"
echo "  4) 按 docs/公网部署/01-迁移方案.md Phase 5 做端到端验证"
