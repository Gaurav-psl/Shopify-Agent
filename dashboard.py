"""
dashboard.py
------------
The store owner's dashboard — separate from their Shopify login. After
installing the app (via shopify_auth.py), they land here to:

  1. Create a login (signup) tied to their store, or log in on return visits
  2. Customize the chatbot: display name, title, description, and button
     icon (custom upload OR a preset icon recolored to their theme_color)

Sessions are simple signed cookies (Starlette's SessionMiddleware — add
that middleware in main.py). Passwords are hashed with bcrypt via
passlib.

This uses plain inline HTML so the whole dashboard is a single file with
no extra template setup required. Swap in Jinja2 templates / a proper
frontend later if you want a nicer UI — the routes and logic stay the same.
"""

import os
import shutil
import uuid

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from passlib.hash import bcrypt
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Store, DashboardUser, AgentCustomization

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "static/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 480px; margin: 60px auto; color: #222; }}
  h1 {{ font-size: 20px; }}
  label {{ display:block; margin-top:14px; font-size:13px; font-weight:600; }}
  input[type=text], input[type=email], input[type=password], textarea, select {{
    width:100%; padding:8px 10px; margin-top:4px; border:1px solid #ddd; border-radius:8px; box-sizing:border-box; font-size:13px;
  }}
  textarea {{ min-height: 70px; }}
  button {{ margin-top:18px; padding:10px 18px; border:none; background:#2b2b2b; color:#fff; border-radius:999px; cursor:pointer; font-size:13px; }}
  .error {{ color:#b04040; font-size:12.5px; margin-top:10px; }}
  .hint {{ color:#777; font-size:11.5px; margin-top:4px; }}
  a {{ color:#2b2b2b; }}
  .row {{ display:flex; gap:16px; align-items:center; }}
</style></head>
<body>{body}</body></html>"""


def _get_session_store(request: Request, db: Session) -> Store | None:
    store_id = request.session.get("store_id")
    if not store_id:
        return None
    return db.query(Store).filter_by(id=store_id, uninstalled=False).first()


# --------------------------------------------------------------------
# Signup — first-time setup after install
# --------------------------------------------------------------------
@router.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request):
    shop = request.query_params.get("shop", "")
    body = f"""
    <h1>Set up your dashboard login</h1>
    <p class="hint">Store: {shop}</p>
    <form method="post" action="/dashboard/signup">
      <input type="hidden" name="shop" value="{shop}">
      <label>Email<input type="email" name="email" required></label>
      <label>Password<input type="password" name="password" required minlength="8"></label>
      <button type="submit">Create account</button>
    </form>
    <p class="hint">Already have an account? <a href="/dashboard/login?shop={shop}">Log in</a></p>
    """
    return HTMLResponse(_page("Set up dashboard", body))


@router.post("/signup")
async def signup_submit(request: Request, shop: str = Form(...), email: str = Form(...), password: str = Form(...)):
    db: Session = SessionLocal()
    try:
        store = db.query(Store).filter_by(shop_domain=shop, uninstalled=False).first()
        if not store:
            return PlainTextResponse("Store not found — please reinstall the app.", status_code=404)
        if store.dashboard_user:
            return RedirectResponse(f"/dashboard/login?shop={shop}", status_code=303)
        if db.query(DashboardUser).filter_by(email=email).first():
            return HTMLResponse(_page("Set up dashboard", '<p class="error">That email is already in use.</p><a href="javascript:history.back()">Back</a>'))

        user = DashboardUser(store_id=store.id, email=email, password_hash=bcrypt.hash(password))
        db.add(user)
        db.commit()
        db.refresh(user)

        request.session["store_id"] = store.id
        request.session["user_id"] = user.id
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()


# --------------------------------------------------------------------
# Login
# --------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    shop = request.query_params.get("shop", "")
    body = f"""
    <h1>Log in</h1>
    <form method="post" action="/dashboard/login">
      <label>Email<input type="email" name="email" required></label>
      <label>Password<input type="password" name="password" required></label>
      <button type="submit">Log in</button>
    </form>
    """
    return HTMLResponse(_page("Log in", body))


@router.post("/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    db: Session = SessionLocal()
    try:
        user = db.query(DashboardUser).filter_by(email=email).first()
        if not user or not bcrypt.verify(password, user.password_hash):
            return HTMLResponse(_page("Log in", '<p class="error">Invalid email or password.</p><a href="/dashboard/login">Try again</a>'))

        request.session["store_id"] = user.store_id
        request.session["user_id"] = user.id
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/dashboard/login", status_code=303)


# --------------------------------------------------------------------
# Main dashboard — agent customization
# --------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    db: Session = SessionLocal()
    try:
        store = _get_session_store(request, db)
        if not store:
            return RedirectResponse("/dashboard/login", status_code=303)

        cfg = db.query(AgentCustomization).filter_by(store_id=store.id).first()
        if not cfg:
            cfg = AgentCustomization(store_id=store.id)
            db.add(cfg)
            db.commit()
            db.refresh(cfg)

        preset_checked = "checked" if cfg.icon_type == "preset" else ""
        custom_checked = "checked" if cfg.icon_type == "custom" else ""
        preview_img = f'<img src="{cfg.custom_icon_url}" style="width:40px;height:40px;border-radius:50%;">' if cfg.custom_icon_url else ""

        body = f"""
        <h1>Chatbot settings — {store.shop_domain}</h1>
        <p class="hint"><a href="/dashboard/logout">Log out</a></p>

        <form method="post" action="/dashboard/customize" enctype="multipart/form-data">
          <label>Agent name (shown in the widget header)
            <input type="text" name="agent_name" value="{cfg.agent_name}" maxlength="100">
          </label>
          <label>Greeting title (first message shown to shoppers)
            <input type="text" name="agent_title" value="{cfg.agent_title}" maxlength="150">
          </label>
          <label>Description (internal notes, not shown to shoppers)
            <textarea name="agent_description" maxlength="500">{cfg.agent_description}</textarea>
          </label>

          <label>Button icon</label>
          <div class="row">
            <label style="display:flex;align-items:center;gap:6px;font-weight:400;">
              <input type="radio" name="icon_type" value="preset" {preset_checked}> Preset icon (recolored)
            </label>
            <label style="display:flex;align-items:center;gap:6px;font-weight:400;">
              <input type="radio" name="icon_type" value="custom" {custom_checked}> Custom image
            </label>
          </div>

          <label>Theme color (used when "Preset icon" is selected)
            <input type="text" name="theme_color" value="{cfg.theme_color}" placeholder="#2b2b2b">
          </label>

          <label>Custom icon image (used when "Custom image" is selected)
            {preview_img}
            <input type="file" name="custom_icon" accept="image/*">
          </label>

          <button type="submit">Save changes</button>
        </form>

        <hr style="margin-top:30px;">
        <h1 style="font-size:15px;">Install snippet</h1>
        <p class="hint">Add this once to your theme (before &lt;/body&gt;):</p>
        <code style="display:block;background:#f5f5f5;padding:10px;border-radius:8px;font-size:11px;word-break:break-all;">
          &lt;script src="{request.url.scheme}://{request.url.netloc}/widget.js" data-shop="{store.shop_domain}" defer&gt;&lt;/script&gt;
        </code>
        """
        return HTMLResponse(_page("Dashboard", body))
    finally:
        db.close()


@router.post("/customize")
async def customize_submit(
    request: Request,
    agent_name: str = Form(...),
    agent_title: str = Form(...),
    agent_description: str = Form(""),
    icon_type: str = Form("preset"),
    theme_color: str = Form("#2b2b2b"),
    custom_icon: UploadFile | None = File(None),
):
    db: Session = SessionLocal()
    try:
        store = _get_session_store(request, db)
        if not store:
            return RedirectResponse("/dashboard/login", status_code=303)

        cfg = db.query(AgentCustomization).filter_by(store_id=store.id).first()
        cfg.agent_name = agent_name.strip() or "AI Assistant"
        cfg.agent_title = agent_title.strip() or "How can I help you today?"
        cfg.agent_description = agent_description.strip()
        cfg.icon_type = icon_type if icon_type in ("preset", "custom") else "preset"
        cfg.theme_color = theme_color.strip() or "#2b2b2b"

        if custom_icon is not None and custom_icon.filename:
            ext = os.path.splitext(custom_icon.filename)[1] or ".png"
            filename = f"{store.id}_{uuid.uuid4().hex}{ext}"
            filepath = os.path.join(UPLOAD_DIR, filename)
            with open(filepath, "wb") as f:
                shutil.copyfileobj(custom_icon.file, f)
            cfg.custom_icon_url = f"/{filepath}"

        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()
