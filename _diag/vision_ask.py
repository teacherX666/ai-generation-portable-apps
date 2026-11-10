"""用**方舟上的 DeepSeek 视觉模型**看图（不是本地 7B），支持一次给多张图做对比。

为什么要有这个：本机 `mcp__vision__describe_image` 走的是 Ollama qwen2.5vl:7b，
在"数几个罐子/颜色顺序/位置对应"这类细节上不可靠（2026-10-20 因此误判过一次）。
DSH 自己的 dsh-vision-fallback 插件用的是 `deepseek-v4-1-flash-260910`（方舟），
视觉记录见 ~/.dsh/vision-fallback/usage.jsonl（kind=vision）。这里复刻它。

⚠️ Key 顺序：本机有两个 Ark Key —— `state/deepseek.key`（ark-e13d…，LLM/视觉用）与
`state/secrets.json` 的 `volcengine_api_key`（ark-875f…，生图/生视频用）。拿错直接 403。

用法：
    python _diag/vision_ask.py --question "..." --images a.png [b.png ...] [--out 答案.md]
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import model_gateway  # noqa: E402

DEFAULT_MODEL = "deepseek-v4-1-flash-260910"


def find_key() -> str:
    for candidate in (
        ROOT / "seedance" / "state" / "deepseek.key",
        ROOT / "director" / "state" / "deepseek.key",
    ):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8").strip()
            if text:
                return text
    for name in ("DEEPSEEK_API_KEY", "ARK_LLM_API_KEY", "VOLCENGINE_ARK_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value
    for candidate in (
        ROOT / "seedance" / "state" / "secrets.json",
        ROOT / "director" / "state" / "secrets.json",
    ):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for key in ("volcengine_api_key", "ark_api_key", "api_key"):
            if data.get(key):
                return str(data[key])
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", required=True)
    parser.add_argument("--images", nargs="+", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-tokens", type=int, default=2500)
    args = parser.parse_args()

    content: list[dict] = [{"type": "text", "text": args.question}]
    for index, raw in enumerate(args.images, 1):
        path = Path(raw)
        if not path.is_file():
            print(f"找不到图片（第 {index} 张）：", path)
            return 1
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

    result = model_gateway.call_llm(
        [{"role": "user", "content": content}],
        provider="deepseek",
        model=args.model,
        api_key=find_key(),
        local_first=False,
        enable_thinking=False,
        temperature=0.1,
        max_tokens=args.max_tokens,
    )
    print("provider:", result.get("provider"), "| model:", result.get("model"), "| ok:", result.get("ok"))
    if not result.get("ok"):
        print("失败:", result.get("error"))
        return 1
    answer = str(result.get("content") or "")
    target = Path(args.out) if args.out else (ROOT / "_diag" / "anchor" / "vision_answer.md")
    header = (
        f"# 视觉问答（{result.get('model')}）\n\n"
        f"**图片**：{chr(10).join('- ' + str(p) for p in args.images)}\n\n"
        f"**问题**：{args.question}\n\n---\n\n"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(header + answer + "\n", encoding="utf-8")
    print("答案已写入:", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
