#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
贞贞 ai 工坊 · 生图脚本（自包含 · 纯标准库）

用途：为打斗特效提示词生成主角图（半身正面 / 四视图）与场景图。

★ 渠道铁律（不可绕过）
  **生图只走「贞贞 ai 工坊」** → POST {BASE}/v1/image/generations（路径是**单数** `image`）
  · 默认模型 `zhenzhen-image-g-v2.5-lowprice`（带 `size` + `resolution` 两个**顶层**入参）
  · 档位口径：测试 1k（¥0.0446）· 默认 2k（¥0.105）· 正式大场景 4k（¥0.1575）
  · 对照：老默认 `qwen-image-3.0-pro-t2i` = ¥0.45，且**不认** size/resolution
  **换通道必须先得到用户明确同意 ——「没同意」就是「不许」。**

鉴权：四级自动读取（本脚本自包含，不依赖其它 skill）
  ① --api-key 参数
  ② 环境变量 SEEDANCE_API_KEY
  ③ <当前目录>/.env
  ④ ~/.workbuddy/credentials/mv-auto-pipeline/keys.env   ← 用户级兜底，跨项目永久生效
  **永远不问用户要 key。**

用法：
  # 单张
  python gen_image.py --prompt-file char.txt --output ./fx_assets/char.png --size 3:4 --resolution 2k
  # 批量（资产清单 JSON，支持逐条 size + 断点续跑）
  python gen_image.py --batch assets.json --output-dir ./fx_assets --resolution 2k

排障：
  HTTP 403 先挖 body：{"code":"insufficient_user_quota"} = 额度不足 → 让用户充值，不许换通道
  连接重置 10054 / EOF in violation / 1010 → 先怀疑代理，命令前加
    env -u https_proxy -u HTTPS_PROXY -u http_proxy -u HTTP_PROXY
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Windows 控制台中文输出
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------- 常量

DEFAULT_BASE = "https://api.seedance.nz"
DEFAULT_MODEL = "zhenzhen-image-g-v2.5-lowprice"
MODEL_NEEDS_SIZE_RES = ("zhenzhen-image-g-v2.5",)   # 按模型名前缀判定，不写死全名
RES_CHOICES = ("1k", "2k", "4k")
SIZE_CHOICES = ("3:4", "16:9", "1:1", "9:16", "4:3")
DEFAULT_RES = "2k"

POLL_INTERVAL = 5
POLL_MAX_ATTEMPTS = 90          # 5s × 90 = 7.5 分钟（单张实测 43s~253s 波动大）

# 2026-09-12 实测：Cloudflare 拦截 Python 默认 UA → 403 code 1010
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

KEY_NAME = "SEEDANCE_API_KEY"
CRED_CANDIDATES = [
    Path.home() / ".workbuddy" / "credentials" / "mv-auto-pipeline" / "keys.env",
    Path.home() / ".workbuddy" / "credentials" / "keys.env",
]


# ---------------------------------------------------------------- 凭证

def _parse_env_file(path):
    """极简 .env 解析：KEY=VALUE，忽略注释与引号。"""
    out = {}
    try:
        for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def mask(val):
    if not val:
        return "(空)"
    return val[:4] + "*" * max(0, len(val) - 8) + val[-4:] if len(val) > 8 else "***"


def resolve_key(cli_key=None):
    """四级读取链，返回 (value, source)。"""
    if cli_key:
        return cli_key, "命令行参数"
    v = os.environ.get(KEY_NAME)
    if v:
        return v, "环境变量"
    local_env = Path.cwd() / ".env"
    if local_env.exists():
        v = _parse_env_file(local_env).get(KEY_NAME)
        if v:
            return v, f"项目 .env（{local_env}）"
    for cand in CRED_CANDIDATES:
        if cand.exists():
            v = _parse_env_file(cand).get(KEY_NAME)
            if v:
                return v, f"用户级凭证（{cand}）"
    return None, None


def missing_key_hint():
    return (
        "❌ 未找到 %s。请任选其一：\n"
        "   ① 写入 %s（推荐，一次性永久生效）：\n"
        "        SEEDANCE_API_KEY=你的key\n"
        "   ② 当前目录建 .env 写入同一行\n"
        "   ③ 命令行传 --api-key\n"
        "   （生图通道为「贞贞 ai 工坊」，不改用其它通道。）" % (KEY_NAME, CRED_CANDIDATES[0])
    )


# ---------------------------------------------------------------- HTTP

def _normalize_base(base):
    """统一成不带 /v1 的主机名，路径里再拼 /v1/...（只给主机名会打错端点是踩过的坑）。"""
    base = (base or DEFAULT_BASE).strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def api_call(base, method, path, api_key, body=None, retry=3):
    """带指数退避的 API 调用。4xx 不重试。"""
    url = f"{base}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": DEFAULT_UA,
    }
    data = json.dumps(body).encode("utf-8") if body else None

    last_err = None
    for attempt in range(retry):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            last_err = f"HTTP {e.code}: {err_body}"
            if e.code == 403 and "insufficient_user_quota" in err_body:
                raise RuntimeError(
                    "HTTP 403：**用户额度不足**（insufficient_user_quota）。\n"
                    "  → 请充值后再试。**不许擅自改用其它生图通道。**\n"
                    f"  原始响应：{err_body}"
                )
            if 400 <= e.code < 500:
                raise RuntimeError(last_err)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = f"{type(e).__name__}: {e}"
            if any(s in str(e).lower() for s in ("proxy", "reset", "eof", "10054")):
                print("  ⚠️ 疑似本机代理拦截 —— 命令前加："
                      "env -u https_proxy -u HTTPS_PROXY -u http_proxy -u HTTP_PROXY",
                      flush=True)
        wait = 2 ** attempt
        print(f"  [retry {attempt + 1}/{retry}] {last_err}，{wait}s 后重试", flush=True)
        time.sleep(wait)
    raise RuntimeError(f"重试 {retry} 次仍失败：{last_err}")


# ---------------------------------------------------------------- 生图

def needs_size_res(model):
    return any(k in (model or "") for k in MODEL_NEEDS_SIZE_RES)


def submit_image(base, prompt, api_key, model, resolution, size, width=None, height=None,
                 output_format="jpeg"):
    body = {"model": model, "prompt": prompt}
    if needs_size_res(model):
        if size:
            body["size"] = size
        if resolution:
            body["resolution"] = resolution
        if width and height:
            body["width"] = width
            body["height"] = height
    else:
        if size:
            print(f"  ⚠️ 模型 {model} 不认顶层 `size`（只有 {'/'.join(MODEL_NEEDS_SIZE_RES)} 认）"
                  f"—— `size={size}` 不会被发送，别以为改了画幅。", flush=True)
        body["metadata"] = {"output_format": output_format}
        if width and height:
            body["metadata"]["width"] = width
            body["metadata"]["height"] = height
        elif resolution:
            body["metadata"]["resolution"] = resolution

    resp = api_call(base, "POST", "/v1/image/generations", api_key, body)
    task_id = resp.get("task_id") or resp.get("id")
    if not task_id:
        raise RuntimeError(f"提交响应缺 task_id：{resp}")
    return task_id


def extract_url(data):
    """结果 URL 字段名不固定 → 多路径兜底（含递归）。"""
    if isinstance(data, str) and data.startswith("http"):
        return data
    if isinstance(data, dict):
        for k in ("result_url", "url", "image_url", "fileUrl", "output",
                  "outputs", "urls", "images", "result", "results", "content", "data"):
            if k in data:
                got = extract_url(data[k])
                if got:
                    return got
        for v in data.values():
            got = extract_url(v)
            if got:
                return got
    if isinstance(data, list):
        for item in data:
            got = extract_url(item)
            if got:
                return got
    return None


def poll_task(base, task_id, api_key, max_attempts=POLL_MAX_ATTEMPTS):
    for attempt in range(1, max_attempts + 1):
        resp = api_call(base, "GET", f"/v1/image/generations/{task_id}", api_key)
        data = resp.get("data", resp)
        status = data.get("status")
        print(f"  [poll {attempt}] status={status} progress={data.get('progress', '?')}",
              flush=True)
        if status == "SUCCESS":
            return data
        # 终态两个枚举都认（接口在不同版本下回过 FAILED 与 FAILURE）
        if status in ("FAILED", "FAILURE"):
            reason = (data.get("fail_reason") or data.get("failedReason")
                      or data.get("message") or "未知")
            raise RuntimeError(f"任务失败（status={status}）：{reason}")
        time.sleep(POLL_INTERVAL)
    raise RuntimeError(f"轮询 {max_attempts} 次仍未完成"
                       f"（{(max_attempts * POLL_INTERVAL) // 60} 分钟）")


def download_file(url, dest_path, retry=3):
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    last_err = None
    for attempt in range(retry):
        try:
            safe_url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%")
            req = urllib.request.Request(safe_url, headers={"User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=120) as resp:
                dest_path.write_bytes(resp.read())
            return dest_path
        except Exception as e:
            last_err = e
            wait = 2 ** attempt
            print(f"  [download retry {attempt + 1}/{retry}] {e}，{wait}s 后重试", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"下载失败：{last_err}")


def generate_one(base, prompt, output_path, api_key, model=DEFAULT_MODEL,
                 resolution=DEFAULT_RES, size=None, width=None, height=None):
    print(f"[submit] model={model} resolution={resolution} "
          f"size={size or '（不传，用服务端默认）'}", flush=True)
    print(f"  prompt: {prompt[:80]}{'...' if len(prompt) > 80 else ''}", flush=True)
    task_id = submit_image(base, prompt, api_key, model, resolution, size, width, height)
    print(f"  task_id: {task_id}", flush=True)

    data = poll_task(base, task_id, api_key)
    url = extract_url(data)
    if not url:
        raise RuntimeError(f"SUCCESS 但未取到图片 URL：{data}")

    usage = data.get("usage", {}) or {}
    amount = usage.get("amount", "?")
    print(f"  [done] 消耗 {amount} {usage.get('currency', '')}", flush=True)

    download_file(url, output_path)
    print(f"  [saved] {output_path}", flush=True)
    return {"task_id": task_id, "url": url, "path": str(output_path), "cost": amount}


def batch_generate(base, assets_file, output_dir, api_key, model, resolution, global_size):
    assets = json.loads(Path(assets_file).read_text(encoding="utf-8"))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    total_cost = 0.0
    for asset in assets:
        aid = asset["asset_id"]
        if state.get(aid, {}).get("status") == "done":
            existing = state[aid].get("path")
            if existing and Path(existing).exists():
                print(f"[skip] {aid} 已完成：{existing}", flush=True)
                continue

        prompt = asset["prompt_text"]
        out_path = output_dir / f"{aid}.png"
        a_size = asset.get("size") or global_size      # 逐条优先，其次命令行

        last_err = None
        for attempt in range(3):
            try:
                print(f"\n=== [{aid}] {asset.get('name', '')}（尝试 {attempt + 1}/3）===",
                      flush=True)
                result = generate_one(base, prompt, out_path, api_key, model, resolution, a_size)
                state[aid] = {"status": "done", **result}
                try:
                    total_cost += float(result.get("cost") or 0)
                except (TypeError, ValueError):
                    pass
                break
            except Exception as e:
                last_err = e
                print(f"  [fail attempt {attempt + 1}] {e}", flush=True)
                time.sleep(5)
        else:
            state[aid] = {"status": "failed", "error": str(last_err)}
            print(f"  [FAILED] {aid} 重试 3 次仍失败", flush=True)

        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    done = sum(1 for v in state.values() if isinstance(v, dict) and v.get("status") == "done")
    failed = sum(1 for v in state.values() if isinstance(v, dict) and v.get("status") == "failed")
    print("\n=== 批量完成 ===", flush=True)
    print(f"  成功：{done} / 失败：{failed} / 总成本：¥{total_cost:.4f}", flush=True)
    print(f"  状态文件：{state_path}", flush=True)
    return state


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="贞贞 ai 工坊生图（自包含）")
    ap.add_argument("--prompt", help="单张生图的提示词")
    ap.add_argument("--prompt-file", help="从文件读提示词（中文推荐）")
    ap.add_argument("--output", help="单张生图的输出路径")
    ap.add_argument("--batch", help="批量模式：资产清单 JSON 路径")
    ap.add_argument("--output-dir", help="批量模式：输出目录")
    ap.add_argument("--api-key", help="API Key（四级链最高优先）")
    ap.add_argument("--api-base", default=os.environ.get("SEEDANCE_API_BASE") or DEFAULT_BASE,
                    help=f"网关（默认 {DEFAULT_BASE}）")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"模型（默认 {DEFAULT_MODEL}）")
    ap.add_argument("--resolution", default=DEFAULT_RES, choices=RES_CHOICES,
                    help="1k 测试（¥0.0446）/ 2k 默认（¥0.105）/ 4k 正式大场景（¥0.1575）")
    ap.add_argument("--size", default=None, choices=SIZE_CHOICES,
                    help="画幅：主角图 3:4 / 场景图 16:9 / 道具 1:1；不传＝服务端默认（不猜）")
    ap.add_argument("--width", type=int, help="自定义宽（老模型口径）")
    ap.add_argument("--height", type=int, help="自定义高")
    args = ap.parse_args()

    base = _normalize_base(args.api_base)

    key, src = resolve_key(args.api_key)
    if not key:
        sys.exit(missing_key_hint())
    if src != "命令行参数":
        print(f"[key] {KEY_NAME} 来源：{src}（{mask(key)}）", flush=True)

    if args.batch:
        if not args.output_dir:
            sys.exit("❌ 批量模式必须指定 --output-dir")
        batch_generate(base, args.batch, args.output_dir, key,
                       args.model, args.resolution, args.size)
        return

    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if not prompt:
        sys.exit("❌ 必须指定 --prompt 或 --prompt-file")
    if not args.output:
        sys.exit("❌ 单张模式必须指定 --output")
    generate_one(base, prompt, args.output, key, args.model,
                 args.resolution, args.size, args.width, args.height)


if __name__ == "__main__":
    main()
