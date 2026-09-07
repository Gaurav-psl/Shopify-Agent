"""
Turns structured action results (order data, product lists, policy text,
etc.) into a natural-sounding reply in the user's own language.

Keeps the two concerns separate on purpose:
  - intent_classifier.py decides WHAT the user wants and detects their language
  - reply_generator.py decides HOW to phrase the answer, in that language

This means your Shopify data layer never needs to know or care about
language at all — it just returns structured data, and this module handles
the human-facing phrasing.

RAG integration: when rag_context is passed in (a list of {"content", "score",
"document"} dicts from rag_retriever.retrieve_context), it's injected into the
prompt as grounding material, and the model is instructed to answer only from
it — this keeps knowledge-base-backed answers (e.g. policy questions) factual
instead of relying on the model's own training data.
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


def _format_rag_context(rag_context: list[dict]) -> str:
    """Turns retrieved chunks into a numbered block for the prompt."""
    if not rag_context:
        return ""
    parts = []
    for i, chunk in enumerate(rag_context, start=1):
        content = chunk.get("content", "").strip()
        if content:
            parts.append(f"[{i}] {content}")
    return "\n\n".join(parts)


def generate_reply(
    action_name: str,
    data: dict,
    language: str,
    original_message: str,
    custom_instructions: str = "",
    rag_context: list[dict] | None = None,
) -> str:
    """
    action_name: e.g. "track_order", "search_products", "answer_policy_question"
    data: whatever your Shopify data layer returned (dict/list/etc.)
    language: ISO 639-1 code from the classifier, e.g. "hi"
    original_message: the user's original message, for tone/context
    custom_instructions: optional merchant-configured instructions from dashboard
    rag_context: optional list of retrieved chunks from rag_retriever.retrieve_context(),
                 e.g. [{"content": "...", "score": 0.87, "document": "policy.txt"}, ...]
    """
    language_name = LANGUAGE_NAMES.get(language, language)

    extra_guidance = ""
    if action_name in ("recommend_products", "search_products"):
        extra_guidance = " Briefly and warmly introduce the picks in 1-2 sentences. The products are displayed as interactive cards directly below your message, so you do not need to list every product detail or price manually."

    instruction_clause = ""
    if custom_instructions and custom_instructions.strip():
        instruction_clause = f" Adhere strictly to the store's custom tone and guidelines: {custom_instructions.strip()}."

    rag_context_text = _format_rag_context(rag_context) if rag_context else ""
    rag_clause = ""
    if rag_context_text:
        rag_clause = (
            " You have been given reference material retrieved from the store's knowledge base "
            "below. Base your answer ONLY on that material — do not use outside knowledge or make "
            "assumptions beyond what it states. If the material doesn't contain enough information "
            "to answer, say so honestly and suggest the user contact support instead of guessing."
        )

    system_prompt = (
        f"You are a friendly Shopify store assistant. Reply ONLY in {language_name} "
        f"({language}), regardless of what language this instruction is written in. "
        "Keep the reply short, warm, and easy to understand for a non-technical user. "
        "Use the structured data given to you as the source of truth — do not invent "
        f"details that aren't in it.{extra_guidance}{instruction_clause}{rag_clause} If the data indicates an error or empty result, "
        "say so gently and suggest what the user could try next."
    )

    user_prompt_parts = [
        f"User's original message: {original_message}",
        f"Action performed: {action_name}",
        f"Result data:\n{json.dumps(data, ensure_ascii=False, indent=2)}",
    ]
    if rag_context_text:
        user_prompt_parts.append(f"Retrieved reference material:\n{rag_context_text}")
    user_prompt_parts.append(f"Write the reply in {language_name}.")
    user_prompt = "\n\n".join(user_prompt_parts)

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
            if rag_context_text:
                # Fallback with RAG context but no working LLM call: return the
                # top chunk verbatim rather than a generic message, since it's
                # still more useful than nothing.
                top_chunk = rag_context[0].get("content", "").strip() if rag_context else ""
                if top_chunk:
                    return top_chunk
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

    # Example of a RAG-backed policy answer:
    # from rag_retriever import retrieve_context
    # chunks = retrieve_context("what is your return policy")
    # reply = generate_reply(
    #     "answer_policy_question", {}, "en",
    #     "What is your return policy?", rag_context=chunks
    # )
    # print(reply)
