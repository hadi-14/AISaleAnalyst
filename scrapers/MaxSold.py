import re
import sys
import json
import time
import urllib.request
import urllib.parse
import os
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://maxsold.com/",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Build ID extracted from the API URL format:
# https://maxsold.com/_next/data/<BUILD_ID>/auction/<ID>/bidgallery.json
# The build ID changes with deployments — we scrape it from the HTML page
BUILD_ID_PATTERN = re.compile(r'"buildId"\s*:\s*"([^"]+)"')

print_lock = Lock()

def log(msg):
    with print_lock:
        print(msg, flush=True)


def extract_auction_id(url: str) -> str:
    """Extract auction ID from maxsold.com URL.
    e.g. https://maxsold.com/auction/110533/bidgallery -> 110533
    """
    match = re.search(r'/auction/(\d+)', url.strip())
    if match:
        return match.group(1)
    raise ValueError(f"Could not extract auction ID from: {url}")


def get_build_id(auction_id: str) -> str:
    """Scrape the Next.js build ID from the auction page HTML."""
    page_url = f"https://maxsold.com/auction/{auction_id}/bidgallery"
    print(f"Fetching build ID from: {page_url}")
    req = urllib.request.Request(page_url, headers={
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "text/html",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    match = BUILD_ID_PATTERN.search(html)
    if match:
        build_id = match.group(1)
        print(f"Build ID: {build_id}")
        return build_id

    # Fallback: try to find __NEXT_DATA__ script tag
    next_data_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', html, re.DOTALL)
    if next_data_match:
        try:
            data = json.loads(next_data_match.group(1))
            build_id = data.get("buildId", "")
            if build_id:
                print(f"Build ID (from NEXT_DATA): {build_id}")
                return build_id
        except Exception:
            pass

    raise ValueError(
        "Could not extract Next.js build ID from page HTML.\n"
        "The site may have changed structure. Try passing --build-id manually."
    )


def fetch_auction_data(auction_id: str, build_id: str) -> dict:
    """Fetch auction JSON from the Next.js data API."""
    api_url = f"https://maxsold.com/_next/data/{build_id}/auction/{auction_id}/bidgallery.json?auctionId={auction_id}"
    print(f"Fetching auction data: {api_url}")
    req = urllib.request.Request(api_url, headers={
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "application/json",
        "Referer": f"https://maxsold.com/auction/{auction_id}/bidgallery",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    print(f"API response top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
    return data


def extract_items(data: dict) -> tuple[dict, list]:
    """Extract auction info and items list from API response."""
    # Navigate: pageProps -> auction + items
    page_props = data.get("pageProps", data)

    auction = page_props.get("auction", {})
    items   = page_props.get("auction", {}).get("items", [])

    if not items:
        # Sometimes items are at top level
        items = page_props.get("items", [])

    if not items:
        print("[DEBUG] pageProps keys:", list(page_props.keys()))
        print("[DEBUG] Full response (first 2000 chars):")
        print(json.dumps(data, indent=2)[:2000])
        raise ValueError("Could not find 'items' in API response.")

    return auction, items


def download_one(task: dict) -> dict:
    idx      = task["index"]
    total    = task["total"]
    url      = task["url"]
    filepath = task["filepath"]
    filename = task["filename"]

    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        with open(filepath, "wb") as f:
            f.write(data)
        log(f"  ✓ [{idx:04d}/{total}] {filename}  ({len(data)//1024} KB)")
        return {"ok": True,  "index": idx, "filename": filename, "url": url}
    except Exception as e:
        log(f"  ✗ [{idx:04d}/{total}] {filename}  ERROR: {e}")
        return {"ok": False, "index": idx, "url": url, "error": str(e)}


def download_auction(data: dict, output_dir: str, workers: int = 16) -> dict:
    os.makedirs(output_dir, exist_ok=True)

    auction, items = extract_items(data)

    auction_id    = auction.get("id", "unknown")
    auction_title = auction.get("title", "")

    print(f"\nAuction : {auction_title}")
    print(f"Lots    : {len(items)}")
    print(f"Dir     : {output_dir}")
    print(f"Workers : {workers}")

    # ── Build tasks + links ────────────────────────────────────────────────
    tasks = []
    links = []  # per-item with all image urls
    global_idx = 0  # global image counter across all lots

    for item in items:
        lot_num   = item.get("lot_number", "?")
        item_id   = item.get("id")
        title     = item.get("title", "")
        images    = item.get("images", [])
        cur_bid   = item.get("current_bid", 0)
        min_bid   = item.get("minimum_bid", 0)

        item_images = []

        for img_idx, img_url in enumerate(images, start=1):
            global_idx += 1
            ext = urllib.parse.urlparse(img_url).path.rsplit(".", 1)[-1].lower() or "jpg"
            # filename: lot_{lot_number}_{image_index}.ext  e.g. lot_001_1.jpg
            filename = f"lot_{str(lot_num).zfill(3)}_{img_idx:02d}.{ext}"
            filepath = os.path.join(output_dir, filename)

            tasks.append({
                "index":    global_idx,
                "total":    None,  # filled below
                "url":      img_url,
                "filepath": filepath,
                "filename": filename,
            })
            item_images.append({
                "image_index": img_idx,
                "filename":    filename,
                "url":         img_url,
            })

        links.append({
            "lot_number":  lot_num,
            "item_id":     item_id,
            "title":       title,
            "current_bid": cur_bid,
            "minimum_bid": min_bid,
            "image_count": len(images),
            "images":      item_images,
        })

    total_images = global_idx
    for t in tasks:
        t["total"] = total_images

    # ── Save links.json BEFORE downloading ────────────────────────────────
    links_path = os.path.join(output_dir, "links.json")
    with open(links_path, "w", encoding="utf-8") as f:
        json.dump({
            "auction_id":    auction_id,
            "auction_title": auction_title,
            "total_lots":    len(items),
            "total_images":  total_images,
            "lots":          links,
        }, f, indent=2)
    print(f"\nLinks saved : {links_path}  ({total_images} images across {len(items)} lots)")
    print("Starting downloads...\n")

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
        "auction_id":       auction_id,
        "auction_title":    auction_title,
        "total_lots":       len(items),
        "total_images":     total_images,
        "total_downloaded": len(ok),
        "total_failed":     len(failed),
        "elapsed_seconds":  round(elapsed, 2),
        "images":           sorted(ok,     key=lambda r: r["index"]),
        "failed":           sorted(failed, key=lambda r: r["index"]),
    }
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n{'='*50}")
    print(f"Done in {elapsed:.1f}s")
    print(f"Lots        : {len(items)}")
    print(f"✓ Downloaded: {len(ok)}")
    print(f"✗ Failed    : {len(failed)}")
    print(f"Links JSON  : {links_path}")
    print(f"Manifest    : {manifest_path}")
    return manifest

def ProcessSaleUrl(url: str, output_dir: str = "MaxSoldOutput", workers: int = 16) -> str:
    auction_id = extract_auction_id(url)
    
    build_id   = get_build_id(auction_id)
    data       = fetch_auction_data(auction_id, build_id)
    download_auction(data, output_dir, workers=workers)

def main():
    parser = argparse.ArgumentParser(description="Fast concurrent MaxSold auction image downloader")
    parser.add_argument("url", help="MaxSold auction URL (e.g. https://maxsold.com/auction/110533/bidgallery)")
    parser.add_argument("output_dir", nargs="?", default=None, help="Output directory (default: ./maxsold_<id>)")
    parser.add_argument("--workers",  type=int, default=16, help="Concurrent download threads (default: 16)")
    args = parser.parse_args()

    ProcessSaleUrl(args.url, output_dir=args.output_dir, workers=args.workers)

if __name__ == "__main__":
    main()