"""
webhooks.py
-----------
Shopify webhooks this app needs to register (Partner Dashboard > App
setup > Webhooks, or via the Admin API `webhooks.json` on install):

  POST /webhooks/app/uninstalled
      Shopify calls this the moment a merchant uninstalls the app.
      We mark the store `uninstalled=True` so the widget stops
      responding for it and the (now-revoked) token is never used
      again. REQUIRED — without this, an uninstalled store's shoppers
      would still see a chatbot trying to call a dead token.

  POST /webhooks/customers/data_request
  POST /webhooks/customers/redact
  POST /webhooks/shop/redact
      Shopify's mandatory GDPR webhooks — every public app must expose
      these three endpoints to be approved for the App Store, even if
      you don't store customer PII beyond the order tags this app
      already writes. They're stubbed to 200 OK here; fill in real
      data export/delete logic if you start storing more.

Every request is HMAC-signed by Shopify with SHOPIFY_API_SECRET —
verify_webhook_hmac() checks that before trusting the payload.
"""

import base64
import hashlib
import hmac
import os

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Store

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SHOPIFY_API_SECRET = os.environ.get("SHOPIFY_API_SECRET", "")


async def _verify(request: Request) -> bytes | None:
    body = await request.body()
    sent_hmac = request.headers.get("X-Shopify-Hmac-Sha256", "")
    digest = hmac.new(SHOPIFY_API_SECRET.encode(), body, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode()
    if not hmac.compare_digest(computed, sent_hmac):
        return None
    return body


@router.post("/app/uninstalled")
async def app_uninstalled(request: Request):
    body = await _verify(request)
    if body is None:
        return PlainTextResponse("Invalid HMAC", status_code=401)

    shop = request.headers.get("X-Shopify-Shop-Domain")
    db: Session = SessionLocal()
    try:
        store = db.query(Store).filter_by(shop_domain=shop).first()
        if store:
            store.uninstalled = True
            store.access_token = ""  # the token is revoked by Shopify anyway; drop it
            db.commit()
    finally:
        db.close()
    return PlainTextResponse("ok")


@router.post("/customers/data_request")
async def customers_data_request(request: Request):
    body = await _verify(request)
    if body is None:
        return PlainTextResponse("Invalid HMAC", status_code=401)
    # This app doesn't retain shopper-identifying chat data beyond the
    # order tags/notes written for warranty claims. Add real export
    # logic here if that changes.
    return PlainTextResponse("ok")


@router.post("/customers/redact")
async def customers_redact(request: Request):
    body = await _verify(request)
    if body is None:
        return PlainTextResponse("Invalid HMAC", status_code=401)
    return PlainTextResponse("ok")


@router.post("/shop/redact")
async def shop_redact(request: Request):
    body = await _verify(request)
    if body is None:
        return PlainTextResponse("Invalid HMAC", status_code=401)
    shop = request.headers.get("X-Shopify-Shop-Domain")
    db: Session = SessionLocal()
    try:
        store = db.query(Store).filter_by(shop_domain=shop).first()
        if store:
            db.delete(store)
            db.commit()
    finally:
        db.close()
    return PlainTextResponse("ok")
