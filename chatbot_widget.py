"""
chatbot_widget.py
------------------
Multi-tenant: one deployment serves every installed store. Each store
embeds the same script tag (shown to them on their dashboard, see
dashboard.py):

    <script src="https://your-app-domain.com/widget.js"
            data-shop="{shop}.myshopify.com" defer></script>

Routes:
  GET  /widget.js       -> the embeddable widget (same file for every
                            store; it fetches its own store's config at
                            runtime from /widget-config)
  GET  /widget-config    -> per-store customization (name, title, icon)
                            as JSON, keyed by ?shop=xxx.myshopify.com
  POST /chat             -> classify the message (intent_classifier.py),
                            execute it (shopify_actions.py) or ask for
                            confirmation first, then phrase the reply in
                            the shopper's own language (reply_generator.py)
  POST /confirm          -> confirm/cancel a pending action (used by the
                            REST contract; the widget itself just lets the
                            shopper type "yes"/"no" back into /chat)

Cart mutations are NOT done server-side — see the big comment in
shopify_actions.py. Instead the backend returns a `widget_action` and
widget.js performs the real fetch() against the store's own
`/cart/add.js` etc, in the shopper's browser, same-origin with the store.
"""

import time
from types import SimpleNamespace
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

import repository_appwrite as repo
from intent_classifier import classify_intent, load_schema
from reply_generator import generate_reply
import shopify_actions

router = APIRouter(tags=["chatbot-widget"])

SCHEMA = load_schema()

# ---------------------------------------------------------------------
# Pending-confirmation cache. Keyed by session_id (the widget's random
# per-tab id). Deliberately in-memory + short-lived: it only bridges the
# single "are you sure?" round trip, so it doesn't need a database row
# and is wiped on every deploy/restart, which is fine for that purpose.
# ---------------------------------------------------------------------
PENDING: dict[str, dict] = {}
PENDING_TTL_SECONDS = 300

_AFFIRMATIVE = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "confirm", "confirmed",
                "si", "sí", "oui", "haan", "ha", "theek hai"}
_NEGATIVE = {"no", "n", "nope", "cancel", "nah", "non", "nahi"}


def _prune_pending():
    now = time.time()
    for k in [k for k, v in PENDING.items() if v["expires"] < now]:
        PENDING.pop(k, None)


def _split_widget_action(data: dict) -> tuple[dict, dict | None]:
    """widget_action is an instruction for the browser (redirect, cart
    mutation, ...) — pull it out before handing `data` to the LLM reply
    generator, which should only ever see user-facing facts."""
    if not isinstance(data, dict):
        return data, None
    action = data.get("widget_action")
    if action is None:
        return data, None
    clean = {k: v for k, v in data.items() if k != "widget_action"}
    return clean, action


class ChatRequest(BaseModel):
    message: str
    session_id: str = "anonymous"
    shop: str


class ConfirmRequest(BaseModel):
    shop: str
    session_id: str = "anonymous"
    confirmed: bool


def _get_store(shop: str) -> SimpleNamespace | None:
    """Returns a lightweight object with .shop_domain and .access_token
    attributes — shopify_actions.py was written expecting attribute
    access (store.access_token), and Appwrite documents are plain dicts
    (store["access_token"]), so this wrapper bridges the two without
    needing to touch a single line of shopify_actions.py."""
    doc = repo.get_store(shop)
    if not doc:
        return None
    return SimpleNamespace(shop_domain=doc["shop_domain"], access_token=doc["access_token"], id=doc["$id"])


async def _execute_and_reply(store: SimpleNamespace, intent: str, action: str, entities: dict, language: str, original_message: str) -> dict:
    raw = await shopify_actions.dispatch(intent, action, store, entities)
    data, widget_action = _split_widget_action(raw)
    reply = generate_reply(action, data, language, original_message)
    out = {"status": "done", "reply": reply, "language": language, "intent": intent, "action": action}
    if widget_action:
        out["widget_action"] = widget_action
    return out


@router.post("/chat")
async def chat(req: ChatRequest):
    message = (req.message or "").strip()
    if not message:
        return {"reply": "Could you type or say something first?"}

    store = _get_store(req.shop)
    if not store:
        return {"reply": "Sorry, I couldn't verify this store. Please reload the page and try again."}

    _prune_pending()
    pending = PENDING.get(req.session_id)

    if pending:
        text = message.lower().strip(" .!")
        if text in _AFFIRMATIVE:
            PENDING.pop(req.session_id, None)
            return await _execute_and_reply(store, pending["intent"], pending["action"], pending["entities"], pending["language"], message)
        if text in _NEGATIVE:
            PENDING.pop(req.session_id, None)
            cancel_reply = generate_reply("cancelled", {"message": "The shopper decided not to proceed."}, pending["language"], message)
            return {"status": "cancelled", "reply": cancel_reply, "language": pending["language"]}
        # Anything else: treat as the shopper moving on to a new request.
        PENDING.pop(req.session_id, None)

    try:
        classification = classify_intent(message, SCHEMA)
    except Exception as e:  # noqa: BLE001
        print(f"chatbot_widget: classify_intent error: {e}")
        return {"reply": "Sorry, something went wrong understanding that. Could you rephrase?"}

    intent = classification["intent"]
    action = classification["action"]
    entities = classification["entities"]
    language = classification["language"]

    if classification["requires_confirmation"]:
        PENDING[req.session_id] = {
            "intent": intent, "action": action, "entities": entities,
            "language": language, "expires": time.time() + PENDING_TTL_SECONDS,
        }
        confirm_reply = generate_reply(
            action,
            {"pending_action": action, "details": entities,
             "instruction": "Ask the shopper to reply yes to confirm or no to cancel before this action is taken."},
            language, message,
        )
        return {"status": "confirmation_required", "reply": confirm_reply, "language": language}

    try:
        return await _execute_and_reply(store, intent, action, entities, language, message)
    except Exception as e:  # noqa: BLE001
        print(f"chatbot_widget: dispatch error: {e}")
        return {"reply": "Sorry, something went wrong completing that. Please try again."}


@router.post("/confirm")
async def confirm(req: ConfirmRequest):
    store = _get_store(req.shop)
    if not store:
        return {"reply": "Sorry, I couldn't verify this store."}

    pending = PENDING.pop(req.session_id, None)
    if not pending:
        return {"status": "expired", "reply": "That request has expired — please ask again."}

    if not req.confirmed:
        cancel_reply = generate_reply("cancelled", {"message": "The shopper declined."}, pending["language"], "cancel")
        return {"status": "cancelled", "reply": cancel_reply, "language": pending["language"]}

    return await _execute_and_reply(store, pending["intent"], pending["action"], pending["entities"], pending["language"], "confirmed")


@router.get("/widget-config")
async def widget_config(shop: str):
    store = repo.get_store(shop)
    if not store:
        return {"error": "unknown store"}
    cfg = repo.ensure_customization(store["$id"])
    return {
        "agent_name": cfg.get("agent_name", "AI Assistant"),
        "agent_title": cfg.get("agent_title", "How can I help you today?"),
        "icon_type": cfg.get("icon_type", "preset"),
        "theme_color": cfg.get("theme_color", "#2b2b2b"),
        "custom_icon_url": cfg.get("custom_icon_url", ""),
    }


@router.get("/widget.js")
async def widget_js():
    return Response(content=WIDGET_JS, media_type="application/javascript")


# --------------------------------------------------------------------------
# The embeddable widget. Reads `data-shop` off its own <script> tag, then
# fetches /widget-config?shop=... to personalize name/title/icon before
# rendering — so one script works for every store. Cart mutations run as
# real fetch() calls against the STORE's own domain (same-origin, since
# this script is embedded on the store's page), not against our backend.
# --------------------------------------------------------------------------
WIDGET_JS = r"""
(function () {
  "use strict";

  var THIS_SCRIPT = document.currentScript;
  var SHOP = (THIS_SCRIPT && THIS_SCRIPT.dataset.shop) || "";
  var ORIGIN = THIS_SCRIPT ? new URL(THIS_SCRIPT.src).origin : "";
  var CFG = {
    chatEndpoint: ORIGIN + "/chat",
    configEndpoint: ORIGIN + "/widget-config?shop=" + encodeURIComponent(SHOP)
  };

  if (!SHOP) { console.warn("[chat widget] missing data-shop attribute on script tag"); return; }
  if (document.getElementById("ai-chat-widget-root")) return;

  var style = document.createElement("style");
  style.textContent = [
    "#ai-chat-widget-root, #ai-chat-widget-root * { box-sizing:border-box; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; }",
    "#ai-chat-widget-root .chat-fab { position:fixed; bottom:18px; right:18px; width:46px; height:46px; border-radius:50%; border:none; box-shadow:0 6px 16px rgba(0,0,0,.25); cursor:pointer; display:flex; align-items:center; justify-content:center; z-index:2147483000; transition:transform .2s cubic-bezier(.34,1.56,.64,1); background-size:cover; background-position:center; }",
    "#ai-chat-widget-root .chat-fab:hover { transform:scale(1.07); }",
    "#ai-chat-widget-root .chat-fab svg { width:18px; height:18px; stroke:#fff; }",
    "#ai-chat-widget-root .chat-fab .icon-close { display:none; }",
    "#ai-chat-widget-root .chat-fab.open .icon-mic { display:none; }",
    "#ai-chat-widget-root .chat-fab.open .icon-close { display:block; }",
    "#ai-chat-widget-root .chat-fab.custom-icon .icon-mic { display:none; }",
    "#ai-chat-widget-root .widget { position:fixed; bottom:max(72px, env(safe-area-inset-bottom) + 60px); right:18px; width:min(300px, calc(100vw - 24px)); max-height:calc(100vh - 100px); background:rgba(255,255,255,.72); backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px); border:1.5px solid rgba(0,0,0,.12); border-radius:16px; padding:14px; box-shadow:0 12px 28px rgba(0,0,0,.14); transform-origin:bottom right; transform:scale(.9) translateY(10px); opacity:0; pointer-events:none; transition:transform .2s cubic-bezier(.2,.9,.3,1.2), opacity .15s ease; z-index:2147483000; display:flex; flex-direction:column; }",
    "#ai-chat-widget-root .widget.open { transform:scale(1) translateY(0); opacity:1; pointer-events:auto; }",
    "#ai-chat-widget-root .header { display:flex; align-items:center; gap:8px; margin-bottom:10px; }",
    "#ai-chat-widget-root .avatar { width:30px; height:30px; border-radius:50%; color:#fff; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:10.5px; background-size:cover; background-position:center; }",
    "#ai-chat-widget-root .header h1 { margin:0; font-size:13.5px; font-weight:700; color:#1a1a1a; }",
    "#ai-chat-widget-root .header-right { margin-left:auto; display:flex; align-items:center; gap:6px; }",
    "#ai-chat-widget-root .icon-btn { position:relative; width:26px; height:26px; border-radius:50%; background:#f5f5f5; border:none; display:flex; align-items:center; justify-content:center; cursor:pointer; }",
    "#ai-chat-widget-root .icon-btn svg { width:13px; height:13px; stroke:#444; }",
    "#ai-chat-widget-root .icon-btn.active { background:#2b2b2b; }",
    "#ai-chat-widget-root .icon-btn.active svg { stroke:#fff; }",
    "#ai-chat-widget-root .cart-badge { position:absolute; top:-4px; right:-4px; background:#d64545; color:#fff; font-size:9px; font-weight:700; min-width:15px; height:15px; border-radius:999px; display:none; align-items:center; justify-content:center; padding:0 3px; }",
    "#ai-chat-widget-root .cart-badge.show { display:flex; }",
    "#ai-chat-widget-root .conversation { flex:1; overflow-y:auto; display:flex; flex-direction:column; gap:8px; margin-bottom:10px; max-height:140px; transition:max-height .32s cubic-bezier(.2,.8,.3,1); padding-right:2px; }",
    "#ai-chat-widget-root .bubble { border-radius:12px; padding:9px 11px; font-size:11px; line-height:1.45; max-width:92%; }",
    "#ai-chat-widget-root .bubble.bot { background:rgba(255,255,255,.75); border:1px solid rgba(236,236,236,.8); color:#2a2a2a; box-shadow:0 2px 8px rgba(0,0,0,.05); align-self:flex-start; }",
    "#ai-chat-widget-root .bubble.user { background:#2b2b2b; color:#fff; align-self:flex-end; }",
    "#ai-chat-widget-root .bubble.typing { color:#999; font-style:italic; }",
    "#ai-chat-widget-root #greetingBubble { font-size:14px; font-weight:600; line-height:1.5; }",
    "#ai-chat-widget-root .quick-actions { display:flex; flex-direction:column; gap:6px; align-self:flex-start; max-width:92%; }",
    "#ai-chat-widget-root .quick-action-btn { border:1px solid #e2e2e2; background:#fafafa; color:#2b2b2b; font-size:10.5px; font-weight:600; padding:5px 8px; border-radius:999px; text-align:left; cursor:pointer; opacity:0; transform:translateY(6px); transition:opacity .28s ease, transform .28s ease, background .15s ease; }",
    "#ai-chat-widget-root .quick-action-btn.show { opacity:1; transform:translateY(0); }",
    "#ai-chat-widget-root .quick-action-btn:hover { background:#f0f0f0; }",
    "#ai-chat-widget-root .product-row { display:flex; gap:8px; overflow-x:auto; padding:2px 2px 4px; align-self:flex-start; max-width:100%; }",
    "#ai-chat-widget-root .product-card { flex:0 0 auto; width:110px; border:1px solid #ececec; border-radius:10px; padding:6px; background:#fff; box-shadow:0 2px 8px rgba(0,0,0,.05); display:flex; flex-direction:column; gap:4px; }",
    "#ai-chat-widget-root .product-card img { width:100%; height:70px; object-fit:cover; border-radius:6px; background:#f2f2f2; }",
    "#ai-chat-widget-root .product-card .p-name { font-size:10px; font-weight:600; color:#222; max-height:26px; overflow:hidden; }",
    "#ai-chat-widget-root .product-card .p-price { font-size:10.5px; font-weight:700; color:#2b2b2b; }",
    "#ai-chat-widget-root .product-card button { margin-top:2px; border:none; background:#2b2b2b; color:#fff; font-size:9.5px; padding:3px 0; border-radius:999px; cursor:pointer; }",
    "#ai-chat-widget-root .product-card button:disabled { background:#9c9c9c; }",
    "#ai-chat-widget-root .input-row { position:relative; }",
    "#ai-chat-widget-root .input-row input { width:100%; padding:15px 52px 15px 16px; border-radius:999px; border:1px solid #e5e5e5; background:#fff; font-size:13px; color:#333; outline:none; box-shadow:0 2px 8px rgba(0,0,0,.05); }",
    "#ai-chat-widget-root .mic-btn { position:absolute; right:6px; top:50%; transform:translateY(-50%); width:38px; height:38px; border-radius:50%; background:#2b2b2b; border:none; display:flex; align-items:center; justify-content:center; cursor:pointer; transition:background .15s ease, box-shadow .08s ease, transform .08s ease; }",
    "#ai-chat-widget-root .mic-btn.listening { background:#d64545; }",
    "#ai-chat-widget-root .mic-btn.speaking { box-shadow:0 0 0 calc(4px + var(--level,0)*12px) rgba(214,69,69,calc(.15 + var(--level,0)*.35)), 0 0 calc(6px + var(--level,0)*18px) calc(2px + var(--level,0)*6px) rgba(214,69,69,calc(.4 + var(--level,0)*.5)); transform:translateY(-50%) scale(calc(1 + var(--level,0)*.12)); }",
    "#ai-chat-widget-root .mic-btn svg { width:17px; height:17px; stroke:#fff; }",
    "#ai-chat-widget-root .mic-btn .icon-send-inner { display:none; }",
    "#ai-chat-widget-root .mic-btn.has-text .icon-mic-inner { display:none; }",
    "#ai-chat-widget-root .mic-btn.has-text .icon-send-inner { display:block; }",
    "#ai-chat-widget-root .mic-status { font-size:9.5px; color:#b04040; margin-top:4px; min-height:12px; }"
  ].join("\n");
  document.head.appendChild(style);

  var root = document.createElement("div");
  root.id = "ai-chat-widget-root";
  root.innerHTML =
    '<button class="chat-fab" id="chatFab" aria-label="Open chat">' +
      '<svg class="icon-mic" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>' +
      '<svg class="icon-close" viewBox="0 0 24 24" fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
    "</button>" +
    '<div class="widget" id="chatWidget">' +
      '<div class="header">' +
        '<div class="avatar" id="headerAvatar">AI</div>' +
        '<h1 id="headerName">AI Assistant</h1>' +
        '<div class="header-right">' +
          '<button class="icon-btn" id="ttsToggle" title="Toggle spoken replies">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/></svg>' +
          "</button>" +
          '<div class="icon-btn" id="headerCart">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/></svg>' +
            '<span class="cart-badge" id="cartBadge">0</span>' +
          "</div>" +
        "</div>" +
      "</div>" +
      '<div class="conversation" id="conversation">' +
        '<div class="bubble bot" id="greetingBubble">Hi! How can I help you today?</div>' +
      "</div>" +
      '<div class="input-row">' +
        '<input type="text" id="chatInput" placeholder="Search, add to cart, ask a question....." />' +
        '<button class="mic-btn" id="micBtn" aria-label="Voice input">' +
          '<svg class="icon-mic-inner" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>' +
          '<svg class="icon-send-inner" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>' +
        "</button>" +
      "</div>" +
      '<div class="mic-status" id="micStatus"></div>' +
    "</div>";
  document.body.appendChild(root);

  var fab = document.getElementById("chatFab");
  var widget = document.getElementById("chatWidget");
  var input = document.getElementById("chatInput");
  var micBtn = document.getElementById("micBtn");
  var micStatus = document.getElementById("micStatus");
  var conversation = document.getElementById("conversation");
  var cartBadge = document.getElementById("cartBadge");
  var ttsToggle = document.getElementById("ttsToggle");
  var headerAvatar = document.getElementById("headerAvatar");
  var headerName = document.getElementById("headerName");
  var greetingBubble = document.getElementById("greetingBubble");

  fetch(CFG.configEndpoint)
    .then(function (r) { return r.json(); })
    .then(function (cfg) {
      if (!cfg || cfg.error) return;
      headerName.textContent = cfg.agent_name || "AI Assistant";
      greetingBubble.textContent = cfg.agent_title || "Hi! How can I help you today?";

      if (cfg.icon_type === "custom" && cfg.custom_icon_url) {
        var url = cfg.custom_icon_url.indexOf("http") === 0 ? cfg.custom_icon_url : ORIGIN + cfg.custom_icon_url;
        fab.style.backgroundImage = "url(" + url + ")";
        fab.classList.add("custom-icon");
        headerAvatar.style.backgroundImage = "url(" + url + ")";
        headerAvatar.textContent = "";
      } else {
        var color = cfg.theme_color || "#2b2b2b";
        fab.style.background = color;
        headerAvatar.style.background = color;
      }
    })
    .catch(function () { /* fall back to defaults already in the markup */ });

  var SESSION_ID = (function () {
    try {
      var id = sessionStorage.getItem("chatSessionId");
      if (!id) {
        id = "sess_" + Math.random().toString(36).slice(2) + Date.now();
        sessionStorage.setItem("chatSessionId", id);
      }
      return id;
    } catch (e) {
      return "sess_" + Math.random().toString(36).slice(2) + Date.now();
    }
  })();

  var ttsEnabled = false;
  function speak(text) {
    if (!ttsEnabled || !window.speechSynthesis) return;
    try {
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
    } catch (e) { /* unsupported — ignore */ }
  }
  ttsToggle.addEventListener("click", function () {
    ttsEnabled = !ttsEnabled;
    ttsToggle.classList.toggle("active", ttsEnabled);
    if (!ttsEnabled && window.speechSynthesis) window.speechSynthesis.cancel();
  });

  var quickActionsRendered = false;
  fab.addEventListener("click", function () {
    var isOpen = widget.classList.toggle("open");
    fab.classList.toggle("open", isOpen);
    if (isOpen) renderQuickActions();
  });

  var VISIBLE_MESSAGES = 4;
  function autoResizeConversation() {
    var items = Array.prototype.slice.call(conversation.children, -VISIBLE_MESSAGES);
    var needed = 0;
    items.forEach(function (el, idx) { needed += el.offsetHeight; if (idx > 0) needed += 8; });
    var cap = Math.min(440, window.innerHeight - 220);
    var target = Math.max(90, Math.min(needed || 90, cap));
    conversation.style.maxHeight = target + "px";
    requestAnimationFrame(function () { conversation.scrollTop = conversation.scrollHeight; });
  }
  window.addEventListener("resize", autoResizeConversation);

  function addBubble(text, who) {
    var el = document.createElement("div");
    el.className = "bubble " + who;
    el.textContent = text;
    conversation.appendChild(el);
    autoResizeConversation();
    if (who === "bot") speak(text);
    return el;
  }

  function addProductRow(products) {
    var row = document.createElement("div");
    row.className = "product-row";
    products.forEach(function (p) {
      var card = document.createElement("div");
      card.className = "product-card";
      var img = document.createElement("img");
      img.src = p.image || ""; img.alt = p.name || "";
      var name = document.createElement("div");
      name.className = "p-name"; name.textContent = p.name || "Unnamed product";
      var price = document.createElement("div");
      price.className = "p-price";
      price.textContent = p.price !== undefined ? "$" + p.price : "";
      var btn = document.createElement("button");
      btn.textContent = "Add to cart";
      btn.addEventListener("click", function () { cartAdd(p.id, 1, btn); });
      card.appendChild(img); card.appendChild(name); card.appendChild(price); card.appendChild(btn);
      row.appendChild(card);
    });
    conversation.appendChild(row);
    autoResizeConversation();
  }

  function updateCartBadge(count) {
    if (count === undefined || count === null) return;
    cartBadge.textContent = count;
    cartBadge.classList.toggle("show", count > 0);
  }

  // --------------------------------------------------------------------
  // Real cart mutations, run in the SHOPPER's browser against the
  // store's own /cart/*.js AJAX API — same-origin, since this script is
  // embedded on the store's own page. The backend never touches carts
  // directly (see shopify_actions.py); it only tells us *what* to do.
  // --------------------------------------------------------------------
  function refreshCartBadge() {
    fetch("/cart.js").then(function (r) { return r.json(); }).then(function (cart) {
      updateCartBadge(cart.item_count);
    }).catch(function () {});
  }

  function cartAdd(variantId, quantity, btnEl) {
    if (btnEl) { btnEl.disabled = true; btnEl.textContent = "Adding\u2026"; }
    fetch("/cart/add.js", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: [{ id: variantId, quantity: quantity || 1 }] })
    })
      .then(function (res) { if (!res.ok) throw new Error("add failed"); return res.json(); })
      .then(function () {
        if (btnEl) btnEl.textContent = "Added \u2713";
        refreshCartBadge();
      })
      .catch(function () {
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = "Add to cart"; }
        addBubble("Sorry, could not add that to your cart. Please try again.", "bot");
      });
  }

  function findCartLine(cart, productName) {
    if (!productName) return null;
    var needle = productName.toLowerCase();
    for (var i = 0; i < cart.items.length; i++) {
      if (cart.items[i].product_title.toLowerCase().indexOf(needle) !== -1) {
        return { line: i + 1, item: cart.items[i] };
      }
    }
    return null;
  }

  function cartChangeByName(productName, quantity) {
    fetch("/cart.js").then(function (r) { return r.json(); }).then(function (cart) {
      var match = findCartLine(cart, productName);
      if (!match) { addBubble("I couldn't find \"" + productName + "\" in your cart.", "bot"); return; }
      return fetch("/cart/change.js", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ line: match.line, quantity: quantity })
      }).then(function () { refreshCartBadge(); });
    }).catch(function () {});
  }

  function cartView() {
    fetch("/cart.js").then(function (r) { return r.json(); }).then(function (cart) {
      if (!cart.items.length) { addBubble("Your cart is empty.", "bot"); return; }
      var lines = cart.items.map(function (it) {
        return it.quantity + "x " + it.product_title + " (" + (it.final_line_price / 100).toFixed(2) + ")";
      });
      addBubble("Your cart:\n" + lines.join("\n") + "\nTotal: " + (cart.total_price / 100).toFixed(2), "bot");
      updateCartBadge(cart.item_count);
    }).catch(function () {});
  }

  function cartClear() {
    fetch("/cart/clear.js", { method: "POST" }).then(function () { updateCartBadge(0); }).catch(function () {});
  }

  function runWidgetAction(action) {
    if (!action || !action.type) return;
    switch (action.type) {
      case "redirect":
        if (action.url) setTimeout(function () { window.location.href = action.url; }, 600);
        break;
      case "cart_add":
        cartAdd(action.variant_id, action.quantity || 1, null);
        break;
      case "cart_remove":
        cartChangeByName(action.product_name, 0);
        break;
      case "cart_set_quantity":
        cartChangeByName(action.product_name, action.quantity || 1);
        break;
      case "cart_view":
        cartView();
        break;
      case "cart_clear":
        cartClear();
        break;
    }
  }

  var QUICK_ACTIONS = [
    { icon: "\uD83D\uDD0D", label: "Search products", command: "Show me products" },
    { icon: "\uD83D\uDED2", label: "Add an item to cart", command: "Add a t-shirt to my cart" },
    { icon: "\uD83D\uDCB2", label: "Filter by price", command: "Show me products under $20" },
    { icon: "\uD83D\uDEE1\uFE0F", label: "Claim a warranty", command: "I want to claim a warranty for order #1001" },
    { icon: "\uD83D\uDCE6", label: "Track my order", command: "Track my order #1001" }
  ];
  function renderQuickActions() {
    if (quickActionsRendered) return;
    quickActionsRendered = true;
    var row = document.createElement("div");
    row.className = "quick-actions";
    conversation.appendChild(row);
    QUICK_ACTIONS.forEach(function (action, i) {
      var btn = document.createElement("button");
      btn.className = "quick-action-btn";
      btn.textContent = action.icon + " " + action.label;
      btn.addEventListener("click", function () { sendMessage(action.command); });
      row.appendChild(btn);
      setTimeout(function () { btn.classList.add("show"); autoResizeConversation(); }, i * 500);
    });
  }

  function sendMessage(text) {
    text = (text || "").trim();
    if (!text) return;
    addBubble(text, "user");
    input.value = "";
    micBtn.classList.remove("has-text");
    var typingEl = addBubble("typing\u2026", "bot typing");

    fetch(CFG.chatEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: SESSION_ID, shop: SHOP })
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        typingEl.remove();
        addBubble(data.reply || "(no reply)", "bot");
        if (Array.isArray(data.products) && data.products.length > 0) addProductRow(data.products);
        if (data.widget_action) runWidgetAction(data.widget_action);
        refreshCartBadge();
      })
      .catch(function () {
        typingEl.remove();
        addBubble("Sorry, I could not reach the server. Please try again.", "bot");
      });
  }
  input.addEventListener("keydown", function (e) { if (e.key === "Enter") sendMessage(input.value); });
  input.addEventListener("input", function () {
    micBtn.classList.toggle("has-text", input.value.trim().length > 0);
  });
  refreshCartBadge();

  var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  var recognition = null, listening = false;
  var audioCtx = null, analyser = null, micStream = null, rafId = null;

  function startGlow() {
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      micStream = stream;
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      var source = audioCtx.createMediaStreamSource(stream);
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.6;
      source.connect(analyser);
      var data = new Uint8Array(analyser.frequencyBinCount);
      (function tick() {
        analyser.getByteTimeDomainData(data);
        var sumSquares = 0;
        for (var i = 0; i < data.length; i++) { var c = (data[i] - 128) / 128; sumSquares += c * c; }
        var level = Math.min(1, Math.sqrt(sumSquares / data.length) * 6);
        micBtn.style.setProperty("--level", level.toFixed(3));
        micBtn.classList.toggle("speaking", level > 0.04);
        rafId = requestAnimationFrame(tick);
      })();
    }).catch(function () { micStatus.textContent = "Mic permission needed for glow effect."; });
  }
  function stopGlow() {
    if (rafId) cancelAnimationFrame(rafId);
    rafId = null;
    micBtn.classList.remove("speaking");
    micBtn.style.setProperty("--level", 0);
    if (micStream) { micStream.getTracks().forEach(function (t) { t.stop(); }); micStream = null; }
    if (audioCtx) { audioCtx.close(); audioCtx = null; }
    analyser = null;
  }

  if (SpeechRecognition) {
    recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = "en-US";
    recognition.onstart = function () {
      listening = true;
      micBtn.classList.add("listening");
      micStatus.textContent = "Listening\u2026";
      startGlow();
    };
    recognition.onresult = function (event) {
      var interim = "", final = "";
      for (var i = 0; i < event.results.length; i++) {
        var t = event.results[i][0].transcript;
        if (event.results[i].isFinal) final += t; else interim += t;
      }
      input.value = final || interim;
    };
    recognition.onerror = function (event) { micStatus.textContent = "Mic error: " + event.error; };
    recognition.onend = function () {
      listening = false;
      micBtn.classList.remove("listening");
      micStatus.textContent = "";
      stopGlow();
      if (input.value.trim()) sendMessage(input.value);
    };
    micBtn.addEventListener("click", function () {
      if (micBtn.classList.contains("has-text")) { sendMessage(input.value); return; }
      if (listening) { recognition.stop(); } else { input.value = ""; recognition.start(); }
    });
  } else {
    micBtn.addEventListener("click", function () {
      if (micBtn.classList.contains("has-text")) { sendMessage(input.value); return; }
      micStatus.textContent = "Voice input is not supported in this browser.";
    });
  }
})();
"""
