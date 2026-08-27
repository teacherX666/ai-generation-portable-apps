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
import re
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

# 即使密钥文件被放在 state/ 以外，也不能进入发给模型的源码包。
# providers.json 等普通配置仍可按后缀规则参与分析。
SENSITIVE_FILE_NAMES = {
    "secret.json", "secrets.json",
    "credential.json", "credentials.json",
    "token.json", "tokens.json",
    "key.json", "keys.json", "api_key.json", "api_keys.json",
    "private_key.json", "private_keys.json",
}
SENSITIVE_FILE_NAME_RE = re.compile(
    r"(?:^|[._-])(secret|secrets|credential|credentials|token|tokens|"
    r"api[_-]?key|private[_-]?key)(?:[._-]|$)",
    re.IGNORECASE,
)
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks"}
_BLACKLIST_DIR_NAMES_CASEFOLD = {name.casefold() for name in BLACKLIST_DIR_NAMES}
_SENSITIVE_FILE_NAMES_CASEFOLD = {name.casefold() for name in SENSITIVE_FILE_NAMES}

# 文件名禁区之外，再对源码中的常见密钥赋值做脱敏。这样即便某个旧的
# app.py 把 API Key 写死，也只会把代码逻辑交给模型，不会把真实值发出去。
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?im)(?P<prefix>^[ \t]*(?:[\w.-]*(?:api[_-]?key|app[_-]?secret|"
    r"access[_-]?token|refresh[_-]?token|private[_-]?key|secret|token)"
    r"[\w.-]*|[\"'][^\"']*(?:api[_-]?key|app[_-]?secret|"
    r"access[_-]?token|refresh[_-]?token|private[_-]?key|secret|token)"
    r"[^\"']*[\"'])\s*[:=]\s*[\"'])"
    r"(?P<value>.*?)(?P<suffix>[\"']\s*(?:,|$))"
)
_SENSITIVE_QUOTED_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>[\"'][^\"']*(?:api[_-]?key|app[_-]?secret|"
    r"access[_-]?token|refresh[_-]?token|private[_-]?key|secret|token)"
    r"[^\"']*[\"']\s*:\s*[\"'])"
    r"(?P<value>.*?)(?P<suffix>[\"'])"
)
_SECRET_TOKEN_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:sk|key|token)-[A-Za-z0-9._~-]{16,}")

MAX_SINGLE_FILE_BYTES = 200 * 1024

PRIORITY_ORDER = [".py", ".js", ".command", ".bat", ".sh", ".html", ".md"]


def _is_sensitive_file_name(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered in _SENSITIVE_FILE_NAMES_CASEFOLD
        or lowered.startswith(".env")
        or lowered.endswith(tuple(SENSITIVE_SUFFIXES))
        or bool(SENSITIVE_FILE_NAME_RE.search(name))
    )


def _is_blacklisted(rel_path: Path) -> bool:
    """判断相对路径是否属于源码扫描禁区。

    这里是只读扫描的最后一道文件级保险：不要只依赖 state/ 目录名，
    因为 code_scan_root 可能被用户配置成其他目录，密钥也可能被误放到
    config/、private/ 等位置。
    """
    if any(part.casefold() in _BLACKLIST_DIR_NAMES_CASEFOLD for part in rel_path.parts):
        return True
    return _is_sensitive_file_name(rel_path.name)


def _is_within_root(path: Path, root: Path) -> bool:
    """只允许读取 root 内的真实文件，拒绝越界 symlink。"""
    try:
        path.resolve(strict=False).relative_to(root)
        return True
    except ValueError:
        return False


def _redact_sensitive_content(content: str) -> str:
    """只在发送给模型的副本中脱敏，不改动磁盘上的源文件。"""
    content = _SENSITIVE_ASSIGNMENT_RE.sub(
        lambda m: f"{m.group('prefix')}[REDACTED]{m.group('suffix')}",
        content,
    )
    content = _SENSITIVE_QUOTED_VALUE_RE.sub(
        lambda m: f"{m.group('prefix')}[REDACTED]{m.group('suffix')}",
        content,
    )
    return _SECRET_TOKEN_RE.sub("[REDACTED]", content)


def _priority(suffix: str) -> int:
    try:
        return PRIORITY_ORDER.index(suffix)
    except ValueError:
        return 999


def _estimate_tokens(text: str) -> int:
    """粗估:chars / 3.5(中文更多点,英文少点,均值取 3.5)。"""
    return int(len(text) / 3.5)


def pack_repository(root: Path, max_tokens: int) -> str:
    """只读打包 root 下的安全源码，供现场分析模型阅读。

    函数只执行 stat/read_text，不会创建、修改、删除或改权限任何文件。
    state/、密钥文件、环境变量文件、证书和 symlink 都不会进入结果。
    """
    root = root.expanduser().resolve()
    if root.name.casefold() in _BLACKLIST_DIR_NAMES_CASEFOLD or _is_sensitive_file_name(root.name):
        raise ValueError(f"禁止把敏感目录作为源码扫描根目录: {root.name}")
    files: list[tuple[Path, str]] = []  # (rel_path, content)

    for path in sorted(root.rglob("*")):
        # 不跟随 symlink，避免通过链接读到仓库外或密钥目录里的文件。
        if path.is_symlink() or not path.is_file():
            continue
        if not _is_within_root(path, root):
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

        files.append((rel, _redact_sensitive_content(content)))

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
