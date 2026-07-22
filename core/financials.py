"""
financials.py
=============
Financial calculations and sorting utilities for AISaleAnalyst.

Functions
---------
calc_financials(item)
    Compute sell price, buy price, profit and ROI for a single item.
tier(roi)
    Map an ROI value to a display tier label.
get_sort_key(item)
    Extract the numeric value used when sorting the final report.
"""

from .config import MIN_PROFIT_MARGIN_PCT, SORT_BY
from .shipping import get_shipping_rate

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def estimate_ebay_fee(sell_price: float, category_id: int | None) -> float:
    """Estimate eBay fees based on category and selling price."""
    if category_id:
        try:
            cat_id = int(category_id)
            if cat_id in (26429, 6000, 26443, 152737):  # Boats / Motors / Vehicles / Parts
                return min(100.0, sell_price * 0.13)
        except (ValueError, TypeError):
            pass
    return (sell_price * 0.1325) + 0.30


# estimate_shipping_cost() has been replaced by the live Shippo integration.
# See core/shipping.py for the full implementation.
# This stub is retained only for any legacy references.
def estimate_shipping_cost(sell_price: float, item_group: str, item_name: str = "") -> float:
    """Deprecated: use get_shipping_rate() from core.shipping instead."""
    from .shipping import _flat_rate_fallback
    return _flat_rate_fallback(item_group, item_name)["cost"]


def calc_financials(item: dict) -> dict:
    """
    Calculate resale financials for a single item record, including estimated
    resale value, recommended maximum buy price, projected gross return, and gross ROI.

    Primary financial metrics intentionally EXCLUDE assumed platform fees and
    shipping costs, as requested by client feedback.
    """
    ai         = item["ai"]
    comps      = item["comps"]
    item_group = ai.get("item_group") or ""
    item_name  = ai.get("item_name") or ""
    cat_id     = ai.get("ebay_category_id")

    # 1. Determine Estimated Resale Value (Sell Price)
    mean_str = comps.get("mean", "N/A")
    if mean_str != "N/A":
        sell_price = float(mean_str.replace("$", "").replace(",", ""))
    else:
        lo = float(ai.get("ai_value_low",  0) or 0)
        hi = float(ai.get("ai_value_high", 0) or 0)
        # Discount the AI's naked estimate by 50% if there are 0 reliable sold comps
        sell_price = ((lo + hi) / 2) * 0.5

    # 2. Determine Estate Sale Buy Price (Observed Tag or 100% Est. Full Value)
    raw_buy = ai.get("estate_buy_price")
    has_buy_price = raw_buy is not None and float(raw_buy or 0) > 0
    buy_price = float(raw_buy) if has_buy_price else 0.0
    price_tag_visible = bool(ai.get("price_tag_visible", False))

    if not has_buy_price and sell_price > 0 and comps.get("count", 0) > 0:
        # Fallback 100% full estate-sale asking price estimate (typical ~20% of resale value)
        buy_price = round(sell_price * 0.20, 2)
        has_buy_price = True

    # 3. Recommended Maximum Purchase Price (incorporates safety margin for unknown fees/shipping)
    if sell_price > 0:
        recommended_max_buy = sell_price * (1.0 - MIN_PROFIT_MARGIN_PCT)
    else:
        recommended_max_buy = 0.0

    # 4. Primary Gross Projections (Gross Return & Gross ROI without fee/shipping deductions)
    if has_buy_price:
        projected_gross_return = sell_price - buy_price
        gross_roi = (projected_gross_return / buy_price * 100.0) if buy_price > 0 else 0.0
    else:
        # Require manual research when no buy price / tag is available and no comps exist
        projected_gross_return = 0.0
        gross_roi = 0.0

    # Still calculate package shipping details for informational display in the UI if needed
    pkg_l  = float(ai.get("pkg_length_in", 0) or 0)
    pkg_w  = float(ai.get("pkg_width_in",  0) or 0)
    pkg_h  = float(ai.get("pkg_height_in", 0) or 0)
    pkg_wt = float(ai.get("pkg_weight_lb", 0) or 0)

    shipping_detail = get_shipping_rate(
        length=pkg_l,
        width=pkg_w,
        height=pkg_h,
        weight=pkg_wt,
        item_group=item_group,
        item_name=item_name,
    )
    item["shipping_detail"] = shipping_detail

    # 5. Adjusted Confidence Calculation
    initial_conf = int(ai.get("confidence", 0))
    count = comps.get("count", 0)
    if count >= 10:
        adj_conf = initial_conf + 10
    elif count >= 5:
        adj_conf = initial_conf + 5
    elif count >= 3:
        adj_conf = initial_conf
    elif count > 0:
        adj_conf = initial_conf - 15
    else:
        adj_conf = initial_conf - 30

    if ai.get("exact_model_identified") is False:
        adj_conf -= 25
    if ai.get("multi_item_detected") is True:
        adj_conf -= 15

    adjusted_confidence = max(10, min(99, adj_conf))

    # 6. Composite Ranking Key
    if not has_buy_price or count == 0:
        sort_roi = -100.0
    elif adjusted_confidence < 70:
        sort_roi = gross_roi * ((adjusted_confidence / 100.0) ** 2) if gross_roi > 0 else gross_roi
    else:
        sort_roi = gross_roi

    return {
        "sell_price":             sell_price,
        "recommended_max_buy":    round(recommended_max_buy, 2),
        "buy_price":              round(buy_price, 2),
        "has_buy_price":          has_buy_price,
        "price_tag_visible":      price_tag_visible,
        "projected_gross_return": round(projected_gross_return, 2),
        "gross_roi":              round(gross_roi, 1),
        "sort_roi":               sort_roi,
        "adjusted_confidence":    adjusted_confidence,
        "profit":                 round(projected_gross_return, 2), # Backward compatibility fallback
        "roi":                    round(gross_roi, 1),              # Backward compatibility fallback
    }


def tier(roi: float) -> tuple[str, str]:
    """
    Map an ROI percentage to a CSS class name and display label.

    Parameters
    ----------
    roi:
        Return-on-investment percentage (e.g. 150.0 for 150 %).

    Returns
    -------
    tuple[str, str]
        ``(css_class, label)`` — e.g. ``("high", "High")``.
    """
    if roi >= 200:
        return "high", "High"
    if roi >= 80:
        return "med", "Medium"
    return "low", "Low"


def get_sort_key(item: dict) -> float:
    """
    Extract a numeric sort key from a fully-calculated item dict.

    The sort field is controlled by :data:`config.SORT_BY`.

    Parameters
    ----------
    item:
        Item dict that must contain ``"financials"`` and either ``"comps"``
        or ``"ai"`` sub-dicts.

    Returns
    -------
    float
        Numeric value used for descending sort (higher is better).
    """
    fin = item.get("financials", {})

    if SORT_BY == "roi":
        return fin.get("sort_roi", fin.get("roi", 0.0))
    if SORT_BY == "profit":
        return fin.get("profit", 0.0)
    if SORT_BY == "confidence":
        return float(item["ai"].get("confidence", 0))

    # Default: sort by mean comp price
    mean = item.get("comps", {}).get("mean", "N/A")
    return float(mean.replace("$", "").replace(",", "")) if mean != "N/A" else 0.0
