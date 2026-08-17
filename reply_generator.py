"""
Turns structured action results (order data, product lists, policy text,
etc.) into a natural-sounding reply in the user's own language.

Keeps the two concerns separate on purpose:
  - intent_classifier.py decides WHAT the user wants and detects their language
  - reply_generator.py decides HOW to phrase the answer, in that language

This means your Shopify data layer never needs to know or care about
language at all — it just returns structured data, and this module handles
the human-facing phrasing.
"""

import os
import json
from openai import OpenAI

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

_client = None


def _get_client():
    """Lazy init — same reasoning as intent_classifier.py: a missing key
    should only break reply generation, never app startup."""
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    return _client

LANGUAGE_NAMES = {
    "en": "English",
    "hi": "Hindi",
    "pa": "Punjabi",
    "ta": "Tamil",
    "mr": "Marathi",
    "bn": "Bengali",
    "te": "Telugu",
    "gu": "Gujarati",
    "kn": "Kannada",
    "es": "Spanish",
    "fr": "French",
    "ar": "Arabic",
    "de": "German",
    "zh": "Chinese",
    "ja": "Japanese",
    "pt": "Portuguese",
}


def generate_reply(action_name: str, data: dict, language: str, original_message: str) -> str:
    """
    action_name: e.g. "track_order", "search_products"
    data: whatever your Shopify data layer returned (dict/list/etc.)
    language: ISO 639-1 code from the classifier, e.g. "hi"
    original_message: the user's original message, for tone/context
    """
    language_name = LANGUAGE_NAMES.get(language, language)

    system_prompt = (
        f"You are a friendly Shopify store assistant. Reply ONLY in {language_name} "
        f"({language}), regardless of what language this instruction is written in. "
        "Keep the reply short, warm, and easy to understand for a non-technical user. "
        "Use the structured data given to you as the source of truth — do not invent "
        "details that aren't in it. If the data indicates an error or empty result, "
        "say so gently and suggest what the user could try next."
    )

    user_prompt = (
        f"User's original message: {original_message}\n"
        f"Action performed: {action_name}\n"
        f"Result data:\n{json.dumps(data, ensure_ascii=False, indent=2)}\n\n"
        f"Write the reply in {language_name}."
    )

    response = _get_client().chat.completions.create(
        model=MODEL,
        temperature=0.4,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    return response.choices[0].message.content.strip()


if __name__ == "__main__":
    # Quick manual test — run: python reply_generator.py
    sample_data = {
        "order_id": "1042",
        "status": "shipped",
        "carrier": "FedEx",
        "estimated_delivery": "Thursday",
    }
    for lang in ["en", "hi", "es"]:
        reply = generate_reply("track_order", sample_data, lang, "Where is my order?")
        print(f"\n[{lang}] {reply}")
