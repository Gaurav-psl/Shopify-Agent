"""
shopify_actions.py — the Shopify data layer.

It has four layers. Each one only calls the layer above it.

  1. TRANSPORT      rest()            Admin REST      (legacy; kept for compatibility)
                    graphql_admin()   Admin GraphQL   (primary API for all CRUD)
                    storefront_query() Storefront GraphQL (carts, as a shopper would)
  2. CRUD LIBRARY   products / customers / orders / metafields / carts
                    Plain async functions: get_*, find_*, create_*, update_*, delete_*.
                    Reusable from anywhere (webhooks, admin scripts, tests).
                    Only the read functions are reachable by the chatbot; the
                    create/update/delete ones are NOT mapped to any intent.
  3. ACTIONS        one function per action in intent_schema.json. Signature:
                        async def action(store, entities, session_id, customer_id=None) -> dict
                    They return plain dicts that reply_generator.py phrases.
  4. dispatch()     "intent.action" -> function.

LOGGED-IN SHOPPERS
  When customer_id is given (verified by customer_profiles.read_session_token),
  order tracking / order list / warranty claims need no email and no order
  number: the shopper's orders are found from their customer id, and an order
  is only used if it belongs to that customer. Guests still need an order
  number plus the email on the order.

CARTS (CART_MODE env var)
  "browser" (default): cart actions return a `widget_action` that widget.js runs
                       against the store's real /cart/*.js, so the theme's cart
                       and the badge update.
  "server":            a separate Storefront API cart per chat session (not
                       visible in the theme's cart; checkout via checkout_url).

ACCESS SCOPES this file needs (app config):
  read_products, write_products          product search + product CRUD
  read_customers, write_customers        customer lookup + CRUD
  read_orders, write_orders              order lookup, tags/notes (claims), cancel
  read_all_orders                        orders older than 60 days
  read_legal_policies                    store policies
  read_files not needed. Storefront token is created on first use.
  Order email / customer fields need "protected customer data" approval
  in the Partner Dashboard.

SHOPIFY_API_VERSION: bump this as Shopify retires old versions.
"""

import asyncio
import os
import re

import httpx

import tracking_provider

SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-10")
CART_MODE = os.environ.get("CART_MODE", "browser").lower()  # "browser" | "server"


# ==========================================================================
# 1. TRANSPORT
# ==========================================================================
def _headers(store) -> dict:
    return {"X-Shopify-Access-Token": store.access_token, "Content-Type": "application/json"}


def _url(store, path: str) -> str:
    return f"https://{store.shop_domain}/admin/api/{SHOPIFY_API_VERSION}/{path}"


async def rest(store, method: str, path: str, *, params: dict | None = None,
               json: dict | None = None, retries: int = 2) -> httpx.Response:
    """Admin REST call with a retry on 429 (rate limit). Legacy API: prefer
    graphql_admin() for anything new."""
    resp = None
    for attempt in range(retries + 1):
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.request(method, _url(store, path), headers=_headers(store),
                                        params=params or {}, json=json)
        if resp.status_code == 429 and attempt < retries:
            try:
                wait = float(resp.headers.get("Retry-After", "1"))
            except ValueError:
                wait = 1.0
            await asyncio.sleep(min(wait, 5))
            continue
        break
    return resp


# Thin wrappers: chatbot_widget.py calls _get(store, "products.json", ...)
async def _get(store, path: str, params: dict | None = None) -> httpx.Response:
    return await rest(store, "GET", path, params=params)


async def _post(store, path: str, json: dict) -> httpx.Response:
    return await rest(store, "POST", path, json=json)


async def _put(store, path: str, json: dict) -> httpx.Response:
    return await rest(store, "PUT", path, json=json)


async def _delete(store, path: str) -> httpx.Response:
    return await rest(store, "DELETE", path)


async def graphql_admin(store, query: str, variables: dict | None = None, retries: int = 2) -> dict | None:
    """Admin GraphQL. Returns the `data` dict, or None on any failure
    (HTTP error, GraphQL errors). Retries when throttled."""
    url = f"https://{store.shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, headers=_headers(store),
                                         json={"query": query, "variables": variables or {}})
        except Exception as e:  # noqa: BLE001
            print(f"shopify_actions: GraphQL request failed: {e!r}")
            return None
        if resp.status_code == 429 and attempt < retries:
            await asyncio.sleep(1.5)
            continue
        if resp.status_code != 200:
            print(f"shopify_actions: GraphQL HTTP {resp.status_code}: {resp.text[:500]}")
            return None
        body = resp.json()
        errors = body.get("errors")
        if errors:
            throttled = isinstance(errors, list) and any(
                isinstance(e, dict) and (e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors)
            if throttled and attempt < retries:
                await asyncio.sleep(1.5)
                continue
            print(f"shopify_actions: GraphQL errors: {errors}")
            return None
        return body.get("data")
    return None


_graphql_admin = graphql_admin  # old name


async def _mutate(store, mutation: str, variables: dict, root: str):
    """Runs a mutation. Returns (payload, error_messages). `payload` is the
    mutation's result object; error_messages is [] on success."""
    data = await graphql_admin(store, mutation, variables)
    if data is None:
        return None, ["request_failed"]
    payload = data.get(root) or {}
    errs = payload.get("userErrors") or payload.get(f"{root}UserErrors") or []
    return payload, [e.get("message", "error") for e in errs if isinstance(e, dict)]


def _gid(kind: str, value) -> str:
    s = str(value)
    return s if s.startswith("gid://") else f"gid://shopify/{kind}/{s}"


def _num(gid) -> str:
    return str(gid or "").split("/")[-1]


def _price_number(value):
    try:
        f = float(value)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return value


def _extract_order_number(entities: dict, message: str = "") -> str | None:
    for key in ("order_number", "order_id"):
        val = entities.get(key)
        if val:
            return str(val).lstrip("#").strip()
    match = re.search(r"#?\s*(\d{3,})", message or "")
    return match.group(1) if match else None


# ==========================================================================
# 2a. CRUD — CUSTOMERS  (Admin GraphQL)
# ==========================================================================
async def get_customer(store, customer_id: str) -> dict | None:
    """READ one customer by numeric id."""
    q = """query($id: ID!) { customer(id: $id) {
        id firstName lastName defaultEmailAddress { emailAddress } numberOfOrders } }"""
    data = await graphql_admin(store, q, {"id": _gid("Customer", customer_id)})
    return (data or {}).get("customer")


async def find_customer_by_email(store, email: str) -> dict | None:
    """READ (search) a customer by email."""
    q = """query($q: String!) { customers(first: 1, query: $q) { nodes {
        id firstName lastName defaultEmailAddress { emailAddress } numberOfOrders } } }"""
    data = await graphql_admin(store, q, {"q": f"email:{email}"})
    nodes = ((data or {}).get("customers") or {}).get("nodes") or []
    return nodes[0] if nodes else None


async def create_customer(store, email: str, *, first_name: str | None = None, last_name: str | None = None,
                          phone: str | None = None, tags: list[str] | None = None, note: str | None = None):
    """CREATE. Returns (customer_dict | None, errors)."""
    inp = {"email": email}
    for k, v in (("firstName", first_name), ("lastName", last_name), ("phone", phone), ("tags", tags), ("note", note)):
        if v:
            inp[k] = v
    m = """mutation($input: CustomerInput!) { customerCreate(input: $input) {
        customer { id firstName lastName } userErrors { field message } } }"""
    payload, errs = await _mutate(store, m, {"input": inp}, "customerCreate")
    return (payload or {}).get("customer"), errs


async def update_customer(store, customer_id: str, fields: dict):
    """UPDATE. `fields` uses GraphQL names: firstName, lastName, phone, tags, note..."""
    m = """mutation($input: CustomerInput!) { customerUpdate(input: $input) {
        customer { id firstName lastName } userErrors { field message } } }"""
    payload, errs = await _mutate(store, m, {"input": {"id": _gid("Customer", customer_id), **fields}}, "customerUpdate")
    return (payload or {}).get("customer"), errs


async def delete_customer(store, customer_id: str):
    """DELETE. Returns (deleted_id | None, errors)."""
    m = """mutation($input: CustomerDeleteInput!) { customerDelete(input: $input) {
        deletedCustomerId userErrors { field message } } }"""
    payload, errs = await _mutate(store, m, {"input": {"id": _gid("Customer", customer_id)}}, "customerDelete")
    return (payload or {}).get("deletedCustomerId"), errs


# ==========================================================================
# 2b. CRUD — PRODUCTS  (Admin GraphQL)
# ==========================================================================
_PRODUCT_FIELDS = """
    id title handle productType tags vendor status
    featuredImage { url }
    variants(first: 50) { nodes { id title price availableForSale selectedOptions { name value } } }
"""


async def get_product(store, product_id: str) -> dict | None:
    """READ one product."""
    q = "query($id: ID!) { product(id: $id) { " + _PRODUCT_FIELDS + " } }"
    data = await graphql_admin(store, q, {"id": _gid("Product", product_id)})
    return (data or {}).get("product")


async def create_product(store, title: str, *, description_html: str | None = None, vendor: str | None = None,
                         product_type: str | None = None, tags: list[str] | None = None, status: str = "DRAFT"):
    """CREATE. status: ACTIVE | DRAFT | ARCHIVED. Returns (product | None, errors)."""
    inp = {"title": title, "status": status}
    for k, v in (("descriptionHtml", description_html), ("vendor", vendor), ("productType", product_type), ("tags", tags)):
        if v:
            inp[k] = v
    m = """mutation($product: ProductCreateInput!) { productCreate(product: $product) {
        product { id title handle } userErrors { field message } } }"""
    payload, errs = await _mutate(store, m, {"product": inp}, "productCreate")
    return (payload or {}).get("product"), errs


async def update_product(store, product_id: str, fields: dict):
    """UPDATE. `fields` uses GraphQL names: title, descriptionHtml, vendor, productType, tags, status."""
    m = """mutation($product: ProductUpdateInput!) { productUpdate(product: $product) {
        product { id title handle status } userErrors { field message } } }"""
    payload, errs = await _mutate(store, m, {"product": {"id": _gid("Product", product_id), **fields}}, "productUpdate")
    return (payload or {}).get("product"), errs


async def update_variant_price(store, product_id: str, variant_id: str, price: str | float):
    """UPDATE a variant's price."""
    m = """mutation($pid: ID!, $variants: [ProductVariantsBulkInput!]!) {
        productVariantsBulkUpdate(productId: $pid, variants: $variants) {
            productVariants { id price } userErrors { field message } } }"""
    payload, errs = await _mutate(
        store, m,
        {"pid": _gid("Product", product_id), "variants": [{"id": _gid("ProductVariant", variant_id), "price": str(price)}]},
        "productVariantsBulkUpdate")
    return (payload or {}).get("productVariants"), errs


async def delete_product(store, product_id: str):
    """DELETE. Returns (deleted_id | None, errors)."""
    m = """mutation($input: ProductDeleteInput!) { productDelete(input: $input) {
        deletedProductId userErrors { field message } } }"""
    payload, errs = await _mutate(store, m, {"input": {"id": _gid("Product", product_id)}}, "productDelete")
    return (payload or {}).get("deletedProductId"), errs


def _search_clause(term: str | None) -> str:
    """'running shoes' -> (title:*running* OR product_type:*running* OR tag:running) AND (...shoe...)"""
    clauses = []
    for w in re.findall(r"[a-z0-9]+", (term or "").lower()):
        s = w[:-1] if w.endswith("s") and len(w) > 3 else w
        clauses.append(f"(title:*{s}* OR product_type:*{s}* OR tag:{s})")
    return " AND ".join(clauses)


async def search_products_graphql(store, term: str | None = None, *, price_min=None, price_max=None,
                                  color: str | None = None, size: str | None = None, limit: int = 6) -> list[dict] | None:
    """READ (search). Returns product cards (one per matching product, using the
    first variant that passes the filters), [] when nothing matches, or None
    when Shopify couldn't be reached."""
    clause = _search_clause(term)
    q = "status:active" + (f" AND {clause}" if clause else "")
    query = "query($q: String!) { products(first: 50, query: $q) { nodes { " + _PRODUCT_FIELDS + " } } }"
    data = await graphql_admin(store, query, {"q": q})
    if data is None:
        return None

    color, size = (color or "").lower(), (size or "").lower()
    cards = []
    for p in ((data.get("products") or {}).get("nodes") or []):
        for v in (p.get("variants") or {}).get("nodes") or []:
            if v.get("availableForSale") is False:
                continue
            try:
                price = float(v.get("price") or 0)
            except ValueError:
                price = 0.0
            if price_min is not None and price < float(price_min):
                continue
            if price_max is not None and price > float(price_max):
                continue
            opts = " ".join(str(o.get("value", "")) for o in (v.get("selectedOptions") or [])).lower()
            if color and color not in opts:
                continue
            if size and size not in opts:
                continue
            card = {
                "id": _num(v.get("id")),
                "name": p.get("title", "Unnamed product"),
                "price": _price_number(price),
                "image": (p.get("featuredImage") or {}).get("url", ""),
            }
            if p.get("handle"):
                card["url"] = f"https://{store.shop_domain}/products/{p['handle']}"
            cards.append(card)
            break
        if len(cards) >= limit:
            break
    return cards


async def get_active_products_graphql(store, first: int = 100) -> list[dict]:
    """READ the catalog in the shape recommendation_engine.py expects."""
    query = """query($first: Int!) { products(first: $first, query: "status:ACTIVE") { nodes {
        id title handle productType tags featuredImage { url }
        variants(first: 10) { nodes { id title price } } } } }"""
    data = await graphql_admin(store, query, {"first": min(max(first, 1), 250)})
    nodes = ((data or {}).get("products") or {}).get("nodes") or []
    return [{
        "id": _num(p.get("id")), "title": p.get("title"), "handle": p.get("handle"),
        "product_type": p.get("productType"), "tags": p.get("tags") or [],
        "image": (p.get("featuredImage") or {}).get("url", ""),
        "variants": [{"id": _num(v.get("id")), "title": v.get("title"), "price": v.get("price")}
                     for v in ((p.get("variants") or {}).get("nodes") or [])],
    } for p in nodes]


# ==========================================================================
# 2c. CRUD — ORDERS  (Admin GraphQL)
# ==========================================================================
_ORDER_FIELDS = """
    id name createdAt cancelledAt email tags note
    displayFinancialStatus displayFulfillmentStatus
    customer { id }
    totalPriceSet { shopMoney { amount currencyCode } }
    lineItems(first: 20) { nodes { name quantity variant { id title product { id } } } }
    fulfillments { displayStatus estimatedDeliveryAt trackingInfo { number url company } }
"""


def _normalize_order(o: dict) -> dict:
    """Full internal view of an order (includes email / customer id / tags —
    never send this straight to the shopper or the LLM; use _order_card)."""
    total = (o.get("totalPriceSet") or {}).get("shopMoney") or {}
    fulfillments = o.get("fulfillments") or []
    tracking = [t for f in fulfillments for t in (f.get("trackingInfo") or []) if t and (t.get("number") or t.get("url"))]
    tracked_f = next((f for f in fulfillments if f.get("trackingInfo")), fulfillments[0] if fulfillments else {})
    status = (o.get("displayFulfillmentStatus") or "").lower().replace("_", " ") or "unknown"
    if o.get("cancelledAt"):
        status = "cancelled"
    items = []
    for line in (o.get("lineItems") or {}).get("nodes") or []:
        v = line.get("variant") or {}
        pid = (v.get("product") or {}).get("id")
        items.append({
            "name": line.get("name"), "quantity": line.get("quantity"), "variant": v.get("title"),
            "variant_id": _num(v.get("id")) if v.get("id") else None,
            "product_id": _num(pid) if pid else None,
        })
    eta = tracked_f.get("estimatedDeliveryAt")
    return {
        "gid": o.get("id"),
        "order_number": str(o.get("name") or "").lstrip("#"),
        "date": (o.get("createdAt") or "")[:10],
        "email": (o.get("email") or "").strip().lower(),
        "customer_id": _num((o.get("customer") or {}).get("id")) if o.get("customer") else None,
        "status": status,
        "financial_status": (o.get("displayFinancialStatus") or "").lower().replace("_", " ") or "unknown",
        "total": total.get("amount"), "currency": total.get("currencyCode"),
        "items": items, "tracking": tracking,
        "shipment_status": (tracked_f.get("displayStatus") or "").lower() or None,
        "estimated_delivery": eta[:10] if eta else None,
        "tags": o.get("tags") or [], "note": o.get("note") or "",
    }


def _order_card(o: dict) -> dict:
    """Safe, shopper-facing view of an order (no email, tags, internal ids).
    `id` is the human order number, which is what the widget shows and taps."""
    return {
        "id": o["order_number"], "order_number": o["order_number"], "date": o["date"],
        "status": o["status"], "financial_status": o["financial_status"],
        "total": o["total"], "currency": o["currency"],
        "items": [{"name": i["name"], "quantity": i["quantity"], "variant": i["variant"]} for i in o["items"]],
        "tracking": o["tracking"],
    }


def _is_done(o: dict) -> bool:
    return o["status"] in {"fulfilled", "cancelled", "restocked"}


async def get_order_by_name(store, number: str) -> dict | None:
    """READ one order by its number (e.g. '1001'). Returns the normalized order."""
    q = "query($q: String!) { orders(first: 1, query: $q) { nodes { " + _ORDER_FIELDS + " } } }"
    data = await graphql_admin(store, q, {"q": f"name:#{number}"})
    nodes = ((data or {}).get("orders") or {}).get("nodes") or []
    if not nodes:
        return None
    order = _normalize_order(nodes[0])
    return order if order["order_number"] == str(number).lstrip("#") else None


async def list_customer_orders(store, customer_id: str, first: int = 50) -> list[dict]:
    """READ a customer's orders, newest first (normalized)."""
    q = ("query($first: Int!, $q: String!) { orders(first: $first, query: $q, sortKey: CREATED_AT, reverse: true) "
         "{ nodes { " + _ORDER_FIELDS + " } } }")
    data = await graphql_admin(store, q, {"first": min(max(first, 1), 100), "q": f"customer_id:{customer_id}"})
    return [_normalize_order(n) for n in (((data or {}).get("orders") or {}).get("nodes") or [])]


async def get_customer_orders(store, customer_id: str, first: int = 50) -> dict:
    """Shape used by /customer-session in chatbot_widget.py."""
    active, past, purchased = [], [], []
    for o in await list_customer_orders(store, customer_id, first):
        for it in o["items"]:
            if it.get("product_id"):
                purchased.append(it["product_id"])
        (past if _is_done(o) else active).append(_order_card(o))
    return {"active_orders": active, "past_orders": past,
            "purchased_product_ids": list(dict.fromkeys(purchased))}  # product ids (what recommendation_engine compares)


async def add_order_tags(store, order_gid: str, tags: list[str]):
    """UPDATE: add tags (idempotent, doesn't touch existing tags)."""
    m = "mutation($id: ID!, $tags: [String!]!) { tagsAdd(id: $id, tags: $tags) { node { id } userErrors { field message } } }"
    return await _mutate(store, m, {"id": order_gid, "tags": tags}, "tagsAdd")


async def set_order_note(store, order_gid: str, note: str):
    """UPDATE: replaces the order note (merge with the old one yourself)."""
    m = """mutation($input: OrderInput!) { orderUpdate(input: $input) {
        order { id note } userErrors { field message } } }"""
    return await _mutate(store, m, {"input": {"id": order_gid, "note": note}}, "orderUpdate")


async def cancel_order(store, order_gid: str, *, reason: str = "CUSTOMER", refund: bool = False,
                       restock: bool = True, notify_customer: bool = False, staff_note: str | None = None):
    """DELETE-equivalent. Verify the orderCancel arguments against your API version before using."""
    m = """mutation($orderId: ID!, $reason: OrderCancelReason!, $refund: Boolean!, $restock: Boolean!,
                    $notify: Boolean, $note: String) {
        orderCancel(orderId: $orderId, reason: $reason, refund: $refund, restock: $restock,
                    notifyCustomer: $notify, staffNote: $note) {
            job { id } orderCancelUserErrors { field message } } }"""
    return await _mutate(store, m, {"orderId": order_gid, "reason": reason, "refund": refund, "restock": restock,
                                    "notify": notify_customer, "note": staff_note}, "orderCancel")


# ==========================================================================
# 2d. CRUD — METAFIELDS  (custom data on any object: orders, customers, products)
# ==========================================================================
async def metafield_set(store, owner_gid: str, namespace: str, key: str, value: str,
                        type_: str = "single_line_text_field"):
    """CREATE/UPDATE one metafield. Returns (metafield | None, errors)."""
    m = """mutation($m: [MetafieldsSetInput!]!) { metafieldsSet(metafields: $m) {
        metafields { id namespace key value } userErrors { field message } } }"""
    payload, errs = await _mutate(
        store, m, {"m": [{"ownerId": owner_gid, "namespace": namespace, "key": key, "value": value, "type": type_}]},
        "metafieldsSet")
    mf = (payload or {}).get("metafields") or []
    return (mf[0] if mf else None), errs


async def metafield_get(store, owner_gid: str, namespace: str, key: str) -> str | None:
    """READ one metafield value."""
    q = """query($id: ID!, $ns: String!, $key: String!) { node(id: $id) {
        ... on HasMetafields { metafield(namespace: $ns, key: $key) { value } } } }"""
    data = await graphql_admin(store, q, {"id": owner_gid, "ns": namespace, "key": key})
    return (((data or {}).get("node") or {}).get("metafield") or {}).get("value")


# ==========================================================================
# 2e. CRUD — CARTS  (Storefront GraphQL, a separate API with its own token)
# ==========================================================================
_STOREFRONT_TOKENS: dict[str, str] = {}   # shop_domain -> storefront access token
_SESSION_CARTS: dict[str, str] = {}       # chat session_id -> Storefront cart GID
_STOREFRONT_TOKEN_TITLE = "AI Shopping Assistant"

_CART_FIELDS = """
    id checkoutUrl totalQuantity
    cost { totalAmount { amount currencyCode } }
    lines(first: 50) { edges { node { id quantity merchandise { ... on ProductVariant {
        id title product { title } price { amount currencyCode } } } } } }
"""


async def _get_storefront_token(store) -> str | None:
    cached = _STOREFRONT_TOKENS.get(store.shop_domain)
    if cached:
        return cached
    data = await graphql_admin(store, "query { shop { storefrontAccessTokens(first: 10) { nodes { accessToken title } } } }")
    for tok in (((data or {}).get("shop") or {}).get("storefrontAccessTokens") or {}).get("nodes") or []:
        if tok.get("accessToken"):
            _STOREFRONT_TOKENS[store.shop_domain] = tok["accessToken"]
            return tok["accessToken"]
    m = """mutation($input: StorefrontAccessTokenInput!) { storefrontAccessTokenCreate(input: $input) {
        storefrontAccessToken { accessToken } userErrors { field message } } }"""
    payload, errs = await _mutate(store, m, {"input": {"title": _STOREFRONT_TOKEN_TITLE}}, "storefrontAccessTokenCreate")
    token = ((payload or {}).get("storefrontAccessToken") or {}).get("accessToken")
    if token:
        _STOREFRONT_TOKENS[store.shop_domain] = token
    return token


async def storefront_query(store, query: str, variables: dict) -> dict | None:
    token = await _get_storefront_token(store)
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://{store.shop_domain}/api/{SHOPIFY_API_VERSION}/graphql.json",
                headers={"X-Shopify-Storefront-Access-Token": token, "Content-Type": "application/json"},
                json={"query": query, "variables": variables})
    except Exception as e:  # noqa: BLE001
        print(f"shopify_actions: storefront request failed: {e!r}")
        return None
    if resp.status_code != 200:
        return None
    body = resp.json()
    return None if body.get("errors") else body.get("data")


def _summarize_cart(cart: dict | None) -> dict:
    if not cart:
        return {"items": [], "item_count": 0, "total": "0.00", "currency": None, "checkout_url": None}
    items = []
    for edge in cart.get("lines", {}).get("edges", []):
        line = edge["node"]
        merch = line.get("merchandise", {}) or {}
        items.append({
            "line_id": line["id"],
            "name": (merch.get("product") or {}).get("title") or merch.get("title") or "Item",
            "variant": merch.get("title"), "quantity": line["quantity"],
            "price": (merch.get("price") or {}).get("amount"),
        })
    cost = (cart.get("cost") or {}).get("totalAmount") or {}
    return {"items": items, "item_count": cart.get("totalQuantity", 0), "total": cost.get("amount"),
            "currency": cost.get("currencyCode"), "checkout_url": cart.get("checkoutUrl")}


def _find_line(summary: dict, product_query: str) -> dict | None:
    needle = (product_query or "").strip().lower()
    return next((i for i in summary["items"] if needle and needle in i["name"].lower()), None) if needle else None


async def cart_get(store, cart_id: str) -> dict | None:
    data = await storefront_query(store, "query($id: ID!) { cart(id: $id) { " + _CART_FIELDS + " } }", {"id": cart_id})
    return (data or {}).get("cart")


async def cart_create(store) -> dict | None:
    data = await storefront_query(
        store, "mutation { cartCreate { cart { " + _CART_FIELDS + " } userErrors { field message } } }", {})
    result = (data or {}).get("cartCreate") or {}
    return None if result.get("userErrors") else result.get("cart")


async def _cart_for_session(store, session_id: str) -> dict | None:
    cart_id = _SESSION_CARTS.get(session_id)
    cart = await cart_get(store, cart_id) if cart_id else None
    if cart is None:
        cart = await cart_create(store)
        if cart:
            _SESSION_CARTS[session_id] = cart["id"]
    return cart


async def _cart_mutation(store, name: str, args: str, variables: dict) -> dict:
    """Runs cartLinesAdd / cartLinesUpdate / cartLinesRemove. Returns the result object."""
    arg = "lineIds" if name == "cartLinesRemove" else "lines"
    m = ("mutation(" + args + ") { " + name + "(cartId: $cartId, " + arg + ": $" + arg + ") { cart { "
         + _CART_FIELDS + " } userErrors { field message } } }")
    data = await storefront_query(store, m, variables)
    return (data or {}).get(name) or {}


# ==========================================================================
# 3. ACTIONS  (one per intent_schema.json action)
# ==========================================================================

# ---- registration_login: Shopify handles shopper auth on its own pages ----
def _account_redirect(store, page: str, message: str) -> dict:
    return {"status": "redirect", "message": message,
            "widget_action": {"type": "redirect", "url": f"https://{store.shop_domain}/account/{page}"}}


async def register(store, entities, session_id, customer_id=None):
    return _account_redirect(store, "register", "Taking you to the account creation page.")


async def login(store, entities, session_id, customer_id=None):
    return _account_redirect(store, "login", "Taking you to the sign-in page.")


async def logout(store, entities, session_id, customer_id=None):
    return _account_redirect(store, "logout", "Signing you out.")


async def forgot_password(store, entities, session_id, customer_id=None):
    return _account_redirect(store, "login#recover", "Taking you to the password recovery page.")


# ---- order lookup shared by tracking + claims -----------------------------
async def _resolve_order(store, entities: dict, customer_id: str | None, *, strategy: str):
    """Finds the order a request is about. Returns (order, None) or (None, result_dict).

    Logged in: order number optional. Ownership = the order's customer id equals
    the verified customer id. strategy "track": the one active order (several
    active -> returns them for a picker) else the newest; "claim": their only
    order, or asks which.
    Guest: order number + matching order email required."""
    number = _extract_order_number(entities)

    if not customer_id:
        if not number:
            return None, {"error": "missing_order_number", "message": "No order number was given."}
        email = (entities.get("email") or "").strip().lower()
        if not email:
            return None, {"error": "verification_required", "order_number": number,
                          "message": "For privacy, please confirm the email address used on this order first."}
        order = await get_order_by_name(store, number)
        if not order:
            return None, {"error": "not_found", "order_number": number}
        if not order["email"] or order["email"] != email:
            return None, {"error": "verification_failed", "order_number": number,
                          "message": "That email doesn't match our records for this order."}
        return order, None

    if number:
        order = await get_order_by_name(store, number)
        if not order or order["customer_id"] != str(customer_id):
            return None, {"error": "not_found", "order_number": number}
        return order, None

    orders = await list_customer_orders(store, customer_id, first=25)
    if not orders:
        return None, {"error": "no_orders", "message": "No orders were found on this account yet."}
    if strategy == "track":
        active = [o for o in orders if not _is_done(o)]
        if len(active) > 1:
            return None, {"orders": [_order_card(o) for o in active[:10]],
                          "message": "The shopper has several active orders. Ask which one to track; they can tap one."}
        return (active[0] if active else orders[0]), None
    if len(orders) == 1:
        return orders[0], None
    return None, {"error": "missing_order_number", "message": "Ask which order this is for.",
                  "recent_orders": [{"order_number": o["order_number"], "date": o["date"],
                                     "items": [i["name"] for i in o["items"]][:3]} for o in orders[:5]]}


# ---- order_tracking --------------------------------------------------------
async def track_order(store, entities, session_id, customer_id=None):
    order, problem = await _resolve_order(store, entities, customer_id, strategy="track")
    if problem:
        return problem

    first = next((t for t in order["tracking"] if t.get("number")), order["tracking"][0] if order["tracking"] else {})
    tracking_number = first.get("number")
    result = {
        "order_number": order["order_number"],
        "fulfillment_status": order["status"] if order["status"] != "unknown" else "unfulfilled",
        "financial_status": order["financial_status"],
        "tracking_number": tracking_number,
        "tracking_url": first.get("url"),
        "shipment_status": order["shipment_status"],
        "current_location": None,
        "last_scan_message": None,
        "last_scan_time": None,
        "estimated_delivery": order["estimated_delivery"],
    }
    # AfterShip gives a real physical location + delivery estimate; Shopify's own
    # coarse status above is the fallback when AfterShip has nothing yet.
    if tracking_number:
        live = await tracking_provider.get_live_status(tracking_number)
        if live and (live.get("location") or live.get("status") or live.get("estimated_delivery")):
            result["shipment_status"] = live.get("status") or order["shipment_status"]
            result["current_location"] = live.get("location")
            result["last_scan_message"] = live.get("message")
            result["last_scan_time"] = live.get("checkpoint_time")
            result["estimated_delivery"] = live.get("estimated_delivery") or order["estimated_delivery"]
    return result


async def list_recent_orders(store, entities, session_id, customer_id=None):
    # An email is not proof of ownership, so only logged-in shoppers can list orders.
    if not customer_id:
        return {"error": "authentication_required",
                "message": "Please log in to see your orders, or give an order number and its email to track one order."}
    orders = await list_customer_orders(store, customer_id, first=50)
    if not orders:
        return {"orders": [], "message": "This account has no orders yet."}
    return {"orders": [_order_card(o) for o in orders[:10]]}


async def get_recommendations(store, entities, session_id, customer_id=None):
    if not customer_id:
        return {"error": "authentication_required"}
    import customer_profiles
    import recommendation_engine  # imported here because it imports this module
    profile = await customer_profiles.get_profile(store.shop_domain, customer_id) or {}
    return await recommendation_engine.recommend_products(store, profile, limit=6)


# ---- cart_management --------------------------------------------------------
async def _resolve_variant(store, product_query: str) -> dict | None:
    if not product_query:
        return None
    found = await search_products_graphql(store, product_query, limit=1)
    if not found:
        return None
    c = found[0]
    return {"variant_id": c["id"], "name": c["name"], "price": c["price"], "image": c["image"]}


def _vid(value):
    s = str(value)
    return int(s) if s.isdigit() else s


async def add_item(store, entities, session_id, customer_id=None):
    query = entities.get("product_name_or_id", "")
    match = await _resolve_variant(store, query)
    if not match:
        return {"error": "not_found", "query": query}
    qty = int(entities.get("quantity") or 1)

    if CART_MODE == "server":
        cart = await _cart_for_session(store, session_id)
        if not cart:
            return {"error": "cart_unavailable", "query": query}
        res = await _cart_mutation(store, "cartLinesAdd", "$cartId: ID!, $lines: [CartLineInput!]!", {
            "cartId": cart["id"],
            "lines": [{"merchandiseId": _gid("ProductVariant", match["variant_id"]), "quantity": qty}]})
        if res.get("userErrors"):
            return {"error": "cart_update_failed", "details": res["userErrors"], "query": query}
        return {"added": match["name"], "quantity": qty, "cart": _summarize_cart(res.get("cart") or cart)}

    return {"added": match["name"], "quantity": qty,
            "widget_action": {"type": "cart_add", "variant_id": _vid(match["variant_id"]), "quantity": qty}}


async def remove_item(store, entities, session_id, customer_id=None):
    name = entities.get("product_name_or_id", "")
    if CART_MODE != "server":
        return {"removing": name, "widget_action": {"type": "cart_remove", "product_name": name}}

    cart_id = _SESSION_CARTS.get(session_id)
    if not cart_id:
        return {"removed": None, "message": "Your cart is already empty."}
    summary = _summarize_cart(await cart_get(store, cart_id))
    line = _find_line(summary, name)
    if not line:
        return {"error": "not_found", "query": name, "cart": summary}
    res = await _cart_mutation(store, "cartLinesRemove", "$cartId: ID!, $lineIds: [ID!]!",
                               {"cartId": cart_id, "lineIds": [line["line_id"]]})
    if res.get("userErrors"):
        return {"error": "cart_update_failed", "details": res["userErrors"], "query": name}
    return {"removed": line["name"], "cart": _summarize_cart(res.get("cart"))}


async def edit_quantity(store, entities, session_id, customer_id=None):
    name = entities.get("product_name_or_id", "")
    qty = int(entities.get("quantity") or 1)
    if CART_MODE != "server":
        return {"item": name, "new_quantity": qty,
                "widget_action": {"type": "cart_set_quantity", "product_name": name, "quantity": qty}}

    cart_id = _SESSION_CARTS.get(session_id)
    if not cart_id:
        return {"error": "not_found", "query": name}
    summary = _summarize_cart(await cart_get(store, cart_id))
    line = _find_line(summary, name)
    if not line:
        return {"error": "not_found", "query": name, "cart": summary}
    res = await _cart_mutation(store, "cartLinesUpdate", "$cartId: ID!, $lines: [CartLineUpdateInput!]!",
                               {"cartId": cart_id, "lines": [{"id": line["line_id"], "quantity": qty}]})
    if res.get("userErrors"):
        return {"error": "cart_update_failed", "details": res["userErrors"], "query": name}
    return {"item": name, "new_quantity": qty, "cart": _summarize_cart(res.get("cart"))}


async def view_cart(store, entities, session_id, customer_id=None):
    if CART_MODE != "server":
        return {"message": "The shopper's cart contents are being shown in the chat.",
                "widget_action": {"type": "cart_view"}}
    cart_id = _SESSION_CARTS.get(session_id)
    return {"cart": _summarize_cart(await cart_get(store, cart_id) if cart_id else None)}


async def clear_cart(store, entities, session_id, customer_id=None):
    if CART_MODE != "server":
        return {"message": "The shopper's cart is being emptied.", "widget_action": {"type": "cart_clear"}}
    cart_id = _SESSION_CARTS.get(session_id)
    if not cart_id:
        return {"cart": _summarize_cart(None)}
    summary = _summarize_cart(await cart_get(store, cart_id))
    line_ids = [i["line_id"] for i in summary["items"]]
    if not line_ids:
        return {"cart": summary}
    res = await _cart_mutation(store, "cartLinesRemove", "$cartId: ID!, $lineIds: [ID!]!",
                               {"cartId": cart_id, "lineIds": line_ids})
    if res.get("userErrors"):
        return {"error": "cart_update_failed", "details": res["userErrors"]}
    return {"cart": _summarize_cart(res.get("cart"))}


# ---- warranty_claim: recorded as an order tag + note the merchant sees in Admin ----
async def submit_claim(store, entities, session_id, customer_id=None):
    order, problem = await _resolve_order(store, entities, customer_id, strategy="claim")
    if problem:
        return problem
    issue = entities.get("issue_description") or "Not specified"
    claim_type = entities.get("claim_type")

    _, tag_errs = await add_order_tags(store, order["gid"], ["warranty-claim"])
    line = f"[Warranty claim{' - ' + claim_type if claim_type else ''}] {issue}"
    _, note_errs = await set_order_note(store, order["gid"], (order["note"] + "\n" + line).strip())
    if tag_errs or note_errs:
        print(f"submit_claim: errors tag={tag_errs} note={note_errs}")
        return {"error": "claim_failed", "order_number": order["order_number"]}
    return {"status": "submitted", "order_number": order["order_number"], "issue": issue}


async def check_claim_status(store, entities, session_id, customer_id=None):
    if customer_id and not _extract_order_number(entities):
        orders = await list_customer_orders(store, customer_id, first=25)
        if not orders:
            return {"error": "no_orders", "message": "No orders were found on this account yet."}
        claims = [{"order_number": o["order_number"], "status": "under review", "note": o["note"]}
                  for o in orders if "warranty-claim" in [t.lower() for t in o["tags"]]]
        return {"claims": claims} if claims else {"status": "no warranty claim on file for any recent order"}

    order, problem = await _resolve_order(store, entities, customer_id, strategy="claim")
    if problem:
        return problem
    if "warranty-claim" in [t.lower() for t in order["tags"]]:
        return {"order_number": order["order_number"], "status": "under review", "note": order["note"]}
    return {"order_number": order["order_number"], "status": "no claim on file for this order"}


# ---- product_search ----------------------------------------------------------
async def search_products(store, entities, session_id, customer_id=None):
    results = await search_products_graphql(
        store, entities.get("query") or entities.get("category"),
        price_min=entities.get("price_min"), price_max=entities.get("price_max"),
        color=entities.get("color"), size=entities.get("size"), limit=6)
    if results is None:
        return {"error": "lookup_failed"}
    return {"results": results, "filters_applied": entities}


# ---- policy_query: Shopify's real store policies ------------------------------
_POLICY_TYPES = {
    "refund_policy": "REFUND_POLICY", "shipping_policy": "SHIPPING_POLICY",
    "privacy_policy": "PRIVACY_POLICY", "terms_of_service": "TERMS_OF_SERVICE",
}


async def answer_policy_question(store, entities, session_id, customer_id=None):
    policy_type = entities.get("policy_type", "refund_policy")
    data = await graphql_admin(store, "query { shop { shopPolicies { type title body url } } }")
    if data is None:
        return {"error": "lookup_failed", "policy_type": policy_type}
    wanted = _POLICY_TYPES.get(policy_type)
    for p in ((data.get("shop") or {}).get("shopPolicies") or []):
        if wanted and p.get("type") == wanted:
            body = re.sub(r"<[^>]+>", " ", p.get("body") or "")
            return {"policy_type": policy_type, "title": p.get("title"),
                    "body": re.sub(r"\s+", " ", body).strip(), "url": p.get("url")}
    if policy_type == "warranty_policy":
        return {"policy_type": policy_type, "not_found": True,
                "note": "This store has not published a separate warranty policy."}
    return {"policy_type": policy_type, "not_found": True}


# ---- fallback --------------------------------------------------------------------
async def clarify(store, entities, session_id, customer_id=None):
    return {"message": "Could not confidently match this to a supported action."}


# ==========================================================================
# 4. ROUTER
# ==========================================================================
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
    "product_search.get_recommendations": get_recommendations,
    "fallback.clarify": clarify,
}


async def dispatch(intent: str, action: str, store, entities: dict, session_id: str,
                   customer_id: str | None = None) -> dict:
    key = f"{intent}.{action}"
    fn = ACTION_MAP.get(key)
    if fn is None:
        return {"error": f"No handler registered for {key}"}
    try:
        return await fn(store, entities or {}, session_id, customer_id=customer_id)
    except Exception as e:  # noqa: BLE001
        print(f"shopify_actions.dispatch: {key} failed: {e!r}")
        return {"error": "action_failed", "action": key}