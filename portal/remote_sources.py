"""从「另一台同款平台」拉取按次明细，合并进本机使用统计。

背景（2026-10-13 用户需求）：另一台服务器 https://192.168.30.56:9090 也是同一套平台，
用户想把**两边的记录放进同一张表**一起看。

设计取舍：
- **本机做中心**，主动去拉（对面的明细接口 `/api/platform/stats/generations` 现成，不用改对面代码）。
- 账号密码存在 `portal/state/remote_sources.json`（与其它本机密钥同处，gitignored）；
  拉到 token 后缓存到 `portal/state/remote_tokens.json`，失效再重新登录。
- 数据落 `portal/state/remote_generations.jsonl`：一条一行，带 `source`（来源名）。
  每次同步**整源替换**（先删该源的旧行再追加新行）→ 幂等，重复拉不会翻倍。
- 对面挂了/超时**只影响该来源**：失败保留上次数据并记错误，绝不拖垮本机统计。
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent / "state"
CONFIG_PATH = STATE_DIR / "remote_sources.json"
TOKENS_PATH = STATE_DIR / "remote_tokens.json"
ROWS_PATH = STATE_DIR / "remote_generations.jsonl"

_LOCK = threading.RLock()
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE          # 对面是自签证书（和本机一样）
_PAGE_LIMIT = 500
_MAX_PAGES = 40
DEFAULT_DAYS = 30
DEFAULT_INTERVAL_MIN = 10

# 内存缓存：{mtime: rows}
_ROWS_CACHE: dict[str, object] = {"mtime": None, "rows": []}


def _atomic_write(path: Path, text: str) -> None:
    """唯一 tmp 名 + 重试（本仓库在 Windows 上踩过 os.replace 与并发读互斥的坑）。"""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    last: Exception | None = None
    for _ in range(6):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:                      # 杀软/并发读短暂占用
            last = exc
            time.sleep(0.05)
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    if last:
        raise last


def use_state_dir(path) -> None:
    """换掉远端来源的落盘目录（测试隔离用，也让部署能换目录）。

    不这么做的话，单元测试会读到**真实**的 state/remote_generations.jsonl，
    真实远端记录会混进用例、把行数断言全打乱（2026-10-13 实际踩到）。
    """
    global STATE_DIR, CONFIG_PATH, TOKENS_PATH, ROWS_PATH
    STATE_DIR = Path(path)
    CONFIG_PATH = STATE_DIR / "remote_sources.json"
    TOKENS_PATH = STATE_DIR / "remote_tokens.json"
    ROWS_PATH = STATE_DIR / "remote_generations.jsonl"
    _ROWS_CACHE["mtime"] = None
    _ROWS_CACHE["rows"] = []


def local_label() -> str:
    """本机在「来源」列里显示的名字（默认服务器A）。

    同步到另一台机器时，在那台的 portal/state/remote_sources.json 里写
    `"local_label": "服务器B"` 即可 —— 否则两台都会把自己标成服务器A。
    """
    try:
        return str(load_config().get("local_label") or "服务器A")
    except Exception:  # noqa: BLE001
        return "服务器A"


def load_config() -> dict:
    """读配置；文件不存在时返回内置默认（指向 192.168.30.56，但 enabled=false）。"""
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("sources", [])
                return data
        except Exception:  # noqa: BLE001
            pass
    return {"sources": [], "interval_minutes": DEFAULT_INTERVAL_MIN, "days": DEFAULT_DAYS}


def save_config(cfg: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))


def _load_tokens() -> dict:
    if TOKENS_PATH.exists():
        try:
            d = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_tokens(tokens: dict) -> None:
    _atomic_write(TOKENS_PATH, json.dumps(tokens, ensure_ascii=False, indent=2))


def _http_json(base_url: str, path: str, token: str | None = None,
               payload: dict | None = None, timeout: int = 30) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base_url.rstrip("/") + path, data=body,
                                 method="POST" if payload is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Session", token)
    with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as resp:
        raw = resp.read()
        try:
            return resp.status, json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            return resp.status, {}


def login_source(src: dict) -> str:
    """返回可用 token（优先复用缓存，401 再重新登录）。"""
    name = str(src.get("name") or src.get("base_url") or "remote")
    base = str(src.get("base_url") or "")
    tokens = _load_tokens()
    tok = str((tokens.get(name) or {}).get("token") or "")
    if tok:
        try:
            status, _ = _http_json(base, "/api/auth/me", token=tok, timeout=15)
            if status == 200:
                return tok
        except Exception:  # noqa: BLE001
            pass
    status, data = _http_json(base, "/api/auth/login",
                              payload={"username": src.get("username", ""),
                                       "password": src.get("password", "")})
    tok = str(data.get("token") or "")
    if status != 200 or not tok:
        raise RuntimeError(f"登录失败 HTTP {status}: {data.get('error') or ''}")
    tokens[name] = {"token": tok, "ts": time.time()}
    _save_tokens(tokens)
    return tok


def fetch_source_rows(src: dict, days: int = DEFAULT_DAYS) -> list[dict]:
    """拉取一个来源最近 days 天的按次明细（分页直到取完）。"""
    base = str(src.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("缺少 base_url")
    tok = login_source(src)
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - max(1, days) * 86400))
    out: list[dict] = []
    for page in range(_MAX_PAGES):
        qs = urllib.parse.urlencode({"start": start, "end": end,
                                     "offset": page * _PAGE_LIMIT, "limit": _PAGE_LIMIT})
        status, data = _http_json(base, f"/api/platform/stats/generations?{qs}", token=tok, timeout=60)
        if status == 401:                       # token 过期：重登一次再试
            tok = login_source({**src, "_force": True})
            status, data = _http_json(base, f"/api/platform/stats/generations?{qs}", token=tok, timeout=60)
        if status != 200:
            raise RuntimeError(f"取明细失败 HTTP {status}")
        rows = data.get("rows") or []
        total = int(data.get("total") or 0)
        out.extend(r for r in rows if isinstance(r, dict))
        if not rows or len(out) >= total:
            break
    return out


def _read_rows_file() -> list[dict]:
    """读本地缓存的所有来源行（按 mtime 缓存）。"""
    with _LOCK:
        if not ROWS_PATH.exists():
            return []
        try:
            mtime = ROWS_PATH.stat().st_mtime
        except OSError:
            return []
        if _ROWS_CACHE["mtime"] == mtime:
            return list(_ROWS_CACHE["rows"])            # type: ignore[arg-type]
        rows: list[dict] = []
        try:
            for line in ROWS_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
        except Exception:  # noqa: BLE001
            rows = []
        _ROWS_CACHE["mtime"] = mtime
        _ROWS_CACHE["rows"] = rows
        return list(rows)


def _write_rows(rows: list[dict]) -> None:
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    _atomic_write(ROWS_PATH, text)
    _ROWS_CACHE["mtime"] = None          # 下次重读


def all_remote_rows() -> list[dict]:
    """给统计合并用的全部远端行（带 source 字段）。"""
    return _read_rows_file()


def sync_all(force: bool = False) -> dict:
    """同步所有启用的来源。返回每个来源的结果摘要（供界面显示）。"""
    cfg = load_config()
    sources = [s for s in (cfg.get("sources") or []) if isinstance(s, dict)]
    days = int(cfg.get("days") or DEFAULT_DAYS)
    with _LOCK:
        kept = [r for r in _read_rows_file() if str(r.get("source") or "") not in
                {str(s.get("name")) for s in sources if s.get("enabled", True)}]
    result: dict[str, dict] = {}
    new_rows: list[dict] = []
    for src in sources:
        name = str(src.get("name") or src.get("base_url") or "remote")
        if not src.get("enabled", True):
            result[name] = {"ok": False, "skipped": True, "error": "已停用"}
            continue
        try:
            rows = fetch_source_rows(src, days=days)
            for r in rows:
                r["source"] = name
                r["source_host"] = str(src.get("base_url") or "")
                r["is_remote"] = True
            new_rows.extend(rows)
            result[name] = {"ok": True, "count": len(rows), "ts": time.time()}
        except Exception as exc:  # noqa: BLE001
            # 失败保留上次数据（把旧行原样带回去），只记错误
            old = [r for r in _read_rows_file() if str(r.get("source") or "") == name]
            new_rows.extend(old)
            result[name] = {"ok": False, "error": str(exc), "ts": time.time(),
                            "kept": len(old)}
    with _LOCK:
        _write_rows(kept + new_rows)
    return result


def status_snapshot() -> dict:
    """给界面用的同步状态（每个来源：上次条数/错误 + 文件时间）。"""
    cfg = load_config()
    rows = _read_rows_file()
    by_source: dict[str, int] = {}
    for r in rows:
        key = str(r.get("source") or "remote")
        by_source[key] = by_source.get(key, 0) + 1
    last_ts = None
    try:
        last_ts = ROWS_PATH.stat().st_mtime if ROWS_PATH.exists() else None
    except OSError:
        last_ts = None
    return {
        "sources": [{"name": s.get("name"), "base_url": s.get("base_url"),
                     "enabled": bool(s.get("enabled", True)),
                     "rows": by_source.get(str(s.get("name")), 0)}
                    for s in (cfg.get("sources") or [])],
        "days": int(cfg.get("days") or DEFAULT_DAYS),
        "interval_minutes": int(cfg.get("interval_minutes") or DEFAULT_INTERVAL_MIN),
        "total_rows": len(rows),
        "last_sync_ts": last_ts,
    }


# ---------------------------------------------------------------------------
# 「中心」转发（2026-10-14 用户口径：A 唯一存放两台的记录，B 的统计页去问 A）
# ---------------------------------------------------------------------------

def center_config() -> dict:
    """读取「中心」配置；没配返回 {}。

    配置写在 portal/state/remote_sources.json（本机文件、gitignored）：

        {
          "local_label": "服务器B",
          "sources": [],
          "center": { "url": "https://192.168.30.116:9090",
                      "username": "center", "password": "……" }
        }

    配了 center 的机器 = 不自己存汇总，使用统计的查询全部转发到中心那台。
    """
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return {}
    c = cfg.get("center") or {}
    url = str(c.get("url") or "").strip()
    user = str(c.get("username") or "").strip()
    if not url or not user:
        return {}
    return {"name": "center", "base_url": url, "username": user,
            "password": str(c.get("password") or ""), "enabled": True}


def center_enabled() -> bool:
    return bool(center_config())


def center_request(path_with_query: str, timeout: int = 180) -> tuple[int, bytes, str]:
    """把一次使用统计查询转发到中心，返回 (status, body, content_type)。

    - token 复用 login_source 的缓存（401 会自动重登一次）；
    - **任何失败都抛异常**，调用方负责回退到本机数据（中心挂了不能让人看不到东西）。
    """
    c = center_config()
    if not c:
        raise RuntimeError("center not configured")
    token = login_source(c)
    req = urllib.request.Request(str(c["base_url"]).rstrip("/") + path_with_query)
    req.add_header("X-Session", token)
    req.add_header("Accept", "*/*")
    with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as resp:
        body = resp.read()
        ctype = str(resp.headers.get("Content-Type") or "")
        return resp.status, body, ctype


def center_status() -> dict:
    """给界面/排查用：当前是否配置了中心。"""
    c = center_config()
    if not c:
        return {"enabled": False}
    return {"enabled": True, "url": c["base_url"], "username": c["username"]}
