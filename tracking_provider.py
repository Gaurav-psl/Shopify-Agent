"""
tracking_provider.py
---------------------
Fetches a shipment's live location/status from AfterShip's Tracking API,
given the carrier tracking number Shopify already has on file for an
order (see shopify_actions.track_order).

Why this file exists: Shopify's own Admin API only stores a tracking
NUMBER and a link to the carrier's own tracking page — it has no idea
where a package physically is right now. That data lives on the
carrier's systems (FedEx, UPS, USPS, etc.), and each carrier has its
own separate API and format. AfterShip aggregates real-time scan
events from hundreds of carriers behind one consistent API, which is
what lets the chat widget report an actual last-known location and
status instead of just handing the shopper a link to go check
themselves.

Env vars expected:
  AFTERSHIP_API_KEY   Your AfterShip API key (from app.aftership.com ->
                       Settings -> API Keys). Free tier covers a decent
                       number of trackings/month; check current limits
                       on their pricing page if you expect high volume.

If AFTERSHIP_API_KEY isn't set, get_live_status() always returns None
and shopify_actions.py falls back to tracking-link-only, exactly like
before this module existed — this integration is optional, not a hard
dependency of the app.

One real timing quirk to know about: the first time a tracking number
is ever looked up, AfterShip needs to detect the carrier and pull an
initial checkpoint from them, which isn't always instant. If a shopper
asks for tracking within seconds of AfterShip first seeing that number,
there may be no checkpoints yet — get_live_status() handles this by
returning a "just requested, check back shortly" status rather than
nothing, so the reply doesn't awkwardly imply the package is missing.
"""

import os
import httpx

AFTERSHIP_API_KEY = os.environ.get("AFTERSHIP_API_KEY", "")
AFTERSHIP_BASE_URL = "https://api.aftership.com/tracking/2024-04"


def _headers() -> dict:
    return {"as-api-key": AFTERSHIP_API_KEY, "Content-Type": "application/json"}


async def _get_tracking(tracking_number: str, carrier_slug: str | None = None) -> dict | None:
    async with httpx.AsyncClient(timeout=10) as client:
        if carrier_slug:
            resp = await client.get(
                f"{AFTERSHIP_BASE_URL}/trackings/{carrier_slug}/{tracking_number}",
                headers=_headers(),
            )
        else:
            resp = await client.get(
                f"{AFTERSHIP_BASE_URL}/trackings",
                headers=_headers(),
                params={"tracking_numbers": tracking_number},
            )
    if resp.status_code != 200:
        return None

    data = resp.json().get("data", {})
    if "tracking" in data:
        return data["tracking"]
    trackings = data.get("trackings", [])
    return trackings[0] if trackings else None


async def _create_tracking(tracking_number: str, carrier_slug: str | None = None) -> dict | None:
    payload = {"tracking": {"tracking_number": tracking_number}}
    if carrier_slug:
        payload["tracking"]["slug"] = carrier_slug

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{AFTERSHIP_BASE_URL}/trackings", headers=_headers(), json=payload)

    if resp.status_code in (200, 201):
        return resp.json().get("data", {}).get("tracking")

    # 4003/409-style "already exists" responses mean someone else (or an
    # earlier request) already registered this number — just fetch it.
    if resp.status_code == 400:
        return await _get_tracking(tracking_number, carrier_slug)

    return None


def _summarize(tracking: dict) -> dict:
    checkpoints = tracking.get("checkpoints") or []
    if not checkpoints:
        return {
            "status": tracking.get("tag", "Pending"),
            "location": None,
            "message": "Tracking was just requested from the carrier — a live update should be available shortly.",
            "checkpoint_time": None,
            "estimated_delivery": tracking.get("expected_delivery"),
        }

    latest = checkpoints[-1]
    location = ", ".join(
        p for p in [latest.get("city"), latest.get("state"), latest.get("country_name")] if p
    ) or None

    return {
        "status": tracking.get("tag"),
        "location": location,
        "message": latest.get("message"),
        "checkpoint_time": latest.get("checkpoint_time"),
        "estimated_delivery": tracking.get("expected_delivery"),
    }


async def get_live_status(tracking_number: str, carrier_slug: str | None = None) -> dict | None:
    """
    Returns the shipment's current known location/status, or None if
    live tracking isn't available at all (no API key configured, no
    tracking number, or the lookup/creation failed outright).

    {
      "status": "InTransit",
      "location": "Memphis, TN, US",
      "message": "Departed from carrier facility",
      "checkpoint_time": "2026-09-27T14:32:00+00:00",
      "estimated_delivery": "2026-09-30",
    }

    `location`/`checkpoint_time` may be None even on a successful call
    if AfterShip hasn't pulled a first checkpoint yet — callers should
    treat that as "not available yet", not as an error.
    """
    if not AFTERSHIP_API_KEY or not (tracking_number or "").strip():
        return None

    tracking = await _get_tracking(tracking_number, carrier_slug)
    if tracking is None:
        tracking = await _create_tracking(tracking_number, carrier_slug)
    if tracking is None:
        return None

    return _summarize(tracking)
