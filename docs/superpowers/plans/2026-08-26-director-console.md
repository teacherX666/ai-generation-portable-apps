# 导演台（Director Console）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Portal 右侧栏加「导演台」面板：提示词优化/扩写（DeepSeek）+ 文生图（方舟 Seedream），单步 + 可串联，出图按「张」计入统计。

**Architecture:** 新子应用 `director/`（stdlib app.py，端口 8895，portal 按 apps.json 自动拉起并注入 `VOLCENGINE_ARK_API_KEY`）；portal 前端加右侧栏原生 Vue 组件 `DirectorApp()`，经 `/director/*` 反代调后端。出图走 `POST /api/jobs` + `X-Job-Id` 响应头——命中 portal 统计白名单，自动计数。

**Tech Stack:** Python stdlib（http.server/urllib/threading）、PetiteVue、pytest、node（前端冒烟）。

**设计文档:** `docs/superpowers/specs/2026-08-26-director-console-design.md`

---

### Task 1: director 子应用骨架 + /api/config

**Files:**
- Create: `director/app.py`
- Create: `director/providers.json`
- Create: `tests/test_director_config.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_director_config.py
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "director"))

import app as director  # noqa: E402


def test_seedream_size_mapping():
    assert director.seedream_size("2K", "1:1") == "2048x2048"
    assert director.seedream_size("2K", "9:16") == "1584x2816"
    assert director.seedream_size("1K", "4:3") == "1152x864"


def test_seedream_size_rejects_unknown_ratio():
    try:
        director.seedream_size("2K", "5:4")
    except ValueError as exc:
        assert "比例" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_config_payload_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PORT", "8895")
    monkeypatch.setenv("CORS", "1")
    monkeypatch.setenv("VOLCENGINE_ARK_API_KEY", "test-ark-key")
    import importlib
    importlib.reload(director)
    payload = director.config_payload()
    assert payload["aspect_ratios"] == list(director.ASPECT_RATIOS)
    assert payload["resolutions"] == ["1K", "1.5K", "2K"]
    assert payload["ark_ready"] is True
    assert payload["model"] == "doubao-seedream-5-0-pro-260628"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_director_config.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'app'`）

- [ ] **Step 3: 写骨架实现**

```python
# director/app.py
#!/usr/bin/env python3
"""导演台子应用：提示词优化/扩写（DeepSeek）+ 文生图（火山方舟 Seedream）。

与 nano-banana 同款 stdlib 骨架；由 portal 按 apps.json 拉起
（env: PORT / HOST / CORS / VOLCENGINE_ARK_API_KEY）。
"""
from __future__ import annotations

import json
import mimetypes
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
_DATA_BASE = Path(os.environ.get("DATA_DIR", str(ROOT)))
OUTPUT_DIR = _DATA_BASE / "outputs"
STATE_DIR = _DATA_BASE / "state"
PROVIDERS_PATH = ROOT / "providers.json"
SKILL_PATH = ROOT / "SKILL.md"
DEEPSEEK_KEY_PATH = STATE_DIR / "deepseek.key"

PORT = int(os.environ.get("PORT", "8895"))
HOST = os.environ.get("HOST", "127.0.0.1")
CORS = os.environ.get("CORS") == "1"

ASPECT_RATIOS = ("1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9", "9:21")

# 来自 nano-banana 子应用实测约束，勿凭空推导。
_SEEDREAM_5_PRO_SIZES: dict[str, dict[str, str]] = {
    "1K": {
        "1:1": "1024x1024", "4:3": "1152x864", "3:4": "864x1152",
        "16:9": "1424x800", "9:16": "800x1424", "3:2": "1248x832",
        "2:3": "832x1248", "21:9": "1568x672", "9:21": "672x1568",
    },
    "1.5K": {
        "1:1": "1536x1536", "4:3": "1792x1344", "3:4": "1344x1792",
        "16:9": "2048x1152", "9:16": "1152x2048", "3:2": "1872x1248",
        "2:3": "1248x1872", "21:9": "2352x1008", "9:21": "1008x2352",
    },
    "2K": {
        "1:1": "2048x2048", "4:3": "2368x1776", "3:4": "1776x2368",
        "16:9": "2816x1584", "9:16": "1584x2816", "3:2": "2496x1664",
        "2:3": "1664x2496", "21:9": "3136x1344", "9:21": "1344x3136",
    },
}


def _load_providers() -> dict[str, Any]:
    if PROVIDERS_PATH.exists():
        return json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
    return {}


PROVIDERS = _load_providers()


def seedream_size(resolution: str, aspect_ratio: str) -> str:
    resolution = str(resolution or "2K").strip()
    aspect_ratio = str(aspect_ratio or "auto").strip()
    mapping = _SEEDREAM_5_PRO_SIZES.get(resolution)
    if mapping is None:
        raise ValueError("Seedream 5.0 Pro 尺寸只支持 1K、1.5K、2K")
    if aspect_ratio == "auto":
        return resolution
    if aspect_ratio not in mapping:
        raise ValueError(f"Seedream 5.0 Pro 不支持比例 {aspect_ratio}")
    return mapping[aspect_ratio]


def _ark_key() -> str:
    return (os.environ.get("VOLCENGINE_ARK_API_KEY") or "").strip()


def config_payload() -> dict[str, Any]:
    ark = PROVIDERS.get("ark", {})
    deepseek = PROVIDERS.get("deepseek", {})
    return {
        "model": ark.get("model", "doubao-seedream-5-0-pro-260628"),
        "aspect_ratios": list(ASPECT_RATIOS),
        "resolutions": ["1K", "1.5K", "2K"],
        "default_resolution": ark.get("default_resolution", "2K"),
        "default_aspect_ratio": ark.get("default_aspect_ratio", "1:1"),
        "default_count": int(ark.get("default_count", 1)),
        "ark_ready": bool(_ark_key()),
        "deepseek_ready": bool(_load_deepseek_key() or SKILL_PATH.exists()),
        "deepseek_model": deepseek.get("model", "deepseek-chat"),
    }


def _load_deepseek_key() -> str:
    env_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env_key:
        return env_key
    if DEEPSEEK_KEY_PATH.exists():
        return DEEPSEEK_KEY_PATH.read_text(encoding="utf-8").strip()
    return ""


def json_response(handler: SimpleHTTPRequestHandler, status: int, payload: Any,
                  extra_headers: dict[str, str] | None = None) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    if CORS:
        handler.send_header("Access-Control-Allow-Origin", "*")
    for key, value in (extra_headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(data)


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/config":
            json_response(self, 200, {"ok": True, **config_payload()})
            return
        super().do_GET()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
        if not CORS:
            super().log_message(fmt, *args)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[director] listening on http://{HOST}:{PORT} (cors={CORS})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
```

```json
// director/providers.json
{
  "ark": {
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "model": "doubao-seedream-5-0-pro-260628",
    "default_resolution": "2K",
    "default_aspect_ratio": "1:1",
    "default_count": 1
  },
  "deepseek": {
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat"
  }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_director_config.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add director/app.py director/providers.json tests/test_director_config.py
git commit -m "feat(director): 子应用骨架——Seedream 尺寸表、providers.json、/api/config"
```

---

### Task 2: 提示词优化/扩写端点（DeepSeek）

**Files:**
- Create: `director/SKILL.md`
- Modify: `director/app.py`（追加 optimize 相关函数与路由）
- Create: `tests/test_director_optimize.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_director_optimize.py
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "director"))

import app as director  # noqa: E402


def test_optimize_empty_text_rejected():
    result = director.optimize_prompt("   ", "refine")
    assert result["ok"] is False
    assert "请输入" in result["error"]


def test_optimize_missing_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(director, "SKILL_PATH", tmp_path / "none.md")
    monkeypatch.setattr(director, "_load_deepseek_key", lambda: "sk-test")
    result = director.optimize_prompt("一只猫", "refine")
    assert result["ok"] is False
    assert "SKILL" in result["error"]


def test_optimize_calls_deepseek_and_returns_prompt(tmp_path, monkeypatch):
    skill = tmp_path / "SKILL.md"
    skill.write_text("你是提示词专家。", encoding="utf-8")
    monkeypatch.setattr(director, "SKILL_PATH", skill)
    monkeypatch.setattr(director, "_load_deepseek_key", lambda: "sk-test")

    captured: dict = {}

    def fake_request_json(method, url, api_key, body=None, timeout=None):
        captured.update(method=method, url=url, body=body)
        return {"choices": [{"message": {"content": "优化后的提示词"}}]}

    monkeypatch.setattr(director, "request_json", fake_request_json)
    result = director.optimize_prompt("一只猫", "refine")
    assert result["ok"] is True
    assert result["prompt"] == "优化后的提示词"
    assert captured["url"].startswith("https://api.deepseek.com")
    assert "提示词专家" in captured["body"]["messages"][0]["content"]
    assert "优化" in captured["body"]["messages"][1]["content"]  # refine 模式的 user 消息含模式指令
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_director_optimize.py -q`
Expected: FAIL（`AttributeError: module 'app' has no attribute 'optimize_prompt'`）

- [ ] **Step 3: 写 SKILL.md 与实现**

```markdown
<!-- director/SKILL.md -->
你是资深 AI 绘画与视频提示词导演，服务对象是中文创作团队。你的任务是把用户给的粗糙想法改写成可交付给生成模型（Seedream/Seedance 等）的高质量提示词。

## 输出格式（必须遵守）

只输出最终提示词正文，不要任何解释、前后缀、markdown 标记、编号或客套话。

## 优化（refine）规则

1. 保持用户原意与主体不变，不擅自添加用户没提的角色或物件。
2. 补齐四要素：主体描述、场景/环境、光线与氛围、画风/风格（可用「参考图一」类指代时保留原指代）。
3. 风格术语用生成模型熟悉的英文关键词（如 cinematic lighting, bokeh, 35mm），主体与场景用中文。
4. 若用户给了负面要求（不要 XX），单列一行「负面词：」汇总。
5. 长度 80-200 字，宁可精炼不要堆砌。

## 扩写（expand）规则

1. 把用户的一句话扩展为 150-400 字的分镜式描述：镜头、主体动作、环境细节、光效、色调、氛围依次展开。
2. 可以补环境与氛围细节，但不得改变主体与核心情节。

## 非交互模式

永远不要反问用户、不要要求补充信息；输入不完整时就按最合理的默认补全，直接输出结果。
```

```python
# director/app.py 追加（放在 json_response 之后）
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEEPSEEK_MODEL = PROVIDERS.get("deepseek", {}).get("model", "deepseek-chat")
DEEPSEEK_BASE = PROVIDERS.get("deepseek", {}).get("base_url", "https://api.deepseek.com/v1")

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def _load_skill() -> str:
    if SKILL_PATH.exists():
        return SKILL_PATH.read_text(encoding="utf-8").strip()
    return ""


def request_json(method: str, url: str, api_key: str, body: dict | None = None,
                 timeout: float = 120) -> dict:
    """urllib JSON 请求；非 2xx 抛 APIError（message 为中文翻译）。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        detail = ""
        try:
            detail = json.loads(raw).get("error", {}).get("message", "")
        except Exception:
            detail = raw[:200]
        if exc.code == 401:
            raise APIError(401, "密钥无效或已过期（InvalidApiKey）", raw)
        if exc.code == 429:
            raise APIError(429, "请求过于频繁，稍后重试", raw)
        raise APIError(exc.code, f"上游服务错误：{detail or exc.reason}", raw)
    except urllib.error.URLError as exc:
        raise APIError(502, f"网络错误：{exc.reason}", "") from exc


def optimize_prompt(text: str, mode: str) -> dict[str, Any]:
    skill = _load_skill()
    if not skill:
        return {"ok": False, "error": "SKILL.md 未找到或为空，无法进行优化"}
    if not (text or "").strip():
        return {"ok": False, "error": "请先输入提示词"}
    api_key = _load_deepseek_key()
    if not api_key:
        return {"ok": False, "error": (
            "提示词优化未配置 DeepSeek API Key，"
            "请联系管理员把 sk-... 写入 director/state/deepseek.key"
        )}
    mode_text = "按「优化 refine」规则改写" if mode == "refine" else "按「扩写 expand」规则改写"
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": skill},
            {"role": "user", "content": f"{mode_text}：\n{text.strip()}"},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    try:
        result = request_json(
            "POST", f"{DEEPSEEK_BASE}/chat/completions", api_key, body, timeout=120,
        )
        content = (result.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if not content.strip():
            return {"ok": False, "error": "DeepSeek 返回了空结果，请重试"}
        return {"ok": True, "prompt": content.strip()}
    except APIError as exc:
        return {"ok": False, "error": exc.message}
```

Handler 路由追加（`do_GET` 之后、`log_message` 之前）：

```python
    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/optimize-prompt":
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                json_response(self, 400, {"ok": False, "error": "请求格式异常"})
                return
            json_response(self, 200, optimize_prompt(
                str(data.get("text", "")), str(data.get("mode", "refine"))
            ))
            return
        json_response(self, 404, {"ok": False, "error": "未知接口"})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_director_optimize.py tests/test_director_config.py -q`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add director/app.py director/SKILL.md tests/test_director_optimize.py
git commit -m "feat(director): 提示词优化/扩写端点——DeepSeek + SKILL.md，中文错误透传"
```

---

### Task 3: 文生图 job 端点（方舟 Seedream + X-Job-Id）

**Files:**
- Modify: `director/app.py`（jobs 相关函数与路由、/outputs 服务）
- Create: `tests/test_director_jobs.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_director_jobs.py
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "director"))

import app as director  # noqa: E402


def _fake_ark_json(method, url, api_key, body=None, timeout=None):
    if "images/generations" in url:
        return {"data": [{"url": "https://fake.example/img1.png"}]}
    return {}


def test_run_text2image_writes_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("VOLCENGINE_ARK_API_KEY", "sk-ark-test")
    import importlib
    importlib.reload(director)
    monkeypatch.setattr(director, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(director, "request_json", _fake_ark_json)
    monkeypatch.setattr(director, "_download_image", lambda url, dest: dest.write_bytes(b"PNG"))

    job_id = "job-1"
    director.JOBS[job_id] = {"id": job_id, "status": "pending", "prompt": "一只猫",
                             "aspect_ratio": "1:1", "count": 1, "results": [], "error": ""}
    director._run_text2image(job_id, "一只猫", "1:1", 1, "2K")
    job = director.JOBS[job_id]
    assert job["status"] == "done"
    assert len(job["results"]) == 1
    assert (tmp_path / "outputs" / "job-1-0.png").exists()


def test_jobs_post_returns_x_job_id_header(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))  # 隔离，避免写入真实 director/outputs
    monkeypatch.setenv("VOLCENGINE_ARK_API_KEY", "sk-ark-test")
    monkeypatch.setenv("CORS", "1")
    import importlib
    importlib.reload(director)
    monkeypatch.setattr(director, "request_json", _fake_ark_json)
    monkeypatch.setattr(director, "_download_image", lambda url, dest: dest.write_bytes(b"PNG"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), director.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps({"prompt": "一只猫", "aspect_ratio": "1:1", "count": 1})
        conn.request("POST", "/api/jobs", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("X-Job-Id")
        payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ok"] is True
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_director_jobs.py -q`
Expected: FAIL（`AttributeError: ... '_run_text2image'` / POST /api/jobs 返 404）

- [ ] **Step 3: 写实现**

```python
# director/app.py 追加
import shutil

def _download_image(url: str, dest: Path) -> None:
    """下载生成结果到本地文件；失败抛 APIError。"""
    req = urllib.request.Request(url, headers={"User-Agent": "director/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            with dest.open("wb") as fh:
                shutil.copyfileobj(resp, fh)
    except Exception as exc:
        raise APIError(502, "生成结果下载失败", str(exc)) from exc


def _run_text2image(job_id: str, prompt: str, aspect_ratio: str,
                    count: int, resolution: str) -> None:
    """在线程中执行：逐张调方舟 Seedream（同步接口）并落盘。"""
    job = JOBS[job_id]
    ark = PROVIDERS.get("ark", {})
    base_url = ark.get("base_url", "https://ark.cn-beijing.volces.com/api/v3")
    model = ark.get("model", "doubao-seedream-5-0-pro-260628")
    api_key = _ark_key()
    try:
        size = seedream_size(resolution, aspect_ratio)
    except ValueError as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        body = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "response_format": "url",
            "output_format": "png",
            "watermark": False,
        }
        try:
            result = request_json(
                "POST", f"{base_url}/images/generations", api_key, body, timeout=180,
            )
            items = result.get("data") or []
            if not items:
                raise APIError(502, "方舟未返回图片结果", json.dumps(result, ensure_ascii=False)[:300])
            dest = OUTPUT_DIR / f"{job_id}-{index}.png"
            _download_image(items[0].get("url") or "", dest)
            job["results"].append({"index": index, "url": f"/outputs/{dest.name}"})
        except APIError as exc:
            job["status"] = "failed"
            job["error"] = exc.message
            return
    job["status"] = "done"
```

Handler 路由变更：

- `do_GET` 中 `/api/config` 分支之前追加：

```python
        if self.path.startswith("/api/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if job is None:
                json_response(self, 404, {"ok": False, "error": "任务不存在"})
                return
            json_response(self, 200, {"ok": True, **job})
            return
        if self.path.startswith("/outputs/"):
            self._serve_output()
            return
```

- `do_POST` 中 optimize 分支之前追加：

```python
        if self.path == "/api/jobs":
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                json_response(self, 400, {"ok": False, "error": "请求格式异常"})
                return
            prompt = str(data.get("prompt", "")).strip()
            if not prompt:
                json_response(self, 400, {"ok": False, "error": "请先输入提示词"})
                return
            aspect_ratio = str(data.get("aspect_ratio", "1:1")).strip()
            if aspect_ratio not in ASPECT_RATIOS:
                json_response(self, 400, {"ok": False, "error": f"不支持的比例：{aspect_ratio}"})
                return
            try:
                count = max(1, min(int(data.get("count", 1)), 4))
            except (TypeError, ValueError):
                count = 1
            resolution = str(data.get("resolution", "2K")).strip()
            if not _ark_key():
                json_response(self, 503, {"ok": False, "error": "方舟 Ark key 未配置，请联系管理员"})
                return
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "status": "pending", "prompt": prompt,
                   "aspect_ratio": aspect_ratio, "count": count,
                   "resolution": resolution, "results": [], "error": ""}
            with JOBS_LOCK:
                JOBS[job_id] = job
            threading.Thread(
                target=_run_text2image,
                args=(job_id, prompt, aspect_ratio, count, resolution),
                daemon=True,
            ).start()
            # X-Job-Id 让 portal 按「张」登记用量（统计红线）
            json_response(self, 200, {"ok": True, "job_id": job_id},
                          extra_headers={"X-Job-Id": job_id})
            return
```

- `Handler` 内追加 `_serve_output`：

```python
    def _serve_output(self) -> None:
        name = Path(urllib.parse.unquote(self.path.rsplit("/", 1)[-1])).name
        path = OUTPUT_DIR / name
        if not path.exists() or not path.is_file():
            json_response(self, 404, {"ok": False, "error": "文件不存在"})
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_director_jobs.py tests/test_director_optimize.py tests/test_director_config.py -q`
Expected: PASS（9 passed）

- [ ] **Step 5: 提交**

```bash
git add director/app.py tests/test_director_jobs.py
git commit -m "feat(director): 文生图 job 端点——方舟 Seedream、X-Job-Id 统计登记、/outputs 服务"
```

---

### Task 4: apps.json 注册 + 单机验证

**Files:**
- Modify: `portal/apps.json`（追加 director 条目）
- Create: `tests/test_apps_json_director.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_apps_json_director.py
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "portal"))

from app_spec import load_specs  # noqa: E402


def test_director_spec_registered():
    specs = {s.name: s for s in load_specs(ROOT / "portal" / "apps.json", ROOT)}
    assert "director" in specs
    spec = specs["director"]
    assert spec.port_default == 8895
    assert spec.job_type == "image"
    assert spec.metrics == ("images",)
    assert spec.unit_label == "张"
    assert spec.needs_ark_key is True
    assert (spec.dir_path / "app.py").exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_apps_json_director.py -q`
Expected: FAIL（`assert "director" in specs`）

- [ ] **Step 3: 在 portal/apps.json 数组末尾追加条目**

```json
  {
    "name": "director",
    "display_name": "导演台",
    "port_env": "DIRECTOR_PORT",
    "port_default": 8895,
    "mount": "component",
    "component_factory": "DirectorApp",
    "color": "#f59e0b",
    "credential_scheme": "none",
    "needs_ark_key": true,
    "job_type": "image",
    "metrics": ["images"],
    "unit_label": "张",
    "stats_combine": "images_or_seconds"
  }
```

- [ ] **Step 4: 单机验证 director 可独立起服**

Run:
```bash
cd /Users/260413a/ai-generation-portable-apps/director && \
  DIRECTOR_PORT= PORT=8896 CORS=1 VOLCENGINE_ARK_API_KEY=test python3 app.py &
sleep 1
curl -s http://127.0.0.1:8896/api/config | python3 -m json.tool | head -8
curl -s -X POST http://127.0.0.1:8896/api/jobs -H 'Content-Type: application/json' \
  -d '{"prompt":"","aspect_ratio":"1:1"}'
kill %1
```
Expected: config 返回 JSON 且 `ark_ready: true`；空 prompt 返回 400「请先输入提示词」

- [ ] **Step 5: 跑测试确认通过并提交**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -m pytest tests/test_apps_json_director.py -q`
Expected: PASS

```bash
git add portal/apps.json tests/test_apps_json_director.py
git commit -m "feat(portal): apps.json 注册导演台子应用（8895，needs_ark_key）"
```

---

### Task 5: portal 右侧栏 UI（index.html + styles.css）

**Files:**
- Modify: `portal/static/index.html`（tab 面板容器之后加右侧栏）
- Modify: `portal/static/styles.css`

- [ ] **Step 1: index.html 追加右侧栏骨架**（放在最后一个 `tab-panel` 的 `</div>` 之后、页面主体结尾之前）

```html
  <aside id="director-sidebar" class="director-sidebar" v-scope="DirectorApp()" @vue:mounted="init()">
    <button id="director-toggle" class="director-toggle" type="button"
            @click="toggleCollapse()" title="折叠/展开导演台">▸</button>
    <header class="director-header">
      <h2>🎬 导演台</h2>
    </header>
    <div class="director-body" v-show="!collapsed">
      <label class="director-field">
        <span class="director-field-label">Skill</span>
        <select v-model="skill">
          <option v-for="s in skills" :key="s.id" :value="s.id">{{ s.label }}</option>
        </select>
      </label>
      <label class="director-field" v-show="skill === 'text2image'">
        <span class="director-field-label">比例</span>
        <select v-model="ratio"><option v-for="r in ratios" :key="r" :value="r">{{ r }}</option></select>
      </label>
      <label class="director-field" v-show="skill === 'text2image'">
        <span class="director-field-label">清晰度</span>
        <select v-model="resolution"><option v-for="r in resolutions" :key="r" :value="r">{{ r }}</option></select>
      </label>
      <label class="director-field" v-show="skill === 'text2image'">
        <span class="director-field-label">数量（1-4）</span>
        <input type="number" v-model.number="count" min="1" max="4">
      </label>
      <label class="director-field" v-show="skill !== 'text2image'">
        <span class="director-field-label">风格补充（可选）</span>
        <input v-model="style" placeholder="如：电影感、赛博朋克">
      </label>
      <label class="director-field">
        <span class="director-field-label">输入</span>
        <textarea v-model="input" rows="6"
          :placeholder="skill === 'text2image' ? '描述要生成的画面…' : '输入想优化的提示词或一句话想法…'"></textarea>
      </label>
      <button class="director-run" type="button" :disabled="running || !input.trim()" @click="run()">
        {{ running ? '处理中…' : (skill === 'text2image' ? '生成图片' : '处理提示词') }}
      </button>
      <p class="director-status" v-show="statusText || error">
        <span v-show="error" class="director-error">{{ error }}</span>
        <span v-show="!error && statusText">{{ statusText }}</span>
      </p>
      <section class="director-result" v-show="resultText">
        <div class="director-result-head">
          <span>处理结果</span>
          <span class="director-result-actions">
            <button type="button" @click="copyPrompt()">复制</button>
            <button type="button" @click="fillToImage()">填入文生图</button>
          </span>
        </div>
        <pre class="director-prompt">{{ resultText }}</pre>
      </section>
      <section class="director-images" v-show="images.length">
        <div class="director-image-card" v-for="(img, i) in images" :key="img.url">
          <img :src="'/director' + img.url" :alt="'生成结果 ' + (i + 1)" loading="lazy">
          <button type="button" @click="downloadImage(img, i)">下载</button>
        </div>
      </section>
    </div>
  </aside>
```

- [ ] **Step 2: styles.css 追加**

```css
/* ===== 导演台右侧栏 ===== */
.director-sidebar {
  position: fixed; top: 94px; right: 0; bottom: 0; width: 320px;
  background: var(--surface); border-left: 1px solid var(--border);
  z-index: 40; display: flex; flex-direction: column;
  transition: width .2s ease;
}
body.director-collapsed .director-sidebar { width: 36px; }
.director-toggle {
  position: absolute; left: -14px; top: 16px; width: 14px; height: 48px;
  border: 1px solid var(--border-strong); border-right: none; border-radius: 6px 0 0 6px;
  background: var(--surface); color: var(--text); cursor: pointer; z-index: 2;
}
.director-header { padding: 14px 16px 8px; border-bottom: 1px solid var(--border); }
.director-header h2 { font-size: 15px; margin: 0; }
.director-body { padding: 12px 16px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; }
.director-field { display: flex; flex-direction: column; gap: 4px; }
.director-field-label { font-size: 12px; color: #697386; }
.director-field select, .director-field input, .director-field textarea {
  width: 100%; padding: 7px 9px; border: 1px solid var(--border-strong); border-radius: 6px;
  background: var(--surface); color: var(--text); font-size: 13px; box-sizing: border-box;
}
.director-field textarea { resize: vertical; font-family: inherit; }
.director-run {
  padding: 9px 12px; border: none; border-radius: 6px; cursor: pointer;
  background: #f59e0b; color: #fff; font-size: 13px; font-weight: 600;
}
.director-run:disabled { opacity: .5; cursor: not-allowed; }
.director-status { font-size: 12px; margin: 0; }
.director-error { color: #ef4444; }
.director-result { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
.director-result-head { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: #697386; }
.director-result-actions button {
  margin-left: 6px; padding: 3px 8px; font-size: 11px; border: 1px solid var(--border-strong);
  border-radius: 4px; background: var(--surface); color: var(--text); cursor: pointer;
}
.director-prompt {
  margin: 8px 0 0; white-space: pre-wrap; word-break: break-word;
  font-size: 12px; line-height: 1.6; background: transparent; color: var(--text);
}
.director-images { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.director-image-card { display: flex; flex-direction: column; gap: 4px; }
.director-image-card img { width: 100%; border-radius: 6px; border: 1px solid var(--border); }
.director-image-card button {
  padding: 3px 6px; font-size: 11px; border: 1px solid var(--border-strong);
  border-radius: 4px; background: var(--surface); color: var(--text); cursor: pointer;
}
/* 展开时给主体让位；窄屏改为悬浮不挤内容 */
@media (min-width: 1200px) {
  body.director-open { padding-right: 320px; }
  body.director-collapsed { padding-right: 36px; }
}
@media (max-width: 1199px) {
  body.director-open, body.director-collapsed { padding-right: 0; }
}
```

- [ ] **Step 3: 静态检查并提交**

Run: `cd /Users/260413a/ai-generation-portable-apps && python3 -c "import re; html=open('portal/static/index.html').read(); assert 'director-sidebar' in html and 'v-scope=\"DirectorApp()\"' in html; css=open('portal/static/styles.css').read(); assert '.director-sidebar' in css; print('ok')"`
Expected: `ok`

```bash
git add portal/static/index.html portal/static/styles.css
git commit -m "feat(portal): 导演台右侧栏骨架——可折叠、窄屏悬浮"
```

---

### Task 6: DirectorApp 组件（app.js）

**Files:**
- Modify: `portal/static/app.js`（末尾追加 DirectorApp 工厂函数并注册进全局）

- [ ] **Step 1: 在 app.js 末尾追加组件**（复用现有 `api()` 与 `_blobDownload()`，二者 portal 已有）

```javascript
// ============ 导演台（右侧栏） ============
function DirectorApp() {
  return {
    skills: [
      { id: "refine", label: "提示词优化" },
      { id: "expand", label: "提示词扩写" },
      { id: "text2image", label: "文生图" },
    ],
    skill: "refine",
    input: "",
    style: "",
    ratio: "1:1",
    resolution: "2K",
    count: 1,
    ratios: ["1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9", "9:21"],
    resolutions: ["1K", "1.5K", "2K"],
    running: false,
    error: "",
    statusText: "",
    resultText: "",
    images: [],
    collapsed: false,
    async init() {
      try {
        const res = await api("/director/api/config", { method: "GET" });
        if (res && res.ok) {
          const cfg = res.data || {};
          if (Array.isArray(cfg.aspect_ratios) && cfg.aspect_ratios.length) this.ratios = cfg.aspect_ratios;
          if (Array.isArray(cfg.resolutions) && cfg.resolutions.length) this.resolutions = cfg.resolutions;
          if (cfg.default_aspect_ratio) this.ratio = cfg.default_aspect_ratio;
          if (cfg.default_resolution) this.resolution = cfg.default_resolution;
          if (cfg.default_count) this.count = cfg.default_count;
          if (cfg.ark_ready === false && cfg.deepseek_ready === false) {
            this.error = "导演台未配置任何密钥，请联系管理员";
          }
        }
      } catch (e) {
        this.error = "导演台服务不可用：" + (e && e.message ? e.message : "网络错误");
      }
    },
    async run() {
      this.error = "";
      this.statusText = "";
      if (!this.input.trim()) return;
      this.running = true;
      try {
        if (this.skill === "text2image") {
          this.resultText = "";
          this.images = [];
          this.statusText = "提交生成任务…";
          const res = await api("/director/api/jobs", {
            method: "POST",
            body: JSON.stringify({
              prompt: this.input.trim(),
              aspect_ratio: this.ratio,
              count: this.count,
              resolution: this.resolution,
            }),
          });
          if (!res || !res.ok || !(res.data || {}).job_id) {
            this.error = (res && res.data && res.data.error) || "提交任务失败";
            return;
          }
          const jobId = res.data.job_id;
          for (let i = 0; i < 60; i++) {
            await new Promise((r) => setTimeout(r, 1500));
            const poll = await api("/director/api/jobs/" + jobId, { method: "GET" });
            if (!poll || !poll.ok) { this.error = "查询任务失败"; return; }
            const job = poll.data || {};
            if (job.status === "done") {
              this.images = job.results || [];
              this.statusText = "生成完成";
              return;
            }
            if (job.status === "failed") {
              this.error = job.error || "生成失败";
              return;
            }
          }
          this.error = "任务超时，请稍后在统计页核对结果";
        } else {
          this.resultText = "";
          this.statusText = "正在处理提示词…";
          const res = await api("/director/api/optimize-prompt", {
            method: "POST",
            body: JSON.stringify({
              text: this.input.trim() + (this.style.trim() ? "\n风格补充：" + this.style.trim() : ""),
              mode: this.skill,
            }),
          });
          if (!res || !res.ok || !(res.data || {}).prompt) {
            this.error = (res && res.data && res.data.error) || "处理失败";
            return;
          }
          this.resultText = res.data.prompt;
          this.statusText = "处理完成";
        }
      } catch (e) {
        this.error = "请求异常：" + (e && e.message ? e.message : "网络错误");
      } finally {
        this.running = false;
      }
    },
    fillToImage() {
      this.input = this.resultText;
      this.skill = "text2image";
      this.statusText = "已填入文生图，检查后点「生成图片」";
    },
    async copyPrompt() {
      try {
        await navigator.clipboard.writeText(this.resultText);
        this.statusText = "已复制到剪贴板";
      } catch (e) {
        this.statusText = "复制失败，请手动选择文本";
      }
    },
    toggleCollapse() {
      this.collapsed = !this.collapsed;
      document.body.classList.toggle("director-collapsed", this.collapsed);
      document.body.classList.toggle("director-open", !this.collapsed);
    },
    downloadImage(img, index) {
      _blobDownload("/director" + img.url, "director-" + index + ".png");
    },
  };
}
```

注册：在 app.js 中 PetiteVue 组件注册处（现有 DreaminaApp 等注册的地方）加一行：

```javascript
window.DirectorApp = DirectorApp;
```

- [ ] **Step 2: 静态检查**

Run: `cd /Users/260413a/ai-generation-portable-apps && node -e "const fs=require('fs'); const js=fs.readFileSync('portal/static/app.js','utf8'); if(!js.includes('function DirectorApp')) throw new Error('missing DirectorApp'); if(!js.includes('window.DirectorApp = DirectorApp')) throw new Error('missing registration'); console.log('ok')"`
Expected: `ok`

- [ ] **Step 3: 提交**

```bash
git add portal/static/app.js
git commit -m "feat(portal): DirectorApp 组件——skill 切换、串联填入、轮询、blob 下载"
```

---

### Task 7: 前端冒烟测试（node）

**Files:**
- Create: `tests/test_director_sidebar.mjs`

- [ ] **Step 1: 写冒烟测试**（DOM 桩 + eval app.js，验证 DirectorApp 暴露且交互函数存在）

```javascript
// tests/test_director_sidebar.mjs
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const appJs = fs.readFileSync(path.join(ROOT, "portal/static/app.js"), "utf8");

// 最小 DOM 桩：app.js 顶层只应触碰这些
const elStub = () => ({
  addEventListener() {}, classList: { toggle() {} }, style: {}, value: "",
});
globalThis.document = {
  getElementById: () => elStub(),
  querySelectorAll: () => [],
  body: elStub(),
  addEventListener() {},
};
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.navigator = { clipboard: { writeText: async () => {} } };
globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });
globalThis.PetiteVue = {
  createApp: () => ({ mount() {} }),
  reactive: (v) => v,
};

// 把 IIFE 求值到共享作用域
const fn = new Function(appJs + "\nreturn { DirectorApp };");
const { DirectorApp } = fn();

if (typeof DirectorApp !== "function") {
  throw new Error("DirectorApp not defined");
}
const app = DirectorApp();
if (app.skills.length !== 3) throw new Error("skills must contain 3 entries");
if (typeof app.run !== "function" || typeof app.fillToImage !== "function") {
  throw new Error("run/fillToImage missing");
}
app.resultText = "测试提示词";
app.fillToImage();
if (app.skill !== "text2image" || app.input !== "测试提示词") {
  throw new Error("fillToImage chaining broken");
}
console.log("director sidebar: ok");
```

- [ ] **Step 2: 跑测试**

Run: `cd /Users/260413a/ai-generation-portable-apps && node tests/test_director_sidebar.mjs`
Expected: `director sidebar: ok`
（若 app.js 顶层还触碰了其他 DOM API，按报错往桩里补对应方法，不改产品代码）

- [ ] **Step 3: 提交**

```bash
git add tests/test_director_sidebar.mjs
git commit -m "test(portal): 导演台侧边栏 node 冒烟测试"
```

---

### Task 8: 部署 + live 实测

**Files:**
- Create: `director/state/deepseek.key`（gitignored，内容拷自 seedance）
- 不改任何已提交文件

- [ ] **Step 1: 准备 DeepSeek key**

Run:
```bash
cd /Users/260413a/ai-generation-portable-apps
mkdir -p director/state director/outputs
[ -f seedance/state/deepseek.key ] && cp seedance/state/deepseek.key director/state/deepseek.key && echo copied
grep -q "state/" director/.gitignore 2>/dev/null || echo -e "state/\noutputs/" >> director/.gitignore
git check-ignore director/state/deepseek.key && echo ignored-ok
```
Expected: `copied`（若 seedance key 不存在则报给用户，让其提供）与 `ignored-ok`

- [ ] **Step 2: 与用户确认后重启 portal**（plist 未改，kickstart 即可；重启会终止全部子应用与进行中任务，必须先确认无任务在跑）

Run: `launchctl kickstart -k gui/$(id -u)/com.ai-portal && sleep 6 && lsof -iTCP:8895 -sTCP:LISTEN -P -n`
Expected: 有进程监听 8895

- [ ] **Step 3: live 实测后端**

Run:
```bash
curl -sk https://127.0.0.1:9090/director/api/config | python3 -m json.tool | head -10
curl -sk -X POST https://127.0.0.1:9090/director/api/optimize-prompt \
  -H 'Content-Type: application/json' -d '{"text":"一只戴帽子的猫在雨夜霓虹街头","mode":"refine"}' | head -c 400
```
Expected: config 显示 `ark_ready: true`、`deepseek_ready: true`；optimize 返回优化后的中文提示词（真实 DeepSeek 调用）

- [ ] **Step 4: live 出 1 张真图（少量费用，符合真实复现优先）**

Run:
```bash
JOB=$(curl -sk -X POST https://127.0.0.1:9090/director/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"一只戴帽子的猫在雨夜霓虹街头，cinematic lighting","aspect_ratio":"1:1","count":1}')
echo "$JOB"
JOB_ID=$(echo "$JOB" | python3 -c "import json,sys; print(json.load(sys.stdin)['job_id'])")
sleep 25
curl -sk "https://127.0.0.1:9090/director/api/jobs/$JOB_ID" | python3 -m json.tool | head -12
```
Expected: job status `done`，`results[0].url` 为 `/outputs/xxx.png`；`curl -sk https://127.0.0.1:9090/director/outputs/xxx.png -o /tmp/director-live.png && file /tmp/director-live.png` 显示 PNG 图像

- [ ] **Step 5: 统计核对（核心红线）**

Run: `python3 -c "import json; d=json.load(open('portal/state/usage.json')); today=list(d['daily'].keys())[-1]; print(d['daily'][today].get('director'))"`
Expected: `director` 分组存在且 `jobs` 计数 = 本次测试任务数（提示词优化不计、出图计「张」）

- [ ] **Step 6: 浏览器端验收（用户操作）**

打开 `https://<局域网IP>:9090`，确认：右侧栏可见「🎬 导演台」；试一次「提示词优化 → 填入文生图 → 生成图片」；折叠按钮正常；其它 tab 面板不被遮挡。

- [ ] **Step 7: 提交部署收尾**

```bash
git add director/.gitignore
git commit -m "chore(director): gitignore state/outputs"
git push origin main
```

---

## Self-Review 备注

- 统计红线：Task 3 的 `X-Job-Id` 头 + apps.json `job_type=image/metrics=images`，Task 8 Step 5 实测核对。
- 无 placeholder；所有代码块完整。
- 类型一致性：`JOBS[job_id]` 结构在 Task 3 与 Task 6 前端轮询字段一致（status/results/error）。
- 端口：8895 生产 / 8896 测试，与现有端口表无冲突。
