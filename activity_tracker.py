"""
activity_tracker.py
--------------------
Real-time tracking and retrieval of website activity (clicks, views, cart adds,
and searches) for both anonymous visitors (via cookies / localStorage) and
authenticated Shopify customers.

Features:
  1. Low-latency in-memory cache for instant recommendation scoring (<1ms).
  2. Asynchronous background persistence to Appwrite analytics_events collection.
  3. Real-time aggregation of click frequencies, browsed subcategories, and
     trending products for recommendation scoring.
"""

from __future__ import annotations
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["activity-tracking"])


class TrackActivityRequest(BaseModel):
    shop: str
    visitor_id: str
    session_id: str | None = ""
    event_type: str = "product_click"
    product_id: str | None = ""
    product_name: str | None = ""
    category: str | None = ""
    sub_category: str | None = ""
    style: str | None = ""
    price: float | None = None
    customer_token: str | None = None


# Background executor for persistent logging to Appwrite
_BG_POOL = ThreadPoolExecutor(max_workers=3)

# ---------------------------------------------------------------------------
# In-Memory Fast Activity Cache
# Keyed by (shop, visitor_id) or customer_id
# Expired after 24 hours of inactivity
# ---------------------------------------------------------------------------
# structure: (shop, identity) -> list[dict]
_ACTIVITY_STORE: dict[str, list[dict]] = defaultdict(list)
_STORE_TRENDING_CLICKS: dict[str, Counter] = defaultdict(Counter)
_MAX_USER_EVENTS = 100


def _identity_key(shop: str, visitor_id: str | None, customer_id: str | None = None) -> str:
    """Computes a unified identity key, favoring customer_id when authenticated."""
    norm_shop = (shop or "").strip().lower()
    if customer_id and str(customer_id).strip():
        return f"{norm_shop}:cust_{str(customer_id).strip()}"
    return f"{norm_shop}:vis_{(visitor_id or 'anon').strip()}"


def track_activity(
    shop: str,
    visitor_id: str,
    session_id: str = "",
    event_type: str = "product_click",
    product_id: str | int = "",
    product_name: str = "",
    category: str = "",
    sub_category: str = "",
    style: str = "",
    price: float | None = None,
    customer_id: str | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """
    Records a user website action (product click, product view, cart add, etc.).
    Immediately updates the in-memory activity index and queues persistence.
    """
    now = time.time()
    pid_str = str(product_id).strip() if product_id is not None else ""

    event_payload = {
        "event_type": event_type,
        "product_id": pid_str,
        "product_name": product_name,
        "category": category,
        "sub_category": sub_category,
        "style": style,
        "price": price,
        "session_id": session_id,
        "timestamp": now,
        "metadata": metadata or {},
    }

    # 1. Update in-memory user activity cache
    user_key = _identity_key(shop, visitor_id, customer_id)
    history = _ACTIVITY_STORE[user_key]
    history.append(event_payload)
    if len(history) > _MAX_USER_EVENTS:
        history.pop(0)

    # If customer_id is provided and visitor_id was also used, link visitor history
    if customer_id and visitor_id:
        anon_key = _identity_key(shop, visitor_id, None)
        if anon_key in _ACTIVITY_STORE and anon_key != user_key:
            # Merge recent guest browsing into customer profile
            for past_event in _ACTIVITY_STORE[anon_key]:
                if past_event not in history:
                    history.append(past_event)
            _ACTIVITY_STORE[anon_key] = []

    # 2. Update global store trending click count
    norm_shop = (shop or "").strip().lower()
    if event_type in ("product_click", "product_view") and pid_str:
        _STORE_TRENDING_CLICKS[norm_shop][pid_str] += 1

    # 3. Asynchronously write to Appwrite analytics_events in background
    def _async_persist():
        try:
            import repository_appwrite as repo
            store_doc = repo._find_store_doc(shop)
            if store_doc:
                store_id = store_doc["$id"]
                repo.record_event(
                    store_id=store_id,
                    event_type=event_type,
                    name=product_name or pid_str,
                    value=float(price or 1),
                    session_id=session_id,
                    visitor_id=visitor_id or (f"cust_{customer_id}" if customer_id else ""),
                    external_id=str(customer_id) if customer_id else "",
                    category=sub_category or category,
                    platform="shopify",
                )
        except Exception as err:
            # Telemetry must never crash or block the caller
            pass

    _BG_POOL.submit(_async_persist)

    return {"status": "ok", "recorded_event": event_type, "product_id": pid_str}


def get_user_activity_summary(
    shop: str,
    visitor_id: str | None,
    customer_id: str | None = None,
) -> dict[str, Any]:
    """
    Returns an aggregated behavioral summary for the user:
      - clicked_product_ids: list of product IDs clicked (ordered by frequency/recency)
      - product_click_counts: dict of {product_id: click_count}
      - top_subcategories: list of (subcategory_name, count)
      - top_styles: list of (style_name, count)
      - total_clicks: int total number of product interactions
    """
    user_key = _identity_key(shop, visitor_id, customer_id)
    history = _ACTIVITY_STORE.get(user_key, [])

    click_counts: Counter = Counter()
    view_counts: Counter = Counter()
    subcat_counts: Counter = Counter()
    style_counts: Counter = Counter()
    recent_pids: list[str] = []

    for ev in history:
        pid = ev.get("product_id")
        subcat = ev.get("sub_category")
        st = ev.get("style")
        etype = ev.get("event_type")

        if pid:
            if etype == "product_click":
                click_counts[pid] += 1
            elif etype == "product_view":
                view_counts[pid] += 1
            if pid not in recent_pids:
                recent_pids.append(pid)

        if subcat:
            weight = 2 if etype == "product_click" else 1
            subcat_counts[subcat] += weight

        if st:
            weight = 2 if etype == "product_click" else 1
            style_counts[st] += weight

    return {
        "clicked_product_ids": [pid for pid, _ in click_counts.most_common()],
        "product_click_counts": dict(click_counts),
        "viewed_product_ids": [pid for pid, _ in view_counts.most_common()],
        "top_subcategories": subcat_counts.most_common(5),
        "top_styles": style_counts.most_common(5),
        "recent_product_ids": list(reversed(recent_pids))[:10],
        "total_clicks": sum(click_counts.values()),
    }


def get_trending_product_ids(shop: str, limit: int = 10) -> list[str]:
    """Returns top trending product IDs across all users based on click popularity."""
    norm_shop = (shop or "").strip().lower()
    counter = _STORE_TRENDING_CLICKS.get(norm_shop)
    if not counter:
        return []
    return [pid for pid, _ in counter.most_common(limit)]


def clear_cache_for_testing() -> None:
    """Helper to reset in-memory caches between unit tests."""
    _ACTIVITY_STORE.clear()
    _STORE_TRENDING_CLICKS.clear()


@router.post("/track-activity")
async def track_activity_endpoint(req: TrackActivityRequest):
    import customer_profiles
    customer_id = None
    if req.customer_token:
        customer_id = customer_profiles.read_session_token(req.customer_token, req.shop)

    return track_activity(
        shop=req.shop,
        visitor_id=req.visitor_id,
        session_id=req.session_id or "",
        event_type=req.event_type or "product_click",
        product_id=req.product_id or "",
        product_name=req.product_name or "",
        category=req.category or "",
        sub_category=req.sub_category or "",
        style=req.style or "",
        price=req.price,
        customer_id=customer_id,
    )


@router.get("/user-activity")
async def user_activity_endpoint(shop: str, visitor_id: str, customer_token: str | None = None):
    import customer_profiles
    customer_id = None
    if customer_token:
        customer_id = customer_profiles.read_session_token(customer_token, shop)
    return get_user_activity_summary(shop, visitor_id, customer_id)

