"""
Shopify data layer — one function per action defined in intent_schema.json.

Every function now calls the REAL Shopify Admin API using the access
token stored for that store during OAuth (models.Store.access_token —
see shopify_auth.py). Functions take a `Store` ORM object (not just a
shop string) so they always have the token, and return a plain dict —
that dict becomes the "source of truth" data reply_generator.py turns
into a natural-language reply in the shopper's own language.

Two kinds of actions need special handling, because a shopper's cart
is a *browser-side, cookie-based* concept — the Admin API cannot add
to "a shopper's cart" on the server. So cart actions return a small
`widget_action` instruction instead of doing the mutation themselves.
chatbot_widget.py strips this out of the data before it reaches the
LLM reply generator and sends it to the widget separately; widget.js
then performs the actual `fetch('/cart/add.js', ...)` call itself,
from the shopper's own browser — which works because the widget
script is embedded on the store's own domain, so it's same-origin
with the store's cart.

SHOPIFY_API_VERSION: bump this as Shopify deprecates old ones.
"""

import os
import re
import httpx

SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2024-10")


def _headers(store) -> dict:
    return {"X-Shopify-Access-Token": store.access_token, "Content-Type": "application/json"}


def _url(store, path: str) -> str:
    return f"https://{store.shop_domain}/admin/api/{SHOPIFY_API_VERSION}/{path}"


def _extract_order_number(entities: dict, message: str = "") -> str | None:
    for key in ("order_number", "order_id"):
        val = entities.get(key)
        if val:
            return str(val).lstrip("#").strip()
    match = re.search(r"#?\s*(\d{3,})", message or "")
    return match.group(1) if match else None


async def _get(store, path: str, params: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15) as client:
        return await client.get(_url(store, path), headers=_headers(store), params=params or {})


async def _put(store, path: str, json: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15) as client:
        return await client.put(_url(store, path), headers=_headers(store), json=json)


# ==========================================================================
# registration_login — shoppers use the store's own native account pages
# (Shopify handles customer auth itself; there is no Admin API endpoint
# for "log a shopper in"). We just point the widget at the right page.
# ==========================================================================
def _account_redirect(store, page: str, message: str) -> dict:
    return {
        "status": "redirect",
        "message": message,
        "widget_action": {"type": "redirect", "url": f"https://{store.shop_domain}/account/{page}"},
    }


async def register(store, entities: dict) -> dict:
    return _account_redirect(store, "register", "Taking you to the account creation page.")


async def login(store, entities: dict) -> dict:
    return _account_redirect(store, "login", "Taking you to the sign-in page.")


async def logout(store, entities: dict) -> dict:
    return _account_redirect(store, "logout", "Signing you out.")


async def forgot_password(store, entities: dict) -> dict:
    return _account_redirect(store, "login#recover", "Taking you to the password recovery page.")


# ==========================================================================
# order_tracking
# ==========================================================================
async def track_order(store, entities: dict) -> dict:
    order_number = _extract_order_number(entities)
    if not order_number:
        return {"error": "missing_order_number", "message": "No order number was given."}

    resp = await _get(store, "orders.json", {"name": f"#{order_number}", "status": "any"})
    if resp.status_code != 200:
        return {"error": "lookup_failed", "message": "Could not reach Shopify to look up this order."}

    orders = resp.json().get("orders", [])
    if not orders:
        return {"error": "not_found", "order_number": order_number}

    order = orders[0]
    tracking_number, tracking_url = None, None
    for f in order.get("fulfillments", []):
        if f.get("tracking_number"):
            tracking_number = f["tracking_number"]
            tracking_url = f.get("tracking_url")
            break

    return {
        "order_number": order_number,
        "fulfillment_status": order.get("fulfillment_status") or "unfulfilled",
        "financial_status": order.get("financial_status", "unknown"),
        "tracking_number": tracking_number,
        "tracking_url": tracking_url,
    }


async def list_recent_orders(store, entities: dict) -> dict:
    params = {"status": "any", "limit": 5, "order": "created_at desc"}
    if entities.get("email"):
        params["email"] = entities["email"]

    resp = await _get(store, "orders.json", params)
    if resp.status_code != 200:
        return {"error": "lookup_failed", "message": "Could not reach Shopify to look up orders."}

    orders = resp.json().get("orders", [])
    return {
        "orders": [
            {
                "order_number": o.get("name", "").lstrip("#"),
                "status": o.get("fulfillment_status") or "unfulfilled",
                "total": o.get("total_price"),
                "currency": o.get("currency"),
            }
            for o in orders
        ]
    }


# ==========================================================================
# cart_management — resolve the product server-side (Admin API), then
# hand the *action* off to the browser, which owns the real cart.
# ==========================================================================
async def _resolve_variant(store, product_query: str) -> dict | None:
    if not product_query:
        return None
    resp = await _get(store, "products.json", {"title": product_query, "status": "active", "limit": 5})
    products = resp.json().get("products", []) if resp.status_code == 200 else []

    # If exact title lookup returned no match, search across active products
    if not products:
        all_resp = await _get(store, "products.json", {"status": "active", "limit": 50})
        if all_resp.status_code == 200:
            q_lower = product_query.lower().strip()
            for p in all_resp.json().get("products", []):
                p_text = f"{p.get('title', '')} {p.get('product_type', '')} {p.get('tags', '')}".lower()
                if q_lower in p_text or any(w in p_text for w in q_lower.split() if len(w) > 2):
                    products.append(p)
                    break

    if not products:
        return None
    product = products[0]
    variant = (product.get("variants") or [{}])[0]
    return {
        "product_id": product.get("id"),
        "variant_id": variant.get("id"),
        "name": product.get("title"),
        "price": variant.get("price"),
        "image": (product.get("image") or {}).get("src", ""),
        "url": f"https://{store.shop_domain}/products/{product.get('handle', '')}",
    }


async def add_item(store, entities: dict) -> dict:
    query = entities.get("product_name_or_id", "")
    match = await _resolve_variant(store, query)
    if not match:
        return {"error": "not_found", "query": query}

    quantity = int(entities.get("quantity") or 1)
    return {
        "added": match["name"],
        "quantity": quantity,
        "widget_action": {"type": "cart_add", "variant_id": match["variant_id"], "quantity": quantity},
    }


async def remove_item(store, entities: dict) -> dict:
    query = entities.get("product_name_or_id", "")
    return {
        "removed": query,
        "widget_action": {"type": "cart_remove", "product_name": query},
    }


async def edit_quantity(store, entities: dict) -> dict:
    query = entities.get("product_name_or_id", "")
    quantity = int(entities.get("quantity") or 1)
    return {
        "item": query,
        "new_quantity": quantity,
        "widget_action": {"type": "cart_set_quantity", "product_name": query, "quantity": quantity},
    }


async def view_cart(store, entities: dict) -> dict:
    # The widget fetches /cart.js itself (same-origin, has the real cart
    # cookie) and renders the summary — the backend can't see it.
    return {"widget_action": {"type": "cart_view"}}


async def clear_cart(store, entities: dict) -> dict:
    return {"widget_action": {"type": "cart_clear"}}


# ==========================================================================
# warranty_claim — no universal Shopify "warranty" object, so we record
# it as an order tag + note that shows up for the merchant in Admin.
# Swap this for a real helpdesk (Gorgias/Zendesk) API call if you have one.
# ==========================================================================
async def submit_claim(store, entities: dict) -> dict:
    order_number = _extract_order_number(entities)
    issue = entities.get("issue_description", "Not specified")
    if not order_number:
        return {"error": "missing_order_number"}

    resp = await _get(store, "orders.json", {"name": f"#{order_number}", "status": "any"})
    if resp.status_code != 200 or not resp.json().get("orders"):
        return {"error": "not_found", "order_number": order_number}

    order = resp.json()["orders"][0]
    existing_tags = order.get("tags", "")
    new_tags = ", ".join(filter(None, [existing_tags, "warranty-claim"]))
    existing_note = order.get("note") or ""
    new_note = (existing_note + f"\n[Warranty claim] {issue}").strip()

    await _put(store, f"orders/{order['id']}.json", {"order": {"id": order["id"], "tags": new_tags, "note": new_note}})

    return {"status": "submitted", "order_number": order_number, "issue": issue}


async def check_claim_status(store, entities: dict) -> dict:
    order_number = _extract_order_number(entities)
    if not order_number:
        return {"error": "missing_order_number"}

    resp = await _get(store, "orders.json", {"name": f"#{order_number}", "status": "any"})
    if resp.status_code != 200 or not resp.json().get("orders"):
        return {"error": "not_found", "order_number": order_number}

    order = resp.json()["orders"][0]
    tags = order.get("tags", "")
    if "warranty-claim" in tags:
        return {"order_number": order_number, "status": "under review", "note": order.get("note", "")}
    return {"order_number": order_number, "status": "no claim on file for this order"}


# ==========================================================================
# product_search
# ==========================================================================
async def search_products(store, entities: dict) -> dict:
    params = {"status": "active", "limit": 10}
    query = (entities.get("query") or entities.get("category") or "").strip()
    if query:
        params["title"] = query

    resp = await _get(store, "products.json", params)
    if resp.status_code != 200:
        return {"error": "lookup_failed"}

    products = resp.json().get("products", [])

    # If title search returned nothing, fall back to broader active products search
    if not products and query:
        all_resp = await _get(store, "products.json", {"status": "active", "limit": 50})
        if all_resp.status_code == 200:
            q_lower = query.lower()
            q_words = [w for w in q_lower.split() if len(w) > 2]
            for p in all_resp.json().get("products", []):
                p_text = f"{p.get('title', '')} {p.get('product_type', '')} {p.get('tags', '')} {p.get('body_html', '')}".lower()
                if q_lower in p_text or (q_words and any(w in p_text for w in q_words)):
                    products.append(p)

    price_min = entities.get("price_min")
    price_max = entities.get("price_max")
    color = (entities.get("color") or "").lower()
    size = (entities.get("size") or "").lower()

    results = []
    for p in products:
        for variant in p.get("variants", [{}]):
            price = float(variant.get("price", 0) or 0)
            if price_min is not None and price < float(price_min):
                continue
            if price_max is not None and price > float(price_max):
                continue
            opts = " ".join(str(v) for v in [variant.get("option1"), variant.get("option2"), variant.get("option3")] if v).lower()
            if color and color not in opts:
                continue
            if size and size not in opts:
                continue
            results.append({
                "id": str(variant.get("id")),
                "product_id": str(p.get("id")),
                "name": p.get("title", "Unnamed product"),
                "price": price,
                "image": (p.get("image") or {}).get("src", ""),
                "url": f"https://{store.shop_domain}/products/{p.get('handle', '')}",
            })
            break
        if len(results) >= 6:
            break

    return {"results": results, "filters_applied": entities}


# ==========================================================================
# policy_query — Shopify's real store policies (Admin API), not mocks.
# ==========================================================================
_POLICY_FIELD_MAP = {
    "refund_policy": "refund_policy",
    "shipping_policy": "shipping_policy",
    "privacy_policy": "privacy_policy",
    "terms_of_service": "terms_of_service",
}


async def answer_policy_question(store, entities: dict) -> dict:
    policy_type = entities.get("policy_type", "refund_policy")

    resp = await _get(store, "policies.json")
    if resp.status_code != 200:
        return {"error": "lookup_failed", "policy_type": policy_type}

    policies = resp.json().get("policies", [])
    field = _POLICY_FIELD_MAP.get(policy_type)
    for p in policies:
        # Shopify returns policies keyed by e.g. "title": "Refund Policy"
        title = (p.get("title") or "").lower().replace(" ", "_")
        if field and (field.replace("_policy", "") in title or field in title):
            return {"policy_type": policy_type, "title": p.get("title"), "body": p.get("body"), "url": p.get("url")}

    # If asking for warranty policy, check if refund policy or terms mention warranty
    if policy_type == "warranty_policy":
        for p in policies:
            body = (p.get("body") or "").lower()
            if "warranty" in body or "guarantee" in body:
                return {
                    "policy_type": policy_type,
                    "title": f"Warranty section in {p.get('title')}",
                    "body": p.get("body"),
                    "url": p.get("url"),
                }
        return {"policy_type": policy_type, "not_found": True, "note": "This store has not published a separate warranty policy."}

    return {"policy_type": policy_type, "not_found": True}


# ==========================================================================
# smalltalk
# ==========================================================================
async def greet(store, entities: dict) -> dict:
    return {"status": "greet", "message": "Hello! Welcome to our store. How can I assist you today?"}


async def thank_you(store, entities: dict) -> dict:
    return {"status": "thank_you", "message": "You're very welcome! Let me know if you need any further help."}


async def say_goodbye(store, entities: dict) -> dict:
    return {"status": "say_goodbye", "message": "Goodbye! Have a great day and happy shopping."}


# ==========================================================================
# fallback
# ==========================================================================
async def clarify(store, entities: dict) -> dict:
    return {"message": "Could not confidently match this to a supported action."}


# Maps "intent.action" -> async function, built directly from the names
# used in intent_schema.json so the router and the schema can never drift.
ACTION_MAP = {
    "registration_login.register": register,
    "registration_login.login": login,
    "registration_login.logout": logout,
    "registration_login.forgot_password": forgot_password,
    "order_tracking.track_order": track_order,
    "order_tracking.list_recent_orders": list_recent_orders,
    "cart_management.add_item": add_item,
    "cart_management.remove_item": remove_item,
    "cart_management.edit_quantity": edit_quantity,
    "cart_management.view_cart": view_cart,
    "cart_management.clear_cart": clear_cart,
    "warranty_claim.submit_claim": submit_claim,
    "warranty_claim.check_claim_status": check_claim_status,
    "product_search.search_products": search_products,
    "policy_query.answer_policy_question": answer_policy_question,
    "smalltalk.greet": greet,
    "smalltalk.thank_you": thank_you,
    "smalltalk.say_goodbye": say_goodbye,
    "fallback.clarify": clarify,
}


async def dispatch(intent: str, action: str, store, entities: dict) -> dict:
    key = f"{intent}.{action}"
    fn = ACTION_MAP.get(key)
    if fn is None:
        return {"error": f"No handler registered for {key}"}
    return await fn(store, entities)
