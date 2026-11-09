#!/usr/bin/env python3
"""Measures retrieval quality on real shopper questions, so tuning (thresholds, rerank, translation, chunk size)
is based on numbers instead of guesses.

  python eval/run_eval.py eval/questions.jsonl [--top-k 3]

questions.jsonl, one JSON object per line:
  {"tenant": "shop.myshopify.com", "question": "how long do refunds take?", "expect": ["policy:refund_policy"]}
  {"tenant": "...", "question": "do you sell cars?", "expect": []}          <- must return nothing
  optional: "hints": {"policy_type": "refund_policy", "language": "hi"}
`expect` entries match if they are a substring of a returned doc_id.
Env: RAG_SERVICE_URL, RAG_API_KEY
"""
import argparse
import json
import os
import sys

import httpx

URL = os.environ.get("RAG_SERVICE_URL", "").rstrip("/")
KEY = os.environ.get("RAG_API_KEY", "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--top-k", type=int, default=3)
    a = ap.parse_args()
    if not (URL and KEY):
        sys.exit("Set RAG_SERVICE_URL and RAG_API_KEY")
    rows = [json.loads(line) for line in open(a.file, encoding="utf-8") if line.strip()]
    hits = rr = answerable = neg = neg_empty = 0
    hit_scores, neg_scores, misses = [], [], []
    for q in rows:
        r = httpx.post(f"{URL}/v1/retrieve", headers={"Authorization": f"Bearer {KEY}"}, timeout=30,
                       json={"tenant": q["tenant"], "query": q["question"], "top_k": a.top_k, "hints": q.get("hints")})
        r.raise_for_status()
        res = r.json()["results"]
        expect = q.get("expect", [])
        if not expect:
            neg += 1
            neg_empty += not res
            neg_scores += [x["score"] for x in res[:1]]
            continue
        answerable += 1
        rank = next((i for i, x in enumerate(res, 1) if any(e in x["doc_id"] for e in expect)), None)
        if rank:
            hits += 1
            rr += 1 / rank
            hit_scores.append(res[rank - 1]["score"])
        else:
            misses.append((q["question"], [x["doc_id"] for x in res]))
    print(f"answerable questions: {answerable}   hit@{a.top_k}: {hits / max(answerable, 1):.0%}   MRR: {rr / max(answerable, 1):.2f}")
    if neg:
        print(f"unanswerable questions: {neg}   correctly empty: {neg_empty / neg:.0%}")
    if hit_scores:
        print(f"score of correct hits:  min {min(hit_scores):.3f}  median {sorted(hit_scores)[len(hit_scores) // 2]:.3f}")
    if neg_scores:
        print(f"top score on unanswerable questions: max {max(neg_scores):.3f}")
        if hit_scores and max(neg_scores) < min(hit_scores):
            print(f"-> a cutoff between {max(neg_scores):.3f} and {min(hit_scores):.3f} separates them "
                  f"(set MIN_SCORE_DENSE, or MIN_SCORE_RERANK if a reranker is on)")
    for question, got in misses:
        print(f"MISS: {question!r} got {got}")


if __name__ == "__main__":
    main()
