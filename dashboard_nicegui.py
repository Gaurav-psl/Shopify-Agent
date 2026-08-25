"""
dashboard_nicegui.py
---------------------
The full "RenderLink" dashboard (sidebar nav, Overview, Store Info,
AI Agent, Features, Appearance, Knowledge/FAQs, Feedback) built with
NiceGUI — pure Python, reactive components (switches flip instantly,
no full-page reloads needed like the plain-HTML version), no JSX, no
npm, no build step.

HOW THIS MOUNTS INTO YOUR EXISTING FASTAPI APP
------------------------------------------------
NiceGUI runs on top of FastAPI. `@ui.page(...)` below registers real
routes on your `app` the same way `@router.get(...)` does elsewhere in
your project — you just attach it once, at the bottom of main.py:

    from nicegui import ui
    import dashboard_nicegui  # noqa: F401 — importing registers the @ui.page routes
    ui.run_with(app, storage_secret=os.environ.get("SESSION_SECRET", "change-me"))

Because this file's pages already live at /dashboard/..., it takes
over the exact same URLs your old HTML dashboard.py used — so REMOVE
(or comment out) `app.include_router(dashboard.router)` in main.py so
the two don't fight over the same routes. dashboard_api.py can stay or
go; this file doesn't use it (it talks to repository_appwrite.py
directly, same as dashboard.py did).

SESSIONS
--------
NiceGUI ships its own per-browser storage (`app.storage.user`), backed
by a signed cookie — separate from Starlette's SessionMiddleware used
by dashboard_api.py. That's fine since this file is self-contained; it
doesn't need to share a session with the JSON API.

Run: pip install nicegui
"""

import os

import bcrypt
from nicegui import ui, app

import repository_appwrite as repo

BRAND = "#4B5563"
BRAND_SOFT = "#F3F4F6"
PAGE_BG = "#F3F4F6"

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "static/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

NAV_ITEMS = [
    ("overview", "Overview", "home"),
    ("store", "Store Information", "store"),
    ("agent", "AI Agent", "smart_toy"),
    ("features", "Features", "grid_view"),
    ("appearance", "Appearance", "palette"),
    ("knowledge", "Knowledge (FAQs)", "menu_book"),
    ("feedback", "Feedback & Help", "help"),
]

FEATURE_LIST = [
    ("product_search", "Product Search", "search"),
    ("recommendations", "Recommendations", "auto_awesome"),
    ("product_filtering", "Product Filtering", "filter_alt"),
    ("warranty", "Warranty", "verified"),
    ("cart_editing", "Cart Editing", "shopping_cart"),
    ("returns", "Returns", "replay"),
    ("track_orders", "Track Orders", "local_shipping"),
]

STATUS_STYLES = {
    "active": {"bg": "#E5E7EB", "text": "#111827", "dot": "#4B5563", "label": "Active"},
    "inactive": {"bg": "#F3F4F6", "text": "#9CA3AF", "dot": "#D1D5DB", "label": "Inactive"},
    "maintenance": {"bg": "#F3F4F6", "text": "#6B7280", "dot": "#9CA3AF", "label": "Maintenance"},
}

THEME_SWATCHES = ["#4B5563", "#6B7280", "#9CA3AF", "#374151", "#1F2937"]

CARD_CLASSES = "bg-white rounded-2xl border border-gray-100 shadow-sm w-full"


# --------------------------------------------------------------------
# Session helpers — backed by NiceGUI's app.storage.user
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
    """Returns the store, or navigates to /login and returns None."""
    store = _current_store()
    if not store:
        ui.navigate.to("/dashboard/login")
        return None
    return store


def _logout():
    app.storage.user.clear()
    ui.navigate.to("/dashboard/login")


# --------------------------------------------------------------------
# Shared layout — header + sidebar, wraps every logged-in page
# --------------------------------------------------------------------
def _layout(active_key: str, store: dict, agent: dict):
    """Builds the header + sidebar chrome, and returns the content
    container to add page-specific content into."""
    s = STATUS_STYLES.get(agent.get("status", "active"), STATUS_STYLES["active"])

    with ui.header().classes("items-center justify-between px-6").style(
        "background:white;border-bottom:1px solid #F3F4F6;color:#111827;height:60px;"
    ):
        with ui.row().classes("items-center gap-2"):
            ui.icon("store", size="18px").style("color:#374151;")
            ui.label(store["shop_domain"]).classes("text-sm font-semibold")
        with ui.row().classes("items-center gap-2"):
            ui.badge(s["label"]).style(f"background:{s['bg']};color:{s['text']};font-weight:700;")

    with ui.left_drawer(fixed=True).classes("bg-white").style("border-right:1px solid #F3F4F6;padding:0;"):
        with ui.column().classes("w-full h-full justify-between p-3"):
            with ui.column().classes("w-full gap-1"):
                with ui.row().classes("items-center gap-2 p-3 mb-2"):
                    with ui.element("div").classes("w-9 h-9 rounded-xl flex items-center justify-center").style(f"background:{BRAND};"):
                        ui.icon("forum", size="18px").style("color:white;")
                    with ui.column().classes("gap-0"):
                        ui.label("RenderLink").classes("font-bold text-gray-900 leading-tight")
                        ui.label("AI Shopping Assistant").classes("text-[11px] text-gray-400 leading-tight")

                for key, label, icon in NAV_ITEMS:
                    is_active = key == active_key
                    btn = ui.button(label, icon=icon, on_click=lambda k=key: ui.navigate.to(f"/dashboard/{k}"))
                    btn.props("flat align=left no-caps").classes("w-full justify-start")
                    if is_active:
                        btn.style(f"background:{BRAND};color:white;border-radius:10px;")
                    else:
                        btn.style("color:#4B5563;border-radius:10px;")

            with ui.column().classes("w-full gap-2"):
                ui.button("Log out", icon="logout", on_click=_logout).props("flat no-caps").classes("w-full").style(
                    "border:1px solid #E5E7EB;border-radius:10px;color:#4B5563;"
                )

    content = ui.column().classes("w-full max-w-3xl mx-auto p-6 gap-4")
    return content


def _page_header(title: str, subtitle: str):
    ui.label(title).classes("text-2xl font-bold text-gray-900")
    ui.label(subtitle).classes("text-sm text-gray-500 -mt-3")


# --------------------------------------------------------------------
# AUTH — Signup / Login
# --------------------------------------------------------------------
def _auth_shell():
    ui.query("body").style(f"background:{PAGE_BG};")
    outer = ui.column().classes("w-full items-center justify-center").style("min-height:100vh;")
    return outer


@ui.page("/dashboard/signup")
def signup_page(shop: str = ""):
    with _auth_shell():
        with ui.column().classes("items-center gap-2 mb-2"):
            with ui.element("div").classes("w-10 h-10 rounded-xl flex items-center justify-center").style(f"background:{BRAND};"):
                ui.icon("forum", size="20px").style("color:white;")
            ui.label("RenderLink").classes("text-xl font-bold text-gray-900")

        with ui.card().classes("p-7 w-full max-w-sm gap-1"):
            ui.label("Set up your dashboard").classes("text-lg font-bold text-gray-900")
            ui.label("Create your login to manage your store's AI assistant.").classes("text-sm text-gray-500 mb-3")

            store_domain = ui.input("Store domain", value=shop, placeholder="myshop.myshopify.com").classes("w-full")
            email = ui.input("Email", placeholder="you@myshop.com").classes("w-full")
            password = ui.input("Password", password=True, password_toggle_button=True, placeholder="At least 8 characters").classes("w-full")
            error_label = ui.label("").classes("text-xs text-gray-500")

            def submit():
                if not store_domain.value.strip() or not email.value.strip() or len(password.value) < 8:
                    error_label.text = "Fill in every field — password needs at least 8 characters."
                    return
                store = repo.get_store(store_domain.value.strip())
                if not store:
                    error_label.text = "Store not found — please reinstall the app."
                    return
                if repo.has_dashboard_user(store["$id"]):
                    error_label.text = "This store already has a dashboard login — try logging in instead."
                    return
                if repo.get_dashboard_user_by_email(email.value.strip()):
                    error_label.text = "That email is already in use."
                    return
                user = repo.create_dashboard_user(
                    store["$id"], email.value.strip(),
                    bcrypt.hashpw(password.value.encode(), bcrypt.gensalt()).decode(),
                )
                app.storage.user["store_id"] = store["$id"]
                app.storage.user["user_id"] = user["$id"]
                app.storage.user["email"] = user["email"]
                ui.navigate.to("/dashboard/overview")

            ui.button("Create account", on_click=submit).props("no-caps").classes("w-full mt-3").style(
                f"background:{BRAND};color:white;border-radius:10px;"
            )
            with ui.row().classes("w-full justify-center mt-2"):
                ui.label("Already have an account?").classes("text-xs text-gray-500")
                ui.link("Log in", "/dashboard/login").classes("text-xs font-semibold text-gray-800")


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
                store_id = user["store"]["$id"] if isinstance(user["store"], dict) else user["store"]
                app.storage.user["store_id"] = store_id
                app.storage.user["user_id"] = user["$id"]
                app.storage.user["email"] = user["email"]
                ui.navigate.to("/dashboard/overview")

            ui.button("Log in", on_click=submit).props("no-caps").classes("w-full mt-3").style(
                f"background:{BRAND};color:white;border-radius:10px;"
            )
            with ui.row().classes("w-full justify-center mt-2"):
                ui.label("New here?").classes("text-xs text-gray-500")
                ui.link("Create an account", "/dashboard/signup").classes("text-xs font-semibold text-gray-800")


@ui.page("/dashboard")
def dashboard_root():
    ui.navigate.to("/dashboard/overview")


# --------------------------------------------------------------------
# OVERVIEW
# --------------------------------------------------------------------
@ui.page("/dashboard/overview")
def overview_page():
    store = _require_store()
    if not store:
        return
    cfg = repo.ensure_customization(store["$id"])
    features = repo.ensure_features(store["$id"])
    s = STATUS_STYLES.get(cfg.get("status", "active"), STATUS_STYLES["active"])

    content = _layout("overview", store, cfg)
    with content:
        _page_header("Overview", "Here's what's happening with your AI assistant today.")

        with ui.card().classes(CARD_CLASSES + " p-5 gap-3"):
            ui.label("AI Agent").classes("font-bold text-gray-900")
            with ui.row().classes("w-full gap-8"):
                with ui.column().classes("gap-0.5"):
                    ui.label("Agent Name").classes("text-xs text-gray-500")
                    ui.label(cfg.get("agent_name", "")).classes("font-bold")
                    ui.label("Welcome Message").classes("text-xs text-gray-500 mt-2")
                    ui.label(cfg.get("agent_title", "")).classes("text-sm bg-gray-50 rounded-lg px-3 py-2")
                    ui.label("Status").classes("text-xs text-gray-500 mt-2")
                    ui.badge(s["label"]).style(f"background:{s['bg']};color:{s['text']};")
                    ui.button("Manage Agent", icon="settings", on_click=lambda: ui.navigate.to("/dashboard/agent")).props(
                        "no-caps flat"
                    ).classes("mt-2").style(f"background:{BRAND_SOFT};color:{BRAND};border-radius:8px;")

        with ui.card().classes(CARD_CLASSES + " p-5 gap-2"):
            ui.label("Enabled Features").classes("font-bold text-gray-900 mb-1")
            for key, label, icon in FEATURE_LIST:
                with ui.row().classes("w-full items-center justify-between px-3 py-2 rounded-xl").style(f"background:{BRAND_SOFT};"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon(icon, size="16px").style("color:#4B5563;")
                        ui.label(label).classes("text-sm text-gray-700")
                    ui.label("On" if features.get(key) else "Off").classes("text-[11px] font-bold text-gray-400")
            ui.button("Manage Features", icon="tune", on_click=lambda: ui.navigate.to("/dashboard/features")).props(
                "no-caps flat"
            ).classes("mt-1").style(f"background:{BRAND_SOFT};color:{BRAND};border-radius:8px;")

        with ui.card().classes(CARD_CLASSES + " p-5 gap-1"):
            ui.label("Quick Actions").classes("font-bold text-gray-900 mb-1")
            quick = [
                ("palette", "Customize Appearance", "Change colors, icon & position", "appearance"),
                ("menu_book", "Manage FAQs", "Add or edit knowledge base", "knowledge"),
                ("store", "Store Information", "Update your store details", "store"),
                ("smart_toy", "AI Agent Settings", "Name, instructions & status", "agent"),
            ]
            for icon, title, sub, page in quick:
                with ui.row().classes("w-full items-center gap-3 px-3 py-2 rounded-xl cursor-pointer hover:bg-gray-50").on(
                    "click", lambda p=page: ui.navigate.to(f"/dashboard/{p}")
                ):
                    with ui.element("div").classes("w-9 h-9 rounded-lg flex items-center justify-center").style(f"background:{BRAND_SOFT};"):
                        ui.icon(icon, size="16px").style(f"color:{BRAND};")
                    with ui.column().classes("gap-0"):
                        ui.label(title).classes("text-sm font-bold text-gray-800")
                        ui.label(sub).classes("text-xs text-gray-500")


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
        with ui.card().classes(CARD_CLASSES + " p-6 gap-2"):
            name = ui.input("Agent name", value=cfg.get("agent_name", "")).classes("w-full")
            welcome = ui.input("Welcome message", value=cfg.get("agent_title", "")).classes("w-full")
            instructions = ui.textarea("Agent instructions", value=cfg.get("instructions", "")).classes("w-full").props("rows=5")

            ui.label("Status").classes("text-xs font-semibold text-gray-600 mt-2")
            status_value = {"v": cfg.get("status", "active")}
            with ui.row().classes("gap-2") as status_row:
                pass

            def render_status_buttons():
                status_row.clear()
                with status_row:
                    for key, s in STATUS_STYLES.items():
                        selected = status_value["v"] == key
                        b = ui.button(s["label"], on_click=lambda k=key: (status_value.update(v=k), render_status_buttons())).props("no-caps flat")
                        if selected:
                            b.style(f"background:{s['bg']};color:{s['text']};border-radius:999px;border:1px solid {s['dot']};")
                        else:
                            b.style("background:white;color:#6B7280;border-radius:999px;border:1px solid #E5E7EB;")

            render_status_buttons()
            ui.label('"Inactive" or "Maintenance" stops the widget from responding to shoppers.').classes("text-[11px] text-gray-400")

            saved_label = ui.label("").classes("text-xs text-gray-500 font-medium")

            def save():
                repo.update_customization(
                    store["$id"],
                    agent_name=name.value.strip() or "AI Assistant",
                    agent_title=welcome.value.strip() or "How can I help you today?",
                    instructions=instructions.value.strip(),
                    status=status_value["v"],
                )
                saved_label.text = "Saved ✓"

            ui.button("Save changes", on_click=save).props("no-caps").classes("mt-2").style(
                f"background:{BRAND};color:white;border-radius:10px;"
            )


# --------------------------------------------------------------------
# FEATURES — toggles switch instantly, no save button needed
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
        with ui.card().classes(CARD_CLASSES + " p-6 gap-2"):
            for key, label, icon in FEATURE_LIST:
                with ui.row().classes("w-full items-center justify-between px-4 py-3 rounded-xl").style(f"background:{BRAND_SOFT};"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon(icon, size="16px").style("color:#4B5563;")
                        ui.label(label).classes("text-sm font-medium text-gray-700")

                    def on_toggle(e, k=key):
                        repo.update_features(store["$id"], **{k: e.value})

                    ui.switch(value=bool(features.get(key)), on_change=on_toggle).props(f'color=grey-8')


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
        position_value = {"v": cfg.get("widget_position", "bottom-right")}

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

            ui.label("Widget position").classes("text-xs font-semibold text-gray-600 mt-1")
            with ui.row().classes("gap-2") as position_row:
                pass

            def render_positions():
                position_row.clear()
                with position_row:
                    for p, label in [("bottom-right", "Bottom right"), ("bottom-left", "Bottom left")]:
                        selected = position_value["v"] == p
                        b = ui.button(label, on_click=lambda p=p: (position_value.update(v=p), render_positions())).props("no-caps flat")
                        if selected:
                            b.style(f"background:{BRAND_SOFT};color:{BRAND};border-radius:8px;border:1px solid {BRAND};")
                        else:
                            b.style("background:white;color:#6B7280;border-radius:8px;border:1px solid #E5E7EB;")

            render_positions()

            saved_label = ui.label("").classes("text-xs text-gray-500 font-medium")

            def save():
                repo.update_customization(
                    store["$id"],
                    theme_color=color_value["v"],
                    widget_position=position_value["v"],
                    agent_title=welcome.value.strip() or cfg.get("agent_title", ""),
                )
                saved_label.text = "Saved ✓"

            ui.button("Save changes", on_click=save).props("no-caps").classes("mt-2").style(
                f"background:{BRAND};color:white;border-radius:10px;"
            )

        with ui.card().classes(CARD_CLASSES + " p-6 gap-2"):
            ui.label("Custom icon image").classes("text-xs font-semibold text-gray-600")
            if cfg.get("custom_icon_url"):
                ui.image(cfg["custom_icon_url"]).classes("w-10 h-10 rounded-full")

            def handle_upload(e):
                import shutil
                import uuid as _uuid

                ext = os.path.splitext(e.name)[1] or ".png"
                filename = f"{store['$id']}_{_uuid.uuid4().hex}{ext}"
                filepath = os.path.join(UPLOAD_DIR, filename)
                with open(filepath, "wb") as f:
                    shutil.copyfileobj(e.content, f)
                repo.update_customization(store["$id"], custom_icon_url=f"/{filepath}", icon_type="custom")
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
                    with ui.card().classes(CARD_CLASSES + " p-4 flex-row items-start justify-between"):
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