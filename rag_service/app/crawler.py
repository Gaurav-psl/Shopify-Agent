"""Firecrawl integration: crawl a site (or scrape one page) and index every page. Crawls run in the background;
job state lives in memory (a restart forgets job status, never indexed data)."""
import asyncio
import ipaddress
import time
import uuid
from urllib.parse import urlparse

import httpx

from . import config, http, indexer, textutil
from .errors import ServiceError

_jobs: dict[str, dict] = {}
_tasks: set[asyncio.Task] = set()


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.FIRECRAWL_API_KEY}"} if config.FIRECRAWL_API_KEY else {}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def validate_url(url: str) -> str:
    p = urlparse(url or "")
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ServiceError(400, "url must be a full http(s) address")
    h = p.hostname.lower()
    private = h == "localhost" or h.endswith(".local") or h.endswith(".internal")
    try:
        ip = ipaddress.ip_address(h)
        private = private or ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass
    if private:
        raise ServiceError(400, "private / local addresses cannot be crawled")
    if config.ALLOWED_CRAWL_HOSTS and h not in config.ALLOWED_CRAWL_HOSTS:
        raise ServiceError(403, f"host '{h}' is not in ALLOWED_CRAWL_HOSTS")
    return url


def _allowed(page_url: str, start_url: str) -> bool:
    """Only pages on the start host (or an explicitly allowed host) are indexed: crawled text goes into your reply
    prompt, so it must come from a site you chose."""
    h = _host(page_url)
    return h == _host(start_url) or h in config.ALLOWED_CRAWL_HOSTS


def _fc_error(r: httpx.Response) -> ServiceError:
    detail = ""
    try:
        detail = r.json().get("error") or ""
    except Exception:  # noqa: BLE001
        detail = r.text[:200]
    return ServiceError(502, f"firecrawl error {r.status_code}: {detail}")


async def _index_pages(tenant: str, start_url: str, pages: list[dict]) -> dict:
    docs, skipped = [], 0
    for pg in pages:
        meta = pg.get("metadata") or {}
        url = meta.get("sourceURL") or meta.get("url") or ""
        md = pg.get("markdown") or ""
        if not url or not md.strip() or (meta.get("statusCode") or 200) >= 400 or not _allowed(url, start_url):
            skipped += 1
            continue
        docs.append({"doc_id": f"page:{url.split('#')[0]}", "text": md, "title": (meta.get("title") or "").strip(),
                     "url": url})
    out = await indexer.index_batch(tenant, docs)
    out["skipped"] = skipped
    return out


# ------------------------------------------------------------------ single page
async def scrape_and_index(tenant: str, url: str) -> dict:
    validate_url(url)
    r = await http.get().post(f"{config.FIRECRAWL_BASE_URL}/scrape", headers=_headers(), timeout=90,
                              json={"url": url, "formats": ["markdown"], "onlyMainContent": True})
    if r.status_code >= 400:
        raise _fc_error(r)
    data = r.json().get("data") or {}
    return await _index_pages(tenant, url, [data])


# ------------------------------------------------------------------ whole site
async def start_crawl(tenant: str, url: str, limit: int | None = None, include_paths: list[str] | None = None,
                      exclude_paths: list[str] | None = None) -> dict:
    validate_url(url)
    job = {"job_id": uuid.uuid4().hex, "tenant": tenant, "url": url, "status": "queued", "found": 0, "scraped": 0,
           "indexed": 0, "error": None, "started_at": int(time.time())}
    if len(_jobs) > 200:                                    # keep memory bounded
        for k in sorted(_jobs, key=lambda k: _jobs[k]["started_at"])[:100]:
            _jobs.pop(k, None)
    _jobs[job["job_id"]] = job
    task = asyncio.create_task(_run(job, min(limit or config.MAX_CRAWL_PAGES, config.MAX_CRAWL_PAGES),
                                    include_paths, exclude_paths))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


def get_job(job_id: str) -> dict:
    if job_id not in _jobs:
        raise ServiceError(404, "unknown job (job status is lost when the service restarts)")
    return _jobs[job_id]


async def _run(job: dict, limit: int, include_paths, exclude_paths) -> None:
    try:
        body: dict = {"url": job["url"], "limit": limit, "allowExternalLinks": False,
                      "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True}}
        if include_paths:
            body["includePaths"] = include_paths
        if exclude_paths:
            body["excludePaths"] = exclude_paths
        r = await http.get().post(f"{config.FIRECRAWL_BASE_URL}/crawl", headers=_headers(), json=body, timeout=60)
        if r.status_code >= 400:
            raise _fc_error(r)
        fc_id = r.json().get("id")
        if not fc_id:
            raise ServiceError(502, "firecrawl did not return a crawl id")
        job["status"] = "crawling"
        deadline = time.monotonic() + config.CRAWL_TIMEOUT_S
        pages: list[dict] = []
        while True:
            if time.monotonic() > deadline:
                raise ServiceError(504, "crawl timed out (raise CRAWL_TIMEOUT_S or lower the limit)")
            s = await http.get().get(f"{config.FIRECRAWL_BASE_URL}/crawl/{fc_id}", headers=_headers(), timeout=60)
            if s.status_code >= 400:
                raise _fc_error(s)
            st = s.json()
            job["found"], job["scraped"] = st.get("total", 0), st.get("completed", 0)
            if st.get("status") == "completed":
                pages = list(st.get("data") or [])
                nxt = st.get("next")
                while nxt:                                  # large crawls come back in pages of results
                    pr = await http.get().get(nxt, headers=_headers(), timeout=60)
                    if pr.status_code >= 400:
                        raise _fc_error(pr)
                    pj = pr.json()
                    pages += pj.get("data") or []
                    nxt = pj.get("next")
                break
            if st.get("status") in ("failed", "cancelled"):
                raise ServiceError(502, f"firecrawl crawl {st.get('status')}")
            await asyncio.sleep(config.CRAWL_POLL_S)
        job["status"] = "indexing"
        res = await _index_pages(job["tenant"], job["url"], pages)
        job["indexed"] = res["summary"].get("indexed", 0) + res["summary"].get("unchanged", 0)
        job["summary"], job["skipped"] = res["summary"], res["skipped"]
        job["errors"] = [r for r in res["results"] if r["status"] == "error"][:10]
        job["status"] = "done"
    except ServiceError as e:
        job["status"], job["error"] = "failed", e.message
    except Exception as e:  # noqa: BLE001
        job["status"], job["error"] = "failed", repr(e)
    job["finished_at"] = int(time.time())
