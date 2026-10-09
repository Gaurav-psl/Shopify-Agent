"""
Shopify data layer — one function per action defined in intent_schema.json.

Every function calls the REAL Shopify Admin API using the access token
stored for that store during OAuth (models.Store.access_token — see
shopify_auth.py). Functions take a `Store` ORM object (not just a shop
string) so they always have the token, and return a plain dict — that
dict becomes the "source of truth" data reply_generator.py turns into
a natural-language reply in the shopper's own language.

Cart actions run entirely server-side via Shopify's Storefront API
(cartCreate / cartLinesAdd / cartLinesUpdate / cartLinesRemove), using a
per-store Storefront API access token fetched (or created, on first use)
through the Admin API — not the shopper's browser. This is a deliberate
architecture choice with one real tradeoff: a Storefront API cart is a
*separate* cart object from the storefront theme's own cookie-based
cart. It has its own `checkoutUrl`, and it will not appear if a shopper
independently opens the theme's native cart drawer/page — the widget's
own cart badge and "what's in my cart" replies are the only place this
cart is visible, and checkout must go through the `checkout_url` this
module returns rather than the store's normal /cart or /checkout URLs.

Each store's Storefront cart is tracked per chat session (in memory,
session_id -> cart GID — see _SESSION_CARTS below), not per shopper
account, since the widget has no concept of a logged-in customer.

Order-related lookups (track_order, list_recent_orders, submit_claim,
check_claim_status) require the shopper to confirm the email address
on file for that order before any details are returned or any claim is
filed/checked. Without this, an order number alone — something a
shopper could guess, reuse from their own past order, or see on a
packing slip — would let them pull up *any* customer's order status
and tracking info. If no email is given, or it doesn't match Shopify's
own record for that order, these functions return a
"verification_required"/"verification_failed" error dict instead of
data; chatbot_widget.py's PENDING mechanism (mode="verify_email")
keeps the original request alive across that follow-up turn, so the
shopper only needs to reply with an email rather than repeat the whole
request.

track_order also reports a live shipment location/status (current
city, last scan message, estimated delivery) on top of Shopify's own
fulfillment data, via tracking_provider.py (AfterShip). Shopify itself
only ever stores a tracking number and a link to the carrier's site —
it has no idea where a package actually is. This is additive and
optional: if AFTERSHIP_API_KEY isn't configured, track_order still
works exactly as before, just without the live-location fields.

SHOPIFY_API_VERSION: bump this as Shopify deprecates old ones.
"""

import os
import re
import httpx

import tracking_provider

SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-10")


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
# Authenticated customer data (GraphQL Admin API)
# ==========================================================================
async def _graphql_admin(store, query: str, variables: dict | None = None) -> dict | None:
    url = f"https://{store.shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, headers=_headers(store), json={"query": query, "variables": variables or {}})
    except Exception as e:
        print(f"shopify_actions: GraphQL request failed: {e!r}")
        return None
    if resp.status_code != 200:
        print(f"shopify_actions: GraphQL HTTP {resp.status_code}: {resp.text[:500]}")
        return None
    body=resp.json()
    if body.get("errors"):
        print(f"shopify_actions: GraphQL errors: {body['errors']}")
        return None
    return body.get("data")


async def get_customer(store, customer_id: str) -> dict | None:
    query="""
    query Customer($id: ID!) {
      customer(id: $id) {
        id firstName lastName
        defaultEmailAddress { emailAddress }
        numberOfOrders
      }
    }
    """
    data=await _graphql_admin(store,query,{"id":f"gid://shopify/Customer/{customer_id}"})
    return (data or {}).get("customer")


async def get_customer_orders(store, customer_id: str, first: int=50) -> dict:
    query="""
    query CustomerOrders($first: Int!, $query: String!) {
      orders(first: $first, query: $query, sortKey: CREATED_AT, reverse: true) {
        nodes {
          id name createdAt cancelledAt displayFinancialStatus displayFulfillmentStatus
          totalPriceSet { shopMoney { amount currencyCode } }
          lineItems(first: 20) { nodes { name quantity product { id } variant { id title } } }
          fulfillments { trackingInfo { number url company } }
        }
      }
    }
    """
    data=await _graphql_admin(store,query,{"first":min(max(first,1),100),"query":f"customer_id:{customer_id}"})
    if data is None: return {"error":"lookup_failed","message":"Could not load your orders from the store right now."}
    nodes=((data or {}).get("orders") or {}).get("nodes") or []
    active=[]; past=[]; purchased=[]
    for order in nodes:
        items=[]
        for line in (order.get("lineItems") or {}).get("nodes") or []:
            variant=line.get("variant") or {}
            product=line.get("product") or {}
            if product.get("id"): purchased.append(str(product["id"]).split("/")[-1])
            items.append({"name":line.get("name"),"quantity":line.get("quantity"),"variant":variant.get("title")})
        total=(order.get("totalPriceSet") or {}).get("shopMoney") or {}
        fulfillment=(order.get("displayFulfillmentStatus") or "").lower()
        if order.get("cancelledAt"): fulfillment="cancelled"
        entry={
            "id":order.get("id"),"order_number":str(order.get("name") or "").lstrip("#"),
            "date":(order.get("createdAt") or "")[:10],"status":fulfillment.replace("_"," ") or "unknown",
            "financial_status":order.get("displayFinancialStatus"),"total":total.get("amount"),
            "currency":total.get("currencyCode"),"items":items,
            "tracking":[t for f in (order.get("fulfillments") or []) for t in (f.get("trackingInfo") or []) if t]
        }
        if fulfillment in {"fulfilled","shipped","delivered","cancelled","restocked"}: past.append(entry)
        else: active.append(entry)
    return {"active_orders":active,"past_orders":past,"purchased_product_ids":list(dict.fromkeys(purchased))}


async def get_active_products_graphql(store, first: int=100) -> list[dict]:
    query="""
    query Products($first: Int!) {
      products(first: $first, query: "status:ACTIVE") {
        nodes { id title handle productType tags featuredImage { url } variants(first: 10) { nodes { id title price } } }
      }
    }
    """
    data=await _graphql_admin(store,query,{"first":min(max(first,1),250)})
    nodes=((data or {}).get("products") or {}).get("nodes") or []
    return [{
        "id":str(p.get("id") or "").split("/")[-1],"title":p.get("title"),"handle":p.get("handle"),
        "product_type":p.get("productType"),"tags":p.get("tags") or [],"image":(p.get("featuredImage") or {}).get("url",""),
        "variants":[{"id":str(v.get("id") or "").split("/")[-1],"title":v.get("title"),"price":v.get("price")} for v in ((p.get("variants") or {}).get("nodes") or [])]
    } for p in nodes]


async def get_my_orders(store, entities: dict, session_id: str, customer_id: str | None=None) -> dict:
    if not customer_id: return {"error":"authentication_required"}
    return await get_customer_orders(store,customer_id,first=50)


async def get_recommendations(store, entities: dict, session_id: str, customer_id: str | None=None) -> dict:
    import customer_profiles, recommendation_engine
    profile=(await customer_profiles.get_profile(store.shop_domain,customer_id) or {}) if customer_id else {}
    if customer_id and not profile.get("purchased_product_ids"):
        # Stored profile missing/empty (first visit, DB not configured, sync not run yet): read the orders live.
        live=await get_customer_orders(store,customer_id,first=50)
        if not live.get("error"):
            names=[i.get("name") for o in (live.get("active_orders") or [])+(live.get("past_orders") or []) for i in (o.get("items") or []) if i.get("name")]
            profile={**profile,"purchased_product_ids":live.get("purchased_product_ids") or [],"top_types":list(dict.fromkeys(names))[:20]}
    result=await recommendation_engine.recommend_products(store,profile,limit=6)
    result["based_on"]={"previous_orders":bool(profile.get("purchased_product_ids")),"recent_searches":(profile.get("recent_searches") or [])[:3]}
    if not customer_id: result["note"]="The shopper is not logged in, so these are general picks. Suggest logging in for picks based on their orders and searches."
    return result

# ==========================================================================
# Storefront API — cart engine
#
# In-memory caches. Both are cheap to lose on a restart: a missing
# Storefront token just gets re-fetched/re-created on the next cart
# action; a missing session->cart mapping just means that session starts
# a fresh, empty cart rather than erroring. Same volatility tradeoff
# already accepted for chatbot_widget.py's PENDING confirmation dict.
# For production durability across restarts, consider persisting both
# to Appwrite instead (mirrors how rag_retriever.py flags its own
# STORE_DATASET_MAP as a stand-in for a real Appwrite-backed mapping).
# ==========================================================================

_STOREFRONT_TOKENS: dict[str, str] = {}   # shop_domain -> storefront access token
_SESSION_CARTS: dict[str, str] = {}       # session_id  -> Storefront API cart GID
_STOREFRONT_TOKEN_TITLE = "AI Shopping Assistant"

_CART_FIELDS = """
    id
    checkoutUrl
    totalQuantity
    cost { totalAmount { amount currencyCode } }
    lines(first: 50) {
      edges {
        node {
          id
          quantity
          merchandise {
            ... on ProductVariant {
              id
              title
              product { title }
              price { amount currencyCode }
            }
          }
        }
      }
    }
"""


async def _get_storefront_token(store) -> str | None:
    """Fetches an existing Storefront API access token for this store via
    the Admin API, or creates one if none exists yet. Cached in memory
    per shop after the first lookup."""
    cached = _STOREFRONT_TOKENS.get(store.shop_domain)
    if cached:
        return cached

    resp = await _get(store, "storefront_access_tokens.json")
    if resp.status_code == 200:
        for tok in resp.json().get("storefront_access_tokens", []):
            token = tok.get("access_token")
            if token:
                _STOREFRONT_TOKENS[store.shop_domain] = token
                return token

    async with httpx.AsyncClient(timeout=15) as client:
        create_resp = await client.post(
            _url(store, "storefront_access_tokens.json"),
            headers=_headers(store),
            json={"storefront_access_token": {"title": _STOREFRONT_TOKEN_TITLE}},
        )
    if create_resp.status_code not in (200, 201):
        return None

    token = create_resp.json().get("storefront_access_token", {}).get("access_token")
    if token:
        _STOREFRONT_TOKENS[store.shop_domain] = token
    return token


def _storefront_url(store) -> str:
    return f"https://{store.shop_domain}/api/{SHOPIFY_API_VERSION}/graphql.json"


async def _storefront_query(store, query: str, variables: dict) -> dict | None:
    token = await _get_storefront_token(store)
    if not token:
        return None
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _storefront_url(store),
            headers={"X-Shopify-Storefront-Access-Token": token, "Content-Type": "application/json"},
            json={"query": query, "variables": variables},
        )
    if resp.status_code != 200:
        return None
    body = resp.json()
    if body.get("errors"):
        return None
    return body.get("data")


def _summarize_cart(cart: dict | None) -> dict:
    """Turns a Storefront API cart node into the plain-dict shape both
    reply_generator.py and widget.js consume."""
    if not cart:
        return {"items": [], "item_count": 0, "total": "0.00", "currency": None, "checkout_url": None}

    items = []
    for edge in cart.get("lines", {}).get("edges", []):
        line = edge["node"]
        merch = line.get("merchandise", {}) or {}
        price = merch.get("price", {}) or {}
        items.append({
            "line_id": line["id"],
            "name": (merch.get("product") or {}).get("title") or merch.get("title") or "Item",
            "variant": merch.get("title"),
            "quantity": line["quantity"],
            "price": price.get("amount"),
        })

    cost = (cart.get("cost") or {}).get("totalAmount") or {}
    return {
        "items": items,
        "item_count": cart.get("totalQuantity", 0),
        "total": cost.get("amount"),
        "currency": cost.get("currencyCode"),
        "checkout_url": cart.get("checkoutUrl"),
    }


def _find_line(cart_summary: dict, product_query: str) -> dict | None:
    needle = (product_query or "").strip().lower()
    if not needle:
        return None
    for item in cart_summary["items"]:
        if needle in item["name"].lower():
            return item
    return None


async def _fetch_cart(store, cart_id: str) -> dict | None:
    query = f"query getCart($id: ID!) {{ cart(id: $id) {{ {_CART_FIELDS} }} }}"
    data = await _storefront_query(store, query, {"id": cart_id})
    return (data or {}).get("cart")


async def _create_cart(store) -> dict | None:
    mutation = f"""
    mutation createCart {{
      cartCreate {{
        cart {{ {_CART_FIELDS} }}
        userErrors {{ field message }}
      }}
    }}
    """
    data = await _storefront_query(store, mutation, {})
    result = (data or {}).get("cartCreate") or {}
    if result.get("userErrors"):
        return None
    return result.get("cart")


async def _get_or_create_cart(store, session_id: str) -> dict | None:
    cart_id = _SESSION_CARTS.get(session_id)
    cart = await _fetch_cart(store, cart_id) if cart_id else None
    if cart is None:
        cart = await _create_cart(store)
        if cart:
            _SESSION_CARTS[session_id] = cart["id"]
    return cart


# ==========================================================================
# registration_login — shoppers use the store's own native account pages
# (Shopify handles customer auth itself; there is no Admin API endpoint
# for "log a shopper in"). We just point the widget at the right page —
# this is the one remaining case where a `widget_action` (a same-tab
# redirect) is still needed, since it's not something the server can do
# on the shopper's behalf.
# ==========================================================================
def _account_redirect(store, page: str, message: str) -> dict:
    return {
        "status": "redirect",
        "message": message,
        "widget_action": {"type": "redirect", "url": f"https://{store.shop_domain}/account/{page}"},
    }


async def register(store, entities: dict, session_id: str) -> dict:
    return _account_redirect(store, "register", "Taking you to the account creation page.")


async def login(store, entities: dict, session_id: str) -> dict:
    return _account_redirect(store, "login", "Taking you to the sign-in page.")


async def logout(store, entities: dict, session_id: str) -> dict:
    return _account_redirect(store, "logout", "Signing you out.")


async def forgot_password(store, entities: dict, session_id: str) -> dict:
    return _account_redirect(store, "login#recover", "Taking you to the password recovery page.")


# ==========================================================================
# order_tracking
# ==========================================================================
async def track_order(store, entities: dict, session_id: str) -> dict:
    order_number = _extract_order_number(entities)
    if not order_number:
        return {"error": "missing_order_number", "message": "No order number was given."}

    email = (entities.get("email") or "").strip().lower()
    if not email:
        return {
            "error": "verification_required",
            "order_number": order_number,
            "message": "For privacy, please confirm the email address used on this order before it can be looked up.",
        }

    resp = await _get(store, "orders.json", {"name": f"#{order_number}", "status": "any"})
    if resp.status_code != 200:
        print(f"track_order: orders.json -> {resp.status_code} {resp.text[:400]}")
        return {"error": "lookup_failed", "message": "Could not reach Shopify to look up this order."}

    orders = resp.json().get("orders", [])
    if not orders:
        print(f"track_order: no order found for name=#{order_number}")
        return {"error": "not_found", "order_number": order_number}

    order = orders[0]
    order_email = (order.get("email") or order.get("contact_email") or "").strip().lower()
    if not order_email or order_email != email:
        print(f"track_order: email mismatch for #{order_number} (order email present: {bool(order_email)})")
        return {
            "error": "verification_failed",
            "order_number": order_number,
            "message": "That email doesn't match our records for this order. Order details can only be shared with the email on file.",
        }

    tracking_number, tracking_url, native_shipment_status = None, None, None
    for f in order.get("fulfillments", []):
        if f.get("tracking_number"):
            tracking_number = f["tracking_number"]
            tracking_url = f.get("tracking_url")
            # Free, no extra API call: Shopify includes this on the same
            # fulfillment object for its own list of integrated carriers
            # (see the Shipping Carriers help page) — coarse status only
            # (in_transit/out_for_delivery/delivered/etc.), never a
            # physical location.
            native_shipment_status = f.get("shipment_status")
            break

    result = {
        "order_number": order_number,
        "fulfillment_status": order.get("fulfillment_status") or "unfulfilled",
        "financial_status": order.get("financial_status", "unknown"),
        "tracking_number": tracking_number,
        "tracking_url": tracking_url,
        "shipment_status": native_shipment_status,
        "current_location": None,
        "last_scan_message": None,
        "last_scan_time": None,
        "estimated_delivery": None,
    }

    # AfterShip is the primary source for a real physical location and a
    # genuine estimated-delivery date — Shopify's own API never provides
    # either. If AfterShip has usable data, it overrides the coarse
    # native status above with something more specific; if it has
    # nothing yet (not configured, carrier not recognized, or too soon
    # after the label was created), the native status above is already
    # in `result` as a free fallback rather than leaving the field empty.
    if tracking_number:
        live = await tracking_provider.get_live_status(tracking_number)
        if live and (live.get("location") or live.get("status") or live.get("estimated_delivery")):
            result["shipment_status"] = live.get("status") or native_shipment_status
            result["current_location"] = live.get("location")
            result["last_scan_message"] = live.get("message")
            result["last_scan_time"] = live.get("checkpoint_time")
            result["estimated_delivery"] = live.get("estimated_delivery")

    return result


async def list_recent_orders(store, entities: dict, session_id: str) -> dict:
    email = (entities.get("email") or "").strip().lower()
    if not email:
        return {
            "error": "verification_required",
            "message": "Please share the email address on your account so recent orders can be looked up.",
        }

    resp = await _get(store, "orders.json", {"status": "any", "limit": 5, "order": "created_at desc", "email": email})
    if resp.status_code != 200:
        print(f"track_order: orders.json -> {resp.status_code} {resp.text[:400]}")
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
# cart_management — resolved server-side end to end: the product lookup
# uses the Admin API (as before), and the cart mutation itself now uses
# the Storefront API cart tied to this chat session (see _get_or_create_cart
# above). Nothing here is visible in the shopper's browser network tab.
# ==========================================================================
async def _resolve_variant(store, product_query: str) -> dict | None:
    if not product_query:
        return None
    resp = await _get(store, "products.json", {"title": product_query, "status": "active", "limit": 5})
    if resp.status_code != 200:
        return None
    products = resp.json().get("products", [])
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
    }


async def add_item(store, entities: dict, session_id: str) -> dict:
    query = entities.get("product_name_or_id", "")
    match = await _resolve_variant(store, query)
    if not match or not match.get("variant_id"):
        return {"error": "not_found", "query": query}

    quantity = int(entities.get("quantity") or 1)
    cart = await _get_or_create_cart(store, session_id)
    if not cart:
        return {"error": "cart_unavailable", "query": query}

    mutation = f"""
    mutation addLine($cartId: ID!, $lines: [CartLineInput!]!) {{
      cartLinesAdd(cartId: $cartId, lines: $lines) {{
        cart {{ {_CART_FIELDS} }}
        userErrors {{ field message }}
      }}
    }}
    """
    data = await _storefront_query(store, mutation, {
        "cartId": cart["id"],
        "lines": [{"merchandiseId": f"gid://shopify/ProductVariant/{match['variant_id']}", "quantity": quantity}],
    })
    result = (data or {}).get("cartLinesAdd") or {}
    if result.get("userErrors"):
        return {"error": "cart_update_failed", "details": result["userErrors"], "query": query}

    return {"added": match["name"], "quantity": quantity, "cart": _summarize_cart(result.get("cart") or cart)}


async def remove_item(store, entities: dict, session_id: str) -> dict:
    query = entities.get("product_name_or_id", "")
    cart_id = _SESSION_CARTS.get(session_id)
    if not cart_id:
        return {"removed": None, "message": "Your cart is already empty."}

    cart = await _fetch_cart(store, cart_id)
    summary = _summarize_cart(cart)
    line = _find_line(summary, query)
    if not line:
        return {"error": "not_found", "query": query, "cart": summary}

    mutation = f"""
    mutation removeLine($cartId: ID!, $lineIds: [ID!]!) {{
      cartLinesRemove(cartId: $cartId, lineIds: $lineIds) {{
        cart {{ {_CART_FIELDS} }}
        userErrors {{ field message }}
      }}
    }}
    """
    data = await _storefront_query(store, mutation, {"cartId": cart_id, "lineIds": [line["line_id"]]})
    result = (data or {}).get("cartLinesRemove") or {}
    if result.get("userErrors"):
        return {"error": "cart_update_failed", "details": result["userErrors"], "query": query}

    return {"removed": line["name"], "cart": _summarize_cart(result.get("cart"))}


async def edit_quantity(store, entities: dict, session_id: str) -> dict:
    query = entities.get("product_name_or_id", "")
    quantity = int(entities.get("quantity") or 1)
    cart_id = _SESSION_CARTS.get(session_id)
    if not cart_id:
        return {"error": "not_found", "query": query}

    cart = await _fetch_cart(store, cart_id)
    summary = _summarize_cart(cart)
    line = _find_line(summary, query)
    if not line:
        return {"error": "not_found", "query": query, "cart": summary}

    mutation = f"""
    mutation updateLine($cartId: ID!, $lines: [CartLineUpdateInput!]!) {{
      cartLinesUpdate(cartId: $cartId, lines: $lines) {{
        cart {{ {_CART_FIELDS} }}
        userErrors {{ field message }}
      }}
    }}
    """
    data = await _storefront_query(store, mutation, {
        "cartId": cart_id,
        "lines": [{"id": line["line_id"], "quantity": quantity}],
    })
    result = (data or {}).get("cartLinesUpdate") or {}
    if result.get("userErrors"):
        return {"error": "cart_update_failed", "details": result["userErrors"], "query": query}

    return {"item": query, "new_quantity": quantity, "cart": _summarize_cart(result.get("cart"))}


async def view_cart(store, entities: dict, session_id: str) -> dict:
    cart_id = _SESSION_CARTS.get(session_id)
    if not cart_id:
        return {"cart": _summarize_cart(None)}
    cart = await _fetch_cart(store, cart_id)
    return {"cart": _summarize_cart(cart)}


async def clear_cart(store, entities: dict, session_id: str) -> dict:
    cart_id = _SESSION_CARTS.get(session_id)
    if not cart_id:
        return {"cart": _summarize_cart(None)}

    cart = await _fetch_cart(store, cart_id)
    summary = _summarize_cart(cart)
    line_ids = [item["line_id"] for item in summary["items"]]
    if not line_ids:
        return {"cart": summary}

    mutation = f"""
    mutation clearCart($cartId: ID!, $lineIds: [ID!]!) {{
      cartLinesRemove(cartId: $cartId, lineIds: $lineIds) {{
        cart {{ {_CART_FIELDS} }}
        userErrors {{ field message }}
      }}
    }}
    """
    data = await _storefront_query(store, mutation, {"cartId": cart_id, "lineIds": line_ids})
    result = (data or {}).get("cartLinesRemove") or {}
    if result.get("userErrors"):
        return {"error": "cart_update_failed", "details": result["userErrors"]}

    return {"cart": _summarize_cart(result.get("cart"))}


# ==========================================================================
# warranty_claim — no universal Shopify "warranty" object, so we record
# it as an order tag + note that shows up for the merchant in Admin.
# Swap this for a real helpdesk (Gorgias/Zendesk) API call if you have one.
# ==========================================================================
async def submit_claim(store, entities: dict, session_id: str) -> dict:
    order_number = _extract_order_number(entities)
    issue = entities.get("issue_description", "Not specified")
    if not order_number:
        return {"error": "missing_order_number"}

    email = (entities.get("email") or "").strip().lower()
    if not email:
        return {
            "error": "verification_required",
            "order_number": order_number,
            "message": "For privacy, please confirm the email address used on this order before filing a claim.",
        }

    resp = await _get(store, "orders.json", {"name": f"#{order_number}", "status": "any"})
    if resp.status_code != 200 or not resp.json().get("orders"):
        return {"error": "not_found", "order_number": order_number}

    order = resp.json()["orders"][0]
    order_email = (order.get("email") or order.get("contact_email") or "").strip().lower()
    if not order_email or order_email != email:
        return {
            "error": "verification_failed",
            "order_number": order_number,
            "message": "That email doesn't match our records for this order.",
        }

    existing_tags = order.get("tags", "")
    new_tags = ", ".join(filter(None, [existing_tags, "warranty-claim"]))
    existing_note = order.get("note") or ""
    new_note = (existing_note + f"\n[Warranty claim] {issue}").strip()

    await _put(store, f"orders/{order['id']}.json", {"order": {"id": order["id"], "tags": new_tags, "note": new_note}})

    return {"status": "submitted", "order_number": order_number, "issue": issue}


async def check_claim_status(store, entities: dict, session_id: str) -> dict:
    order_number = _extract_order_number(entities)
    if not order_number:
        return {"error": "missing_order_number"}

    email = (entities.get("email") or "").strip().lower()
    if not email:
        return {
            "error": "verification_required",
            "order_number": order_number,
            "message": "For privacy, please confirm the email address used on this order before checking claim status.",
        }

    resp = await _get(store, "orders.json", {"name": f"#{order_number}", "status": "any"})
    if resp.status_code != 200 or not resp.json().get("orders"):
        return {"error": "not_found", "order_number": order_number}

    order = resp.json()["orders"][0]
    order_email = (order.get("email") or order.get("contact_email") or "").strip().lower()
    if not order_email or order_email != email:
        return {
            "error": "verification_failed",
            "order_number": order_number,
            "message": "That email doesn't match our records for this order.",
        }

    tags = order.get("tags", "")
    if "warranty-claim" in tags:
        return {"order_number": order_number, "status": "under review", "note": order.get("note", "")}
    return {"order_number": order_number, "status": "no claim on file for this order"}


# ==========================================================================
# product_search
# ==========================================================================
async def search_products(store, entities: dict, session_id: str) -> dict:
    params = {"status": "active", "limit": 10}
    query = entities.get("query") or entities.get("category")
    if query:
        params["title"] = query

    resp = await _get(store, "products.json", params)
    if resp.status_code != 200:
        return {"error": "lookup_failed"}

    price_min = entities.get("price_min")
    price_max = entities.get("price_max")
    color = (entities.get("color") or "").lower()
    size = (entities.get("size") or "").lower()

    results = []
    for p in resp.json().get("products", []):
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
                "name": p.get("title", "Unnamed product"),
                "price": price,
                "image": (p.get("image") or {}).get("src", ""),
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


async def answer_policy_question(store, entities: dict, session_id: str) -> dict:
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

    if policy_type == "warranty_policy":
        return {"policy_type": policy_type, "not_found": True, "note": "This store has not published a separate warranty policy."}

    return {"policy_type": policy_type, "not_found": True}


# ==========================================================================
# fallback
# ==========================================================================
async def clarify(store, entities: dict, session_id: str) -> dict:
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
    "customer_account.get_my_orders": get_my_orders,
    "customer_account.get_recommendations": get_recommendations,
    "fallback.clarify": clarify,
}


async def dispatch(intent: str, action: str, store, entities: dict, session_id: str, customer_id: str | None = None) -> dict:
    key = f"{intent}.{action}"
    fn = ACTION_MAP.get(key)
    if fn is None:
        return {"error": f"No handler registered for {key}"}
    if intent == "customer_account":
        return await fn(store, entities, session_id, customer_id=customer_id)
    return await fn(store, entities, session_id)
