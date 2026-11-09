"""Tiny OpenAI-compatible chat client for the optional query steps. Never raises: returns None on any problem,
so every caller has a plain-retrieval fallback."""
import re

from . import config, http

_THINK = re.compile(r"<(?:think|thought)>.*?</(?:think|thought)>", re.S | re.I)


async def complete(system: str, user: str, max_tokens: int = 160, timeout: float | None = None) -> str | None:
    if not config.LLM_ENABLED:
        return None
    body = {
        "model": config.LLM_MODEL, "temperature": 0, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        **config.LLM_EXTRA_BODY,
    }
    headers = {"Authorization": f"Bearer {config.LLM_API_KEY}"} if config.LLM_API_KEY else {}
    try:
        r = await http.get().post(f"{config.LLM_BASE_URL}/chat/completions", json=body, headers=headers,
                                  timeout=timeout or config.LLM_TIMEOUT_S)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:  # noqa: BLE001
        print(f"llm: {e!r}")
        return None
    return _THINK.sub("", text).strip() or None
