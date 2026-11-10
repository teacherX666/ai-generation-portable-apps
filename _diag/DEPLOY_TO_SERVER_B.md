# 服务器B 更新检查单（2026-10-14）

> B **正在对外服务**，所以按顺序做，别跳步。核心风险只有一个：
> **重启会打断在跑的任务，而恢复是「重新入队重跑」不是断点续传 → 可能重复计费。**

---

## 0. 更新前：备份 + 记下当前版本（2 分钟，必须做）

```powershell
cd <B 的仓库目录>

# 记下当前版本（回滚要用）
git rev-parse --short HEAD          # 抄下来，例如 af00e0b

# 备份运行态数据（代码同步不会碰 state/，但备份是底线）
Copy-Item portal\state\history.json            portal\state\history.json.bak-update -Force
Copy-Item portal\state\users.json              portal\state\users.json.bak-update -Force
Copy-Item portal\state\download_files.json     portal\state\download_files.json.bak-update -ErrorAction SilentlyContinue
Copy-Item portal\state\remote_sources.json     portal\state\remote_sources.json.bak-update -ErrorAction SilentlyContinue
```

## 1. 确认「没有任务在跑」再重启（最关键的一步）

B 上如果**没有**空闲判定脚本，直接用下面这段（跑在 B 上，纯 stdlib、无依赖）：
它读各子应用的 `activity_log.json`，有非终态任务就退出码 3。

```powershell
@'
import json, sys, time
from pathlib import Path
ROOT = Path(r".")   # 在 B 的仓库根目录执行
APPS = ["feishu-generation-agent", "seedance", "nano-banana", "dreamina",
        "volcengine-portrait", "infinite-canvas", "director", "previz",
        "rag-assistant", "bubble"]
TERMINAL = {"succeeded","failed","cancelled","canceled","interrupted","expired",
            "done","finished","error","timed_out","partial_success"}
busy = []
for app in APPS:
    for name in ("state/activity_log.json", "state/runs.json"):
        p = ROOT / app / name
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = d if isinstance(d, list) else (d.get("items") or d.get("jobs") or d.get("activities") or [])
        for it in items:
            if isinstance(it, dict) and str(it.get("status","")).lower() not in TERMINAL:
                busy.append((app, it.get("status"), it.get("created_at")))
print("非终态任务:", len(busy))
for b in busy[:5]:
    print("   ", b)
sys.exit(3 if busy else 0)
'@ | Set-Content -Encoding UTF8 _check_idle.py
python _check_idle.py ; echo "退出码=$LASTEXITCODE"   # 0 = 可以重启；3 = 有任务在跑，等
```

**要连续看到 0 两次 + 最后一次提交（`POST /api/jobs`）过去 2 分钟以上**再动手 ——
只看一次会踩到「刚判定空闲就有人提交」的坑（我们在 A 上被打断过同事的任务，两次）。

## 2. 更新代码

```powershell
git fetch teacherx
git checkout main-local-20261013        # 或 git pull 到该分支
git rev-parse --short HEAD             # 应为 b76c4e9（或更新）
```

`state/`、`outputs/`、`archives/`、`logs/` 都在 `.gitignore` 里 —— **不会覆盖 B 的数据** ✓

## 3. 写 B 自己的「来源」标签（必须，否则 B 会把自己标成「服务器A」）

```jsonc
// portal/state/remote_sources.json
{
  "local_label": "服务器B",
  "sources": []          // 空 = B 不去拉任何人（重要：两边互拉会重复计数）
}
```

## 4. 重启

按 B 自己的启动方式重启（看门狗拉起 / 计划任务 / 手动 `python app.py`）。
**重启 Portal 会连带换掉全部子应用**（8765 飞书、8787、8797、8891、8893、8895、8896、8900、8901、8903）。

> ⚠️ B 上如果也有多个仓库副本（A 上踩过：另一个目录的旧副本抢了 9090），
> 重启前先确认跑的是**你要更新的那个目录**：
> `Get-CimInstance Win32_Process -Filter "ProcessId=<pid>" | Select CommandLine,ParentProcessId`

## 5. 更新后核对（每条都要过）

```powershell
# 5.1 Portal 活着且是新版本行为（200 JSON，不是 302 到 /login）
curl.exe -sk https://127.0.0.1:9090/healthz

# 5.2 端口都在（应为 10 个左右）
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 9090,8765,8787,8797,8891,8893,8895,8896,8900,8901,8903 } |
  Select-Object LocalPort, OwningProcess | Sort-Object LocalPort
```

用 admin 登进 B 的网页，**使用统计 → 使用记录** 看三件事：

1. **「来源」列** = `服务器B`（不是「服务器A」、也不是「本机」）
2. **飞书的行带 token 与金额**（这是本次更新最主要的目的）——
   没有的话看第 6 节
3. **状态列**不再出现「待确认」（旧版会把飞书任务误标成待确认）

## 6. 飞书还是没 token？按这个顺序查

飞书 token 来自方舟账号，链路是：
`飞书 run 的 official_id（方舟任务号）→ 拉方舟任务列表取 usage → 按单价算钱`

1. **B 的方舟 key 可用吗** —— seedance 的行有没有 token/金额？
   有（我们实测 B 是 32 条里 31 条有）→ key 没问题，问题在飞书那一环；
2. **飞书 Agent 是新的吗** —— 新版 `/api/portal/history` 会多返回 `provider_task_ids`；
3. **等一次同步**：token 是靠 `ark_task_index()` 拉的（TTL 10 分钟），刷新页面多等一会儿；
4. 还不行就把 B 的 `portal/state/portal.out.log` 末尾 + 一条飞书行的字段发我。

## 7. 回滚（万一）

```powershell
git checkout <第 0 步记下的 SHA>
# 需要的话把三个 .bak-update 还原回 state/
# 再按第 4 步重启
```

---

## 千万别做的两件事

1. **别在有人跑任务时重启** —— 恢复是重跑，不是续跑，可能重复计费（A 上被打断过 3 次）。
2. **别让 B 也配 `sources` 去拉 A** —— A 已经在拉 B 了，双向会互相重复计一遍。

## 本次更新带来的变化（B 上会看到什么）

| 变化 | 说明 |
|---|---|
| 飞书行有 token / 金额 | 靠飞书 run 的方舟任务号去方舟取 usage |
| 「来源」列 服务器A / 服务器B | 本机标签读 `local_label` 配置 |
| 导出 CSV/Excel 带「来源」列 | 本机行写「服务器A」（B 上写「服务器B」） |
| 状态不再显示「待确认」 | 飞书的 waiting_review/done 统一成「成功」 |
| 人像卡片不再下载整段视频 | 一屏曾达 331MB，改用服务端抽帧（页面快 100 倍以上） |
| 人像下载端点支持 Range | `Range: bytes=0-1023` 现在返回 206，不再回整个文件 |
| 绿幕归一已下线（默认关） | 你反馈「不准确、一直闪」，且曾丢音轨 → 流水线不再自动改动成片 |
| 用户口径自动记忆 | 你反复提的要求会自动沉淀，说 3 次后注入后续规划 |

> 依赖提示：绿幕归一那套工具仍保留但**已不在流水线里**，所以 B **不需要**装 numpy/cv2。
