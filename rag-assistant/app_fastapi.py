"""报错问答助手 —— web 入口（对齐 rag-agent，Claude→DeepSeek，飞书机器人→网页）。"""
from __future__ import annotations

import os

# 本应用访问的是国内端点（ai.t8star.org / DeepSeek / 飞书），直连即可。
# 系统代理若是 socks4://127.0.0.1:1080 会被 httpx 拒绝并导致启动崩溃，这里显式绕过。
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import base64
import json
import logging
import math
import re
import threading
import time
import urllib.request
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
from rag_agent.query.semantic_gate import SemanticGate
from rag_agent.self_learn.analyzer import format_scan_answer, scan_and_analyze
from rag_agent.self_learn.candidate_writer import write_candidate_if_new
from rag_agent.sync.indexer import split_markdown
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
    unrelated_margin=settings.semantic_gate_unrelated_margin,
    top_k=settings.semantic_gate_top_k,
    min_error_score=settings.semantic_gate_min_error_score,
    min_unrelated_score=settings.semantic_gate_min_unrelated_score,
    failure_cooldown_seconds=settings.semantic_gate_failure_cooldown_seconds,
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



GENERATION_RAG_TTL_SECONDS = 30.0
GENERATION_RAG_MIN_LEXICAL_SCORE = 1.0
GENERATION_RAG_SEMANTIC_MIN_SIMILARITY = 0.28
GENERATION_RAG_SEMANTIC_TOP_K = 20

_generation_rag_lock = threading.Lock()
_generation_rag_cache = {"fetched_at": 0.0, "docs": [], "vectors": []}
_RAG_WORD_RE = re.compile(r"[a-z0-9]+")
_RAG_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _rag_features(text: str) -> tuple[set[str], set[str]]:
    normalized = (text or "").lower().replace("_", " ").replace("-", " ")
    words = set(_RAG_WORD_RE.findall(normalized))
    cjk = _RAG_CJK_RE.findall(normalized)
    grams: set[str] = set()
    if len(cjk) == 1:
        grams.add(cjk[0])
    for index in range(len(cjk) - 1):
        grams.add(cjk[index] + cjk[index + 1])
    return words, grams


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _semantic_doc_text(doc) -> str:
    metadata = doc.metadata or {}
    title = str(metadata.get("error_title", "") or "").strip()
    keywords = str(metadata.get("kb_keywords", "") or "").strip()
    content = (doc.page_content or "").strip()
    return "\n".join(part for part in (title, keywords, content) if part)


def _embed_texts_safe(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    try:
        return embeddings.embed_documents(texts)
    except Exception:
        logger.exception("generation rule embedding failed")
        return []


def _embed_query_safe(text: str) -> list[float] | None:
    try:
        return embeddings.embed_query(text)
    except Exception:
        logger.exception("generation query embedding failed")
        return None


_GENERIC_RULE_STOPWORDS = {
    "生成", "图片", "图像", "人物", "画面", "出现", "露出", "漏出", "带有", "包含", "使用", "描写", "描述",
    "不要", "避免", "禁止", "请勿", "不得", "严禁", "拒绝", "不可", "切勿", "防止", "任何", "以及", "或者",
    "一个", "这个", "那个", "时候", "可以", "需要", "应该", "建议", "内容", "提示词", "视频", "照片", "镜头",
    "背景", "场景", "风格", "无法", "不能", "无需", "没有", "没", "不", "无", "画面中", "生成图片", "生成图像",
}
_GENERIC_NEGATION_RE = re.compile(r"(?:禁止|不要|避免|请勿|不得|严禁|拒绝|不可|切勿|防止)([^。；;\n，,]{0,40})")


def _generic_rule_terms(doc, title: str) -> list[str]:
    metadata = doc.metadata or {}
    title_core = _clean_title(title)
    title_core = re.sub(r"^(?:禁止|不要|避免|请勿|不得|严禁|拒绝|不可|切勿|防止)\s*", "", title_core)
    seeds = [title_core, str(metadata.get("kb_keywords", "") or "")]
    for match in _GENERIC_NEGATION_RE.finditer(doc.page_content or ""):
        clause = match.group(1).strip()
        if clause:
            seeds.append(clause)

    terms: set[str] = set()

    def add_seed(seed: str) -> None:
        cleaned = re.sub(r"^(?:出现|露出|漏出|带有|包含|使用|描写|描述|需要|应该|建议)\s*", "", seed.strip())
        cleaned = cleaned.strip()
        if len(cleaned) >= 2 and cleaned not in _GENERIC_RULE_STOPWORDS:
            terms.add(cleaned)
        for part in re.split(r"[，,、/；;]", cleaned):
            part = part.strip()
            if len(part) >= 2 and part not in _GENERIC_RULE_STOPWORDS:
                terms.add(part)

    for seed in seeds:
        if not seed:
            continue
        add_seed(seed)
        lower = seed.casefold()
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_.:/-]{1,}|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}", lower):
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                for size in range(2, min(5, len(token)) + 1):
                    for index in range(len(token) - size + 1):
                        gram = token[index:index + size]
                        if gram not in _GENERIC_RULE_STOPWORDS:
                            terms.add(gram)
            elif token not in _GENERIC_RULE_STOPWORDS and len(token) >= 2:
                terms.add(token)

    return sorted(terms, key=len, reverse=True)


def _generic_rule_satisfied(prompt: str, terms: list[str], doc, title: str) -> bool:
    normalized = (prompt or "").strip()
    title_core = _clean_title(title)
    title_core = re.sub(r"^(?:禁止|不要|避免|请勿|不得|严禁|拒绝|不可|切勿|防止)\s*", "", title_core)
    phrases = [title_core] + terms
    for phrase in phrases:
        if not phrase or len(phrase) < 2:
            continue
        for prefix in ("不要出现", "不要露出", "不要带", "不要包含", "不出现", "不穿", "不带", "不包含", "不要", "不", "无", "没有", "没", "避免", "禁止", "拒绝", "请勿", "不得", "严禁", "不可"):
            if (prefix + phrase) in normalized:
                return True
        for suffix in ("不可见", "不出现", "隐藏", "朝向自己", "背面", "遮住", "遮挡", "模糊"):
            if (phrase + suffix) in normalized:
                return True
    return False


def _generic_rule_match(prompt: str, doc, title: str) -> tuple[bool, float]:
    terms = _generic_rule_terms(doc, title)
    if not terms:
        return False, 0.0
    if _generic_rule_satisfied(prompt, terms, doc, title):
        return False, 0.0
    normalized = (prompt or "").strip()
    hit_terms = [term for term in terms if term in normalized]
    if not hit_terms:
        return False, 0.0
    return True, float(len(hit_terms))


_FACE_TRIGGER_PHRASES = (
    "愤怒", "生气", "恼怒", "发怒", "怒气", "怒火", "愤懑", "暴怒", "愤怒的", "生气的",
)
_FACE_SATISFIED_PHRASES = (
    "不要脸红", "不脸红", "避免脸红", "拒绝夸张脸红", "不要夸张脸红",
    "脸色正常", "肤色正常", "自然肤色", "面部肤色保持自然", "面部保持自然肤色",
    "不出现泛红", "不泛红", "不要泛红", "面部无泛红", "面部不泛红",
    "只描述表情", "使用具体表情", "具体表情描述", "皱起眉头", "紧锁眉头", "瘪嘴", "咧着嘴",
)
_FACE_TRIGGER_EXCLUSIONS = (
    "生气勃勃",
)
_FACE_NEGATED_PHRASES = (
    "不生气", "没有生气", "没生气", "别生气", "不要生气",
    "不愤怒", "没有愤怒", "没愤怒", "别愤怒", "不要愤怒",
)
_PHONE_TRIGGER_PHRASES = (
    "手机", "智能手机", "电话", "移动电话", "手拿手机", "拿着手机", "拿手机",
)
_PHONE_SATISFIED_PHRASES = (
    "手机不要漏出屏幕", "不要漏出屏幕", "不要露出屏幕", "禁止漏出屏幕",
    "屏幕不要露", "屏幕不可见", "屏幕完全不可见", "手机屏幕不可见", "手机屏幕完全不可见", "屏幕完全隐藏", "屏幕完全朝向自己",
    "屏幕朝向自己", "不要让屏幕露出来", "不要让屏幕露出", "屏幕隐藏", "隐藏屏幕", "手机背面", "手机背面图片", "手机背面特写", "背面朝向镜头",
    "禁止露出手机屏幕", "禁止漏出手机屏幕",
)
_PHONE_EXPLICIT_OVERRIDE_PHRASES = (
    "手机屏幕清晰可见", "屏幕清晰可见", "屏幕可见", "展示手机屏幕", "显示手机屏幕",
    "露出手机屏幕", "漏出手机屏幕",
)


_PHONE_NEGATED_PHRASES = (
    "没有手机", "没手机", "不要手机", "不要出现手机", "无手机",
    "不出现手机", "没有出现手机", "禁止出现手机",
)

def _clean_title(title: str) -> str:
    cleaned = re.sub(r"^\d+\.?\s*", "", title or "").strip()
    return cleaned.rstrip("：:").strip()


def _contains_phrase(text: str, phrases: tuple[str, ...] | list[str]) -> bool:
    normalized = (text or "").strip()
    return any(phrase in normalized for phrase in phrases)


def _rule_kind(title: str) -> str:
    if any(word in title for word in ("手机", "屏幕")):
        return "phone"
    if any(word in title for word in ("脸红", "表情", "愤怒", "生气")):
        return "face"
    return "unknown"


def _rule_match(prompt: str, doc, title: str) -> tuple[bool, float]:
    kind = _rule_kind(title)
    if kind == "face":
        if _contains_phrase(prompt, _FACE_TRIGGER_EXCLUSIONS):
            return False, 0.0
        if _contains_phrase(prompt, _FACE_NEGATED_PHRASES):
            return False, 0.0
        if not _contains_phrase(prompt, _FACE_TRIGGER_PHRASES):
            return False, 0.0
        if _contains_phrase(prompt, _FACE_SATISFIED_PHRASES):
            return False, 0.0
        score = sum(1 for phrase in _FACE_TRIGGER_PHRASES if phrase in prompt)
        return True, float(score)

    if kind == "phone":
        if _contains_phrase(prompt, _PHONE_NEGATED_PHRASES):
            return False, 0.0
        if not _contains_phrase(prompt, _PHONE_TRIGGER_PHRASES):
            return False, 0.0
        if _contains_phrase(prompt, _PHONE_SATISFIED_PHRASES):
            return False, 0.0
        # An explicit request to show the screen is a user intention, not a
        # missing constraint. Do not override it with the KB recommendation.
        if _contains_phrase(prompt, _PHONE_EXPLICIT_OVERRIDE_PHRASES):
            return False, 0.0
        score = sum(1 for phrase in _PHONE_TRIGGER_PHRASES if phrase in prompt)
        return True, float(score)

    return False, 0.0



def _parse_json_object(text: str) -> dict | None:
    """从模型输出中提取 JSON，兼容代码围栏和少量前后说明。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\\s*|\\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\\{.*\\}", raw, flags=re.DOTALL)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None


def _semantic_verify_rules(prompt: str, candidates: list[tuple[float, object, str]]) -> list[dict] | None:
    """批量判断候选规则；None 表示裁决失败，空列表表示确认没有违规。"""
    if not candidates:
        return []
    rules = [
        {
            "id": index,
            "title": str(raw_title),
            "content": (doc.page_content or "").strip()[:4000],
            "similarity": round(float(similarity), 4),
        }
        for index, (similarity, doc, raw_title) in enumerate(candidates)
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "你是生成提示词规则判定器，只做判断，不改写提示词。\\n"
                "每条规则只能判断为 violation（明确违反）、satisfied（已明确满足）、"
                "irrelevant（无关）或 uncertain（无法确定）。\\n"
                "规则通常是禁止某种内容。用户明确写了不要、无、避免、背面、隐藏、遮挡等限制时，优先判为 satisfied。"
                "不能因为主题相似就判违反，只有用户明确要求生成被禁止内容时才判 violation。\\n"
                '只返回 JSON，不要 markdown：{"results":[{"id":0,"status":"violation","reason":"简短原因"}]}'
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"prompt": prompt, "rules": rules}, ensure_ascii=False),
        },
    ]
    try:
        result = _parse_json_object(chat(settings, messages, max_tokens=1200))
        if result is None or not isinstance(result.get("results"), list):
            return None
        verified: list[dict] = []
        for item in result["results"]:
            if not isinstance(item, dict) or item.get("status") != "violation":
                continue
            try:
                index = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(candidates):
                similarity, doc, raw_title = candidates[index]
                verified.append({
                    "title": raw_title,
                    "content": doc.page_content.strip(),
                    "score": similarity,
                    "source": "semantic",
                    "reason": str(item.get("reason") or "提示词明确触发了该规则").strip(),
                })
        return verified
    except Exception:
        logger.exception("semantic generation rule verification failed")
        return None

def _generation_kb_docs():
    now = time.time()
    with _generation_rag_lock:
        if (
            _generation_rag_cache["docs"]
            and now - _generation_rag_cache["fetched_at"] < GENERATION_RAG_TTL_SECONDS
        ):
            return _generation_rag_cache["docs"]

    markdown = fetch_kb_markdown(api_client, settings.lark_generation_kb_doc_id)
    docs = split_markdown(markdown)

    with _generation_rag_lock:
        _generation_rag_cache.update({"fetched_at": time.time(), "docs": docs, "vectors": []})
    return docs


def _generation_kb_vectors(docs) -> list[list[float]]:
    with _generation_rag_lock:
        cached = _generation_rag_cache.get("vectors") or []
        if len(cached) == len(docs):
            return cached

    vectors = _embed_texts_safe([_semantic_doc_text(doc) for doc in docs])
    with _generation_rag_lock:
        _generation_rag_cache["vectors"] = vectors
    return vectors


def _preflight_generation_prompt(prompt: str, optimize: bool = False) -> dict:
    try:
        docs = _generation_kb_docs()
        if not docs:
            return {"ok": True, "detected": False, "matches": [], "updated_prompt": prompt}

        matches: list[dict] = []
        unknown_entries: list[tuple[object, str, int]] = []
        for index, doc in enumerate(docs):
            raw_title = doc.metadata.get("error_title", "")
            title = _clean_title(raw_title)
            kind = _rule_kind(title)
            if kind == "unknown":
                unknown_entries.append((doc, raw_title, index))
                continue
            matched, score = _rule_match(prompt, doc, title)
            if not matched:
                continue
            matches.append(
                {
                    "title": raw_title,
                    "content": doc.page_content.strip(),
                    "score": score,
                }
            )

        if unknown_entries:
            doc_vectors = _generation_kb_vectors(docs)
            query_vector = _embed_query_safe(prompt)
            candidates: list[tuple[float, object, str]] = []
            if query_vector:
                for doc, raw_title, index in unknown_entries:
                    if index >= len(doc_vectors) or not doc_vectors[index]:
                        continue
                    similarity = _cosine_similarity(query_vector, doc_vectors[index])
                    if similarity >= GENERATION_RAG_SEMANTIC_MIN_SIMILARITY:
                        candidates.append((similarity, doc, raw_title))
            candidates.sort(key=lambda item: item[0], reverse=True)
            candidates = candidates[:GENERATION_RAG_SEMANTIC_TOP_K]

            semantic_matches = _semantic_verify_rules(prompt, candidates)
            if semantic_matches is not None:
                matches.extend(semantic_matches)
            else:
                # 语义裁决服务不可用时，保留确定性降级逻辑，避免阻塞生成流程。
                for similarity, doc, raw_title in candidates:
                    title = _clean_title(raw_title)
                    matched, _ = _generic_rule_match(prompt, doc, title)
                    if not matched:
                        continue
                    matches.append(
                        {
                            "title": raw_title,
                            "content": doc.page_content.strip(),
                            "score": similarity,
                            "source": "semantic-fallback",
                        }
                    )
        if not matches:
            return {"ok": True, "detected": False, "matches": [], "updated_prompt": prompt}

        matches.sort(key=lambda item: item["score"], reverse=True)
        if not optimize:
            return {"ok": True, "detected": True, "matches": matches, "updated_prompt": prompt}
        context = "\n\n".join(f"【{item['title']}】\n{item['content']}" for item in matches)
        updated_prompt = f"{prompt.strip()}\n\n[飞书知识库自动补充]\n{context}"
        optimized_prompt = _director_optimize_prompt(updated_prompt)
        if optimized_prompt:
            updated_prompt = optimized_prompt
        return {"ok": True, "detected": True, "matches": matches, "updated_prompt": updated_prompt}
    except Exception:
        logger.exception("generation KB preflight failed")
        return {"ok": False, "detected": False, "matches": [], "updated_prompt": prompt, "error": "RAG 检查暂时不可用"}


def _director_optimize_prompt(prompt: str) -> str:
    try:
        port = int(os.environ.get("DIRECTOR_PORT", "8895"))
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/optimize-prompt",
            data=json.dumps({"text": prompt, "mode": "rag"}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        if data.get("ok") and data.get("prompt"):
            return str(data["prompt"]).strip()
    except Exception:
        logger.warning("director rag prompt optimization unavailable; using KB context")
    return ""

@app.post("/api/rag/preflight")
async def rag_preflight(request: Request):
    token = portal_token()
    if token and verify_portal_identity(request.headers) is None:
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "JSON body required"})
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse(status_code=400, content={"error": "prompt required"})
    optimize = bool(body.get("optimize"))
    return await run_in_threadpool(_preflight_generation_prompt, prompt, optimize)
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
        # 图片先做视觉摘要，让语义闸门和后续 KB 都能看到截图中的报错信息。
        # 闸门只在这里提前拦截“明确无关”；报错或不确定内容仍继续查 KB。
        try:
            prep = prepare_query(
                text=question,
                image_data_urls=image_data_urls,
                summarizer=lambda urls: summarize_error_screenshots(settings, urls),
            )
        except Exception:
            # 视觉服务偶发失败时：有文字就继续查 KB；纯图片无法提取问题时，
            # 直接提示补充文字，且绝不进入源码扫描。
            logger.exception("image summarization failed")
            if not question:
                user_visible = "截图识别暂时失败，请把截图中的错误文字粘贴过来，或稍后重试。"
                return {
                    "answer": user_visible,
                    "coverage": "未命中",
                    "confidence": "图片识别失败",
                    "candidate_written": False,
                    "retrieved_titles": [],
                }
            prep = prepare_query(text=question, image_data_urls=[], summarizer=lambda _urls: "")
        # 视觉接口有时返回 HTTP 200 但 content 为空。纯图片没有可检索文本时
        # 不猜测、不扫码；同时有文字时退回文字继续查 KB。
        if image_data_urls and not prep.image_summary.strip():
            if not question:
                user_visible = "截图里暂时没有识别到错误文字，请把报错文本粘贴过来，或重新上传清晰截图。"
                return {
                    "answer": user_visible,
                    "coverage": "未命中",
                    "confidence": "图片摘要为空",
                    "candidate_written": False,
                    "retrieved_titles": [],
                }
            prep = prepare_query(text=question, image_data_urls=[], summarizer=lambda _urls: "")

        # KB 前只短路明确无关内容，以节省检索和生成调用。error_report、uncertain
        # 与 gate_error 一律继续查 KB，避免新型或描述不完整的真实报错被提前挡住。
        gate_decision = semantic_gate.decide(prep.context_text_for_generation)
        logger.info(
            "pre-KB semantic route: label=%s reason=%s error=%.4f unrelated=%.4f",
            gate_decision.label,
            gate_decision.reason,
            gate_decision.error_score,
            gate_decision.unrelated_score,
        )
        if gate_decision.label == "unrelated":
            user_visible = settings.unrelated_reply
            confidence = "KB 前语义判断为无关"
            try:
                append_query_log(
                    settings.query_log_path,
                    user_id="web",
                    query=question,
                    image_count=len(image_data_urls),
                    retrieved_titles=[],
                    answer=strip_coverage_tag(user_visible),
                    latency_ms=int((time.time() - start) * 1000),
                    metadata={
                        "coverage": "未查询",
                        "confidence": confidence,
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
                logger.exception("append_query_log failed (non-fatal)")
            return {
                "answer": user_visible,
                "coverage": "未查询",
                "confidence": confidence,
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
    else:
        # 复用 KB 前已经得到的语义决定，不再重复调用 embedding。只有明确命中
        # error_report 才允许扫码；uncertain 和服务异常都失败关闭。
        if not gate_decision.allow_scan:
            if gate_decision.label == "uncertain":
                user_visible = (
                    "KB 里没有找到足够匹配的条目，且暂时无法确认这是一个明确的报错问题。"
                    "请补充完整错误文本、错误码、任务状态或更清晰的截图。"
                )
                confidence = "KB 后语义判断不确定"
            else:
                user_visible = (
                    "KB 里没有找到足够匹配的条目，当前无法安全进行源码分析。"
                    "请稍后重试，或补充完整报错文本。"
                )
                confidence = "语义闸门异常"
        else:
            analysis = scan_and_analyze(
                # 图片摘要已进入 context，KB 未命中时源码兜底也必须看到这份摘要。
                query_text=prep.context_text_for_generation,
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
