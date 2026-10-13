"""让「加载 portal/app.py」的测试不碰生产 state。

**为什么必须这么干**（2026-10-12 真实踩过）：
`portal/app.py` 在 import 时就会构造 `UsageTracker()` 并起后台轮询线程；测试只要驱动
到轮询终态，写入用的就是**模块级**的 `GENERATIONS_PATH` / `USAGE_PATH` / `HISTORY_PATH`
—— 它们默认指向 `portal/state/`（生产）。当时跑测试把 alice/bob/dave 的假明细写进了
生产的 `generations.jsonl`，页面上就会出现根本不存在的「生成记录」。

**做法**：把 `DATA_DIR` 临时指到一个空目录再 `exec_module` —— 模块在 import 期就把
`STATE_DIR` 等路径捕获下来了（生产也是用 `DATA_DIR` 覆盖数据目录的），于是这个模块实例
（连同它起的线程）只会写临时目录。exec 完**立刻还原环境变量**，避免污染同一进程里
后续加载的其它测试模块。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType

# 每个测试进程一个；被模块实例长期引用（线程还在用它），所以不能提前清理
_TMP: tempfile.TemporaryDirectory | None = None


def state_dir() -> Path:
    """本进程隔离出来的数据目录（顺带可用来断言"没写到生产"）。"""
    global _TMP
    if _TMP is None:
        _TMP = tempfile.TemporaryDirectory(prefix="portal-state-")
    return Path(_TMP.name)


def load_isolated(module_name: str, app_path: Path) -> ModuleType:
    """在隔离的数据目录下加载 portal/app.py。"""
    target = state_dir()
    (target / "state").mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = str(target)
    try:
        spec = importlib.util.spec_from_file_location(module_name, app_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = previous
    return module
