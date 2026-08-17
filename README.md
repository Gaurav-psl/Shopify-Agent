# Shopify AI Chatbot Plugin — Full Backend

A multi-tenant Shopify app: any store owner installs it, gets a dashboard
to sign up/log in and customize their chatbot's name, greeting, and icon,
and gets an embeddable chat widget with voice input, product search,
price filtering, cart management, order tracking, and warranty claims —
in whatever language the shopper types or speaks.

## What changed from what you uploaded

You had two overlapping starts: a Node/Express OAuth skeleton
(`server.js`/`package.json`) and a FastAPI backend (`main.py` +
`intent_classifier.py` + `reply_generator.py` + `shopify_actions.py`)
with an in-memory token store and no dashboard, plus a separate scaffold
with a database, OAuth, dashboard, and widget but a simpler keyword-based
router instead of your LLM classifier. **This repo merges them into one
app**: the database-backed OAuth + dashboard + widget scaffold now drives
your existing LLM intent classifier and reply generator, and every
action hits the real Shopify Admin API instead of mock data.

- **Delete `server.js` and `package.json`.** The Python/FastAPI backend
  now owns the entire OAuth flow; running both would just cause port and
  routing conflicts. If you need a Node service for something else later,
  keep them elsewhere.
- **`VoiceAssistantWidget.jsx`** is a React component — useful only if
  you're building a *headless* storefront (Hydrogen/Next.js) where React
  runs on the shop's pages. For a normal Shopify theme (Liquid, not
  React), the embeddable widget is the plain-JS `widget.js` served by
  `chatbot_widget.py`, which already includes voice input, TTS, and
  quick actions. It's moved to `optional_react_widget/` — wire it up
  yourself only if you're on a headless stack; otherwise ignore it.

## File overview

| File | Role |
|---|---|
| `main.py` | App entrypoint — wires every router together |
| `database.py` | SQLAlchemy engine/session (SQLite by default, swap in Postgres for production) |
| `models.py` | `Store` (installed shops + access tokens), `DashboardUser` (owner login), `AgentCustomization` (name/title/icon per store) |
| `shopify_auth.py` | `/`, `/auth`, `/auth/callback` — the install (OAuth) flow |
| `dashboard.py` | `/dashboard/signup`, `/dashboard/login`, `/dashboard` — owner login + chatbot customization UI |
| `intent_schema.json` | Single source of truth for every intent/action the bot supports |
| `intent_classifier.py` | Calls OpenAI to classify each shopper message + detect language |
| `reply_generator.py` | Calls OpenAI to phrase the final reply in the shopper's language |
| `shopify_actions.py` | Executes each action against the real Shopify Admin API |
| `chatbot_widget.py` | `/widget.js`, `/widget-config`, `/chat`, `/confirm` — serves the widget and runs the classify → confirm → execute → reply pipeline |
| `webhooks.py` | `app/uninstalled` + the mandatory GDPR webhooks Shopify requires for App Store review |

## Chatbot capabilities (from `intent_schema.json`)

- **Order tracking** — status, fulfillment, tracking number, recent orders
- **Product search & filtering** — by keyword, price range, color, size
- **Cart management** — add/remove/edit quantity/view/clear
- **Warranty claims** — files a tagged note on the order for the merchant to action, plus status checks
- **Store policy Q&A** — pulled live from the store's actual published policies
- **Registration/login** — routes shoppers to the store's native account pages

Anything ambiguous or low-confidence routes to a clarifying question
instead of guessing (`confidence_threshold` in `intent_schema.json`).

### Why cart actions work differently

Shopify's cart is tied to the *shopper's own browser cookie* — your
server can't add to it directly. So `shopify_actions.py` resolves what
the shopper means (e.g. searches Admin API for "red hoodie" → gets a
variant ID) and returns an instruction; `widget.js`, which is embedded on
the store's own domain, then makes the real `fetch('/cart/add.js', ...)`
call itself, same-origin with the store's cart. This is the correct
approach — there's no safe server-side alternative for anonymous carts.

### Confirmation flow

Any action marked `"requires_confirmation": true` in `intent_schema.json`
(account creation, cart changes, warranty claims) doesn't run
immediately. The bot asks "are you sure?" in the shopper's language and
holds the pending action in memory for 5 minutes, keyed by the widget's
session ID. The shopper's next message is checked for a yes/no before
anything else — reply "yes"/"no" (or a few other languages' equivalents)
right in the chat box.

## Setup

### 1. Create the Shopify Partner app

1. [partners.shopify.com](https://partners.shopify.com) → **Apps** → **Create app** → "Create app manually".
2. Note the **Client ID** and **Client secret**.
3. Under **App setup**:
   - App URL: `https://your-app-domain.com/`
   - Allowed redirection URL(s): `https://your-app-domain.com/auth/callback`
4. Under **Webhooks**, subscribe (or let the app register these on install if you extend `shopify_auth.py` to call `POST /admin/api/{version}/webhooks.json`):
   - `app/uninstalled` → `https://your-app-domain.com/webhooks/app/uninstalled`
   - `customers/data_request`, `customers/redact`, `shop/redact` → matching `/webhooks/...` paths (required for public App Store listing)

### 2. Environment variables

Copy `.env.example` → `.env` and fill in:

```
SHOPIFY_API_KEY=          # Client ID from the Partner Dashboard
SHOPIFY_API_SECRET=       # Client secret
SHOPIFY_SCOPES=read_products,read_orders,write_orders,read_customers
APP_URL=https://your-app-domain.com
SESSION_SECRET=<any long random string>
OPENAI_API_KEY=sk-...
```

`SHOPIFY_SCOPES` is what the merchant is asked to approve on install —
this is the actual "ask for permissions" step:
- `read_products` → product search/filtering
- `read_orders`, `write_orders` → order tracking + warranty claim tagging
- `read_customers` → optional, only if you extend registration/login beyond redirecting to native pages

### 3. Install dependencies & run locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

For Shopify to reach `/auth/callback` during local development, tunnel
it with ngrok (`ngrok http 8000`) and set `APP_URL` to the ngrok URL.

### 4. Deploy

Any host that runs a long-lived Python process works (Render, Railway,
Fly.io, a VPS). Minimum:

- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Set `DATABASE_URL` to a real Postgres instance — SQLite is fine for
  testing but most free-tier hosts wipe local disk on restart, which
  would silently uninstall every store from your database.
- Set `APP_URL` to your real deployed HTTPS URL, and update the
  redirect URL in the Partner Dashboard to match exactly.

### 5. Install flow (what a store owner experiences)

1. They open your app's listing (or you send them
   `https://your-app-domain.com/?shop=their-store.myshopify.com`).
2. `shopify_auth.py` redirects to Shopify's permission screen showing
   the scopes above.
3. They approve → Shopify redirects to `/auth/callback` → we verify the
   request, exchange the code for an access token, and store it.
4. First-time installs land on `/dashboard/signup` to set an email/password
   for **your app's own dashboard** (separate from their Shopify login).
   Returning installs land on `/dashboard/login`.
5. On the dashboard they set the chatbot's name, greeting title, and
   button icon (upload a custom image, or pick a preset icon + theme
   color), and copy the install snippet shown at the bottom of the page.

### 6. Embed the widget in their theme

The dashboard shows this automatically, filled in with their shop
domain — they paste it once, near the end of `theme.liquid`, before
`</body>`:

```html
<script src="https://your-app-domain.com/widget.js"
        data-shop="their-store.myshopify.com" defer></script>
```

One script serves every installed store — it fetches that store's name/
title/icon from `/widget-config` at load time, so there's nothing else
to configure per store.

## Testing the pipeline without a live store

```bash
python intent_classifier.py   # sanity-check classification + language detection
python reply_generator.py     # sanity-check reply phrasing in a few languages
```

`shopify_actions.py` needs a real `Store` row (with a live access token)
to hit the Admin API — test it end-to-end by installing on a Shopify
[development store](https://partners.shopify.com) (free, unlimited).

## Known limitations / production TODOs

- **Token storage**: `Store.access_token` is stored in plaintext in the
  database. Encrypt it at rest (e.g. via `cryptography.Fernet` with a key
  in your secrets manager) before handling real merchant data.
- **Warranty claims** are recorded as an order tag + note, visible to the
  merchant in Shopify Admin — swap `submit_claim()` in `shopify_actions.py`
  for a real helpdesk integration (Gorgias, Zendesk, etc.) if you have one.
- **Confirmation cache is in-memory** (`PENDING` dict in
  `chatbot_widget.py`) — fine for a single process, but won't share state
  across multiple server instances. Move it to Redis if you scale
  horizontally.
- **Registration/login** currently just redirects to the store's native
  `/account/*` pages, which is the safest default. If you want the bot to
  actually create accounts conversationally, that needs the Storefront
  API's `customerCreate`/`customerAccessTokenCreate` mutations and a
  Storefront API access token per store, in addition to the Admin token.
- Register the mandatory webhooks (see Setup step 1) or Shopify will
  reject the app for public listing.
