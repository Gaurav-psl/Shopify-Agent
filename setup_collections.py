"""
setup_collections.py
---------------------
Run this to create your database structure in Appwrite. Equivalent to
Base.metadata.create_all() in SQLAlchemy, but explicit — you define
every attribute (column) and relationship as a separate API call.

Safe to re-run: every create_* call is wrapped so a 409 "already exists"
response is printed and skipped instead of crashing the whole script.

Also handles Appwrite's ASYNC attribute creation automatically: an
attribute (e.g. "email") briefly stays in "processing" status right
after creation, and any index or relationship that references it will
fail with "attribute ... is not yet available" if it runs too soon.
Rather than making you manually re-trigger a Render build every time
that happens, this script retries automatically with a short delay.

Run: python setup_collections.py
"""

import time

from appwrite_client import databases, DATABASE_ID, STORES_COLLECTION, FLOWS_COLLECTION, REQUEST_LOGS_COLLECTION, CUSTOMIZATIONS_COLLECTION, DASHBOARD_USERS_COLLECTION
from appwrite.exception import AppwriteException
from appwrite.permission import Permission
from appwrite.role import Role
from appwrite.enums.relationship_type import RelationshipType
from appwrite.enums.index_type import IndexType

NOT_YET_AVAILABLE_RETRIES = 12   # up to ~1 minute of waiting per step
NOT_YET_AVAILABLE_DELAY_SECONDS = 5


def _run(step_description, fn, *args, **kwargs):
    """Call fn(*args, **kwargs).
    - 409 "already exists"      -> treated as success, skipped
    - "not yet available"       -> Appwrite is still processing a
                                    dependency (e.g. an attribute this
                                    index/relationship references) —
                                    wait and retry a few times
    - anything else             -> re-raised, stops the script
    """
    print(step_description)
    attempt = 0
    while True:
        try:
            fn(*args, **kwargs)
            return
        except AppwriteException as e:
            message = str(e).lower()
            if getattr(e, "code", None) == 409 or "already exists" in message:
                print("  -> already exists, skipped")
                return
            if "not yet available" in message or "processing" in message:
                attempt += 1
                if attempt > NOT_YET_AVAILABLE_RETRIES:
                    print(f"  -> FAILED (still not available after {attempt - 1} retries): {e}")
                    raise
                print(f"  -> not ready yet, waiting {NOT_YET_AVAILABLE_DELAY_SECONDS}s and retrying ({attempt}/{NOT_YET_AVAILABLE_RETRIES})...")
                time.sleep(NOT_YET_AVAILABLE_DELAY_SECONDS)
                continue
            print(f"  -> FAILED: {e}")
            raise


def setup():
    # ---- Database ----
    _run(f"Database '{DATABASE_ID}'...", databases.create, database_id=DATABASE_ID, name="shopify_agent_db")

    # ---- Stores collection (equivalent of your Store table) ----
    _run(
        f"Collection '{STORES_COLLECTION}'...",
        databases.create_collection,
        database_id=DATABASE_ID,
        collection_id=STORES_COLLECTION,
        name="Stores",
        # Only your server (via API key) can write; nobody can read/write
        # directly from the client, since this holds access tokens.
        permissions=[],
    )
    _run("  attribute 'shop_domain'...", databases.create_string_attribute, DATABASE_ID, STORES_COLLECTION, "shop_domain", size=255, required=True)
    _run("  attribute 'access_token'...", databases.create_string_attribute, DATABASE_ID, STORES_COLLECTION, "access_token", size=500, required=True)
    _run("  attribute 'scopes'...", databases.create_string_attribute, DATABASE_ID, STORES_COLLECTION, "scopes", size=500, required=False)
    _run("  attribute 'uninstalled'...", databases.create_boolean_attribute, DATABASE_ID, STORES_COLLECTION, "uninstalled", required=False, default=False)
    _run("  attribute 'installed_at'...", databases.create_datetime_attribute, DATABASE_ID, STORES_COLLECTION, "installed_at", required=False)
    # A unique index — Appwrite's equivalent of unique=True on a SQLAlchemy column
    _run("  index 'unique_shop_domain'...", databases.create_index, DATABASE_ID, STORES_COLLECTION, key="unique_shop_domain", type=IndexType.UNIQUE, attributes=["shop_domain"])

    # ---- Flows collection ----
    _run(f"Collection '{FLOWS_COLLECTION}'...", databases.create_collection, database_id=DATABASE_ID, collection_id=FLOWS_COLLECTION, name="Flows", permissions=[])
    _run("  attribute 'intent'...", databases.create_string_attribute, DATABASE_ID, FLOWS_COLLECTION, "intent", size=100, required=True)
    _run("  attribute 'action'...", databases.create_string_attribute, DATABASE_ID, FLOWS_COLLECTION, "action", size=100, required=True)
    _run("  attribute 'url'...", databases.create_string_attribute, DATABASE_ID, FLOWS_COLLECTION, "url", size=1000, required=True)
    _run("  attribute 'steps_json'...", databases.create_string_attribute, DATABASE_ID, FLOWS_COLLECTION, "steps_json", size=20000, required=True)  # stored as JSON text

    # ---- Request logs collection ----
    _run(f"Collection '{REQUEST_LOGS_COLLECTION}'...", databases.create_collection, database_id=DATABASE_ID, collection_id=REQUEST_LOGS_COLLECTION, name="RequestLogs", permissions=[])
    _run("  attribute 'message'...", databases.create_string_attribute, DATABASE_ID, REQUEST_LOGS_COLLECTION, "message", size=2000, required=True)
    _run("  attribute 'detected_intent'...", databases.create_string_attribute, DATABASE_ID, REQUEST_LOGS_COLLECTION, "detected_intent", size=100, required=False)
    _run("  attribute 'detected_action'...", databases.create_string_attribute, DATABASE_ID, REQUEST_LOGS_COLLECTION, "detected_action", size=100, required=False)
    _run("  attribute 'status'...", databases.create_string_attribute, DATABASE_ID, REQUEST_LOGS_COLLECTION, "status", size=50, required=True)
    _run("  attribute 'reply'...", databases.create_string_attribute, DATABASE_ID, REQUEST_LOGS_COLLECTION, "reply", size=2000, required=False)

    # ---- Agent customizations collection ----
    _run(f"Collection '{CUSTOMIZATIONS_COLLECTION}'...", databases.create_collection, database_id=DATABASE_ID, collection_id=CUSTOMIZATIONS_COLLECTION, name="AgentCustomizations", permissions=[])
    _run("  attribute 'agent_name'...", databases.create_string_attribute, DATABASE_ID, CUSTOMIZATIONS_COLLECTION, "agent_name", size=100, required=False, default="AI Assistant")
    _run("  attribute 'agent_title'...", databases.create_string_attribute, DATABASE_ID, CUSTOMIZATIONS_COLLECTION, "agent_title", size=150, required=False)
    _run("  attribute 'theme_color'...", databases.create_string_attribute, DATABASE_ID, CUSTOMIZATIONS_COLLECTION, "theme_color", size=20, required=False, default="#2b2b2b")
    _run("  attribute 'icon_type'...", databases.create_string_attribute, DATABASE_ID, CUSTOMIZATIONS_COLLECTION, "icon_type", size=20, required=False, default="preset")
    _run("  attribute 'custom_icon_url'...", databases.create_string_attribute, DATABASE_ID, CUSTOMIZATIONS_COLLECTION, "custom_icon_url", size=500, required=False)

    # ---- Dashboard users collection (store owner's login, separate from
    # their Shopify login) ----
    _run(f"Collection '{DASHBOARD_USERS_COLLECTION}'...", databases.create_collection, database_id=DATABASE_ID, collection_id=DASHBOARD_USERS_COLLECTION, name="DashboardUsers", permissions=[])
    _run("  attribute 'email'...", databases.create_email_attribute, DATABASE_ID, DASHBOARD_USERS_COLLECTION, "email", required=True)
    _run("  attribute 'password_hash'...", databases.create_string_attribute, DATABASE_ID, DASHBOARD_USERS_COLLECTION, "password_hash", size=255, required=True)
    _run("  index 'unique_email'...", databases.create_index, DATABASE_ID, DASHBOARD_USERS_COLLECTION, key="unique_email", type=IndexType.UNIQUE, attributes=["email"])

    # ---- Relationships — Appwrite's version of a foreign key ----
    # These link Flows, RequestLogs, and Customizations each back to a
    # Store, exactly like store_id = ForeignKey("stores.id") in SQLAlchemy.
    _run(
        "  relationship 'flows.store' <-> 'stores.flows'...",
        databases.create_relationship_attribute,
        database_id=DATABASE_ID,
        collection_id=FLOWS_COLLECTION,
        related_collection_id=STORES_COLLECTION,
        type=RelationshipType.MANYTOONE,  # many flows belong to one store
        two_way=True,
        key="store",
        two_way_key="flows",
    )
    _run(
        "  relationship 'request_logs.store' <-> 'stores.request_logs'...",
        databases.create_relationship_attribute,
        database_id=DATABASE_ID,
        collection_id=REQUEST_LOGS_COLLECTION,
        related_collection_id=STORES_COLLECTION,
        type=RelationshipType.MANYTOONE,
        two_way=True,
        key="store",
        two_way_key="request_logs",
    )
    _run(
        "  relationship 'agent_customizations.store' <-> 'stores.customization'...",
        databases.create_relationship_attribute,
        database_id=DATABASE_ID,
        collection_id=CUSTOMIZATIONS_COLLECTION,
        related_collection_id=STORES_COLLECTION,
        type=RelationshipType.ONETOONE,   # one customization row per store
        two_way=True,
        key="store",
        two_way_key="customization",
    )
    _run(
        "  relationship 'dashboard_users.store' <-> 'stores.dashboard_user'...",
        databases.create_relationship_attribute,
        database_id=DATABASE_ID,
        collection_id=DASHBOARD_USERS_COLLECTION,
        related_collection_id=STORES_COLLECTION,
        type=RelationshipType.ONETOONE,   # one dashboard login per store
        two_way=True,
        key="store",
        two_way_key="dashboard_user",
    )

    print("\nDone. If anything above said 'FAILED' (not 'already exists, skipped'),\n"
          "wait ~10-15 seconds for Appwrite to finish processing the prior attribute\n"
          "and run this script again — it will pick up from there safely.")


if __name__ == "__main__":
    setup()
