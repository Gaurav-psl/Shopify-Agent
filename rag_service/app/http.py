"""One shared httpx client per process (created/closed by main.py's lifespan)."""
import httpx

_client: httpx.AsyncClient | None = None


def get() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
