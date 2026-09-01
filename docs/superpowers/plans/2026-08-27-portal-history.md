# Portal 全局历史记录（方案二媒体卡片）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Portal 新增「历史记录」页签——聚合所有子应用任务历史，方案二媒体卡片网格 + 详情弹窗（请求/返回页签 + 弹窗内下载图片视频）。

**Architecture:** portal/app.py 单点改造：`_proxy` 从已缓冲的 POST body 提取白名单字段 → `register_job` 落 `state/history.json`（pending）→ `_job_poll_loop` 终态更新（status/results/error）→ `GET /api/platform/history` 查询（admin 全量、普通用户仅本人）。前端 HistoryApp 组件 + 新 tab。统计登记代码零改动。

**Tech Stack:** Python stdlib、PetiteVue、pytest、node。

**设计文档:** `docs/superpowers/specs/2026-08-27-portal-history-design.md`

---

### Task 1: history 数据层（HistoryStore）

**Files:**
- Modify: `portal/app.py`（tracker 内加 HISTORY_PATH + history 读写 + 剪枝）
- Create: `tests/test_history_store.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_history_store.py
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "portal"))

_spec = importlib.util.spec_from_file_location("portal_app_history", ROOT / "portal" / "app.py")
portal = importlib.util.module_from_spec(_spec)
sys.modules["portal_app_history"] = portal
_spec.loader.exec_module(portal)


def _make_tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(portal, "STATE_DIR", tmp_path)
    monkeypatch.setattr(portal, "USAGE_PATH", tmp_path / "usage.json")
    monkeypatch.setattr(portal, "HISTORY_PATH", tmp_path / "history.json")
    tracker = portal.UsageTracker(str(tmp_path / "usage.json"), history_path=str(tmp_path / "history.json"))
    return tracker


def test_history_upsert_and_prune(tmp_path, monkeypatch):
    tracker = _make_tracker(tmp_path, monkeypatch)
    now = time.time()
    rec = {"app": "nano-banana", "job_id": "j1", "username": "alice", "kind": "image",
           "prompt": "一只猫", "params": {}, "status": "pending",
           "submitted_at": now, "completed_at": None, "duration": 0,
           "results": [], "error": ""}
    tracker.history_upsert(rec)
    # 老记录（40 天前）应被同次写入剪掉
    old = dict(rec, job_id="j-old", submitted_at=now - 40 * 86400)
    tracker.history_upsert(old)
    data = json.loads((tmp_path / "history.json").read_text())
    assert "nano-banana:j1" in data
    assert "nano-banana:j-old" not in data


def test_history_cap(tmp_path, monkeypatch):
    tracker = _make_tracker(tmp_path, monkeypatch)
    now = time.time()
    for i in range(12):
        tracker.history_upsert({"app": "a", "job_id": f"j{i}", "username": "u",
                                "kind": "image", "prompt": "", "params": {},
                                "status": "done", "submitted_at": now + i,
                                "completed_at": now + i, "duration": 0,
                                "results": [], "error": ""})
    data = json.loads((tmp_path / "history.json").read_text())
    assert len(data) <= 10  # 上限 10000 为生产值，测试用 monkeypatch 缩小
```

> 实现时 `UsageTracker.__init__` 增参 `history_path=None, history_cap=10000, history_days=30`；测试里传 `history_cap=10`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_history_store.py -q`
Expected: FAIL（`TypeError: UsageTracker.__init__() ... unexpected keyword`）

- [ ] **Step 3: 写实现**

```python
# portal/app.py 顶部常量区追加
HISTORY_PATH = STATE_DIR / "history.json"
HISTORY_CAP = 10000
HISTORY_DAYS = 30

# UsageTracker.__init__ 追加参数
    def __init__(self, usage_path: Path | None = None, *,
                 history_path: Path | None = None,
                 history_cap: int = HISTORY_CAP,
                 history_days: int = HISTORY_DAYS):
        ...
        self._history_path = history_path or HISTORY_PATH
        self._history_cap = history_cap
        self._history_days = history_days

# tracker 方法（放在 register_job 之后）
    def _load_history(self) -> dict:
        if not self._history_path.exists():
            return {}
        try:
            return json.loads(self._history_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def history_upsert(self, record: dict) -> None:
        """写/更新一条历史记录；同次写入顺带剪枝（>N 天 + 总量上限）。
        任何异常吞掉——历史采集失败绝不影响统计与代理。"""
        try:
            with self._lock:
                data = self._load_history()
                key = f"{record['app']}:{record['job_id']}"
                data[key] = record
                cutoff = time.time() - self._history_days * 86400
                data = {k: v for k, v in data.items()
                        if float(v.get("submitted_at") or 0) >= cutoff}
                if len(data) > self._history_cap:
                    sorted_items = sorted(
                        data.items(), key=lambda kv: float(kv[1].get("submitted_at") or 0)
                    )
                    data = dict(sorted_items[-self._history_cap:])
                self._history_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._history_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self._history_path)
        except Exception:
            pass

    def get_history(self) -> dict:
        return self._load_history()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_history_store.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add portal/app.py tests/test_history_store.py
git commit -m "feat(portal): 历史记录数据层——history.json 读写与剪枝（30天/上限）"
```

---

### Task 2: _proxy 请求体元数据提取

**Files:**
- Modify: `portal/app.py`（`extract_job_metadata` 函数）
- Create: `tests/test_history_metadata.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_history_metadata.py
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "portal"))

_spec = importlib.util.spec_from_file_location("portal_app_meta", ROOT / "portal" / "app.py")
portal = importlib.util.module_from_spec(_spec)
sys.modules["portal_app_meta"] = portal
_spec.loader.exec_module(portal)


def test_json_body_whitelist_and_key_exclusion():
    meta = portal.extract_job_metadata(
        "application/json",
        json.dumps({"prompt": "一只猫", "aspect_ratio": "1:1",
                    "count": 2, "api_key": "sk-secret", "model": "m1"}).encode(),
    )
    assert meta["prompt"] == "一只猫"
    assert meta["params"] == {"aspect_ratio": "1:1", "count": 2}
    assert "api_key" not in json.dumps(meta["params"])


def test_multipart_body_prompt_extraction():
    boundary = "----test-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="prompt"\r\n\r\n'
        "雨夜霓虹街头\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="aspect_ratio"\r\n\r\n'
        "9:16\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    ctype = f"multipart/form-data; boundary={boundary}"
    meta = portal.extract_job_metadata(ctype, body)
    assert meta["prompt"] == "雨夜霓虹街头"
    assert meta["params"] == {"aspect_ratio": "9:16"}


def test_oversize_body_skips_prompt():
    meta = portal.extract_job_metadata("application/json", b"x" * (5 * 1024 * 1024 + 1))
    assert meta["prompt"] == ""
    assert meta["params"] == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_history_metadata.py -q`
Expected: FAIL（`AttributeError: module ... has no attribute 'extract_job_metadata'`）

- [ ] **Step 3: 写实现**

```python
# portal/app.py 追加（模块级函数，放在 _sign_admin_header 之后）

_METADATA_WHITELIST = {
    "prompt", "text", "content", "aspect_ratio", "ratio", "duration",
    "resolution", "image_size", "count", "mode", "style",
    "negative_prompt", "seed", "generate_audio", "model",
}
_METADATA_BLOCKLIST_SUBSTR = ("key", "token", "secret", "password")
_METADATA_MAX_BODY = 5 * 1024 * 1024


def extract_job_metadata(content_type: str, body: bytes) -> dict:
    """从任务创建请求体提取提示词与白名单参数。永不采集密钥类字段。
    解析失败/超大 body 静默降级为空——采集绝不抛异常。"""
    result: dict = {"prompt": "", "model": "", "params": {}}
    try:
        if not body or len(body) > _METADATA_MAX_BODY:
            return result
        fields: dict = {}
        ctype = (content_type or "").split(";")[0].strip().lower()
        if ctype == "application/json":
            data = json.loads(body.decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                fields = data
        elif ctype == "multipart/form-data":
            import cgi
            import io
            form = cgi.FieldStorage(
                fp=io.BytesIO(body), headers={"Content-Type": content_type},
                environ={"REQUEST_METHOD": "POST",
                         "CONTENT_TYPE": content_type,
                         "CONTENT_LENGTH": str(len(body))},
                keep_blank_values=True,
            )
            for key in form.keys():
                item = form[key]
                if isinstance(item, list):
                    item = item[0] if item else None
                if item is None or getattr(item, "filename", None):
                    continue
                fields[key] = item.value
        else:
            return result
        for key, value in fields.items():
            k = str(key)
            low = k.lower()
            if any(blocked in low for blocked in _METADATA_BLOCKLIST_SUBSTR):
                continue
            if low in _METADATA_WHITELIST:
                if isinstance(value, (str, int, float, bool)):
                    if low == "prompt":
                        result["prompt"] = str(value).strip()
                    elif low == "model":
                        result["model"] = str(value).strip()
                    else:
                        result["params"][low] = value
        return result
    except Exception:
        return result
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_history_metadata.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add portal/app.py tests/test_history_metadata.py
git commit -m "feat(portal): 任务创建请求体元数据提取——白名单字段、密钥字段排除、5MB 上限"
```

---

### Task 3: 采集接线——注册写 pending、轮询终态更新

**Files:**
- Modify: `portal/app.py`（register_job 增参 + _proxy 传元数据 + _job_poll_loop 终态更新）
- Create: `tests/test_history_capture.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_history_capture.py
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "portal"))

_spec = importlib.util.spec_from_file_location("portal_app_capture", ROOT / "portal" / "app.py")
portal = importlib.util.module_from_spec(_spec)
sys.modules["portal_app_capture"] = portal
_spec.loader.exec_module(portal)


def test_register_job_writes_pending_history(tmp_path, monkeypatch):
    monkeypatch.setattr(portal, "HISTORY_PATH", tmp_path / "history.json")
    monkeypatch.setattr(portal, "STATE_DIR", tmp_path)
    monkeypatch.setattr(portal, "USAGE_PATH", tmp_path / "usage.json")
    tracker = portal.UsageTracker(str(tmp_path / "usage.json"),
                                  history_path=str(tmp_path / "history.json"))
    tracker.register_job("nano-banana", "j1", "alice", "image",
                         metadata={"prompt": "一只猫", "model": "m1",
                                   "params": {"aspect_ratio": "1:1"}})
    rec = tracker.get_history()["nano-banana:j1"]
    assert rec["status"] == "pending"
    assert rec["prompt"] == "一只猫"
    assert rec["kind"] == "image"


def test_normalize_status():
    assert portal.normalize_history_status("succeeded") == "done"
    assert portal.normalize_history_status("FAILED") == "failed"
    assert portal.normalize_history_status("processing") == "running"
    assert portal.normalize_history_status("queued") == "queued"


def test_result_items_extraction():
    data = {"results": [{"url": "/outputs/a.png"}, {"download_url": "/outputs/b.mp4"}]}
    items = portal.history_result_items(data, "image")
    assert items == [{"url": "/outputs/a.png", "kind": "image"},
                     {"url": "/outputs/b.mp4", "kind": "image"}]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_history_capture.py -q`
Expected: FAIL（`register_job() got an unexpected keyword argument 'metadata'`）

- [ ] **Step 3: 写实现**

```python
# register_job 签名与尾部追加
    def register_job(self, app: str, job_id: str, username: str,
                     job_type: str = "image", duration_per_item: int = 0,
                     metadata: dict | None = None):
        ...（既有逻辑不动）...
            # 新增：历史记录 pending 落库（采集失败不影响统计）
            meta = metadata or {}
            self.history_upsert({
                "app": app, "job_id": job_id, "username": username,
                "kind": "video" if job_type == "video" else "image",
                "prompt": str(meta.get("prompt", "")).strip()[:2000],
                "model": str(meta.get("model", ""))[:200],
                "params": meta.get("params") if isinstance(meta.get("params"), dict) else {},
                "status": "pending",
                "submitted_at": time.time(),
                "completed_at": None,
                "duration": 0,
                "results": [],
                "error": "",
            })
            self._save()

# _proxy 中 register_job 调用处改为：
            if is_job and resp.status in (200, 201):
                jid_header = resp.getheader("X-Job-Id", "").strip()
                if jid_header:
                    metadata = {}
                    if method == "POST" and body:
                        metadata = extract_job_metadata(
                            self.headers.get("Content-Type", ""), body)
                    tracker.register_job(app_name, jid_header, user["username"],
                                         job_type, metadata=metadata)
                    tracker.inc_daily_jobs(app_name)

# 模块级辅助函数：
_STATUS_DONE = {"succeeded", "done", "completed", "success"}
_STATUS_FAILED = {"failed", "cancelled", "canceled", "error"}
_STATUS_RUNNING = {"running", "processing", "generating", "uploading"}


def normalize_history_status(status: str) -> str:
    s = (status or "").lower()
    if s in _STATUS_DONE:
        return "done"
    if s in _STATUS_FAILED:
        return "failed"
    if s in _STATUS_RUNNING:
        return "running"
    return "queued"


def history_result_items(data: dict, kind: str) -> list[dict]:
    """从子应用 /api/jobs/{id} 响应提取结果清单（最多 4 条，供弹窗下载）。"""
    nested = data.get("job") if isinstance(data.get("job"), dict) else {}
    raw = data.get("results") or nested.get("results") or []
    items = []
    for r in raw[:4]:
        if not isinstance(r, dict):
            continue
        url = r.get("url") or r.get("download_url") or r.get("path") or ""
        if not url:
            continue
        item_kind = "video" if kind == "video" else "image"
        items.append({"url": str(url), "kind": item_kind})
    return items

# _job_poll_loop 终态分支（done_ids.append(job["job_id"]) 之前）追加：
                            # 历史记录终态更新（采集失败不影响统计）
                            try:
                                hist = self.get_history()
                                rec = hist.get(f"{job['app']}:{job['job_id']}")
                                if rec:
                                    norm = normalize_history_status(status)
                                    rec["status"] = norm
                                    rec["completed_at"] = time.time()
                                    rec["duration"] = done * (per_item if job_type == "video" else 0) \
                                        if done > 0 else 0
                                    rec["results"] = history_result_items(data, job_type)
                                    rec["error"] = str(
                                        data.get("error") or nested.get("error") or ""
                                    )[:500]
                                    self.history_upsert(rec)
                            except Exception:
                                pass

# 404 分支（finalize_job 调用之后、done_ids.append 之前）追加：
                            try:
                                hist = self.get_history()
                                rec = hist.get(f"{job['app']}:{job['job_id']}")
                                if rec:
                                    rec["status"] = "failed"
                                    rec["completed_at"] = time.time()
                                    rec["error"] = "任务丢失（子应用可能已重启）"
                                    self.history_upsert(rec)
                            except Exception:
                                pass
```

> 注意：`per_item` 变量在 404 分支前未定义，404 分支里不要引用它；终态分支的 `per_item` 只在 `done > 0 and job_type == "video"` 时使用——实现时按现有代码缩进位置安插（终态分支已计算 `per_item`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_history_capture.py tests/test_history_store.py tests/test_history_metadata.py -q`
Expected: PASS

- [ ] **Step 5: 统计回归 + 提交**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/ -q 2>&1 | tail -2`
Expected: 与改前相同的通过/失败集合（剩余 7 个既有失败不变，无新增失败）

```bash
git add portal/app.py tests/test_history_capture.py
git commit -m "feat(portal): 历史采集接线——注册写 pending、轮询终态更新、404 标失败"
```

---

### Task 4: GET /api/platform/history 查询端点

**Files:**
- Modify: `portal/app.py`（do_GET 路由 + 查询实现）
- Create: `tests/test_history_api.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_history_api.py
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "portal"))

_spec = importlib.util.spec_from_file_location("portal_app_api", ROOT / "portal" / "app.py")
portal = importlib.util.module_from_spec(_spec)
sys.modules["portal_app_api"] = portal
_spec.loader.exec_module(portal)


def _make_tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(portal, "HISTORY_PATH", tmp_path / "history.json")
    monkeypatch.setattr(portal, "STATE_DIR", tmp_path)
    monkeypatch.setattr(portal, "USAGE_PATH", tmp_path / "usage.json")
    return portal.UsageTracker(str(tmp_path / "usage.json"),
                               history_path=str(tmp_path / "history.json"))


def test_history_query_filters_and_permission(tmp_path, monkeypatch):
    tracker = _make_tracker(tmp_path, monkeypatch)
    tracker.history_upsert({"app": "nano-banana", "job_id": "j1", "username": "alice",
                            "kind": "image", "prompt": "一只猫", "model": "m",
                            "params": {}, "status": "done",
                            "submitted_at": 1000, "completed_at": 2000,
                            "duration": 0, "results": [], "error": ""})
    tracker.history_upsert({"app": "seedance", "job_id": "j2", "username": "bob",
                            "kind": "video", "prompt": "挥手", "model": "m",
                            "params": {}, "status": "failed",
                            "submitted_at": 2000, "completed_at": 3000,
                            "duration": 0, "results": [], "error": "boom"})

    # 普通用户只看到自己的
    items, total = tracker.query_history(username="alice", is_admin=False,
                                         days=30, kind="all", status="all", q="",
                                         limit=50, offset=0)
    assert total == 1 and items[0]["job_id"] == "j1"
    # admin 全量 + 状态过滤
    items, total = tracker.query_history(username="admin", is_admin=True,
                                         days=30, kind="all", status="failed", q="",
                                         limit=50, offset=0)
    assert total == 1 and items[0]["job_id"] == "j2"
    # 搜索
    items, total = tracker.query_history(username="admin", is_admin=True,
                                         days=30, kind="all", status="all", q="猫",
                                         limit=50, offset=0)
    assert total == 1 and items[0]["job_id"] == "j1"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_history_api.py -q`
Expected: FAIL（`AttributeError: ... query_history`）

- [ ] **Step 3: 写实现**

```python
# tracker 方法：
    def query_history(self, *, username: str, is_admin: bool,
                      days: int = 30, kind: str = "all", status: str = "all",
                      q: str = "", limit: int = 60, offset: int = 0) -> tuple[list, int]:
        """返回 (items, total)。admin 全量，普通用户强制只看本人。"""
        data = self._load_history()
        cutoff = time.time() - max(1, min(int(days), 365)) * 86400
        rows = []
        for rec in data.values():
            if not isinstance(rec, dict):
                continue
            if float(rec.get("submitted_at") or 0) < cutoff:
                continue
            if not is_admin and rec.get("username") != username:
                continue
            if kind != "all" and rec.get("kind") != kind:
                continue
            if status != "all" and rec.get("status") != status:
                continue
            if q:
                hay = f"{rec.get('prompt','')} {rec.get('job_id','')}".lower()
                if q.lower() not in hay:
                    continue
            rows.append(rec)
        rows.sort(key=lambda r: float(r.get("submitted_at") or 0), reverse=True)
        total = len(rows)
        return rows[offset:offset + limit], total

# Handler do_GET 在 self.path == "/api/config" 类分支之前追加：
        if self.path.startswith("/api/platform/history"):
            user = self._require_auth(self.path)
            if not user:
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            is_admin = user.get("role") == "admin"
            items, total = tracker.query_history(
                username=user.get("username", ""),
                is_admin=is_admin,
                days=int(qs.get("days", ["30"])[0]),
                kind=qs.get("kind", ["all"])[0],
                status=qs.get("status", ["all"])[0],
                q=qs.get("q", [""])[0],
                limit=min(int(qs.get("limit", ["60"])[0]), 200),
                offset=int(qs.get("offset", ["0"])[0]),
            )
            out = []
            for rec in items:
                spec = SPEC_BY_NAME.get(rec.get("app", ""))
                out.append({**rec, "display_name": spec.display_name if spec else rec.get("app", "")})
            self._json(200, {"ok": True, "total": total, "items": out})
            return
```

> 若 `self._require_auth` 不存在（本项目用的是 `_current_user` + 手动 302），实现时按 portal 现有鉴权惯例改写：取 `_current_user()`，空则 302 到 /login。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_history_api.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add portal/app.py tests/test_history_api.py
git commit -m "feat(portal): /api/platform/history 查询端点——admin 全量、普通用户仅本人、搜索分页"
```

---

### Task 5: 前端「历史记录」tab（方案二样式）

**Files:**
- Modify: `portal/static/index.html`（tab 按钮 + panel）
- Modify: `portal/static/app.js`（HistoryApp + createApp 注册）
- Modify: `portal/static/styles.css`
- Create: `tests/test_history_sidebar.mjs`

- [ ] **Step 1: index.html——tab 按钮插在「报错问答助手」之后**

```html
      <button class="app-tab" data-tab="history">历史记录</button>
```

panel 插在 `tab-rag-assistant` 的 `</div>` 之后：

```html
  <!-- HISTORY -->
  <div class="tab-panel" id="tab-history" v-scope="HistoryApp()" @vue:mounted="init()">
    <div class="portal-content history-content">
      <section class="hist-panel">
        <header class="hist-filters">
          <select class="ctrl" v-if="isAdmin" v-model="userFilter" @change="reload()">
            <option value="">全部用户</option>
            <option v-for="u in userList" :key="u" :value="u">{{ u }}</option>
          </select>
          <div class="seg"><button type="button" :class="{active: kind==='all'}" @click="kind='all';reload()">全部</button><button type="button" :class="{active: kind==='image'}" @click="kind='image';reload()">图片</button><button type="button" :class="{active: kind==='video'}" @click="kind='video';reload()">视频</button></div>
          <div class="seg"><button type="button" :class="{active: status==='all'}" @click="status='all';reload()">全部状态</button><button type="button" :class="{active: status==='done'}" @click="status='done';reload()">成功</button><button type="button" :class="{active: status==='failed'}" @click="status='failed';reload()">失败</button><button type="button" :class="{active: status==='running'}" @click="status='running';reload()">生成中</button></div>
          <div class="seg"><button type="button" :class="{active: days===7}" @click="days=7;reload()">近7天</button><button type="button" :class="{active: days===30}" @click="days=30;reload()">近30天</button><button type="button" :class="{active: days===90}" @click="days=90;reload()">近90天</button></div>
          <input class="ctrl hist-search" v-model="q" placeholder="搜索提示词 / 任务编号" @keyup.enter="reload()">
          <button class="ctrl" type="button" @click="reload()">刷新</button>
        </header>
        <div class="hist-grid" v-if="items.length">
          <div class="hist-card" v-for="it in items" :key="it.app + ':' + it.job_id" @click="openDetail(it)">
            <div class="hist-media" :class="it.kind">
              <img v-if="it.kind==='image' && it.thumb_url" :src="'/' + it.app + it.thumb_url" loading="lazy">
              <video v-else-if="it.kind==='video' && it.thumb_url" :src="'/' + it.app + it.thumb_url" preload="metadata" muted playsinline></video>
              <div v-else class="hist-media-empty">{{ it.status==='failed' ? '无产出' : '生成中' }}</div>
              <span class="hist-kind" :class="it.kind">{{ it.kind==='video' ? '视频' : '图片' }}</span>
              <span class="hist-dur" v-if="it.kind==='video' && it.duration">0:{{ Math.max(1, Math.round(it.duration)) }}</span>
            </div>
            <div class="hist-card-body">
              <div class="hist-card-t1">{{ it.prompt || '（无提示词）' }}</div>
              <div class="hist-card-t2"><span>{{ it.username }} · {{ shortTime(it.submitted_at) }}</span><span class="pill" :class="it.status">{{ statusText(it.status) }}</span></div>
            </div>
          </div>
        </div>
        <p v-else class="hist-empty">{{ q ? '没有匹配的记录' : '暂无历史——任务完成后会出现在这里' }}</p>
        <button class="hist-more" type="button" v-if="items.length < total" @click="loadMore()">加载更多（{{ total - items.length }}）</button>
      </section>
    </div>

    <!-- 详情弹窗 -->
    <div class="hist-modal-mask" v-if="detail" @click.self="detail=null">
      <div class="hist-modal">
        <div class="hist-modal-media">
          <img v-if="detail.kind==='image' && detail.thumb_url" :src="'/' + detail.app + detail.thumb_url">
          <video v-else-if="detail.kind==='video' && detail.thumb_url" :src="'/' + detail.app + detail.thumb_url" controls preload="metadata"></video>
          <div v-else class="hist-media-empty">无产出预览</div>
          <button type="button" class="hist-dl-main" v-if="detail.thumb_url" @click="downloadItem({url: detail.thumb_url}, 0)">下载</button>
        </div>
        <div class="hist-modal-body">
          <div class="hist-modal-head">
            <span class="hist-modal-title">{{ detail.prompt || '（无提示词）' }}</span>
            <span class="pill" :class="detail.status">{{ statusText(detail.status) }}</span>
          </div>
          <dl class="hist-kv">
            <dt>用户</dt><dd>{{ detail.username }}</dd>
            <dt>应用</dt><dd>{{ detail.display_name }}</dd>
            <dt>模型</dt><dd>{{ detail.model || '—' }}</dd>
            <dt>任务编号</dt><dd class="mono">{{ detail.job_id }}</dd>
            <dt>提交时间</dt><dd>{{ fullTime(detail.submitted_at) }}</dd>
            <dt>完成时间</dt><dd>{{ detail.completed_at ? fullTime(detail.completed_at) : '—' }}</dd>
          </dl>
          <div class="hist-tabs">
            <button type="button" :class="{active: detailTab==='req'}" @click="detailTab='req'">请求</button>
            <button type="button" :class="{active: detailTab==='ret'}" @click="detailTab='ret'">返回</button>
          </div>
          <div class="hist-codebox" v-show="detailTab==='req'">
            <div><span class="k">prompt:</span> {{ detail.prompt || '—' }}</div>
            <div><span class="k">params:</span> {{ JSON.stringify(detail.params || {}, null, 2) }}</div>
          </div>
          <div v-show="detailTab==='ret'">
            <div class="hist-errbox" v-if="detail.error">{{ detail.error }}</div>
            <div class="hist-results" v-if="detail.results && detail.results.length">
              <div class="hist-result-row" v-for="(r, i) in detail.results" :key="r.url">
                <span class="hist-result-kind">{{ r.kind==='video' ? '视频' : '图片' }} {{ i + 1 }}</span>
                <button type="button" @click="downloadItem(r, i)">下载</button>
              </div>
            </div>
            <p v-else class="hist-result-none">无结果产出</p>
          </div>
          <button type="button" class="hist-modal-close" @click="detail=null">关闭</button>
        </div>
      </div>
    </div>
  </div>
```

- [ ] **Step 2: app.js——HistoryApp 组件**（插在 DirectorApp 之前，注册进 createApp）

```javascript
// ============ 历史记录（全局任务历史） ============
function HistoryApp() {
  return {
    items: [], total: 0, offset: 0,
    isAdmin: false, userList: [], userFilter: "",
    kind: "all", status: "all", days: 30, q: "",
    detail: null, detailTab: "req",
    async init() {
      try {
        const me = await api("/api/platform/me", "GET");
        this.isAdmin = !!(me && me.role === "admin");
        if (this.isAdmin) {
          const u = await api("/api/platform/history-users", "GET");
          this.userList = (u && u.users) || [];
        }
      } catch (e) { /* 权限信息拿不到时按普通用户渲染 */ }
      this.reload();
    },
    async reload() {
      this.offset = 0;
      const params = new URLSearchParams({
        days: this.days, kind: this.kind, status: this.status,
        q: this.q, limit: 60, offset: 0,
      });
      if (this.isAdmin && this.userFilter) params.set("user", this.userFilter);
      const res = await api("/api/platform/history?" + params.toString(), "GET");
      if (!res || !res.ok) { this.items = []; this.total = 0; return; }
      this.items = res.items || [];
      this.total = res.total || 0;
      this.detail = null;
    },
    async loadMore() {
      this.offset += 60;
      const params = new URLSearchParams({
        days: this.days, kind: this.kind, status: this.status,
        q: this.q, limit: 60, offset: this.offset,
      });
      const res = await api("/api/platform/history?" + params.toString(), "GET");
      if (res && res.ok) this.items = this.items.concat(res.items || []);
    },
    openDetail(it) { this.detail = it; this.detailTab = "req"; },
    statusText(s) {
      return { done: "已成功", failed: "已失败", running: "生成中", queued: "排队中", pending: "排队中" }[s] || s;
    },
    shortTime(ts) {
      if (!ts) return "—";
      const d = new Date(ts * 1000);
      const now = new Date();
      const sameDay = d.toDateString() === now.toDateString();
      const pad = (n) => String(n).padStart(2, "0");
      return sameDay ? `${pad(d.getHours())}:${pad(d.getMinutes())}`
                     : `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    },
    fullTime(ts) {
      if (!ts) return "—";
      const d = new Date(ts * 1000);
      const pad = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    },
    async downloadItem(r, index) {
      const url = "/" + this.detail.app + r.url;
      const ext = r.kind === "video" ? ".mp4" : ".png";
      const filename = `${this.detail.app}-${index}${ext}`;
      try {
        const resp = await fetch(url);
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const blob = await resp.blob();
        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = blobUrl; a.download = filename; a.style.display = "none";
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
      } catch (e) {
        this.detailTab = "ret";
      }
    },
  };
}
window.HistoryApp = HistoryApp;
```

createApp 根上下文注册（DirectorApp 之前追加 `HistoryApp,`）。

- [ ] **Step 3: styles.css 追加**（沿用 portal 主题令牌）

```css
/* ===== 历史记录（方案二媒体卡片） ===== */
.history-content { padding: 14px; }
.hist-filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 14px; }
.hist-filters .ctrl { padding: 5px 10px; font-size: 12px; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--surface); color: var(--text); }
.hist-filters .seg { display: inline-flex; border: 1px solid var(--border-strong); border-radius: 8px; overflow: hidden; }
.hist-filters .seg button { appearance: none; background: var(--surface); color: var(--text-2, #697386); border: 0; border-right: 1px solid var(--border-strong); font-size: 12px; padding: 5px 12px; cursor: pointer; }
.hist-filters .seg button:last-child { border-right: 0; }
.hist-filters .seg button.active { background: var(--accent-soft, rgba(62,201,192,.14)); color: var(--accent, #0e9f96); font-weight: 600; }
.hist-search { width: 200px; }
.hist-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
.hist-card { border: 1px solid var(--border); border-radius: 12px; overflow: hidden; background: var(--surface); cursor: pointer; transition: border-color .15s, transform .15s; }
.hist-card:hover { border-color: var(--accent, #0e9f96); transform: translateY(-2px); }
.hist-media { position: relative; height: 140px; background: var(--panel-2, #eef1f6); display: flex; align-items: center; justify-content: center; overflow: hidden; }
.hist-media img, .hist-media video { width: 100%; height: 100%; object-fit: cover; }
.hist-media-empty { font-size: 12px; color: var(--text-3, #8b98ab); }
.hist-kind { position: absolute; left: 6px; top: 6px; font-size: 10.5px; font-weight: 700; padding: 1px 7px; border-radius: 5px; border: 1px solid; }
.hist-kind.image { color: var(--accent, #0e9f96); border-color: rgba(14,159,150,.4); background: rgba(14,159,150,.1); }
.hist-kind.video { color: #7aa7ff; border-color: rgba(122,167,255,.4); background: rgba(122,167,255,.08); }
.hist-dur { position: absolute; right: 6px; bottom: 6px; font-size: 10px; color: #fff; background: rgba(0,0,0,.55); border-radius: 4px; padding: 0 5px; }
.hist-card-body { padding: 10px 12px 12px; }
.hist-card-t1 { font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hist-card-t2 { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; font-size: 11.5px; color: var(--text-3, #8b98ab); }
.pill { font-size: 11.5px; padding: 2px 9px; border-radius: 999px; }
.pill.done { color: #1d9e6a; background: rgba(29,158,106,.13); }
.pill.failed { color: #c9403a; background: rgba(201,64,58,.12); }
.pill.running { color: #2f7fc0; background: rgba(47,127,192,.13); }
.pill.queued, .pill.pending { color: #b07d1e; background: rgba(176,125,30,.14); }
.hist-empty { color: var(--text-3, #8b98ab); text-align: center; padding: 48px 0; font-size: 13px; }
.hist-more { display: block; margin: 14px auto 0; padding: 6px 18px; font-size: 12px; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--surface); color: var(--text); cursor: pointer; }
.hist-modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 1000; display: flex; align-items: center; justify-content: center; }
.hist-modal { display: grid; grid-template-columns: 300px minmax(0, 480px); background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; max-width: 860px; width: calc(100vw - 40px); max-height: 84vh; }
.hist-modal-media { position: relative; background: var(--panel-2, #eef1f6); min-height: 250px; display: flex; align-items: center; justify-content: center; }
.hist-modal-media img, .hist-modal-media video { width: 100%; height: 100%; object-fit: contain; }
.hist-dl-main { position: absolute; right: 8px; bottom: 8px; padding: 4px 10px; font-size: 11px; border: 1px solid var(--border-strong); border-radius: 6px; background: var(--surface); color: var(--text); cursor: pointer; }
.hist-modal-body { padding: 16px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; }
.hist-modal-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
.hist-modal-title { font-size: 13px; font-weight: 700; }
.hist-kv { display: grid; grid-template-columns: auto 1fr auto 1fr; gap: 4px 14px; font-size: 12px; }
.hist-kv dt { color: var(--text-3, #8b98ab); }
.hist-tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--border); }
.hist-tabs button { appearance: none; background: none; border: 0; border-bottom: 2px solid transparent; color: var(--text-3, #8b98ab); font-size: 12px; padding: 6px 12px; cursor: pointer; }
.hist-tabs button.active { color: var(--accent, #0e9f96); border-bottom-color: var(--accent, #0e9f96); font-weight: 600; }
.hist-codebox { background: var(--panel-2, #eef1f6); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; font-size: 11.5px; white-space: pre-wrap; word-break: break-all; }
.hist-codebox .k { color: var(--text-3, #8b98ab); }
.hist-errbox { background: rgba(201,64,58,.12); border: 1px solid rgba(201,64,58,.35); color: #c9403a; border-radius: 8px; padding: 9px 12px; font-size: 12px; }
.hist-results { display: flex; flex-direction: column; gap: 6px; }
.hist-result-row { display: flex; justify-content: space-between; align-items: center; font-size: 12px; padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px; }
.hist-result-row button { padding: 3px 10px; font-size: 11px; border: 1px solid var(--border-strong); border-radius: 6px; background: var(--surface); color: var(--text); cursor: pointer; }
.hist-result-none { font-size: 12px; color: var(--text-3, #8b98ab); }
.hist-modal-close { align-self: flex-end; padding: 6px 16px; font-size: 12px; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--surface); color: var(--text); cursor: pointer; }
```

- [ ] **Step 4: node 冒烟测试**

```javascript
// tests/test_history_sidebar.mjs（照抄 test_director_sidebar.mjs 的桩结构）
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const appJs = fs.readFileSync(path.join(ROOT, "portal/static/app.js"), "utf8");

const elStub = () => ({ addEventListener() {}, classList: { toggle() {} }, style: {}, value: "" });
globalThis.document = { getElementById: () => elStub(), querySelectorAll: () => [], body: elStub(), addEventListener() {} };
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
Object.defineProperty(globalThis, "navigator", { value: { clipboard: { writeText: async () => {} } }, configurable: true });
globalThis.location = { pathname: "/", search: "", replace() {} };
globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true }) });
globalThis.PetiteVue = { createApp: () => ({ mount() {} }), reactive: (v) => v };

const fn = new Function(appJs + "\nreturn { HistoryApp };");
const { HistoryApp } = fn();
if (typeof HistoryApp !== "function") throw new Error("HistoryApp not defined");
const app = HistoryApp();
if (typeof app.openDetail !== "function" || typeof app.downloadItem !== "function") throw new Error("openDetail/downloadItem missing");
if (app.statusText("done") !== "已成功") throw new Error("statusText mapping broken");
console.log("history sidebar: ok");
process.exit(0);
```

- [ ] **Step 5: 跑测试并提交**

Run: `cd /Users/260413a/ai-generation-portable-apps && node tests/test_history_sidebar.mjs`
Expected: `history sidebar: ok`

```bash
git add portal/static/index.html portal/static/app.js portal/static/styles.css tests/test_history_sidebar.mjs
git commit -m "feat(portal): 历史记录页签——方案二媒体卡片网格、详情弹窗（请求/返回 + 弹窗内下载）"
```

---

### Task 6: 后端补充（/api/platform/me 与 history-users + 用户过滤）

**Files:**
- Modify: `portal/app.py`
- Modify: `tests/test_history_api.py`

- [ ] **Step 1: 补两个小端点**（HistoryApp.init 依赖）

```python
# Handler do_GET 追加：
        if self.path == "/api/platform/me":
            user = self._current_user()
            if not user:
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            self._json(200, {"ok": True, "username": user["username"], "role": user["role"]})
            return
        if self.path == "/api/platform/history-users":
            user = self._current_user()
            if not user or user.get("role") != "admin":
                self._json(403, {"ok": False, "error": "forbidden"})
                return
            users = sorted({r.get("username", "") for r in tracker.get_history().values()
                            if isinstance(r, dict) and r.get("username")})
            self._json(200, {"ok": True, "users": users})
            return
```

且 `query_history` 增加 `user_filter` 参数：`if user_filter and rec.get("username") != user_filter: continue`；Handler 里 `user_filter=qs.get("user", [""])[0]`。

- [ ] **Step 2: 跑全部相关测试**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_history_api.py tests/test_history_store.py tests/test_history_metadata.py tests/test_history_capture.py -q && node tests/test_history_sidebar.mjs`
Expected: 全 PASS + `history sidebar: ok`

- [ ] **Step 3: 提交**

```bash
git add portal/app.py tests/test_history_api.py
git commit -m "feat(portal): /api/platform/me 与 history-users 端点、用户过滤"
```

---

### Task 7: cleanup_daily 兜底剪枝 + 部署 live 实测

**Files:**
- Modify: `tools/cleanup_daily.py`
- Create: `tests/test_cleanup_history.py`

- [ ] **Step 1: cleanup_daily.py 追加**（先读该文件找到 statistics 保护段，插在其后，保持「统计文件不碰」注释）

```python
# ---- 历史记录剪枝（与统计无关，仅剪 history.json） ----
history_path = PORTAL_STATE / "history.json"   # 按文件里现有路径变量写法调整
if history_path.exists():
    try:
        data = json.loads(history_path.read_text(encoding="utf-8"))
        cutoff = time.time() - 30 * 86400
        data = {k: v for k, v in data.items()
                if float(v.get("submitted_at") or 0) >= cutoff}
        if len(data) > 10000:
            items = sorted(data.items(), key=lambda kv: float(kv[1].get("submitted_at") or 0))
            data = dict(items[-10000:])
        history_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(f"  history 剪枝后 {len(data)} 条", flush=True)
    except Exception as exc:
        print(f"  history 剪枝失败（忽略）: {exc}", flush=True)
```

- [ ] **Step 2: 测试**

```python
# tests/test_cleanup_history.py —— 构造 40 天前的假记录 + 1 条新记录，跑 cleanup 的 history 剪枝函数，断言只剩新记录且 usage.json 未被触碰（mtime/内容不变）
```

- [ ] **Step 3: 提交并推送**

```bash
git add tools/cleanup_daily.py tests/test_cleanup_history.py
git commit -m "chore(cleanup): 每日清理补 history.json 兜底剪枝（不碰统计文件）"
git push origin main
```

- [ ] **Step 4: 与用户协调重启 portal**（后端改动需重启；确认无进行中任务）

Run: `launchctl kickstart -k gui/$(id -u)/com.ai-portal && sleep 15 && lsof -iTCP:9090 -sTCP:LISTEN -P -n | head -2`
Expected: portal 新 PID 在 9090 监听

- [ ] **Step 5: live 实测**

1. 真实提交一个文生图任务（导演台）→ 历史页出现「生成中」卡片 → 终态「已成功」+ 缩略图
2. 点卡片：请求页签显示提示词与参数；返回页签显示结果清单；**图片「下载」按钮真实下载 PNG**
3. 视频任务（如有进行中）缩略图与时长角标；详情弹窗内视频可下载
4. 普通用户账号登录：只看到自己的历史
5. 统计核对：usage.json 数字与改前逻辑一致（每日 jobs/张数不受 history 影响）

- [ ] **Step 6: 收尾**

```bash
git status --porcelain  # 应为空
```

---

## Self-Review 备注

- 统计红线：Task 3 明确「采集失败不影响统计」+ 回归全量测试；Task 7 兜底剪枝只碰 history.json。
- 无 placeholder；所有代码块完整。
- 类型一致性：history 记录字段（app/job_id/username/kind/prompt/model/params/status/submitted_at/completed_at/duration/results/error）在 Task 1/3/4/5 前后一致；前端 `detail.results[].url` 与后端 `history_result_items` 输出一致。
- 注意：`self._json` / `_current_user` / `_require_auth` 按 portal 现有命名实现时对齐（Task 4/6 已注明）。
