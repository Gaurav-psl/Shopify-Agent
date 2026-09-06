"""
llm_selector.py
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
    providers = []
    raw = (os.environ.get("FALLBACK_LLM_APIKEY_ENDPOINT_MODEL") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                for i, item in enumerate(parsed):
                    if isinstance(item, dict) and all(k in item and item[k] for k in ("api_key", "base_url", "model")):
                        providers.append(item)
                    else:
                        print(f"[llm_selector] fallback provider #{i} is missing api_key/base_url/model — skipped")
        except Exception as e:
            print(f"[llm_selector] Error parsing FALLBACK_LLM_APIKEY_ENDPOINT_MODEL: {e}")

    # Auto-detect secondary custom provider
    sec_key = os.environ.get("SECONDARY_LLM_API_KEY")
    sec_base = os.environ.get("SECONDARY_LLM_BASE_URL")
    sec_model = os.environ.get("SECONDARY_LLM_MODEL", "gpt-4o-mini")
    if sec_key and sec_base:
        providers.append({"api_key": sec_key, "base_url": sec_base, "model": sec_model})

    # Auto-detect Groq if key is provided and not already in providers
    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key and not any("groq.com" in p.get("base_url", "") for p in providers):
        providers.append({
            "api_key": groq_key,
            "base_url": "https://api.groq.com/openai/v1",
            "model": os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
        })

    # Auto-detect Gemini OpenAI-compatible endpoint
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key and not any("generativelanguage" in p.get("base_url", "") for p in providers):
        providers.append({
            "api_key": gemini_key,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "model": os.environ.get("GEMINI_MODEL", "gemini-1.5-flash"),
        })

    # Auto-detect OpenAI if primary is something else (e.g. self-hosted/local/Groq)
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key and PRIMARY_BASE_URL and not any("api.openai.com" in p.get("base_url", "") for p in providers):
        providers.append({
            "api_key": openai_key,
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
        })

    if not providers:
        print("[llm_selector] No fallback providers configured (set FALLBACK_LLM_APIKEY_ENDPOINT_MODEL or GROQ_API_KEY)")
    else:
        print(f"[llm_selector] loaded {len(providers)} fallback provider(s)")
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
    each configured fallback in order, returning the first success.

    `extra_body` is treated as PRIMARY-ONLY and stripped before trying
    any fallback. It exists for provider-specific parameters (e.g. a
    self-hosted vLLM/SGLang server's `chat_template_kwargs`) that a
    different provider (Gemini, OpenAI, etc.) won't recognize and will
    outright reject the request over — forwarding it blindly to every
    fallback defeats the whole purpose of having a fallback.
    """
    last_error: Exception | None = None

    try:
        return _get_primary().chat.completions.create(model=PRIMARY_MODEL, messages=messages, **kwargs)
    except Exception as e:
        last_error = e
        print(f"[llm_selector] primary endpoint failed ({e!r}) — trying {len(_FALLBACK_PROVIDERS)} fallback(s)")

    fallback_kwargs = {k: v for k, v in kwargs.items() if k != "extra_body"}

    for i, provider in enumerate(_FALLBACK_PROVIDERS):
        try:
            client = _get_fallback_client(i)
            return client.chat.completions.create(model=provider["model"], messages=messages, **fallback_kwargs)
        except Exception as e:
            last_error = e
            print(f"[llm_selector] fallback provider #{i} ({provider.get('base_url')}) failed ({e!r})")
            continue

    print("[llm_selector] ALL providers (primary + every fallback) failed")
    raise last_error
