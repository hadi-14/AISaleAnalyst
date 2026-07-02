"""
ebay.py
=======
eBay sold-listings scraper for AISaleAnalyst.

Uses ``curl_cffi`` (Chrome TLS impersonation) + ``BeautifulSoup`` to fetch
eBay search pages without any browser.  A one-time warm-up request to the
eBay homepage establishes the session cookies that make subsequent searches
return a 200 OK with fully-rendered HTML.

Public API
----------
scrape_ebay_comps(query, ai_val_low, item_name, ...)
    Scrape eBay completed/sold listings for ``query`` and return a comps
    summary dict.  Uses a 4-level progressive fallback so that results are
    always returned even when strict filters over-restrict.

get_ai_negative_keywords(item_name, query)
    Ask the AI for item-specific eBay exclusion keywords (``-word`` syntax).
    Results are cached per query for the lifetime of the process.

should_filter_by_title(title, query)
    Post-filter: return True if a listing title contains known parts /
    accessory words that are not part of the search query itself.
"""

import re
import time
import threading

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from .config import AI_PROVIDER, EBAY_DELAY, fix_and_parse_json

if AI_PROVIDER == "openai":
    from .config import openai_client
else:
    from .config import gemini_client

# ---------------------------------------------------------------------------
# Shared curl_cffi session (one warm-up, reused for all searches)
# ---------------------------------------------------------------------------

_session: cffi_requests.Session | None = None
_session_lock = threading.Lock()

_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def _get_session() -> cffi_requests.Session:
    """
    Return the shared curl_cffi session, creating and warming it up on first call.

    The warm-up GET to the eBay homepage establishes the bot-detection cookies
    (``__uzma``, ``nonsession``, etc.) that are required for search pages to
    return 200 OK with full HTML instead of a 403 Error Page.
    """
    global _session
    if _session is not None:
        return _session

    with _session_lock:
        if _session is not None:          # double-checked locking
            return _session

        print("  [eBay] Warming up HTTP session...")
        sess = cffi_requests.Session(impersonate="chrome124")
        try:
            sess.get("https://www.ebay.com/", headers=_REQUEST_HEADERS, timeout=15)
            time.sleep(0.5)
            print("  [eBay] Session ready.")
        except Exception as exc:
            print(f"  [eBay] Warm-up warning: {exc}")

        _session = sess
        return _session


# ---------------------------------------------------------------------------
# Post-filter: static negative word list
# ---------------------------------------------------------------------------

#: Words that strongly suggest a listing is for a *part*, *accessory*, or
#: *manual* rather than the complete item being searched for.
_STATIC_NEGATIVE_WORDS: frozenset[str] = frozenset({
    "part", "parts", "accessory", "accessories", "cover", "covers",
    "manual", "manuals", "decal", "decals", "sticker", "stickers",
    "toy", "toys", "model", "models", "miniature", "brochure", "brochures",
    "catalog", "catalogs", "instructions", "latch", "latches", "plugs", "plug",
    "wheel", "wheels", "tire", "tires", "trailer", "trailers",
    "keychain", "keychains", "poster", "posters", "print", "prints",
    "replacement", "repair", "service", "guide", "guides", "handbook",
    "pdf", "download", "dvd", "cd", "software", "copy", "reprint",
    "cabling", "cable", "cables", "cord", "cords", "charger", "chargers",
    "case", "cases", "bag", "bags", "sleeve", "sleeves", "strap", "straps",
    "battery", "batteries", "bulb", "bulbs", "remote", "remotes",
    "stand", "stands", "mount", "mounts", "bracket", "brackets",
    "screw", "screws", "bolt", "bolts", "nut", "nuts", "adapter", "adapters",
    "diagram", "harness", "harnesses", "switch", "switches", "sensor", "sensors",
    "gasket", "gaskets", "seal", "seals", "filter", "filters",
    "windshield", "windshields", "panel", "panels", "curtain", "curtains",
    "seat", "seats", "cushion", "cushions", "steering", "motor", "motors",
    "engine", "engines", "propeller", "propellers", "impeller", "impellers",
    "carburetor", "carburetors", "pump", "pumps", "wiring", "light", "lights",
    "hardware", "frame", "frames", "glass", "canvas", "canvases", "bimini",
    "top", "tops", "hatch", "hatches", "lock", "locks", "key", "keys",
    "pedestal", "pedestals", "blade", "blades", "knife", "knives",
    "belt", "belts", "pulley", "pulleys", "clutch", "clutches",
    "spark", "sparkplug", "sparkplugs", "screen", "screens", "carb",
    "carbs", "bearing", "bearings", "hose", "hoses", "oil", "gas",
    "cap", "caps", "spring", "springs", "shaft", "shafts", "pin", "pins",
    "box", "boxes", "empty", "packaging", "package", "packages",
})


def should_filter_by_title(title: str, query: str) -> bool:
    """
    Return True if *title* contains a known parts/accessories word that
    does not appear in *query* (so we don't accidentally filter e.g. a
    "boat motor" search when the word "motor" appears in the query).

    Parameters
    ----------
    title:
        eBay listing title text.
    query:
        The search query used to find this listing.

    Returns
    -------
    bool
        True  -> listing should be excluded.
        False -> listing is likely a whole-unit sale.
    """
    title_lower = title.lower()
    query_words = set(re.findall(r"\b\w+\b", query.lower()))

    for word in _STATIC_NEGATIVE_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", title_lower):
            singular = word[:-1] if word.endswith("s") else word
            plural   = word + "s" if not word.endswith("s") else word
            if word not in query_words and singular not in query_words and plural not in query_words:
                return True

    return False


# ---------------------------------------------------------------------------
# AI-generated negative keywords (cached per session)
# ---------------------------------------------------------------------------

#: Session-level cache: query string -> list of exclusion keywords.
_neg_kw_cache: dict[str, list[str]] = {}


def get_ai_negative_keywords(item_name: str, query: str) -> list[str]:
    """
    Ask the AI for 8-12 item-specific keywords to exclude from the eBay
    search (``-keyword`` syntax).  Results are cached per *query* so
    repeated calls for the same item are free.

    Parameters
    ----------
    item_name:
        Human-readable item name (used in the prompt for context).
    query:
        The eBay search query string (used as the cache key).

    Returns
    -------
    list[str]
        Lowercase single-word exclusion terms, e.g.
        ``["carburetor", "blade", "manual", "gasket"]``.
        Returns an empty list on any error.
    """
    cache_key = query.lower().strip()
    if cache_key in _neg_kw_cache:
        return _neg_kw_cache[cache_key]

    prompt = (
        f'I am searching eBay sold listings for: "{item_name}"\n'
        "Generate 8-12 specific single-word keywords for PARTS, ACCESSORIES, "
        "or COMPONENTS of this item that would appear in unrelated cheap listings "
        "and should be EXCLUDED from search results.\n"
        "Rules:\n"
        '- Return ONLY a JSON array of lowercase single words, e.g. ["carburetor", "blade", "manual"]\n'
        "- Do NOT include words that are part of the item's name itself\n"
        "- Focus on small cheap parts/accessories/consumables/manuals that often appear "
        "when searching for the full item\n"
        "- No explanation, no markdown, just the JSON array"
    )

    try:
        if AI_PROVIDER == "openai":
            response = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=120,
            )
            text = response.choices[0].message.content.strip()
        else:  # gemini
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[prompt],
            )
            text = response.text.strip()

        keywords = fix_and_parse_json(text)
        if isinstance(keywords, list):
            keywords = [
                k.strip().lower()
                for k in keywords
                if isinstance(k, str) and " " not in k.strip()
            ][:5]
            _neg_kw_cache[cache_key] = keywords
            print(f"  [eBay exclusions] {keywords}")
            return keywords

    except Exception as exc:
        print(f"  [eBay exclusions] AI error ({exc}) -- skipping")

    _neg_kw_cache[cache_key] = []
    return []


# ---------------------------------------------------------------------------
# HTML scraping helpers (BeautifulSoup)
# ---------------------------------------------------------------------------

def _parse_prices_from_html(html: str, query: str) -> tuple[list[float], list[str]]:
    """
    Parse sold prices and listing links from an eBay search results HTML page.

    Targets the ``su-card-container__attributes`` card layout used by eBay's
    current SSR search pages.  Falls back to a regex price sweep if no cards
    are found.

    Parameters
    ----------
    html:
        Raw HTML text of the eBay search results page.
    query:
        The search query string, used by :func:`should_filter_by_title`.

    Returns
    -------
    tuple[list[float], list[str]]
        ``(prices, comp_links)`` -- prices in page order, and up to 3 item URLs.
    """
    soup = BeautifulSoup(html, "html.parser")

    prices:     list[float] = []
    comp_links: list[str]   = []

    # If eBay explicitly states 0 results (but shows "results matching fewer words"), abort early if we can detect it.
    heading = soup.select_one("h1.srp-controls__count-heading, h1.rs-controls__count-heading, h1.s-title-count")
    if heading:
        heading_text = heading.get_text(strip=True).lower()
        if heading_text.startswith("0 result") or "no exact matches" in heading_text:
            return [], []

    # Select the main listing cards (supports both traditional and new SSR layouts)
    cards = soup.select("li.s-item, div.s-item, div.su-card-container")

    for card in cards:
        # Title: used for post-filtering parts/accessories
        title_el = card.select_one("a.s-card__link, [class*='s-card__link'], h3.s-item__title, .s-item__title")
        title = title_el.get_text(strip=True) if title_el else ""
        if title and should_filter_by_title(title, query):
            continue

        # Price: positive/non-strikethrough = final sold price
        price_el = card.select_one("span.s-card__price:not(.strikethrough)")
        if not price_el:
            price_el = card.select_one("[class*='s-card__price']")

        price_txt = price_el.get_text(strip=True) if price_el else ""
        price_m = re.search(r"([\d,]+\.?\d*)", price_txt.replace(",", ""))
        if price_m:
            val = float(price_m.group(1))
            if val > 0:
                prices.append(val)

                # Grab listing link
                if len(comp_links) < 3:
                    link_el = card.select_one("a[href*='itm']")
                    if not link_el and title_el:
                        link_el = title_el if title_el.name == "a" else title_el.find_parent("a")
                    if link_el:
                        href = link_el.get("href", "")
                        if href and href not in comp_links:
                            comp_links.append(href)

    if not prices:
        for el in soup.select("span.s-card__price:not(.strikethrough), [class*='s-card__price'], .s-item__price span.ITALIC, .s-item__price"):
            txt = el.get_text(strip=True)
            m = re.search(r"([\d,]+\.?\d*)", txt.replace(",", ""))
            if m:
                val = float(m.group(1))
                if val > 0:
                    prices.append(val)

    return prices, comp_links

import threading
import time
import random

_request_lock = threading.Lock()

def _fetch_prices_from_url(search_url: str, query: str) -> tuple[list[float], list[str]]:
    """
    Fetch *search_url* via the shared curl_cffi session and extract sold prices.

    Parameters
    ----------
    search_url:
        Fully-formed eBay search URL.
    query:
        The search query string, used by :func:`should_filter_by_title`.

    Returns
    -------
    tuple[list[float], list[str]]
        ``(prices, comp_links)``
    """
    session = _get_session()
    try:
        with _request_lock:
            # Sleep 1.5 - 3.5s to mimic human browsing and completely prevent CAPTCHAs
            time.sleep(random.uniform(1.5, 3.5))
            resp = session.get(
                search_url,
                headers={**_REQUEST_HEADERS, "Referer": "https://www.ebay.com/"},
                timeout=20,
            )
        if resp.status_code != 200:
            print(f"  [eBay] HTTP {resp.status_code} for {search_url[:80]}")
            return [], []
        return _parse_prices_from_html(resp.text, query)
    except Exception as exc:
        print(f"  [eBay] Request error: {exc}")
        return [], []


# ---------------------------------------------------------------------------
# Price processing
# ---------------------------------------------------------------------------

def process_ebay_prices(prices_recent_first: list[float]) -> tuple[float, float, float]:
    """
    Filter out obvious outliers and calculate the weighted mean sold price.
    Recent listings (first in the list) are weighted more heavily than older ones.
    """
    if not prices_recent_first:
        return 0.0, 0.0, 0.0

    sorted_prices = sorted(prices_recent_first)
    simple_median = sorted_prices[len(sorted_prices) // 2]

    filtered = [
        p for p in prices_recent_first
        if (0.3 * simple_median) <= p <= (3.0 * simple_median)
    ]

    if not filtered:
        filtered = prices_recent_first

    weighted_pool = []
    for i, p in enumerate(filtered):
        if i < 3:
            weight = 3
        elif i < 8:
            weight = 2
        else:
            weight = 1
        weighted_pool.extend([p] * weight)

    mean_val = sum(weighted_pool) / len(weighted_pool)

    return min(filtered), mean_val, max(filtered)


# ---------------------------------------------------------------------------
# Condition helper
# ---------------------------------------------------------------------------

def get_condition_param(condition: str | None) -> str:
    """Map human condition names to eBay condition URL parameter filters."""
    if not condition:
        return ""
    cond_lower = condition.lower().strip()
    if "new" in cond_lower:
        return "&LH_ItemCondition=1000"
    elif "open" in cond_lower or "box" in cond_lower:
        return "&LH_ItemCondition=1500"
    elif "parts" in cond_lower or "repair" in cond_lower:
        return "&LH_ItemCondition=7000"
    elif "used" in cond_lower or "second" in cond_lower:
        return "&LH_ItemCondition=3000"
    return ""


# ---------------------------------------------------------------------------
# Main public scraping function
# ---------------------------------------------------------------------------

#: eBay sold/completed filter suffix appended to every search URL.
_EBAY_SUFFIX = "&LH_Complete=1&LH_Sold=1&_sop=13"


def scrape_ebay_comps(
    driver,          # kept for signature compatibility -- no longer used
    query: str,
    ai_val_low: float = 0,
    item_name: str = "",
    category_id: int | None = None,
    fallback_query: str | None = None,
    ebay_condition: str | None = None,
) -> dict:
    """
    Scrape eBay sold listings for *query* and return a comps summary.

    The ``driver`` parameter is accepted but ignored -- scraping now uses
    ``curl_cffi`` + ``BeautifulSoup`` with no browser required.

    Uses a 4-level progressive fallback to guarantee results:

    1. AI exclusions + price floor + condition (strictest)
    2. AI exclusions + price floor + condition (or exclusions+condition only if cheap)
    3. AI exclusions + price floor (no condition filter)
    4. Bare query + price floor (no condition filter)

    Parameters
    ----------
    driver:
        Ignored (kept for backward compatibility).
    query:
        eBay search query string.
    ai_val_low:
        Lower bound of the AI's USD value estimate. Used for price floor.
    item_name:
        Human-readable item name.
    category_id:
        Optional eBay Category ID (sacat) to restrict the search.
    fallback_query:
        Optional broader search query string.
    ebay_condition:
        Optional general condition matching: New, Open Box, Used, For parts.

    Returns
    -------
    dict
        Keys: ``low``, ``median``, ``high``, ``count``, ``link``, ``links``, ``fallback_used``.
    """
    try:
        cleaned_query = re.sub(r"\b(sold|completed|complete)\b", "", query, flags=re.IGNORECASE).strip()
        cleaned_query = re.sub(r"\s+", " ", cleaned_query)

        min_price       = int(ai_val_low * 0.20) if ai_val_low and ai_val_low > 0 else 0
        neg_keywords    = get_ai_negative_keywords(item_name or cleaned_query, cleaned_query)
        exclusion_str   = "".join(f"+-{kw}" for kw in neg_keywords)
        base_nkw        = cleaned_query.replace(" ", "+")
        floor_param     = f"&_udlo={min_price}" if min_price > 0 else ""
        category_param  = f"&_sacat={category_id}" if category_id and int(category_id) > 0 else ""
        condition_param = get_condition_param(ebay_condition)
        strict_floor    = (ai_val_low >= 100.0)

        attempts = [
            f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{exclusion_str}{_EBAY_SUFFIX}{floor_param}{category_param}{condition_param}",
            f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{exclusion_str}{_EBAY_SUFFIX}{floor_param}{category_param}{condition_param}" if strict_floor else f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{exclusion_str}{_EBAY_SUFFIX}{category_param}{condition_param}",
            f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{''.join(f'+-{kw}' for kw in neg_keywords[:3])}{_EBAY_SUFFIX}{floor_param}{category_param}" if strict_floor else f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{''.join(f'+-{kw}' for kw in neg_keywords[:3])}{_EBAY_SUFFIX}{category_param}",
            f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{_EBAY_SUFFIX}{floor_param}{category_param}" if strict_floor else f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{_EBAY_SUFFIX}{category_param}",
        ]
        labels = [
            "exclusions+floor+condition",
            "exclusions+floor+condition" if strict_floor else "exclusions+condition only",
            "top-3 exclusions+floor" if strict_floor else "top-3 exclusions",
            "bare query+floor" if strict_floor else "bare query",
        ]

        best_prices: list[float] = []
        best_links: list[str]   = []
        best_url: str         = attempts[0]

        for url, label in zip(attempts, labels):
            prices, comp_links = _fetch_prices_from_url(url, cleaned_query)
            
            if len(prices) > len(best_prices):
                best_prices = prices
                best_links = comp_links
                best_url = url

            if len(prices) >= 3:
                if label != "exclusions+floor+condition":
                    print(f"  [fallback -> {label}]")
                break
            print(f"  [{len(prices)} results with {label} - trying next]")

        prices = best_prices
        comp_links = best_links
        used_url = best_url

        if not prices:
            if fallback_query:
                print(f"  [0 results -> trying fallback query: '{fallback_query}']")
                res = scrape_ebay_comps(
                    driver,
                    fallback_query,
                    ai_val_low=ai_val_low,
                    item_name=item_name,
                    category_id=category_id,
                    fallback_query=None,
                    ebay_condition=ebay_condition,
                )
                res["fallback_used"] = True
                return res
            return {
                "low": "N/A", "mean": "N/A", "high": "N/A",
                "count": 0, "link": used_url, "links": [],
                "fallback_used": False, "query_used": cleaned_query,
            }

        low_val, mean_val, high_val = process_ebay_prices(prices)

        return {
            "low":          f"${low_val:.0f}",
            "mean":         f"${mean_val:.0f}",
            "high":         f"${high_val:.0f}",
            "count":        len(prices),
            "link":         used_url,
            "links":        comp_links,
            "fallback_used": False,
            "query_used":   cleaned_query,
        }

    except Exception as exc:
        print(f"  eBay scrape error: {exc}")
        return {
            "low": "N/A", "mean": "N/A", "high": "N/A",
            "count": 0, "link": "", "links": [],
            "fallback_used": False, "query_used": query,
        }
