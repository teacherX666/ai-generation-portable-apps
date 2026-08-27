"""报错问答助手 —— web 入口（对齐 rag-agent，Claude→DeepSeek，飞书机器人→网页）。"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from langchain_openai import OpenAIEmbeddings
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from portal_identity import portal_token, verify_portal_identity
from rag_agent.config import load_settings
from rag_agent.lark.client import build_api_client
from rag_agent.llm.deepseek import chat
from rag_agent.llm.vision import summarize_error_screenshots
from rag_agent.query.log import append_query_log
from rag_agent.query.preprocessor import prepare_query
from rag_agent.query.prompt import build_messages, parse_coverage_tag, strip_coverage_tag
from rag_agent.query.retriever import KbRetriever
from rag_agent.query.semantic_gate import GateDecision, SemanticGate, prescreen_error
from rag_agent.self_learn.analyzer import format_scan_answer, scan_and_analyze
from rag_agent.self_learn.candidate_writer import write_candidate_if_new
from rag_agent.sync.lark_fetcher import fetch_kb_markdown
from rag_agent.sync.service import SyncService

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

MAX_IMAGE_COUNT = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_BODY_BYTES = 20 * 1024 * 1024
ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

settings = load_settings()
api_client = build_api_client(settings)
embeddings = OpenAIEmbeddings(
    model=settings.openai_embedding_model,
    api_key=settings.openai_api_key,
    base_url=settings.openai_base_url,
)
retriever = KbRetriever(
    chroma_dir=settings.chroma_dir,
    status_path=settings.sync_status_path,
    embeddings=embeddings,
    top_k=settings.retrieval_top_k,
    candidate_k=settings.retrieval_candidate_k,
    min_similarity=settings.retrieval_min_similarity,
    min_hybrid_score=settings.retrieval_min_hybrid_score,
    vector_weight=settings.retrieval_vector_weight,
    keyword_weight=settings.retrieval_keyword_weight,
)
semantic_gate = SemanticGate(
    embeddings=embeddings,
    margin=settings.semantic_gate_margin,
    top_k=settings.semantic_gate_top_k,
)


def _fetcher() -> str:
    return fetch_kb_markdown(api_client, settings.lark_kb_doc_id)


sync_service = SyncService(
    fetcher=_fetcher,
    embeddings=embeddings,
    chroma_dir=settings.chroma_dir,
    snapshots_dir=settings.kb_snapshots_dir,
    status_path=settings.sync_status_path,
    doc_id=settings.lark_kb_doc_id,
)

app = FastAPI(title="rag-assistant", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def _no_store(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


class ReindexRequest(BaseModel):
    dry_run: bool = False


def _require_admin(request: Request) -> JSONResponse | None:
    """未配置 Portal token 时（本地调试）放行；配置后要求管理员身份。"""
    token = portal_token()
    if not token:
        return None
    identity = verify_portal_identity(request.headers)
    if identity is None or identity.get("role") != "admin":
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    return None


def _validate_images(body: dict) -> tuple[list[str] | None, str | None]:
    """校验截图数量、格式和大小，返回（规范化 data_url 列表，错误信息）。"""
    imgs = body.get("images") or []
    if isinstance(imgs, str):
        imgs = [imgs]
    if not isinstance(imgs, list):
        return None, "图片字段格式不正确"
    if len(imgs) > MAX_IMAGE_COUNT:
        return None, f"最多只能上传 {MAX_IMAGE_COUNT} 张截图"

    out: list[str] = []
    for i in imgs:
        if not isinstance(i, str) or not i.strip():
            continue
        i = i.strip()
        if i.startswith("data:"):
            s = i
        else:
            s = "data:image/png;base64," + i

        m = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", s, re.DOTALL)
        if not m:
            return None, "截图格式不正确，请重新上传"
        if m.group(1).lower() not in ALLOWED_IMAGE_MIMES:
            return None, "只支持 png、jpeg、webp、gif 格式的截图"
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except Exception:
            return None, "截图数据不完整，请重新上传"
        if len(raw) > MAX_IMAGE_BYTES:
            return None, "单张截图不能超过 5MB"
        out.append(s)
    return out, None


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/admin/status")
def admin_status(request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    if not settings.sync_status_path.exists():
        return {"active_collection": None, "note": "KB 还未同步过"}
    return json.loads(settings.sync_status_path.read_text("utf-8"))


@app.post("/admin/reindex")
def admin_reindex(req: ReindexRequest, request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    result = sync_service.run(dry_run=req.dry_run)
    return {
        "dry_run": result.dry_run,
        "chunk_count": result.chunk_count,
        "chunk_titles": result.chunk_titles,
        "active_collection": result.active_collection,
        "duration_seconds": result.duration_seconds,
    }


@app.get("/admin/query-log")
def admin_query_log(request: Request, n: int = 20):
    denied = _require_admin(request)
    if denied:
        return denied
    if not settings.query_log_path.exists():
        return {"entries": []}
    lines = settings.query_log_path.read_text("utf-8").strip().split("\n")
    if n <= 0:
        n = 20
    n = min(n, 200)
    entries = [json.loads(line) for line in lines[-n:] if line.strip()]
    return {"entries": entries}


@app.post("/api/ask")
async def ask(request: Request):
    token = portal_token()
    if token and verify_portal_identity(request.headers) is None:
        return JSONResponse(status_code=403, content={"error": "forbidden"})

    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"error": "请求内容过大，请压缩截图或减少文字"})
    try:
        body = json.loads(raw_body)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON"})

    question = (body.get("question") or "").strip()
    image_data_urls, img_error = _validate_images(body)
    if img_error:
        return JSONResponse(status_code=400, content={"error": img_error})
    if not question and not image_data_urls:
        return JSONResponse(status_code=400, content={"error": "请提供文字或截图"})

    # 问答链路全是同步阻塞调用（视觉、embedding、DeepSeek、飞书、源码扫描），
    # 放进线程池执行，避免长时间占用 FastAPI 事件循环导致其他请求排队。
    return await run_in_threadpool(_answer_question, question, image_data_urls)


def _answer_question(question: str, image_data_urls: list[str]):
    start = time.time()
    gate_decision = None
    try:
        # 图片必须先做视觉摘要，之后才能把“文字 + 图片摘要”交给前置闸门。
        # 纯文本则在这里直接完成一次低成本语义路由，避免无关输入触发 KB/LLM。
        prep = prepare_query(
            text=question,
            image_data_urls=image_data_urls,
            summarizer=lambda urls: summarize_error_screenshots(settings, urls),
        )
        prescreen = prescreen_error(prep.query_for_retrieval)
        if prescreen == "unrelated":
            gate_decision = GateDecision(
                label="unrelated",
                error_score=0.0,
                unrelated_score=0.0,
                allow_retrieval=False,
                allow_scan=False,
                reason="prescreen_unrelated",
            )
        elif prescreen == "error":
            gate_decision = GateDecision(
                label="error_report",
                error_score=0.0,
                unrelated_score=0.0,
                allow_retrieval=True,
                allow_scan=True,
                reason="prescreen_error",
            )
        else:
            gate_decision = semantic_gate.decide(prep.query_for_retrieval)
        if not gate_decision.allow_retrieval:
            user_visible = settings.unrelated_reply
            try:
                append_query_log(
                    settings.query_log_path,
                    user_id="web",
                    query=question,
                    image_count=len(image_data_urls),
                    retrieved_titles=[],
                    answer=user_visible,
                    latency_ms=int((time.time() - start) * 1000),
                    metadata={
                        "coverage": "无关",
                        "confidence": "无关",
                        "candidate_written": False,
                        "gate_label": gate_decision.label,
                        "gate_reason": gate_decision.reason,
                        "gate_error_score": gate_decision.error_score,
                        "gate_unrelated_score": gate_decision.unrelated_score,
                        "gate_margin": gate_decision.margin_score,
                        "short_circuited": True,
                    },
                )
            except Exception:
                logger.exception("append_query_log failed for gate short-circuit (non-fatal)")
            return {
                "answer": user_visible,
                "coverage": "无关",
                "confidence": "无关",
                "candidate_written": False,
                "retrieved_titles": [],
            }

        chunks = retriever.retrieve(prep.query_for_retrieval)
        messages = build_messages(
            query_text=prep.context_text_for_generation,
            chunks=chunks,
            image_data_urls=prep.image_data_urls,
        )
        raw_answer = chat(settings, messages).strip()
        coverage = parse_coverage_tag(raw_answer)
    except RuntimeError as exc:
        return JSONResponse(status_code=503, content={"error": f"KB 尚未同步：{exc}。请先触发 /admin/reindex。"})
    except Exception:
        logger.exception("ask pipeline failed")
        return JSONResponse(status_code=502, content={"error": "检索/生成服务暂时不可用，请稍后重试。"})

    retrieved_titles = [c.metadata.get("error_title", "") for c in chunks]
    confidence = None
    candidate_written = False

    if coverage in ("完全命中", "部分命中"):
        user_visible = raw_answer
    elif gate_decision is not None and not gate_decision.allow_scan:
        # 闸门服务异常时仍允许 KB 回答，但绝不触发全仓源码扫描。
        user_visible = (
            "KB 里没有找到足够匹配的条目，当前无法进一步进行源码分析，"
            "请补充完整报错文本或稍后重试。"
        )
        confidence = "闸门异常"
    else:
        analysis = scan_and_analyze(
            query_text=question,
            top_kb_titles=retrieved_titles[:3],
            settings=settings,
        )
        user_visible = format_scan_answer(
            analysis, show_kb_candidate=settings.show_kb_candidate_to_user
        )
        confidence = analysis.confidence
        # 照文档：置信度低只答「请联系管理员」，不写候选池。
        if analysis.confidence != "低" and analysis.kb_candidate_section:
            try:
                candidate_written = write_candidate_if_new(
                    api_client,
                    settings.lark_kb_pending_doc_id,
                    analysis,
                    question,
                )
            except Exception:
                logger.exception("write_candidate_if_new failed (non-fatal)")

    try:
        append_query_log(
            settings.query_log_path,
            user_id="web",
            query=question,
            image_count=len(image_data_urls),
            retrieved_titles=retrieved_titles,
            answer=strip_coverage_tag(user_visible),
            latency_ms=int((time.time() - start) * 1000),
            metadata={
                "coverage": coverage,
                "confidence": confidence,
                "candidate_written": candidate_written,
                "gate_label": gate_decision.label if gate_decision else None,
                "gate_reason": gate_decision.reason if gate_decision else None,
                "gate_error_score": gate_decision.error_score if gate_decision else None,
                "gate_unrelated_score": gate_decision.unrelated_score if gate_decision else None,
                "gate_margin": gate_decision.margin_score if gate_decision else None,
                "short_circuited": False,
            },
        )
    except Exception:
        logger.exception("append_query_log failed (non-fatal)")

    return {
        "answer": user_visible,
        "coverage": coverage,
        "confidence": confidence,
        "candidate_written": candidate_written,
        "retrieved_titles": retrieved_titles,
    }
