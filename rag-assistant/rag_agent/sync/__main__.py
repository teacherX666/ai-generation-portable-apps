"""CLI:uv run python -m rag_agent.sync [--dry-run]"""
import argparse
import logging
import sys

from langchain_openai import OpenAIEmbeddings

from rag_agent.config import load_settings
from rag_agent.lark.client import build_api_client
from rag_agent.sync.lark_fetcher import fetch_kb_markdown
from rag_agent.sync.service import SyncService


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG Agent KB 同步器")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只切分打印章节,不 embed 不写库",
    )
    parser.add_argument(
        "--generation",
        action="store_true",
        help="同步生成任务知识库（而不是报错问答知识库）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings()
    api_client = build_api_client(settings)
    doc_id = settings.lark_generation_kb_doc_id if args.generation else settings.lark_kb_doc_id
    chroma_dir = settings.generation_chroma_dir if args.generation else settings.chroma_dir
    status_path = settings.generation_sync_status_path if args.generation else settings.sync_status_path

    embeddings = OpenAIEmbeddings(
        model=settings.openai_embedding_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )

    def fetcher() -> str:
        return fetch_kb_markdown(api_client, doc_id)

    svc = SyncService(
        fetcher=fetcher,
        embeddings=embeddings,
        chroma_dir=chroma_dir,
        snapshots_dir=settings.kb_snapshots_dir,
        status_path=status_path,
        doc_id=doc_id,
    )

    try:
        result = svc.run(dry_run=args.dry_run)
    except Exception:
        logging.getLogger(__name__).exception("sync failed")
        return 1

    print(
        f"同步完成:{result.chunk_count} chunks, "
        f"耗时 {result.duration_seconds:.1f}s"
        + (f", collection={result.active_collection}" if result.active_collection else " [dry-run]")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
