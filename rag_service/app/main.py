"""RAG service API. Run:  uvicorn app.main:app --host 0.0.0.0 --port 8080

Retrieval:   POST /v1/retrieve
Documents:   POST /v1/documents | POST /v1/documents/batch | GET /v1/documents?tenant= | DELETE /v1/documents
Web:         POST /v1/scrape | POST /v1/crawl | GET /v1/crawl/{job_id}
Tenants:     GET /v1/tenants/{tenant}/stats | DELETE /v1/tenants/{tenant}
Every route except /health needs  Authorization: Bearer $RAG_API_KEY."""
import hmac
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import config, crawler, http, indexer, retrieval, store
from .errors import ServiceError


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not config.RAG_API_KEY:
        print("WARNING: RAG_API_KEY is not set - every request will be rejected")
    if not config.QDRANT_URL:
        print("WARNING: QDRANT_URL is not set - using embedded Qdrant at", config.QDRANT_PATH, "(dev only)")
    yield
    await http.close()
    await store.close()


app = FastAPI(title="Store RAG service", version="2.0", lifespan=lifespan)


@app.exception_handler(ServiceError)
async def _service_error(_: Request, e: ServiceError):
    return JSONResponse({"error": e.message}, status_code=e.status)


@app.exception_handler(Exception)
async def _unexpected(_: Request, e: Exception):
    print(f"unhandled: {e!r}")
    return JSONResponse({"error": "internal error"}, status_code=500)


def auth(authorization: str = Header(default="")) -> None:
    if not config.RAG_API_KEY:
        raise ServiceError(503, "service has no RAG_API_KEY configured")
    token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(token.encode(), config.RAG_API_KEY.encode()):
        raise ServiceError(401, "invalid or missing API key")


def _tenant(v: str) -> str:
    v = (v or "").strip().lower()
    if not v or len(v) > 200 or any(ch.isspace() for ch in v):
        raise ValueError("tenant must be a non-empty store identifier without spaces")
    return v


class Tenanted(BaseModel):
    tenant: str

    @field_validator("tenant")
    @classmethod
    def _t(cls, v):
        return _tenant(v)


class Hints(BaseModel):
    policy_type: str | list[str] | None = None     # e.g. "refund_policy"  -> soft metadata filter
    kinds: list[str] | None = None                 # e.g. ["policy", "faq"]
    language: str | None = None                    # classifier's language code, e.g. "hi"


class RetrieveReq(Tenanted):
    query: str = Field(max_length=2000)
    top_k: int = 3
    hints: Hints | None = None
    deadline_ms: int | None = None


class DocReq(Tenanted):
    doc_id: str
    text: str
    title: str = ""
    kind: str | None = None
    policy_type: str | None = None
    url: str = ""
    format: str = "markdown"                       # markdown | html | text
    force: bool = False


class DocItem(BaseModel):
    doc_id: str
    text: str
    title: str = ""
    kind: str | None = None
    policy_type: str | None = None
    url: str = ""
    format: str = "markdown"


class BatchReq(Tenanted):
    documents: list[DocItem] = Field(max_length=500)
    prune_prefix: str | None = None                # delete docs with this id prefix that are missing from the batch
    force: bool = False


class DeleteDocReq(Tenanted):
    doc_id: str


class ScrapeReq(Tenanted):
    url: str


class CrawlReq(Tenanted):
    url: str
    limit: int | None = None
    include_paths: list[str] | None = None
    exclude_paths: list[str] | None = None


@app.get("/health")
async def health(deep: bool = False):
    out = {"ok": True, "llm": config.LLM_ENABLED, "rerank": config.RERANK_ENABLED, "embedding_model": config.EMBED_MODEL}
    if deep:
        try:
            out["qdrant"] = True
            out["collection"] = await store.collection_exists()
        except Exception as e:  # noqa: BLE001
            out["ok"], out["qdrant"], out["error"] = False, False, repr(e)[:200]
    return out


@app.post("/v1/retrieve", dependencies=[Depends(auth)])
async def retrieve(req: RetrieveReq):
    return await retrieval.retrieve(req.tenant, req.query, req.top_k, req.hints.model_dump() if req.hints else None,
                                    req.deadline_ms)


@app.post("/v1/documents", dependencies=[Depends(auth)])
async def put_document(req: DocReq):
    return await indexer.index_document(req.tenant, req.doc_id, req.text, title=req.title, kind=req.kind,
                                        policy_type=req.policy_type, url=req.url, fmt=req.format, force=req.force)


@app.post("/v1/documents/batch", dependencies=[Depends(auth)])
async def put_documents(req: BatchReq):
    return await indexer.index_batch(req.tenant, [d.model_dump() for d in req.documents], req.prune_prefix, req.force)


@app.get("/v1/documents", dependencies=[Depends(auth)])
async def list_documents(tenant: str):
    return {"documents": await store.list_docs(_tenant(tenant))}


@app.delete("/v1/documents", dependencies=[Depends(auth)])
async def delete_document(req: DeleteDocReq):
    await store.delete_doc(req.tenant, req.doc_id)
    return {"deleted": req.doc_id}


@app.post("/v1/scrape", dependencies=[Depends(auth)])
async def scrape(req: ScrapeReq):
    return await crawler.scrape_and_index(req.tenant, req.url)


@app.post("/v1/crawl", dependencies=[Depends(auth)], status_code=202)
async def crawl(req: CrawlReq):
    return await crawler.start_crawl(req.tenant, req.url, req.limit, req.include_paths, req.exclude_paths)


@app.get("/v1/crawl/{job_id}", dependencies=[Depends(auth)])
async def crawl_status(job_id: str):
    return crawler.get_job(job_id)


@app.get("/v1/tenants/{tenant}/stats", dependencies=[Depends(auth)])
async def tenant_stats(tenant: str):
    t = _tenant(tenant)
    docs = await store.list_docs(t)
    return {"tenant": t, "documents": len(docs), "chunks": await store.count(t),
            "by_kind": {k: sum(1 for d in docs if d.get("kind") == k) for k in {d.get("kind") for d in docs}}}


@app.delete("/v1/tenants/{tenant}", dependencies=[Depends(auth)])
async def delete_tenant(tenant: str):
    t = _tenant(tenant)
    await store.delete_tenant(t)
    return {"deleted": t}
