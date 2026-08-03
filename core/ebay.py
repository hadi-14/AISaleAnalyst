"""
ebay.py
=======
eBay sold-listings scraper for AISaleAnalyst.

Strategy, in plain terms
------------------------
Every request goes straight through a real Chrome browser
(``undetected_chromedriver``), not a bare HTTP request. There used to be a
faster ``curl_cffi``-based path tried first, with Selenium only as a
fallback when that got blocked -- it's been retired. In practice eBay was
blocking the curl_cffi path on effectively every request, so it wasn't
saving anything; it was just adding a doomed round-trip in front of every
single fetch before falling through to Selenium anyway. Going straight to
Selenium is both simpler and, empirically, no slower.

To avoid paying a full "cold" trust-building cost on every browser launch,
session cookies are persisted to disk after each fetch (see
``_save_selenium_cookies_to_disk`` / ``_load_cookies_from_disk``) and
re-applied to a freshly launched browser before its first real request.
The Chrome profile directory (``_SELENIUM_PROFILE_DIR``) is also reused
run to run, which is what lets a one-time manual sign-in "stick" for
future runs, headless or not.

If eBay serves an anti-bot block (PerimeterX interstitial or an
hCaptcha):
- If the browser is running visibly (the default, CLI use), the script
  waits for YOU to solve it by hand in that window -- nothing here
  auto-solves captchas.
- If the browser is running headless (see "Headless mode" below), there
  is nobody to solve it and that request will simply come back blocked.

Headless mode
-------------
By default the browser launches *visible*, since solving an hCaptcha or a
sign-in/2FA flow requires a human looking at the screen. When
AISaleAnalyst is driven from ``webapp.py`` (no desktop/display attached,
e.g. a server), set the environment variable ``EBAY_HEADLESS=1`` before
the run starts and the browser will launch headless instead. ``webapp.py``
does this automatically.

**Trade-off:** in headless mode, any hCaptcha or eBay sign-in wall cannot
be solved by a human. Those requests will simply fail (return 0 comps for
that item) rather than pausing for manual input.

Concurrency
-----------
There is exactly **one** browser instance for the whole process, and every
fetch runs under `_state.lock` -- fully exclusive, including holding the
lock through a manual captcha/sign-in solve (when running visibly), which
is exactly the behavior we want (nothing else touches eBay while you're
solving it). This means eBay requests from multiple worker threads now
serialize rather than overlapping -- there's no longer a pool of several
requests genuinely in flight at once. Keep ``EBAY_WORKERS`` low (1-2);
raising it no longer buys real eBay-side parallelism, it just queues more
threads up waiting for the same lock.

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
import json
import random
import threading

from bs4 import BeautifulSoup

from .config import AI_PROVIDER, EBAY_DELAY, fix_and_parse_json

if AI_PROVIDER == "openai":
    from .config import openai_client
else:
    from .config import gemini_client


# ---------------------------------------------------------------------------
# Shared scraper state
# ---------------------------------------------------------------------------
# `lock` guards the single shared browser -- every fetch is fully
# exclusive while it's active (see module docstring's "Concurrency"
# section).

class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_request_time: float = 0.0
        self.driver = None
        #: Whether the Selenium browser has visited plain ebay.com yet in
        #: this process. Jumping straight from a brand-new/relaunched
        #: browser session into a deep, filtered search URL (LH_Complete,
        #: LH_Sold, exclusions, etc.) with no normal browsing history looks
        #: bot-like to eBay and gets redirected to a sign-in wall -- even
        #: though the exact same URL opens fine once the browser has some
        #: history. Visiting the homepage first avoids that.
        self.selenium_warmed_up: bool = False


_state = _State()

#: Where a run's cookies get saved so the *next* run can start already
#: trusted, instead of needing to hit a block and rebuild trust again from
#: cold. Best-effort only -- if this file is missing, corrupt, or stale, we
#: just fall back to the normal cold-start path.
_COOKIE_CACHE_PATH = os.path.join(os.path.expanduser("~"), ".ebay_scraper_cookies.json")

#: Safety net for the cache file's overall age, on top of each cookie's
#: own `expires` field. Session-style cookies often have no `expires` of
#: their own (they're meant to die with the browser), so without this a
#: months-old cache could look "valid" forever. Chosen generously (most
#: session cookies won't actually survive this long) -- the per-cookie
#: expiry check below is what does the real filtering.
_COOKIE_CACHE_MAX_AGE: float = 12 * 3600

#: Minimum seconds between successive page fetches (jittered ±30%), so we
#: don't hammer eBay back-to-back. Since every fetch is already serialized
#: behind `_state.lock`, this is a straightforward pause between one
#: fetch finishing and the next one starting.
_MIN_REQUEST_INTERVAL: float = 1.2


def _stagger() -> None:
    """Pause briefly (with jitter) since the previous request, so
    consecutive fetches don't fire back-to-back."""
    now = time.time()
    interval = random.uniform(_MIN_REQUEST_INTERVAL * 0.7, _MIN_REQUEST_INTERVAL * 1.3)
    wait = (_state.last_request_time + interval) - now
    if wait > 0:
        time.sleep(wait)
    _state.last_request_time = time.time()


# ---------------------------------------------------------------------------
# Generic "wait for a human to finish something" helper
# ---------------------------------------------------------------------------
# Both the sign-in wall and the hCaptcha need the same shape of loop: poll
# every few seconds, up to a timeout, until some condition on the driver
# becomes true. One helper instead of two copies of the same while-loop.

def _wait_until(driver, condition_met, description: str, timeout: float, poll: float = 3.0) -> bool:
    """Poll every `poll` seconds until `condition_met(driver)` is True or
    `timeout` seconds have elapsed. Returns whether the condition was met."""
    print(f"  [eBay/Selenium] ⏳ {description} (waiting up to {timeout / 60:.0f} min)...")
    waited = 0.0
    while waited < timeout:
        time.sleep(poll)
        waited += poll
        try:
            met = condition_met(driver)
        except Exception:
            met = False
        if met:
            print(f"  [eBay/Selenium] ✅ Done after ~{waited:.0f}s.")
            return True
    print(f"  [eBay/Selenium] ⏱️ Timed out after {timeout / 60:.0f} min.")
    return False


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


def _load_cookies_from_disk() -> list[dict]:
    """Return cookies saved by a previous run, dropping anything already
    expired -- either its own `expires` timestamp, or the whole file being
    older than `_COOKIE_CACHE_MAX_AGE` (the safety net for cookies with no
    `expires` of their own). Best-effort: any read/parse failure just means
    we start cold, exactly like before this feature existed."""
    try:
        with open(_COOKIE_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    if time.time() - data.get("saved_at", 0) > _COOKIE_CACHE_MAX_AGE:
        return []

    now = time.time()
    return [
        c for c in data.get("cookies", [])
        if c.get("expires") is None or c["expires"] > now
    ]


def _save_selenium_cookies_to_disk(driver) -> None:
    """Persist the browser's current cookies to disk so the *next* run (or
    a relaunch within this run) can start already trusted. Best-effort --
    a failure here should never interrupt a scrape that's otherwise
    working fine."""
    try:
        cookies = driver.get_cookies()
    except Exception as exc:
        print(f"  [eBay] Could not read cookies from browser (non-fatal): {exc}")
        return
    if not cookies:
        return
    try:
        # Selenium cookie dicts use "expiry"; normalize to "expires" so the
        # cache format matches what _load_cookies_from_disk() expects.
        normalized = [
            {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain", ".ebay.com"),
                "path": c.get("path", "/"),
                "expires": c.get("expiry"),
            }
            for c in cookies
        ]
        with open(_COOKIE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"saved_at": time.time(), "cookies": normalized}, f)
    except Exception as exc:
        print(f"  [eBay] Cookie cache save failed (non-fatal): {exc}")


def close_ebay_session() -> None:
    """Persist cookies from the browser (if one is open) and quit it. Call
    this once at the end of a run."""
    if _state.driver is not None:
        _save_selenium_cookies_to_disk(_state.driver)
    close_selenium_driver()


# ---------------------------------------------------------------------------
# Selenium browser (the only fetch path)
# ---------------------------------------------------------------------------

_SELENIUM_PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".ebay_scraper_chrome_profile")
_SELENIUM_WAIT_TIMEOUT = 25.0     # seconds to wait for a plain interstitial to auto-clear
_MANUAL_CAPTCHA_TIMEOUT = 600.0   # seconds to wait for you to manually solve an hCaptcha (visible mode only)
_MANUAL_SIGNIN_TIMEOUT = 900.0    # seconds to wait for you to sign in (visible mode only; email + password + 2FA all take longer than a captcha)


def _is_headless() -> bool:
    """Whether the browser should launch headless.

    Controlled by the ``EBAY_HEADLESS`` environment variable (``"1"`` =
    headless, anything else/unset = visible). Defaults to visible (False)
    because that's the only mode where a human can solve an hCaptcha or
    complete a sign-in/2FA flow -- CLI use of main.py should almost always
    leave this unset. ``webapp.py`` sets ``EBAY_HEADLESS=1`` before running
    the pipeline, since a server has no display for a human to use anyway.

    Read dynamically (not cached at import time) so a caller like
    webapp.py can set the env var right before invoking the pipeline,
    without needing to reload this module.
    """
    return os.environ.get("EBAY_HEADLESS", "0") == "1"


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
    """True while eBay is anywhere inside its sign-in flow -- including the
    2FA / "verify it's you" step.

    This deliberately checks the URL host (`signin.ebay.com`) rather than
    the page title. The whole sign-in flow (email -> password -> 2FA code)
    stays on that host, but each step has a *different title* ("Sign in",
    "Enter password", "Verify it's you", ...). The old title-text check
    ("sign in" / "register") stopped matching as soon as you moved past
    the first screen, so the wait loop below thought you were already
    signed in the moment you reached the 2FA step -- and reloaded the
    target URL out from under you before you could enter the code. Keying
    off the host instead means we only consider you "done" once eBay
    actually redirects you back to ebay.com.
    """
    try:
        url = (driver.current_url or "").lower()
    except Exception:
        return False
    return "signin.ebay.com" in url


def _driver_is_alive(driver) -> bool:
    """A crashed/closed Chrome leaves the Python object intact but every
    call into it fails -- this is a cheap way to detect that."""
    try:
        _ = driver.title
        return True
    except Exception:
        return False


def _get_selenium_driver():
    """Return the shared, persistent Chrome driver, launching it (or
    relaunching it, if the previous one crashed) as needed. Must be called
    while holding `_state.lock`.

    Launches visible or headless depending on `_is_headless()` -- see that
    function's docstring for the trade-off."""
    if _state.driver is not None and _driver_is_alive(_state.driver):
        return _state.driver

    if _state.driver is not None:
        print("  [eBay/Selenium] Previous Chrome instance isn't responding -- relaunching.")
        try:
            _state.driver.quit()
        except Exception:
            pass
        _state.driver = None
        _state.selenium_warmed_up = False  # fresh process -- needs a homepage visit again

    import undetected_chromedriver as uc
    import chrome_version

    options = uc.ChromeOptions()
    # Reusing the same profile dir means a relaunch keeps any cookies /
    # trust eBay already granted the previous instance (including being
    # signed in).
    options.add_argument(f"--user-data-dir={_SELENIUM_PROFILE_DIR}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1280,900")

    headless = _is_headless()
    if headless:
        options.add_argument('--no-sandbox')
        options.add_argument("--headless=new")
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument("--disable-gpu")
        
        print(
            "  [eBay/Selenium] Launching Chrome in HEADLESS mode (EBAY_HEADLESS=1). "
            "Any hCaptcha or sign-in wall hit in this mode cannot be solved by a "
            "human and will fail instead of pausing."
        )
    else:
        print("  [eBay/Selenium] Launching Chrome (visible)...")

    _state.driver = uc.Chrome(
        options=options,
        version_main=int(chrome_version.get_chrome_version().split(".")[0]),
    )
    return _state.driver


def close_selenium_driver() -> None:
    if _state.driver is not None:
        try:
            _state.driver.quit()
        except Exception:
            pass
        _state.driver = None
    # A relaunch is a fresh browser process even if it reuses the same
    # profile dir -- it needs the homepage visit (and cached-cookie
    # injection) again before a deep search URL, same as a first launch.
    _state.selenium_warmed_up = False


def _apply_cached_cookies_to_selenium(driver) -> None:
    """Inject the same disk-cached cookies into the browser, on top of
    whatever its persistent Chrome profile already carries. Gives a
    freshly launched/relaunched driver a head start instead of relying
    solely on the profile dir -- useful the first time a profile is
    created, or if it's ever wiped. Must be called after the driver has
    already loaded an ebay.com page at least once (Selenium can only set
    cookies for the domain of the page currently loaded)."""
    cached = _load_cookies_from_disk()
    if not cached:
        return
    applied = 0
    for c in cached:
        try:
            cookie = {"name": c["name"], "value": c["value"], "path": c.get("path", "/")}
            domain = c.get("domain", "")
            if domain:
                cookie["domain"] = domain.lstrip(".")
            driver.add_cookie(cookie)
            applied += 1
        except Exception:
            continue
    if applied:
        print(f"  [eBay/Selenium] Applied {applied} cached cookie(s) to the browser.")


def _selenium_visit_homepage(driver) -> None:
    """Land on plain ebay.com first, like a person would, before searching."""
    print("  [eBay/Selenium] Visiting ebay.com first (avoids the sign-in wall on deep links)...")
    driver.get("https://www.ebay.com/")
    time.sleep(random.uniform(1.5, 3.0))
    _apply_cached_cookies_to_selenium(driver)
    _state.selenium_warmed_up = True


def _selenium_fetch(url: str) -> str:
    """
    Navigate to *url* with the persistent browser and return its final
    page source, waiting out any interstitial, sign-in flow (including
    2FA), or hCaptcha first. In visible mode, hCaptcha/sign-in waits are
    for a human to act; in headless mode (`_is_headless()`), there is no
    human, so those waits are skipped and the request returns whatever
    blocked/incomplete page eBay served. Must be called while holding
    `_state.lock`.
    """
    driver = _get_selenium_driver()
    headless = _is_headless()

    if not _state.selenium_warmed_up:
        _selenium_visit_homepage(driver)

    driver.get(url)

    # Sign-in wall: eBay increasingly requires an actual logged-in session
    # to view sold/completed listings, not just a "less fresh" browser. A
    # single homepage re-warm can clear the "session looks too new" case,
    # but if it's still there after that, it genuinely wants you signed
    # in. Because the browser profile persists (`_SELENIUM_PROFILE_DIR`),
    # you only need to sign in once (in visible mode) -- future runs, even
    # headless ones, stay logged in against the same profile dir.
    if _page_is_signin_wall(driver):
        print("  [eBay/Selenium] Hit the sign-in wall -- re-warming via the homepage and retrying once.")
        _selenium_visit_homepage(driver)
        driver.get(url)

    if _page_is_signin_wall(driver):
        if headless:
            print(
                "  [eBay/Selenium] 🔑 eBay wants a signed-in session for sold listings, but "
                "Chrome is running headless -- nobody can sign in here. Skipping the wait; "
                "this request will likely come back with 0 comps. Sign in once from a "
                "visible (non-headless) run against the same Chrome profile to fix this "
                "for future headless runs too."
            )
        else:
            print(
                "  [eBay/Selenium] 🔑 eBay wants you signed in to view sold listings. "
                "Please sign in (including any 2FA/verification step) in the open Chrome "
                "window -- this only needs doing once, the browser profile remembers it "
                "after that."
            )
            signed_in = _wait_until(
                driver,
                condition_met=lambda d: not _page_is_signin_wall(d),
                description="Waiting for sign-in (email, password, and any 2FA code) to complete",
                timeout=_MANUAL_SIGNIN_TIMEOUT,
            )
            if signed_in:
                # eBay usually lands you on the homepage or your account page
                # after sign-in finishes, not the search URL we actually
                # wanted -- go get it now that we're authenticated.
                driver.get(url)

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

    # hCaptcha: in visible mode, wait for a human to solve it (no
    # auto-solving). In headless mode, there's nobody to solve it, so
    # don't burn the full timeout waiting -- just log and move on.
    if _page_has_hcaptcha(driver):
        if headless:
            print(
                "  [eBay/Selenium] 🧩 hCaptcha detected, but Chrome is running headless -- "
                "cannot be solved automatically. This request will likely come back blocked."
            )
        else:
            print("  [eBay/Selenium] 🧩 hCaptcha detected -- please solve it in the open Chrome window.")
            _wait_until(
                driver,
                condition_met=lambda d: not _page_has_hcaptcha(d) and "pardon our interruption" not in (d.title or "").lower(),
                description="Waiting for the hCaptcha to be solved",
                timeout=_MANUAL_CAPTCHA_TIMEOUT,
            )

    return driver.page_source


def _fetch_page(url: str) -> str:
    """Fetch *url* via the shared Selenium browser -- the only fetch path
    now (see module docstring). Runs fully under `_state.lock`, so
    concurrent callers serialize rather than overlapping; see the
    "Concurrency" section of the module docstring."""
    with _state.lock:
        _stagger()
        html = _selenium_fetch(url)
        # Persist cookies after every fetch, not just at shutdown -- if the
        # process dies mid-run, whatever trust was earned this request is
        # still saved for next time. Cheap enough to not worry about doing
        # it every request.
        driver = _state.driver
        if driver is not None:
            _save_selenium_cookies_to_disk(driver)
        return html


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
    number of times on genuine errors or an unrecognized page. Blocking
    (interstitials, captchas, sign-in walls) is handled transparently
    inside `_fetch_page` / `_selenium_fetch`, so this function doesn't need
    to know or care about any of that.
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
    manages its own shared Selenium browser internally (see `_fetch_page`).

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