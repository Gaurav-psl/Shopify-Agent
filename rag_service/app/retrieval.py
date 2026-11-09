"""The retrieval pipeline. For each request:

  1. query preparation   optional LLM step: translate to the documents' language (+ paraphrases). Skipped when not
                         needed, when no LLM is configured, or when the deadline is tight.
  2. query construction  hints from your backend (policy_type, kinds) become a metadata filter. The filter is soft:
                         if it matches nothing (that policy isn't indexed), the search is repeated without it.
  3. hybrid search       dense (semantic) + sparse (keyword) search per query variant, merged with reciprocal-rank
                         fusion. If the embedding provider is down it degrades to keyword-only.
  4. rerank              optional cross-encoder over the best candidates.
  5. cutoff + dedupe     irrelevant chunks are dropped here, so [] means "nothing relevant" and the backend never
                         sees weak documents.

Every optional step fails open: a slow or broken helper never breaks retrieval."""
import asyncio
import hashlib
import time

from . import config, embeddings, llm, rerank, store, textutil
from .errors import ServiceError

_LANG = {"en": "English", "hi": "Hindi", "pa": "Punjabi", "es": "Spanish", "fr": "French", "de": "German"}


def _needs_translation(query: str, language: str | None) -> bool:
    mode = config.QUERY_TRANSLATION
    if not config.LLM_ENABLED or mode == "off":
        return False
    if mode == "always":
        return True
    lang = (language or "").lower().split("-")[0]
    if lang:                                          # the classifier's language wins (it also catches romanised Hindi)
        return lang != config.DOC_LANGUAGE
    return textutil.detect_script(query) == "non_latin"


async def _prepare(query: str, translate: bool, timeout: float) -> list[str]:
    n = config.QUERY_EXPANSIONS
    if not ((translate or n) and config.LLM_ENABLED) or timeout < 0.3:
        return []
    lang = _LANG.get(config.DOC_LANGUAGE, config.DOC_LANGUAGE)
    system = (f"You write search queries for an online store's knowledge base (policies, FAQs, product information) "
              f"written in {lang}. Keep product names, brand names, numbers and order ids unchanged. "
              "Output only the queries, one per line, with no numbering and no commentary.")
    user = f"Shopper message: {query}\nLine 1: the message as one short standalone search query in {lang}."
    if n:
        user += f"\nThen {n} more lines: alternative phrasings that use different words."
    raw = await llm.complete(system, user, max_tokens=60 + 40 * n, timeout=timeout)
    if not raw:
        return []
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-*•0123456789.) ").strip(" \"'")
        if line and len(line) <= 300 and line.lower() != query.strip().lower() and line not in out:
            out.append(line)
    return out[:1 + n]


async def _embed_query(texts: list[str], timeout: float) -> list[list[float] | None]:
    try:
        return await embeddings.embed(texts, query=True, timeout=max(timeout, 0.5))
    except ServiceError as e:
        print(f"retrieval: dense embedding unavailable, keyword-only ({e.message})")
        return [None] * len(texts)


async def _search_all(tenant, variants, vectors, flt_pt, flt_kind) -> list[tuple[str, list[dict]]]:
    jobs = []
    for text, vec in zip(variants, vectors):
        if vec is not None:
            jobs.append(("dense", store.search(tenant, "dense", vec, config.CANDIDATES, flt_pt, flt_kind)))
        sp = textutil.sparse_vector(text, query=True)
        if sp:
            jobs.append(("sparse", store.search(tenant, "sparse", store.sparse_query(*sp), config.CANDIDATES,
                                                flt_pt, flt_kind)))
    results = await asyncio.gather(*(j[1] for j in jobs))
    return [(j[0], r) for j, r in zip(jobs, results)]


def _fuse(lists: list[tuple[str, list[dict]]]) -> list[dict]:
    fused: dict[str, dict] = {}
    for kind, hits in lists:
        for rank, h in enumerate(hits):
            e = fused.setdefault(h["id"], {"id": h["id"], "payload": h["payload"], "rrf": 0.0, "dense": None})
            e["rrf"] += 1.0 / (config.RRF_K + rank + 1)
            if kind == "dense" and (e["dense"] is None or h["score"] > e["dense"]):
                e["dense"] = h["score"]
    return sorted(fused.values(), key=lambda e: e["rrf"], reverse=True)


def _as_list(v) -> list[str] | None:
    if not v:
        return None
    return [str(x) for x in (v if isinstance(v, (list, tuple)) else [v])]


def _doc_text(p: dict) -> str:
    head = " > ".join(x for x in (p.get("title"), p.get("section")) if x)
    return f"{head}\n{p.get('text', '')}" if head else p.get("text", "")


async def retrieve(tenant: str, query: str, top_k: int = 3, hints: dict | None = None,
                   deadline_ms: int | None = None) -> dict:
    t0 = time.monotonic()
    budget = (deadline_ms or config.DEFAULT_DEADLINE_MS) / 1000
    left = lambda: budget - (time.monotonic() - t0)  # noqa: E731
    top_k = max(1, min(int(top_k or 3), config.MAX_TOP_K))
    hints = hints or {}
    meta: dict = {"strategy": "plain", "translated": False, "reranked": False, "degraded": []}

    def done(results: list[dict]) -> dict:
        meta["latency_ms"] = int((time.monotonic() - t0) * 1000)
        meta["returned"] = len(results)
        meta["confidence"] = max((r["score"] for r in results), default=0.0)
        return {"results": results, "meta": meta}

    if not query.strip() or not await store.collection_exists():
        meta["reason"] = "empty_query" if not query.strip() else "no_index"
        return done([])

    # 1 ── prepare variants (LLM runs while the original query is being embedded)
    translate = _needs_translation(query, hints.get("language"))
    prep = asyncio.create_task(_prepare(query, translate, min(config.LLM_TIMEOUT_S, left() * 0.5)))
    orig_vec = (await _embed_query([query], left() * 0.6))[0]
    extra = await prep
    variants, vectors = [query], [orig_vec]
    if extra:
        variants += extra
        vectors += await _embed_query(extra, left() * 0.5)
        meta["translated"] = translate
    meta["queries"] = variants
    if all(v is None for v in vectors):
        meta["degraded"].append("keyword_only")

    # 2+3 ── query construction (soft metadata filter) + hybrid search
    pt, kinds = _as_list(hints.get("policy_type")), _as_list(hints.get("kinds"))
    cands = _fuse(await _search_all(tenant, variants, vectors, pt, kinds))
    if pt or kinds:
        meta["strategy"] = "filtered"
        if not cands:       # the hinted policy / kind isn't indexed for this store: search everything instead
            cands = _fuse(await _search_all(tenant, variants, vectors, None, None))
            meta["strategy"] = "filtered+fallback"

    # dedupe identical text (crawled pages repeat headers / footers)
    uniq, seen_hash = [], set()
    for c in cands:
        h = hashlib.md5(" ".join(c["payload"].get("text", "").lower().split()).encode()).hexdigest()
        if h not in seen_hash:
            seen_hash.add(h)
            uniq.append(c)
    cands = uniq
    meta["candidates"] = len(cands)

    # 4 ── rerank (optional)
    if config.RERANK_ENABLED and cands and left() > 0.4:
        pool = cands[:config.RERANK_CANDIDATES]
        scores = await rerank.rerank(variants[1] if extra else query, [_doc_text(c["payload"]) for c in pool],
                                     timeout=min(config.RERANK_TIMEOUT_S, left() * 0.8))
        if scores is not None:
            for i, c in enumerate(pool):
                c["rerank"] = scores.get(i)
            cands = sorted((c for c in pool if c.get("rerank") is not None), key=lambda c: c["rerank"], reverse=True)
            meta["reranked"] = True
        else:
            meta["degraded"].append("rerank_failed")

    # 5 ── cutoff
    if meta["reranked"]:
        kept = [c for c in cands if c["rerank"] >= config.MIN_SCORE_RERANK]
    else:       # keyword-only matches have no cosine score and are kept; they matched real words
        kept = [c for c in cands if c["dense"] is None or c["dense"] >= config.MIN_SCORE_DENSE]
    results = []
    for c in kept[:top_k]:
        p = c["payload"]
        score = c["rerank"] if meta["reranked"] else (c["dense"] if c["dense"] is not None else 0.0)
        results.append({
            "content": p.get("text", ""), "score": round(float(score), 4), "document": p.get("title") or p.get("doc_id", ""),
            "section": p.get("section", ""), "url": p.get("url", ""), "kind": p.get("kind"),
            "policy_type": p.get("policy_type"), "doc_id": p.get("doc_id", ""),
        })
    return done(results)
