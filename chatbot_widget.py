"""
chatbot_widget.py
------------------
Multi-tenant: one deployment serves every installed store. Each store
embeds the same script tag (shown to them on their dashboard, see
dashboard_nicegui.py):

    <script src="https://your-app-domain.com/widget.js"
            data-shop="{shop}.myshopify.com" defer></script>

Routes:
  GET  /widget.js       -> the embeddable widget (same file for every
                            store; it fetches its own store's config at
                            runtime from /widget-config)
  GET  /widget-config    -> per-store customization (name, title, icon)
                            as JSON, keyed by ?shop=xxx.myshopify.com
  POST /chat             -> classify the message (intent_classifier.py),
                            execute it (shopify_actions.py) or ask for
                            confirmation first, then phrase the reply in
                            the shopper's own language (reply_generator.py)
  POST /confirm          -> confirm/cancel a pending action (the widget's
                            Yes/No buttons call this; typing "yes"/"no"
                            back into /chat works too)

Cart mutations are NOT done server-side — see the big comment in
shopify_actions.py. Instead the backend returns a `widget_action` and
widget.js performs the real fetch() against the store's own
`/cart/add.js` etc, in the shopper's browser, same-origin with the store.

Response shape consumed by the widget (all keys optional except `reply`):
  reply           -> text bubble
  status          -> "done" | "confirmation_required" | "cancelled" | "expired"
  products        -> [{id, name, price, image, url?}] rendered as cards; `id`
                     is the Shopify VARIANT id (what /cart/add.js expects)
  orders          -> [{id, date?, status?}] rendered as a tappable order picker
  widget_action   -> {type: redirect|cart_add|cart_remove|cart_set_quantity|
                            cart_view|cart_clear|checkout, ...}

Conversation state kept in PENDING (in-memory, 5 min TTL, per shop+session):
  confirm            -> waiting for yes/no on a sensitive action
  await_search_item  -> "which item would you like to search for?"
  await_item         -> "which item would you like to add to your cart?"
  await_remove_item  -> "which item would you like to remove?"
  await_order_number -> "what's your order number?"
"""

import re
import time
from types import SimpleNamespace
from fastapi import APIRouter, Response
from pydantic import BaseModel
from langfuse import observe, get_client

import repository_appwrite as repo
from intent_classifier import classify_intent, load_schema
from reply_generator import generate_reply
import shopify_actions

router = APIRouter(tags=["chatbot-widget"])

SCHEMA = load_schema()

# ---------------------------------------------------------------------
# Pending-state cache. Keyed by shop + session_id (the widget's random
# per-tab id). Deliberately in-memory + short-lived: it only bridges the
# single "are you sure?" / "which item?" round trip, so it doesn't need
# a database row and is wiped on every deploy/restart, which is fine for
# that purpose.
# ---------------------------------------------------------------------
PENDING: dict[str, dict] = {}
PENDING_TTL_SECONDS = 300

_AFFIRMATIVE = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "confirm", "confirmed",
                "si", "sí", "oui", "haan", "ha", "theek hai"}
_NEGATIVE = {"no", "n", "nope", "cancel", "nah", "non", "nahi"}

# Which dashboard "Features" toggle (repository_appwrite._DEFAULT_FEATURES)
# gates which classifier action. Anything not listed here is always allowed.
_FEATURE_FOR_ACTION = {
    ("product_search", "search_products"): "product_search",
    ("cart_management", "add_item"): "cart_editing",
    ("cart_management", "remove_item"): "cart_editing",
    ("cart_management", "edit_quantity"): "cart_editing",
    ("cart_management", "clear_cart"): "cart_editing",
    ("warranty_claim", "submit_claim"): "warranty",
    ("warranty_claim", "check_claim_status"): "warranty",
    ("order_tracking", "track_order"): "track_orders",
    ("order_tracking", "list_recent_orders"): "track_orders",
}
_FILTER_ENTITIES = ("price_min", "price_max", "color", "size")


def _pkey(shop: str, session_id: str) -> str:
    return f"{shop}::{session_id}"


def _set_pending(key: str, **fields) -> None:
    PENDING[key] = {**fields, "expires": time.time() + PENDING_TTL_SECONDS}


def _prune_pending():
    now = time.time()
    for k in [k for k, v in PENDING.items() if v["expires"] < now]:
        PENDING.pop(k, None)


def _split_widget_action(data: dict) -> tuple[dict, dict | None]:
    """widget_action is an instruction for the browser (redirect, cart
    mutation, ...) — pull it out before handing `data` to the LLM reply
    generator, which should only ever see user-facing facts."""
    if not isinstance(data, dict):
        return data, None
    action = data.get("widget_action")
    if action is None:
        return data, None
    clean = {k: v for k, v in data.items() if k != "widget_action"}
    return clean, action


class ChatRequest(BaseModel):
    message: str
    session_id: str = "anonymous"
    shop: str


class ConfirmRequest(BaseModel):
    shop: str
    session_id: str = "anonymous"
    confirmed: bool


def _get_store(shop: str) -> SimpleNamespace | None:
    """shopify_actions.py expects attribute access (store.access_token),
    and Appwrite documents are plain dicts (store["access_token"]), so
    this wrapper bridges the two without touching shopify_actions.py."""
    doc = repo.get_store(shop)
    if not doc:
        return None
    return SimpleNamespace(shop_domain=doc["shop_domain"], access_token=doc["access_token"], id=doc["$id"])


def _log(shop: str, message: str, status: str, intent=None, action=None, entities=None, reply: str = "") -> None:
    """Best-effort request logging for the dashboard's Insights section.
    Must never break the actual chat response, so failures here are
    swallowed (and printed for server logs) rather than raised.

    The intent/action names logged here MUST match intent_schema.json
    ("cart_management"/"add_item", "product_search"/"search_products", ...)
    because repository_appwrite's analytics group on those exact strings."""
    try:
        repo.log_request(shop, message, status, detected_intent=intent, detected_action=action, reply=reply, entities=entities)
    except Exception as e:  # noqa: BLE001
        print(f"chatbot_widget: log_request failed: {e!r}")


def _safe_reply(action: str, data: dict, language: str, message: str, fallback: str, **kwargs) -> str:
    """generate_reply() that can never take the chat down: if the LLM call
    fails, the shopper still gets a sensible plain-English line (the
    widget_action / product cards that accompany it still work)."""
    try:
        return generate_reply(action, data, language, message, **kwargs) or fallback
    except Exception as e:  # noqa: BLE001
        print(f"chatbot_widget: generate_reply failed for {action}: {e!r}")
        return fallback


def _feature_enabled(store_id: str, intent: str, action: str, entities: dict) -> bool:
    """Honors the per-store toggles on the dashboard's Features page.
    Fails open (returns True) if the lookup itself errors, so an Appwrite
    hiccup never silently disables the whole assistant."""
    keys = []
    base = _FEATURE_FOR_ACTION.get((intent, action))
    if base:
        keys.append(base)
    if (intent, action) == ("product_search", "search_products"):
        if any(entities.get(k) not in (None, "", []) for k in _FILTER_ENTITIES):
            keys.append("product_filtering")
    if not keys:
        return True
    try:
        features = repo.ensure_features(store_id)
    except Exception as e:  # noqa: BLE001
        print(f"chatbot_widget: feature lookup failed: {e!r}")
        return True
    return all(bool(features.get(k, True)) for k in keys)


# =======================================================================
# Catalog helpers — real product lookup against the store's own catalog.
#
# shopify_actions filters Admin API results with `title=<query>`, which is
# an exact-title match, so a shopper's everyday wording ("tee", "candles",
# "tshirt") finds nothing. These helpers port the forgiving search from
# the demo version of this file (strip "show me / do you have", word-prefix
# + singular matching, best-match-wins) onto the live catalog, and are
# only used where the exact lookup comes back empty or needs to ask
# "which one?".
# =======================================================================
_SEARCH_LEAD_IN = re.compile(
    r"^(do you have|have you got|got any|any|show me|search for|search|looking for|find me|find|browse|see|view|recommend|suggest|what)\b"
)
_SEARCH_FILLER = re.compile(r"\b(products?|items?|please|for me|any|around|got|add|remove|delete|to my cart|from my cart|my cart|cart)\b")
_LEADING_ARTICLE = re.compile(r"^\s*(a|an|the)\s+")
_CHECKOUT_RE = re.compile(r"\b(check ?out|place (my|the|an?) order|buy (it )?now|purchase now)\b", re.IGNORECASE)
_ORDER_NO_RE = re.compile(r"#?\s*(\d{3,})")

# Order/claim lookups (track_order, list_recent_orders, submit_claim,
# check_claim_status in shopify_actions.py) require the shopper to
# confirm the email on file before any details are returned — otherwise
# anyone could pull up anyone else's order just by knowing a number.
# When dispatch() comes back needing that, we park the original request
# here (type="verify_email", handled in _handle_followup below) so the
# shopper's next message — just an email, nothing else — completes it
# instead of being classified fresh and losing the order number/context.
EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")
_VERIFY_ERRORS = {"verification_required", "verification_failed"}


def extract_search_term(message: str) -> str:
    """Strips request-phrasing scaffolding ('search for', 'show me', 'do
    you have any ___ products') off a message, leaving the product term
    the shopper actually cares about."""
    m = _SEARCH_LEAD_IN.sub("", (message or "").lower().strip())
    m = _SEARCH_FILLER.sub("", m)
    m = _LEADING_ARTICLE.sub("", m)
    return re.sub(r"\s+", " ", m).strip(" .,!?")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _term_variants(word: str) -> set:
    singular = word[:-1] if word.endswith("s") and len(word) > 3 else word
    return {word, singular}


def _product_card(p: dict, store) -> dict:
    variant = (p.get("variants") or [{}])[0]
    price = variant.get("price")
    try:
        price = float(price)
        price = int(price) if price == int(price) else price
    except (TypeError, ValueError):
        pass
    card = {
        "id": str(variant.get("id")),
        "name": p.get("title", "Unnamed product"),
        "price": price,
        "image": (p.get("image") or {}).get("src", ""),
    }
    if p.get("handle"):
        card["url"] = f"https://{store.shop_domain}/products/{p['handle']}"
    return card


async def _catalog_search(store, term: str, limit: int = 6) -> list[dict] | None:
    """Forgiving product search over the live catalog. Returns a list of
    product cards (best matches only), [] when nothing matches, or None
    when there's nothing to search for / Shopify couldn't be reached."""
    words = [w for w in _words(extract_search_term(term) or term) if len(w) >= 2]
    if not words:
        return None
    try:
        resp = await shopify_actions._get(store, "products.json", {"status": "active", "limit": 250})
    except Exception as e:  # noqa: BLE001
        print(f"chatbot_widget: catalog fetch failed: {e!r}")
        return None
    if resp.status_code != 200:
        return None

    scored = []
    for p in resp.json().get("products", []):
        hay = _words(p.get("title")) + _words(p.get("product_type")) + _words(p.get("tags"))
        score = sum(1 for w in words if any(h.startswith(v) for h in hay for v in _term_variants(w)))
        if score:
            scored.append((score, p))
    if not scored:
        return []
    best = max(sc for sc, _ in scored)
    top = [p for sc, p in scored if sc == best]
    return [_product_card(p, store) for p in top[:limit]]


async def _resolve_product(store, text: str):
    """Resolves what the shopper said ('tee', 'the candle', 'Linen Tote')
    to ONE product. Returns (product, options): `product` is set only for
    an unambiguous match; `options` holds the candidates when several
    match, so the caller can ask which one instead of guessing."""
    found = await _catalog_search(store, text)
    if not found:
        return None, None
    wanted = (text or "").strip().lower()
    for p in found:
        if p["name"].lower() == wanted:
            return p, None
    if len(found) == 1:
        return found[0], None
    return None, found


def _apply_price_filters(cards: list[dict], entities: dict) -> list[dict]:
    lo, hi = entities.get("price_min"), entities.get("price_max")
    out = []
    for c in cards:
        try:
            price = float(c.get("price"))
        except (TypeError, ValueError):
            out.append(c)
            continue
        if lo is not None and price < float(lo):
            continue
        if hi is not None and price > float(hi):
            continue
        out.append(c)
    return out


def _llm_view(data):
    """What the reply generator sees: names and prices, not image URLs /
    variant ids (they only bloat the prompt)."""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return data
    slim = [{"name": r.get("name"), "price": r.get("price")} for r in data["results"]]
    return {**data, "results": slim}


def _as_variant_id(value):
    s = str(value)
    return int(s) if s.isdigit() else value


# =======================================================================
# Execution
# =======================================================================
@observe(name="execute_and_reply")
async def _execute_and_reply(store: SimpleNamespace, intent: str, action: str, entities: dict, language: str, original_message: str, session_id: str) -> dict:
    raw = await shopify_actions.dispatch(intent, action, store, entities, session_id)
    print(f"DEBUG dispatch: {intent}.{action} entities={entities} -> {raw}")
    data, widget_action = _split_widget_action(raw)

    # Exact-title search came back empty → retry with the forgiving
    # catalog search before telling the shopper "not found".
    if action == "search_products" and isinstance(data, dict) and not data.get("error") and not data.get("results"):
        query = entities.get("query") or entities.get("category")
        if query:
            alt = await _catalog_search(store, str(query))
            if alt:
                alt = _apply_price_filters(alt, entities)
                if alt:
                    data = {**data, "results": alt}

    products = data.get("results") if action == "search_products" and isinstance(data, dict) else None
    orders = None
    if action == "list_recent_orders" and isinstance(data, dict) and isinstance(data.get("orders"), list):
        orders = [{"id": o.get("order_number"), "status": o.get("status")} for o in data["orders"] if o.get("order_number")]

    rag_query = original_message
    if action == "answer_policy_question" and entities.get("policy_type"):
        rag_query = f"{entities['policy_type'].replace('_', ' ')}: {original_message}"

    fallback = "Here's what I found:" if products else ("Done." if widget_action else "Sorry, I couldn't complete that.")
    reply = _safe_reply(
        action, _llm_view(data), language, original_message, fallback,
        store_identifier=store.shop_domain,
        needs_rag=(action == "answer_policy_question"),
        rag_query=rag_query,
    )
    out = {"status": "done", "reply": reply, "language": language, "intent": intent, "action": action}
    if products:
        out["products"] = products
    if orders:
        out["orders"] = orders
    if widget_action:
        out["widget_action"] = widget_action
    if isinstance(data, dict) and "cart" in data:
        out["cart"] = data["cart"]
    if isinstance(data, dict) and data.get("error") in _VERIFY_ERRORS:
        out["action_error"] = data["error"]
    return out


def _arm_verification_if_needed(key: str, intent: str, action: str, entities: dict, language: str, result: dict) -> None:
    """If the action just run came back needing email verification, park
    it (type="verify_email") so the shopper's next message — just an
    email — completes this same request via _handle_followup below."""
    if result.get("action_error") not in _VERIFY_ERRORS:
        return
    _set_pending(key, type="verify_email", intent=intent, action=action, entities=entities, language=language)


def _add_to_cart_result(product: dict, quantity: int, language: str, message: str) -> dict:
    reply = _safe_reply(
        "add_item", {"added": product["name"], "quantity": quantity}, language, message,
        fallback=f"Added {product['name']} to your cart!",
    )
    return {
        "status": "done", "reply": reply, "language": language,
        "intent": "cart_management", "action": "add_item",
        "widget_action": {"type": "cart_add", "variant_id": _as_variant_id(product["id"]), "quantity": quantity},
    }


def _ask(key: str, ptype: str, instruction: str, language: str, message: str, fallback: str, **extra) -> dict:
    """Parks the session awaiting an answer to a 'which item / which
    order?' question and returns the question as a normal chat reply."""
    _set_pending(key, type=ptype, language=language, **extra)
    reply = _safe_reply("clarify", {"instruction": instruction}, language, message, fallback)
    return {"status": "done", "reply": reply, "language": language}


async def _handle_followup(store, shop: str, key: str, pending: dict, message: str, session_id: str) -> dict | None:
    """The shopper's message is the answer to a question we just asked
    ('which item?', 'what's your order number?'). The pending entry has
    already been popped by the caller; re-park it here if we need to ask
    again. Returns None when the message isn't an answer at all, so the
    caller treats it as a brand-new request."""
    ptype = pending["type"]
    language = pending.get("language", "en")
    asked = pending.get("asked", 1)

    if ptype == "verify_email":
        email_match = EMAIL_RE.search(message)
        if not email_match:
            # Doesn't look like an email — the shopper's moved on to
            # something else rather than answering; treat as a fresh request.
            return None
        entities = {**pending["entities"], "email": email_match.group(0)}
        result = await _execute_and_reply(store, pending["intent"], pending["action"], entities, language, message, session_id)
        _arm_verification_if_needed(key, pending["intent"], pending["action"], entities, language, result)
        _log(shop, message, result.get("status", "done"), pending["intent"], pending["action"], entities, result.get("reply", ""))
        return result

    if ptype == "await_search_item":
        term = extract_search_term(message) or message.strip()
        result = await _execute_and_reply(store, "product_search", "search_products", {"query": term}, language, message, session_id)
        _log(shop, message, "done", "product_search", "search_products", {"query": term}, result["reply"])
        return result

    if ptype == "await_order_number":
        m = _ORDER_NO_RE.search(message)
        if m:
            entities = {"order_number": m.group(1)}
            result = await _execute_and_reply(store, "order_tracking", "track_order", entities, language, message, session_id)
            _arm_verification_if_needed(key, "order_tracking", "track_order", entities, language, result)
            _log(shop, message, result.get("status", "done"), "order_tracking", "track_order", entities, result["reply"])
            return result
        # No order number in the reply → the shopper has moved on to
        # something else; let the caller handle it as a fresh request.
        return None

    # await_item / await_remove_item
    is_remove = ptype == "await_remove_item"
    intent, action = "cart_management", ("remove_item" if is_remove else "add_item")

    if is_remove:
        # Removal is matched against the shopper's actual cart in the
        # browser (widget.js asks if it's ambiguous), so no catalog lookup.
        name = extract_search_term(message) or message.strip()
        result = await _execute_and_reply(store, intent, action, {"product_name_or_id": name}, language, message, session_id)
        _log(shop, message, "done", intent, action, {"product_name_or_id": name}, result["reply"])
        return result

    product, options = await _resolve_product(store, message)
    if product:
        result = _add_to_cart_result(product, 1, language, message)
        _log(shop, message, "done", intent, action, {"product_name_or_id": product["name"]}, result["reply"])
        return result
    if options:
        names = [p["name"] for p in options]
        out = _ask(key, ptype, f"Tell the shopper several products match ({', '.join(names)}) and ask which one they mean.",
                   language, message, f"I found a few matches — did you mean {', '.join(names)}?", asked=asked)
        _log(shop, message, "ambiguous", intent, action, {"product_name_or_id": message.strip()}, out["reply"])
        return out

    # Nothing recognizable. Ask once more before giving up, so a vague
    # answer ("yes please", "hmm") gets a second chance.
    if asked < 2:
        out = _ask(key, ptype, "Say you didn't catch an item name and ask the shopper to type the name of the item they'd like to add to their cart.",
                   language, message, "Sorry, I didn't catch an item name. Please enter the name of the item you'd like to add to your cart.",
                   asked=asked + 1)
        _log(shop, message, "awaiting_item", intent, action, None, out["reply"])
        return out
    reply = _safe_reply("add_item", {"error": "not_found", "query": message.strip(),
                                     "instruction": "Say you couldn't find that item and suggest trying the exact product name."},
                        language, message, f'I couldn\'t find "{message.strip()}" — could you try the exact product name?')
    _log(shop, message, "not_found", intent, action, {"product_name_or_id": message.strip()}, reply)
    return {"status": "done", "reply": reply, "language": language}


# =======================================================================
# Routes
# =======================================================================
@router.post("/chat")
@observe(name="chat_request")
async def chat(req: ChatRequest):
    message = (req.message or "").strip()
    if not message:
        return {"reply": "Could you type or say something first?"}

    store = _get_store(req.shop)
    if not store:
        return {"reply": "Sorry, I couldn't verify this store. Please reload the page and try again."}

    cfg = repo.ensure_customization(store.id)
    if cfg.get("status", "active") == "inactive":
        return {"reply": "This assistant isn't available right now."}

    _prune_pending()
    key = _pkey(req.shop, req.session_id)
    pending = PENDING.get(key)

    if pending:
        PENDING.pop(key, None)
        if pending.get("type", "confirm") != "confirm":
            # The shopper's message answers a question we asked.
            try:
                followup = await _handle_followup(store, req.shop, key, pending, message, req.session_id)
            except Exception as e:  # noqa: BLE001
                get_client().update_current_span(level="ERROR", status_message=str(e))
                print(f"chatbot_widget: follow-up error: {e!r}")
                _log(req.shop, message, "error")
                return {"reply": "Sorry, something went wrong completing that. Please try again."}
            if followup is not None:
                return followup
            pending = None  # not an answer — fall through to normal handling

    if pending is not None:
        text = message.lower().strip(" .!")
        if text in _AFFIRMATIVE:
            result = await _execute_and_reply(store, pending["intent"], pending["action"], pending["entities"], pending["language"], message, req.session_id)
            _arm_verification_if_needed(key, pending["intent"], pending["action"], pending["entities"], pending["language"], result)
            _log(req.shop, message, result.get("status", "done"), pending["intent"], pending["action"], pending["entities"], result.get("reply", ""))
            return result
        if text in _NEGATIVE:
            cancel_reply = _safe_reply("cancelled", {"message": "The shopper decided not to proceed."}, pending["language"], message,
                                       fallback="No problem — that request was not submitted.")
            _log(req.shop, message, "cancelled", pending["intent"], pending["action"], pending["entities"], cancel_reply)
            return {"status": "cancelled", "reply": cancel_reply, "language": pending["language"]}
        # Anything else: treat as the shopper moving on to a new request.

    try:
        classification = classify_intent(message, SCHEMA)
    except Exception as e:  # noqa: BLE001
        get_client().update_current_span(level="ERROR", status_message=str(e))
        import traceback
        print(f"chatbot_widget: classify_intent error: {e!r}")
        print(f"chatbot_widget: underlying cause: {e.__cause__!r}")
        traceback.print_exc()
        _log(req.shop, message, "error")
        return {"reply": "Sorry, something went wrong understanding that. Could you rephrase?"}

    intent = classification["intent"]
    action = classification["action"]
    entities = classification.get("entities") or {}
    if intent == "order_tracking" and not (entities.get("order_number") or entities.get("order_id")):
        m = _ORDER_NO_RE.search(message)
        if m:
            entities["order_number"] = m.group(1)
    language = classification["language"]

    try:
        # --- One-tap checkout: not in intent_schema.json, so catch it when
        # the classifier gave up on it.
        if intent == "fallback" and _CHECKOUT_RE.search(message):
            reply = _safe_reply("checkout", {"message": "Taking the shopper to checkout."}, language, message, fallback="Taking you to checkout.")
            _log(req.shop, message, "done", "cart_management", "checkout", None, reply)
            return {"status": "done", "reply": reply, "language": language, "widget_action": {"type": "checkout"}}

        # --- Per-store Features toggles from the dashboard.
        if not _feature_enabled(store.id, intent, action, entities):
            reply = _safe_reply("feature_disabled", {"message": "This store has turned this feature off for the assistant."}, language, message,
                                fallback="Sorry, I can't help with that on this store.")
            _log(req.shop, message, "feature_disabled", intent, action, entities, reply)
            return {"status": "done", "reply": reply, "language": language}

        # --- Ask instead of guessing when the item / order is missing.
        if (intent, action) == ("product_search", "search_products") and not (
            entities.get("query") or entities.get("category") or any(entities.get(k) not in (None, "", []) for k in _FILTER_ENTITIES)
        ):
            out = _ask(key, "await_search_item", "Ask the shopper which item they would like to search for.",
                       language, message, "Sure — which item would you like to search for?")
            _log(req.shop, message, "awaiting_item", intent, action, None, out["reply"])
            return out

        if (intent, action) == ("cart_management", "add_item"):
            term = entities.get("product_name_or_id")
            if not term:
                out = _ask(key, "await_item", "Ask the shopper which item they would like to add to their cart.",
                           language, message, "Sure — which item would you like to add to your cart?", asked=1)
                _log(req.shop, message, "awaiting_item", intent, action, None, out["reply"])
                return out
            product, options = await _resolve_product(store, str(term))
            quantity = int(entities.get("quantity") or 1)
            if product:
                result = _add_to_cart_result(product, quantity, language, message)
                _log(req.shop, message, "done", intent, action, {**entities, "product_name_or_id": product["name"]}, result["reply"])
                return result
            if options:
                names = [p["name"] for p in options]
                out = _ask(key, "await_item", f"Tell the shopper several products match ({', '.join(names)}) and ask which one they mean.",
                           language, message, f"I found a few matches — did you mean {', '.join(names)}?", asked=1)
                _log(req.shop, message, "ambiguous", intent, action, entities, out["reply"])
                return out
            # No forgiving match → fall through to the exact-title lookup below.

        if (intent, action) == ("cart_management", "remove_item") and not entities.get("product_name_or_id"):
            out = _ask(key, "await_remove_item", "Ask the shopper which item they would like to remove from their cart.",
                       language, message, "Sure — which item would you like to remove from your cart?", asked=1)
            _log(req.shop, message, "awaiting_item", intent, action, None, out["reply"])
            return out

        if intent == "order_tracking":
            has_number = bool(entities.get("order_number") or entities.get("order_id") or _ORDER_NO_RE.search(message))
            # list_recent_orders with no email would list the store's most
            # recent orders (other customers') to an anonymous shopper, so
            # without an email we ask for a specific order number instead.
            needs_number = (action == "track_order" and not has_number) or (action == "list_recent_orders" and not entities.get("email"))
            if needs_number:
                out = _ask(key, "await_order_number", "Ask the shopper for their order number (for example #1001).",
                           language, message, "Sure — what's your order number?", asked=1)
                _log(req.shop, message, "awaiting_item", intent, action, None, out["reply"])
                return out
    except Exception as e:  # noqa: BLE001
        get_client().update_current_span(level="ERROR", status_message=str(e))
        print(f"chatbot_widget: pre-dispatch error: {e!r}")
        _log(req.shop, message, "error", intent, action, entities)
        return {"reply": "Sorry, something went wrong completing that. Please try again."}

    if classification["requires_confirmation"]:
        _set_pending(key, type="confirm", intent=intent, action=action, entities=entities, language=language)
        confirm_reply = _safe_reply(
            action,
            {"pending_action": action, "details": entities,
             "instruction": "Ask the shopper to reply yes to confirm or no to cancel before this action is taken."},
            language, message, fallback="Should I go ahead? Please reply yes to confirm or no to cancel.",
        )
        _log(req.shop, message, "confirmation_required", intent, action, entities, confirm_reply)
        return {"status": "confirmation_required", "reply": confirm_reply, "language": language}

    try:
        result = await _execute_and_reply(store, intent, action, entities, language, message, req.session_id)
        _arm_verification_if_needed(key, intent, action, entities, language, result)
        _log(req.shop, message, result.get("status", "done"), intent, action, entities, result.get("reply", ""))
        return result
    except Exception as e:  # noqa: BLE001
        get_client().update_current_span(level="ERROR", status_message=str(e))
        print(f"chatbot_widget: dispatch error: {e}")
        _log(req.shop, message, "error", intent, action, entities)
        return {"reply": "Sorry, something went wrong completing that. Please try again."}


@router.post("/confirm")
@observe(name="confirm_request")
async def confirm(req: ConfirmRequest):
    store = _get_store(req.shop)
    if not store:
        return {"reply": "Sorry, I couldn't verify this store."}

    cfg = repo.ensure_customization(store.id)
    if cfg.get("status", "active") == "inactive":
        return {"reply": "This assistant isn't available right now."}

    _prune_pending()
    pending = PENDING.pop(_pkey(req.shop, req.session_id), None)
    if not pending or pending.get("type", "confirm") != "confirm":
        return {"status": "expired", "reply": "That request has expired — please ask again."}

    if not req.confirmed:
        cancel_reply = _safe_reply("cancelled", {"message": "The shopper declined."}, pending["language"], "cancel",
                                   fallback="No problem — that request was not submitted.")
        _log(req.shop, "confirmed=false", "cancelled", pending["intent"], pending["action"], pending["entities"], cancel_reply)
        return {"status": "cancelled", "reply": cancel_reply, "language": pending["language"]}

    try:
        result = await _execute_and_reply(store, pending["intent"], pending["action"], pending["entities"], pending["language"], "confirmed", req.session_id)
        _arm_verification_if_needed(_pkey(req.shop, req.session_id), pending["intent"], pending["action"], pending["entities"], pending["language"], result)
    except Exception as e:  # noqa: BLE001
        get_client().update_current_span(level="ERROR", status_message=str(e))
        print(f"chatbot_widget: confirm dispatch error: {e}")
        _log(req.shop, "confirmed=true", "error", pending["intent"], pending["action"], pending["entities"])
        return {"reply": "Sorry, something went wrong completing that. Please try again."}
    _log(req.shop, "confirmed=true", result.get("status", "done"), pending["intent"], pending["action"], pending["entities"], result.get("reply", ""))
    return result


@router.get("/widget-config")
async def widget_config(shop: str):
    store = repo.get_store(shop)
    if not store:
        return {"error": "unknown store"}
    cfg = repo.ensure_customization(store["$id"])
    return {
        "status": cfg.get("status", "active"),
        "agent_name": cfg.get("agent_name", "AI Assistant"),
        "agent_title": cfg.get("agent_title", "How can I help you today?"),
        "icon_type": cfg.get("icon_type", "preset"),
        "theme_color": cfg.get("theme_color", "#2b2b2b"),
        "custom_icon_url": cfg.get("custom_icon_url", ""),
    }


@router.get("/widget.js")
async def widget_js():
    return Response(content=WIDGET_JS, media_type="application/javascript")


# --------------------------------------------------------------------------
# The embeddable widget. Reads `data-shop` off its own <script> tag, then
# fetches /widget-config?shop=... to personalize name/title/icon before
# rendering — so one script works for every store. Cart mutations run as
# real fetch() calls against the STORE's own domain (same-origin, since
# this script is embedded on the store's page), not against our backend.
# --------------------------------------------------------------------------
WIDGET_JS = r"""
(function () {
  "use strict";

  var THIS_SCRIPT = document.currentScript;
  var SHOP = (THIS_SCRIPT && THIS_SCRIPT.dataset.shop) || "";
  var ORIGIN = THIS_SCRIPT ? new URL(THIS_SCRIPT.src).origin : "";
  var CFG = {
    chatEndpoint: ORIGIN + "/chat",
    confirmEndpoint: ORIGIN + "/confirm",
    configEndpoint: ORIGIN + "/widget-config?shop=" + encodeURIComponent(SHOP)
  };

  if (!SHOP) { console.warn("[chat widget] missing data-shop attribute on script tag"); return; }
  if (document.getElementById("ai-chat-widget-root")) return;

  var style = document.createElement("style");
  style.textContent = [
    "#ai-chat-widget-root, #ai-chat-widget-root * { box-sizing:border-box; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; }",
    "#ai-chat-widget-root .chat-fab { position:fixed; bottom:18px; right:18px; width:38px; height:38px; border-radius:50%; border:none; box-shadow:0 6px 16px rgba(0,0,0,.25); cursor:pointer; display:flex; align-items:center; justify-content:center; z-index:2147483000; transition:transform .2s cubic-bezier(.34,1.56,.64,1); background-size:cover; background-position:center; }",
    "#ai-chat-widget-root .chat-fab:hover { transform:scale(1.07); }",
    "#ai-chat-widget-root .chat-fab svg { width:18px; height:18px; stroke:#fff; }",
    "#ai-chat-widget-root .chat-fab .icon-close { display:none; }",
    "#ai-chat-widget-root .chat-fab.open .icon-mic { display:none; }",
    "#ai-chat-widget-root .chat-fab.open .icon-close { display:block; }",
    "#ai-chat-widget-root .chat-fab.custom-icon .icon-mic { display:none; }",
    "#ai-chat-widget-root .fab-greeting { position:fixed; bottom:22px; right:64px; max-width:210px; background:#1a1a1a; color:#fff; font-size:12.5px; font-weight:600; line-height:1.3; padding:8px 12px; border-radius:14px; border-bottom-right-radius:4px; box-shadow:0 6px 16px rgba(0,0,0,.2); z-index:2147482999; cursor:pointer; opacity:0; transform:translateY(6px) scale(.95); pointer-events:none; transition:opacity .25s ease, transform .25s cubic-bezier(.34,1.56,.64,1); }",
    "#ai-chat-widget-root .fab-greeting.show { opacity:1; transform:translateY(0) scale(1); pointer-events:auto; }",
    "#ai-chat-widget-root .fab-greeting .fg-order { display:block; margin-top:3px; font-size:11px; font-weight:500; line-height:1.35; opacity:.85; }",
    "#ai-chat-widget-root .widget { position:fixed; bottom:max(72px, env(safe-area-inset-bottom) + 60px); right:18px; width:min(250px, calc(100vw - 24px)); max-height:min(360px, calc(100vh - 100px)); background:rgba(255,255,255,.72); backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px); border:1.5px solid rgba(0,0,0,.12); border-radius:16px; padding:11px; box-shadow:0 12px 28px rgba(0,0,0,.14); transform-origin:bottom right; transform:scale(.9) translateY(10px); opacity:0; pointer-events:none; transition:transform .2s cubic-bezier(.2,.9,.3,1.2), opacity .15s ease, width .25s ease, max-height .25s ease; z-index:2147483000; display:flex; flex-direction:column; }",
    "#ai-chat-widget-root .widget.open { transform:scale(1) translateY(0); opacity:1; pointer-events:auto; }",
    "#ai-chat-widget-root .widget.expanded { width:min(340px, calc(100vw - 24px)); max-height:min(500px, calc(100vh - 60px)); }",
    "#ai-chat-widget-root .header { display:flex; align-items:center; gap:8px; margin-bottom:10px; }",
    "#ai-chat-widget-root .avatar { width:24px; height:24px; border-radius:50%; color:#fff; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:9px; background-size:cover; background-position:center; }",
    "#ai-chat-widget-root .header h1 { margin:0; font-size:12px; font-weight:700; color:#1a1a1a; }",
    "#ai-chat-widget-root .header-right { margin-left:auto; display:flex; align-items:center; gap:6px; }",
    "#ai-chat-widget-root .icon-btn { position:relative; width:21px; height:21px; border-radius:50%; background:#f5f5f5; border:none; display:flex; align-items:center; justify-content:center; cursor:pointer; }",
    "#ai-chat-widget-root .icon-btn svg { width:13px; height:13px; stroke:#444; }",
    "#ai-chat-widget-root .icon-btn.active { background:#2b2b2b; }",
    "#ai-chat-widget-root .icon-btn.active svg { stroke:#fff; }",
    "#ai-chat-widget-root .cart-badge { position:absolute; top:-4px; right:-4px; background:#d64545; color:#fff; font-size:9px; font-weight:700; min-width:15px; height:15px; border-radius:999px; display:none; align-items:center; justify-content:center; padding:0 3px; }",
    "#ai-chat-widget-root .cart-badge.show { display:flex; }",
    "#ai-chat-widget-root #expandToggle .icon-collapse { display:none; }",
    "#ai-chat-widget-root #expandToggle.active-expand .icon-expand { display:none; }",
    "#ai-chat-widget-root #expandToggle.active-expand .icon-collapse { display:block; }",
    "#ai-chat-widget-root .history-divider { text-align:center; font-size:9.5px; color:#999; margin:6px 0; }",
    "#ai-chat-widget-root .history-hint { position:relative; z-index:3; background:transparent !important; border:0; box-shadow:none; flex-shrink:0; text-align:center; font-size:10px; font-weight:500; letter-spacing:.2px; color:rgba(110,110,110,.55); padding:2px 0 4px; max-height:44px; overflow:hidden; cursor:pointer; user-select:none; -webkit-user-select:none; animation:hhFadeIn .6s ease .15s both; transition:transform .2s ease, opacity .3s ease, max-height .35s ease, padding .35s ease; }",
    "@keyframes hhFadeIn { from { opacity:0; transform:translateY(-6px); } to { opacity:1; transform:translateY(0); } }",
    "#ai-chat-widget-root .history-hint .hh-arrow { display:block; width:7px; height:7px; margin:4px auto 0; border-right:1.5px solid rgba(120,120,120,.45); border-bottom:1.5px solid rgba(120,120,120,.45); background:transparent; transform:rotate(45deg); animation:hhBounce 1.4s ease-in-out infinite; }",
    "@keyframes hhBounce { 0%,100% { transform:translateY(0) rotate(45deg); opacity:.45; } 50% { transform:translateY(4px) rotate(45deg); opacity:1; } }",
    "#ai-chat-widget-root .history-hint.leaving { opacity:0; max-height:0; padding:0; }",
    "#ai-chat-widget-root .history-in { animation:hhIn .45s cubic-bezier(.22,1,.36,1) both; }",
    "@keyframes hhIn { from { opacity:0; transform:translateY(-16px); } to { opacity:1; transform:translateY(0); } }",
    "#ai-chat-widget-root .history-label { font-size:9.5px; color:#999; text-align:center; margin:2px 0 6px; }",
    "#ai-chat-widget-root .conversation { flex:1; min-height:0; overflow-y:auto; overflow-x:hidden; display:flex; flex-direction:column; gap:8px; margin-bottom:10px; padding-right:2px; }",
    "#ai-chat-widget-root .bubble { box-sizing:border-box !important; display:block !important; height:auto !important; max-height:none !important; overflow:visible !important; border-radius:12px; padding:7px 9px; font-size:10px; line-height:1.4; width:fit-content; max-width:92%; overflow-wrap:anywhere; word-break:break-word; white-space:pre-wrap; min-width:0; }",
    "#ai-chat-widget-root .bubble.bot { background:rgba(255,255,255,.75) !important; border:1px solid rgba(236,236,236,.8); color:#2a2a2a; box-shadow:0 2px 8px rgba(0,0,0,.05); align-self:flex-start; }",
    "#ai-chat-widget-root .bubble.user { background:#2b2b2b !important; color:#fff; align-self:flex-end; }",
    "#ai-chat-widget-root .bubble.typing { color:#999; font-style:italic; }",
    "#ai-chat-widget-root #greetingBubble { font-size:14px; font-weight:600; line-height:1.5; }",
    "#ai-chat-widget-root .quick-actions { display:flex; flex-direction:column; gap:6px; align-self:flex-start; max-width:92%; }",
    "#ai-chat-widget-root .quick-action-btn { border:1px solid #e2e2e2; background:#fafafa; color:#2b2b2b; font-size:10.5px; font-weight:600; padding:5px 8px; border-radius:999px; text-align:left; cursor:pointer; opacity:0; transform:translateY(6px); transition:opacity .28s ease, transform .28s ease, background .15s ease; }",
    "#ai-chat-widget-root .quick-action-btn.show { opacity:1; transform:translateY(0); }",
    "#ai-chat-widget-root .quick-action-btn:hover { background:#f0f0f0; }",
    "#ai-chat-widget-root .product-row { display:flex; gap:8px; overflow-x:auto; padding:2px 2px 4px; align-self:flex-start; max-width:100%; }",
    "#ai-chat-widget-root .product-card { flex:0 0 auto; width:110px; border:1px solid #ececec; border-radius:10px; padding:6px; background:#fff; box-shadow:0 2px 8px rgba(0,0,0,.05); display:flex; flex-direction:column; gap:4px; cursor:pointer; transition:box-shadow .15s ease, transform .15s ease; }",
    "#ai-chat-widget-root .product-card:hover { box-shadow:0 4px 12px rgba(0,0,0,.1); transform:translateY(-1px); }",
    "#ai-chat-widget-root .product-card img { width:100%; height:70px; object-fit:cover; border-radius:6px; background:#f2f2f2; }",
    "#ai-chat-widget-root .product-card .p-name { font-size:10px; font-weight:600; color:#222; max-height:26px; overflow:hidden; }",
    "#ai-chat-widget-root .product-card .p-price { font-size:10.5px; font-weight:700; color:#2b2b2b; }",
    "#ai-chat-widget-root .product-card button { margin-top:2px; border:none; background:#2b2b2b; color:#fff; font-size:9.5px; padding:3px 0; border-radius:999px; cursor:pointer; }",
    "#ai-chat-widget-root .product-card button:disabled { background:#9c9c9c; }",
    "#ai-chat-widget-root .confirm-row { display:flex; gap:8px; align-self:flex-start; }",
    "#ai-chat-widget-root .confirm-btn { border:1px solid rgba(236,236,236,.8); background:rgba(255,255,255,.75); color:#2a2a2a; font-size:11px; font-weight:700; padding:6px 16px; border-radius:999px; cursor:pointer; transition:background .15s ease, color .15s ease, border-color .15s ease, transform .1s ease; }",
    "#ai-chat-widget-root .confirm-btn:hover:not(:disabled) { background:#2b2b2b; color:#fff; border-color:#2b2b2b; }",
    "#ai-chat-widget-root .confirm-btn:active { transform:scale(.96); }",
    "#ai-chat-widget-root .confirm-btn:disabled { opacity:.5; cursor:default; }",
    "#ai-chat-widget-root .input-row { position:relative;margin-top:15px; }",
    "#ai-chat-widget-root .input-row input { width:100%; padding:11px 42px 11px 13px; border-radius:999px; border:1px solid #e5e5e5; background:#fff; font-size:12px; color:#333; outline:none; box-shadow:0 2px 8px rgba(0,0,0,.05); }",
    "#ai-chat-widget-root .mic-btn { position:absolute; right:5px; top:50%; transform:translateY(-50%); width:31px; height:31px; border-radius:50%; background:#2b2b2b; border:none; display:flex; align-items:center; justify-content:center; cursor:pointer; transition:background .15s ease, box-shadow .08s ease, transform .08s ease; }",
    "#ai-chat-widget-root .mic-btn.listening { background:#d64545; }",
    "#ai-chat-widget-root .mic-btn.speaking { box-shadow:0 0 0 calc(4px + var(--level,0)*12px) rgba(214,69,69,calc(.15 + var(--level,0)*.35)), 0 0 calc(6px + var(--level,0)*18px) calc(2px + var(--level,0)*6px) rgba(214,69,69,calc(.4 + var(--level,0)*.5)); transform:translateY(-50%) scale(calc(1 + var(--level,0)*.12)); }",
    "#ai-chat-widget-root .mic-btn svg { width:17px; height:17px; stroke:#fff; }",
    "#ai-chat-widget-root .mic-btn .icon-send-inner { display:none; }",
    "#ai-chat-widget-root .mic-btn.has-text .icon-mic-inner { display:none; }",
    "#ai-chat-widget-root .mic-btn.has-text .icon-send-inner { display:block; }",
    "#ai-chat-widget-root .mic-status { font-size:9.5px; color:#b04040; margin-top:4px; min-height:12px; }"
  ].join("\n");
  document.head.appendChild(style);

  var root = document.createElement("div");
  root.id = "ai-chat-widget-root";
  root.innerHTML =
    '<button class="chat-fab" id="chatFab" aria-label="Open chat">' +
      '<svg class="icon-mic" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>' +
      '<svg class="icon-close" viewBox="0 0 24 24" fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
    "</button>" +
    '<div class="fab-greeting" id="fabGreeting"></div>' +
    '<div class="widget" id="chatWidget">' +
      '<div class="header">' +
        '<div class="avatar" id="headerAvatar">AI</div>' +
        '<h1 id="headerName">AI Assistant</h1>' +
        '<div class="header-right">' +
          '<button class="icon-btn" id="ttsToggle" title="Toggle spoken replies">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/></svg>' +
          "</button>" +
          '<div class="icon-btn" id="headerCart">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/></svg>' +
            '<span class="cart-badge" id="cartBadge">0</span>' +
          "</div>" +
          '<button class="icon-btn" id="expandToggle" title="Expand chat" aria-label="Expand chat">' +
            '<svg class="icon-expand" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>' +
            '<svg class="icon-collapse" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg>' +
          "</button>" +
        "</div>" +
      "</div>" +
      '<div class="conversation" id="conversation">' +
        '<div class="bubble bot" id="greetingBubble">Hi! How can I help you today?</div>' +
      "</div>" +
      '<div class="input-row">' +
        '<input type="text" id="chatInput" placeholder="Search, add to cart, ask a question....." />' +
        '<button class="mic-btn" id="micBtn" aria-label="Voice input">' +
          '<svg class="icon-mic-inner" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>' +
          '<svg class="icon-send-inner" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>' +
        "</button>" +
      "</div>" +
      '<div class="mic-status" id="micStatus"></div>' +
    "</div>";
  root.style.display = "none"; // hidden until /widget-config confirms this shop's widget is active
  document.body.appendChild(root);

  var fab = document.getElementById("chatFab");
  var widget = document.getElementById("chatWidget");
  var input = document.getElementById("chatInput");
  var micBtn = document.getElementById("micBtn");
  var micStatus = document.getElementById("micStatus");
  var conversation = document.getElementById("conversation");
  var cartBadge = document.getElementById("cartBadge");
  var headerCart = document.getElementById("headerCart");
  var ttsToggle = document.getElementById("ttsToggle");
  var expandToggle = document.getElementById("expandToggle");
  var headerAvatar = document.getElementById("headerAvatar");
  var headerName = document.getElementById("headerName");
  var greetingBubble = document.getElementById("greetingBubble");
  var fabGreeting = document.getElementById("fabGreeting");

  // --------------------------------------------------------------------
  // Shopper/user name: personalizes the greeting when we can find it,
  // without touching payment/card data or any other site's data. Works
  // the same regardless of what platform the embedding site runs on.
  // Legitimate, same-origin sources only:
  //   1) The embedding page puts it on the script tag itself, e.g.
  //        <script src="..." data-shop="..."
  //                data-user-name="Priya"></script>
  //      which the site renders server-side for logged-in users (works
  //      for a Shopify Liquid `{{ customer.first_name }}`, a WordPress
  //      template tag, a hand-rolled server template — anything that can
  //      print a value into HTML). The site opts in by adding this.
  //   2) window.__aiChatUserName, if the page sets that global itself
  //      (e.g. from its own logged-in-user JS state) before this script
  //      runs.
  //   3) The visitor told the widget their name in an earlier chat on
  //      THIS site; kept in localStorage the same way HISTORY_KEY keeps
  //      the conversation, scoped per site (via SHOP as the site key).
  // There's no cross-site or "any source" lookup: same-origin rules (and
  // the visitor's privacy) mean the widget only ever knows what THIS
  // site's page told it or what the visitor told the widget directly.
  // --------------------------------------------------------------------
  var NAME_KEY = "aiChatShopperName_" + SHOP;

  function getKnownShopperName() {
    var fromTag = THIS_SCRIPT && THIS_SCRIPT.dataset.userName;
    if (fromTag && fromTag.trim()) return fromTag.trim();

    try {
      var fromGlobal = window.__aiChatUserName;
      if (fromGlobal && String(fromGlobal).trim()) return String(fromGlobal).trim();
    } catch (e) { /* not set — fall through */ }

    try {
      var saved = localStorage.getItem(NAME_KEY);
      if (saved) return saved;
    } catch (e) { /* storage unavailable */ }

    return null;
  }

  function rememberShopperName(name) {
    if (!name) return;
    try { localStorage.setItem(NAME_KEY, name); } catch (e) { /* storage unavailable — still works this session */ }
  }

  var shopperName = getKnownShopperName();
  if (shopperName) rememberShopperName(shopperName); // refresh in case it came fresh off the tag this visit


  // --------------------------------------------------------------------
  // Order status shown in the launcher pill ("Hi, Priya" + "Your order
  // #1001 has reached Portland, OR"). Same same-origin sources as the name:
  //   1) data-order on the script tag — JSON the site renders server-side
  //      for a logged-in shopper with a recent order, e.g.
  //      data-order='{"id":"1001","place":"Portland, OR"}'
  //      (optional "status": "delivered" | "in_transit" changes the wording)
  //   2) window.__aiChatOrder, if the page sets the same object itself.
  // No order given -> the pill just says hi, like before.
  // --------------------------------------------------------------------
  function getKnownOrder() {
    var o = null;
    try {
      var fromTag = THIS_SCRIPT && THIS_SCRIPT.dataset.order;
      if (fromTag) o = JSON.parse(fromTag);
    } catch (e) { /* malformed or absent — ignore */ }
    try { if (!o && window.__aiChatOrder) o = window.__aiChatOrder; } catch (e) { /* not set */ }
    if (!o || !o.place) return null;
    return o;
  }
  function orderLine(o) {
    var ref = o.id ? "Your order #" + o.id : "Your order";
    if (o.status === "delivered") return ref + " was delivered to " + o.place;
    if (o.status === "in_transit") return ref + " is on its way \u2014 now in " + o.place;
    return ref + " has reached " + o.place;
  }
  var knownOrder = getKnownOrder();

  // Small "Hi, {name}" pill next to the launcher icon — visible on the
  // page itself, before the shopper ever opens the chat. Only shown
  // when we actually know their name and the widget is closed; stays
  // up persistently (doesn't auto-fade) until they open the chat, via
  // this bubble or the FAB itself.
  function showFabGreeting() {
    if (!fabGreeting || (!shopperName && !knownOrder) || widget.classList.contains("open")) return;
    fabGreeting.textContent = "";
    var hi = document.createElement("span");
    hi.textContent = shopperName ? "Hi, " + shopperName + " \uD83D\uDC4B" : "Hi \uD83D\uDC4B";
    fabGreeting.appendChild(hi);
    if (knownOrder) {
      var ord = document.createElement("span");
      ord.className = "fg-order";
      ord.textContent = "\uD83D\uDCE6 " + orderLine(knownOrder);
      fabGreeting.appendChild(ord);
    }
    fabGreeting.classList.add("show");
  }
  function hideFabGreeting() {
    if (!fabGreeting) return;
    fabGreeting.classList.remove("show");
  }
  if (fabGreeting) {
    fabGreeting.addEventListener("click", function () {
      hideFabGreeting();
      fab.click();
    });
  }

  // A fresh, slightly different greeting each visit instead of the same
  // static sentence every time. If the store configured its own custom
  // agent_title (different from the plain default), that's respected
  // as-is — variety only kicks in for stores using the default copy.
  var GREETING_VARIANTS = [
    "How can I help you today?",
    "What can I help you find?",
    "Looking for something specific?",
    "What can I do for you today?",
    "Need help with an order, or just browsing?"
  ];
  var DEFAULT_GREETING = "How can I help you today?";
  function pickRandomGreeting() {
    return GREETING_VARIANTS[Math.floor(Math.random() * GREETING_VARIANTS.length)];
  }

  var resolvedGreetingBase = null; // cached so a name change re-uses the same sentence instead of re-rolling a new random one
  function greetingFor(baseTitle) {
    if (resolvedGreetingBase === null) {
      var isCustomized = baseTitle && baseTitle.trim() && baseTitle.trim() !== DEFAULT_GREETING;
      resolvedGreetingBase = isCustomized ? baseTitle.trim() : pickRandomGreeting();
    }
    var base = resolvedGreetingBase;
    if (!shopperName) return base;
    // Strip a leading "Hi!"/"Hello!" so we don't end up with "Hi Priya! Hi! ..."
    base = base.replace(/^(hi|hello|hey)[!,.\s]*/i, "");
    return "Hi " + shopperName + "! " + base;
  }

  // Re-renders the greeting bubble already sitting at the top of the
  // chat so a name change is visible immediately there too, not just in
  // messages sent after the change.
  function refreshGreetingBubble() {
    if (!greetingBubble) return;
    greetingBubble.textContent = greetingFor(DEFAULT_GREETING);
  }

  var NAME_STOPWORDS = [
    "looking", "trying", "just", "not", "here", "going", "interested",
    "new", "also", "still", "done", "sorry", "fine", "good", "ok",
    "okay", "back", "ready", "waiting", "confused", "stuck", "lost",
    "curious", "wondering", "thinking", "checking", "browsing",
    "shopping", "sure", "afraid", "glad", "happy"
  ];

  // Matches both an initial introduction ("my name is ___", "I'm ___")
  // and a later change ("change my name to ___", "call me ___ instead")
  // — same shape either way, so one regex covers both.
  // Also handles self-corrections in a single message, e.g.
  // "I'm not Priya, I'm Raj" or "my name is not Priya, it's Raj":
  // scans every match in the message (not just the first) and skips
  // any name immediately after "not", so the corrected name wins.
  function extractNameCandidate(text) {
    if (!text) return null;
    var re = /\b(?:change\s+(?:my\s+)?name\s+to|update\s+(?:my\s+)?name\s+to|my name is|i am|i\s+m\b|i'm|im|call me|this is)\s+(not\s+)?([a-zA-Z]{2,20})\b/gi;
    var match, best = null;
    while ((match = re.exec(text)) !== null) {
      var word = match[2];
      var lower = word.toLowerCase();
      if (NAME_STOPWORDS.indexOf(lower) !== -1) continue; // e.g. "I am looking for..."
      if (match[1]) continue; // "...not Priya" — the rejected name, skip it
      best = word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
    }
    if (!best) {
      // Correction phrased without repeating "I'm"/"my name is" a second
      // time: "I'm not Priya but Raj" / "...not Priya, actually Raj".
      var correction = text.match(/\bnot\s+[a-zA-Z]{2,20}\b[,.]?\s*(?:but|actually|i mean|rather|it'?s|its)\s+([a-zA-Z]{2,20})\b/i);
      if (correction && correction[1] && NAME_STOPWORDS.indexOf(correction[1].toLowerCase()) === -1) {
        best = correction[1].charAt(0).toUpperCase() + correction[1].slice(1).toLowerCase();
      }
    }
    return best;
  }

  // Learns the shopper's name the first time, and updates it again any
  // later time they give a different one. One write here (localStorage
  // + shopperProfile) keeps it in sync everywhere the widget uses it:
  // the greeting, the bot's own replies (personalizeReply), and future
  // visits. Returns null if nothing changed (no name found, or it's the
  // same name we already had).
  function tryExtractNameFromMessage(text) {
    var candidate = extractNameCandidate(text);
    if (!candidate) return null;
    if (shopperName && candidate.toLowerCase() === shopperName.toLowerCase()) return null;
    var previous = shopperName;
    shopperName = candidate;
    rememberShopperName(candidate);
    if (shopperProfile) {
      shopperProfile.name = candidate;
      rememberShopperProfile(shopperProfile);
    }
    return { name: candidate, previous: previous };
  }

  // Once the name is known, use it in the bot's own replies too — not
  // just the initial greeting.
  function personalizeReply(text) {
    if (!shopperName || !text) return text;
    if (text.toLowerCase().indexOf(shopperName.toLowerCase()) !== -1) return text;
    return shopperName + ", " + text;
  }

  // --------------------------------------------------------------------
  // Shipping profile for one-tap checkout. NEVER includes payment/card
  // data — that always happens inside Shopify's own secure checkout,
  // never through this widget. Same same-origin sources as the name:
  //   1) data-user-profile on the script tag — a JSON string the site
  //      renders server-side for a logged-in visitor with a saved
  //      address, e.g.
  //      data-user-profile='{"email":"a@b.com","address1":"12 Oak St",...}'
  //   2) window.__aiChatUserProfile, if the page sets it itself.
  //   3) Whatever the shopper has already given the widget on THIS site,
  //      kept in localStorage.
  // NOTE: a shopper logged into their own Shopify account with a saved
  // default address already gets it auto-filled at checkout — natively,
  // with no help from this widget. This only helps guest shoppers who've
  // told the widget their details before. Which fields the checkout URL
  // actually honors can vary by store/checkout version — verify on the
  // live store before relying on it.
  // --------------------------------------------------------------------
  var PROFILE_KEY = "aiChatShopperProfile_" + SHOP;

  function mergeProfile(base, extra) {
    var out = {};
    for (var k in base) out[k] = base[k];
    for (var k2 in extra) { if (extra[k2]) out[k2] = extra[k2]; }
    return out;
  }

  function getKnownShopperProfile() {
    var profile = {};
    try {
      var saved = localStorage.getItem(PROFILE_KEY);
      if (saved) profile = JSON.parse(saved) || {};
    } catch (e) { profile = {}; }
    try {
      var fromTag = THIS_SCRIPT && THIS_SCRIPT.dataset.userProfile;
      if (fromTag) profile = mergeProfile(profile, JSON.parse(fromTag));
    } catch (e) { /* malformed or absent — ignore */ }
    try {
      if (window.__aiChatUserProfile) profile = mergeProfile(profile, window.__aiChatUserProfile);
    } catch (e) { /* not set */ }
    return profile;
  }

  function rememberShopperProfile(profile) {
    try { localStorage.setItem(PROFILE_KEY, JSON.stringify(profile)); } catch (e) { /* storage unavailable */ }
  }

  var shopperProfile = getKnownShopperProfile();
  if (shopperName && !shopperProfile.name) shopperProfile.name = shopperName;
  if (Object.keys(shopperProfile).length) rememberShopperProfile(shopperProfile);

  // Emails are safe/unambiguous to lift straight out of a chat message
  // (unlike a full address, which is too error-prone to parse from free
  // text — that needs the site-provided sources above instead).
  function tryExtractEmailFromMessage(text) {
    if (shopperProfile.email || !text) return;
    var m = text.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/);
    if (m) {
      shopperProfile.email = m[0];
      rememberShopperProfile(shopperProfile);
    }
  }

  function buildCheckoutUrl(profile) {
    var params = [];
    function add(key, val) { if (val) params.push(encodeURIComponent(key) + "=" + encodeURIComponent(val)); }
    if (profile) {
      var nameParts = (profile.name || "").trim().split(/\s+/);
      add("checkout[email]", profile.email);
      add("checkout[shipping_address][first_name]", nameParts[0]);
      add("checkout[shipping_address][last_name]", nameParts.slice(1).join(" "));
      add("checkout[shipping_address][address1]", profile.address1);
      add("checkout[shipping_address][address2]", profile.address2);
      add("checkout[shipping_address][city]", profile.city);
      add("checkout[shipping_address][province]", profile.province);
      add("checkout[shipping_address][zip]", profile.zip);
      add("checkout[shipping_address][country]", profile.country);
      add("checkout[shipping_address][phone]", profile.phone);
    }
    return "/checkout" + (params.length ? "?" + params.join("&") : "");
  }

  fetch(CFG.configEndpoint)
    .then(function (r) { return r.json(); })
    .then(function (cfg) {
      if (cfg && cfg.status === "inactive") {
        root.remove(); // shop turned the widget off — take it off the page entirely
        return;
      }
      root.style.display = "";
      setTimeout(showFabGreeting, 800);
      if (!cfg || cfg.error) return;
      headerName.textContent = cfg.agent_name || "AI Assistant";
      greetingBubble.textContent = greetingFor(cfg.agent_title);

      if (cfg.icon_type === "custom" && cfg.custom_icon_url) {
        var url = cfg.custom_icon_url.indexOf("http") === 0 ? cfg.custom_icon_url : ORIGIN + cfg.custom_icon_url;
        fab.style.backgroundImage = "url(" + url + ")";
        fab.classList.add("custom-icon");
        headerAvatar.style.backgroundImage = "url(" + url + ")";
        headerAvatar.textContent = "";
      } else {
        var color = cfg.theme_color || "#2b2b2b";
        fab.style.background = color;
        headerAvatar.style.background = color;
      }
    })
    .catch(function () { root.style.display = ""; setTimeout(showFabGreeting, 800); /* fall back to defaults already in the markup */ });

  var SESSION_ID = (function () {
    try {
      var id = sessionStorage.getItem("chatSessionId");
      if (!id) {
        id = "sess_" + Math.random().toString(36).slice(2) + Date.now();
        sessionStorage.setItem("chatSessionId", id);
      }
      return id;
    } catch (e) {
      return "sess_" + Math.random().toString(36).slice(2) + Date.now();
    }
  })();

  // --------------------------------------------------------------------
  // Cross-page persistence: Shopify does a full page reload on nearly
  // every navigation, so any in-memory chat state is normally lost.
  // We mirror open/expanded state + the full message history to
  // sessionStorage (scoped to this browser tab's session, same lifetime
  // as SESSION_ID above) and replay it back into the DOM on load.
  // --------------------------------------------------------------------
  var STATE_KEY = "aiChatWidgetState_" + SHOP;
  // Durable copy of the conversation, kept in localStorage (unlike STATE_KEY
  // above, this survives closing the tab/browser). Feeds the "Previous
  // chats" block shown under the quick actions when the chat is
  // opened (see showPreviousChats).
  var HISTORY_KEY = "aiChatWidgetHistory_" + SHOP;
  // How many past messages to surface when the chat is opened, and a
  // guard so they're only ever shown once per page load.
  var RECENT_HISTORY_MAX = 7;
  var historyShown = false;
  var chatHistory = [];
  // Earlier visits' messages, read ONCE before anything can overwrite
  // localStorage. persistState() writes prior + current back together so
  // opening the chat can't wipe what the swipe-down is meant to show.
  var priorHistory = (function () {
    try {
      var raw = localStorage.getItem(HISTORY_KEY);
      var parsed = raw ? JSON.parse(raw) : null;
      return parsed && parsed.history ? parsed.history : [];
    } catch (e) { return []; }
  })();
  function persistState() {
    try {
      sessionStorage.setItem(STATE_KEY, JSON.stringify({
        open: widget.classList.contains("open"),
        expanded: widget.classList.contains("expanded"),
        quickActionsShown: quickActionsRendered,
        history: chatHistory
      }));
    } catch (e) { /* storage unavailable/full — chat still works, just won't persist */ }
    try {
      // Cap what we keep long-term so localStorage doesn't grow unbounded
      // over a long-running chat history.
      localStorage.setItem(HISTORY_KEY, JSON.stringify({ history: priorHistory.concat(chatHistory).slice(-50) }));
    } catch (e) { /* storage unavailable/full — popup just won't have data */ }
  }

  function replayHistory(history) {
    (history || []).forEach(function (item) {
      if (item.type === "bubble") addBubble(item.text, item.who, { record: false, silent: true });
      else if (item.type === "products") addProductRow(item.products, { record: false });
      else if (item.type === "orders") addOrderPicker(item.orders, { record: false });
      else if (item.type === "confirm") addConfirmationButtons({ record: false, id: item.id });
    });
    chatHistory = (history || []).slice();
  }

  var ttsEnabled = false;
  function speak(text) {
    if (!ttsEnabled || !window.speechSynthesis) return;
    try {
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
    } catch (e) { /* unsupported — ignore */ }
  }
  ttsToggle.addEventListener("click", function () {
    ttsEnabled = !ttsEnabled;
    ttsToggle.classList.toggle("active", ttsEnabled);
    if (!ttsEnabled && window.speechSynthesis) window.speechSynthesis.cancel();
  });

  expandToggle.addEventListener("click", function () {
    var isExpanded = widget.classList.toggle("expanded");
    expandToggle.classList.toggle("active-expand", isExpanded);
    expandToggle.title = isExpanded ? "Collapse chat" : "Expand chat";
    autoResizeConversation();
    persistState();
  });

  var quickActionsRendered = false;
  fab.addEventListener("click", function () {
    var isOpen = widget.classList.toggle("open");
    fab.classList.toggle("open", isOpen);
    if (isOpen) { renderQuickActions(); hideFabGreeting(); }
    else { showFabGreeting(); }
    persistState();
  });

  function autoResizeConversation() {
    requestAnimationFrame(function () { conversation.scrollTop = conversation.scrollHeight; });
  }

  function addBubble(text, who, opts) {
    opts = opts || {};
    var el = document.createElement("div");
    el.className = "bubble " + who;
    el.textContent = text;
    conversation.appendChild(el);
    autoResizeConversation();
    if (who.indexOf("typing") === -1) {
      if (opts.record !== false) { chatHistory.push({ type: "bubble", text: text, who: who }); persistState(); }
      if (who === "bot" && !opts.silent) speak(text);
    }
    return el;
  }

  function addProductRow(products, opts) {
    opts = opts || {};
    var row = document.createElement("div");
    row.className = "product-row";
    products.forEach(function (p) {
      var card = document.createElement("div");
      card.className = "product-card";
      var img = document.createElement("img");
      img.src = p.image || ""; img.alt = p.name || "";
      var name = document.createElement("div");
      name.className = "p-name"; name.textContent = p.name || "Unnamed product";
      var price = document.createElement("div");
      price.className = "p-price";
      price.textContent = p.price !== undefined ? "$" + p.price : "";
      var btn = document.createElement("button");
      btn.textContent = "Add to cart";
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        cartAdd(p.id, 1, btn);
      });
      card.appendChild(img); card.appendChild(name); card.appendChild(price); card.appendChild(btn);
      if (p.url) {
        // Open the product page in a new tab (rather than navigating
        // the current tab away) so the shopper doesn't lose their place
        // in the chat/storefront while browsing.
        card.addEventListener("click", function () { window.open(p.url, "_blank", "noopener"); });
      }
      row.appendChild(card);
    });
    conversation.appendChild(row);
    autoResizeConversation();
    if (opts.record !== false) { chatHistory.push({ type: "products", products: products }); persistState(); }
  }

  // --------------------------------------------------------------------
  // Yes/No confirmation prompts. The backend already has a full pending-
  // confirmation flow (see /chat and /confirm in this file): when /chat
  // returns status "confirmation_required", it means the shopper's
  // request is parked server-side awaiting a yes/no. We render buttons
  // that call POST /confirm with { session_id, shop, confirmed } — that
  // endpoint runs the pending action (or cancels it) and returns a
  // normal chat-shaped response (reply / products / widget_action).
  // --------------------------------------------------------------------
  // --------------------------------------------------------------------
  // Order picker: shown when the shopper has more than one order and the
  // backend can't tell which one "track my order" means. The BACKEND
  // decides whether to show this at all — if the shopper only has one
  // order, shopify_actions.py should just track it directly instead of
  // asking. This only renders whatever list a normal /chat response
  // sends back as `orders: [{id, date, status}, ...]`; clicking one
  // re-asks the same way the shopper would by typing the order number.
  // --------------------------------------------------------------------
  function addOrderPicker(orders, opts) {
    opts = opts || {};
    var row = document.createElement("div");
    row.className = "quick-actions";
    orders.forEach(function (o, i) {
      var btn = document.createElement("button");
      btn.className = "quick-action-btn";
      var label = "#" + o.id;
      if (o.date) label += " \u2014 " + o.date;
      if (o.status) label += " (" + o.status + ")";
      btn.textContent = label;
      btn.addEventListener("click", function () {
        Array.prototype.forEach.call(row.querySelectorAll("button"), function (b) { b.disabled = true; });
        sendMessage("Track order #" + o.id);
      });
      row.appendChild(btn);
      setTimeout(function () { btn.classList.add("show"); autoResizeConversation(); }, i * 150);
    });
    conversation.appendChild(row);
    autoResizeConversation();
    if (opts.record !== false) { chatHistory.push({ type: "orders", orders: orders }); persistState(); }
  }

  function handleChatResponse(data) {
    addBubble(personalizeReply(data.reply) || "(no reply)", "bot");
    if (Array.isArray(data.products) && data.products.length > 0) addProductRow(data.products);
    if (Array.isArray(data.orders) && data.orders.length > 0) addOrderPicker(data.orders);
    if (data.status === "confirmation_required") addConfirmationButtons();
    if (data.widget_action) runWidgetAction(data.widget_action);
    refreshCartBadge();
  }

  function addConfirmationButtons(opts) {
    opts = opts || {};
    var confirmId = opts.id || (Date.now() + "_" + Math.random().toString(36).slice(2));
    var row = document.createElement("div");
    row.className = "confirm-row";
    [{ label: "Yes", cls: "confirm-yes", confirmed: true }, { label: "No", cls: "confirm-no", confirmed: false }].forEach(function (opt) {
      var btn = document.createElement("button");
      btn.className = "confirm-btn " + opt.cls;
      btn.textContent = opt.label;
      btn.addEventListener("click", function () {
        Array.prototype.forEach.call(row.querySelectorAll("button"), function (b) { b.disabled = true; });
        chatHistory = chatHistory.filter(function (item) { return !(item.type === "confirm" && item.id === confirmId); });
        persistState();
        row.remove();
        addBubble(opt.label, "user");
        var typingEl = addBubble("typing\u2026", "bot typing");
        fetch(CFG.confirmEndpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: SESSION_ID, shop: SHOP, confirmed: opt.confirmed })
        })
          .then(function (res) { return res.json(); })
          .then(function (data) { typingEl.remove(); handleChatResponse(data); })
          .catch(function () {
            typingEl.remove();
            addBubble("Sorry, I could not reach the server. Please try again.", "bot");
          });
      });
      row.appendChild(btn);
    });
    conversation.appendChild(row);
    autoResizeConversation();
    if (opts.record !== false) {
      chatHistory.push({ type: "confirm", id: confirmId });
      persistState();
    }
    return row;
  }

  function updateCartBadge(count) {
    if (count === undefined || count === null) return;
    cartBadge.textContent = count;
    cartBadge.classList.toggle("show", count > 0);
  }

  // --------------------------------------------------------------------
  // Tapping the header cart icon should open the STORE's own cart, not
  // some cart view we invent inside the widget. Every Shopify theme wires
  // its cart icon/drawer differently and there's no way to know which one
  // a given store uses ahead of time, so this tries a list of selectors
  // used by common theme families first — clicking whichever one exists
  // and is actually visible on the page — and only falls back to a plain
  // navigation to /cart if none of them match.
  // --------------------------------------------------------------------
  var CART_TRIGGER_SELECTORS = [
    "#siteCartBtn", // demo/preview page's own cart button, harmless on a real store
    "#cart-icon-bubble", "#CartDrawer-Toggle", "#cart-icon", "#CartLink",
    "[data-cart-drawer-toggle]", "[data-drawer-toggle='cart']", "[data-cart-icon]",
    "[aria-controls='CartDrawer']", ".header__icon--cart", ".cart-icon-bubble",
    ".js-drawer-open-cart", "a.site-header__cart-link", "button[data-cart-toggle]",
    "a[href='/cart']"
  ];

  function openSiteCart() {
    for (var i = 0; i < CART_TRIGGER_SELECTORS.length; i++) {
      var el;
      try { el = document.querySelector(CART_TRIGGER_SELECTORS[i]); } catch (e) { continue; }
      // Skip anything that doesn't exist, isn't actually visible, or is
      // part of our own widget (so this can never click itself).
      if (el && !root.contains(el) && el.offsetParent !== null) {
        el.click();
        return;
      }
    }
    window.location.href = "/cart";
  }
  headerCart.addEventListener("click", openSiteCart);

  // --------------------------------------------------------------------
  // Real cart mutations, run in the SHOPPER's browser against the
  // store's own /cart/*.js AJAX API — same-origin, since this script is
  // embedded on the store's own page. The backend never touches carts
  // directly (see shopify_actions.py); it only tells us *what* to do.
  // --------------------------------------------------------------------
  function refreshCartBadge() {
    fetch("/cart.js").then(function (r) { return r.json(); }).then(function (cart) {
      updateCartBadge(cart.item_count);
    }).catch(function () {});
  }

  function cartAdd(variantId, quantity, btnEl) {
    if (btnEl) { btnEl.disabled = true; btnEl.textContent = "Adding\u2026"; }
    fetch("/cart/add.js", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: [{ id: variantId, quantity: quantity || 1 }] })
    })
      .then(function (res) { if (!res.ok) throw new Error("add failed"); return res.json(); })
      .then(function () {
        if (btnEl) btnEl.textContent = "Added \u2713";
        refreshCartBadge();
      })
      .catch(function () {
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = "Add to cart"; }
        addBubble("Sorry, could not add that to your cart. Please try again.", "bot");
      });
  }

  // --------------------------------------------------------------------
  // Targeted line lookup: an exact variant_id match (the backend's own
  // catalog id for that item) is tried first so a remove/quantity command
  // only ever touches the single line it named. Falling back to a name
  // match is scoped the same way — if the name matches more than one line
  // we ask instead of guessing, so "remove the mug" never removes
  // everything or the wrong item.
  // --------------------------------------------------------------------
  function findCartLineByVariant(cart, variantId) {
    if (variantId === undefined || variantId === null) return null;
    for (var i = 0; i < cart.items.length; i++) {
      if (String(cart.items[i].variant_id) === String(variantId)) return { line: i + 1, item: cart.items[i] };
    }
    return null;
  }

  function findCartLinesByName(cart, productName) {
    if (!productName) return [];
    var needle = productName.toLowerCase();
    var matches = [];
    for (var i = 0; i < cart.items.length; i++) {
      if (cart.items[i].product_title.toLowerCase().indexOf(needle) !== -1) {
        matches.push({ line: i + 1, item: cart.items[i] });
      }
    }
    return matches;
  }

  function cartChangeItem(action, quantity) {
    fetch("/cart.js").then(function (r) { return r.json(); }).then(function (cart) {
      var target = findCartLineByVariant(cart, action.variant_id);
      if (target) {
        return fetch("/cart/change.js", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ line: target.line, quantity: quantity })
        }).then(function () { refreshCartBadge(); });
      }

      var byName = action.product_name ? findCartLinesByName(cart, action.product_name) : [];
      if (byName.length === 1) {
        return fetch("/cart/change.js", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ line: byName[0].line, quantity: quantity })
        }).then(function () { refreshCartBadge(); });
      }

      // Nothing specific enough to act on safely — ask by text rather
      // than showing a tappable picker.
      if (quantity === 0) {
        if (!cart.items.length) { addBubble("Your cart is empty \u2014 nothing to remove.", "bot"); return; }
        var titles = (byName.length > 1 ? byName.map(function (e) { return e.item.product_title; })
                                        : cart.items.map(function (it) { return it.product_title; }));
        addBubble(byName.length > 1
          ? "I found a few matches \u2014 did you mean " + titles.join(", ") + "?"
          : "Which item would you like to remove? Your cart has: " + titles.join(", "), "bot");
        return;
      }
      addBubble(action.product_name
        ? "I couldn't find \"" + action.product_name + "\" in your cart."
        : "Could you tell me which item, and I'll update the quantity?", "bot");
    }).catch(function () {});
  }

  function cartView() {
    fetch("/cart.js").then(function (r) { return r.json(); }).then(function (cart) {
      if (!cart.items.length) { addBubble("Your cart is empty.", "bot"); return; }
      var lines = cart.items.map(function (it) {
        return it.quantity + "x " + it.product_title + " (" + (it.final_line_price / 100).toFixed(2) + ")";
      });
      addBubble("Your cart:\n" + lines.join("\n") + "\nTotal: " + (cart.total_price / 100).toFixed(2), "bot");
      updateCartBadge(cart.item_count);
    }).catch(function () {});
  }

  function cartClear() {
    fetch("/cart/clear.js", { method: "POST" }).then(function () { updateCartBadge(0); }).catch(function () {});
  }

  // --------------------------------------------------------------------
  // One-tap checkout: adds whatever the shopper just asked to order
  // (if anything — they may already have items in cart), then sends
  // them straight to checkout with their saved shipping details
  // pre-filled if we have any. Payment is never touched here; that part
  // always happens inside Shopify's own secure checkout page.
  // --------------------------------------------------------------------
  function goToCheckout(items) {
    var url = buildCheckoutUrl(shopperProfile);
    if (items && items.length) {
      var remaining = items.length;
      items.forEach(function (it) {
        fetch("/cart/add.js", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ items: [{ id: it.variant_id, quantity: it.quantity || 1 }] })
        })
          .catch(function () {})
          .then(function () {
            remaining -= 1;
            if (remaining === 0) window.location.href = url;
          });
      });
    } else {
      window.location.href = url;
    }
  }

  function runWidgetAction(action) {
    if (!action || !action.type) return;
    switch (action.type) {
      case "redirect":
        if (action.url) setTimeout(function () { window.location.href = action.url; }, 600);
        break;
      case "cart_add":
        cartAdd(action.variant_id, action.quantity || 1, null);
        break;
      case "cart_remove":
        cartChangeItem(action, 0);
        break;
      case "cart_set_quantity":
        cartChangeItem(action, action.quantity || 1);
        break;
      case "cart_view":
        cartView();
        break;
      case "cart_clear":
        cartClear();
        break;
      case "checkout":
        goToCheckout(action.items || (action.variant_id ? [{ variant_id: action.variant_id, quantity: action.quantity || 1 }] : []));
        break;
    }
  }

  var QUICK_ACTIONS = [
    { icon: "\uD83D\uDD0D", label: "Search products", command: "Show me products" },
    { icon: "\uD83D\uDED2", label: "Add an item to cart", command: "I'd like to add an item to my cart" },
    { icon: "\uD83D\uDCB2", label: "Filter by price", command: "Show me products under $20" },
    { icon: "\uD83D\uDEE1\uFE0F", label: "Claim a warranty", command: "I want to claim a warranty for order #1001" },
    { icon: "\uD83D\uDCE6", label: "Track my order", command: "Track my order" }
  ];
  function renderQuickActions() {
    if (quickActionsRendered) return;
    quickActionsRendered = true;
    offerPreviousChats(RECENT_HISTORY_MAX);
    var row = document.createElement("div");
    row.className = "quick-actions";
    conversation.appendChild(row);
    QUICK_ACTIONS.forEach(function (action, i) {
      var btn = document.createElement("button");
      btn.className = "quick-action-btn";
      btn.textContent = action.icon + " " + action.label;
      btn.addEventListener("click", function () { sendMessage(action.command); });
      row.appendChild(btn);
      setTimeout(function () {
        btn.classList.add("show");
        autoResizeConversation();
      }, i * 500);
    });
  }

  function sendMessage(text) {
    text = (text || "").trim();
    if (!text) return;
    var nameUpdate = tryExtractNameFromMessage(text);
    tryExtractEmailFromMessage(text);
    addBubble(text, "user");
    input.value = "";
    micBtn.classList.remove("has-text");

    if (nameUpdate) {
      // Introduction vs. correction get a different tone: a fresh name
      // is a cheerful "nice to meet you", a changed one gets a small,
      // gender-neutral apology first (we don't guess Mr./Ms. from a
      // name — that'd be a guess we can easily get wrong).
      var ack = nameUpdate.previous
        ? "My apologies, " + nameUpdate.previous + " \u2014 I'll call you " + nameUpdate.name + " from now on!"
        : "Nice to meet you, " + nameUpdate.name + "! How can I help you today?";
      addBubble(ack, "bot");
      refreshGreetingBubble();
      return;
    }

    var typingEl = addBubble("typing\u2026", "bot typing");

    fetch(CFG.chatEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: SESSION_ID, shop: SHOP })
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        typingEl.remove();
        handleChatResponse(data);
      })
      .catch(function () {
        typingEl.remove();
        addBubble("Sorry, I could not reach the server. Please try again.", "bot");
      });
  }
  input.addEventListener("keydown", function (e) { if (e.key === "Enter") sendMessage(input.value); });
  input.addEventListener("input", function () {
    micBtn.classList.toggle("has-text", input.value.trim().length > 0);
  });

  // Replay persisted state (previous page's conversation + open/expanded
  // state), if any, before the first paint-affecting network call.
  (function restoreState() {
    var saved = null;
    try {
      var raw = sessionStorage.getItem(STATE_KEY);
      if (raw) saved = JSON.parse(raw);
    } catch (e) { saved = null; }
    if (!saved) return;

    // Same-tab restore: the conversation being replayed below IS the
    // current one, so don't also append a "previous conversation" block
    // underneath it.
    priorHistory = priorHistory.slice(0, Math.max(0, priorHistory.length - (saved.history || []).length));

    if (saved.quickActionsShown) renderQuickActions();
    replayHistory(saved.history);
    if (saved.open) { widget.classList.add("open"); fab.classList.add("open"); }
    if (saved.expanded) {
      widget.classList.add("expanded");
      expandToggle.classList.add("active-expand");
      expandToggle.title = "Collapse chat";
    }
    autoResizeConversation();
  })();

  // getRecentBubbles feeds showPreviousChats below with the last N
  // bubble messages from localStorage.
  function getRecentBubbles(history, max) {
    var out = [];
    for (var i = history.length - 1; i >= 0 && out.length < max; i--) {
      if (history[i].type === "bubble") out.unshift(history[i]);
    }
    return out;
  }

  // Previous chats are NOT dumped into the chat automatically. Instead a
  // faint "Swipe down to see previous chats" hint sits at the very top of
  // the conversation; when the shopper pulls down (touch swipe, mouse
  // drag, scroll-up at the top, or a tap on the hint) the last few
  // messages from an earlier visit slide in underneath the quick-action
  // buttons, one after another.
  function getPreviousBubbles(count) {
    return getRecentBubbles(priorHistory, count);
  }

  var historyHintEl = null;

  function offerPreviousChats(count) {
    if (historyShown || historyHintEl) return;

    var hint = document.createElement("div");
    hint.className = "history-hint";
    hint.setAttribute("role", "button");
    hint.setAttribute("tabindex", "0");
    var label = document.createElement("span");
    label.textContent = "Swipe down to see previous chats";
    var arrow = document.createElement("span");
    arrow.className = "hh-arrow";
    hint.appendChild(label);
    hint.appendChild(arrow);
    conversation.insertBefore(hint, conversation.firstChild);
    conversation.scrollTop = 0;
    historyHintEl = hint;

    var PULL_TO_OPEN = 45;
    var startY = null;
    function pullStart(y) { startY = conversation.scrollTop <= 0 ? y : null; }
    function pullMove(y) {
      if (startY === null) return;
      var dy = y - startY;
      if (dy <= 0) { hint.style.transform = ""; return; }
      hint.style.transform = "translateY(" + Math.min(dy, 60) * 0.5 + "px)";
      if (dy >= PULL_TO_OPEN) { startY = null; showPreviousChats(count); }
    }
    function pullEnd() { startY = null; hint.style.transform = ""; }

    conversation.addEventListener("touchstart", function (e) { pullStart(e.touches[0].clientY); }, { passive: true });
    conversation.addEventListener("touchmove", function (e) { pullMove(e.touches[0].clientY); }, { passive: true });
    conversation.addEventListener("touchend", pullEnd);
    conversation.addEventListener("touchcancel", pullEnd);

    var mouseDown = false;
    conversation.addEventListener("mousedown", function (e) { mouseDown = true; pullStart(e.clientY); });
    document.addEventListener("mousemove", function (e) { if (mouseDown) pullMove(e.clientY); });
    document.addEventListener("mouseup", function () { mouseDown = false; pullEnd(); });

    conversation.addEventListener("wheel", function (e) {
      if (e.deltaY < -12 && conversation.scrollTop <= 0) showPreviousChats(count);
    }, { passive: true });

    hint.addEventListener("click", function () { showPreviousChats(count); });
    hint.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); showPreviousChats(count); }
    });
  }

  // Slides the last few messages from a previous session in, directly
  // underneath the quick-action buttons (above anything sent since).
  function showPreviousChats(count) {
    if (historyShown) return;
    historyShown = true;
    var recent = getPreviousBubbles(count);

    if (!recent.length) {
      // Nothing from an earlier visit: say so, then tidy the hint away.
      if (historyHintEl) {
        var e = historyHintEl;
        historyHintEl = null;
        e.firstChild.textContent = "No previous chats yet";
        var arrowEl = e.querySelector(".hh-arrow");
        if (arrowEl) arrowEl.style.display = "none";
        setTimeout(function () {
          e.classList.add("leaving");
          setTimeout(function () { if (e.parentNode) e.parentNode.removeChild(e); }, 400);
        }, 1600);
      }
      return;
    }
    if (historyHintEl) {
      var h = historyHintEl;
      historyHintEl = null;
      h.classList.add("leaving");
      setTimeout(function () { if (h.parentNode) h.parentNode.removeChild(h); }, 400);
    }

    var anchor = conversation.querySelector(".quick-actions");
    function place(el, index) {
      el.classList.add("history-in");
      el.style.animationDelay = (index * 90) + "ms";
      if (anchor) { conversation.insertBefore(el, anchor.nextSibling); anchor = el; }
    }

    var divider = document.createElement("div");
    divider.className = "history-divider";
    divider.textContent = "Previous conversation";
    if (!anchor) conversation.appendChild(divider);
    place(divider, 0);

    // record:false keeps these out of chatHistory (they're already in it
    // from last time); silent:true stops the TTS from reading them back.
    recent.forEach(function (item, i) {
      var b = addBubble(item.text, item.who, { record: false, silent: true });
      place(b, i + 1);
    });
    autoResizeConversation();
  }

  refreshCartBadge();

  var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  var recognition = null, listening = false;
  var audioCtx = null, analyser = null, micStream = null, rafId = null;

  function startGlow() {
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      micStream = stream;
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      var source = audioCtx.createMediaStreamSource(stream);
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.6;
      source.connect(analyser);
      var data = new Uint8Array(analyser.frequencyBinCount);
      (function tick() {
        analyser.getByteTimeDomainData(data);
        var sumSquares = 0;
        for (var i = 0; i < data.length; i++) { var c = (data[i] - 128) / 128; sumSquares += c * c; }
        var level = Math.min(1, Math.sqrt(sumSquares / data.length) * 6);
        micBtn.style.setProperty("--level", level.toFixed(3));
        micBtn.classList.toggle("speaking", level > 0.04);
        rafId = requestAnimationFrame(tick);
      })();
    }).catch(function () { micStatus.textContent = "Mic permission needed for glow effect."; });
  }
  function stopGlow() {
    if (rafId) cancelAnimationFrame(rafId);
    rafId = null;
    micBtn.classList.remove("speaking");
    micBtn.style.setProperty("--level", 0);
    if (micStream) { micStream.getTracks().forEach(function (t) { t.stop(); }); micStream = null; }
    if (audioCtx) { audioCtx.close(); audioCtx = null; }
    analyser = null;
  }

  if (SpeechRecognition) {
    recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = "en-US";
    recognition.onstart = function () {
      listening = true;
      micBtn.classList.add("listening");
      micStatus.textContent = "Listening\u2026";
      startGlow();
    };
    recognition.onresult = function (event) {
      var interim = "", final = "";
      for (var i = 0; i < event.results.length; i++) {
        var t = event.results[i][0].transcript;
        if (event.results[i].isFinal) final += t; else interim += t;
      }
      input.value = final || interim;
    };
    recognition.onerror = function (event) { micStatus.textContent = "Mic error: " + event.error; };
    recognition.onend = function () {
      listening = false;
      micBtn.classList.remove("listening");
      micStatus.textContent = "";
      stopGlow();
      if (input.value.trim()) sendMessage(input.value);
    };
    micBtn.addEventListener("click", function () {
      if (micBtn.classList.contains("has-text")) { sendMessage(input.value); return; }
      if (listening) { recognition.stop(); } else { input.value = ""; recognition.start(); }
    });
  } else {
    micBtn.addEventListener("click", function () {
      if (micBtn.classList.contains("has-text")) { sendMessage(input.value); return; }
      micStatus.textContent = "Voice input is not supported in this browser.";
    });
  }
})();
"""
