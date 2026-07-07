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

# Soft-block detection: track how many items in a row returned 0 results
# across ALL fallbacks. If this hits _BLOCK_THRESHOLD, we pause and cycle.
_consecutive_failures: int = 0
_failures_lock = threading.Lock()
_BLOCK_THRESHOLD: int = 3       # pause after this many consecutive zero-result items
_BLOCK_PAUSE_SECONDS: int = 70  # how long to sleep when a block is detected
_cooldown_until: float = 0.0    # global timestamp for pausing all workers

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
    # Target only children of the main srp-results list to avoid "Similar sponsored items" or "Results matching fewer words"
    cards = soup.select("ul.srp-results > li.s-card, ul.srp-results > li.s-item")

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
        for el in soup.select("ul.srp-results span.s-card__price:not(.strikethrough), ul.srp-results [class*='s-card__price'], ul.srp-results .s-item__price span.ITALIC, ul.srp-results .s-item__price"):
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

def _fetch_prices_from_url(search_url: str, query: str, max_retries: int = 3) -> tuple[list[float], list[str]]:
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
    global _session, _cooldown_until
    for attempt in range(max_retries):
        while True:
            if time.time() < _cooldown_until:
                time.sleep(1.0)
            else:
                break
                
        session = _get_session()
        try:
            with _request_lock:
                # Sleep to mimic human browsing and prevent CAPTCHAs.
                # Increase sleep time if we are retrying due to a block.
                sleep_time = random.uniform(2.5, 4.5) if attempt == 0 else random.uniform(10.0, 15.0)
                time.sleep(sleep_time)
                resp = session.get(
                    search_url,
                    headers={**_REQUEST_HEADERS, "Referer": "https://www.ebay.com/"},
                    timeout=20,
                )
            
            text_lower = resp.text.lower()
            is_blocked = False
            
            if "captcha" in text_lower or "security measure" in text_lower:
                print(f"  [eBay] 🚨 CAPTCHA BLOCK DETECTED (Attempt {attempt+1}/{max_retries})")
                is_blocked = True
            elif "error page | ebay" in text_lower or "something went wrong on our end" in text_lower:
                print(f"  [eBay] 🚨 ERROR PAGE BLOCK DETECTED (Attempt {attempt+1}/{max_retries})")
                is_blocked = True
            elif resp.status_code != 200:
                print(f"  [eBay] HTTP {resp.status_code} for {search_url[:80]} (Attempt {attempt+1}/{max_retries})")
                is_blocked = True
                
            if is_blocked:
                if attempt < max_retries - 1:
                    print("  [eBay] Auto-resolving block... sleeping and cycling session.")
                    with _request_lock:
                        _session = None # Force session recreation
                        _cooldown_until = max(_cooldown_until, time.time() + 15)
                    continue
                else:
                    print("  [eBay] Max retries reached. Raising exception to prevent fallback cascade.")
                    raise RuntimeError("eBay Anti-Bot Blocked")
            
            prices, links = _parse_prices_from_html(resp.text, query)
            
            # Save HTML if 0 results, to help diagnose if it's a DOM change or block
            if not prices:
                with open("ebay_debug_0_results.html", "w", encoding="utf-8") as f:
                    f.write(resp.text)
                    
            return prices, links
        except RuntimeError:
            raise # Re-raise the block exception to abort fallbacks
        except Exception as exc:
            print(f"  [eBay] Request error: {exc}")
            if attempt < max_retries - 1:
                time.sleep(5)
                continue
            return [], []
            
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
_EBAY_SUFFIX = "&LH_Complete=1&LH_Sold=1&_sop=13&LH_PrefLoc=1"


def scrape_ebay_comps(
    driver,          # kept for signature compatibility -- no longer used
    query: str,
    ai_val_low: float = 0,
    item_name: str = "",
    fallback_query: str | None = None,
    ebay_condition: str | None = None,
    exclusion_keywords: list[str] | None = None,
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
    fallback_query:
        Optional broader search query string.
    ebay_condition:
        Optional general condition matching: New, Open Box, Used, For parts.

    Returns
    -------
    dict
        Keys: ``low``, ``median``, ``high``, ``count``, ``link``, ``links``, ``fallback_used``.
    """
    global _consecutive_failures, _session, _cooldown_until
    try:
        import urllib.parse
        cleaned_query = re.sub(r"\b(sold|completed|complete)\b", "", query, flags=re.IGNORECASE).strip()
        cleaned_query = re.sub(r"\s+", " ", cleaned_query)

        min_price       = int(ai_val_low * 0.20) if ai_val_low and ai_val_low > 0 else 0
        neg_keywords    = exclusion_keywords or []
        
        # Proper URL encoding for the search query and exclusions
        base_nkw        = urllib.parse.quote_plus(cleaned_query)
        exclusion_str   = "".join(f"+-{urllib.parse.quote_plus(kw)}" for kw in neg_keywords)
        
        floor_param     = f"&_udlo={min_price}" if min_price > 0 else ""
        condition_param = get_condition_param(ebay_condition)
        strict_floor    = (ai_val_low >= 100.0)

        attempts = [
            f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{exclusion_str}{_EBAY_SUFFIX}{floor_param}{condition_param}",
            f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{exclusion_str}{_EBAY_SUFFIX}{floor_param}{condition_param}" if strict_floor else f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{exclusion_str}{_EBAY_SUFFIX}{condition_param}",
            f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{''.join(f'+-{urllib.parse.quote_plus(kw)}' for kw in neg_keywords[:3])}{_EBAY_SUFFIX}{floor_param}" if strict_floor else f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{''.join(f'+-{urllib.parse.quote_plus(kw)}' for kw in neg_keywords[:3])}{_EBAY_SUFFIX}",
            f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{_EBAY_SUFFIX}{floor_param}" if strict_floor else f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{_EBAY_SUFFIX}",
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

        # Extract exact query string used from the URL
        import urllib.parse
        parsed = urllib.parse.urlparse(used_url)
        exact_query = urllib.parse.parse_qs(parsed.query).get('_nkw', [cleaned_query])[0]

        if not prices:
            if fallback_query:
                print(f"  [0 results -> trying fallback query: '{fallback_query}']")
                res = scrape_ebay_comps(
                    driver,
                    fallback_query,
                    ai_val_low=ai_val_low,
                    item_name=item_name,
                    fallback_query=None,
                    ebay_condition=ebay_condition,
                    exclusion_keywords=exclusion_keywords,
                )
                res["fallback_used"] = True
                return res

            # All fallbacks exhausted with 0 results — check for soft block
            with _failures_lock:
                _consecutive_failures += 1
                fails = _consecutive_failures

            if fails >= _BLOCK_THRESHOLD:
                print(f"  [eBay] ⚠️  {fails} consecutive items returned 0 results — "
                      f"eBay soft block detected. Pausing {_BLOCK_PAUSE_SECONDS}s and cycling session...")
                with _failures_lock:
                    _consecutive_failures = 0
                with _session_lock:
                    _session = None   # Force a fresh warm-up on next request
                _cooldown_until = time.time() + _BLOCK_PAUSE_SECONDS

            return {
                "low": "N/A", "mean": "N/A", "high": "N/A",
                "count": 0, "link": used_url, "links": [],
                "fallback_used": False, "query_used": exact_query,
            }

        # Successful result — reset consecutive failure counter
        with _failures_lock:
            _consecutive_failures = 0

        low_val, mean_val, high_val = process_ebay_prices(prices)

        return {
            "low":          f"${low_val:.0f}",
            "mean":         f"${mean_val:.0f}",
            "high":         f"${high_val:.0f}",
            "count":        len(prices),
            "link":         used_url,
            "links":        comp_links,
            "fallback_used": False,
            "query_used":   exact_query,
        }

    except Exception as exc:
        print(f"  eBay scrape error: {exc}")
        return {
            "low": "N/A", "mean": "N/A", "high": "N/A",
            "count": 0, "link": "", "links": [],
            "fallback_used": False, "query_used": query,
        }

def cleanup_session():
    """Close the underlying curl_cffi session to cleanly exit background threads."""
    global _session
    if _session is not None:
        try:
            _session.close()
        except Exception:
            pass
        _session = None
