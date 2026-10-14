# 迁移 / 部署到另一台机器

本文覆盖两件事：**怎么把代码搬过去**（含 GitHub 不通时的办法）、**搬过去要配什么**。
两台机器跑**同一份代码**，靠**角色配置**区分：一台是「中心」（存全部记录），其余是「查询端」。

---

## 一、代码怎么搬

### 方式 1：走 GitHub（正常情况）

```bash
git fetch teacherx
git checkout main-local-20261013        # 部署分支
git rev-parse --short HEAD              # 记下来，回滚要用
```

> 如果 `git fetch` 报 `Failed to connect to github.com port 443`：本机 `hosts` 里钉了旧 IP。
> 管理员 PowerShell 改一行（或直接删掉那一行）：
> ```powershell
> $h = 'C:\Windows\System32\drivers\etc\hosts'
> (Get-Content $h) -replace '140\.82\.112\.3\s+github\.com', '20.205.243.166 github.com' | Set-Content $h -Encoding ASCII
> ```

### 方式 2：git bundle（GitHub 不通时，实测可用）

在**源机器**上：

```powershell
powershell -ExecutionPolicy Bypass -File tools\make_migration_bundle.ps1
# 产物：_migration\portal-<日期>-<sha>.bundle （只有代码，约 20MB；不含 state/outputs）
```

把 `.bundle` 文件拷到**目标机器**，然后：

```bash
# 目标机器已有仓库（B 上正在跑的情形）
git fetch "D:\portal-20261014-1430-b76c4e9.bundle" "refs/heads/*:refs/remotes/kit/*"
git checkout -B main-local-20261013 kit/main-local-20261013

# 全新机器
git clone "D:\portal-20261014-1430-b76c4e9.bundle" ai-generation-portable-apps
```

bundle 是 `--all` 自包含的：目标机器无论停在哪一版都能 fetch。

> `state/`、`outputs/`、`archives/`、`logs/`、`certs/` 都在 `.gitignore` 里，
> **迁移不会覆盖目标机器已有的数据** ✓

---

## 二、角色配置（两台的区别只有这一处）

| 角色 | 谁 | 行为 |
|---|---|---|
| **中心（hub）** | 服务器A | 存两台的记录；自己不去问谁；使用统计直接算本机 |
| **查询端** | 服务器B | 不存汇总；使用统计（管理员）直接转发去问中心 |

### 推荐：用环境变量（跟启动脚本走，不用碰任何 JSON）

写在各自的启动脚本 / 看门狗 / 计划任务里：

**服务器A（中心）** — 只要标签，不要 center：
```powershell
$env:DSH_LOCAL_LABEL = "服务器A"
```

**服务器B（查询端）** — 标签 + 指向中心：
```powershell
$env:DSH_LOCAL_LABEL     = "服务器B"
$env:DSH_CENTER_URL      = "https://192.168.30.116:9090"
$env:DSH_CENTER_USER     = "center"
$env:DSH_CENTER_PASSWORD = "……"      # 中心机器上的账号（需管理员角色，才能读全员记录）
```

### 或者写配置文件（`portal/state/remote_sources.json`，本机文件、gitignored）

```jsonc
// 服务器B
{
  "local_label": "服务器B",
  "sources": [],                       // 空 = 不去拉任何人（别和中心互拉，会重复计数）
  "center": {
    "url": "https://192.168.30.116:9090",
    "username": "center",
    "password": "……"
  }
}
```

```jsonc
// 服务器A：只配来源 + 标签，**不要**配 center（自己是中心）
{
  "local_label": "服务器A",
  "sources": [ { "name": "服务器B", "base_url": "https://192.168.30.56:9090",
                 "username": "admin", "password": "……", "enabled": true } ],
  "days": 30,
  "interval_minutes": 10
}
```

取值优先级：**环境变量 > 配置文件 > 默认值**。

### 中心账号

中心机器上需要一个**管理员**账号给查询端用（读全员记录需要管理员角色）。
本机现状：用户名 `center`。密码可用界面「系统与权限 → 用户管理」改，或让我重置。
它在查询端的 `state/` 里（gitignored），不会进仓库。

---

## 三、部署步骤（目标机器上）

1. **备份**（底线，代码迁移不会碰这些，但先备份不吃亏）
   ```powershell
   Copy-Item portal\state\history.json portal\state\history.json.bak-update -Force
   Copy-Item portal\state\users.json   portal\state\users.json.bak-update -Force
   ```
2. **确认没有任务在跑**（重启会把未完成的任务**重新入队重跑**，可能重复计费）
   - 判据：各子应用 `state/activity_log.json` 里没有非终态任务；
   - 要**连续两次看到空闲 + 最近 2 分钟没有新的 `POST /api/jobs`** 才动手。
3. **搬代码**（第一节的方式 1 或 2）
4. **配角色**（第二节，环境变量最省事）
5. **重启**（Portal 重启会连带换掉全部子应用）
6. **核对**
   ```powershell
   curl.exe -sk https://127.0.0.1:9090/healthz      # 200 {"ok": true, "service": "portal"}
   Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 9090,8765,8787,8797,8891,8893,8895,8896,8900,8901,8903 }
   ```
   然后登录网页看 **使用统计 → 使用记录**：
   - 查询端（B）：来源列应出现 `服务器A` 与 `服务器B` 两种值（数据来自中心）
   - 中心（A）：`服务器B` 的记录带 token 与金额
   - 状态列不再出现「待确认」

## 四、回滚

```bash
git checkout <第 1 步记下的 SHA>
# 需要的话把 .bak-update 还原回 state/
# 再重启一次
```

---

## 五、两条别踩的线

1. **别让中心反过来去拉查询端**（`sources` 留空）——两边互拉会把对方的记录重复计一遍。
2. **别在有任务在跑时重启** —— `recover_backlog()` 是**重新入队重跑**，不是断点续传。

## 六、排查

| 现象 | 先看什么 |
|---|---|
| 查询端页面还是只有本机记录 | 是不是**管理员**登录（只对管理员转发）；`portal.out.log` 里有没有 `[center] forward failed` |
| 中心不可达 | 查询端会自动回退本机数据（不会白屏），日志里有 `[center] forward failed: …` |
| 飞书行没有 token | 飞书任务用的是**中心的方舟账号**（实测），所以 token 只在中心侧算得出来；查询端看到的是中心算好的数据 |
| 来源列显示错的名字 | `DSH_LOCAL_LABEL` / `local_label` 配错了 |
