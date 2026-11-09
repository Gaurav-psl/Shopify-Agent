"""Second test pass: reranker + expansions, backend client, rag_sync.py, run_eval.py. Run via tests/run_tests.sh."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "backend_client"))
BASE, KEY, T = "http://127.0.0.1:8080", os.environ["RAG_API_KEY"], "shop-c.myshopify.com"
H = {"Authorization": f"Bearer {KEY}"}
n = 0


def check(name, cond, extra=""):
    global n
    if not cond:
        print(f"FAIL  {name}  {extra}")
        sys.exit(1)
    n += 1
    print(f"ok    {name}")


def retrieve(q, **kw):
    return httpx.post(BASE + "/v1/retrieve", headers=H, json={"tenant": T, "query": q, **kw}, timeout=30).json()


# ---- sync tool (fake Shopify)
env = {**os.environ, "RAG_SERVICE_URL": BASE, "SHOPIFY_ADMIN_TOKEN": "t", "SHOPIFY_BASE_URL": "http://127.0.0.1:9100"}
run = lambda: subprocess.run([sys.executable, os.path.join(ROOT, "tools/rag_sync.py"), T], env=env, capture_output=True, text=True)  # noqa: E731
p = run()
check("rag_sync runs", p.returncode == 0, p.stdout + p.stderr)
check("sync indexed policies + product", "'indexed': 2" in p.stdout.split("product:")[0] and "product:: {'indexed': 1}" in p.stdout, p.stdout)
check("empty privacy policy skipped", "'documents': 3" in p.stdout, p.stdout)
p = run()
check("second sync embeds nothing (all unchanged)", "indexed" not in p.stdout.replace("indexed documents", "") and p.stdout.count("unchanged") == 2, p.stdout)
out = retrieve("how many days to return an item")
check("policy from shopify html retrievable, hidden text gone",
      out["results"][0]["doc_id"] == "policy:refund_policy" and "ignore previous" not in out["results"][0]["content"], out)
out = retrieve("roomy linen tote colors")
check("product retrievable with options", out["results"][0]["doc_id"] == "product:linen-tote" and "Sand" in out["results"][0]["content"], out)

# ---- reranker + expansions (service started with RERANK_* and QUERY_EXPANSIONS=1, MIN_SCORE_RERANK=0.2)
out = retrieve("how many days to return an item")
check("reranker used", out["meta"]["reranked"] and out["results"][0]["doc_id"] == "policy:refund_policy", out["meta"])
out = retrieve("what is the capital of France")
check("irrelevant question -> [] (cutoff)", out["results"] == [], out)
out = retrieve("रिफंड कितने दिन में मिलेगा", hints={"language": "hi"})
check("original + translation + 1 paraphrase = 3 query variants", len(out["meta"]["queries"]) == 3, out["meta"])

# ---- eval harness
qs = [{"tenant": T, "question": "how many days to return an item", "expect": ["refund_policy"]},
      {"tenant": T, "question": "do you ship fast", "expect": ["shipping_policy"]},
      {"tenant": T, "question": "capital of France", "expect": []}]
f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
f.write("\n".join(json.dumps(q) for q in qs))
f.close()
p = subprocess.run([sys.executable, os.path.join(ROOT, "eval/run_eval.py"), f.name], env=env, capture_output=True, text=True)
check("eval harness reports hit rate", p.returncode == 0 and "hit@3: 100%" in p.stdout and "correctly empty: 100%" in p.stdout, p.stdout + p.stderr)

# ---- backend client
os.environ.update(RAG_SERVICE_URL=BASE)
import rag_retriever as rr  # noqa: E402

docs = rr.retrieve_context(T, "how many days to return an item")
check("client returns reply_generator's shape", docs and {"content", "score", "document"} <= set(docs[0]), docs)
check("client auto-hint from widget prefix", rr._auto_hints("refund policy: where is it", None) == {"policy_type": "refund_policy"}
      and rr._auto_hints("hello: there", None) == {} and rr._auto_hints("x", {"language": "hi"}) == {"language": "hi"})
calls = []
orig = rr._client().post
rr._client().post = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
rr.retrieve_context(T, "how many days to return an item")
check("second identical call served from cache", not calls)
rr.retrieve_context(T, "how many days to return an item", use_cache=False)
check("use_cache=False bypasses cache", len(calls) == 1)
check("unknown store -> []", rr.retrieve_context("nobody.myshopify.com", "refund") == [])
check("async twin works", asyncio.run(rr.aretrieve_context(T, "do you ship fast"))[0]["document"])
check("threshold filters client-side", rr.retrieve_context(T, "do you ship fast", score_threshold=99, use_cache=False) == [])
rr.ingest_text(T, "faq:gifts", "# Gift cards\n\nGift cards never expire.")
check("admin ingest clears cache + is retrievable", rr.retrieve_context(T, "do gift cards expire")[0]["doc_id"] == "faq:gifts")
rr.delete_store(T)
check("delete_store", rr.store_stats(T)["chunks"] == 0)

rr.RAG_SERVICE_URL = "http://127.0.0.1:9"       # dead service
rr.clear_cache()
t0 = time.time()
for _ in range(3):
    rr.retrieve_context(T, "anything new %d" % _, use_cache=False)
t1 = time.time()
check("breaker opens after 3 failures", rr._breaker_open())
rr.retrieve_context(T, "another", use_cache=False)
check("open breaker returns [] instantly", time.time() - t1 < 0.2)
print(f"\nALL {n} EXTRA CHECKS PASSED")
