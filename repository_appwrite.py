"""
repository_appwrite.py
------------------------
Same function names and signatures as the SQLAlchemy repository.py, so
main.py's route handlers don't need to change — only the import line
does: `import repository_appwrite as repo` instead of `import repository`.

NEW IN THIS VERSION (to support the rich "RenderLink" dashboard UI):
  - ensure_customization() now also seeds `instructions`, `status`,
    and `widget_position` alongside the existing agent_name/agent_title/
    icon_type/theme_color/custom_icon_url fields.
  - get_dashboard_user_by_id() — needed because the dashboard now shows
    the logged-in owner's email in the sidebar/topbar.
  - Features, Store Info, FAQs, and Feedback: new collections + CRUD
    functions, mirroring the existing ensure_customization/
    update_customization pattern.

>>> ACTION NEEDED IN appwrite_client.py <<<
Add these four new collection ID constants (create the matching
collections in your Appwrite console first) and import them below:

    FEATURES_COLLECTION       — one doc per store. Boolean attributes:
                                 product_search, recommendations,
                                 product_filtering, warranty,
                                 cart_editing, returns, track_orders.
                                 Plus a `store` relationship/string attr.
    STORE_INFO_COLLECTION     — one doc per store. String attributes:
                                 business_name, support_email, timezone.
                                 Plus a `store` relationship/string attr.
    FAQS_COLLECTION           — many docs per store. String attributes:
                                 question, answer. Plus `store`.
    FEEDBACK_COLLECTION       — many docs per store (write-mostly).
                                 String attribute: message. Plus `store`.

Also add `instructions` (string, large), `status` (string, default
"active"), and `widget_position` (string, default "bottom-right") as
attributes on your existing CUSTOMIZATIONS_COLLECTION.
"""

import json
from appwrite.query import Query
from appwrite.id import ID
from appwrite.exception import AppwriteException
from appwrite_client import (
    databases, DATABASE_ID, STORES_COLLECTION, FLOWS_COLLECTION,
    REQUEST_LOGS_COLLECTION, CUSTOMIZATIONS_COLLECTION, DASHBOARD_USERS_COLLECTION,
    FEATURES_COLLECTION, STORE_INFO_COLLECTION, FAQS_COLLECTION, FEEDBACK_COLLECTION,
)


def _find_store_doc(shop_domain: str):
    result = databases.list_documents(
        DATABASE_ID, STORES_COLLECTION,
        queries=[Query.equal("shop_domain", shop_domain)],
    )
    docs = result["documents"]
    return docs[0] if docs else None


def get_store(shop_domain: str) -> dict | None:
    """Returns the raw store document (not just the token), for callers
    that need more than the access token — e.g. checking is_new_store."""
    doc = _find_store_doc(shop_domain)
    if doc and doc.get("uninstalled"):
        return None
    return doc


def get_store_by_id(store_id: str) -> dict | None:
    try:
        return databases.get_document(DATABASE_ID, STORES_COLLECTION, store_id)
    except AppwriteException:
        return None


def upsert_shop(shop_domain: str, access_token: str, scopes: str) -> dict:
    existing = _find_store_doc(shop_domain)
    if existing:
        return databases.update_document(
            DATABASE_ID, STORES_COLLECTION, existing["$id"],
            data={"access_token": access_token, "scopes": scopes, "uninstalled": False},
        )
    return databases.create_document(
        DATABASE_ID, STORES_COLLECTION, ID.unique(),
        data={"shop_domain": shop_domain, "access_token": access_token, "scopes": scopes, "uninstalled": False},
    )


def get_shop_token(shop_domain: str) -> str | None:
    doc = _find_store_doc(shop_domain)
    if not doc or doc.get("uninstalled"):
        return None
    return doc["access_token"]


def mark_shop_uninstalled(shop_domain: str) -> None:
    doc = _find_store_doc(shop_domain)
    if doc:
        databases.update_document(
            DATABASE_ID, STORES_COLLECTION, doc["$id"],
            data={"uninstalled": True, "access_token": ""},
        )


def delete_shop(shop_domain: str) -> None:
    """Used by the shop/redact GDPR webhook — permanently removes the
    store and, since Appwrite relationship attributes can cascade,
    optionally its related documents too (configure on_delete when
    creating the relationship if you want automatic cascade)."""
    doc = _find_store_doc(shop_domain)
    if doc:
        databases.delete_document(DATABASE_ID, STORES_COLLECTION, doc["$id"])


# --- Flows ---

def save_flow(shop_domain: str, intent: str, action: str, url: str, steps: list) -> dict:
    store = _find_store_doc(shop_domain)
    if not store:
        raise ValueError(f"No store record for {shop_domain} — must complete OAuth install first")

    existing = databases.list_documents(
        DATABASE_ID, FLOWS_COLLECTION,
        queries=[Query.equal("intent", intent), Query.equal("action", action), Query.equal("store", store["$id"])],
    )
    steps_json = json.dumps(steps)

    if existing["documents"]:
        return databases.update_document(
            DATABASE_ID, FLOWS_COLLECTION, existing["documents"][0]["$id"],
            data={"url": url, "steps_json": steps_json},
        )
    return databases.create_document(
        DATABASE_ID, FLOWS_COLLECTION, ID.unique(),
        data={"intent": intent, "action": action, "url": url, "steps_json": steps_json, "store": store["$id"]},
    )


def get_flow(shop_domain: str, intent: str, action: str) -> dict | None:
    store = _find_store_doc(shop_domain)
    if not store:
        return None
    result = databases.list_documents(
        DATABASE_ID, FLOWS_COLLECTION,
        queries=[Query.equal("intent", intent), Query.equal("action", action), Query.equal("store", store["$id"])],
    )
    if not result["documents"]:
        return None
    doc = result["documents"][0]
    return {"url": doc["url"], "steps": json.loads(doc["steps_json"])}


# --- Request logs ---

def log_request(shop_domain: str, message: str, status: str, detected_intent=None, detected_action=None, reply: str = "") -> None:
    store = _find_store_doc(shop_domain)
    databases.create_document(
        DATABASE_ID, REQUEST_LOGS_COLLECTION, ID.unique(),
        data={
            "message": message,
            "status": status,
            "detected_intent": detected_intent,
            "detected_action": detected_action,
            "reply": reply,
            "store": store["$id"] if store else None,
        },
    )


# --- Dashboard users (store owner's login) ---

def has_dashboard_user(store_id: str) -> bool:
    result = databases.list_documents(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION,
        queries=[Query.equal("store", store_id)],
    )
    return len(result["documents"]) > 0


def get_dashboard_user_by_email(email: str) -> dict | None:
    normalized = (email or "").strip().lower()
    result = databases.list_documents(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION,
        queries=[Query.equal("email", normalized)],
    )
    return result["documents"][0] if result["documents"] else None


def get_dashboard_user_by_id(user_id: str) -> dict | None:
    try:
        return databases.get_document(DATABASE_ID, DASHBOARD_USERS_COLLECTION, user_id)
    except AppwriteException:
        return None


def create_dashboard_user(store_id: str, email: str, password_hash: str) -> dict:
    normalized = (email or "").strip().lower()
    return databases.create_document(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION, ID.unique(),
        data={"store": store_id, "email": normalized, "password_hash": password_hash},
    )


# --- Agent customization (name, welcome/title, instructions, status, icon, theme, position) ---

def get_customization(store_id: str) -> dict | None:
    result = databases.list_documents(
        DATABASE_ID, CUSTOMIZATIONS_COLLECTION,
        queries=[Query.equal("store", store_id)],
    )
    return result["documents"][0] if result["documents"] else None


def ensure_customization(store_id: str) -> dict:
    """Creates a default customization row if one doesn't exist yet —
    called right after install, same as your old code did in the same
    db.commit() as creating the Store row."""
    existing = get_customization(store_id)
    if existing:
        return existing
    return databases.create_document(
        DATABASE_ID, CUSTOMIZATIONS_COLLECTION, ID.unique(),
        data={
            "store": store_id,
            "agent_name": "AI Assistant",
            "agent_title": "How can I help you today?",
            "theme_color": "#2b2b2b",
            "icon_type": "preset",
            "instructions": (
                "You are a helpful AI shopping assistant for this store. "
                "Be friendly, helpful and concise. Always try to provide "
                "accurate information about products, orders, shipping, "
                "returns and store policies."
            ),
            "status": "active",
            "widget_position": "bottom-right",
        },
    )


def update_customization(store_id: str, **fields) -> dict:
    existing = ensure_customization(store_id)
    return databases.update_document(DATABASE_ID, CUSTOMIZATIONS_COLLECTION, existing["$id"], data=fields)


# --- Features (per-store toggles for what the AI agent can do) ---

_DEFAULT_FEATURES = {
    "product_search": True,
    "recommendations": True,
    "product_filtering": True,
    "warranty": True,
    "cart_editing": True,
    "returns": False,
    "track_orders": True,
}


def get_features(store_id: str) -> dict | None:
    result = databases.list_documents(
        DATABASE_ID, FEATURES_COLLECTION,
        queries=[Query.equal("store", store_id)],
    )
    return result["documents"][0] if result["documents"] else None


def ensure_features(store_id: str) -> dict:
    existing = get_features(store_id)
    if existing:
        return existing
    return databases.create_document(
        DATABASE_ID, FEATURES_COLLECTION, ID.unique(),
        data={"store": store_id, **_DEFAULT_FEATURES},
    )


def update_features(store_id: str, **fields) -> dict:
    existing = ensure_features(store_id)
    return databases.update_document(DATABASE_ID, FEATURES_COLLECTION, existing["$id"], data=fields)


# --- Store info (business name, support email, timezone) ---

def get_store_info(store_id: str) -> dict | None:
    result = databases.list_documents(
        DATABASE_ID, STORE_INFO_COLLECTION,
        queries=[Query.equal("store", store_id)],
    )
    return result["documents"][0] if result["documents"] else None


def ensure_store_info(store_id: str) -> dict:
    existing = get_store_info(store_id)
    if existing:
        return existing
    return databases.create_document(
        DATABASE_ID, STORE_INFO_COLLECTION, ID.unique(),
        data={"store": store_id, "business_name": "", "support_email": "", "timezone": "UTC"},
    )


def update_store_info(store_id: str, **fields) -> dict:
    existing = ensure_store_info(store_id)
    return databases.update_document(DATABASE_ID, STORE_INFO_COLLECTION, existing["$id"], data=fields)


# --- Knowledge base / FAQs ---

def list_faqs(store_id: str) -> list[dict]:
    result = databases.list_documents(
        DATABASE_ID, FAQS_COLLECTION,
        queries=[Query.equal("store", store_id), Query.order_asc("$createdAt")],
    )
    return result["documents"]


def add_faq(store_id: str, question: str, answer: str) -> dict:
    return databases.create_document(
        DATABASE_ID, FAQS_COLLECTION, ID.unique(),
        data={"store": store_id, "question": question, "answer": answer},
    )


def delete_faq(store_id: str, faq_id: str) -> bool:
    """Deletes a FAQ only if it actually belongs to this store, so one
    store owner can't delete another store's FAQ by guessing an id."""
    try:
        doc = databases.get_document(DATABASE_ID, FAQS_COLLECTION, faq_id)
    except AppwriteException:
        return False
    owner_id = doc["store"]["$id"] if isinstance(doc.get("store"), dict) else doc.get("store")
    if owner_id != store_id:
        return False
    databases.delete_document(DATABASE_ID, FAQS_COLLECTION, faq_id)
    return True


# --- Feedback ---

def submit_feedback(store_id: str, message: str) -> dict:
    return databases.create_document(
        DATABASE_ID, FEEDBACK_COLLECTION, ID.unique(),
        data={"store": store_id, "message": message},
    )
"""
ADDITIONS FOR repository_appwrite.py
-------------------------------------
Append these functions to your existing repository_appwrite.py — they
assume the same `databases`, `DATABASE_ID`, `STORES_COLLECTION`,
`DASHBOARD_USERS_COLLECTION`, and `Query`/`ID` imports your file
already has.

REQUIRED NEW APPWRITE ATTRIBUTES (add these in the Appwrite console,
or via setup_collections.py if you're re-running it):

  On the `stores` collection:
    - setup_completed   (boolean, default: false)

  On the `dashboard_users` collection:
    - reset_token           (string, size 255, not required)
    - reset_token_expires   (string, size 64, not required)  -- stored
                              as an ISO datetime string
"""

import secrets
from datetime import datetime, timedelta, timezone


def is_setup_complete(store: dict) -> bool:
    return bool(store.get("setup_completed"))


def mark_setup_complete(store_id: str) -> dict:
    return databases.update_document(
        DATABASE_ID, STORES_COLLECTION, store_id, data={"setup_completed": True}
    )


def create_password_reset_token(email: str) -> str | None:
    """Returns a fresh reset token if the email matches a real dashboard
    user, or None if it doesn't. Callers should show the SAME message
    either way ("if that email exists, we sent a link") — never reveal
    whether an email is registered, that's a real security leak."""
    user = get_dashboard_user_by_email(email)
    if not user:
        return None
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    databases.update_document(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION, user["$id"],
        data={"reset_token": token, "reset_token_expires": expires},
    )
    return token


def get_dashboard_user_by_reset_token(token: str) -> dict | None:
    """Returns the user if the token is valid AND not expired. A token
    older than 1 hour is treated as if it doesn't exist at all."""
    result = databases.list_documents(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION,
        queries=[Query.equal("reset_token", token)],
    )
    docs = result["documents"]
    if not docs:
        return None
    user = docs[0]
    expires = user.get("reset_token_expires")
    if not expires:
        return None
    try:
        if datetime.fromisoformat(expires) < datetime.now(timezone.utc):
            return None  # expired
    except ValueError:
        return None
    return user


def reset_password(user_id: str, new_password_hash: str) -> None:
    """Sets the new password AND invalidates the reset token — a token
    must only ever be usable once."""
    databases.update_document(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION, user_id,
        data={"password_hash": new_password_hash, "reset_token": None, "reset_token_expires": None},
    )
