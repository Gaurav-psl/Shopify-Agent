"""
Intent classifier for the Shopify AI agent.

Reads intent_schema.json (the single source of truth for supported intents,
actions, and entities) and uses it to build a system prompt for the OpenAI
API. Every user message gets classified into one of the schema's intents
before the backend decides which Shopify action to run.

Requires:
    pip install openai
    export OPENAI_API_KEY=sk-...
"""

import os
import json
import re
from pathlib import Path
from openai import OpenAI

SCHEMA_PATH = Path(__file__).parent / "intent_schema.json"
MODEL = os.environ.get("OPENAI_MODEL", "Qwen/Qwen3-8B-AWQ")

_client = None

# Qwen3-family models can emit an internal reasoning block wrapped in
# <think>...</think> before the actual answer when "thinking mode" is on.
# Strip it before parsing, so a stray reasoning block never breaks the
# json.loads() call below.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_thinking(text: str) -> str:
    if not text:
        return ""
    return _THINK_RE.sub("", text).strip()


def _get_client():
    """Create the OpenAI client on first use, not at import time. This means
    a missing/bad OPENAI_API_KEY only breaks /chat when it's actually
    called — it can never take down OAuth or any other route on startup."""
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"),
                        base_url=os.environ.get("OPENAI_BASE_URL") )
    return _client


def load_schema() -> dict:
    with open(SCHEMA_PATH, "r") as f:
        return json.load(f)


def build_system_prompt(schema: dict) -> str:
    """Turn the schema into a system prompt the model can follow, including
    every intent, its actions, entities, and example phrasing."""
    lines = [
        "You are an intent classifier for a Shopify shopping assistant.",
        "Classify the user's message into exactly one intent and one action from the list below.",
        "Extract any relevant entities you can find in the message.",
        "If the message doesn't clearly match anything, or you're not confident, use the 'fallback' intent.",
        "",
        "Supported intents:",
    ]

    for intent in schema["intents"]:
        actions = ", ".join(a["name"] for a in intent["actions"])
        entities = ", ".join(intent.get("required_entities", []) + intent.get("optional_entities", []))
        examples = " | ".join(intent.get("example_utterances", [])[:3])
        lines.append(f"- {intent['name']}: {intent['description']}")
        lines.append(f"  actions: {actions}")
        if entities:
            lines.append(f"  entities to extract if present: {entities}")
        if examples:
            lines.append(f"  example phrasings: {examples}")

    lines.append("")
    lines.append("Always detect the language the user wrote or spoke in, and return its ISO 639-1 code as 'language' — even if it's not English. Do not translate the user's message; only report what language it's in.")
    lines.append("")
    lines.append("Respond ONLY with a JSON object in this exact shape, no other text:")
    lines.append(json.dumps(schema["classification_output_format"], indent=2))

    return "\n".join(lines)


def _find_action(schema: dict, intent_name: str, action_name: str) -> dict | None:
    for intent in schema["intents"]:
        if intent["name"] == intent_name:
            for action in intent["actions"]:
                if action["name"] == action_name:
                    return action
    return None


def classify_intent(user_message: str, schema: dict | None = None) -> dict:
    """Classify a single user message. Returns a dict matching
    classification_output_format from the schema, with requires_confirmation
    filled in from the schema (not trusted from the model's own output)."""
    schema = schema or load_schema()
    system_prompt = build_system_prompt(schema)

    response = _get_client().chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )

    raw = _strip_thinking(response.choices[0].message.content)
    result = json.loads(raw)

    # Don't trust the model's own confidence/requires_confirmation blindly —
    # cross-check against the schema and fall back safely if anything looks off.
    intent_name = result.get("intent", "fallback")
    action_name = result.get("action", "clarify")
    confidence = float(result.get("confidence", 0))
    language = result.get("language", "en")

    action_def = _find_action(schema, intent_name, action_name)

    if confidence < schema.get("confidence_threshold", 0.6) or action_def is None:
        return {
            "intent": "fallback",
            "action": "clarify",
            "entities": {},
            "confidence": confidence,
            "requires_confirmation": False,
            "language": language,
        }

    return {
        "intent": intent_name,
        "action": action_name,
        "entities": result.get("entities", {}),
        "confidence": confidence,
        "requires_confirmation": action_def["requires_confirmation"],
        "language": language,
    }


if __name__ == "__main__":
    # Quick manual test — run: python intent_classifier.py
    test_messages = [
        "Where is my order #1042?",
        "Add the red hoodie to my cart",
        "What's your refund policy?",
        "asdkfj",
    ]
    for msg in test_messages:
        print(f"\n> {msg}")
        print(json.dumps(classify_intent(msg), indent=2))
