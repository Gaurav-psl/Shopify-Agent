"""Turns a document (markdown / html / text) into chunks and stores them. Re-ingesting a document replaces it, and
skips the embedding cost entirely when nothing changed."""
import asyncio
import hashlib
import re
import time

from . import config, embeddings, store, textutil
from .chunking import chunk_markdown
from .errors import ServiceError
from .htmlmd import html_to_markdown

_sem = asyncio.Semaphore(config.INGEST_CONCURRENCY)
_DOC_ID = re.compile(r"^[^\s]{1,300}$")


def _title_from(text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.*\S)", text, re.M)
    return (m.group(1).strip() if m else fallback)[:200]


async def index_document(tenant: str, doc_id: str, text: str, *, title: str = "", kind: str | None = None,
                         policy_type: str | None = None, url: str = "", fmt: str = "markdown",
                         force: bool = False) -> dict:
    if not tenant or not _DOC_ID.match(doc_id or ""):
        raise ServiceError(400, "tenant and doc_id are required (doc_id must not contain whitespace)")
    if len(text or "") > config.MAX_DOC_CHARS:
        raise ServiceError(413, f"document too large (max {config.MAX_DOC_CHARS} characters)")
    if fmt == "html":
        text = html_to_markdown(text)
    text = textutil.clean_markdown(text)
    if not text:
        await store.delete_doc(tenant, doc_id)          # an emptied page should disappear from search
        return {"doc_id": doc_id, "status": "empty", "chunks": 0}

    guess = textutil.infer_metadata(url or doc_id)
    kind = kind or guess["kind"]
    policy_type = policy_type or guess["policy_type"]
    title = (title or "").strip() or _title_from(text, doc_id)

    chunks = chunk_markdown(text, config.CHUNK_CHARS)
    if not chunks:
        return {"doc_id": doc_id, "status": "empty", "chunks": 0}

    doc_hash = hashlib.sha256("|".join([title, str(kind), str(policy_type), url, text, str(config.CHUNK_CHARS),
                                        config.EMBED_MODEL]).encode()).hexdigest()
    async with _sem:
        if not force and await store.get_doc_hash(tenant, doc_id) == doc_hash:
            return {"doc_id": doc_id, "status": "unchanged", "chunks": len(chunks)}

        def enriched(c) -> str:                         # heading path helps both embedders and keyword search
            head = c.section if c.section.lower().startswith(title.lower()) else " > ".join(x for x in (title, c.section) if x)
            return f"{head}\n{c.body}"

        texts = [enriched(c) for c in chunks]
        dense = await embeddings.embed(texts)
        await store.ensure_collection(len(dense[0]))
        now = int(time.time())
        rows = [{
            "dense": dense[i],
            "sparse": textutil.sparse_vector(texts[i]),
            "payload": {"text": c.body, "section": c.section, "title": title, "kind": kind, "policy_type": policy_type,
                        "url": url, "doc_hash": doc_hash, "n_chunks": len(chunks), "updated_at": now},
        } for i, c in enumerate(chunks)]
        await store.upsert_chunks(tenant, doc_id, rows)
    return {"doc_id": doc_id, "status": "indexed", "chunks": len(chunks)}


async def index_batch(tenant: str, docs: list[dict], prune_prefix: str | None = None, force: bool = False) -> dict:
    """Indexes many documents. With prune_prefix, documents of this tenant whose id starts with the prefix and
    that are not in the batch are deleted (e.g. products removed from Shopify). Pruning only happens if every
    document succeeded, so a partial failure can never wipe the index."""
    async def one(d: dict) -> dict:
        try:
            return await index_document(tenant, d["doc_id"], d.get("text", ""), title=d.get("title", ""),
                                        kind=d.get("kind"), policy_type=d.get("policy_type"), url=d.get("url", ""),
                                        fmt=d.get("format", "markdown"), force=force)
        except ServiceError as e:
            return {"doc_id": d.get("doc_id"), "status": "error", "error": e.message}
        except Exception as e:  # noqa: BLE001
            return {"doc_id": d.get("doc_id"), "status": "error", "error": repr(e)}

    results = await asyncio.gather(*(one(d) for d in docs))
    pruned: list[str] = []
    if prune_prefix and all(r["status"] != "error" for r in results):
        keep = {d["doc_id"] for d in docs}
        for existing in await store.list_docs(tenant):
            did = existing.get("doc_id", "")
            if did.startswith(prune_prefix) and did not in keep:
                await store.delete_doc(tenant, did)
                pruned.append(did)
    summary: dict = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {"summary": summary, "pruned": pruned, "results": results}
