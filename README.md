# 🏷️ AISaleAnalyst — AI Estate Sale & Resale Valuation Engine

**AISaleAnalyst** is an enterprise-grade Python application designed for estate sale buyers, liquidators, and resale arbitrageurs. It automates the end-to-end process of pulling item photos from estate sale listings, analyzing them with AI computer vision, deduplicating inventory items, fetching real-time sold market comps from eBay, and generating comprehensive financial valuation reports.

---

## 🚀 Key Features

### 📸 1. Multi-Source Image Collectors
- **Automated Web Scrapers**: Built-in support for scraping listing galleries directly from major estate sale platforms:
  - **EstateSales.net**
  - **EstateSales.org**
  - **MaxSold**
- **Local & Direct Input**: Support for analyzing local image folders or direct image URL batches.

### 🧠 2. AI Vision Identification (GPT-4o / Gemini 2.5)
- **Parallel Multi-Threading**: Analyzes image batches concurrently (`VISION_WORKERS`) for ultra-fast processing on large sales.
- **Rich Asset Metadata Extraction**:
  - Item name and noun-only category group.
  - Estimated value range (USD).
  - Item condition assessment (New, Open Box, Used, For Parts).
  - **Resale Highlights ("Why Selected")**: Identifies key value drivers such as *"Vintage"*, *"High Demand"*, *"Made in USA"*, or *"Rare Collectible"*.
  - **Automated Search Query Generation**: Formulates optimized eBay search queries and fallback queries.
- **Skipped Photos Audit**: Blurry, structural, or unidentified photos are flagged and preserved for manual review rather than silently dropped.

### 🔍 3. Universal 2-Stage Deduplication
Prevents over-counting assets across multi-angle photo sets while strictly preserving distinct individual inventory pieces:
- **Stage 1: Strict Fuzzy Deduplication**: Uses `SequenceMatcher` similarity (threshold `0.88`) to merge near-identical AI labels without collapsing separate product types.
- **Stage 2: Universal AI Deduplication**: Enforces a product-agnostic "Same Physical Object" rule. Multi-photo asset systems (e.g., a boat with its motor, trailer, and accessories) are collapsed into a single item, while individual items in shared categories (e.g., separate silver rings, necklaces, and pendants) remain distinct inventory records.

### ⚡ 4. Browserless, Super-Fast eBay Comps Scraper
- **No Selenium / Chrome Windows**: Operates 100% via HTTP requests using `curl_cffi` (Chrome TLS fingerprint impersonation) + `BeautifulSoup`.
- **4-Level Progressive Fallback Engine**:
  1. *Strictest*: AI exclusion keywords + price floor + condition filter.
  2. *Strict*: AI exclusions + price floor (or condition only for budget items).
  3. *Relaxed*: Top-3 exclusions + price floor.
  4. *Broad*: Bare search query + price floor.
- **Dynamic Exclusion Filtering**: Uses AI to dynamically generate negative search terms (e.g., `-manual`, `-parts`, `-cover`) to exclude cheap accessories from whole-unit comps.

### 📊 5. Financial Valuation & Profit Analytics
- **Recency-Weighted Median Comps**: Calculates weighted median prices, favoring recent sales and filtering out pricing anomalies (outliers <30% or >300% of median).
- **Category-Aware Shipping Estimates**: Dynamically estimates shipping based on size/weight (e.g., $0 for heavy local-pickup items like boats/furniture; tiered rates for small/medium goods).
- **Net Margin Calculations**: Computes estimated eBay platform fees, net proceeds, expected profit, return on investment (ROI %), and recommended maximum estate buy limits.
- **Confidence Penalty Logic**: Automatically reduces valuation confidence when fallback comp queries are triggered or when exact model numbers cannot be identified.

### 📄 6. Interactive HTML Reports & Incremental Persistence
- **Responsive HTML Dashboard**: Clean, modern web report featuring sortable ROI opportunities, interactive eBay verification links, search query audits, and condition badges.
- **Progress Auto-Save**: Saves intermediate analysis state to `vision_progress.json`. Interrupted runs on large sales (100+ images) can be resumed instantly without re-analyzing images or re-spending API tokens.

---

## 🛠️ Project Architecture

```
AISaleAnalyst/
├── analyze.py                 # Main application entry point & pipeline orchestrator
├── requirements.txt           # Production Python dependencies
├── .env                       # API keys configuration (OpenAI / Gemini)
├── core/                      # Core business logic modules
│   ├── config.py              # Central settings, tunable constants & AI client init
│   ├── vision.py              # Vision model prompts & parallel image analysis
│   ├── deduplication.py       # Fuzzy & AI deduplication engine
│   ├── ebay.py                # Browserless eBay sold comps scraper & fallback logic
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
OUTPUT_HTML=./demo_report.html

# ===============================================================================
# ⚡ CONCURRENCY & WORKER THREADS
# ===============================================================================
VISION_WORKERS=8
EBAY_WORKERS=8
EBAY_DELAY=1.0

# ===============================================================================
# 🔍 DEDUPLICATION & VALUATION SETTINGS
# ===============================================================================
FUZZY_THRESHOLD=0.88
USE_AI_DEDUP=true

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
python analyze.py
```

### Execution Flow:
1. **Source Selection**: The app will prompt you for an Estate Sale Listing URL (from EstateSales.net, EstateSales.org, or MaxSold) or you can set `IMAGES_FOLDER` in `.env` to target a local directory.
2. **Vision Analysis**: Images are processed concurrently (`VISION_WORKERS`). Progress is printed in real time.
3. **Deduplication**: Multi-angle photos of identical items are grouped automatically.
4. **Comps Scraping**: Market data is fetched from eBay across parallel HTTP workers (`EBAY_WORKERS`).
5. **Report Generation**: The final dashboard is generated and saved to `OUTPUT_HTML`.

---

## ⚙️ Environment Configuration Reference (`.env`)

| Variable | Type | Default | Description |
|---|---|---|---|
| `MAX_IMAGES` | `int` | `10` | Maximum number of images to analyze per run. |
| `VISION_WORKERS` | `int` | `8` | Parallel worker threads for AI vision analysis. |
| `EBAY_WORKERS` | `int` | `8` | Parallel worker threads for eBay comps scraping. |
| `EBAY_DELAY` | `float` | `1.0` | Seconds delay between eBay HTTP requests. |
| `FUZZY_THRESHOLD` | `float` | `0.88` | Similarity threshold (0.0 to 1.0) for Stage 1 deduplication. |
| `USE_AI_DEDUP` | `bool` | `true` | Enable/disable Stage 2 AI deduplication pass. |
| `OUTPUT_HTML` | `str` | `"./demo_report.html"` | File path for the output HTML report. |
| `SORT_BY` | `str` | `"roi"` | Primary sorting key (`roi`, `profit`, `median`, `confidence`). |
| `TOP_N` | `int` | `20` | Maximum top opportunities shown in summary section. |

---

## 📋 License & Credits

Developed for high-efficiency resale analysis, inventory auditing, and estate valuation automation.
