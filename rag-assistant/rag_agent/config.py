"""集中配置：从 state/secrets.json 加载（不再用 .env / anthropic）。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # rag-assistant/
logger = logging.getLogger(__name__)


def _secrets() -> dict:
    path = BASE_DIR / "state" / "secrets.json"
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        logger.warning(
            "secrets.json 权限过宽（%o），建议设为 600，避免 API Key 被其他用户读取",
            mode,
        )
    return json.loads(path.read_text("utf-8"))


@dataclass
class Settings:
    # 飞书
    lark_app_id: str
    lark_app_secret: str
    lark_kb_doc_id: str
    lark_kb_pending_doc_id: str

    # OpenAI 兼容（embedding，走 t8star）
    openai_api_key: str
    openai_base_url: str

    # DeepSeek（生成，替换 Claude）
    deepseek_api_key: str

    openai_embedding_model: str = "text-embedding-3-small"
    vision_model: str = "gpt-4o"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # 扫码前语义闸门（复用 embedding，避免无关输入触发扫码）
    semantic_gate_margin: float = 0.08
    semantic_gate_top_k: int = 3
    unrelated_reply: str = (
        "您提交的内容看起来不是报错信息。本助手只解答报错类问题，请粘贴报错文本或截图；"
        "若确实是报错但知识库未命中，会自动进入候选池。"
    )

    # 自学习
    code_scan_root: Path = field(default_factory=Path)
    code_scan_max_tokens: int = 200000
    show_kb_candidate_to_user: bool = False

    # 管理端点
    admin_http_host: str = "127.0.0.1"
    admin_http_port: int = 9527

    # 检索
    retrieval_top_k: int = 5
    retrieval_candidate_k: int = 20
    # 向量相似度最低门槛；低于此值且没有明确关键词命中就丢弃。
    retrieval_min_similarity: float = 0.52
    # 混合分数 = 向量分 * 权重 + 关键词分 * 权重。
    retrieval_min_hybrid_score: float = 0.38
    retrieval_vector_weight: float = 0.55
    retrieval_keyword_weight: float = 0.45

    # 数据 / 状态目录
    data_dir: Path = field(default_factory=lambda: BASE_DIR / "data")
    state_dir: Path = field(default_factory=lambda: BASE_DIR / "state")

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def kb_snapshots_dir(self) -> Path:
        return self.data_dir / "kb_snapshots"

    @property
    def query_log_path(self) -> Path:
        return self.data_dir / "query_log.jsonl"

    @property
    def sync_status_path(self) -> Path:
        return self.state_dir / "sync_status.json"


def load_settings() -> Settings:
    s = _secrets()
    return Settings(
        lark_app_id=s["lark_app_id"],
        lark_app_secret=s["lark_app_secret"],
        lark_kb_doc_id=s["lark_kb_doc_id"],
        lark_kb_pending_doc_id=s["lark_kb_pending_doc_id"],
        openai_api_key=s["embedding_api_key"],
        openai_base_url=s["embedding_base_url"],
        openai_embedding_model=s.get("embedding_model", "text-embedding-3-small"),
        vision_model=s.get("vision_model", "gpt-4o"),
        deepseek_api_key=s["deepseek_api_key"],
        semantic_gate_margin=float(s.get("semantic_gate_margin", 0.08)),
        semantic_gate_top_k=int(s.get("semantic_gate_top_k", 3)),
        unrelated_reply=s.get("unrelated_reply", Settings.unrelated_reply),
        code_scan_root=Path(s.get("code_scan_root") or str(BASE_DIR.parent)),
        retrieval_top_k=int(s.get("retrieval_top_k", 5)),
        retrieval_candidate_k=int(s.get("retrieval_candidate_k", 20)),
        retrieval_min_similarity=float(s.get("retrieval_min_similarity", 0.52)),
        retrieval_min_hybrid_score=float(s.get("retrieval_min_hybrid_score", 0.38)),
        retrieval_vector_weight=float(s.get("retrieval_vector_weight", 0.55)),
        retrieval_keyword_weight=float(s.get("retrieval_keyword_weight", 0.45)),
    )
