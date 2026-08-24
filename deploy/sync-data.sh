#!/usr/bin/env bash
# 在 Mac 上执行：把仓库（代码+数据，约 7G）推到服务器 /opt/ai-portal
# 用法：./deploy/sync-data.sh root@<服务器公网IP> [SSH密钥路径]
# 幂等 + 断点续传（--partial），跑在非高峰时段
set -euo pipefail

SERVER="${1:?用法: ./deploy/sync-data.sh root@<服务器公网IP> [SSH密钥路径]}"
RSH="ssh"
if [ -n "${2:-}" ]; then RSH="ssh -i $2"; fi
SRC="/Users/260413a/ai-generation-portable-apps/"

EXCLUDES=(
  --exclude '.git'
  --exclude '.venv'                        # Mac 的 venv 不能在 Linux 用，服务器自建
  --exclude 'feishu-generation-agent/.venv'
  --exclude '.claude'
  --exclude '__pycache__'
  --exclude '*.pyc'
  --exclude 'state/workspaces'             # 草稿缓存（seedance 9.1G + nano-banana 7.7G，可弃）
  --exclude 'state/certs'                  # Mac 自签证书目录
  --exclude 'portal/state/portal.pem'
  --exclude 'portal/state/portal.key'
  --exclude 'logs'
  --exclude 'release'
)

echo "→ 同步 $SRC 到 $SERVER:/opt/ai-portal/ （约 7G，可 Ctrl-C 后重跑续传）"
rsync -avh --progress --partial "${EXCLUDES[@]}" -e "$RSH" "$SRC" "$SERVER:/opt/ai-portal/"
echo "✓ 完成。注意：dreamina CLI 是 Go 二进制，需另找 Linux 版（见 deploy/README.md）"
