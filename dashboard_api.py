"""
dashboard_api.py
-----------------
A JSON API version of dashboard.py's routes, for the React frontend
(the rich RenderLink dashboard) to call instead of getting back
rendered HTML.

Reuses the exact same repository_appwrite functions and the exact same
session-cookie auth (Starlette's SessionMiddleware) as dashboard.py —
only the response format changes (JSON instead of HTML strings).

NEW IN THIS VERSION — routes to back every page of the rich UI:
  GET  /api/dashboard/me              overview bundle (store, agent,
                                       appearance, features, store_info)
  POST /api/dashboard/agent           save name/welcome/instructions/status
  GET  /api/dashboard/appearance      theme/position/icon (subset of /me)
  POST /api/dashboard/appearance      save theme/position/custom icon
  GET  /api/dashboard/features        feature toggles (subset of /me)
  POST /api/dashboard/features        save feature toggles
  GET  /api/dashboard/store-info      business name/support email/timezone
  POST /api/dashboard/store-info      save store info
  GET  /api/dashboard/faqs            list FAQs
  POST /api/dashboard/faqs            add a FAQ
  DELETE /api/dashboard/faqs/{id}     delete a FAQ
  POST /api/dashboard/feedback        submit feedback
"""

import os
import shutil
import uuid

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
import bcrypt

import repository_appwrite as repo

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-api"])

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "static/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

FEATURE_KEYS = (
    "product_search", "recommendations", "product_filtering",
    "warranty", "cart_editing", "returns", "track_orders",
)


def _get_session_store(request: Request) -> dict | None:
    store_id = request.session.get("store_id")
    if not store_id:
        return None
    store = repo.get_store_by_id(store_id)
    if store and not store.get("uninstalled"):
        return store
    return None


def _agent_shape(cfg: dict) -> dict:
    return {
        "name": cfg.get("agent_name", "AI Assistant"),
        "welcome": cfg.get("agent_title", "How can I help you today?"),
        "instructions": cfg.get("instructions", ""),
        "status": cfg.get("status", "active"),
    }


def _appearance_shape(cfg: dict) -> dict:
    return {
        "theme_color": cfg.get("theme_color", "#2b2b2b"),
        "widget_position": cfg.get("widget_position", "bottom-right"),
        "icon_type": cfg.get("icon_type", "preset"),
        "custom_icon_url": cfg.get("custom_icon_url", ""),
    }


def _features_shape(doc: dict) -> dict:
    return {key: bool(doc.get(key, False)) for key in FEATURE_KEYS}


def _store_info_shape(doc: dict) -> dict:
    return {
        "business_name": doc.get("business_name", ""),
        "support_email": doc.get("support_email", ""),
        "timezone": doc.get("timezone", "UTC"),
    }


def _faq_shape(doc: dict) -> dict:
    return {"id": doc["$id"], "question": doc.get("question", ""), "answer": doc.get("answer", "")}


# --------------------------------------------------------------------
# Signup
# --------------------------------------------------------------------
@router.post("/signup")
async def signup(request: Request, shop: str = Form(...), email: str = Form(...), password: str = Form(...)):
    store = repo.get_store(shop)
    if not store:
        return JSONResponse({"error": "Store not found — please reinstall the app."}, status_code=404)

    if repo.has_dashboard_user(store["$id"]):
        return JSONResponse({"error": "already_signed_up", "message": "This store already has a dashboard login."}, status_code=409)

    if repo.get_dashboard_user_by_email(email):
        return JSONResponse({"error": "That email is already in use."}, status_code=409)

    user = repo.create_dashboard_user(store["$id"], email, bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode())

    request.session["store_id"] = store["$id"]
    request.session["user_id"] = user["$id"]
    request.session["email"] = user["email"]
    return {"status": "ok", "shop": store["shop_domain"]}


# --------------------------------------------------------------------
# Login / Logout
# --------------------------------------------------------------------
@router.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = repo.get_dashboard_user_by_email(email)
    if not user or not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return JSONResponse({"error": "Invalid email or password."}, status_code=401)

    store_id = user["store"]["$id"] if isinstance(user["store"], dict) else user["store"]
    request.session["store_id"] = store_id
    request.session["user_id"] = user["$id"]
    request.session["email"] = user["email"]

    store = repo.get_store_by_id(store_id)
    return {"status": "ok", "shop": store["shop_domain"]}


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return {"status": "ok"}


# --------------------------------------------------------------------
# Overview bundle — everything the dashboard shell needs on load
# --------------------------------------------------------------------
@router.get("/me")
async def me(request: Request):
    store = _get_session_store(request)
    if not store:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    cfg = repo.ensure_customization(store["$id"])
    features = repo.ensure_features(store["$id"])
    store_info = repo.ensure_store_info(store["$id"])

    return {
        "shop_domain": store["shop_domain"],
        "email": request.session.get("email", ""),
        "agent": _agent_shape(cfg),
        "appearance": _appearance_shape(cfg),
        "features": _features_shape(features),
        "store_info": _store_info_shape(store_info),
    }


# --------------------------------------------------------------------
# AI Agent — name, welcome message, instructions, status
# --------------------------------------------------------------------
@router.post("/agent")
async def save_agent(
    request: Request,
    agent_name: str = Form(...),
    agent_title: str = Form(...),
    instructions: str = Form(""),
    status: str = Form("active"),
):
    store = _get_session_store(request)
    if not store:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    updates = {
        "agent_name": agent_name.strip() or "AI Assistant",
        "agent_title": agent_title.strip() or "How can I help you today?",
        "instructions": instructions.strip(),
        "status": status if status in ("active", "inactive", "maintenance") else "active",
    }
    cfg = repo.update_customization(store["$id"], **updates)
    return _agent_shape(cfg)


# --------------------------------------------------------------------
# Appearance — theme color, widget position, icon
# --------------------------------------------------------------------
@router.get("/appearance")
async def get_appearance(request: Request):
    store = _get_session_store(request)
    if not store:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    cfg = repo.ensure_customization(store["$id"])
    return _appearance_shape(cfg)


@router.post("/appearance")
async def save_appearance(
    request: Request,
    theme_color: str = Form(None),
    widget_position: str = Form(None),
    icon_type: str = Form(None),
    custom_icon: UploadFile | None = File(None),
):
    store = _get_session_store(request)
    if not store:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    current = repo.ensure_customization(store["$id"])
    updates = {}

    if theme_color is not None:
        updates["theme_color"] = theme_color.strip() or current.get("theme_color", "#2b2b2b")
    if widget_position is not None:
        updates["widget_position"] = widget_position if widget_position in ("bottom-right", "bottom-left") else "bottom-right"
    if icon_type is not None:
        updates["icon_type"] = icon_type if icon_type in ("preset", "custom") else "preset"

    if custom_icon is not None and custom_icon.filename:
        ext = os.path.splitext(custom_icon.filename)[1] or ".png"
        filename = f"{store['$id']}_{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(UPLOAD_DIR, filename)
        with open(filepath, "wb") as f:
            shutil.copyfileobj(custom_icon.file, f)
        updates["custom_icon_url"] = f"/{filepath}"
        updates.setdefault("icon_type", "custom")

    cfg = repo.update_customization(store["$id"], **updates) if updates else current
    return _appearance_shape(cfg)


# --------------------------------------------------------------------
# Features — what the AI agent is allowed to do
# --------------------------------------------------------------------
@router.get("/features")
async def get_features(request: Request):
    store = _get_session_store(request)
    if not store:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    doc = repo.ensure_features(store["$id"])
    return _features_shape(doc)


@router.post("/features")
async def save_features(
    request: Request,
    product_search: bool = Form(False),
    recommendations: bool = Form(False),
    product_filtering: bool = Form(False),
    warranty: bool = Form(False),
    cart_editing: bool = Form(False),
    returns: bool = Form(False),
    track_orders: bool = Form(False),
):
    store = _get_session_store(request)
    if not store:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    doc = repo.update_features(
        store["$id"],
        product_search=product_search,
        recommendations=recommendations,
        product_filtering=product_filtering,
        warranty=warranty,
        cart_editing=cart_editing,
        returns=returns,
        track_orders=track_orders,
    )
    return _features_shape(doc)


# --------------------------------------------------------------------
# Store info — business name, support email, timezone
# --------------------------------------------------------------------
@router.get("/store-info")
async def get_store_info(request: Request):
    store = _get_session_store(request)
    if not store:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    doc = repo.ensure_store_info(store["$id"])
    return _store_info_shape(doc)


@router.post("/store-info")
async def save_store_info(
    request: Request,
    business_name: str = Form(""),
    support_email: str = Form(""),
    timezone: str = Form("UTC"),
):
    store = _get_session_store(request)
    if not store:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    doc = repo.update_store_info(
        store["$id"],
        business_name=business_name.strip(),
        support_email=support_email.strip(),
        timezone=timezone.strip() or "UTC",
    )
    return _store_info_shape(doc)


# --------------------------------------------------------------------
# Knowledge base / FAQs
# --------------------------------------------------------------------
@router.get("/faqs")
async def get_faqs(request: Request):
    store = _get_session_store(request)
    if not store:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    return {"faqs": [_faq_shape(d) for d in repo.list_faqs(store["$id"])]}


@router.post("/faqs")
async def create_faq(request: Request, question: str = Form(...), answer: str = Form(...)):
    store = _get_session_store(request)
    if not store:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    if not question.strip() or not answer.strip():
        return JSONResponse({"error": "Both a question and an answer are required."}, status_code=400)
    doc = repo.add_faq(store["$id"], question.strip(), answer.strip())
    return _faq_shape(doc)


@router.delete("/faqs/{faq_id}")
async def remove_faq(request: Request, faq_id: str):
    store = _get_session_store(request)
    if not store:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    ok = repo.delete_faq(store["$id"], faq_id)
    if not ok:
        return JSONResponse({"error": "FAQ not found."}, status_code=404)
    return {"status": "ok"}


# --------------------------------------------------------------------
# Feedback
# --------------------------------------------------------------------
@router.post("/feedback")
async def send_feedback(request: Request, message: str = Form(...)):
    store = _get_session_store(request)
    if not store:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    if not message.strip():
        return JSONResponse({"error": "Message can't be empty."}, status_code=400)
    repo.submit_feedback(store["$id"], message.strip())
    return {"status": "ok"}


# --------------------------------------------------------------------
# Install snippet — small helper so the frontend doesn't hardcode host
# --------------------------------------------------------------------
@router.get("/install-snippet")
async def install_snippet(request: Request):
    store = _get_session_store(request)
    if not store:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    origin = f"{request.url.scheme}://{request.url.netloc}"
    snippet = f'<script src="{origin}/widget.js" data-shop="{store["shop_domain"]}" defer></script>'
    return {"snippet": snippet}
