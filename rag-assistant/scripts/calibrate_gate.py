"""打印语义闸门在若干样例上的分数分布，用于标定 margin。

用法：
  repo/.venv/bin/python repo/rag-assistant/scripts/calibrate_gate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_openai import OpenAIEmbeddings

from rag_agent.config import load_settings
from rag_agent.query.semantic_gate import SemanticGate

SAMPLES = [
    ("无关", "1+1"),
    ("无关", "2 的三次方"),
    ("无关", "今天天气怎么样"),
    ("无关", "帮我写一首诗"),
    ("无关", "你好"),
    ("报错", "Traceback (most recent call last):"),
    ("报错", "NameError: name 'x' is not defined"),
    ("报错", "服务启动失败，日志如下：connection refused"),
    ("报错", "部署时报错：KeyError: 'token'"),
    ("报错", "接口请求返回 500，帮忙看下"),
    ("边界", "我的 python 脚本报错，能帮我看看吗"),
    ("边界", "这里有个 bug"),
    ("报错", "TaskTypeConstraint"),
    ("报错", "ModelNotOpen"),
    ("报错", "quota exceeded"),
    ("报错", "账号欠费"),
    ("无关", "生成一张图片，不要出现错误文字"),
    ("无关", "连接飞书机器人"),
]


def main() -> None:
    settings = load_settings()
    embeddings = OpenAIEmbeddings(
        model=settings.openai_embedding_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    # 标定脚本要展示真实分数分布，不能被生产阈值提前截断；
    # 这里只关闭判定门槛，不影响生产配置。
    gate = SemanticGate(
        embeddings,
        margin=-999.0,
        top_k=settings.semantic_gate_top_k,
        min_error_score=-1.0,
    )
    print(f"model={settings.openai_embedding_model} base_url={settings.openai_base_url}")
    print(f"{'期望':<4} | {'error':>8} | {'unrelated':>9} | {'diff':>7} | {'判定':<12} | 输入")
    for expect, text in SAMPLES:
        d = gate.decide(text)
        print(
            f"{expect:<4} | {d.error_score:>8.4f} | {d.unrelated_score:>9.4f} | "
            f"{d.error_score - d.unrelated_score:>7.4f} | "
            f"{d.label:<12} | {text}"
        )


if __name__ == "__main__":
    main()
