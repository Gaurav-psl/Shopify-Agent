"""
llm_client.py
-------------
Single shared entry point for every chat-completion call in the project.
Both intent_classifier.py and reply_generator.py call
create_chat_completion() instead of building their own OpenAI client.

Behavior: try the primary endpoint. If that call fails for ANY reason,
log it and try each fallback provider in order (as listed in
FALLBACK_LLM_APIKEY_ENDPOINT_MODEL) until one succeeds. If every
provider fails, the LAST provider's exception is what propagates to the
caller.

Required environment variables:
    PRIMARY_LLM_API_KEY     - required
    PRIMARY_LLM_BASE_URL    - your primary custom endpoint's base URL
    PRIMARY_LLM_MODEL       - model name as that provider expects it

    FALLBACK_LLM_APIKEY_ENDPOINT_MODEL
        A JSON-encoded list of fallback providers, tried in the order
        given, e.g.:
        [
          {"api_key": "abc", "base_url": "https://.../v1", "model": "gemma-4-31b-it"},
          {"api_key": "xyz", "base_url": "https://.../v1", "model": "llama-3"}
        ]
        Each item needs exactly these three keys. Malformed entries or
        an unparseable/missing env var are logged and skipped rather
        than crashing at import time — worst case, you just have zero
        fallbacks instead of a broken deploy.

All endpoints must be OpenAI-compatible (support the /chat/completions
shape) — true for anything you can point the `openai` Python SDK's
`base_url` at.
"""

import json
import os

from openai import OpenAI

PRIMARY_API_KEY = os.environ.get("PRIMARY_LLM_API_KEY", "")
PRIMARY_BASE_URL = os.environ.get("PRIMARY_LLM_BASE_URL") or None
PRIMARY_MODEL = os.environ.get("PRIMARY_LLM_MODEL", "gpt-4o-mini")


def _load_fallback_providers() -> list[dict]:
    raw = (os.environ.get("FALLBACK_LLM_APIKEY_ENDPOINT_MODEL") or "").strip()
    if not raw:
        print("[llm_client] FALLBACK_LLM_APIKEY_ENDPOINT_MODEL not set — no fallback providers configured")
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[llm_client] FALLBACK_LLM_APIKEY_ENDPOINT_MODEL is not valid JSON ({e}) — ignoring, no fallbacks")
        return []

    if not isinstance(parsed, list):
        print("[llm_client] FALLBACK_LLM_APIKEY_ENDPOINT_MODEL must be a JSON list — ignoring, no fallbacks")
        return []

    providers = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict) or not all(k in item and item[k] for k in ("api_key", "base_url", "model")):
            print(f"[llm_client] fallback provider #{i} is missing api_key/base_url/model — skipped")
            continue
        providers.append(item)

    print(f"[llm_client] loaded {len(providers)} fallback provider(s)")
    return providers


_FALLBACK_PROVIDERS = _load_fallback_providers()

_primary_client = None
_fallback_clients: dict[int, OpenAI] = {}  # built lazily, one per provider index


def _get_primary() -> OpenAI:
    global _primary_client
    if _primary_client is None:
        _primary_client = OpenAI(
            api_key=PRIMARY_API_KEY,
            base_url=PRIMARY_BASE_URL,
            max_retries=2,
            timeout=20.0,
        )
    return _primary_client


def _get_fallback_client(index: int) -> OpenAI:
    if index not in _fallback_clients:
        provider = _FALLBACK_PROVIDERS[index]
        _fallback_clients[index] = OpenAI(
            api_key=provider["api_key"],
            base_url=provider["base_url"],
            max_retries=2,
            timeout=20.0,
        )
    return _fallback_clients[index]


def create_chat_completion(*, messages, **kwargs):
    """Drop-in replacement for `client.chat.completions.create(...)`.
    Pass everything you'd normally pass (response_format, temperature,
    etc.) as keyword arguments. Tries the primary provider first, then
    each configured fallback in order, returning the first success."""
    last_error: Exception | None = None

    try:
        return _get_primary().chat.completions.create(model=PRIMARY_MODEL, messages=messages, **kwargs)
    except Exception as e:
        last_error = e
        print(f"[llm_client] primary endpoint failed ({e!r}) — trying {len(_FALLBACK_PROVIDERS)} fallback(s)")

    for i, provider in enumerate(_FALLBACK_PROVIDERS):
        try:
            client = _get_fallback_client(i)
            return client.chat.completions.create(model=provider["model"], messages=messages, **kwargs)
        except Exception as e:
            last_error = e
            print(f"[llm_client] fallback provider #{i} ({provider.get('base_url')}) failed ({e!r})")
            continue

    print("[llm_client] ALL providers (primary + every fallback) failed")
    raise last_error
