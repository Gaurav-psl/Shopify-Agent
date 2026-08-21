"""
appwrite_client.py
-------------------
Appwrite connection setup — the equivalent of your database.py, but
there's no engine/session lifecycle to manage. You configure the client
once, and every request just calls the Databases service directly.

Required environment variables (from your Appwrite Project Settings):
  APPWRITE_ENDPOINT     - e.g. "https://cloud.appwrite.io/v1"
  APPWRITE_PROJECT_ID   - your Project ID
  APPWRITE_API_KEY      - a server-side API key with databases.* scopes
  APPWRITE_DATABASE_ID  - the Database ID you created inside the project
"""

import os
from appwrite.client import Client
from appwrite.services.databases import Databases

client = Client()
client.set_endpoint(os.environ.get("APPWRITE_ENDPOINT", "https://cloud.appwrite.io/v1"))
client.set_project(os.environ.get("APPWRITE_PROJECT_ID", ""))
client.set_key(os.environ.get("APPWRITE_API_KEY", ""))

databases = Databases(client)

DATABASE_ID = os.environ.get("APPWRITE_DATABASE_ID", "")

# Collection IDs — set these to whatever you name them in the Appwrite
# console (or create them programmatically, see setup_collections.py)
STORES_COLLECTION = "stores"
FLOWS_COLLECTION = "flows"
REQUEST_LOGS_COLLECTION = "request_logs"
CUSTOMIZATIONS_COLLECTION = "agent_customizations"
DASHBOARD_USERS_COLLECTION = "dashboard_users"
