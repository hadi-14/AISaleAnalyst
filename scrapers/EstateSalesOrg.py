"""
EstateSales.org Image Downloader
- Uses undetected-chromedriver to bypass bot detection
- Solves Amazon WAF CAPTCHA via 2captcha API (AmazonTaskProxyless)
- Paginates /gallery?page=N (100 images per page)
- Dismisses subscribe popup on every page
- Downloads all images concurrently

Usage: python EstateSalesOrg.py <listing_url> [output_dir] [--workers N]
Example:
  python EstateSalesOrg.py https://estatesales.org/estate-sales/ca/hanford-/93230/estate-sale-in-hanford-ca-2447915
  python EstateSalesOrg.py https://estatesales.org/... ./images --workers 20

Env vars:
  TWOCAPTCHA_API_KEY  — your 2captcha API key

Importable:
  from EstateSalesOrg import ProcessSaleUrl
  ProcessSaleUrl("https://estatesales.org/...", output_dir="./images", workers=16)
"""

import os
import re
import sys
import time
import json
import argparse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ── Patch undetected_chromedriver to suppress WinError 6 on shutdown ─────────
def _safe_uc_del(self):
    try:
        self.quit()
    except Exception:
        pass

try:
    uc.Chrome.__del__ = _safe_uc_del
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv()

try:
    import chrome_version
    CHROME_MAIN = int(chrome_version.get_chrome_version().split(".")[0])
except Exception:
    CHROME_MAIN = None  # let uc auto-detect


# ── AWS WAF param extraction ───────────────────────────────────────────────────

def extract_aws_waf_params(driver) -> dict | None:
    """
    Extract AWS WAF CAPTCHA parameters from the live browser.

    challenge.js and captcha.js are loaded dynamically by the WAF bootstrap
    script — they never appear in raw HTML — so we capture them from Chrome's
    network performance log first, then fall back to DOM inspection and regex.

    Returns dict with: websiteKey, iv, context, challengeScript, captchaScript
    or None if this doesn't look like an AWS WAF page.
    """

    # ── 1. Script URLs from Chrome network log ────────────────────────────
    challenge_script = None
    captcha_script   = None

    try:
        logs = driver.get_log("performance")
        for entry in logs:
            msg = json.loads(entry["message"])["message"]
            if msg.get("method") == "Network.requestWillBeSent":
                url = msg.get("params", {}).get("request", {}).get("url", "")
                if ".awswaf.com" in url:
                    if "challenge.js" in url and not challenge_script:
                        challenge_script = url
                        print(f"  [CAPTCHA] Found challenge.js via network log")
                    elif "captcha.js" in url and not captcha_script:
                        captcha_script = url
                        print(f"  [CAPTCHA] Found captcha.js via network log")
    except Exception as e:
        print(f"  [CAPTCHA] Perf log unavailable: {e}")

    # ── 2. Script URLs from DOM <script src="..."> tags ───────────────────
    if not challenge_script or not captcha_script:
        try:
            found = driver.execute_script("""
                var urls = {challenge: null, captcha: null};
                document.querySelectorAll('script[src]').forEach(function(s) {
                    var u = s.src || '';
                    if (u.indexOf('.awswaf.com') !== -1) {
                        if (u.indexOf('challenge.js') !== -1) urls.challenge = u;
                        if (u.indexOf('captcha.js')   !== -1) urls.captcha   = u;
                    }
                });
                return urls;
            """)
            if found:
                if not challenge_script and found.get("challenge"):
                    challenge_script = found["challenge"]
                    print(f"  [CAPTCHA] Found challenge.js via DOM")
                if not captcha_script and found.get("captcha"):
                    captcha_script = found["captcha"]
                    print(f"  [CAPTCHA] Found captcha.js via DOM")
        except Exception as e:
            print(f"  [CAPTCHA] DOM script scan failed: {e}")

    # ── 3. Script URLs from raw page source (last resort) ─────────────────
    src = driver.page_source
    if not challenge_script:
        m = re.search(
            r'(https://[^\s"\'<>]*\.awswaf\.com[^\s"\'<>]*challenge\.js[^\s"\'<>]*)', src
        )
        if m:
            challenge_script = m.group(1)
            print(f"  [CAPTCHA] Found challenge.js via regex")
    if not captcha_script:
        m = re.search(
            r'(https://[^\s"\'<>]*\.awswaf\.com[^\s"\'<>]*captcha\.js[^\s"\'<>]*)', src
        )
        if m:
            captcha_script = m.group(1)
            print(f"  [CAPTCHA] Found captcha.js via regex")

    # ── 4. key / iv / context via inline script inspection (JS) ──────────
    params = None
    try:
        params = driver.execute_script(r"""
            try {
                // Only scan inline scripts (not external — those won't have WAF config)
                var scripts = document.querySelectorAll('script:not([src])');
                for (var i = 0; i < scripts.length; i++) {
                    var t = scripts[i].textContent || '';
                    var k   = t.match(/"key"\s*:\s*"([A-Za-z0-9+\/=]{20,})"/);
                    var iv  = t.match(/"iv"\s*:\s*"([^"]{8,})"/);
                    var ctx = t.match(/"context"\s*:\s*"([A-Za-z0-9+\/=]{20,})"/);
                    if (k && iv && ctx) {
                        return { websiteKey: k[1], iv: iv[1], context: ctx[1] };
                    }
                }
                return null;
            } catch(e) { return null; }
        """)
    except Exception as e:
        print(f"  [CAPTCHA] JS param extraction failed: {e}")

    # ── 5. key / iv / context via regex on page source ────────────────────
    if not params:
        key_match = re.search(r'"key"\s*:\s*"([A-Za-z0-9+/=]{20,})"', src)
        iv_match  = re.search(r'"iv"\s*:\s*"([^"]{8,})"', src)
        ctx_match = re.search(r'"context"\s*:\s*"([A-Za-z0-9+/=]{20,})"', src)
        if key_match and iv_match and ctx_match:
            params = {
                "websiteKey": key_match.group(1),
                "iv":         iv_match.group(1),
                "context":    ctx_match.group(1),
            }
            print(f"  [CAPTCHA] key/iv/context extracted via regex fallback")

    if not params:
        return None

    params["challengeScript"] = challenge_script
    params["captchaScript"]   = captcha_script
    return params


# ── 2captcha solver ────────────────────────────────────────────────────────────

def _inject_waf_token(driver, voucher: str, token: str, target_url: str):
    """
    Inject the WAF solution cookies and trigger a complete fresh navigation.
    """
    host_domain = re.search(r'https?://([^/]+)', driver.current_url).group(1)
    parts = host_domain.split('.')
    root_domain = '.'.join(parts[-2:]) if len(parts) >= 2 else host_domain

    val_to_use = token or voucher
    if val_to_use:
        try:
            driver.execute_script(f"""
                try {{
                    if (window.awsWafCaptcha && typeof window.awsWafCaptcha.setToken === 'function') {{
                        window.awsWafCaptcha.setToken('{val_to_use}');
                    }}
                }} catch(e) {{}}
            """)
        except Exception:
            pass

    domains_to_set = list(set([host_domain, root_domain, f".{root_domain}"]))

    for dom in domains_to_set:
        cookie_attrs = {"domain": dom, "path": "/", "secure": True, "samesite": "Lax"}
        if voucher:
            try:
                driver.add_cookie({"name": "captcha_voucher", "value": voucher, **cookie_attrs})
            except Exception:
                pass
            try:
                driver.execute_script(f"document.cookie = 'captcha_voucher={voucher}; path=/; domain={dom}; SameSite=Lax; Secure';")
            except Exception:
                pass
        if token:
            try:
                driver.add_cookie({"name": "aws-waf-token", "value": token, **cookie_attrs})
            except Exception:
                pass
            try:
                driver.execute_script(f"document.cookie = 'aws-waf-token={token}; path=/; domain={dom}; SameSite=Lax; Secure';")
            except Exception:
                pass

    try:
        origin_url = f"https://{host_domain}/"
        if voucher:
            driver.execute_cdp_cmd("Network.setCookie", {
                "name": "captcha_voucher", "value": voucher, "url": origin_url, "path": "/", "secure": True
            })
            driver.execute_cdp_cmd("Network.setCookie", {
                "name": "captcha_voucher", "value": voucher, "domain": f".{root_domain}", "path": "/", "secure": True
            })
        if token:
            driver.execute_cdp_cmd("Network.setCookie", {
                "name": "aws-waf-token", "value": token, "url": origin_url, "path": "/", "secure": True
            })
            driver.execute_cdp_cmd("Network.setCookie", {
                "name": "aws-waf-token", "value": token, "domain": f".{root_domain}", "path": "/", "secure": True
            })
    except Exception as e:
        print(f"  [CAPTCHA] CDP cookie injection note: {e}")

    time.sleep(0.5)
    # Perform actual GET navigation + refresh to force Chrome to issue new HTTP requests with cookies
    driver.get(target_url)
    time.sleep(1.5)
    driver.refresh()
    time.sleep(3)


def is_waf_blocking(driver) -> bool:
    """
    Return True if an active AWS WAF CAPTCHA challenge is currently blocking the page.
    """
    try:
        title = driver.title.lower()
        if any(w in title for w in ["captcha", "security check", "verify", "human", "challenge"]):
            return True

        src = driver.page_source.lower()
        if any(w in src for w in ["awswaf-captcha", "verify you are human", "solve the captcha", "unusual traffic"]):
            return True

        # Check if WAF iframe or modal is present
        if driver.find_elements(By.XPATH, "//iframe[contains(@src, 'awswaf')] | //div[contains(@id, 'aws-waf')] | //div[contains(@id, 'captcha')]"):
            return True
    except Exception:
        pass
    return False


def solve_captcha_2captcha(driver, api_key: str, _retry: bool = False) -> bool:
    """
    Detect and solve AWS WAF CAPTCHA via 2captcha AmazonTaskProxyless.
    """
    if not is_waf_blocking(driver):
        return False

    print("  [CAPTCHA] AWS WAF Challenge Detected!")

    use_2captcha = os.getenv("USE_2CAPTCHA", "true").lower() in ("true", "1", "yes", "on")
    if not api_key or not use_2captcha:
        print("  [CAPTCHA] Automatic 2captcha solving disabled (USE_2CAPTCHA=false or key unset).")
        print("  [CAPTCHA] Please solve the CAPTCHA manually in the browser window, then press Enter here to continue...")
        input()
        return True

    target_url = driver.current_url

    if _retry:
        driver.get(target_url)
        time.sleep(3)
        if not is_waf_blocking(driver):
            print("  [CAPTCHA] WAF cleared after reload!")
            return True

    params = extract_aws_waf_params(driver)

    if not params:
        print("  [CAPTCHA] Could not extract AWS WAF params — solve manually then press Enter...")
        input()
        return True

    user_agent = driver.execute_script("return navigator.userAgent")

    print(f"  [CAPTCHA] websiteKey : {params['websiteKey'][:55]}...")
    print(f"  [CAPTCHA] iv         : {params['iv']}")
    print(f"  [CAPTCHA] context    : {params['context'][:45]}...")
    print(f"  [CAPTCHA] userAgent  : {user_agent[:60]}...")

    task = {
        "type":       "AmazonTaskProxyless",
        "websiteURL": target_url,
        "websiteKey": params["websiteKey"],
        "iv":         params["iv"],
        "context":    params["context"],
        "userAgent":  user_agent,
    }
    if params["challengeScript"]:
        task["challengeScript"] = params["challengeScript"]
    if params["captchaScript"]:
        task["captchaScript"] = params["captchaScript"]

    submit_time = time.time()
    try:
        create_resp = requests.post(
            "https://api.2captcha.com/createTask",
            json={"clientKey": api_key, "task": task},
            timeout=15,
        ).json()
    except Exception as e:
        print(f"  [CAPTCHA] createTask failed: {e} — solve manually then press Enter...")
        input()
        return True

    if create_resp.get("errorId") != 0:
        print(f"  [CAPTCHA] createTask error: {create_resp} — solve manually then press Enter...")
        input()
        return True

    task_id = create_resp["taskId"]
    print(f"  [CAPTCHA] Task {task_id} submitted in {time.time() - submit_time:.1f}s — polling every 2s...")

    for attempt in range(60):
        time.sleep(2)
        elapsed = time.time() - submit_time

        try:
            result_resp = requests.post(
                "https://api.2captcha.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=15,
            ).json()
        except Exception as e:
            print(f"  [CAPTCHA] getTaskResult error: {e}")
            continue

        status = result_resp.get("status")
        err_id = result_resp.get("errorId", 0)

        if err_id != 0:
            err = result_resp.get("errorCode", "UNKNOWN")
            print(f"  [CAPTCHA] Error after {elapsed:.0f}s: {err}")

            if err == "ERROR_CAPTCHA_UNSOLVABLE" and not _retry:
                print("  [CAPTCHA] Reloading for fresh params and retrying once...")
                return solve_captcha_2captcha(driver, api_key, _retry=True)

            print("  [CAPTCHA] Solve manually then press Enter...")
            input()
            return True

        if status == "ready":
            solution = result_resp.get("solution", {})
            voucher  = str(solution.get("captcha_voucher") or solution.get("voucher") or "")
            token    = str(solution.get("existing_token") or solution.get("token") or solution.get("aws-waf-token") or solution.get("cookie") or voucher)
            print(f"  [CAPTCHA] Solved in {elapsed:.1f}s! Solution keys: {list(solution.keys())}. Injecting token...")

            _inject_waf_token(driver, voucher, token, target_url)

            # Check if active AWS WAF challenge is still blocking after injection & reload
            if is_waf_blocking(driver):
                if not _retry:
                    print("  [CAPTCHA] WAF still active after injection — retrying once with fresh parameters...")
                    return solve_captcha_2captcha(driver, api_key, _retry=True)
                else:
                    print("  [CAPTCHA] Still challenged after retry — solve manually then press Enter...")
                    input()

            print("  [CAPTCHA] Verification successful! WAF passed cleanly.")
            return True

        if (attempt + 1) % 5 == 0:
            print(f"  [CAPTCHA] Processing... ({elapsed:.0f}s elapsed)")

    print("  [CAPTCHA] Timed out — solve manually then press Enter...")
    input()
    return True


# ── Popup dismissal ────────────────────────────────────────────────────────────

def dismiss_popup(driver):
    """Close subscribe / reveal modals if they appear after page load or CAPTCHA solve."""
    xpaths = [
        "//a[contains(@class, 'close-reveal-modal')]",
        "//div[contains(@class,'subscribe-modal')]//a[@aria-label='Close']",
        "//a[contains(@class, 'close-modal')]",
        "//button[contains(@class, 'close-reveal-modal')]",
    ]
    for xpath in xpaths:
        try:
            elements = driver.find_elements(By.XPATH, xpath)
            for el in elements:
                if el.is_displayed():
                    try:
                        driver.execute_script("arguments[0].click();", el)
                    except Exception:
                        el.click()
                    print("  [POPUP] Reveal / Subscribe modal dismissed successfully.")
                    time.sleep(0.5)
        except Exception:
            pass


# ── Helpers ───────────────────────────────────────────────────────────────────

print_lock = Lock()

def log(msg):
    with print_lock:
        print(msg, flush=True)


def build_gallery_url(base_url: str, page: int) -> str:
    """Convert listing URL to gallery page URL."""
    base = base_url.rstrip("/")
    base = re.sub(r'/gallery$', '', base)
    url = f"{base}/gallery"
    if page > 1:
        url += f"?page={page}"
    return url


def get_total_photos(driver) -> int:
    """Extract total photo count from //a[@name='photos'] element."""
    try:
        el = driver.find_element(By.XPATH, "//a[@name='photos']")
        text = el.text.strip()  # e.g. "193 Photos"
        match = re.search(r'(\d+)', text)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return 0


def scroll_and_collect(driver, gallery_url: str, api_key: str | None, page: int = 1) -> list:
    """Load a gallery page, scroll to trigger lazy load, return image URLs."""
    driver.get(gallery_url)
    driver.implicitly_wait(10)
    time.sleep(3)

    # Check / solve CAPTCHA on every page (WAF can trigger on any page load)
    if api_key and page == 1:  # Only solve CAPTCHA on first page to avoid repeated solves
        solved = solve_captcha_2captcha(driver, api_key)
        if solved:
            # Solver already refreshed the page internally, but navigate
            # explicitly to the gallery URL to land on the right page
            driver.get(gallery_url)
            time.sleep(3)

    WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )

    # Dismiss subscribe popup if it appears
    dismiss_popup(driver)

    # Slow scroll to trigger lazy loading
    current_pos  = 0
    scroll_step  = 300
    scroll_pause = 0.4

    while True:
        current_pos += scroll_step
        driver.execute_script(f"window.scrollTo(0, {current_pos});")
        time.sleep(scroll_pause)
        page_height = driver.execute_script("return document.body.scrollHeight")
        if current_pos >= page_height:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            break

    # Collect image URLs
    imgs = driver.find_elements(By.XPATH, "//img[@alt='Estate sale photo']")
    urls = []
    for img in imgs:
        src = (
            img.get_attribute("src")           or
            img.get_attribute("data-src")       or
            img.get_attribute("data-lazy-src")  or
            img.get_attribute("data-original")  or
            ""
        )
        if src and not src.startswith("data:"):
            # Prefer full-size over CDN thumbnail tokens
            src = re.sub(r'/_\d+x\d+/', '/orig/', src)
            urls.append(src)

    return urls


# ── Download ───────────────────────────────────────────────────────────────────

DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://estatesales.org/",
}


def download_one(task: dict) -> dict:
    idx      = task["index"]
    total    = task["total"]
    url      = task["url"]
    filepath = task["filepath"]
    filename = task["filename"]

    try:
        resp = requests.get(url, headers=DOWNLOAD_HEADERS, timeout=30)
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(resp.content)
        log(f"  ✓ [{idx:04d}/{total}] {filename}  ({len(resp.content) // 1024} KB)")
        return {"ok": True, "index": idx, "filename": filename, "url": url}
    except Exception as e:
        log(f"  ✗ [{idx:04d}/{total}] {filename}  ERROR: {e}")
        return {"ok": False, "index": idx, "url": url, "error": str(e)}


# ── Core scrape + download logic (importable) ──────────────────────────────────

def _collect_all_urls(driver, listing_url: str, api_key: str | None) -> list[str]:
    """
    Drive the browser through all gallery pages and return a deduplicated
    list of full-size image URLs.
    """
    all_image_urls = []

    # ── Page 1: get total count + first batch ─────────────────────────────
    page1_url = build_gallery_url(listing_url, 1)
    print(f"\nLoading page 1: {page1_url}")
    urls_p1 = scroll_and_collect(driver, page1_url, api_key, page=1)
    all_image_urls.extend(urls_p1)
    print(f"  Page 1: {len(urls_p1)} images found")

    # Determine total pages
    total_photos = get_total_photos(driver)
    if total_photos:
        print(f"  Total photos reported: {total_photos}")
        total_pages = (total_photos + 99) // 100  # 100 per page
    else:
        total_pages = 99  # keep paginating until a page returns 0 images

    # ── Remaining pages ────────────────────────────────────────────────────
    for page in range(2, total_pages + 1):
        page_url = build_gallery_url(listing_url, page)
        print(f"\nLoading page {page}: {page_url}")
        urls_pn = scroll_and_collect(driver, page_url, api_key, page=page)
        if not urls_pn:
            print(f"  No images on page {page} — stopping pagination.")
            break
        all_image_urls.extend(urls_pn)
        print(f"  Page {page}: {len(urls_pn)} images")

    # Deduplicate while preserving order
    seen        = set()
    unique_urls = []
    for u in all_image_urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    return unique_urls


def _download_images(unique_urls: list[str], listing_url: str, output_dir: str, workers: int) -> dict:
    """
    Build tasks, save links.json, download concurrently, save manifest.json.
    Returns the manifest dict.
    """
    os.makedirs(output_dir, exist_ok=True)

    # ── Extract a human-readable sale name from the URL slug ───────────────
    # e.g. ".../estate-sale-in-hanford-ca-2447915" → "estate-sale-in-hanford-ca-2447915"
    slug_match = re.search(r'/([^/]+)$', listing_url.rstrip("/"))
    sale_name  = slug_match.group(1) if slug_match else "unknown"

    # ── listing_id ─────────────────────────────────────────────────────────
    id_match   = re.search(r'(\d{6,})', listing_url)
    listing_id = id_match.group(1) if id_match else "unknown"

    print(f"\nSale    : {sale_name}")
    print(f"Total   : {len(unique_urls)} images")
    print(f"Dir     : {output_dir}")
    print(f"Workers : {workers}")

    # ── Build task list + links list ───────────────────────────────────────
    tasks = []
    links = []

    for idx, url in enumerate(unique_urls, start=1):
        ext      = url.split("?")[0].rsplit(".", 1)[-1].lower() or "jpg"
        filename = f"{idx:04d}.{ext}"
        filepath = os.path.join(output_dir, filename)

        tasks.append({
            "index":    idx,
            "total":    len(unique_urls),
            "url":      url,
            "filepath": filepath,
            "filename": filename,
        })
        links.append({
            "index":    idx,
            "filename": filename,
            "url":      url,
        })

    # ── Save links.json BEFORE downloading ────────────────────────────────
    links_path = os.path.join(output_dir, "links.json")
    with open(links_path, "w", encoding="utf-8") as f:
        json.dump({
            "listing_id":   listing_id,
            "sale_name":    sale_name,
            "listing_url":  listing_url,
            "total_images": len(unique_urls),
            "images":       links,
        }, f, indent=2)
    print(f"\nLinks saved : {links_path}")
    print("Starting concurrent downloads...\n")

    # ── Concurrent download ────────────────────────────────────────────────
    t0      = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download_one, t): t for t in tasks}
        for future in as_completed(futures):
            results.append(future.result())

    elapsed = time.time() - t0
    ok      = [r for r in results if r.get("ok")]
    failed  = [r for r in results if not r.get("ok")]

    # ── Save manifest.json AFTER downloading ──────────────────────────────
    manifest = {
        "listing_id":       listing_id,
        "sale_name":        sale_name,
        "listing_url":      listing_url,
        "total_images":     len(unique_urls),
        "total_downloaded": len(ok),
        "total_failed":     len(failed),
        "elapsed_seconds":  round(elapsed, 2),
        "images":           sorted(ok,     key=lambda r: r["index"]),
        "failed":           sorted(failed, key=lambda r: r["index"]),
    }
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n{'=' * 50}")
    print(f"Done in {elapsed:.1f}s")
    print(f"✓ Downloaded : {len(ok)}")
    print(f"✗ Failed     : {len(failed)}")
    print(f"Links JSON   : {links_path}")
    print(f"Manifest     : {manifest_path}")

    return manifest


# ── Public API ─────────────────────────────────────────────────────────────────

def ProcessSaleUrl(
    url: str,
    output_dir: str = "EstateSalesOrgOutput",
    workers: int = 16,
    api_key: str | None = None,
    max_images: int | None = None,
) -> dict:
    """
    Scrape all images from an EstateSales.org listing and download them.

    Parameters
    ----------
    url        : Full listing URL, e.g.
                 "https://estatesales.org/estate-sales/ca/hanford-/93230/..."
    output_dir : Directory to write images + JSON files into.
    workers    : Number of concurrent download threads.
    api_key    : 2captcha API key. Falls back to TWOCAPTCHA_API_KEY env var,
                 then prompts for manual solve if neither is set.

    Returns
    -------
    manifest dict (same structure as manifest.json)
    """
    use_2captcha = os.getenv("USE_2CAPTCHA", "true").lower() in ("true", "1", "yes", "on")
    if api_key is None and use_2captcha:
        api_key = os.getenv("TWOCAPTCHA_API_KEY")

    if not use_2captcha or not api_key:
        print("ℹ  CAPTCHA Mode: Manual solve active (USE_2CAPTCHA=false or TWOCAPTCHA_API_KEY unset).")

    # ── Launch browser ─────────────────────────────────────────────────────
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver_kwargs = {"options": options, "use_subprocess": True}
    if CHROME_MAIN:
        driver_kwargs["version_main"] = CHROME_MAIN

    driver = uc.Chrome(**driver_kwargs)

    try:
        try:
            unique_urls = _collect_all_urls(driver, url, api_key)
        finally:
            driver.quit()

        print(f"\nTotal unique images collected: {len(unique_urls)}")

        if max_images is not None and max_images > 0:
            unique_urls = unique_urls[:max_images]
            print(f"Limiting to first {max_images} images (MAX_IMAGES limit)")

        if not unique_urls:
            raise RuntimeError(f"[EstateSales.org Scraper Failed] 0 image URLs found for URL: '{url}' (check if page has gallery photos or is blocked by CAPTCHA)")

        manifest = _download_images(unique_urls, url, output_dir, workers)
        if not manifest or manifest.get("total_downloaded", 0) == 0:
            raise RuntimeError(f"[EstateSales.org Scraper Failed] 0 images downloaded for URL: '{url}'")
        return manifest
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise
        raise RuntimeError(f"[EstateSales.org Scraper Error] Failed to extract images from '{url}': {e}") from e


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="EstateSales.org image downloader")
    parser.add_argument("url", help="Listing URL")
    parser.add_argument("output_dir", nargs="?", default="EstateSalesOrgOutput")
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    ProcessSaleUrl(args.url, output_dir=args.output_dir, workers=args.workers)


if __name__ == "__main__":
    main()