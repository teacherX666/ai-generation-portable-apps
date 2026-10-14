"""绿幕归一（**落盘前**把成片里的绿幕刷成同一纯色）。

**为什么必须在成片侧做**（2026-10-14 实测，run `296a79bc`）：用户报「刮奶油的视频绿幕会
出现阴影等影响抠图的东西」。做了两层提示词侧修复（规划后自动补物理约束 + 长期口径种子）
之后，**带着约束新生成的那条依然有阴影**：

```
原片绿幕区： 均值 [1, 117, 19]   绿通道标准差 29.5   通道极差 255
归一之后：   均值 [0, 176, 64]   绿通道标准差 0.0    通道极差 0
```

模型把绿幕当**被照亮的物理表面**在画 —— 又暗（绿 117 而不是标准 176）又不匀（±29），
任何按标准绿设阈值的抠像都会失败。文字改不动渲染，所以这条只能成片侧归一。

**落点**：`FileStore.materialize_provider_result(..., transform=...)` —— 产物文件名必须
恰好是 `{sha256}.{ext}`，**落盘后改文件会破坏校验**，所以只能在**写盘前**对字节做变换。

**失败一律返回 None**（保留原片）：后处理绝不能让 run 失败或产出坏文件。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

#: 需要归一的口径：提示词里出现这些词就认为这一镜是绿幕/抠图素材。
_GREEN_SCREEN_WORDS = ("绿幕", "抠图")

#: 服务机上装了 numpy+cv2 的解释器（feishu venv 没有这两个库）。
#: 与 `integrations/video_reference.py` 探 ffmpeg 同一套路：先看显式配置，再探已知路径。
_KNOWN_PYTHONS = (
    r"C:\Users\123\AppData\Local\Programs\Python\Python312\python.exe",
)
_TIMEOUT_SECONDS = 600.0


def is_green_screen_prompt(prompt: str) -> bool:
    """这一镜是不是绿幕/抠图素材。"""
    text = str(prompt or "")
    return any(word in text for word in _GREEN_SCREEN_WORDS)


def _can_import_cv2(python: str) -> bool:
    """这个解释器能不能真的 `import cv2, numpy`。

    **必须实测**（2026-10-14 端到端回放踩到）：服务机 venv 里 `import cv2` 直接
    ModuleNotFoundError，而 `resolve_python` 原来只判「文件存在」→ 永远选中 venv
    → 每次都失败、静默保留原片（安全但功能等于没做）。
    """
    try:
        completed = subprocess.run(
            [python, "-c", "import cv2, numpy"],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


#: 解释器探测结果缓存（每个进程只探一次，别每个视频都花 0.3 秒）。
_PROBE_CACHE: dict[str, str | None] = {}


def resolve_python(configured: str = "") -> str | None:
    """找到**真能跑**归一脚本的解释器；找不到返回 None（调用方保留原片）。"""
    key = str(configured or "")
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    candidates: list[str] = []
    if configured:
        candidates.append(str(configured))
    candidates.append(sys.executable)
    candidates.extend(_KNOWN_PYTHONS)
    resolved: str | None = None
    for candidate in candidates:
        if candidate and Path(candidate).exists() and _can_import_cv2(candidate):
            resolved = candidate
            break
    _PROBE_CACHE[key] = resolved
    return resolved


def flatten_video_bytes(content: bytes, *, settings: Any) -> bytes | None:
    """把视频字节里的绿幕归一成纯色；任何一步失败都返回 None。"""
    if not content:
        return None
    script = Path(str(getattr(settings, "green_screen_flatten_script", "")))
    if not script.exists():
        _LOGGER.warning("绿幕归一脚本不存在，保留原片 path=%s", script)
        return None
    python = resolve_python(str(getattr(settings, "green_screen_flatten_python", "") or ""))
    if python is None:
        _LOGGER.warning("找不到装了 numpy/cv2 的解释器，绿幕归一跳过")
        return None
    color = str(getattr(settings, "green_screen_target_color", "00B140") or "00B140")
    with tempfile.TemporaryDirectory(prefix="greenscreen-") as workdir:
        source = Path(workdir) / "in.mp4"
        target = Path(workdir) / "out.mp4"
        source.write_bytes(content)
        try:
            completed = subprocess.run(
                [python, str(script), str(source), "--out", str(target), "--color", color],
                capture_output=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _LOGGER.warning("绿幕归一执行失败，保留原片：%s", exc)
            return None
        if completed.returncode != 0 or not target.exists():
            _LOGGER.warning(
                "绿幕归一没产出结果，保留原片 returncode=%s stderr=%s",
                completed.returncode,
                (completed.stderr or b"")[-400:],
            )
            return None
        flattened = target.read_bytes()
    if not flattened or flattened == content:
        return None
    return flattened
