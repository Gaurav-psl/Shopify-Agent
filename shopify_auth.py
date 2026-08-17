"""
shopify_auth.py
----------------
The Shopify OAuth "install app" flow — rebuilt from your old main.py's
auth logic (make_state/verify_state/verify_hmac are carried over almost
unchanged), with two upgrades:

1. Access tokens are saved to the DATABASE (models.Store), not an
   in-memory dict. Your old `token_store = {}` was wiped every time
   Render restarted the dyno, silently "uninstalling" every store from
   your app's point of view.
2. After a successful install, the merchant is handed off to your
   dashboard (signup for first-time installs, login for returning ones)
   instead of just seeing a static "installed successfully" text page.

Three routes, same as before:

GET /                (Shopify sends merchants here — your "App URL" in
                       the Partner Dashboard — when they click Install,
                       or when they open the app from their admin)
    Reads ?shop=xxx.myshopify.com. If we already have a valid token for
    that shop, skip straight to the dashboard. Otherwise start OAuth.

GET /auth?shop=xxx.myshopify.com
    Redirects to Shopify's permission-grant screen for the scopes this
    app needs (SHOPIFY_SCOPES).

GET /auth/callback
    Shopify redirects back here after approval. Verifies state (CSRF)
    and HMAC (request really came from Shopify), exchanges the temporary
    `code` for a permanent Admin API access token, and saves it.

------------------------------------------------------------------------
Required environment variables
------------------------------------------------------------------------
SHOPIFY_API_KEY       - Client ID, from the Partner Dashboard
SHOPIFY_API_SECRET    - Client secret, from the Partner Dashboard
SHOPIFY_SCOPES        - comma-separated, e.g.
                        "read_products,read_orders,write_orders,read_customers"
APP_URL               - your deployed app's public https URL, e.g.
                        "https://your-app.onrender.com"  (no trailing slash)
                        NOTE: your old file called this HOST — renamed to
                        APP_URL to match the rest of the upgraded repo.
                        Update your Render env var name if needed (or add
                        both, see the compatibility shim below).
"""

import base64
import hashlib
import hmac
import os
import time
import urllib.parse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, PlainTextResponse
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Store, AgentCustomization

router = APIRouter(tags=["shopify-auth"])

SHOPIFY_API_KEY = (os.environ.get("SHOPIFY_API_KEY") or "").strip()
SHOPIFY_API_SECRET = (os.environ.get("SHOPIFY_API_SECRET") or "").strip()
SHOPIFY_SCOPES = (
    os.environ.get("SHOPIFY_SCOPES")
    or os.environ.get("SCOPES")  # falls back to your old var name if still set
    or "read_products,read_orders,write_orders,read_customers"
).strip()

# Compatibility shim: your old Render env var was called HOST — this
# accepts either name so you don't have to rename anything on Render
# right now. New installs should standardize on APP_URL.
APP_URL = (os.environ.get("APP_URL") or os.environ.get("HOST") or "http://localhost:8000").strip().rstrip("/")

STATE_MAX_AGE_SECONDS = 600


# --------------------------------------------------------------------
# Carried over from your old main.py, unchanged.
# --------------------------------------------------------------------
def make_state(shop: str) -> str:
    timestamp = str(int(time.time()))
    payload = f"{shop}:{timestamp}"
    signature = hmac.new(SHOPIFY_API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}:{signature}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def verify_state(state: str, shop: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(state.encode()).decode()
        shop_in_state, timestamp, signature = raw.split(":")
    except Exception as e:
        print(f"[state check] FAILED to decode state: {e}. Raw state received: {state!r}")
        return False
    if shop_in_state != shop:
        print(f"[state check] SHOP MISMATCH. In state: {shop_in_state!r}, from query: {shop!r}")
        return False
    expected_payload = f"{shop_in_state}:{timestamp}"
    expected_signature = hmac.new(SHOPIFY_API_SECRET.encode(), expected_payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        print(f"[state check] SIGNATURE MISMATCH. Expected {expected_signature}, got {signature}")
        return False
    age = int(time.time()) - int(timestamp)
    if age > STATE_MAX_AGE_SECONDS:
        print(f"[state check] EXPIRED. Age was {age} seconds (max {STATE_MAX_AGE_SECONDS})")
        return False
    print(f"[state check] OK for shop {shop}")
    return True


def verify_hmac(params: dict) -> bool:
    hmac_value = params.get("hmac")
    if not hmac_value:
        return False
    rest = {k: v for k, v in params.items() if k != "hmac"}
    message = "&".join(f"{k}={v}" for k, v in sorted(rest.items()))
    generated_hash = hmac.new(SHOPIFY_API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(generated_hash, hmac_value)


# --------------------------------------------------------------------
# Entry point — Shopify sends merchants here ("Application URL" in the
# Partner Dashboard). Decide whether they need OAuth or can go straight
# to the dashboard.
# --------------------------------------------------------------------
@router.get("/")
async def app_entry(request: Request):
    shop = request.query_params.get("shop")
    if not shop:
        # Not opened from Shopify — show a plain status page instead of
        # erroring, so your own health checks / manual visits still work.
        return {
            "status": "running",
            "message": "Shopify AI Agent backend is running.",
            "env_check": {
                "SHOPIFY_API_KEY_set": bool(SHOPIFY_API_KEY),
                "SHOPIFY_API_SECRET_set": bool(SHOPIFY_API_SECRET),
                "APP_URL_set": bool(APP_URL),
            },
        }

    db: Session = SessionLocal()
    try:
        store = db.query(Store).filter_by(shop_domain=shop, uninstalled=False).first()
    finally:
        db.close()

    if store and store.access_token:
        return RedirectResponse(f"/dashboard?shop={urllib.parse.quote(shop)}")

    return RedirectResponse(f"/auth?shop={urllib.parse.quote(shop)}")


@router.get("/health")
def health():
    return {"status": "healthy"}


# --------------------------------------------------------------------
# Step 1: redirect the merchant to Shopify's permission screen
# --------------------------------------------------------------------
@router.get("/auth")
def start_auth(shop: str):
    if not SHOPIFY_API_KEY or not SHOPIFY_API_SECRET or not APP_URL:
        return PlainTextResponse(
            "Missing required environment variables. Check SHOPIFY_API_KEY, "
            "SHOPIFY_API_SECRET, and APP_URL (or HOST) on Render.",
            status_code=500,
        )

    state = make_state(shop)
    print(f"[auth start] shop={shop} generated state={state!r}")
    redirect_uri = f"{APP_URL}/auth/callback"
    query = urllib.parse.urlencode(
        {
            "client_id": SHOPIFY_API_KEY,
            "scope": SHOPIFY_SCOPES,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    install_url = f"https://{shop}/admin/oauth/authorize?{query}"
    return RedirectResponse(install_url)


# --------------------------------------------------------------------
# Step 2: Shopify redirects back here with a temporary `code`
# --------------------------------------------------------------------
@router.get("/auth/callback")
async def auth_callback(request: Request):
    params = dict(request.query_params)
    shop = params.get("shop")
    code = params.get("code")
    state = params.get("state")

    if not shop or not code:
        return PlainTextResponse("Missing shop or code in callback", status_code=400)

    print(f"[callback] shop={shop!r} state received={state!r}")

    if not state or not verify_state(state, shop):
        return PlainTextResponse("Invalid state parameter — possible CSRF attempt", status_code=403)

    if not verify_hmac(params):
        return PlainTextResponse("HMAC validation failed", status_code=403)

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://{shop}/admin/oauth/access_token",
            json={
                "client_id": SHOPIFY_API_KEY,
                "client_secret": SHOPIFY_API_SECRET,
                "code": code,
            },
        )

    if response.status_code != 200:
        return PlainTextResponse(f"Failed to exchange code for access token: {response.text}", status_code=500)

    data = response.json()
    access_token = data.get("access_token")
    granted_scopes = data.get("scope", "")
    print(f"Installed on {shop}. Scopes: {granted_scopes}. Token starts with: {access_token[:10]}...")

    # --- This is the upgrade over your old file: save to the DB instead
    # --- of an in-memory dict, and figure out whether this is a brand
    # --- new store (needs dashboard signup) or a reinstall (goes to login).
    db: Session = SessionLocal()
    try:
        store = db.query(Store).filter_by(shop_domain=shop).first()
        if store:
            store.access_token = access_token
            store.scopes = granted_scopes
            store.uninstalled = False
        else:
            store = Store(shop_domain=shop, access_token=access_token, scopes=granted_scopes)
            db.add(store)
        db.commit()
        db.refresh(store)

        # Make sure a customization row exists so the dashboard has
        # sensible defaults to show right away.
        if not db.query(AgentCustomization).filter_by(store_id=store.id).first():
            db.add(AgentCustomization(store_id=store.id))
            db.commit()

        is_new_store = store.dashboard_user is None
    finally:
        db.close()

    destination = (
        f"/dashboard/signup?shop={urllib.parse.quote(shop)}"
        if is_new_store
        else f"/dashboard/login?shop={urllib.parse.quote(shop)}"
    )
    return RedirectResponse(destination)
