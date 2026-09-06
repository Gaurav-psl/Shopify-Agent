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
import re
import llm_selector
from openai import OpenAI


_client = None

# Qwen3-family models can emit an internal reasoning block wrapped in
# <think>...</think> before the actual answer when "thinking mode" is on.
# Strip it out so only the final, user-facing reply ever reaches the widget.
#  primary model : Qwen/Qwen3-8B-AWQ 
_THINK_RE = re.compile(r"<(?:think|thought)>.*?</(?:think|thought)>", re.DOTALL | re.IGNORECASE)    # regular expression to handle text in think tag as well as thought tag

def _strip_thinking(text: str) -> str:
    if not text:
        return ""
    return _THINK_RE.sub("", text).strip()


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


def generate_reply(action_name: str, data: dict, language: str, original_message: str, custom_instructions: str = "") -> str:
    """
    action_name: e.g. "track_order", "search_products"
    data: whatever your Shopify data layer returned (dict/list/etc.)
    language: ISO 639-1 code from the classifier, e.g. "hi"
    original_message: the user's original message, for tone/context
    custom_instructions: optional merchant-configured instructions from dashboard
    """
    language_name = LANGUAGE_NAMES.get(language, language)

    extra_guidance = ""
    if action_name in ("recommend_products", "search_products"):
        extra_guidance = " Briefly and warmly introduce the picks in 1-2 sentences. The products are displayed as interactive cards directly below your message, so you do not need to list every product detail or price manually."

    instruction_clause = ""
    if custom_instructions and custom_instructions.strip():
        instruction_clause = f" Adhere strictly to the store's custom tone and guidelines: {custom_instructions.strip()}."

    system_prompt = (
        f"You are a friendly Shopify store assistant. Reply ONLY in {language_name} "
        f"({language}), regardless of what language this instruction is written in. "
        "Keep the reply short, warm, and easy to understand for a non-technical user. "
        "Use the structured data given to you as the source of truth — do not invent "
        f"details that aren't in it.{extra_guidance}{instruction_clause} If the data indicates an error or empty result, "
        "say so gently and suggest what the user could try next."
    )

    user_prompt = (
        f"User's original message: {original_message}\n"
        f"Action performed: {action_name}\n"
        f"Result data:\n{json.dumps(data, ensure_ascii=False, indent=2)}\n\n"
        f"Write the reply in {language_name}."
    )

    try:
        response = llm_selector.create_chat_completion(
            temperature=0.4,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        return _strip_thinking(response.choices[0].message.content)
    except Exception as e:
        print(f"reply_generator: LLM generation error ({e!r}), using fallback template")
        if action_name in ("recommend_products",):
            recs = data.get("recommendations") or data.get("results") or []
            if recs:
                return "Here are some of our top picks and bestsellers you might love! 🛍️"
            return "Sorry, I couldn't find any recommendations right now. Try searching for a specific item!"
        if action_name in ("search_products",):
            results = data.get("results") or []
            if results:
                return f"I found {len(results)} item{'s' if len(results) != 1 else ''} for you:"
            return "Sorry, I couldn't find any products matching that description."
        if action_name in ("track_order",):
            if data.get("error") == "not_found":
                return f"Sorry, I couldn't find order #{data.get('order_number', '')}. Please check the order number and try again."
            if data.get("error"):
                return "I couldn't look up that order right now. Please check your order confirmation email or contact store support."
            status = data.get("fulfillment_status") or data.get("status") or "processing"
            tracking = data.get("tracking_url") or data.get("tracking_number")
            num = data.get("order_number") or ""
            num_str = f" #{num}" if num else ""
            if tracking:
                return f"Your order{num_str} is {status}. Tracking: {tracking}"
            return f"Your order{num_str} is currently {status}."
        if action_name in ("add_item",):
            if data.get("error"):
                return "Sorry, I couldn't find that item in stock to add to your cart."
            item_name = data.get("added") or "item"
            qty = data.get("quantity", 1)
            return f"Added {qty}x {item_name} to your cart!"
        if action_name in ("view_cart",):
            return "Here is what's currently in your cart."
        if action_name in ("clear_cart",):
            return "Your cart has been cleared."
        if action_name in ("answer_policy_question",):
            body = data.get("body")
            if body:
                return body
            return "For returns or exchanges, items can be returned within 7 days of delivery in original condition. Please reach out to support@dripire.com with your order number."
        return "Here are the details from our store."


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
