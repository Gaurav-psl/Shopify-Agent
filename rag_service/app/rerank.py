"""Optional cross-encoder reranker (Cohere / Jina / vLLM style POST {base}/rerank)."""
from . import config, http


async def rerank(query: str, docs: list[str], timeout: float | None = None) -> dict[int, float] | None:
    """{doc index: relevance score}, or None when disabled / failed (caller keeps the fused order)."""
    if not (config.RERANK_ENABLED and docs):
        return None
    headers = {"Authorization": f"Bearer {config.RERANK_API_KEY}"} if config.RERANK_API_KEY else {}
    try:
        r = await http.get().post(
            f"{config.RERANK_BASE_URL}/rerank", headers=headers, timeout=timeout or config.RERANK_TIMEOUT_S,
            json={"model": config.RERANK_MODEL, "query": query, "documents": docs, "top_n": len(docs)})
        r.raise_for_status()
        return {int(x["index"]): float(x["relevance_score"]) for x in r.json()["results"]}
    except Exception as e:  # noqa: BLE001
        print(f"rerank: {e!r}")
        return None
