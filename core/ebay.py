"""
ebay.py
=======
eBay sold-listings scraper for AISaleAnalyst.

Strategy, in plain terms
------------------------
1. Try a fast, browser-less request via ``curl_cffi`` (Chrome TLS
   impersonation). This works most of the time and is cheap.
2. If eBay serves an anti-bot block (PerimeterX interstitial or an
   hCaptcha), fall back to a real, visible Chrome browser
   (``undetected_chromedriver``). If it's just the plain interstitial, it
   usually clears itself in a few seconds. If it's an hCaptcha, the script
   waits for YOU to solve it by hand in that window -- nothing here
   auto-solves captchas.
3. Once the browser has cleared a block, we just keep using that same
   browser for every request for the rest of the run (curl_cffi's cookies
   don't transfer reliably once eBay has escalated to hCaptcha, so there's
   no point trying to hand things back).

Everything is serialized behind a single lock (`_ebay_lock`), because we
only ever want one eBay request in flight at a time -- that's what keeps
this simple: no separate semaphore, no circuit-breaker class, no
event-based "other threads wait here" bookkeeping. One lock does all of
it, including holding through a manual captcha solve, which is exactly
the behavior we want (nothing else hits eBay while you're solving it).

Public API
----------
scrape_ebay_comps(query, ai_val_low, item_name, ...)
    Scrape eBay completed/sold listings for ``query`` and return a comps
    summary dict.  Uses a progressive fallback so results are returned
    even when strict filters over-restrict.

should_filter_by_title(title, query)
    Post-filter: return True if a listing title contains known parts /
    accessory words that are not part of the search query itself.
"""

import os
import re
import time
import random
import threading

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from .config import AI_PROVIDER, EBAY_DELAY, fix_and_parse_json

if AI_PROVIDER == "openai":
    from .config import openai_client
else:
    from .config import gemini_client

# ---------------------------------------------------------------------------
# Single lock for everything eBay-related
# ---------------------------------------------------------------------------
# Only one eBay request is ever allowed in flight, from any thread. This
# lock enforces that AND doubles as the throttle gate AND doubles as the
# "only one thread solves a block at a time" gate -- one primitive, three
# jobs, instead of a semaphore + rate-limit lock + circuit-breaker Event.
_ebay_lock = threading.Lock()

_last_request_time: float = 0.0
_MIN_REQUEST_INTERVAL: float = 4.0  # seconds between any two eBay requests


def _throttle() -> None:
    """Sleep until at least _MIN_REQUEST_INTERVAL seconds have passed since
    the last eBay request. Must be called while holding `_ebay_lock`."""
    global _last_request_time
    now = time.time()
    wait = (_last_request_time + _MIN_REQUEST_INTERVAL) - now
    if wait > 0:
        time.sleep(wait)
    _last_request_time = time.time()


# ---------------------------------------------------------------------------
# curl_cffi session (fast path)
# ---------------------------------------------------------------------------

_session: cffi_requests.Session | None = None

_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Phrases that indicate eBay served an anti-bot interstitial instead of real
# results.
_BLOCK_PHRASES = (
    "captcha",
    "hcaptcha",
    "security measure",
    "pardon our interruption",
    "perimeterx",
    "access to this page has been denied",
)


def _get_session() -> cffi_requests.Session:
    """Return the shared curl_cffi session, creating and warming it up on
    first call. Must be called while holding `_ebay_lock`."""
    global _session
    if _session is not None:
        return _session

    print("  [eBay] Warming up HTTP session...")
    sess = cffi_requests.Session(impersonate="chrome124")
    try:
        _throttle()
        sess.get("https://www.ebay.com/", headers=_REQUEST_HEADERS, timeout=15)
        print("  [eBay] Session ready.")
    except Exception as exc:
        print(f"  [eBay] Warm-up warning: {exc}")

    _session = sess
    return _session


def close_ebay_session() -> None:
    global _session
    if _session is not None:
        try:
            _session.close()
        except Exception:
            pass
        _session = None


# ---------------------------------------------------------------------------
# Selenium fallback (used once curl_cffi gets blocked)
# ---------------------------------------------------------------------------

_SELENIUM_PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".ebay_scraper_chrome_profile")
_SELENIUM_WAIT_TIMEOUT = 25.0     # seconds to wait for a plain interstitial to auto-clear
_SELENIUM_HEADLESS = False        # must stay False -- you need to see/click any hCaptcha
_MANUAL_CAPTCHA_TIMEOUT = 600.0   # seconds to wait for you to manually solve an hCaptcha

_selenium_driver = None

#: Once True, every fetch uses the browser instead of curl_cffi for the
#: rest of the run (curl_cffi doesn't stay trusted once eBay has escalated
#: to a real hCaptcha, so there's no point switching back).
_use_selenium = False

#: Tracks whether the Selenium browser has visited plain ebay.com yet.
#: Jumping straight from a brand-new/relaunched browser session into a
#: deep, filtered search URL (LH_Complete, LH_Sold, exclusions, etc.) with
#: no normal browsing history looks bot-like to eBay and gets redirected
#: to a "Sign in or Register" wall -- even though the exact same URL opens
#: fine if you paste it in after the browser already has some history.
#: Visiting the homepage first mimics a person landing there before
#: searching, and avoids the wall.
_selenium_warmed_up = False


def _page_has_hcaptcha(driver) -> bool:
    try:
        if driver.find_elements("css selector", "iframe[src*='hcaptcha.com'], div.h-captcha, #h-captcha"):
            return True
        if "hcaptcha" in (driver.page_source or "").lower():
            return True
    except Exception:
        pass
    return False


def _page_is_signin_wall(driver) -> bool:
    title = (driver.title or "").lower()
    return "sign in" in title or "register" in title or "/signin" in (driver.current_url or "").lower()


def _driver_is_alive(driver) -> bool:
    """A crashed/closed Chrome leaves the Python object intact but every
    call into it fails -- this is a cheap way to detect that."""
    try:
        _ = driver.title
        return True
    except Exception:
        return False


def _get_selenium_driver():
    """Return the shared, persistent (visible) Chrome driver, launching it
    (or relaunching it, if the previous one crashed) as needed. Must be
    called while holding `_ebay_lock`."""
    global _selenium_driver, _selenium_warmed_up

    if _selenium_driver is not None and _driver_is_alive(_selenium_driver):
        return _selenium_driver

    if _selenium_driver is not None:
        print("  [eBay/Selenium] Previous Chrome instance isn't responding -- relaunching.")
        try:
            _selenium_driver.quit()
        except Exception:
            pass
        _selenium_driver = None
        _selenium_warmed_up = False  # fresh process -- needs a homepage visit again

    import undetected_chromedriver as uc
    import chrome_version

    options = uc.ChromeOptions()
    # Reusing the same profile dir means a relaunch keeps any cookies /
    # trust eBay already granted the previous instance.
    options.add_argument(f"--user-data-dir={_SELENIUM_PROFILE_DIR}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1280,900")
    if _SELENIUM_HEADLESS:
        options.add_argument("--headless=new")

    print("  [eBay/Selenium] Launching Chrome...")
    _selenium_driver = uc.Chrome(
        options=options,
        version_main=int(chrome_version.get_chrome_version().split(".")[0]),
    )
    return _selenium_driver


def close_selenium_driver() -> None:
    global _selenium_driver
    if _selenium_driver is not None:
        try:
            _selenium_driver.quit()
        except Exception:
            pass
        _selenium_driver = None


def _selenium_visit_homepage(driver) -> None:
    """Land on plain ebay.com first, like a person would, before searching."""
    global _selenium_warmed_up
    print("  [eBay/Selenium] Visiting ebay.com first (avoids the sign-in wall on deep links)...")
    driver.get("https://www.ebay.com/")
    time.sleep(random.uniform(1.5, 3.0))
    _selenium_warmed_up = True


def _selenium_fetch(url: str) -> str:
    """
    Navigate to *url* with the persistent browser and return its final
    page source, waiting out any interstitial or (manually solved)
    hCaptcha first. Must be called while holding `_ebay_lock`.
    """
    global _selenium_warmed_up

    driver = _get_selenium_driver()

    if not _selenium_warmed_up:
        _selenium_visit_homepage(driver)

    driver.get(url)

    # Sign-in wall: eBay increasingly requires an actual logged-in session
    # to view sold/completed listings, not just a "less fresh" browser. A
    # single homepage re-warm can clear the "session looks too new" case,
    # but if it's still there after that, it genuinely wants you signed
    # in -- wait for you to log into your own eBay account in this same
    # window. Because the browser profile persists (`_SELENIUM_PROFILE_DIR`),
    # you only need to do this once; future runs stay logged in.
    if _page_is_signin_wall(driver):
        print("  [eBay/Selenium] Hit the sign-in wall -- re-warming via the homepage and retrying once.")
        _selenium_visit_homepage(driver)
        driver.get(url)

    if _page_is_signin_wall(driver):
        print(f"  [eBay/Selenium] 🔑 eBay wants you signed in to view sold listings. Please log "
              f"into your eBay account in the open Chrome window -- this only needs doing once, "
              f"the browser profile remembers it after that. Waiting up to "
              f"{_MANUAL_CAPTCHA_TIMEOUT/60:.0f} minutes...")
        manual_waited = 0.0
        while manual_waited < _MANUAL_CAPTCHA_TIMEOUT:
            time.sleep(3.0)
            manual_waited += 3.0
            if not _page_is_signin_wall(driver):
                print(f"  [eBay/Selenium] ✅ Signed in after ~{manual_waited:.0f}s -- reloading the page.")
                driver.get(url)
                break
        else:
            print("  [eBay/Selenium] Timed out waiting for sign-in.")

    # Plain interstitial: give it a short while to clear on its own.
    waited = 0.0
    while waited < _SELENIUM_WAIT_TIMEOUT:
        title = (driver.title or "").lower()
        if "pardon our interruption" not in title:
            break
        if _page_has_hcaptcha(driver):
            break
        time.sleep(1.5)
        waited += 1.5

    # hCaptcha: wait for a human to solve it. No auto-solving.
    if _page_has_hcaptcha(driver):
        print(f"  [eBay/Selenium] 🧩 hCaptcha detected -- please solve it in the open "
              f"Chrome window. Waiting up to {_MANUAL_CAPTCHA_TIMEOUT/60:.0f} minutes...")
        manual_waited = 0.0
        while manual_waited < _MANUAL_CAPTCHA_TIMEOUT:
            time.sleep(3.0)
            manual_waited += 3.0
            if not _page_has_hcaptcha(driver) and "pardon our interruption" not in (driver.title or "").lower():
                print(f"  [eBay/Selenium] ✅ Cleared after ~{manual_waited:.0f}s.")
                break
        else:
            print(f"  [eBay/Selenium] Timed out waiting for the hCaptcha to be solved.")

    return driver.page_source


# ---------------------------------------------------------------------------
# The one fetch function everything else calls
# ---------------------------------------------------------------------------

def _fetch_page(url: str) -> str:
    """
    Fetch *url* and return its HTML. Tries curl_cffi first; if that comes
    back blocked, switches to the Selenium browser (solving/waiting out
    the block first) and stays on Selenium for the rest of the run.

    Everything is serialized under `_ebay_lock`, including a manual
    captcha wait -- that's intentional, we only want one thing touching
    eBay at a time regardless of why.
    """
    global _use_selenium

    with _ebay_lock:
        _throttle()

        if _use_selenium:
            return _selenium_fetch(url)

        # --- curl_cffi attempt ---
        session = _get_session()
        try:
            resp = session.get(
                url,
                headers={**_REQUEST_HEADERS, "Referer": "https://www.ebay.com/"},
                timeout=20,
            )
            text_lower = resp.text.lower()
            blocked = any(p in text_lower for p in _BLOCK_PHRASES) or resp.status_code != 200
            if not blocked:
                return resp.text
            print("  [eBay] 🚨 Blocked -- falling back to the browser for this and all further requests.")
        except Exception as exc:
            print(f"  [eBay] Request error ({exc}) -- falling back to the browser.")

        # --- Escalate to the browser, right here, same lock held ---
        _use_selenium = True
        return _selenium_fetch(url)


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
# HTML parsing
# ---------------------------------------------------------------------------

def _parse_prices_from_html(html: str, query: str, inclusion_keywords: list[str] | None = None) -> tuple[list[float], list[str], int]:
    """
    Parse sold prices, listing links, and total result count from an eBay search results HTML page.

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


def _fetch_prices_from_url(search_url: str, query: str, max_retries: int = 3, inclusion_keywords: list[str] | None = None) -> tuple[list[float], list[str], int]:
    """
    Fetch *search_url* and extract sold prices, retrying a small, fixed
    number of times on genuine errors or an unrecognized page. Blocking is
    handled transparently inside `_fetch_page` (curl_cffi -> browser
    fallback), so this function doesn't need to know or care which path
    actually served the page.
    """
    for attempt in range(max_retries):
        try:
            html = _fetch_page(search_url)
        except Exception as exc:
            print(f"  [eBay] Fetch error: {exc}")
            if attempt < max_retries - 1:
                time.sleep(5)
                continue
            return [], [], 0

        try:
            prices, links, total_cnt = _parse_prices_from_html(html, query, inclusion_keywords=inclusion_keywords)
        except ValueError as exc:
            print(f"  [eBay] Unrecognized page ({exc}) -- retrying")
            if attempt < max_retries - 1:
                time.sleep(5)
                continue
            return [], [], 0

        if not prices:
            with open("ebay_debug_0_results.html", "w", encoding="utf-8") as f:
                f.write(html)

        return prices, links, total_cnt

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

    The ``driver`` parameter is accepted but ignored -- scraping now
    manages its own HTTP/browser fetching internally (see `_fetch_page`).

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

            print(f"  [{item_name}] 0 genuine results after all fallbacks.")
            return {
                "low": "N/A", "mean": "N/A", "high": "N/A",
                "count": 0, "active_low": active_low, "active_high": active_high, "active_count": active_count,
                "link": used_url, "links": [],
                "fallback_used": False, "query_used": exact_query,
            }

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