#!/usr/bin/env python3
"""
Estate Sale Image Downloader — Concurrent Edition
Saves links.json first, then downloads all images concurrently.

Usage: python download_estate_images.py <sale_url> [output_dir] [--workers N]
Example:
  python download_estate_images.py https://www.estatesales.net/MI/Warren/48088/4839822
  python download_estate_images.py https://www.estatesales.net/MI/Warren/48088/4839822 ./images --workers 20
"""

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
    "Referer": "https://www.estatesales.net/",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

print_lock = Lock()

def log(msg):
    with print_lock:
        print(msg, flush=True)


def extract_sale_id(url: str) -> str:
    # Always take the LAST numeric segment from the path
    # e.g. /CA/Lakewood/90713/4963119 -> 4963119
    path = urllib.parse.urlparse(url.strip()).path
    segments = [s for s in path.split("/") if s.isdigit()]
    if segments:
        return segments[-1]
    raise ValueError(f"Could not extract sale ID from: {url}")


def fetch_sale_data(sale_id: str) -> dict:
    query = json.dumps({"saleId": int(sale_id), "userId": None, "isSuper": False})
    encoded = urllib.parse.quote(query)
    api_url = (
        f"https://www.estatesales.net/api/legacy/queries/traditional-sales/"
        f"traditional-sale?query={encoded}&explicitTypes=DateTime"
    )
    print(f"Fetching sale data for ID {sale_id}...")
    print(f"API URL: {api_url}")
    req = urllib.request.Request(api_url, headers={
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "application/json",
        "Referer": "https://www.estatesales.net/",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()

    data = json.loads(raw)
    print(f"API response top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")

    if isinstance(data, dict):
        sale = data.get("sale")
        if sale is None:
            print("\n[DEBUG] Full API response (first 2000 chars):")
            print(json.dumps(data, indent=2)[:2000])
            raise ValueError(
                f"API returned null for 'sale' key. "
                f"Sale ID {sale_id} may be invalid or API structure changed."
            )

    return data


def download_one(task: dict) -> dict:
    idx      = task["index"]
    total    = task["total"]
    url      = task["url"]
    filepath = task["filepath"]
    filename = task["filename"]
    pic      = task["pic"]

    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        with open(filepath, "wb") as f:
            f.write(data)
        log(f"  [OK] [{idx:03d}/{total}] {filename}  ({len(data)//1024} KB)")
        return {
            "ok":          True,
            "index":       idx,
            "filename":    filename,
            "original_url": url,
            "picture_id":  pic.get("id"),
            "width":       pic.get("width"),
            "height":      pic.get("height"),
            "order":       pic.get("pictureOrder"),
        }
    except Exception as e:
        log(f"  [FAIL] [{idx:03d}/{total}] {filename}  ERROR: {e}")
        return {"ok": False, "index": idx, "url": url, "error": str(e)}


def download_images(sale_data: dict, output_dir: str, workers: int = 16, max_images: int | None = None) -> dict:
    os.makedirs(output_dir, exist_ok=True)

    # Handle both {"sale": {...}} and bare sale dict
    if "sale" in sale_data and sale_data["sale"] is not None:
        sale = sale_data["sale"]
    elif "pictures" in sale_data:
        sale = sale_data
    else:
        print("[DEBUG] Unexpected data structure:")
        print(json.dumps(sale_data, indent=2)[:2000])
        raise ValueError(f"Cannot find 'pictures' key. Keys found: {list(sale_data.keys())}")

    pictures  = sale.get("pictures", [])
    if max_images is not None and max_images > 0:
        pictures = pictures[:max_images]
    sale_id   = sale.get("saleId", "unknown")
    sale_name = sale.get("name", "")

    if not pictures:
        raise ValueError(f"No pictures found in sale data for sale ID {sale_id}")

    print(f"\nSale    : {sale_name}")
    print(f"Total   : {len(pictures)} images")
    print(f"Dir     : {output_dir}")
    print(f"Workers : {workers}")

    # ── Build task list + links list together ──────────────────────────────
    tasks = []
    links = []

    for idx, pic in enumerate(pictures, start=1):
        url = pic.get("url", "")
        if not url:
            continue
        ext      = urllib.parse.urlparse(url).path.rsplit(".", 1)[-1].lower() or "jpg"
        filename = f"{idx:03d}.{ext}"
        filepath = os.path.join(output_dir, filename)

        tasks.append({
            "index":    idx,
            "total":    len(pictures),
            "url":      url,
            "filepath": filepath,
            "filename": filename,
            "pic":      pic,
        })
        links.append({
            "index":         idx,
            "filename":      filename,
            "url":           url,
            "thumbnail_url": pic.get("thumbnailUrl", ""),
            "picture_id":    pic.get("id"),
            "width":         pic.get("width"),
            "height":        pic.get("height"),
            "order":         pic.get("pictureOrder"),
        })

    # ── Save links.json BEFORE downloading ────────────────────────────────
    links_path = os.path.join(output_dir, "links.json")
    with open(links_path, "w", encoding="utf-8") as f:
        json.dump({
            "sale_id":   sale_id,
            "sale_name": sale_name,
            "sale_url":  f"https://www.estatesales.net/sale/{sale_id}",
            "total":     len(links),
            "images":    links,
        }, f, indent=2)
    print(f"\nLinks saved : {links_path}")
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
        "sale_id":          sale_id,
        "sale_name":        sale_name,
        "total_images":     len(pictures),
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
    print(f"Downloaded : {len(ok)}")
    print(f"Failed     : {len(failed)}")
    print(f"Links JSON   : {links_path}")
    print(f"Manifest     : {manifest_path}")
    return manifest

def ProcessSaleUrl(url: str, output_dir: str = "EstateSaleNetOutput", workers: int = 16, max_images: int | None = None) -> str:
    try:
        sale_id   = extract_sale_id(url)
        sale_data = fetch_sale_data(sale_id)
        manifest  = download_images(sale_data, output_dir, workers=workers, max_images=max_images)
        if not manifest or manifest.get("total_downloaded", 0) == 0:
            raise RuntimeError(f"[EstateSales.net Scraper Failed] 0 images extracted/downloaded for URL: '{url}'")
        return output_dir
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise
        raise RuntimeError(f"[EstateSales.net Scraper Error] Failed to extract images from '{url}': {e}") from e


def main():
    parser = argparse.ArgumentParser(description="Fast concurrent estate sale image downloader")
    parser.add_argument("url", help="EstateSales.net sale URL")
    parser.add_argument("output_dir", nargs="?", default=None, help="Output directory (default: ./estate_sale_<id>)")
    parser.add_argument("--workers", type=int, default=16, help="Concurrent download threads (default: 16)")
    args = parser.parse_args()

    ProcessSaleUrl(args.url, output_dir=args.output_dir, workers=args.workers)

if __name__ == "__main__":
    main() # python EstateSalesNet.py https://www.estatesales.net/CA/Lakewood/90713/4963119