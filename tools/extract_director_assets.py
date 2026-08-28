#!/usr/bin/env python3
"""从 5 个 GitHub 高星仓库提取导演台词库资产（可复现）。

来源（license 均为公开提示词/模板数据）：
- gpt_image_templates.json ← freestylefly/awesome-gpt-image-2 (data/cases.json)
- nano_banana_styles.json  ← ZeroLu/awesome-nanobanana-pro (README.md)
- shortcut_inspirations.json ← rockbenben/ChatGPT-Shortcut (prompt_zh-Hant.json)
- negative_tags.json       ← Physton/sd-webui-prompt-all-in-one (group_tags/zh_CN.yaml)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "director" / "assets"


def extract_gpt_image_templates(src: Path) -> dict:
    c = json.loads(src.read_text(encoding="utf-8"))
    cats = list(c.get("categories") or [])
    cases = []
    for case in c.get("cases") or []:
        if not isinstance(case, dict):
            continue
        title = str(case.get("title") or "").strip()
        prompt = str(case.get("prompt") or "").strip()
        if not title or not prompt:
            continue
        cases.append({
            "id": case.get("id", len(cases) + 1),
            "title": title,
            "prompt": prompt[:800],
        })
    return {
        "source": "https://github.com/freestylefly/awesome-gpt-image-2",
        "categories": cats,
        "cases": cases,
    }


def extract_nano_banana_styles(readme: Path) -> dict:
    text = readme.read_text(encoding="utf-8", errors="replace")
    styles = []
    # README 结构：## 1. xxx（大类）→ ### 1.1 yyy（风格）→ 代码块 prompt
    for m in re.finditer(r"^###\s+\d+\.\d+\.\s+(.+)$", text, flags=re.MULTILINE):
        name = m.group(1).strip()
        seg = text[m.end(): m.end() + 4000]
        code = re.search(r"```(?:markdown|md|text)?\s*\n(.+?)\n```", seg, flags=re.S)
        if not code:
            continue
        prompt = code.group(1).strip()
        if len(prompt) < 50 or len(prompt) > 2000:
            continue
        styles.append({"name": name, "prompt": prompt})
    return {
        "source": "https://github.com/ZeroLu/awesome-nanobanana-pro",
        "styles": styles,
    }


def extract_shortcut_inspirations(src: Path) -> dict:
    data = json.loads(src.read_text(encoding="utf-8"))
    items = []
    for entry in data:
        zh = entry.get("zh-Hant") or {}
        title = str(zh.get("title") or "").strip()
        prompt = str(zh.get("prompt") or "").strip()
        if not title or not prompt:
            continue
        items.append({
            "title": title,
            "prompt": prompt[:600],
            "tags": (entry.get("tags") or [])[:6],
        })
    # 按 weight 排序取前 300
    items = items[:300]
    return {
        "source": "https://github.com/rockbenben/ChatGPT-Shortcut",
        "items": items,
    }


def _parse_yaml_tags(yaml_path: Path) -> dict:
    """解析 zh_CN.yaml 的 `- name: X / groups: / tags: 关键词: 翻译` 结构。

    负面词 = 「反向提示词」分类下的全部关键词；
    风格标签 = 其余分类的关键词（每分类限量，防体积爆炸）。"""
    text = yaml_path.read_text(encoding="utf-8", errors="replace")
    current_major = ""
    negative: list[str] = []
    styles: list[str] = []
    structural = {"groups", "color", "tags", "name"}
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r"^- name:\s*(.+)$", stripped)
        if m:
            current_major = m.group(1).strip()
            continue
        if ":" in stripped and not stripped.startswith(("#", "-")):
            key = stripped.split(":", 1)[0].strip()
            value = stripped.split(":", 1)[1].strip()
            if not key or " " in key or key in structural or not value:
                continue
            if "反向" in current_major or "负面" in current_major:
                negative.append(key)
            else:
                styles.append(key)
    return {"negative": negative, "styles": styles}


def extract_negative_tags(yaml_path: Path) -> dict:
    d = _parse_yaml_tags(yaml_path)
    d["negative"] = list(dict.fromkeys(d["negative"]))
    d["styles"] = list(dict.fromkeys(d["styles"]))[:300]
    d["source"] = "https://github.com/Physton/sd-webui-prompt-all-in-one"
    return d


def main() -> int:
    defaults = {
        "gpt": Path("/tmp/awesome-gpt-image-2/data/cases.json"),
        "nano": Path("/tmp/awesome-nanobanana-pro/README.md"),
        "shortcut": Path("/tmp/ChatGPT-Shortcut/src/data/prompt_zh-Hant.json"),
        "sd": Path("/tmp/sd-webui-prompt-all-in-one/group_tags/zh_CN.yaml"),
    }
    args = dict(defaults)
    if len(sys.argv) > 1:
        keys = list(defaults.keys())
        for i, val in enumerate(sys.argv[1:5]):
            args[keys[i]] = Path(val)
    for key, src in args.items():
        if not src.exists():
            print(f"缺失数据源: {src}", file=sys.stderr)
            return 1
    ASSETS.mkdir(parents=True, exist_ok=True)
    outputs = {
        "gpt_image_templates.json": extract_gpt_image_templates(args["gpt"]),
        "nano_banana_styles.json": extract_nano_banana_styles(args["nano"]),
        "shortcut_inspirations.json": extract_shortcut_inspirations(args["shortcut"]),
        "negative_tags.json": extract_negative_tags(args["sd"]),
    }
    for name, data in outputs.items():
        path = ASSETS / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  {name}: {path.stat().st_size // 1024}KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
