"""
main.py
-------
App entrypoint. No more Base.metadata.create_all() — Appwrite collections
are created once via setup_collections.py, not on every app startup.

Run locally:
    uvicorn main:app --reload --port 8000

Run setup_collections.py once, the first time you connect a new Appwrite
project, before starting the app.
"""

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from nicegui import ui

import dashboard_nicegui
import shopify_auth
import dashboard
import webhooks
# import chatbot_widget  # NOTE: still needs migrating off SQLAlchemy — see below

app = FastAPI(title="Shopify Chatbot Plugin")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "change-me-in-production"),
    same_site="lax",
    https_only=True,
)

os.makedirs("static/uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(shopify_auth.router)
app.include_router(dashboard.router)
app.include_router(webhooks.router)
# app.include_router(chatbot_widget.router)  # add back once migrated

ui.run_with(app, storage_secret=os.environ.get("SESSION_SECRET", "change-me-in-production"))
