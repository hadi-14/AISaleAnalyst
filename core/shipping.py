"""
shipping.py
===========
Live shipping-rate lookup via the Shippo API (goshippo.com).

Provides :func:`get_shipping_rate`, which accepts package dimensions and
weight, queries Shippo for real USPS / UPS rates, and returns the most
relevant rate according to the ``SHIP_SERVICE`` preference set in ``.env``.

If the Shippo API key is not configured, or if the API call fails for any
reason, the function falls back to the legacy flat-rate estimate so the
rest of the pipeline is never interrupted.

Configuration (all via .env)
-----------------------------
SHIPPO_API_KEY      Live API key from goshippo.com > Settings > API
SHIP_FROM_ZIP       Origin ZIP code (where packages ship FROM)
SHIP_TO_ZIP         Destination ZIP code (used for rate estimation)
SHIP_SERVICE        cheapest | usps_ground | usps_priority | ups_ground
SHIP_MANUAL_DIMS    true / false — skip AI dims and use fixed manual values
SHIP_MANUAL_LENGTH  Manual length override (inches)
SHIP_MANUAL_WIDTH   Manual width override (inches)
SHIP_MANUAL_HEIGHT  Manual height override (inches)
SHIP_MANUAL_WEIGHT  Manual weight override (pounds)
"""

from __future__ import annotations

from .config import (
    SHIPPO_API_KEY,
    SHIP_FROM_ZIP,
    SHIP_TO_ZIP,
    SHIP_SERVICE,
    SHIP_MANUAL_DIMS,
    SHIP_MANUAL_LENGTH,
    SHIP_MANUAL_WIDTH,
    SHIP_MANUAL_HEIGHT,
    SHIP_MANUAL_WEIGHT,
)

# ---------------------------------------------------------------------------
# Service-key → carrier + service-level name fragments for fuzzy matching
# ---------------------------------------------------------------------------

_SERVICE_MAP: dict[str, tuple[str, str]] = {
    "usps_ground":    ("USPS", "Ground Advantage"),
    "usps_priority":  ("USPS", "Priority Mail"),
    "ups_ground":     ("UPS",  "Ground"),
}

# ---------------------------------------------------------------------------
# Flat-rate fallback (identical to the original estimate_shipping_cost logic)
# ---------------------------------------------------------------------------

_LOCAL_PICKUP_GROUPS = {
    "boat", "vehicle", "car", "motorcycle", "trailer", "chipper",
    "lawnmower", "tractor", "shredder", "furniture", "sofa", "table",
    "freezer", "refrigerator", "chest", "snowblower",
}
_HEAVY_SHIPPABLE_GROUPS = {
    "generator", "amplifier", "receiver", "stereo", "speaker", "mower",
    "compressor", "saw", "drill press", "lathe", "welder", "outboard", "trolling motor",
}
_LIGHTWEIGHT_GROUPS = {
    "watch", "jewelry", "ring", "necklace", "camera", "lens", "coin",
    "stamp", "card", "shirt", "jacket", "coat", "hat", "glasses", "sunglasses",
    "brooch", "pendant", "earring", "manual", "book", "cd", "dvd", "game", "toy", "figurine",
}


def _flat_rate_fallback(item_group: str = "", item_name: str = "") -> dict:
    """Return a flat-rate estimate dict when Shippo is unavailable."""
    text = f"{item_group} {item_name}".lower()
    if any(g in text for g in _LOCAL_PICKUP_GROUPS):
        cost = 0.0
        note = "Local pickup / freight"
    elif any(g in text for g in _HEAVY_SHIPPABLE_GROUPS):
        cost = 35.0
        note = "Estimated (heavy item)"
    elif any(g in text for g in _LIGHTWEIGHT_GROUPS):
        cost = 8.0
        note = "Estimated (lightweight)"
    else:
        cost = 15.0
        note = "Estimated (standard)"
    return {
        "cost":    cost,
        "carrier": "Estimated",
        "service": note,
        "est_days": None,
        "source":  "fallback",
    }


# ---------------------------------------------------------------------------
# Local-pickup detection (skips Shippo call entirely for heavy freight)
# ---------------------------------------------------------------------------

def _is_local_pickup(item_group: str, item_name: str) -> bool:
    text = f"{item_group} {item_name}".lower()
    return any(g in text for g in _LOCAL_PICKUP_GROUPS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_shipping_rate(
    length: float,
    width: float,
    height: float,
    weight: float,
    item_group: str = "",
    item_name: str = "",
) -> dict:
    """
    Return a shipping rate dict for the given package.

    If ``SHIP_MANUAL_DIMS=true`` the passed-in dimensions are ignored and
    the manual values from ``.env`` are used instead.

    Parameters
    ----------
    length, width, height:
        Package dimensions in **inches** (AI-estimated or manual).
    weight:
        Package weight in **pounds** (AI-estimated or manual).
    item_group, item_name:
        Used for local-pickup detection and flat-rate fallback labelling.

    Returns
    -------
    dict
        Keys: ``cost`` (float), ``carrier`` (str), ``service`` (str),
        ``est_days`` (int | None), ``source`` (str — ``"shippo"`` or ``"fallback"``).
    """
    # --- Local-pickup items: never need a shipping rate
    if _is_local_pickup(item_group, item_name):
        return {
            "cost":    0.0,
            "carrier": "Local Pickup",
            "service": "Local Pickup / Freight",
            "est_days": None,
            "source":  "local_pickup",
        }

    # --- No Shippo key configured → flat-rate fallback
    if not SHIPPO_API_KEY:
        return _flat_rate_fallback(item_group, item_name)

    # --- Resolve dimensions
    if SHIP_MANUAL_DIMS:
        l, w, h, wt = (
            SHIP_MANUAL_LENGTH,
            SHIP_MANUAL_WIDTH,
            SHIP_MANUAL_HEIGHT,
            SHIP_MANUAL_WEIGHT,
        )
    else:
        # Use AI-supplied dims; guard against zero/None with sensible defaults
        l  = max(float(length  or 0), 1.0)
        w  = max(float(width   or 0), 1.0)
        h  = max(float(height  or 0), 1.0)
        wt = max(float(weight  or 0), 0.1)

    try:
        import shippo
        from shippo.models import components

        sdk = shippo.Shippo(api_key_header=SHIPPO_API_KEY)

        shipment = sdk.shipments.create(
            components.ShipmentCreateRequest(
                address_from=components.AddressCreateRequest(
                    name="Seller",
                    street1="1 Main St",
                    city="Origin",
                    state="IL",
                    zip=SHIP_FROM_ZIP,
                    country="US",
                ),
                address_to=components.AddressCreateRequest(
                    name="Buyer",
                    street1="1 Main St",
                    city="Destination",
                    state="NY",
                    zip=SHIP_TO_ZIP,
                    country="US",
                ),
                parcels=[
                    components.ParcelCreateRequest(
                        length=str(l),
                        width=str(w),
                        height=str(h),
                        distance_unit=components.DistanceUnitEnum.IN,
                        weight=str(wt),
                        mass_unit=components.WeightUnitEnum.LB,
                    )
                ],
                async_=False,
            )
        )

        rates = shipment.rates if shipment and shipment.rates else []
        if not rates:
            return _flat_rate_fallback(item_group, item_name)

        # Sort all rates cheapest first
        sorted_rates = sorted(rates, key=lambda r: float(r.amount))

        selected = None

        if SHIP_SERVICE == "cheapest":
            selected = sorted_rates[0]
        else:
            target_carrier, target_service = _SERVICE_MAP.get(SHIP_SERVICE, ("", ""))
            for r in sorted_rates:
                if (
                    target_carrier.lower() in r.provider.lower()
                    and target_service.lower() in r.servicelevel.name.lower()
                ):
                    selected = r
                    break
            # If preferred service not found, fall back to cheapest
            if selected is None:
                selected = sorted_rates[0]

        return {
            "cost":    round(float(selected.amount), 2),
            "carrier": selected.provider,
            "service": selected.servicelevel.name,
            "est_days": selected.estimated_days,
            "source":  "shippo",
        }

    except Exception as exc:
        # Never crash the pipeline — fall back silently
        print(f"  [Shipping] Shippo error ({type(exc).__name__}): {exc} — using flat-rate fallback")
        return _flat_rate_fallback(item_group, item_name)
