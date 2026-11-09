#!/usr/bin/env python3
"""Indexes a Shopify store's policies and product descriptions into the RAG service. Safe to re-run: unchanged
documents are skipped (no embedding cost) and products/policies removed from Shopify are removed from the index.

  python tools/rag_sync.py my-store.myshopify.com [--no-products] [--no-policies]

Env:  RAG_SERVICE_URL, RAG_API_KEY            the RAG service
      SHOPIFY_ADMIN_TOKEN                     Admin API token with read_products + read_legal_policies
      SHOPIFY_API_VERSION                     default 2026-10
Prices and stock are deliberately NOT indexed: they change too often. Your backend answers those from Shopify live.
"""
import argparse
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.htmlmd import html_to_markdown  # noqa: E402

RAG_URL = os.environ.get("RAG_SERVICE_URL", "").rstrip("/")
RAG_KEY = os.environ.get("RAG_API_KEY", "")
TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-10")

POLICIES_Q = "{ shop { shopPolicies { title body type url } } }"
PRODUCTS_Q = """query($after: String) { products(first: 100, after: $after, query: "status:active") {
  pageInfo { hasNextPage endCursor }
  nodes { handle title descriptionHtml productType vendor tags onlineStoreUrl options { name values } } } }"""


def shopify(shop: str, query: str, variables: dict | None = None) -> dict:
    base = os.environ.get("SHOPIFY_BASE_URL", f"https://{shop}")
    r = httpx.post(f"{base}/admin/api/{VERSION}/graphql.json", json={"query": query, "variables": variables or {}},
                   headers={"X-Shopify-Access-Token": TOKEN}, timeout=60)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"Shopify GraphQL errors: {body['errors']}")
    return body["data"]


def rag(method: str, path: str, **kw) -> dict:
    r = httpx.request(method, f"{RAG_URL}{path}", headers={"Authorization": f"Bearer {RAG_KEY}"}, timeout=300, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"RAG service {r.status_code}: {r.text[:300]}")
    return r.json()


def policy_docs(shop: str) -> list[dict]:
    docs = []
    for p in shopify(shop, POLICIES_Q)["shop"]["shopPolicies"]:
        ptype = (p.get("type") or "").lower()
        text = html_to_markdown(p.get("body") or "")
        if text:
            docs.append({"doc_id": f"policy:{ptype}", "title": p.get("title") or ptype.replace("_", " ").title(),
                         "text": f"# {p.get('title') or ptype}\n\n{text}", "kind": "policy", "policy_type": ptype,
                         "url": p.get("url") or ""})
    return docs


def product_docs(shop: str) -> list[dict]:
    docs, after = [], None
    while True:
        page = shopify(shop, PRODUCTS_Q, {"after": after})["products"]
        for p in page["nodes"]:
            lines = [f"# {p['title']}"]
            facts = [f"Type: {p['productType']}" if p.get("productType") else "",
                     f"Brand: {p['vendor']}" if p.get("vendor") else "",
                     f"Tags: {', '.join(p['tags'])}" if p.get("tags") else ""]
            facts += [f"{o['name']} options: {', '.join(o['values'])}" for o in p.get("options") or []
                      if o["name"] != "Title"]
            lines += [f for f in facts if f]
            body = html_to_markdown(p.get("descriptionHtml") or "")
            if body:
                lines += ["", body]
            docs.append({"doc_id": f"product:{p['handle']}", "title": p["title"], "text": "\n".join(lines),
                         "kind": "product", "url": p.get("onlineStoreUrl") or f"https://{shop}/products/{p['handle']}"})
        if not page["pageInfo"]["hasNextPage"]:
            return docs
        after = page["pageInfo"]["endCursor"]


def push(shop: str, docs: list[dict], prefix: str) -> None:
    summary: dict = {}
    errors = []
    for i in range(0, len(docs), 100):
        out = rag("POST", "/v1/documents/batch", json={"tenant": shop, "documents": docs[i:i + 100]})
        for k, v in out["summary"].items():
            summary[k] = summary.get(k, 0) + v
        errors += [r for r in out["results"] if r["status"] == "error"]
    print(f"{prefix}: {summary}")
    for e in errors[:5]:
        print("   error:", e)
    if errors:
        print(f"   {prefix}: {len(errors)} failed, so nothing was pruned")
        return
    keep = {d["doc_id"] for d in docs}      # remove what no longer exists in Shopify
    for d in rag("GET", "/v1/documents", params={"tenant": shop})["documents"]:
        if d["doc_id"].startswith(prefix) and d["doc_id"] not in keep:
            rag("DELETE", "/v1/documents", json={"tenant": shop, "doc_id": d["doc_id"]})
            print(f"   pruned {d['doc_id']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("shop")
    ap.add_argument("--no-products", action="store_true")
    ap.add_argument("--no-policies", action="store_true")
    a = ap.parse_args()
    if not (RAG_URL and RAG_KEY and TOKEN):
        sys.exit("Set RAG_SERVICE_URL, RAG_API_KEY and SHOPIFY_ADMIN_TOKEN")
    shop = a.shop.strip().lower()
    if not a.no_policies:
        push(shop, policy_docs(shop), "policy:")
    if not a.no_products:
        push(shop, product_docs(shop), "product:")
    print("stats:", rag("GET", f"/v1/tenants/{shop}/stats"))


if __name__ == "__main__":
    main()
