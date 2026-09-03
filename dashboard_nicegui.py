"""
dashboard_nicegui.py
---------------------
The full "RenderLink" dashboard, built with NiceGUI to match the
original DashboardApp.jsx layout element-for-element: sidebar with a
"Need help?" card and account block, a full topbar (store pill, status
pill, App Info modal, Help, avatar), a Features
dropdown that opens a right-side slide panel per feature (not a flat
list), a richer Overview page with a live widget preview bubble, and
matching iconography throughout (NiceGUI ships Material Icons, used
here as the closest equivalent to the lucide-react icons in the JSX).

See the bottom of main.py for how this mounts onto your FastAPI app.
Run: pip install nicegui
"""

import os
import time

import bcrypt
import httpx
from nicegui import ui, app

import repository_appwrite as repo
import email_utils

APP_URL = (os.environ.get("APP_URL") or os.environ.get("HOST") or "http://localhost:8080").strip().rstrip("/")

BRAND = "#4B5563"
BRAND_SOFT = "#F3F4F6"
PAGE_BG = "#F3F4F6"


# The exact Client ID Shopify issued for this app. This MUST match
# whatever env var shopify_auth.py already uses for OAuth (it may be
# named differently there — e.g. SHOPIFY_CLIENT_ID — check and align
# the name below if so; App Bridge won't work with the wrong value).
SHOPIFY_API_KEY = os.environ.get("SHOPIFY_API_KEY", "")

NAV_ITEMS = [
    ("Dashboard", "Dashboard", "home"),
    ("store", "Store Information", "storefront"),
    ("agent", "AI Agent", "smart_toy"),
    ("features", "Features", "grid_view"),
    ("appearance", "Appearance", "palette"),
    ("knowledge", "Knowledge (FAQs)", "menu_book"),
    ("feedback", "Feedback & Help", "help_outline"),
]

FEATURE_LIST = [
    ("product_search", "Product Search", "search"),
    ("recommendations", "Recommendations", "auto_awesome"),
    ("product_filtering", "Product Filtering", "search"),
    ("warranty", "Warranty", "check_circle"),
    ("cart_editing", "Cart Editing", "shopping_cart"),
    ("returns", "Returns", "replay"),
    ("track_orders", "Track Orders", "local_shipping"),
]

STATUS_STYLES = {
    "active": {"bg": "#E5E7EB", "text": "#111827", "dot": "#22C55E", "label": "Active"},
    "inactive": {"bg": "#F3F4F6", "text": "#9CA3AF", "dot": "#EF4444", "label": "Inactive"},
    "maintenance": {"bg": "#F3F4F6", "text": "#6B7280", "dot": "#F97316", "label": "Maintenance"},
}

THEME_SWATCHES = ["#4B5563", "#6B7280", "#9CA3AF", "#374151", "#1F2937"]

CARD_CLASSES = "bg-white rounded-2xl border border-gray-100 shadow-sm w-full"


# --------------------------------------------------------------------
# Session helpers
# --------------------------------------------------------------------
def _current_store() -> dict | None:
    store_id = app.storage.user.get("store_id")
    if not store_id:
        return None
    store = repo.get_store_by_id(store_id)
    if store and not store.get("uninstalled"):
        return store
    return None


def _require_store():
    store = _current_store()
    if not store:
        _goto("/dashboard/login")
        return None
    return store


def _logout():
    app.storage.user.clear()
    _goto("/dashboard/login")


def _initial(store: dict) -> str:
    return (app.storage.user.get("email") or store["shop_domain"])[0].upper()


def _goto(path: str):
    """Navigates to an internal /dashboard/... path while preserving the
    shop/host/embedded query params Shopify attaches when it first loads
    the app in its iframe. Dropping these on a full page load (which is
    what every @ui.page navigation is) breaks App Bridge's ability to
    re-initialize on the next page — which is why the nav menu can
    silently vanish after any in-app navigation. Always use this instead
    of calling ui.navigate.to directly for internal /dashboard links."""
    try:
        request = ui.context.client.request
        params = {k: v for k, v in request.query_params.items() if k in ("shop", "host", "embedded", "session", "id_token")}
    except Exception:
        params = {}
    if params:
        from urllib.parse import urlencode
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}{urlencode(params)}"
    ui.navigate.to(path)


def _shopify_nav_menu():
    """Registers this app's nav links with Shopify Admin via App Bridge,
    so they render nested under the app's name in Shopify's OWN sidebar
    (like the screenshot) instead of a sidebar drawn inside our iframe.

    Requires SHOPIFY_API_KEY to be set to the exact Client ID Shopify
    issued for this app — App Bridge silently no-ops if it's wrong or
    missing, so double check it against shopify_auth.py's OAuth setup.
    """
    ui.add_head_html(f'''
        <meta name="shopify-api-key" content="{SHOPIFY_API_KEY}">
        <script src="https://cdn.shopify.com/shopifycloud/app-bridge.js"></script>
    ''')
    links_html = "".join(f'<a href="/dashboard/{key}">{label}</a>' for key, label, _ in NAV_ITEMS)
    ui.html(f"<ui-nav-menu>{links_html}</ui-nav-menu>").style("display:none;")


# --------------------------------------------------------------------
# Live preview — loads your ACTUAL widget.js for this shop, so what the
# owner sees here is pixel-for-pixel what a real shopper sees on the
# storefront (not a mockup). Reads whatever's currently saved via
# /widget-config, same as the real embed does.
#
# The preview backdrop is the merchant's REAL homepage: we fetch its
# HTML server-side (so the browser never has to iframe a third-party
# origin directly — no X-Frame-Options/CSP-frame-ancestors issues,
# since we re-serve it from our own domain), inject a <base> tag so
# relative asset URLs (css/js/images) still resolve against the real
# store, strip any CSP <meta> tag the theme sets (it would otherwise
# block our own injected <script>), and then inject the real widget
# script right before </body> — the widget then calls /widget-config
# itself and renders with whatever's currently saved (agent name,
# welcome message, theme color, icon, position). If the homepage can't
# be fetched for any reason, we fall back to the old blank canvas with
# a small note so the widget itself is still previewable.
# --------------------------------------------------------------------
import re  # noqa: E402
from fastapi import Request  # noqa: E402
from fastapi.responses import HTMLResponse as _HTMLResponse  # noqa: E402

_CSP_META_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']content-security-policy["\'][^>]*>',
    re.IGNORECASE,
)


def _blank_preview_html(shop: str, widget_src: str, note: str = "") -> str:
    banner = (
        f'<div style="position:fixed;top:0;left:0;right:0;padding:8px 14px;'
        f'background:#FEF3C7;color:#92400E;font:12px -apple-system,sans-serif;'
        f'z-index:99999;">{note}</div>'
        if note else ""
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;min-height:100vh;background:#fafafa;">
  {banner}
  <script src="{widget_src}" data-shop="{shop}" defer></script>
</body></html>"""


@app.get("/dashboard/widget-preview")
async def widget_preview(shop: str, request: Request):
    home_url = f"https://{shop}/"

    # CRITICAL: this must be an ABSOLUTE URL pointing at OUR OWN server,
    # never a root-relative path like "/widget.js". The real homepage
    # HTML below gets a <base href="https://{shop}/"> injected so the
    # STORE'S OWN relative asset URLs still resolve correctly — but that
    # base tag affects EVERY relative URL on the page, including
    # root-relative ones. A root-relative "/widget.js" would silently
    # resolve to "https://{shop}/widget.js" (a route that doesn't exist
    # on the merchant's own server) instead of our backend, so the
    # widget script would never actually load — which is exactly the
    # bug this fixes. Confirmed empirically: a page with
    # <base href="https://example.com/"> and <script src="/widget.js">
    # causes the browser to request https://example.com/widget.js, not
    # the server that actually served the page.
    own_origin = f"{request.url.scheme}://{request.url.netloc}"
    widget_src = f"{own_origin}/widget.js"
    widget_tag = f'<script src="{widget_src}" data-shop="{shop}" defer></script>'

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=8.0) as client:
            resp = await client.get(
                home_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; RenderLinkPreview/1.0)"},
            )
        resp.raise_for_status()
        html = resp.text

        # Strip any theme CSP meta tag so it can't block our injected script.
        html = _CSP_META_RE.sub("", html)

        # Make relative asset URLs (css/js/images) resolve against the real
        # store, since we're now serving this HTML from our own domain.
        if "<head>" in html:
            html = html.replace("<head>", f'<head><base href="{home_url}">', 1)
        elif "<head " in html:
            html = re.sub(r"(<head[^>]*>)", rf'\1<base href="{home_url}">', html, count=1)
        else:
            html = f'<base href="{home_url}">' + html

        # Inject the real widget script right before </body> (or append at
        # the end if the fetched page has no closing body tag).
        if "</body>" in html:
            html = html.replace("</body>", f"{widget_tag}</body>", 1)
        else:
            html += widget_tag

        return _HTMLResponse(html)

    except Exception:
        # Homepage couldn't be fetched (site down, password-protected,
        # blocks server-side requests, etc.) — fall back to a blank
        # canvas so the widget is still previewable.
        return _HTMLResponse(
            _blank_preview_html(
                shop,
                widget_src,
                note="Couldn't load your storefront homepage for this preview — showing the widget on a blank canvas instead.",
            )
        )


def _open_preview(store: dict):
    """Opens a dialog embedding the real widget for this store. Rebuilt
    fresh (cache-busted) every time it's called, so it always reflects
    whatever was most recently saved on Appearance/AI Agent — no stale
    preview after an edit."""
    ts = int(time.time())
    with ui.dialog() as dialog:
        with ui.card().classes("p-0 gap-0").style(
            "width:1040px;height:720px;max-width:96vw;max-height:94vh;overflow:hidden;border-radius:18px;"
        ):
            with ui.row().classes("w-full items-center justify-between px-4 py-2.5 flex-shrink-0").style(
                "border-bottom:1px solid #F3F4F6;"
            ):
                ui.label("Live Preview — exactly what shoppers see on your site").classes(
                    "font-bold text-gray-900 text-xs"
                )
                ui.button(icon="close", on_click=dialog.close).props("flat round dense").style("color:#9CA3AF;")
            # A thin fake browser address bar makes it read as "your actual
            # site" rather than an arbitrary popup.
            with ui.row().classes("w-full items-center gap-2 px-4 py-1.5 flex-shrink-0").style(
                "background:#F9FAFB;border-bottom:1px solid #F3F4F6;"
            ):
                ui.icon("lock").classes("text-gray-400").style("font-size:12px;")
                ui.label(store["shop_domain"]).classes("text-xs text-gray-500")
            ui.html(
                f'<iframe src="/dashboard/widget-preview?shop={store["shop_domain"]}&t={ts}" '
                f'style="width:100%;height:100%;border:none;display:block;"></iframe>'
            ).style("flex:1;width:100%;")
    dialog.open()


# --------------------------------------------------------------------
# App Info modal — matches the JSX "App Info" popup
# --------------------------------------------------------------------
def _app_info_dialog(agent: dict):
    s = STATUS_STYLES.get(agent.get("status", "active"), STATUS_STYLES["active"])
    with ui.dialog() as dialog, ui.card().classes("p-6 w-full max-w-sm gap-1"):
        with ui.row().classes("w-full items-center justify-between mb-2"):
            ui.label("App Info").classes("font-bold text-gray-900")
            ui.button(icon="close", on_click=dialog.close).props("flat round dense").style("color:#9CA3AF;")
        ui.label("RenderLink AI Shopping Assistant").classes("text-sm text-gray-500")
        ui.label("Version 1.0.0").classes("text-sm text-gray-500")
        with ui.row().classes("items-center gap-1"):
            ui.label("Status:").classes("text-sm text-gray-500")
            ui.label(s["label"]).classes("text-sm font-semibold").style(f"color:{s['dot']};")
        ui.button("Close", on_click=dialog.close).props("no-caps").classes("w-full mt-3").style(
            f"background:{BRAND};color:white;border-radius:8px;"
        )
    return dialog


# --------------------------------------------------------------------
# Shared layout — header + sidebar, wraps every logged-in page
# --------------------------------------------------------------------
def _layout(active_key: str, store: dict, agent: dict):
    _shopify_nav_menu()
    info_dialog = _app_info_dialog(agent)
    s = STATUS_STYLES.get(agent.get("status", "active"), STATUS_STYLES["active"])

    # ---- Topbar ----
    with ui.header().classes("items-center justify-between px-6").style(
        "background:white;border-bottom:1px solid #F3F4F6;color:#111827;height:64px;"
    ):
        with ui.row().classes("items-center gap-2"):
            ui.label("Store:").classes("text-sm text-gray-500")
            with ui.row().classes("items-center gap-1.5 px-3 py-1.5 rounded-lg").style("background:#F9FAFB;"):
                ui.icon("storefront", size="16px").style("color:#374151;")
                ui.label(store["shop_domain"]).classes("text-sm font-medium text-gray-800")

        with ui.row().classes("items-center gap-2.5"):
            with ui.row().classes("items-center gap-1.5 px-3 py-1.5 rounded-full").style(f"background:{s['bg']};"):
                ui.element("div").classes("w-1.5 h-1.5 rounded-full").style(f"background:{s['dot']};")
                ui.label(s["label"]).classes("text-xs font-semibold").style(f"color:{s['text']};")

            ui.button("App Info", icon="info", on_click=info_dialog.open).props("no-caps flat").classes(
                "px-3 py-1.5 text-xs font-semibold"
            ).style("border:1px solid #E5E7EB;border-radius:999px;color:#4B5563;")

            ui.button("Help", icon="help_outline", on_click=lambda: _goto("/dashboard/feedback")).props(
                "no-caps flat"
            ).classes("px-3 py-1.5 text-xs font-semibold").style("border:1px solid #E5E7EB;border-radius:999px;color:#4B5563;")

            with ui.element("div").classes("w-8 h-8 rounded-full flex items-center justify-center").style(
                f"background:{BRAND};color:white;font-size:12px;font-weight:700;"
            ):
                ui.label(_initial(store))

            ui.button("Log out", icon="logout", on_click=_logout).props("no-caps flat").classes(
                "px-3 py-1.5 text-xs font-semibold"
            ).style("border:1px solid #E5E7EB;border-radius:999px;color:#4B5563;")

    # ---- Shopify Admin's own sidebar now owns navigation (see screenshot) ----
    # No custom left_drawer here anymore — _shopify_nav_menu() (called
    # above) registers the same links via App Bridge, and Shopify renders
    # them nested under the app's name in its own sidebar instead.

    # Wider now that there's no in-frame sidebar taking up the left side —
    # mx-auto keeps the margins equal on both sides as it grows.
    content = ui.column().classes("w-full max-w-5xl mx-auto p-8 gap-4")
    return content


def _page_header(title: str, subtitle: str):
    ui.label(title).classes("text-2xl font-bold text-gray-900")
    ui.label(subtitle).classes("text-sm text-gray-500 -mt-3")


# --------------------------------------------------------------------
# Features dropdown + slide-out detail panel — used on Overview
# --------------------------------------------------------------------
def _features_dropdown(store: dict, features: dict):
    open_state = {"v": False}

    with ui.column().classes("w-full gap-0").style("position:relative;"):
        trigger = ui.row().classes("w-full items-center justify-between px-4 py-3 cursor-pointer").style(
            "background:white;border:1px solid #E5E7EB;border-radius:12px;"
        )
        with trigger:
            with ui.row().classes("items-center gap-2"):
                ui.icon("grid_view", size="16px").style("color:#374151;")
                ui.label("Enabled Features").classes("text-sm font-semibold text-gray-700")
            chevron = ui.icon("expand_more", size="16px").style("color:#9CA3AF;transition:transform 0.15s;")

        dropdown_list = ui.column().classes("w-full gap-0").style(
            "position:absolute;top:52px;left:0;right:0;z-index:20;background:white;"
            "border:1px solid #E5E7EB;border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,0.08);overflow:hidden;display:none;"
        )

        panels = {}
        for key, label, icon in FEATURE_LIST:
            panels[key] = _feature_panel(store, features, key, label, icon)

        with dropdown_list:
            for key, label, icon in FEATURE_LIST:
                with ui.row().classes("w-full items-center justify-between px-4 py-2.5 cursor-pointer hover:bg-gray-50").style(
                    "border-bottom:1px solid #F9FAFB;"
                ).on("click", lambda k=key: panels[k].open()):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon(icon, size="14px").style("color:#4B5563;")
                        ui.label(label).classes("text-sm text-gray-700")
                    with ui.row().classes("items-center gap-2"):
                        ui.label("On" if features.get(key) else "Off").classes("text-[10px] font-semibold text-gray-400")
                        ui.icon("arrow_forward", size="12px").style("color:#D1D5DB;")

        def toggle_open():
            open_state["v"] = not open_state["v"]
            dropdown_list.style(f"display:{'flex' if open_state['v'] else 'none'};")
            chevron.style(f"transform:rotate({180 if open_state['v'] else 0}deg);")

        trigger.on("click", toggle_open)


def _feature_panel(store: dict, features: dict, key: str, label: str, icon: str):
    """A right-side slide-out dialog for a single feature, matching the
    JSX fixed inset-0 overlay + w-80 side panel."""
    with ui.dialog() as dialog:
        dialog.props("position=right")
        with ui.card().classes("h-screen p-6 gap-4").style("width:320px;max-width:90vw;border-radius:0;"):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon(icon, size="18px").style("color:#111827;")
                    ui.label(label).classes("font-bold text-gray-900")
                ui.button(icon="close", on_click=dialog.close).props("flat round dense").style("color:#9CA3AF;")

            with ui.row().classes("w-full items-center justify-between px-4 py-3 rounded-xl").style(f"background:{BRAND_SOFT};"):
                ui.label("Enabled for this store").classes("text-sm font-medium text-gray-700")
                sw = ui.switch(value=bool(features.get(key))).props("color=grey-8")

                def on_change(e, k=key):
                    repo.update_features(store["$id"], **{k: e.value})
                    features[k] = e.value

                sw.on_value_change(on_change)

            ui.label(f"When on, shoppers can ask your AI assistant to {label.lower()} directly in chat.").classes(
                "text-xs text-gray-500 leading-relaxed"
            )
    return dialog


# --------------------------------------------------------------------
# AUTH — Signup / Login
# --------------------------------------------------------------------
def _auth_shell():
    ui.query("body").style(f"background:{PAGE_BG};")
    return ui.column().classes("w-full items-center justify-center").style("min-height:100vh;")


@ui.page("/dashboard/signup")
def signup_page(shop: str = ""):
    # Signup is no longer a standalone page — it's step 1 of the setup
    # wizard, which only ever runs once, the first time a store uses
    # the app. Kept as a route (rather than deleted) purely so
    # shopify_auth.py's existing OAuth redirect for new installs
    # doesn't need to change.
    _goto(f"/dashboard/setup?shop={shop}")


@ui.page("/dashboard/login")
def login_page():
    with _auth_shell():
        with ui.column().classes("items-center gap-2 mb-2"):
            with ui.element("div").classes("w-10 h-10 rounded-xl flex items-center justify-center").style(f"background:{BRAND};"):
                ui.icon("forum", size="20px").style("color:white;")
            ui.label("RenderLink").classes("text-xl font-bold text-gray-900")

        with ui.card().classes("p-7 w-full max-w-sm gap-1"):
            ui.label("Log in").classes("text-lg font-bold text-gray-900")
            ui.label("Welcome back — manage your AI shopping assistant.").classes("text-sm text-gray-500 mb-3")

            email = ui.input("Email").classes("w-full")
            password = ui.input("Password", password=True, password_toggle_button=True).classes("w-full")
            error_label = ui.label("").classes("text-xs text-gray-500")

            def submit():
                user = repo.get_dashboard_user_by_email(email.value.strip())
                if not user or not bcrypt.checkpw(password.value.encode(), user["password_hash"].encode()):
                    error_label.text = "Invalid email or password."
                    return
                store_id = user["store_id"]
                app.storage.user["store_id"] = store_id
                app.storage.user["user_id"] = user["$id"]
                app.storage.user["email"] = user["email"]

                # Login always goes straight to the dashboard — setup is
                # only ever shown to someone creating a new account
                # (via signup -> /dashboard/setup). A returning user who
                # logs in never sees it, even if an earlier setup
                # attempt was abandoned partway through.
                _goto("/dashboard/Dashboard")

            ui.button("Log in", on_click=submit).props("no-caps").classes("w-full mt-3").style(
                f"background:{BRAND};color:white;border-radius:10px;"
            )
            with ui.row().classes("w-full justify-center mt-1"):
                ui.link("Forgot password?", "/dashboard/forgot-password").classes("text-xs text-gray-500")
            with ui.row().classes("w-full justify-center mt-2"):
                ui.label("New here?").classes("text-xs text-gray-500")
                ui.link("Create an account", "/dashboard/signup").classes("text-xs font-semibold text-gray-800")


@ui.page("/dashboard/forgot-password")
def forgot_password_page():
    with _auth_shell():
        with ui.column().classes("items-center gap-2 mb-2"):
            with ui.element("div").classes("w-10 h-10 rounded-xl flex items-center justify-center").style(f"background:{BRAND};"):
                ui.icon("forum", size="20px").style("color:white;")
            ui.label("RenderLink").classes("text-xl font-bold text-gray-900")

        card = ui.card().classes("p-7 w-full max-w-sm gap-1")

        def render_form():
            card.clear()
            with card:
                ui.label("Reset your password").classes("text-lg font-bold text-gray-900")
                ui.label("Enter your email and we'll send you a reset link.").classes("text-sm text-gray-500 mb-3")

                email = ui.input("Email").classes("w-full")

                def submit():
                    token = repo.create_password_reset_token(email.value.strip())
                    if token:
                        reset_link = f"{APP_URL}/dashboard/reset-password?token={token}"
                        email_utils.send_password_reset_email(email.value.strip(), reset_link)
                    # Same message either way — never reveal whether an
                    # email is actually registered.
                    card.clear()
                    with card:
                        ui.icon("mark_email_read", size="32px").style(f"color:{BRAND};")
                        ui.label("Check your email").classes("text-lg font-bold text-gray-900 mt-1")
                        ui.label(
                            "If that email is registered, a password reset link is on its way. "
                            "It expires in 1 hour."
                        ).classes("text-sm text-gray-500")
                        ui.link("Back to login", "/dashboard/login").classes("text-xs font-semibold text-gray-800 mt-3")

                ui.button("Send reset link", on_click=submit).props("no-caps").classes("w-full mt-3").style(
                    f"background:{BRAND};color:white;border-radius:10px;"
                )
                with ui.row().classes("w-full justify-center mt-2"):
                    ui.link("Back to login", "/dashboard/login").classes("text-xs font-semibold text-gray-800")

        render_form()


@ui.page("/dashboard/reset-password")
def reset_password_page(token: str = ""):
    with _auth_shell():
        with ui.column().classes("items-center gap-2 mb-2"):
            with ui.element("div").classes("w-10 h-10 rounded-xl flex items-center justify-center").style(f"background:{BRAND};"):
                ui.icon("forum", size="20px").style("color:white;")
            ui.label("RenderLink").classes("text-xl font-bold text-gray-900")

        with ui.card().classes("p-7 w-full max-w-sm gap-1") as card:
            user = repo.get_dashboard_user_by_reset_token(token) if token else None

            if not user:
                ui.icon("error_outline", size="32px").style("color:#DC2626;")
                ui.label("This reset link is invalid or has expired.").classes("text-sm text-gray-700 mt-1")
                ui.link("Request a new link", "/dashboard/forgot-password").classes("text-xs font-semibold text-gray-800 mt-3")
            else:
                ui.label("Choose a new password").classes("text-lg font-bold text-gray-900")
                ui.label(f"Resetting password for {user['email']}").classes("text-sm text-gray-500 mb-3")

                new_password = ui.input("New password", password=True, password_toggle_button=True, placeholder="At least 8 characters").classes("w-full")
                confirm_password = ui.input("Confirm password", password=True, password_toggle_button=True).classes("w-full")
                error_label = ui.label("").classes("text-xs text-gray-500")

                def submit():
                    if len(new_password.value) < 8:
                        error_label.text = "Password needs at least 8 characters."
                        return
                    if new_password.value != confirm_password.value:
                        error_label.text = "Passwords don't match."
                        return
                    repo.reset_password(user["$id"], bcrypt.hashpw(new_password.value.encode(), bcrypt.gensalt()).decode())
                    ui.notify("Password updated — please log in.", type="positive")
                    _goto("/dashboard/login")

                ui.button("Update password", on_click=submit).props("no-caps").classes("w-full mt-3").style(
                    f"background:{BRAND};color:white;border-radius:10px;"
                )


@ui.page("/dashboard")
def dashboard_root():
    _goto("/dashboard/Dashboard")


# --------------------------------------------------------------------
# SETUP WIZARD — one-time onboarding, replaces the old standalone
# signup page. Step 1 creates the account (was signup), step 2 picks
# features, step 3 optionally customizes the agent (defaults are kept
# if skipped, since ensure_features()/ensure_customization() already
# seed sensible values), step 4 confirms and flags the browser
# third-party-cookie requirement before handing off to the dashboard.
# --------------------------------------------------------------------
@ui.page("/dashboard/setup")
def setup_page(shop: str = ""):
    ui.query("body").style(f"background:{PAGE_BG};")

    # If already logged in (e.g. resuming setup after a login redirect
    # for a store that started but never finished), skip account
    # creation — that part's already done.
    existing_store_id = app.storage.user.get("store_id")
    start_step = "features" if existing_store_id else "account"

    def current_wizard_store() -> dict | None:
        sid = app.storage.user.get("store_id")
        return repo.get_store_by_id(sid) if sid else None

    with ui.column().classes("w-full items-center").style("min-height:100vh;padding:40px 16px;"):
        with ui.column().classes("items-center gap-2 mb-4"):
            with ui.element("div").classes("w-10 h-10 rounded-xl flex items-center justify-center").style(f"background:{BRAND};"):
                ui.icon("forum", size="20px").style("color:white;")
            ui.label("Welcome to RenderLink").classes("text-xl font-bold text-gray-900")
            ui.label("Let's get your AI assistant set up — takes about a minute.").classes("text-sm text-gray-500")

        with ui.card().classes("p-0 w-full max-w-xl gap-0 overflow-hidden"):
            with ui.stepper(value=start_step).props("flat").classes("w-full") as stepper:

                # ---- Step 1: Account details ----
                with ui.step("account", "Account details", icon="person"):
                    ui.label("Create your dashboard login").classes("font-bold text-gray-900")
                    ui.label("Separate from your Shopify login — just for this dashboard.").classes("text-xs text-gray-500 mb-2")

                    store_domain = ui.input("Store domain", value=shop, placeholder="myshop.myshopify.com").classes("w-full")
                    email = ui.input("Email", placeholder="you@myshop.com").classes("w-full")
                    password = ui.input("Password", password=True, password_toggle_button=True, placeholder="At least 8 characters").classes("w-full")
                    account_error = ui.label("").classes("text-xs text-red-500")

                    def create_account():
                        if not store_domain.value.strip() or not email.value.strip() or len(password.value) < 8:
                            account_error.text = "Fill in every field — password needs at least 8 characters."
                            return
                        store = repo.get_store(store_domain.value.strip())
                        if not store:
                            account_error.text = "Store not found — please reinstall the app."
                            return
                        if repo.has_dashboard_user(store["$id"]):
                            account_error.text = "This store already has a dashboard login — log in instead."
                            return
                        if repo.get_dashboard_user_by_email(email.value.strip()):
                            account_error.text = "That email is already in use."
                            return
                        user = repo.create_dashboard_user(
                            store["$id"], email.value.strip(),
                            bcrypt.hashpw(password.value.encode(), bcrypt.gensalt()).decode(),
                        )
                        app.storage.user["store_id"] = store["$id"]
                        app.storage.user["user_id"] = user["$id"]
                        app.storage.user["email"] = user["email"]
                        stepper.next()

                    with ui.stepper_navigation():
                        ui.button("Continue", on_click=create_account).props("no-caps icon-right=arrow_forward").style(
                            f"background:{BRAND};color:white;border-radius:10px;"
                        )

                # ---- Step 2: Features ----
                with ui.step("features", "Features", icon="grid_view"):
                    ui.label("Choose what your assistant can help with").classes("font-bold text-gray-900")
                    ui.label("You can change these anytime from Features in the dashboard.").classes("text-xs text-gray-500 mb-2")

                    _wizard_store = current_wizard_store()
                    features_state = dict(repo.ensure_features(_wizard_store["$id"])) if _wizard_store else {}

                    for key, label, icon in FEATURE_LIST:
                        with ui.row().classes("w-full items-center justify-between px-4 py-2.5 rounded-xl").style(f"background:{BRAND_SOFT};"):
                            with ui.row().classes("items-center gap-2"):
                                ui.icon(icon, size="15px").style("color:#4B5563;")
                                ui.label(label).classes("text-sm font-medium text-gray-700")

                            def on_toggle(e, k=key):
                                features_state[k] = e.value

                            ui.switch(value=bool(features_state.get(key)), on_change=on_toggle).props("color=grey-8")

                    def save_features_and_continue():
                        store_now = current_wizard_store()
                        if store_now:
                            repo.update_features(store_now["$id"], **features_state)
                        stepper.next()

                    with ui.stepper_navigation():
                        ui.button("Continue", on_click=save_features_and_continue).props("no-caps icon-right=arrow_forward").style(
                            f"background:{BRAND};color:white;border-radius:10px;"
                        )
                        ui.button("Back", on_click=stepper.previous).props("no-caps flat").style("color:#6B7280;")

                # ---- Step 3: AI Agent customization (optional) ----
                with ui.step("agent", "AI Agent", icon="smart_toy"):
                    ui.label("Customize your assistant").classes("font-bold text-gray-900")
                    ui.label("Optional — skip this and we'll use these defaults as-is.").classes("text-xs text-gray-500 mb-2")

                    _wizard_store2 = current_wizard_store()
                    cfg_defaults = repo.ensure_customization(_wizard_store2["$id"]) if _wizard_store2 else {}

                    name = ui.input("Agent name", value=cfg_defaults.get("agent_name", "AI Assistant")).classes("w-full")
                    welcome = ui.input("Welcome message", value=cfg_defaults.get("agent_title", "How can I help you today?")).classes("w-full")
                    instructions = ui.textarea("Agent instructions (optional)", value=cfg_defaults.get("instructions", "")).classes("w-full").props("rows=3")

                    def save_agent_and_continue():
                        store_now = current_wizard_store()
                        if store_now:
                            # Whatever's in these fields gets saved — if the
                            # user never touched them, that's just the same
                            # defaults being re-saved unchanged, so "skipping"
                            # this step naturally keeps the defaults.
                            repo.update_customization(
                                store_now["$id"],
                                agent_name=name.value.strip() or "AI Assistant",
                                agent_title=welcome.value.strip() or "How can I help you today?",
                                instructions=instructions.value.strip(),
                            )
                        stepper.next()

                    with ui.stepper_navigation():
                        ui.button("Continue", on_click=save_agent_and_continue).props("no-caps icon-right=arrow_forward").style(
                            f"background:{BRAND};color:white;border-radius:10px;"
                        )
                        ui.button("Back", on_click=stepper.previous).props("no-caps flat").style("color:#6B7280;")

                # ---- Step 4: Finish + third-party cookies notice ----
                with ui.step("finish", "Finish", icon="check_circle"):
                    ui.icon("celebration", size="32px").style(f"color:{BRAND};")
                    ui.label("You're all set!").classes("font-bold text-gray-900 text-lg mt-1")
                    ui.label("Your AI assistant is ready. One last thing before you go:").classes("text-sm text-gray-600 mb-2")

                    with ui.column().classes("w-full p-4 rounded-xl gap-1").style("background:#FEF3C7;"):
                        with ui.row().classes("items-center gap-2"):
                            ui.icon("cookie", size="16px").style("color:#92400E;")
                            ui.label("Please allow third-party cookies").classes("text-sm font-semibold text-gray-900")
                        ui.label(
                            "The live preview loads your store's own domain inside this dashboard. "
                            "If your browser blocks third-party cookies, you may see errors there. To avoid this:"
                        ).classes("text-xs text-gray-700")
                        ui.label('• Chrome: Settings → Privacy and security → Third-party cookies → Allow').classes("text-xs text-gray-600")
                        ui.label('• Safari: Settings → Privacy → uncheck "Prevent cross-site tracking"').classes("text-xs text-gray-600")
                        ui.label('• Firefox: Settings → Privacy & Security → Enhanced Tracking Protection → Standard').classes("text-xs text-gray-600")

                    def finish_setup():
                        store_now = current_wizard_store()
                        if store_now:
                            repo.mark_setup_complete(store_now["$id"])
                        _goto("/dashboard/Dashboard")

                    with ui.stepper_navigation():
                        ui.button("Go to Dashboard", on_click=finish_setup).props("no-caps icon-right=arrow_forward").classes("w-full").style(
                            f"background:{BRAND};color:white;border-radius:10px;"
                        )


# --------------------------------------------------------------------
# OVERVIEW
# --------------------------------------------------------------------
@ui.page("/dashboard/Dashboard")
def dashboard_page():
    store = _require_store()
    if not store:
        return
    cfg = repo.ensure_customization(store["$id"])
    features = repo.ensure_features(store["$id"])

    content = _layout("dashboard", store, cfg)
    with content:
        _page_header("Dashboard", "Here's what's happening with your AI assistant today.")

        # ---- Enabled Features (dropdown) ----
        with ui.card().classes(CARD_CLASSES + " p-2"):
            _features_dropdown(store, features)

        # ---- Agent Insights (single card, dropdown-driven — nothing is
        # shown by default except whichever insight is currently selected) ----
        with ui.card().classes(CARD_CLASSES + " p-5 gap-2"):
            with ui.row().classes("items-center justify-between mb-1"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("insights", size="17px").style(f"color:{BRAND};")
                    ui.label("Agent Insights").classes("font-bold text-gray-900 text-sm")

            _insight_options = {
                "cart_adds": "Agent-Assisted Cart Adds",
                "products": "What Shoppers Search For",
                "features": "Most-Used Agent Features",
            }
            _insight_area = ui.column().classes("w-full gap-2")

            def _render_insight(key: str):
                _insight_area.clear()
                with _insight_area:
                    if key == "cart_adds":
                        ui.label(
                            "The assistant hands checkout off to your store's own Shopify "
                            "cart, so this tracks successful 'add to cart' actions it "
                            "completed for shoppers — the closest signal to agent-driven "
                            "sales available from chat activity. Last 14 days."
                        ).classes("text-[11px] text-gray-400 mb-1")
                        conversions = repo.get_agent_conversions_by_day(store["$id"], days=14)
                        total = sum(row["count"] for row in conversions)
                        if total == 0:
                            ui.label(
                                "No agent-assisted cart adds yet. Once shoppers start adding "
                                "items via the assistant, the trend will show up here."
                            ).classes("text-xs text-gray-400 py-8 text-center w-full")
                        else:
                            ui.echart({
                                "grid": {"left": 32, "right": 12, "top": 12, "bottom": 24},
                                "xAxis": {"type": "category", "data": [row["date"][5:] for row in conversions],
                                          "axisLabel": {"fontSize": 10}, "axisLine": {"lineStyle": {"color": "#E5E7EB"}}},
                                "yAxis": {"type": "value", "minInterval": 1, "axisLabel": {"fontSize": 10},
                                          "splitLine": {"lineStyle": {"color": "#F3F4F6"}}},
                                "series": [{
                                    "type": "line", "data": [row["count"] for row in conversions],
                                    "smooth": True, "symbolSize": 6,
                                    "areaStyle": {"color": BRAND, "opacity": 0.08},
                                    "itemStyle": {"color": BRAND}, "lineStyle": {"color": BRAND, "width": 2},
                                }],
                                "tooltip": {"trigger": "axis"},
                            }).classes("w-full").style("height:200px;")

                    elif key == "products":
                        products = repo.get_top_searched_products(store["$id"], limit=8)
                        if not products:
                            ui.label("No product searches logged yet.").classes("text-xs text-gray-400 py-8 text-center w-full")
                        else:
                            ui.echart({
                                "tooltip": {"trigger": "item"},
                                "legend": {"bottom": 0, "textStyle": {"fontSize": 10}},
                                "series": [{
                                    "type": "pie", "radius": ["35%", "70%"],
                                    "data": [{"value": count, "name": name} for name, count in products],
                                    "label": {"fontSize": 10},
                                    "itemStyle": {"borderRadius": 4, "borderColor": "#fff", "borderWidth": 2},
                                }],
                                "color": ["#4B5563", "#6B7280", "#9CA3AF", "#374151", "#1F2937", "#D1D5DB", "#111827", "#E5E7EB"],
                            }).classes("w-full").style("height:280px;")

                    elif key == "features":
                        features_used = repo.get_top_features_used(store["$id"], limit=8)
                        if not features_used:
                            ui.label("No chat activity logged yet.").classes("text-xs text-gray-400 py-8 text-center w-full")
                        else:
                            flabels = [repo.INTENT_LABELS.get(k, (k.replace("_", " ").title(), "help_outline"))[0] for k, _ in reversed(features_used)]
                            fcounts = [v for _, v in reversed(features_used)]
                            ui.echart({
                                "grid": {"left": 130, "right": 20, "top": 6, "bottom": 6, "containLabel": False},
                                "xAxis": {"type": "value", "minInterval": 1, "axisLabel": {"fontSize": 10},
                                          "splitLine": {"lineStyle": {"color": "#F3F4F6"}}},
                                "yAxis": {"type": "category", "data": flabels, "axisLabel": {"fontSize": 10, "width": 120, "overflow": "truncate"}},
                                "series": [{"type": "bar", "data": fcounts, "itemStyle": {"color": "#6B7280", "borderRadius": [0, 4, 4, 0]}, "barMaxWidth": 16}],
                                "tooltip": {"trigger": "axis"},
                            }).classes("w-full").style(f"height:{max(180, 30 * len(features_used) + 20)}px;")

            ui.select(
                _insight_options, value="cart_adds",
                on_change=lambda e: _render_insight(e.value),
            ).props("dense outlined options-dense").classes("w-full").style("max-width:280px;")
            _render_insight("cart_adds")

        # ---- Quick Actions ----
        with ui.card().classes(CARD_CLASSES + " p-5 gap-1"):
            with ui.row().classes("items-center gap-2 mb-1"):
                ui.icon("bolt", size="17px").style(f"color:{BRAND};")
                ui.label("Quick Actions").classes("font-bold text-gray-900 text-sm")
            quick = [
                ("palette", "Customize Appearance", "Change colors & icon", "appearance"),
                ("menu_book", "Manage FAQs", "Add or edit knowledge base", "knowledge"),
                ("storefront", "Store Information", "Update your store details", "store"),
                ("smart_toy", "AI Agent Settings", "Name, instructions & status", "agent"),
            ]
            for icon, title, sub, page in quick:
                with ui.row().classes("w-full items-center gap-3 px-3 py-2.5 rounded-xl cursor-pointer hover:bg-gray-50").on(
                    "click", lambda p=page: _goto(f"/dashboard/{p}")
                ):
                    with ui.element("div").classes("w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0").style(
                        f"background:{BRAND_SOFT};"
                    ):
                        ui.icon(icon, size="15px").style(f"color:{BRAND};")
                    with ui.column().classes("gap-0 flex-1"):
                        ui.label(title).classes("text-sm font-bold text-gray-800")
                        ui.label(sub).classes("text-xs text-gray-500")
                    ui.icon("arrow_forward", size="13px").style("color:#D1D5DB;")

        ui.label("© 2024 RenderLink AI Assistant. All rights reserved.").classes("text-center text-xs text-gray-400 mt-2 w-full")


# --------------------------------------------------------------------
# STORE INFORMATION
# --------------------------------------------------------------------
@ui.page("/dashboard/store")
def store_info_page():
    store = _require_store()
    if not store:
        return
    cfg = repo.ensure_customization(store["$id"])
    info = repo.ensure_store_info(store["$id"])

    content = _layout("store", store, cfg)
    with content:
        _page_header("Store Information", "Basic details about your store.")
        with ui.card().classes(CARD_CLASSES + " p-6 gap-2"):
            business = ui.input("Business name", value=info.get("business_name", "")).classes("w-full")
            support = ui.input("Support email", value=info.get("support_email", "")).classes("w-full")
            timezone = ui.input("Timezone", value=info.get("timezone", "UTC")).classes("w-full")
            saved_label = ui.label("").classes("text-xs text-gray-500 font-medium")

            def save():
                repo.update_store_info(
                    store["$id"],
                    business_name=business.value.strip(),
                    support_email=support.value.strip(),
                    timezone=timezone.value.strip() or "UTC",
                )
                saved_label.text = "Saved ✓"

            ui.button("Save changes", on_click=save).props("no-caps").classes("mt-2").style(
                f"background:{BRAND};color:white;border-radius:10px;"
            )


# --------------------------------------------------------------------
# AI AGENT
# --------------------------------------------------------------------
@ui.page("/dashboard/agent")
def agent_page():
    store = _require_store()
    if not store:
        return
    cfg = repo.ensure_customization(store["$id"])

    content = _layout("agent", store, cfg)
    with content:
        _page_header("AI Agent", "How your assistant introduces itself, behaves, and whether it's live.")

        agent_color = cfg.get("theme_color") or BRAND
        s = STATUS_STYLES.get(cfg.get("status", "active"), STATUS_STYLES["active"])

        # ---- Display card (moved here from Dashboard) — read-only, with
        #      the embedded Live Preview. Clicking Edit/Customize opens
        #      the popup below where the actual changes are made. ----
        with ui.card().classes(CARD_CLASSES + " p-6 gap-4"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("smart_toy", size="18px").style(f"color:{agent_color};")
                ui.label("AI Agent").classes("font-bold text-gray-900")

            with ui.row().classes("w-full gap-6 items-start"):
                with ui.element("div").classes("w-20 h-20 rounded-full flex items-center justify-center flex-shrink-0").style(
                    f"background:{BRAND_SOFT};"
                ):
                    ui.icon("smart_toy", size="34px").style(f"color:{agent_color};")

                with ui.row().classes("flex-1 gap-8"):
                    with ui.column().classes("gap-0.5"):
                        ui.label("Agent Name").classes("text-xs text-gray-500")
                        with ui.row().classes("items-center gap-2"):
                            name_label = ui.label(cfg.get("agent_name", "")).classes("font-bold text-gray-900")
                            edit_btn = ui.button("Edit").props("no-caps flat dense").classes(
                                "text-[11px] font-semibold px-2 py-0.5"
                            ).style(f"background:{BRAND_SOFT};color:{agent_color};border-radius:999px;min-height:0;")
                        ui.label("Welcome Message").classes("text-xs text-gray-500 mt-2")
                        welcome_label = ui.label(cfg.get("agent_title", "")).classes(
                            "text-sm text-gray-700 rounded-lg px-3 py-2"
                        ).style("background:#F9FAFB;")
                        ui.label("Status").classes("text-xs text-gray-500 mt-2")
                        status_badge = ui.badge(s["label"]).style(f"background:{s['bg']};color:{s['text']};")
                        manage_btn = ui.button("Customize Agent", icon="edit").props("no-caps flat").classes(
                            "mt-1 text-xs font-semibold"
                        ).style(f"background:{BRAND_SOFT};color:{agent_color};border-radius:8px;")

                    with ui.column().classes("gap-0.5"):
                        with ui.row().classes("items-center gap-1.5"):
                            ui.icon("desktop_windows", size="13px").style("color:#6B7280;")
                            ui.label("Widget Live Preview").classes("text-xs text-gray-500")
                        with ui.column().classes("relative p-3 gap-0").style(
                            f"background:{BRAND_SOFT};border-radius:12px;height:128px;width:220px;"
                        ):
                            with ui.column().classes("p-2.5 gap-0").style(
                                "background:white;border-radius:12px;border-top-left-radius:0;box-shadow:0 1px 2px rgba(0,0,0,0.06);max-width:85%;"
                            ):
                                preview_msg_label = ui.label(cfg.get("agent_title", "")).classes("text-xs text-gray-800")
                                ui.label("10:30 AM").classes("text-[10px] text-gray-400")
                            with ui.element("div").classes("flex items-center justify-center").style(
                                f"position:absolute;bottom:10px;right:10px;width:36px;height:36px;border-radius:50%;"
                                f"background:{agent_color};box-shadow:0 4px 10px rgba(0,0,0,0.15);"
                            ):
                                ui.icon("forum", size="15px").style("color:white;")
                        ui.button(
                            "Open full preview", icon="arrow_forward", on_click=lambda: _open_preview(store)
                        ).props("no-caps flat icon-right=arrow_forward").classes("text-xs font-semibold mt-1 text-gray-700")

        # ---- Customize popup — this is where changes actually happen ----
        def open_customize_dialog():
            with ui.dialog() as dialog, ui.card().classes("p-6 gap-2 w-full max-w-md"):
                ui.label("Customize Agent").classes("text-lg font-bold text-gray-900")
                ui.label("Update how your assistant appears and behaves.").classes("text-xs text-gray-500 mb-2")

                name_input = ui.input("Agent name", value=cfg.get("agent_name", "")).classes("w-full")
                welcome_input = ui.input("Welcome message", value=cfg.get("agent_title", "")).classes("w-full")
                instructions_input = ui.textarea("Agent instructions", value=cfg.get("instructions", "")).classes(
                    "w-full"
                ).props("rows=5")

                ui.label("Status").classes("text-xs font-semibold text-gray-600 mt-1")
                status_value = {"v": cfg.get("status", "active")}
                with ui.row().classes("gap-2") as status_row:
                    pass

                def render_status_buttons():
                    status_row.clear()
                    with status_row:
                        for key, st in STATUS_STYLES.items():
                            selected = status_value["v"] == key
                            with ui.button(
                                on_click=lambda k=key: (status_value.update(v=k), render_status_buttons())
                            ).props("no-caps flat") as b:
                                with ui.row().classes("items-center gap-1.5 no-wrap"):
                                    ui.element("div").classes("w-2 h-2 rounded-full").style(
                                        f"background:{st['dot']};flex-shrink:0;"
                                    )
                                    ui.label(st["label"])
                            if selected:
                                b.style(f"background:{st['bg']};color:{st['text']};border-radius:999px;border:1px solid {st['dot']};")
                            else:
                                b.style("background:white;color:#6B7280;border-radius:999px;border:1px solid #E5E7EB;")

                render_status_buttons()
                ui.label('"Inactive" or "Maintenance" stops the widget from responding to shoppers.').classes(
                    "text-[11px] text-gray-400"
                )

                error_label = ui.label("").classes("text-xs text-gray-500 mt-1")

                def save():
                    new_name = (name_input.value or "").strip() or "AI Assistant"
                    new_welcome = (welcome_input.value or "").strip() or "How can I help you today?"
                    new_instructions = (instructions_input.value or "").strip()
                    new_status = status_value["v"]

                    repo.update_customization(
                        store["$id"],
                        agent_name=new_name,
                        agent_title=new_welcome,
                        instructions=new_instructions,
                        status=new_status,
                    )

                    # reflect the change immediately, no page reload needed
                    cfg["agent_name"] = new_name
                    cfg["agent_title"] = new_welcome
                    cfg["instructions"] = new_instructions
                    cfg["status"] = new_status

                    name_label.text = new_name
                    welcome_label.text = new_welcome
                    preview_msg_label.text = new_welcome
                    new_s = STATUS_STYLES.get(new_status, STATUS_STYLES["active"])
                    status_badge.text = new_s["label"]
                    status_badge.style(f"background:{new_s['bg']};color:{new_s['text']};")

                    dialog.close()
                    ui.notify("Agent updated ✓", type="positive")

                with ui.row().classes("gap-2 mt-3 w-full justify-end"):
                    ui.button("Cancel", on_click=dialog.close).props("no-caps flat").style(
                        "color:#6B7280;"
                    )
                    ui.button("Save changes", on_click=save).props("no-caps").style(
                        f"background:{BRAND};color:white;border-radius:10px;"
                    )
            dialog.open()

        edit_btn.on_click(open_customize_dialog)
        manage_btn.on_click(open_customize_dialog)


# --------------------------------------------------------------------
# FEATURES — full grid page (reached via sidebar nav)
# --------------------------------------------------------------------
@ui.page("/dashboard/features")
def features_page():
    store = _require_store()
    if not store:
        return
    cfg = repo.ensure_customization(store["$id"])
    features = repo.ensure_features(store["$id"])

    content = _layout("features", store, cfg)
    with content:
        _page_header("Features", "Choose what your AI assistant can do for your customers.")
        with ui.card().classes(CARD_CLASSES + " p-6"):
            with ui.grid(columns=2).classes("w-full gap-3"):
                for key, label, icon in FEATURE_LIST:
                    with ui.row().classes("items-center justify-between px-4 py-3 rounded-xl").style(f"background:{BRAND_SOFT};"):
                        with ui.row().classes("items-center gap-2"):
                            ui.icon(icon, size="15px").style("color:#4B5563;")
                            ui.label(label).classes("text-sm font-medium text-gray-700")

                        def on_toggle(e, k=key):
                            repo.update_features(store["$id"], **{k: e.value})

                        ui.switch(value=bool(features.get(key)), on_change=on_toggle).props("color=grey-8")


# --------------------------------------------------------------------
# APPEARANCE
# --------------------------------------------------------------------
@ui.page("/dashboard/appearance")
def appearance_page():
    store = _require_store()
    if not store:
        return
    cfg = repo.ensure_customization(store["$id"])

    content = _layout("appearance", store, cfg)
    with content:
        _page_header("Appearance", "Customize how the chat widget looks on your storefront.")

        color_value = {"v": cfg.get("theme_color", "#2b2b2b")}

        with ui.card().classes(CARD_CLASSES + " p-6 gap-3"):
            ui.label("Theme color").classes("text-xs font-semibold text-gray-600")
            with ui.row().classes("gap-2") as swatch_row:
                pass

            def render_swatches():
                swatch_row.clear()
                with swatch_row:
                    for c in THEME_SWATCHES:
                        selected = color_value["v"] == c
                        dot = ui.element("div").classes("w-8 h-8 rounded-full cursor-pointer").style(
                            f"background:{c};border:3px solid {'#111827' if selected else 'transparent'};"
                        )
                        dot.on("click", lambda c=c: (color_value.update(v=c), render_swatches()))

            render_swatches()

            welcome = ui.input("Welcome message", value=cfg.get("agent_title", "")).classes("w-full")

            saved_label = ui.label("").classes("text-xs text-gray-500 font-medium")

            def save():
                repo.update_customization(
                    store["$id"],
                    theme_color=color_value["v"],
                    agent_title=welcome.value.strip() or cfg.get("agent_title", ""),
                )
                saved_label.text = "Saved ✓"

            with ui.row().classes("items-center gap-2 mt-2"):
                ui.button("Save changes", on_click=save).props("no-caps").style(
                    f"background:{BRAND};color:white;border-radius:10px;"
                )
                ui.button("Preview", icon="visibility", on_click=lambda: _open_preview(store)).props("no-caps flat").style(
                    f"border:1px solid #E5E7EB;color:#4B5563;border-radius:10px;"
                )

        with ui.card().classes(CARD_CLASSES + " p-6 gap-2"):
            ui.label("Custom icon image").classes("text-xs font-semibold text-gray-600")
            icon_preview_row = ui.row().classes("items-center gap-3")
            with icon_preview_row:
                if cfg.get("custom_icon_url"):
                    ui.image(cfg["custom_icon_url"]).classes("w-10 h-10 rounded-full")

                    def delete_icon():
                        repo.delete_icon_file(cfg.get("custom_icon_url", ""))
                        repo.update_customization(store["$id"], custom_icon_url="", icon_type="preset")
                        icon_preview_row.clear()
                        ui.notify("Icon removed — refresh to see it applied.", type="positive")

                    ui.button("Remove icon", icon="delete", on_click=delete_icon).props(
                        "no-caps flat dense"
                    ).style("color:#DC2626;")

            def handle_upload(e):
                content = e.content.read()
                new_url = repo.upload_icon_file(store["$id"], e.name, content)
                old_url = cfg.get("custom_icon_url", "")
                repo.update_customization(store["$id"], custom_icon_url=new_url, icon_type="custom")
                if old_url:
                    repo.delete_icon_file(old_url)
                ui.notify("Icon uploaded — refresh to see it applied.", type="positive")

            ui.upload(on_upload=handle_upload, auto_upload=True).props("accept=image/*").classes("w-full")


# --------------------------------------------------------------------
# KNOWLEDGE / FAQs
# --------------------------------------------------------------------
@ui.page("/dashboard/knowledge")
def knowledge_page():
    store = _require_store()
    if not store:
        return
    cfg = repo.ensure_customization(store["$id"])

    content = _layout("knowledge", store, cfg)
    with content:
        _page_header("Knowledge (FAQs)", "Answers your agent can pull from directly.")

        with ui.card().classes(CARD_CLASSES + " p-6 gap-2"):
            question = ui.input("Question", placeholder="How long does shipping take?").classes("w-full")
            answer = ui.input("Answer", placeholder="3-5 business days.").classes("w-full")
            error_label = ui.label("").classes("text-xs text-gray-500")

            def add_faq():
                if not question.value.strip() or not answer.value.strip():
                    error_label.text = "Both a question and an answer are required."
                    return
                repo.add_faq(store["$id"], question.value.strip(), answer.value.strip())
                question.value = ""
                answer.value = ""
                error_label.text = ""
                render_faqs()

            ui.button("Add FAQ", on_click=add_faq).props("no-caps").classes("mt-1").style(
                f"background:{BRAND};color:white;border-radius:10px;"
            )

        faq_list = ui.column().classes("w-full gap-2")

        def render_faqs():
            faq_list.clear()
            faqs = repo.list_faqs(store["$id"])
            with faq_list:
                if not faqs:
                    ui.label("No FAQs yet — add your first one above.").classes("text-xs text-gray-400")
                for f in faqs:
                    with ui.row().classes(CARD_CLASSES + " p-4 items-start justify-between"):
                        with ui.column().classes("gap-0.5"):
                            ui.label(f["question"]).classes("text-sm font-semibold text-gray-800")
                            ui.label(f["answer"]).classes("text-xs text-gray-500")

                        def delete_faq(faq_id=f["$id"]):
                            repo.delete_faq(store["$id"], faq_id)
                            render_faqs()

                        ui.button(icon="delete", on_click=delete_faq).props("flat round dense").style("color:#D1D5DB;")

        render_faqs()


# --------------------------------------------------------------------
# FEEDBACK
# --------------------------------------------------------------------
@ui.page("/dashboard/feedback")
def feedback_page():
    store = _require_store()
    if not store:
        return
    cfg = repo.ensure_customization(store["$id"])

    content = _layout("feedback", store, cfg)
    with content:
        _page_header("Feedback & Help", "Tell us what's working, what's not, or request a feature.")
        with ui.card().classes(CARD_CLASSES + " p-6 gap-2"):
            message = ui.textarea("Your message", placeholder="I'd love to be able to...").classes("w-full").props("rows=4")
            sent_label = ui.label("").classes("text-xs text-gray-500 font-medium")

            def send():
                if not message.value.strip():
                    return
                repo.submit_feedback(store["$id"], message.value.strip())
                message.value = ""
                sent_label.text = "Thanks — we got it ✓"

            ui.button("Send feedback", on_click=send).props("no-caps").classes("mt-1").style(
                f"background:{BRAND};color:white;border-radius:10px;"
            )
