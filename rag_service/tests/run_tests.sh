#!/usr/bin/env bash
# Offline end-to-end test. Needs a Qdrant server on localhost:6333 (docker run -p 6333:6333 qdrant/qdrant) or set QDRANT_URL.
set -u
cd "$(dirname "$0")/.."
export RAG_API_KEY=test-key QDRANT_URL=${QDRANT_URL:-http://127.0.0.1:6333} QDRANT_COLLECTION=e2e_$$
export EMBEDDING_BASE_URL=http://127.0.0.1:9100/v1 LLM_BASE_URL=http://127.0.0.1:9100/v1 LLM_MODEL=fake
export FIRECRAWL_BASE_URL=http://127.0.0.1:9100/fc/v2 CRAWL_POLL_S=0.3 CHUNK_CHARS=400 QUERY_TRANSLATION=non_english
python -m uvicorn tests.fakes:app --port 9100 --log-level warning & P1=$!
python -m uvicorn app.main:app --port 8080 --log-level warning & P2=$!
sleep 3
python tests/e2e.py; RC=$?
kill $P2 2>/dev/null; sleep 1
if [ $RC -eq 0 ]; then
  echo; echo "--- pass 2: reranker + paraphrases + backend client + sync + eval"
  export QDRANT_COLLECTION=e2e2_$$ RERANK_BASE_URL=http://127.0.0.1:9100/v1 RERANK_MODEL=fake MIN_SCORE_RERANK=0.2 QUERY_EXPANSIONS=1
  python -m uvicorn app.main:app --port 8080 --log-level warning & P3=$!
  sleep 3
  python tests/e2e_extras.py; RC=$?
  kill $P3 2>/dev/null
fi
kill $P1 2>/dev/null
exit $RC
