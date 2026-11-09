"""Dense embeddings through any OpenAI-compatible /embeddings endpoint."""
import asyncio

import httpx

from . import config, http
from .errors import ServiceError


async def embed(texts: list[str], query: bool = False, timeout: float | None = None) -> list[list[float]]:
    prefix = config.EMBED_QUERY_PREFIX if query else config.EMBED_DOC_PREFIX
    out: list[list[float]] = []
    for i in range(0, len(texts), config.EMBED_BATCH):
        batch = [prefix + t for t in texts[i:i + config.EMBED_BATCH]]
        out.extend(await _call(batch, timeout or config.EMBED_TIMEOUT_S, retries=1 if query else 3))
    return out


async def _call(batch: list[str], timeout: float, retries: int) -> list[list[float]]:
    body: dict = {"model": config.EMBED_MODEL, "input": batch}
    if config.EMBED_DIMENSIONS:
        body["dimensions"] = config.EMBED_DIMENSIONS
    headers = {"Authorization": f"Bearer {config.EMBED_API_KEY}"} if config.EMBED_API_KEY else {}
    last = "unreachable"
    for attempt in range(retries):
        try:
            r = await http.get().post(f"{config.EMBED_BASE_URL}/embeddings", json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as e:
            last = repr(e)
        else:
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
            elif r.status_code >= 400:
                raise ServiceError(502, f"embedding provider error {r.status_code}: {r.text[:200]}")
            else:
                rows = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
                return [d["embedding"] for d in rows]
        if attempt + 1 < retries:
            await asyncio.sleep(0.5 * 2 ** attempt)
    raise ServiceError(502, f"embedding provider failed: {last}")
