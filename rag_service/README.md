# Store RAG service

A standalone retrieval service for your Shopify chatbot: **ingest** (Firecrawl, Shopify, raw text) and **retrieve**
(hybrid search, translation, rerank, cutoffs). Your backend keeps deciding *whether* to retrieve (intent_schema.json →
reply_generator); this service only answers "which documents are relevant to this question for this store?".

```
reply_generator ─ retrieve_context() ─► backend_client/rag_retriever.py ──HTTP──► this service ─► Qdrant Cloud
   (unchanged)        (same signature)      cache · circuit breaker                 ├─► embeddings API
                                                                                    ├─► LLM (optional: translation)
                                                                                    ├─► reranker (optional)
                                                                                    └─► Firecrawl (crawl / scrape)
```

## Layout
```
app/            the service      main.py (API) · retrieval.py · indexer.py · crawler.py · store.py · embeddings.py
                                 llm.py · rerank.py · chunking.py · textutil.py · htmlmd.py · config.py · errors.py
backend_client/rag_retriever.py  goes into your BACKEND (replaces the Dify version)
tools/rag_sync.py                Shopify policies + products -> index
eval/run_eval.py                 hit-rate / MRR / cutoff tuning on your own questions
tests/run_tests.sh               offline end-to-end tests (fake providers, real Qdrant)
```

## 1. Qdrant Cloud
cloud.qdrant.io → Clusters → Create (free tier is fine to start) → copy the **cluster URL** (`https://…:6333`) and the
**API key** (shown once). Check: `curl "$QDRANT_URL/healthz" -H "api-key: $QDRANT_API_KEY"`.
The collection is created automatically on the first ingest (dense + keyword vectors, tenant index).

## 2. Configure
`cp .env.example .env` and fill: `RAG_API_KEY` (generate one), `QDRANT_*`, `EMBEDDING_*`, `FIRECRAWL_*`.
Optional: `LLM_*` (translation of Hindi/Punjabi questions), `RERANK_*`.
Multilingual shoppers: prefer a multilingual embedding model (bge-m3, multilingual-e5 …) — it often makes the LLM
translation step unnecessary. Test both with `eval/run_eval.py` before paying for the extra call.

## 3. Run / deploy
Local: `pip install -r requirements.txt && uvicorn app.main:app --port 8080` (no `QDRANT_URL` = embedded dev storage).
Production: deploy the Dockerfile (Railway, Render, Fly.io, a VPS …) with the env vars. Keep **one instance** (crawl-job
status is in memory) on an always-on plan (sleeping hosts add cold-start delay to chat). `GET /health` is public;
`/health?deep=1` also checks Qdrant.

## 4. Connect your backend
1. Replace your `rag_retriever.py` with `backend_client/rag_retriever.py`.
2. Backend env: `RAG_SERVICE_URL`, `RAG_API_KEY`.
3. `reply_generator.py` needs **no change**: `retrieve_context(store, query)` returns `[{content, score, document, …}]`.
   Policy queries built as `"refund policy: <message>"` are turned into a metadata hint automatically.
4. Optional: `aretrieve_context(...)` is a non-blocking twin if you make reply generation async.
5. Failure behaviour: any error → `[]` (reply without reference material); after 3 failures the client pauses 30 s.

## 5. Build the knowledge base
```bash
export RAG_SERVICE_URL=https://rag.example.com RAG_API_KEY=... SHOPIFY_ADMIN_TOKEN=shpat_...
python tools/rag_sync.py my-store.myshopify.com          # policies + product descriptions (re-runnable)

curl -X POST $RAG_SERVICE_URL/v1/crawl -H "Authorization: Bearer $RAG_API_KEY" -H 'content-type: application/json' \
  -d '{"tenant":"my-store.myshopify.com","url":"https://my-store.com","limit":25,"include_paths":["/pages/.*"]}'
curl $RAG_SERVICE_URL/v1/crawl/<job_id> -H "Authorization: Bearer $RAG_API_KEY"    # queued → crawling → indexing → done
```
Same from Python: `rag_retriever.crawl_site / scrape_page / ingest_text / job_status / delete_store`.
Re-ingesting an unchanged document costs nothing (content hash). Run the sync on a schedule or from a product-update
webhook. Call `delete_store` from uninstall / `shop/redact`.
Scopes for the sync token: `read_products`, `read_legal_policies`. Prices and stock are **not** indexed — answer those
from Shopify live.

## 6. Tune with numbers
Write 30–100 real questions in `eval/questions.example.jsonl` format (include questions that must return nothing), then
`python eval/run_eval.py my_questions.jsonl`. It prints hit@k, MRR and, when possible, a score gap that tells you what
to set `MIN_SCORE_DENSE` (or `MIN_SCORE_RERANK` with a reranker) to. Add techniques only if they move the numbers:
translation (`QUERY_TRANSLATION`), paraphrases (`QUERY_EXPANSIONS`), reranker, `CHUNK_CHARS`, `CANDIDATES`.

## API
| Route | Purpose |
|---|---|
| `POST /v1/retrieve` `{tenant, query, top_k, hints{policy_type,kinds,language}, deadline_ms}` | → `{results:[{content,score,document,section,url,kind,policy_type,doc_id}], meta{strategy,queries,translated,reranked,degraded,latency_ms,confidence}}` |
| `POST /v1/documents` | ingest one doc (`format`: markdown / html / text) |
| `POST /v1/documents/batch` | many docs; `prune_prefix` deletes missing ones |
| `GET /v1/documents?tenant=` · `DELETE /v1/documents` | list · delete one |
| `POST /v1/scrape` · `POST /v1/crawl` · `GET /v1/crawl/{id}` | Firecrawl |
| `GET /v1/tenants/{t}/stats` · `DELETE /v1/tenants/{t}` | counts · wipe a store |

## Behaviour worth knowing
- **Every query is filtered by tenant** (store). Stores never see each other's data; tenant names are lower-cased.
- **Soft hints:** `policy_type` narrows the search; if that policy isn't indexed it searches everything instead.
- **Fails open:** translation, rerank and dense search each degrade (plain query / fused order / keyword-only) instead of
  failing; `meta.degraded` says what happened. `[]` means "nothing relevant", decided by the service's cutoffs.
- **Crawled text is untrusted.** Only the start host (plus `ALLOWED_CRAWL_HOSTS`) is indexed; HTML comments, invisible
  characters, images and link URLs are stripped; private/local addresses are refused. Keep your reply prompt treating
  reference material as data.
- **Changing embedding model or CHUNK_CHARS:** use a new `QDRANT_COLLECTION` and re-ingest (the service refuses a vector-size
  mismatch rather than mixing models).
- **Not included:** per-customer data (orders, profiles) must never be indexed here, and live price/stock answers belong to Shopify.

## Tests
`bash tests/run_tests.sh` (needs Qdrant on localhost:6333, e.g. `docker run -p 6333:6333 qdrant/qdrant`) runs 56 end-to-end
checks with fake embedding / LLM / reranker / Firecrawl / Shopify servers.
