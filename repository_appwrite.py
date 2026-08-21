"""
repository_appwrite.py
------------------------
Same function names and signatures as the SQLAlchemy repository.py, so
main.py's route handlers don't need to change — only the import line
does: `import repository_appwrite as repo` instead of `import repository`.
"""

import json
from appwrite.query import Query
from appwrite.id import ID
from appwrite.exception import AppwriteException
from appwrite_client import (
    databases, DATABASE_ID, STORES_COLLECTION, FLOWS_COLLECTION,
    REQUEST_LOGS_COLLECTION, CUSTOMIZATIONS_COLLECTION, DASHBOARD_USERS_COLLECTION,
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
    result = databases.list_documents(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION,
        queries=[Query.equal("email", email)],
    )
    return result["documents"][0] if result["documents"] else None


def create_dashboard_user(store_id: str, email: str, password_hash: str) -> dict:
    return databases.create_document(
        DATABASE_ID, DASHBOARD_USERS_COLLECTION, ID.unique(),
        data={"store": store_id, "email": email, "password_hash": password_hash},
    )


# --- Agent customization ---

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
        data={"store": store_id, "agent_name": "AI Assistant", "agent_title": "How can I help you today?", "theme_color": "#2b2b2b"},
    )


def update_customization(store_id: str, **fields) -> dict:
    existing = ensure_customization(store_id)
    return databases.update_document(DATABASE_ID, CUSTOMIZATIONS_COLLECTION, existing["$id"], data=fields)
