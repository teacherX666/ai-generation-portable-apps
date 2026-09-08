# （已归档）待修复隐患清单 —— 2026-09-07 归档

> 本文档 2026-06-25 建立，多数条目已被后续提交覆盖或过期：
> - #15 `_proxy()` 阻塞 → 已由 X-Job-Id 响应头方案根治（2026-06-25 fix plan）+ TLS 握手移出主线程（2428385）+ 轮询渲染去重（46b98dc）
> - 注册门槛/开关 → 见 `docs/security-hardening-v2-plan.md` 与当前 auth 实现
> - 安全重启 → `tools/restart_safe.sh`；launchd 部署文档 → `docs/deployment.md`
> 保留原文仅供参考，勿再作为待办清单使用。

---
# 待修复隐患清单（pending-fixes）

记录截至 2026-06-25 已发现但**暂未动手**的隐患。当前线上环境对外可用，本文档作为后续维护窗口的待办列表。

---

## 任务总览

| # | 任务 | 优先级 | 是否需重启 Portal | 当前状态 |
|---|---|---|---|---|
| #15 | 修 `_proxy()` 同步阻塞导致 Portal 假死 | **P0** | 是 | 反复发生 |
| #9 | 注册开关 UI（admin 可关闭新用户注册） | P1 | 否 | 待做 |
| #12 | 登录页注册门槛提示 | P1 | 否 | 待做 |
| #13 | 清理测试注册用户 | P1 | 否 | 待做 |
| #8 | 修复 VPN 抢路由导致 LAN IP 选错 | P2 | 是 | 仅重启时触发 |
| #10 | 修复重启 Portal 时子应用孤儿端口循环 | P2 | 是 | 仅手动 kickstart 触发 |
| #14 | 写「安全重启」检查脚本 | P3 | 否 | 流程化 |
| #11 | 写 launchd 部署文档 | P3 | 否 | 文档化 |

---

## P0 — Portal 假死（高频复发）

### #15 修 `_proxy()` 同步阻塞导致 Portal 假死

**症状**
- Portal 9090 端口 LISTEN 在，TCP 握手卡死，curl 5 秒 timeout
- 浏览器表现「连登录页都打不开」「网站连不上」
- 几分钟后**自动恢复**
- 子应用本身没死（日志在持续 200 OK）

**触发频率**：今日已两次复发（一次同事注册风暴期间、一次同事使用中）

**实测病例（2026-06-25 10:16）**
```
ps -M PID 33644：6 个线程，主线程 sample 死在 select.poll
Portal 持有 ESTABLISHED 到 127.0.0.1:8787（seedance），FD 13
9090 上有 192.168.30.5:9090->192.168.30.83:49453 (CLOSE_WAIT)
  ↑ 客户端早断了，Portal 还没释放
9090 LISTEN 在，但 accept 无人响应（线程被卡死的代理请求占住）
```

**根因**
1. `portal/app.py` 的 `_proxy()` 创建 `http.client.HTTPConnection` 时**没有设置 timeout**
2. CLAUDE.md 提到 `_proxy()` 会 `read full response body` 来抽 job_id（见任务创建路径），同步阻塞
3. ThreadingHTTPServer 工作线程数有限，所有线程被多条慢请求卡住后，新 SYN 无人 accept
4. 卡死的请求**对应 socket 不释放**——FD 12（CLOSE_WAIT）、FD 13（ESTABLISHED）至今还挂着，FD 越积越多

**修复方向**
1. `http.client.HTTPConnection(host, port, timeout=60)` 加显式超时
2. 读响应循环加 socket 超时
3. 异常时主动 `conn.close()` + `self.close_connection = True` 释放 FD
4. 进阶：把 `_proxy()` 改成 `selectors`-based 双向 splice，避免 buffer 整个 body

**临时缓解（无需重启）**
- 卡死时**等几分钟**会自动恢复（已两次观察）
- 让同事错峰提交任务，避免并发高峰
- 主动关闭浏览器卡死标签可能让 socket 早些释放

**影响**
- 修复需重启 Portal → 杀掉所有 in-memory 任务
- 在同事下班/无 running jobs 时机操作

---

## P1 — 注册无门槛（一次性治理）

### #9 注册开关 UI（admin 可关闭新用户注册）

**症状**
- 2026-06-25 09:58~10:01 三分钟内涌入 9 个新用户
- 注册端点 `/api/auth/register` 只要 `signup_enabled=True` 就允许任何人注册成 user 角色

**根因**
- 后端 `/api/auth/signup-toggle` 已存在但前端没暴露 UI
- `signup_enabled` 默认 `True`（`portal/app.py:262`：`return bool(self._load_users().get("signup_enabled", True))`）

**修复**
- 在 admin 主页加一个「允许新用户注册」开关
- 绑定 GET `/api/auth/first-run` 返回的 `signup_enabled` 字段
- POST `/api/auth/signup-toggle` 切换
- 或后端默认改 `False`（更安全但首次部署要文档化）

**影响**：纯前端改动 + state JSON 字段切换，**不需要重启**，热生效

---

### #12 登录页注册门槛提示

**症状**
- login.html 的注册流程没有任何门槛——任何人在局域网拿到 IP 都能注册成普通用户、消耗 admin 配的火山方舟统一 key

**修复**
- 把首次登录页 signup 链接默认隐藏（v-if `signupEnabled`）
- 加一行说明「新账号需联系管理员开通」
- 配合 #9 的 signup-toggle UI 一起做

**影响**：纯前端改动，不需要重启

---

### #13 清理测试注册用户

**症状**
- 今日注册的 9 个用户里有明显测试账号：`1234`、`123456789`、`13613107392`
- 这些账号有 session、可登录、可消耗资源

**修复**
- admin 在 UI 里删掉（`/api/users/<id>` DELETE 走 manage_users 权限）
- 或 admin 先发名单确认哪些保留
- 同时要思考：未来注册是否需要实名/工号审核流程

**影响**：state JSON 改动，不需要重启

---

## P2 — 启动期问题（重启时才触发）

### #8 修复 VPN 抢路由导致 LAN IP 选错

**症状**
- `get_lan_ip()` 用 `socket.connect(8.8.8.8, 80)` 拿出口 IP
- 当 VPN（GoGoJumpVPN/Tailscale 等）抢默认路由时，选到 240.0.0.1（GoGoJump）/100.64/10（Tailscale）等虚拟段
- 控制台通告错误的 LAN URL，证书 SAN 也写错
- 实测当前 `socket.connect(8.8.8.8) → 240.0.0.1`，下次重启就触发

**根因**
- `portal/app.py:61-83` 的 `get_lan_ip()` 没过滤 VPN 虚拟段

**修复方向**
1. 跳过 240.0.0.0/4、100.64.0.0/10、169.254.0.0/16
2. 优先走 `ifconfig`/socket 拿 192.168/10/172 私有段
3. 启动时打印警告
4. 同步处理 `ensure_certs()` 的 SAN

**影响**：必须重启 Portal → 杀掉 in-memory 任务，安排同事下班后

---

### #10 修复重启 Portal 时子应用孤儿端口循环

**症状**
- 手动 `launchctl kickstart -k` 重启时，旧 Portal 子进程（seedance/nano-banana/dreamina/volcengine-portrait）有时不会跟主进程一起死
- 新 Portal 起来后子应用 bind 8787/8797/8888/8891 报 `[Errno 48] Address already in use` 循环 50+ 次
- 实测 ai-portal.log 里有大段 `[watchdog] nano-banana exited (code 1), restarting`

**根因**
- Portal 主进程退出时没主动 SIGTERM 子进程并 wait
- 子应用 socket 没设 SO_REUSEADDR
- 当前 `_kill_port_squatter` 不够稳

**修复方向**
1. Portal 主进程退出时 atexit/signal handler 里 SIGTERM 所有子进程并 wait
2. 子应用 socket 加 SO_REUSEADDR
3. 启动前更激进地清残留

**验证**：`launchctl kickstart -k com.ai-portal` 后查 ai-portal.log 有无 `Address already in use`

**影响**：必须重启 Portal，跟 #8 一起做

---

## P3 — 文档/治理（无副作用）

### #14 写「安全重启」检查脚本

**目的**：把每次手动重启的踩坑流程固化

**脚本检查项**
1. `curl /api/platform/activity` 看最近 5 分钟有没有人活动
2. 各子应用 `/api/jobs` 看有无 running 任务
3. 钉钉/微信群通知预计停机时长
4. 关 VPN 后再重启避免 240.0.0.1 被写进 lan_ip.txt 和证书 SAN
5. 重启完手工 `curl https://192.168.30.5:9090/login` 验 302
6. 检查 8787/8797/8888/8891 端口正常

**位置**：`portal/state/scripts/safe_restart.sh`

**影响**：新增脚本文件，不需要重启

---

### #11 写 launchd 部署文档

**位置**：仓库根目录 `docs/deployment.md` 或更新 `CLAUDE.md`

**内容要点**
1. Portal 由 `~/Library/LaunchAgents/com.ai-portal.plist` 守护，**不是**双击 `启动器.command`
2. 千万别再加 `PORTAL_HTTP_ONLY=1`（除非要切回 cloudflared 模式）
3. 重启用 `launchctl kickstart -k gui/$(id -u)/com.ai-portal`
4. cloudflared tunnel plist 已 unload，需要外网时再 load
5. 重启前先关 VPN 避免 240.0.0.1 污染
6. 重启会杀掉所有 in-memory jobs，挑同事下班时机
7. plist 里的 Python 路径是 `/usr/bin/python3`（系统 3.9），不是 homebrew 3.12

**影响**：纯文档

---

## 已澄清不需要修的项

- ❌ **同事注册风暴 ≠ 你打不开网站** —— 真因是手动重启时 plist 里 `PORTAL_HTTP_ONLY=1` 残留让 Portal 跑 HTTP 模式，浏览器 https:// 标签遇到 ERR_SSL_PROTOCOL_ERROR
- ❌ **plist 当前没有 `PORTAL_HTTP_ONLY`** —— 已清干净
- ❌ **cloudflared** —— 已 launchctl unload

---

## 推荐修复顺序

### 立即可做（不影响线上）
1. **#13 清理测试用户**（state JSON 改动）
2. **#11 写部署文档**（防再次踩坑）
3. **#14 写安全重启脚本**（为后续重启做准备）

### 同事使用空隙（半小时内可做）
4. **#9 注册开关 UI** + **#12 登录页门槛提示**（前端改动 + 新增 admin 控制）

### 下一次维护窗口（同事下班/周末）
5. **#15 修 _proxy 阻塞** + **#8 修 VPN 抢路由** + **#10 修孤儿端口**（一次重启搞定三个）

---

## 操作禁忌

- ❌ 不要在同事正在跑 seedance/nano-banana 任务时手动重启 Portal
- ❌ 不要在 VPN 开启状态下重启 Portal（lan_ip.txt 会被写成 240.0.0.1）
- ❌ 不要用 `--no-verify` `--force` 等绕过机制的标志
- ❌ 修 `_is_admin` 守卫前确认前端 `v-if="isAdmin"` 已同步放权（参考 [[dreamina-is-local-gate]] 记忆）
