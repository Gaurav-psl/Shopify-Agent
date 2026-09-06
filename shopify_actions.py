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
import repository_appwrite as repo

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
async def _resolve_variant(store, product_query: str, size: str = "", color: str = "") -> dict | None:
    if not product_query:
        return None

    products = []
    if getattr(store, "access_token", None):
        try:
            resp = await _get(store, "products.json", {"title": product_query, "status": "active", "limit": 5})
            if resp.status_code == 200:
                products = resp.json().get("products", [])
        except Exception:
            pass

    if not products:
        catalog = await _fetch_catalog_products(store, limit=25)
        pq = product_query.lower()
        products = [
            p for p in catalog
            if pq in (p.get("title") or "").lower() or pq in (p.get("handle") or "").lower() or pq in (p.get("product_type") or "").lower()
        ]

    if not products:
        return None

    product = products[0]
    variants = product.get("variants") or []
    if not variants:
        return None

    size_clean = (size or "").strip().lower()
    color_clean = (color or "").strip().lower()

    selected_variant = None
    if size_clean or color_clean:
        for v in variants:
            opts = " ".join(str(o) for o in [v.get("option1"), v.get("option2"), v.get("option3"), v.get("title")] if o).lower()
            if size_clean and size_clean not in opts:
                continue
            if color_clean and color_clean not in opts:
                continue
            selected_variant = v
            break

    # If no specific variant matched or requested, pick the first in-stock variant
    if not selected_variant:
        for v in variants:
            if v.get("available") is not False:
                selected_variant = v
                break
    if not selected_variant:
        selected_variant = variants[0]

    v_title = (selected_variant.get("title") or "").strip()
    name = product.get("title", "")
    if v_title and v_title.lower() not in ("default title", "default"):
        name = f"{name} ({v_title})"

    return {
        "product_id": product.get("id"),
        "variant_id": selected_variant.get("id"),
        "name": name,
        "price": selected_variant.get("price"),
        "image": (product.get("image") or {}).get("src", ""),
        "available": selected_variant.get("available") is not False,
    }


async def add_item(store, entities: dict) -> dict:
    query = entities.get("product_name_or_id") or entities.get("product_name") or entities.get("query") or ""
    size = entities.get("size") or entities.get("variant") or ""
    color = entities.get("color") or ""
    match = await _resolve_variant(store, query, size=size, color=color)
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


def _parse_float(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = str(val).replace("$", "").replace("€", "").replace("£", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _format_product(store, p: dict, matched_variant: dict | None = None) -> dict | None:
    variants = p.get("variants") or []
    if not variants:
        return None
    variant = matched_variant if matched_variant else variants[0]

    # Check variant availability
    is_available = variant.get("available")
    if is_available is None:
        inv_qty = variant.get("inventory_quantity")
        if inv_qty is not None:
            is_available = inv_qty > 0 or variant.get("inventory_management") is None or variant.get("inventory_policy") == "continue"
        else:
            is_available = True

    handle = p.get("handle", "")
    url = f"/products/{handle}" if handle else ""
    image_src = ""
    # Try variant-specific image if available
    var_img_id = variant.get("image_id")
    if var_img_id and p.get("images"):
        for img in p.get("images", []):
            if isinstance(img, dict) and img.get("id") == var_img_id:
                image_src = img.get("src", "")
                break
    if not image_src:
        if p.get("image") and isinstance(p["image"], dict):
            image_src = p["image"].get("src", "")
        elif p.get("images") and isinstance(p["images"], list) and len(p["images"]) > 0:
            first_img = p["images"][0]
            image_src = first_img.get("src", "") if isinstance(first_img, dict) else str(first_img)

    raw_price = variant.get("price", 0)
    parsed_price = _parse_float(raw_price)

    title = p.get("title", "Unnamed product")
    v_title = (variant.get("title") or "").strip()
    if v_title and v_title.lower() not in ("default title", "default"):
        title = f"{title} ({v_title})"

    return {
        "id": str(variant.get("id")),
        "product_id": str(p.get("id")),
        "name": title,
        "price": parsed_price if parsed_price is not None else 0.0,
        "image": image_src,
        "url": url,
        "available": bool(is_available),
    }


async def _fetch_shopify_recommendations(store, product_id: str | int, intent: str = "related", limit: int = 4) -> list[dict]:
    """Calls Shopify's native recommendation endpoint:
    https://{shop_domain}/recommendations/products.json?product_id={id}&intent={intent}&limit={limit}
    This endpoint uses Shopify's machine-learning model trained on real buyer co-purchases."""
    url = f"https://{store.shop_domain}/recommendations/products.json"
    params = {"product_id": str(product_id), "intent": intent, "limit": limit}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                products = resp.json().get("products", [])
                formatted = [_format_product(store, p) for p in products]
                return [f for f in formatted if f and f.get("available") is not False]
    except Exception as e:
        print(f"shopify_actions: recommendations.products.json error: {e}")
    return []


async def _fetch_catalog_products(store, limit: int = 25) -> list[dict]:
    """Fetches active products. First tries Shopify Admin API; if that fails (e.g. 
    token issue, scope issue, or 301/401 redirect), seamlessly falls back to the store's
    public storefront /products.json which requires zero auth and always succeeds."""
    if getattr(store, "access_token", None):
        try:
            resp = await _get(store, "products.json", {"status": "active", "limit": limit})
            if resp.status_code == 200:
                products = resp.json().get("products", [])
                if products:
                    return products
        except Exception as e:
            print(f"shopify_actions: admin products.json error: {e}")

    domains = []
    shop_domain = getattr(store, "shop_domain", "") or ""
    if shop_domain:
        domains.append(shop_domain)
        if "nhtcnc-hs" in shop_domain or "dripire" in shop_domain:
            domains.extend(["dripire.com", "dripire-3.myshopify.com"])
    else:
        domains.extend(["dripire.com", "nhtcnc-hs.myshopify.com"])

    for dom in domains:
        try:
            url = f"https://{dom}/products.json?limit={limit}"
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    products = r.json().get("products", [])
                    if products:
                        return products
        except Exception as e:
            print(f"shopify_actions: public products.json for {dom} error: {e}")

    return []


# ==========================================================================
# recommendations
# ==========================================================================
async def recommend_products(store, entities: dict) -> dict:
    rec_type = entities.get("recommendation_type", "general")
    target_product = entities.get("target_product") or entities.get("query")
    price_max = entities.get("price_max")
    category = (entities.get("category") or "").lower()

    recommendations = []

    # 1. If a specific anchor product is provided, try native Shopify recommendations first
    if target_product:
        variant = await _resolve_variant(store, target_product)
        if variant and variant.get("product_id"):
            recommendations = await _fetch_shopify_recommendations(store, variant["product_id"], intent="related", limit=4)

    # 2. If no target product or native endpoint returned empty, query catalog for bestsellers/trending/category
    if not recommendations:
        products = await _fetch_catalog_products(store, limit=25)
        for p in products:
            # Filter by category/tags if specified
            if category:
                ptype = (p.get("product_type") or "").lower()
                ptags = (p.get("tags") or "").lower()
                ptitle = (p.get("title") or "").lower()
                if category not in ptype and category not in ptags and category not in ptitle:
                    continue

            formatted = _format_product(store, p)
            if not formatted:
                continue

            # Filter out out-of-stock items from recommendations
            if not formatted.get("available"):
                continue

            parsed_price_max = _parse_float(price_max)
            if parsed_price_max is not None and formatted["price"] > parsed_price_max:
                continue

            # Don't recommend the exact product they are asking about
            if target_product and target_product.lower() in formatted["name"].lower():
                continue

            recommendations.append(formatted)
            if len(recommendations) >= 4:
                break

    return {
        "recommendation_type": rec_type,
        "recommendations": recommendations,
        "results": recommendations,  # for widget card rendering compatibility
        "count": len(recommendations),
    }


# ==========================================================================
# product_search
# ==========================================================================
async def search_products(store, entities: dict) -> dict:
    query = entities.get("query") or entities.get("category")
    price_min = _parse_float(entities.get("price_min"))
    price_max = _parse_float(entities.get("price_max"))
    color = (entities.get("color") or "").lower()
    size = (entities.get("size") or "").lower()

    products = []
    if getattr(store, "access_token", None):
        params = {"status": "active", "limit": 15}
        if query:
            params["title"] = query
        try:
            resp = await _get(store, "products.json", params)
            if resp.status_code == 200:
                products = resp.json().get("products", [])
        except Exception:
            pass

    if not products:
        products = await _fetch_catalog_products(store, limit=25)
        if query:
            q = query.lower()
            products = [
                p for p in products
                if q in (p.get("title") or "").lower() or q in (p.get("product_type") or "").lower() or q in (p.get("tags") or "").lower()
            ]

    results = []
    for p in products:
        matched_variant = None
        for variant in p.get("variants", [{}]):
            price = _parse_float(variant.get("price", 0)) or 0.0
            if price_min is not None and price < price_min:
                continue
            if price_max is not None and price > price_max:
                continue
            opts = " ".join(str(v) for v in [variant.get("option1"), variant.get("option2"), variant.get("option3"), variant.get("title")] if v).lower()
            if color and color not in opts:
                continue
            if size and size not in opts:
                continue
            # Prioritize in-stock variant
            if variant.get("available") is False:
                continue
            matched_variant = variant
            break

        # Fallback to in-stock variant if no specific variant matched but general query matched
        if not matched_variant and not color and not size and p.get("variants"):
            for v in p["variants"]:
                if v.get("available") is not False:
                    matched_variant = v
                    break
            if not matched_variant:
                matched_variant = p["variants"][0]

        if matched_variant:
            formatted = _format_product(store, p, matched_variant=matched_variant)
            if formatted:
                results.append(formatted)
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
    shop_domain = (getattr(store, "shop_domain", "") or "").lower()
    is_dripire = "dripire" in shop_domain

    # 1. Check merchant custom FAQs from Appwrite first
    try:
        store_id = getattr(store, "id", "") or (store.get("$id", "") if hasattr(store, "get") else "")
        if store_id:
            faqs = repo.list_faqs(store_id)
            user_q = (entities.get("question") or entities.get("query") or entities.get("policy_type") or "").lower()
            for faq in faqs:
                q = (faq.get("question") or "").lower()
                q_words = [w for w in re.findall(r"\w+", q) if len(w) > 3]
                if q_words and any(w in user_q for w in q_words):
                    return {
                        "policy_type": "custom_faq",
                        "title": faq.get("question"),
                        "body": faq.get("answer"),
                    }
    except Exception as e:
        print(f"shopify_actions: custom FAQ lookup error: {e!r}")

    # 2. Query Shopify Admin policies
    try:
        resp = await _get(store, "policies.json")
        if resp.status_code == 200:
            policies = resp.json().get("policies", [])
            field = _POLICY_FIELD_MAP.get(policy_type)
            for p in policies:
                # Shopify returns policies keyed by e.g. "title": "Refund Policy"
                title = (p.get("title") or "").lower().replace(" ", "_")
                if field and (field.replace("_policy", "") in title or field in title):
                    return {"policy_type": policy_type, "title": p.get("title"), "body": p.get("body"), "url": p.get("url")}
    except Exception as e:
        print(f"shopify_actions: policies.json request error: {e!r}")

    # 3. Brand-specific fallback for Dripire returns / exchanges
    if is_dripire and policy_type in ("refund_policy", "return_policy"):
        return {
            "policy_type": policy_type,
            "title": "Return & Exchange Policy",
            "body": (
                "DRIPIRE offers a 7-day hassle-free return and size exchange policy from the date of delivery. "
                "Items must be unused, unwashed, and in their original packaging with all tags attached. "
                "To start a return or size exchange, contact our support team at support@dripire.com with your order number."
            ),
            "url": "https://dripire.com/policies/refund-policy",
        }

    if policy_type == "warranty_policy":
        return {"policy_type": policy_type, "not_found": True, "note": "This store has not published a separate warranty policy."}

    return {"policy_type": policy_type, "not_found": True}


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
    "recommendations.recommend_products": recommend_products,
    "policy_query.answer_policy_question": answer_policy_question,
    "fallback.clarify": clarify,
}


async def dispatch(intent: str, action: str, store, entities: dict) -> dict:
    key = f"{intent}.{action}"
    fn = ACTION_MAP.get(key)
    if fn is None:
        return {"error": f"No handler registered for {key}"}
    return await fn(store, entities)
