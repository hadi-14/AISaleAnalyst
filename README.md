# 🏷️ AISaleAnalyst — AI Estate Sale & Resale Valuation Engine

**AISaleAnalyst** is an enterprise-grade Python application designed for estate sale buyers, liquidators, and resale arbitrageurs. It automates the end-to-end process of pulling item photos from estate sale listings, analyzing them with AI computer vision, deduplicating inventory items, fetching real-time sold market comps from eBay, and generating comprehensive financial valuation reports.

---

## 🚀 Key Features

### 📸 1. Multi-Source Image Collectors
- **Automated Web Scrapers**: Built-in support for scraping listing galleries directly from major estate sale platforms:
  - **EstateSales.net**
  - **EstateSales.org** *(Note: Uses `undetected-chromedriver`. If you hit AWS WAF CAPTCHAs, it will prompt you for manual solving, or you can enable `USE_2CAPTCHA=true` for automated solving).*
  - **MaxSold**
- **Local & Direct Input**: Support for analyzing local image folders or direct image URL batches.

### 🧠 2. AI Vision Identification (GPT-4o / Gemini 2.5)
- **Automatic Provider Selection**: The app automatically uses OpenAI if `OPENAI_API_KEY` is present in `.env`, and falls back to Gemini if only `GEMINI_API_KEY` is provided.
- **Parallel Multi-Threading**: Analyzes image batches concurrently (`VISION_WORKERS`) for ultra-fast processing on large sales.
- **Rich Asset Metadata Extraction**:
  - Item name and noun-only category group.
  - Estimated value range (USD).
  - Item condition assessment (New, Open Box, Used, For Parts).
  - **Estimated Package Dimensions & Weight**: Automatically estimates the boxed package size (`pkg_length_in`, `pkg_width_in`, `pkg_height_in`) and weight (`pkg_weight_lb`) to feed live carrier rate lookups.
  - **Resale Highlights ("Why Selected")**: Identifies key value drivers such as *"Vintage"*, *"High Demand"*, *"Made in USA"*, or *"Rare Collectible"*.
  - **Automated Search Query Generation**: Formulates optimized eBay search queries and fallback queries.
- **Skipped Photos Audit**: Blurry, structural, or unidentified photos are flagged and preserved for manual review rather than silently dropped.

### 🔍 3. Universal AI Deduplication
Prevents over-counting assets across multi-angle photo sets while strictly preserving distinct individual inventory pieces:
- **Universal AI Deduplication**: Enforces a product-agnostic "Same Physical Object" rule. Multi-photo asset systems (e.g., a boat with its motor, trailer, and accessories) are collapsed into a single item, while individual items in shared categories (e.g., two distinct vintage wood chairs) remain distinct inventory records based on their visual condition notes.
- **Zoomable Image Gallery**: When the AI groups multiple photos together, the final HTML report displays the primary photo alongside the alternate angles in a zoomable mini-gallery.

### ⚡ 4. Browserless, Super-Fast eBay Comps Scraper
- **No Selenium / Chrome Windows**: Operates 100% via HTTP requests using `curl_cffi` (Chrome TLS fingerprint impersonation) + `BeautifulSoup`.
- **4-Level Progressive Fallback Engine**:
  1. *Strictest*: AI exclusion keywords + price floor + condition filter.
  2. *Strict*: AI exclusions + price floor (or condition only for budget items).
  3. *Relaxed*: Top-3 exclusions + price floor.
  4. *Broad*: Bare search query + price floor.
- **Dynamic Exclusion Filtering**: Uses AI to dynamically generate negative search terms (e.g., `-manual`, `-parts`, `-cover`) to exclude cheap accessories from whole-unit comps.

### 📊 5. Live Shippo Shipping & Financial Analytics
- **Live Carrier Rate Shopping**: Integrates with the **Shippo API** to fetch exact real-time USPS and UPS shipping rates based on package weight, dimensions, origin ZIP, and destination ZIP.
- **Service Configuration**: Supports rate matching for the cheapest option across all carriers or filtering for specific shipping classes (e.g. USPS Ground Advantage, USPS Priority, UPS Ground).
- **Graceful Fallbacks**: Smart local-pickup detection sets shipping to $0 for freight/vehicles (boats, cars, heavy lawnmowers), and a robust flat-rate fallback engine takes over if Shippo credentials are missing or the API goes down.
- **Recency-Weighted Mean Comps**: Calculates weighted mean prices, favoring recent sales and filtering out pricing anomalies (outliers <30% or >300% of median).
- **Net Margin Calculations**: Computes estimated eBay platform fees, net proceeds, expected profit, return on investment (ROI %), and recommended maximum estate buy limits.
- **Confidence Penalty Logic**: Automatically reduces valuation confidence when fallback comp queries are triggered or when exact model numbers cannot be identified.

### 📄 6. Interactive HTML Reports & Incremental Persistence
- **Responsive HTML Dashboard**: Clean, modern web report featuring sortable ROI opportunities, interactive eBay verification links, search query audits, and condition badges.
- **Progress Auto-Save**: Saves intermediate analysis state to `vision_progress.json`. Interrupted runs on large sales (100+ images) can be resumed instantly without re-analyzing images or re-spending API tokens.

---

## 🛠️ Project Architecture

```
AISaleAnalyst/
├── main.py                    # Main application entry point & pipeline orchestrator
├── requirements.txt           # Production Python dependencies
├── .env                       # API keys configuration (OpenAI / Gemini)
├── core/                      # Core business logic modules
│   ├── config.py              # Central settings, tunable constants & AI client init
│   ├── vision.py              # Vision model prompts & parallel image analysis
│   ├── deduplication.py       # Fuzzy & AI deduplication engine
│   ├── ebay.py                # Browserless eBay sold comps scraper & fallback logic
│   ├── shipping.py            # Live Shippo API client & rate matching logic
│   ├── financials.py          # Pricing medians, fee models, shipping & confidence logic
│   └── report.py              # HTML report builder & template generator
└── scrapers/                  # Platform-specific image collection scrapers
    ├── ListingExtractor.py    # URL detector & scraper dispatcher
    ├── EstateSalesNet.py      # EstateSales.net downloader
    ├── EstateSalesOrg.py      # EstateSales.org downloader
    └── MaxSold.py             # MaxSold downloader
```

---

## 💻 Installation & Setup

### 1. Prerequisites
- Python 3.10 or higher.

### 2. Clone & Install Dependencies
Navigate to the project directory and install the required packages:
```bash
pip install -r requirements.txt
```

### 3. Configure `.env` Environment Variables
Copy `.env.example` to `.env` and configure your API keys, runtime settings, worker threads, and valuation thresholds:
```bash
cp .env.example .env
```

```ini
# ===============================================================================
# 🔑 API KEYS & PROVIDERS
# ===============================================================================
OPENAI_API_KEY=your_openai_api_key_here
# GEMINI_API_KEY=your_gemini_api_key_here

# ===============================================================================
# ⚙️ APPLICATION RUNTIME CONFIGURATION
# ===============================================================================
MAX_IMAGES=10
IMAGES_FOLDER=
OUTPUT_FOLDER=./reports

# ===============================================================================
# ⚡ CONCURRENCY & WORKER THREADS
# ===============================================================================
VISION_WORKERS=8
EBAY_WORKERS=8
EBAY_DELAY=1.0

# ===============================================================================
# 🔍 DEDUPLICATION & VALUATION SETTINGS
# ===============================================================================
USE_DEDUP=true
USE_AI_DEDUP=true

# ===============================================================================
# 📦 SHIPPING (SHIPPO)
# ===============================================================================
SHIPPO_API_KEY=your_shippo_api_key_here
SHIP_FROM_ZIP=60601
SHIP_TO_ZIP=10001
SHIP_SERVICE=cheapest
SHIP_MANUAL_DIMS=false
SHIP_MANUAL_LENGTH=12
SHIP_MANUAL_WIDTH=10
SHIP_MANUAL_HEIGHT=8
SHIP_MANUAL_WEIGHT=3

# ===============================================================================
# 📊 REPORT DISPLAY & SORTING
# ===============================================================================
SORT_BY=roi
TOP_N=20
```

---

## 🚦 Usage Guide

Run the main pipeline script:
```bash
python main.py
```

### Execution Flow:
1. **Source Selection**: The app will prompt you for an Estate Sale Listing URL (from EstateSales.net, EstateSales.org, or MaxSold) or you can set `IMAGES_FOLDER` in `.env` to target a local directory.
2. **Vision Analysis**: Images are processed concurrently (`VISION_WORKERS`). Progress is printed in real time.
3. **Deduplication**: Multi-angle photos of identical items are grouped automatically.
4. **Comps Scraping**: Market data is fetched from eBay across parallel HTTP workers (`EBAY_WORKERS`).
5. **Report Generation**: The final dashboard is generated and saved using `OUTPUT_FOLDER` as the base filename, dynamically appending the sale ID and timestamp.

---

## 📊 Sample AI Output

The vision pipeline converts raw photos into structured JSON payloads like this:

```json
{
  "skip": false,
  "exact_model_identified": true,
  "multi_item_detected": false,
  "item_name": "DeWalt DWS779 12-inch Sliding Compound Miter Saw",
  "condition_notes": "Used, minor sawdust and scuffs",
  "confidence": 95,
  "ebay_condition": "Used",
  "ebay_search_query": "DeWalt DWS779 Miter Saw",
  "ebay_fallback_query": "DeWalt Miter Saw",
  "ebay_exclusion_keywords": ["blade", "manual", "box", "bag", "stand"],
  "platform": "eBay",
  "ebay_category_id": 3312,
  "ai_value_low": 250,
  "ai_value_high": 350,
  "ai_value_notes": "Popular contractor saw, holds value well",
  "estate_buy_price": 75,
  "item_group": "saw",
  "resale_reasons": "High demand, Contractor grade",
  "pkg_length_in": 34,
  "pkg_width_in": 24,
  "pkg_height_in": 20,
  "pkg_weight_lb": 56.0
}
```

---

## ⚙️ Environment Configuration Reference (`.env`)

| Variable | Type | Default | Description |
|---|---|---|---|
| `MAX_IMAGES` | `int` | `10` | Maximum number of images to analyze per run. |
| `VISION_WORKERS` | `int` | `8` | Parallel worker threads for AI vision analysis. |
| `EBAY_WORKERS` | `int` | `8` | Parallel worker threads for eBay comps scraping. |
| `EBAY_DELAY` | `float` | `1.0` | Seconds delay between eBay HTTP requests. |
| `USE_DEDUP` | `bool` | `true` | Enable/disable AI deduplication pass. |
| `USE_AI_DEDUP` | `bool` | `true` | Alias for USE_DEDUP (retained for backward compatibility). |
| `USE_NAME_DEDUP` | `bool` | `true` | Enable post-dedup fuzzy name grouping to catch similar items. |
| `NAME_DEDUP_THRESHOLD` | `float` | `0.85` | Similarity threshold for fuzzy name matching (0.0-1.0). |
| `USE_VISUAL_VERIFY` | `bool` | `true` | Verify name-matched candidates visually with AI before merging. |
| `GENERATE_DUPLICATES_REPORT` | `bool` | `true` | Auto-generate a Duplicates Excel report alongside the HTML report. |
| `USE_2CAPTCHA` | `bool` | `true` | Enable/disable auto 2captcha solving (set to `false` for manual solve). |
| `OUTPUT_FOLDER` | `str` | `"./reports"` | Directory to save the dynamically generated HTML reports. |
| `REPORT_OUTPUT_DIR` | `str` | `None` | Optional absolute path to a custom folder where reports should be saved. |
| `EMAIL_REPORTS` | `bool` | `false` | Set to true to automatically email the completed report after generation. |
| `REPORT_EMAIL_TO` | `str` | `None` | Comma-separated list of email addresses to receive the report. |
| `SMTP_USER` | `str` | `"..."` | Gmail address used for sending reports. |
| `SMTP_PASS` | `str` | `"..."` | App password for the Gmail account. |
| `SORT_BY` | `str` | `"roi"` | Primary sorting key (`roi`, `profit`, `median`, `confidence`). |
| `TOP_N` | `int` | `20` | Maximum top opportunities shown in summary section. |
| `SHIPPO_API_KEY` | `str` | `""` | Live API key from goshippo.com to fetch real-time carrier rates. |
| `SHIP_FROM_ZIP` | `str` | `"60601"` | Origin ZIP code (where packages are shipped from). |
| `SHIP_TO_ZIP` | `str` | `"10001"` | Destination ZIP code for shipping rate estimation. |
| `SHIP_SERVICE` | `str` | `"cheapest"` | Preferred shipping service level (`cheapest`, `usps_ground`, `usps_priority`, `ups_ground`). |
| `SHIP_MANUAL_DIMS` | `bool` | `false` | When true, skips AI package dimension estimates and uses fallback sizes. |
| `SHIP_MANUAL_LENGTH` | `float` | `12.0` | Default fallback package length in inches. |
| `SHIP_MANUAL_WIDTH` | `float` | `10.0` | Default fallback package width in inches. |
| `SHIP_MANUAL_HEIGHT` | `float` | `8.0` | Default fallback package height in inches. |
| `SHIP_MANUAL_WEIGHT` | `float` | `3.0` | Default fallback package weight in pounds. |

---

## 📋 License & Credits

This project is licensed under the **MIT License**.

Developed for high-efficiency resale analysis, inventory auditing, and estate valuation automation.
