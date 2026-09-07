"""
Queries a Dify Knowledge Base for relevant chunks and caches results in a
short-lived in-memory store, so repeated/similar queries within the TTL
window don't re-hit the API.

Keeps this concern separate from reply_generator.py on purpose:
  - rag_retriever.py decides WHAT context to fetch, and caches it
  - reply_generator.py decides HOW to phrase the answer using that context

Env vars expected:
  DIFY_BASE_URL      e.g. "https://dify.yourdomain.com"
  DIFY_DATASET_ID    the knowledge base's UUID
  DIFY_DATASET_API_KEY  the "dataset-..." key generated for that KB
"""

import os
import time
import hashlib
import requests

DIFY_BASE_URL = os.environ.get("DIFY_BASE_URL", "").rstrip("/")
DIFY_DATASET_ID = os.environ.get("DIFY_DATASET_ID", "")
DIFY_DATASET_API_KEY = os.environ.get("DIFY_DATASET_API_KEY", "")

# How long a cached result stays valid, in seconds. Short-lived by design —
# this is meant to smooth out repeated/near-duplicate queries in a single
# conversation burst, not act as a long-term store.
CACHE_TTL_SECONDS = 300

# query_hash -> {"timestamp": float, "chunks": list[dict]}
_cache: dict[str, dict] = {}


def _cache_key(query: str, top_k: int) -> str:
    raw = f"{query.strip().lower()}::{top_k}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prune_expired() -> None:
    now = time.time()
    expired = [k for k, v in _cache.items() if now - v["timestamp"] > CACHE_TTL_SECONDS]
    for k in expired:
        del _cache[k]


def retrieve_context(
    query: str,
    top_k: int = 3,
    score_threshold: float | None = 0.5,
    use_cache: bool = True,
) -> list[dict]:
    """
    Returns a list of chunks: [{"content": str, "score": float, "document": str}, ...]
    Returns an empty list on any failure — callers should treat that as
    "no relevant context found" rather than crash the reply pipeline.
    """
    if not query or not query.strip():
        return []

    if not (DIFY_BASE_URL and DIFY_DATASET_ID and DIFY_DATASET_API_KEY):
        print("rag_retriever: missing DIFY_BASE_URL / DIFY_DATASET_ID / DIFY_DATASET_API_KEY, skipping retrieval")
        return []

    key = _cache_key(query, top_k)

    if use_cache:
        _prune_expired()
        cached = _cache.get(key)
        if cached:
            return cached["chunks"]

    payload = {
        "query": query,
        "retrieval_model": {
            "search_method": "hybrid_search",
            "reranking_enable": False,
            "top_k": top_k,
            "score_threshold_enabled": score_threshold is not None,
            "score_threshold": score_threshold or 0.0,
        },
    }

    try:
        resp = requests.post(
            f"{DIFY_BASE_URL}/v1/datasets/{DIFY_DATASET_ID}/retrieve",
            headers={
                "Authorization": f"Bearer {DIFY_DATASET_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        records = resp.json().get("records", [])
    except Exception as e:
        print(f"rag_retriever: retrieval failed ({e!r}), returning no context")
        return []

    chunks = [
        {
            "content": r.get("segment", {}).get("content", ""),
            "score": r.get("score", 0.0),
            "document": r.get("segment", {}).get("document", {}).get("name", ""),
        }
        for r in records
        if r.get("segment", {}).get("content")
    ]

    if use_cache:
        _cache[key] = {"timestamp": time.time(), "chunks": chunks}

    return chunks


def clear_cache() -> None:
    """Optional manual reset, e.g. between test runs or if the KB was just re-indexed."""
    _cache.clear()
