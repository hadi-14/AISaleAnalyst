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

from .config import SORT_BY

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


def estimate_shipping_cost(sell_price: float, item_group: str, item_name: str = "") -> float:
    """Estimate shipping cost based on item group and size/weight characteristics."""
    text = f"{item_group} {item_name}".lower().strip()
    
    # 1. Local Pickup / Heavy Freight ($0.00)
    local_pickup_groups = {
        "boat", "vehicle", "car", "motorcycle", "trailer", "chipper",
        "lawnmower", "tractor", "shredder", "furniture", "sofa", "table",
        "freezer", "refrigerator", "chest", "snowblower"
    }
    if any(g in text for g in local_pickup_groups):
        return 0.0

    # 2. Large / Heavy Electronics & Equipment ($35.00)
    heavy_shippable = {
        "generator", "amplifier", "receiver", "stereo", "speaker", "mower",
        "compressor", "saw", "drill press", "lathe", "welder", "outboard", "trolling motor"
    }
    if any(g in text for g in heavy_shippable):
        return 35.0

    # 3. Small / Lightweight Collectibles & Wearables ($8.00)
    lightweight = {
        "watch", "jewelry", "ring", "necklace", "camera", "lens", "coin",
        "stamp", "card", "shirt", "jacket", "coat", "hat", "glasses", "sunglasses",
        "brooch", "pendant", "earring", "manual", "book", "cd", "dvd", "game", "toy", "figurine"
    }
    if any(g in text for g in lightweight):
        return 8.0

    # 4. Standard medium item default ($15.00)
    return 15.0


def calc_financials(item: dict) -> dict:
    """
    Calculate resale financials for a single item record, including estimated
    eBay fees, shipping, net proceeds, and recommended purchase prices.

    Sell price is taken from the eBay median comp when available; otherwise
    the midpoint of the AI's estimated value range is used.

    Buy price comes from the AI's ``estate_buy_price`` field; if that is
    zero or missing it defaults to 20 % of the sell price.

    Parameters
    ----------
    item:
        Item dict that must contain ``"ai"`` and ``"comps"`` sub-dicts.
        ``"comps"`` must have a ``"median"`` key (``"$NNN"`` or ``"N/A"``).

    Returns
    -------
    dict
        Keys: ``sell_price`` (float), ``ebay_fee`` (float), ``shipping`` (float),
        ``net_after_fees`` (float), ``recommended_max_buy`` (float),
        ``buy_price`` (float), ``profit`` (float), ``roi`` (float — percentage).
    """
    ai         = item["ai"]
    comps      = item["comps"]
    item_group = ai.get("item_group") or ""
    item_name  = ai.get("item_name") or ""
    cat_id     = ai.get("ebay_category_id")

    median_str = comps.get("median", "N/A")
    if median_str != "N/A":
        sell_price = float(median_str.replace("$", "").replace(",", ""))
    else:
        lo         = float(ai.get("ai_value_low",  0) or 0)
        hi         = float(ai.get("ai_value_high", 0) or 0)
        sell_price = (lo + hi) / 2

    # Calculate fees and shipping
    ebay_fee = estimate_ebay_fee(sell_price, cat_id)
    shipping = estimate_shipping_cost(sell_price, item_group, item_name)

    # Net proceeds after fees
    net_after_fees = sell_price - ebay_fee - shipping

    # Recommended maximum purchase price (30 % of net selling price)
    recommended_max_buy = net_after_fees * 0.30

    # Actual estate sale buy price (AI estimate or default to 20 % of sell price)
    buy_price = float(ai.get("estate_buy_price", 0) or 0)
    if buy_price == 0:
        buy_price = sell_price * 0.20

    # Expected net profit
    profit = net_after_fees - buy_price
    
    # ROI based on actual buy price
    roi = (profit / buy_price * 100) if buy_price > 0 else 0.0

    # Calculate adjusted confidence based on listing match counts
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

    # Lower confidence when exact model is not identified
    if ai.get("exact_model_identified") is False:
        adj_conf -= 25

    adjusted_confidence = max(10, min(99, adj_conf))

    return {
        "sell_price":          sell_price,
        "ebay_fee":            round(ebay_fee, 2),
        "shipping":            round(shipping, 2),
        "net_after_fees":      round(net_after_fees, 2),
        "recommended_max_buy": round(recommended_max_buy, 2),
        "buy_price":           round(buy_price, 2),
        "profit":              round(profit, 2),
        "roi":                 round(roi, 1),
        "adjusted_confidence": adjusted_confidence,
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
        return fin.get("roi", 0.0)
    if SORT_BY == "profit":
        return fin.get("profit", 0.0)
    if SORT_BY == "confidence":
        return float(item["ai"].get("confidence", 0))

    # Default: sort by median comp price
    median = item.get("comps", {}).get("median", "N/A")
    return float(median.replace("$", "").replace(",", "")) if median != "N/A" else 0.0
