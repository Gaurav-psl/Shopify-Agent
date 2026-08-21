"""
setup_collections.py
---------------------
Run this ONCE to create your database structure in Appwrite. Equivalent
to Base.metadata.create_all() in SQLAlchemy, but explicit — you define
every attribute (column) and relationship as a separate API call.

Run: python setup_collections.py
"""

from appwrite_client import databases, DATABASE_ID, STORES_COLLECTION, FLOWS_COLLECTION, REQUEST_LOGS_COLLECTION, CUSTOMIZATIONS_COLLECTION, DASHBOARD_USERS_COLLECTION
from appwrite.permission import Permission
from appwrite.role import Role
from appwrite.enums.relationship_type import RelationshipType
from appwrite.enums.databases_index_type import DatabasesIndexType

def setup():
    # ---- Database ----
    databases.create(database_id=DATABASE_ID, name="shopify_agent_db")

    # ---- Stores collection (equivalent of your Store table) ----
    databases.create_collection(
        database_id=DATABASE_ID,
        collection_id=STORES_COLLECTION,
        name="Stores",
        # Only your server (via API key) can write; nobody can read/write
        # directly from the client, since this holds access tokens.
        permissions=[],
    )
    databases.create_string_attribute(DATABASE_ID, STORES_COLLECTION, "shop_domain", size=255, required=True)
    databases.create_string_attribute(DATABASE_ID, STORES_COLLECTION, "access_token", size=500, required=True)
    databases.create_string_attribute(DATABASE_ID, STORES_COLLECTION, "scopes", size=500, required=False)
    databases.create_boolean_attribute(DATABASE_ID, STORES_COLLECTION, "uninstalled", required=False, default=False)
    databases.create_datetime_attribute(DATABASE_ID, STORES_COLLECTION, "installed_at", required=False)
    # A unique index — Appwrite's equivalent of unique=True on a SQLAlchemy column
    databases.create_index(DATABASE_ID, STORES_COLLECTION, key="unique_shop_domain", type=DatabasesIndexType.UNIQUE, attributes=["shop_domain"])

    # ---- Flows collection ----
    databases.create_collection(database_id=DATABASE_ID, collection_id=FLOWS_COLLECTION, name="Flows", permissions=[])
    databases.create_string_attribute(DATABASE_ID, FLOWS_COLLECTION, "intent", size=100, required=True)
    databases.create_string_attribute(DATABASE_ID, FLOWS_COLLECTION, "action", size=100, required=True)
    databases.create_string_attribute(DATABASE_ID, FLOWS_COLLECTION, "url", size=1000, required=True)
    databases.create_string_attribute(DATABASE_ID, FLOWS_COLLECTION, "steps_json", size=20000, required=True)  # stored as JSON text

    # ---- Request logs collection ----
    databases.create_collection(database_id=DATABASE_ID, collection_id=REQUEST_LOGS_COLLECTION, name="RequestLogs", permissions=[])
    databases.create_string_attribute(DATABASE_ID, REQUEST_LOGS_COLLECTION, "message", size=2000, required=True)
    databases.create_string_attribute(DATABASE_ID, REQUEST_LOGS_COLLECTION, "detected_intent", size=100, required=False)
    databases.create_string_attribute(DATABASE_ID, REQUEST_LOGS_COLLECTION, "detected_action", size=100, required=False)
    databases.create_string_attribute(DATABASE_ID, REQUEST_LOGS_COLLECTION, "status", size=50, required=True)
    databases.create_string_attribute(DATABASE_ID, REQUEST_LOGS_COLLECTION, "reply", size=2000, required=False)

    # ---- Agent customizations collection ----
    databases.create_collection(database_id=DATABASE_ID, collection_id=CUSTOMIZATIONS_COLLECTION, name="AgentCustomizations", permissions=[])
    databases.create_string_attribute(DATABASE_ID, CUSTOMIZATIONS_COLLECTION, "agent_name", size=100, required=False, default="AI Assistant")
    databases.create_string_attribute(DATABASE_ID, CUSTOMIZATIONS_COLLECTION, "agent_title", size=150, required=False)
    databases.create_string_attribute(DATABASE_ID, CUSTOMIZATIONS_COLLECTION, "theme_color", size=20, required=False, default="#2b2b2b")
    databases.create_string_attribute(DATABASE_ID, CUSTOMIZATIONS_COLLECTION, "icon_type", size=20, required=False, default="preset")
    databases.create_string_attribute(DATABASE_ID, CUSTOMIZATIONS_COLLECTION, "custom_icon_url", size=500, required=False)

    # ---- Dashboard users collection (store owner's login, separate from
    # their Shopify login) ----
    databases.create_collection(database_id=DATABASE_ID, collection_id=DASHBOARD_USERS_COLLECTION, name="DashboardUsers", permissions=[])
    databases.create_email_attribute(DATABASE_ID, DASHBOARD_USERS_COLLECTION, "email", required=True)
    databases.create_string_attribute(DATABASE_ID, DASHBOARD_USERS_COLLECTION, "password_hash", size=255, required=True)
    databases.create_index(DATABASE_ID, DASHBOARD_USERS_COLLECTION, key="unique_email", type=DatabasesIndexType.UNIQUE, attributes=["email"])

    # ---- Relationships — Appwrite's version of a foreign key ----
    # These link Flows, RequestLogs, and Customizations each back to a
    # Store, exactly like store_id = ForeignKey("stores.id") in SQLAlchemy.
    databases.create_relationship_attribute(
        database_id=DATABASE_ID,
        collection_id=FLOWS_COLLECTION,
        related_collection_id=STORES_COLLECTION,
        type=RelationshipType.MANYTOONE,  # many flows belong to one store
        two_way=True,
        key="store",
        two_way_key="flows",
    )
    databases.create_relationship_attribute(
        database_id=DATABASE_ID,
        collection_id=REQUEST_LOGS_COLLECTION,
        related_collection_id=STORES_COLLECTION,
        type=RelationshipType.MANYTOONE,
        two_way=True,
        key="store",
        two_way_key="request_logs",
    )
    databases.create_relationship_attribute(
        database_id=DATABASE_ID,
        collection_id=CUSTOMIZATIONS_COLLECTION,
        related_collection_id=STORES_COLLECTION,
        type=RelationshipType.ONETOONE,   # one customization row per store
        two_way=True,
        key="store",
        two_way_key="customization",
    )
    databases.create_relationship_attribute(
        database_id=DATABASE_ID,
        collection_id=DASHBOARD_USERS_COLLECTION,
        related_collection_id=STORES_COLLECTION,
        type=RelationshipType.ONETOONE,   # one dashboard login per store
        two_way=True,
        key="store",
        two_way_key="dashboard_user",
    )

    print("Database structure created successfully.")


if __name__ == "__main__":
    setup()
