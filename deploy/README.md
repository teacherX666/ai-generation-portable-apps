# 公网部署产物（easyq.cn）

完整方案与决策记录见 `docs/公网部署/01-迁移方案.md`。本目录是服务器侧的可执行产物。

| 文件 | 用途 |
|---|---|
| `init-server.sh` | 服务器初始化（Ubuntu 24.04，root 执行，幂等） |
| `sync-data.sh` | 从 Mac 推代码+数据到服务器 `/opt/ai-portal`（约 7G，可续传） |
| `ai-portal.service` | Portal + 全部子应用的 systemd 单元 |
| `feishu-generation-agent.service` | 飞书生成 Agent（8765） |
| `feishu-output-sync.service` | 飞书产出搬运 |
| `Caddyfile` | easyq.cn → 127.0.0.1:9090，自动 HTTPS |

## 首次部署顺序

1. **买服务器**（见方案 §2）：4C8G / 40G+100G / Ubuntu 24.04；安全组放 22（建议先限自己 IP）/80/443
2. **查备案**决定地域：HK（免备案）或大陆（1-4 周）
3. 从 Mac 推数据：`./deploy/sync-data.sh root@<IP>`
4. 服务器执行：`bash /tmp/init-server.sh`（先 scp 上去）
5. 核对 `portal/state/users.json` 的 `signup_enabled == false`，`systemctl start caddy ai-portal feishu-generation-agent feishu-output-sync`
6. 按方案 Phase 5 清单逐项验证（**含统计链路**），全过再切 DNS

## 待办：dreamina CLI 的 Linux 版

Mac 上 `~/.local/bin/dreamina` 是 Go 二进制（内嵌 bytedance/gopkg、cloudwego/hertz 模块），Linux 服务器需要对应构建。请找到当初下载 Mac 版的出处（即梦官方工具链或内部分发页），确认是否有 linux-x64 包；拿到后放 `/usr/local/bin/dreamina` 并 `chmod +x`，再跑 `dreamina` 冒烟验证登录态（`dreamina/accounts/` 已随 rsync 搬运）。

## 日常更新

- 服务器代码更新：`cd /opt/ai-portal && git pull && systemctl restart ai-portal`（或从 Mac 重跑 sync-data.sh 的代码部分）
- 证书：Caddy 自动续期，无需人工
- 日志：`journalctl -u ai-portal -f` 或 `/var/log/ai-portal/*.log`
