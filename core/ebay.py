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

def close_ebay_session():
    global _session
    with _session_lock:
        if _session is not None:
            try:
                _session.close()
            except Exception:
                pass
            _session = None

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
    "keychain", "keychains", "poster", "posters",
    "replacement", "repair", "service", "guide", "guides", "handbook",
    "pdf", "download", "dvd", "cd", "software", "copy", "reprint",
    "cabling", "cable", "cables", "cord", "cords", "charger", "chargers",
    "bag", "bags", "sleeve", "sleeves", "strap", "straps",
    "battery", "batteries", "bulb", "bulbs", "remote", "remotes",
    "bracket", "brackets",
    "screw", "screws", "bolt", "bolts", "nut", "nuts", "adapter", "adapters",
    "diagram", "harness", "harnesses", "switch", "switches", "sensor", "sensors",
    "gasket", "gaskets", "seal", "seals", "filter", "filters",
    "windshield", "windshields", "panel", "panels", "curtain", "curtains",
    "seat", "seats", "cushion", "cushions", "steering", "motor", "motors",
    "engine", "engines", "propeller", "propellers", "impeller", "impellers",
    "carburetor", "carburetors", "pump", "pumps", "wiring",
    "hardware", "bimini", "hatch", "hatches",
    "pedestal", "pedestals", "blade", "blades", "knife", "knives",
    "belt", "belts", "pulley", "pulleys", "clutch", "clutches",
    "spark", "sparkplug", "sparkplugs", "carb", "carbs",
    "bearing", "bearings", "hose", "hoses",
    "spring", "springs", "shaft", "shafts",
    "empty", "packaging", "package", "packages",
    # Removed: "canvas", "canvases", "print", "prints", "frame", "frames",
    # "glass", "box", "boxes", "light", "lights", "stand", "stands",
    # "mount", "mounts", "key", "keys", "lock", "locks", "pin", "pins",
    # "cap", "caps", "top", "tops", "case", "cases", "screen", "screens",
    # "oil", "gas" — common whole-item nouns in art/glass/furniture/
    # lighting/jewelry titles, was causing legit comps to be discarded.
})


def should_filter_by_title(title: str, query: str, inclusion_keywords: list[str] | None = None) -> bool:
    """
    Return True if *title* contains a known parts/accessories word that
    does not appear in *query* (so we don't accidentally filter e.g. a
    "boat motor" search when the word "motor" appears in the query) or if
    the title fails core query token overlap requirements.

    Parameters
    ----------
    title:
        eBay listing title text.
    query:
        The search query used to find this listing.
    inclusion_keywords:
        List of keywords that MUST appear in the title.

    Returns
    -------
    bool
        True  -> listing should be excluded.
        False -> listing is likely a whole-unit sale.
    """
    title_lower = title.lower()

    if inclusion_keywords:
        for kw in inclusion_keywords:
            # Check if keyword is in the title, skip filter if the keyword is a generic instruction
            if "add specific words" in kw.lower():
                continue
            if kw.lower() not in title_lower:
                return True

    query_words = set(re.findall(r"\b\w+\b", query.lower()))

    # Positive Match Filter: Require at least some core query nouns to appear in title
    stop_words = {"vintage", "antique", "retro", "mid-century", "midcentury", "set", "the", "a", "an", "and", "or", "with", "of", "in", "on", "for", "rare", "old", "used", "original"}
    core_query_words = query_words - stop_words
    
    if core_query_words:
        title_words = set(re.findall(r"\b\w+\b", title_lower))
        matches = len(core_query_words.intersection(title_words))
        # Require at least 1 core word. If there are 3+ core words, require at least 2.
        required_matches = 2 if len(core_query_words) >= 3 else 1
        if matches < required_matches:
            return True

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

def _parse_prices_from_html(html: str, query: str, inclusion_keywords: list[str] | None = None) -> tuple[list[float], list[str], int]:
    """
    Parse sold prices, listing links, and total result count from an eBay search results HTML page.

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
    tuple[list[float], list[str], int]
        ``(prices, comp_links, total_count)`` -- prices in page order, up to 3 item URLs, and total matches.
    """
    soup = BeautifulSoup(html, "html.parser")

    prices:     list[float] = []
    comp_links: list[str]   = []
    total_count: int        = 0

    # Check overall SRP header & controls for 0 exact match notices
    heading = soup.select_one("h1.srp-controls__count-heading, h1.rs-controls__count-heading, h1.s-title-count, .srp-controls__count-heading, .srp-river-answer--REWRITE_START, .srp-save-search-options, .s-answer-region")
    if heading:
        heading_text = heading.get_text(strip=True).lower()
        if "0 result" in heading_text or "no exact matches" in heading_text or "fewer words" in heading_text:
            return [], [], 0
        m_count = re.search(r"([\d,]+)\+?\s+result", heading_text)
        if m_count:
            try:
                total_count = int(m_count.group(1).replace(",", ""))
            except ValueError:
                pass

    # Detect loading skeleton pages which mean eBay didn't return real results
    if soup.select_one(".srp-skeleton, .skeleton-placeholder, #srp-skeleton, .strk-loading"):
        raise ValueError("eBay returned a loading skeleton page")

    # Select all direct list items under srp-results to detect rewrite/fewer-words answer banners
    items = soup.select("ul.srp-results > li")
    
    # If there's no heading and no items, the page failed to load fully (stealth block or timeout)
    if not heading and not items:
        page_title = soup.title.string.strip() if soup.title else "No Title"
        raise ValueError(f"Incomplete page load or stealth block (Title: '{page_title}')")

    for item in items:
        item_class = " ".join(item.get("class", []))
        item_text = item.get_text(strip=True).lower()
        
        # Stop iteration if we reach eBay's "Results matching fewer words" or "No exact matches" banner
        if "srp-river-answer" in item_class or "results matching fewer words" in item_text or "no exact matches" in item_text:
            break

        if not ("s-card" in item_class or "s-item" in item_class):
            continue

        card = item
        # Title: used for post-filtering parts/accessories and title relevance
        title_el = card.select_one("a.s-card__link, [class*='s-card__link'], h3.s-item__title, .s-item__title")
        title = title_el.get_text(strip=True) if title_el else ""
        if title and should_filter_by_title(title, query, inclusion_keywords=inclusion_keywords):
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

    if total_count == 0:
        total_count = len(prices)

    return prices, comp_links, total_count

import threading
import time
import random

def _fetch_prices_from_url(search_url: str, query: str, max_retries: int = 3, inclusion_keywords: list[str] | None = None) -> tuple[list[float], list[str], int]:
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
    tuple[list[float], list[str], int]
        ``(prices, comp_links, total_count)``
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
            # Independent per-thread sleep to mimic human browsing and prevent CAPTCHAs.
            # Increase sleep time if we are retrying due to a block.
            sleep_time = random.uniform(1.0, 2.0) if attempt == 0 else random.uniform(5.0, 10.0)
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
                    print(f"  [eBay] Auto-resolving block... sleeping and cycling session.")
                    _session = None # Force session recreation
                    _cooldown_until = max(_cooldown_until, time.time() + 15)
                    continue
                else:
                    print("  [eBay] Max retries reached. Raising exception to prevent fallback cascade.")
                    raise RuntimeError("eBay Anti-Bot Blocked")
            
            prices, links, total_cnt = _parse_prices_from_html(resp.text, query, inclusion_keywords=inclusion_keywords)
            
            # Save HTML if 0 results, to help diagnose if it's a DOM change or block
            if not prices:
                with open("ebay_debug_0_results.html", "w", encoding="utf-8") as f:
                    f.write(resp.text)
                    
            return prices, links, total_cnt
        except RuntimeError:
            raise # Re-raise the block exception to abort fallbacks
        except Exception as exc:
            print(f"  [eBay] Request error: {exc}")
            if attempt < max_retries - 1:
                # If we hit our stealth block ValueError, cycle the session
                if isinstance(exc, ValueError):
                    _session = None
                    _cooldown_until = max(_cooldown_until, time.time() + 15)
                else:
                    time.sleep(5)
                continue
            return [], [], 0
            
    return [], [], 0


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
_EBAY_SUFFIX = "&LH_Complete=1&LH_Sold=1&_sop=13&LH_PrefLoc=1&_ipg=240"


def scrape_ebay_comps(
    driver,          # kept for signature compatibility -- no longer used
    query: str,
    ai_val_low: float = 0,
    item_name: str = "",
    fallback_query: str | None = None,
    ebay_condition: str | None = None,
    inclusion_keywords: list[str] | None = None,
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

        top3_exclusion_str = "".join(f"+-{urllib.parse.quote_plus(kw)}" for kw in neg_keywords[:3])

        attempts = [
            f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{exclusion_str}{_EBAY_SUFFIX}{floor_param}{condition_param}",
            f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{exclusion_str}{_EBAY_SUFFIX}{floor_param}" if strict_floor else f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{exclusion_str}{_EBAY_SUFFIX}",
            f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{top3_exclusion_str}{_EBAY_SUFFIX}{floor_param}" if strict_floor else f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{top3_exclusion_str}{_EBAY_SUFFIX}",
            f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{_EBAY_SUFFIX}{floor_param}" if strict_floor else f"https://www.ebay.com/sch/i.html?_nkw={base_nkw}{_EBAY_SUFFIX}",
        ]
        labels = [
            "exclusions+floor+condition",
            "exclusions+floor (no condition)" if strict_floor else "exclusions only (no condition)",
            "top-3 exclusions+floor" if strict_floor else "top-3 exclusions",
            "bare query+floor" if strict_floor else "bare query",
        ]

        best_prices: list[float] = []
        best_links: list[str]   = []
        best_url: str         = attempts[0]
        best_total_count: int = 0

        for i, (url, label) in enumerate(zip(attempts, labels)):
            if i < 3:
                # Top strict attempts
                prices, comp_links, total_cnt = _fetch_prices_from_url(url, cleaned_query, inclusion_keywords=inclusion_keywords)
            else:
                # Looser bare attempt
                prices, comp_links, total_cnt = _fetch_prices_from_url(url, fallback_query or cleaned_query, inclusion_keywords=None)
            
            if prices:
                best_prices = prices
                best_links = comp_links
                best_url = url
                best_total_count = total_cnt

                # Prioritize strict attempts: if strict attempt 1 or 2 returns results, keep them!
                # Do NOT cascade down to loose bare queries that overwrite accurate results with broad generic junk.
                if len(prices) >= 1 and i <= 1:
                    if label != "exclusions+floor+condition":
                        print(f"  [{item_name}] fallback -> {label}")
                    break
                if len(prices) >= 3:
                    if label != "exclusions+floor+condition":
                        print(f"  [{item_name}] fallback -> {label}")
                    break
            print(f"  [{item_name}] {len(prices)} results with {label} - trying next")

        prices = best_prices
        comp_links = best_links
        used_url = best_url
        final_sold_count = max(len(prices), best_total_count)

        # Extract exact query string used from the URL
        import urllib.parse
        parsed = urllib.parse.urlparse(used_url)
        exact_query = urllib.parse.parse_qs(parsed.query).get('_nkw', [cleaned_query])[0]

        # --- Active Listings Scraping ---
        active_low = "N/A"
        active_high = "N/A"
        active_count = 0
        try:
            active_url = used_url.replace("&LH_Complete=1&LH_Sold=1", "")
            active_prices, _, active_tot_cnt = _fetch_prices_from_url(active_url, cleaned_query, max_retries=1, inclusion_keywords=inclusion_keywords)
            if active_prices:
                active_count = max(len(active_prices), active_tot_cnt)
                active_low = f"${min(active_prices):.0f}"
                active_high = f"${max(active_prices):.0f}"
        except Exception:
            pass

        if not prices:
            if fallback_query:
                print(f"  [{item_name}] 0 results -> trying fallback query: '{fallback_query}'")
                res = scrape_ebay_comps(
                    driver,
                    fallback_query,
                    ai_val_low=ai_val_low,
                    item_name=item_name,
                    fallback_query=None,
                    ebay_condition=ebay_condition,
                    inclusion_keywords=None,
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
                "count": 0, "active_low": active_low, "active_high": active_high, "active_count": active_count,
                "link": used_url, "links": [],
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
            "count":        final_sold_count,
            "active_low":   active_low,
            "active_high":  active_high,
            "active_count": active_count,
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
