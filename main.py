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
import re
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from nicegui import ui

import shopify_auth
import webhooks
import dashboard_nicegui  # noqa: F401 — importing this registers all @ui.page routes below
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
app.include_router(webhooks.router)
# app.include_router(chatbot_widget.router)  # add back once migrated

# --------------------------------------------------------------------
# NiceGUI mount — must come LAST among routers/mounts above.
# This attaches dashboard_nicegui.py's @ui.page("/dashboard/...") routes
# (plus NiceGUI's own internal static/socket.io routes) onto `app`.
# --------------------------------------------------------------------
ui.run_with(app, storage_secret=os.environ.get("SESSION_SECRET", "change-me-in-production"))


# --------------------------------------------------------------------
# Cookie fix for Shopify's embedded iframe
# --------------------------------------------------------------------
# Your dashboard loads inside an <iframe> in Shopify Admin, on a
# different domain than your app. Browsers treat any cookie your app
# sets in that context as a THIRD-PARTY cookie, and by default refuse
# to send it back on the next request unless it's explicitly marked
# `SameSite=None; Secure`. Without this, login "silently" fails —
# the session cookie gets set, but the browser drops it, so the very
# next page load looks logged-out again.
#
# Neither Starlette's SessionMiddleware nor NiceGUI's internal storage
# middleware expose a public option to set this, so this rewrites every
# outgoing Set-Cookie header directly at the ASGI level, after
# everything else (including NiceGUI's own middleware) has run.
#
# Must be added AFTER ui.run_with(...) above, so it wraps the outside
# of everything and can rewrite cookies set by any layer, including
# NiceGUI's.
#
# Requires HTTPS in production — `Secure` cookies are refused entirely
# over plain HTTP. If you're testing this locally over http://, this
# middleware will still add the flag, but the browser will discard the
# cookie anyway; that's expected and not a bug in this middleware.
class ForceCookieSameSiteNone:
    def __init__(self, asgi_app):
        self.asgi_app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.asgi_app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                new_headers = []
                for key, value in message.get("headers", []):
                    if key.lower() == b"set-cookie":
                        cookie = value.decode("latin-1")
                        cookie = re.sub(r"(?i);?\s*samesite=\w+", "", cookie)  # strip any existing SameSite
                        cookie += "; SameSite=None"
                        if "secure" not in cookie.lower():
                            cookie += "; Secure"
                        value = cookie.encode("latin-1")
                    new_headers.append((key, value))
                message["headers"] = new_headers
            await send(message)

        await self.asgi_app(scope, receive, send_wrapper)


app.add_middleware(ForceCookieSameSiteNone)
