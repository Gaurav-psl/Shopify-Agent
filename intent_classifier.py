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
import llm_selector
from pathlib import Path
from openai import OpenAI

SCHEMA_PATH = Path(__file__).parent / "intent_schema.json"
# MODEL = os.environ.get("OPENAI_MODEL", "Qwen/Qwen3-8B-AWQ")

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


_RECOMMEND_FASTPATH = re.compile(
    r"^(what('?s| is| are) (your )?(best[- ]?sellers?|trending|popular)( (right )?now| today)?|"
    r"show (me )?(your )?(best[- ]?sellers?|trending|popular|recommendations?)|"
    r"what (do|would|can) you recommend|"
    r"(any |some )?recommendations?|"
    r"(please )?recommend (me )?(some |a few )?(items?|products?|something)?|"
    r"give me (some )?(product )?recommendations?|"
    r"suggest (me )?(some |a few )?(items?|products?|something)|"
    r"top (picks?|recommendations?|trending)|"
    r"bestsellers?|"
    r"trending( (items?|products?|now))?|"
    r"gift (ideas?|recommendations?))$",
    re.IGNORECASE,
)


def _match_recommend_fastpath(clean_msg: str) -> dict | None:
    if not clean_msg:
        return None
    text = clean_msg.lower()
    # 1. Exact regex match
    if _RECOMMEND_FASTPATH.match(clean_msg):
        rec_type = "bestseller" if "best" in text else ("trending" if "trend" in text else "general")
        return {
            "intent": "recommendations",
            "action": "recommend_products",
            "entities": {"recommendation_type": rec_type},
            "confidence": 0.98,
            "requires_confirmation": False,
            "language": "en",
        }

    # 2. Typo & substring tolerant matching (handles "top commendations", "recomended", "any recs", etc.)
    rec_stems = [
        "recommend", "recommed", "recomend", "commendation", "commedation",
        "bestseller", "best seller", "best-seller", "trending", "top pick",
        "top choice", "popular item"
    ]
    if any(s in text for s in rec_stems):
        # Disqualify if it's clearly a customer service query about orders, carts, or accounts
        non_rec_stems = ["order", "cart", "track", "cancel", "return", "warranty", "refund", "login", "password", "sign in"]
        if not any(nr in text for nr in non_rec_stems):
            rec_type = "bestseller" if "best" in text else ("trending" if "trend" in text else "general")
            return {
                "intent": "recommendations",
                "action": "recommend_products",
                "entities": {"recommendation_type": rec_type},
                "confidence": 0.95,
                "requires_confirmation": False,
                "language": "en",
            }

    return None


def classify_intent(user_message: str, schema: dict | None = None) -> dict:
    """Classify a single user message. Returns a dict matching
    classification_output_format from the schema, with requires_confirmation
    filled in from the schema (not trusted from the model's own output)."""
    schema = schema or load_schema()

    clean_msg = (user_message or "").strip().strip("?!.")
    fastpath_match = _match_recommend_fastpath(clean_msg)
    if fastpath_match:
        return fastpath_match

    system_prompt = build_system_prompt(schema)
    
    # Before adding llm_selector and its create.chat.completions()
    # response = _get_client().chat.completions.create(
    #     model=MODEL,
    #     response_format={"type": "json_object"},
    #     messages=[
    #         {"role": "system", "content": system_prompt},
    #         {"role": "user", "content": user_message},
    #     ],
    #     temperature=0,
    #     extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    # )
    response = llm_selector.create_chat_completion(
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    raw = _strip_thinking(response.choices[0].message.content)
    # result = json.loads(raw)

    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        print(f"intent_classifier: model returned unparseable content ({e!r}); raw={raw!r}")
        return {
            "intent": "fallback",
            "action": "clarify",
            "entities": {},
            "confidence": 0.0,
            "requires_confirmation": False,
            "language": "en",
        }
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
