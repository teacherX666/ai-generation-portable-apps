"""打包应用源码为 XML,供扫码分析器塞给 Claude。

策略:
- 白名单后缀:.py .js .html .command .bat .sh
- 加上根目录的 README.md / API调用说明.md / CLAUDE.md
- 黑名单目录:.git / .venv / venv / __pycache__ / node_modules / outputs / archives / uploads
  / logs / state / test-data / .pytest_cache / .claude / .coordination / .superpowers / dist
  / build / static / 数字前缀目录(0710/7007/7008/7009)以及若干中文素材目录名
- 单文件 > 100KB 跳过并 warning
- 总 tokens 上限(粗估 chars/3.5)超过则按优先级截断:py > js > command > bat/sh > html
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

WHITELIST_SUFFIXES = {".py", ".js", ".html", ".command", ".bat", ".sh"}
ROOT_LEVEL_MD_INCLUDE = {"README.md", "API调用说明.md", "CLAUDE.md"}

BLACKLIST_DIR_NAMES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".claude", ".coordination", ".superpowers",
    "node_modules", "dist", "build",
    "outputs", "archives", "uploads", "logs", "state", "test-data",
    "0710", "7007", "7008", "7009",
    "AI Tool", "Pictures", "视频生成合集", "图", "浏览器下载", "图片生成合集",
    ".idea", ".vscode",
}

MAX_SINGLE_FILE_BYTES = 200 * 1024

PRIORITY_ORDER = [".py", ".js", ".command", ".bat", ".sh", ".html", ".md"]


def _is_blacklisted(rel_path: Path) -> bool:
    return any(part in BLACKLIST_DIR_NAMES for part in rel_path.parts)


def _priority(suffix: str) -> int:
    try:
        return PRIORITY_ORDER.index(suffix)
    except ValueError:
        return 999


def _estimate_tokens(text: str) -> int:
    """粗估:chars / 3.5(中文更多点,英文少点,均值取 3.5)。"""
    return int(len(text) / 3.5)


def pack_repository(root: Path, max_tokens: int) -> str:
    """把 root 目录下的源码打包成 XML 字符串。"""
    root = root.resolve()
    files: list[tuple[Path, str]] = []  # (rel_path, content)

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _is_blacklisted(rel):
            continue

        suffix = path.suffix
        include = False
        if suffix in WHITELIST_SUFFIXES:
            include = True
        elif suffix == ".md" and len(rel.parts) == 1 and rel.name in ROOT_LEVEL_MD_INCLUDE:
            include = True

        if not include:
            continue

        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_SINGLE_FILE_BYTES:
            logger.warning("skip large file (%d bytes > %d): %s", size, MAX_SINGLE_FILE_BYTES, rel)
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("read %s failed: %s", rel, e)
            continue

        files.append((rel, content))

    # 按优先级 + 路径排序
    files.sort(key=lambda x: (_priority(x[0].suffix), str(x[0])))

    # 累积 tokens,超上限则截断
    parts = [f'<repository root="{root}">\n']
    total = _estimate_tokens(parts[0])
    truncated_count = 0

    for rel, content in files:
        block = f'\n<file path="{rel}">\n{content}\n</file>\n'
        cost = _estimate_tokens(block)
        if total + cost > max_tokens:
            truncated_count += 1
            continue
        parts.append(block)
        total += cost

    parts.append("\n</repository>\n")

    if truncated_count > 0:
        logger.warning("pack truncated: %d files skipped due to token limit", truncated_count)

    return "".join(parts)
