"""
main.py
-------
App entrypoint. If your repo already has its own main app file, copy the
`app.include_router(...)` calls and the SessionMiddleware setup into it
instead of running this one directly.

Run locally:
    uvicorn main:app --reload --port 8000

For Shopify to reach your callback URL during OAuth, this needs to be
publicly accessible over HTTPS (e.g. via ngrok while developing, or your
real deployed domain in production) — set APP_URL accordingly.
"""

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from database import Base, engine
import models  # noqa: F401 - ensures models are registered before create_all
import shopify_auth
import dashboard
import chatbot_widget
import webhooks

Base.metadata.create_all(bind=engine)

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
app.include_router(chatbot_widget.router)
app.include_router(webhooks.router)
