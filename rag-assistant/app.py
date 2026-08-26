"""报错问答助手 —— stdlib 占位入口。

本应用只有 FastAPI 实现，Portal 通过共享 .venv 的 uvicorn 启动
app_fastapi:app，需要在 launchd plist 里设置 RAG_ASSISTANT_ENGINE=fastapi。
此文件仅用于满足 Portal start_app 的「app.py 必须存在」检查，正常不会被直接运行。
"""
from __future__ import annotations

import sys


def main() -> None:
    print(
        "rag-assistant 只有 FastAPI 实现，请在 launchd plist 里设置 "
        "RAG_ASSISTANT_ENGINE=fastapi。",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
