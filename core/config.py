"""
config.py
=========
Central configuration and AI-client initialisation for AISaleAnalyst.

All tunable constants live here.  Import this module from every other
module that needs settings or an AI client — do NOT duplicate API-key
loading elsewhere.
"""

import os
import json
import re
import base64
from io import BytesIO

from dotenv import load_dotenv
from PIL import Image

load_dotenv()

# ---------------------------------------------------------------------------
# Environment variable helper functions
# ---------------------------------------------------------------------------

def _env_str(key: str, default: str | None = None) -> str | None:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    return val.strip()

def _env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default

def _env_float(key: str, default: float) -> float:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val.strip())
    except ValueError:
        return default

def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("true", "1", "yes", "on")

# ---------------------------------------------------------------------------
# Tunable constants (loaded from environment / .env with fallback defaults)
# ---------------------------------------------------------------------------

#: Path to the folder that contains downloaded listing images.
#: Set to None to prompt the user at runtime.
IMAGES_FOLDER: str | None = _env_str("IMAGES_FOLDER", None)

#: Path to the folder where the HTML reports are saved.
OUTPUT_FOLDER: str = _env_str("OUTPUT_FOLDER", "./")

#: Maximum number of images to analyse per run.
MAX_IMAGES: int = _env_int("MAX_IMAGES", 10)

#: Number of concurrent worker threads for AI vision analysis.
VISION_WORKERS: int = _env_int("VISION_WORKERS", 8)

#: Number of concurrent worker threads for eBay sold-comps scraping.
EBAY_WORKERS: int = _env_int("EBAY_WORKERS", 8)

#: Seconds to sleep after each eBay page load (avoids rate-limiting).
EBAY_DELAY: float = _env_float("EBAY_DELAY", 1.0)

#: Report sort key.  One of: "roi" | "profit" | "mean" | "confidence"
SORT_BY: str = _env_str("SORT_BY", "roi")

#: Maximum rows shown in the "Top Opportunities" section of the report.
TOP_N: int = _env_int("TOP_N", 20)


#: When True, run a second AI-powered deduplication pass after fuzzy matching.
USE_AI_DEDUP: bool = _env_bool("USE_AI_DEDUP", True)

#: When True, pass the actual thumbnail images to the AI during deduplication.
USE_VISION_DEDUP: bool = _env_bool("USE_VISION_DEDUP", False)

#: When True, enable deduplication. When False, bypass all deduplication passes.
USE_DEDUP: bool = _env_bool("USE_DEDUP", True)

#: When True, enable automatic CAPTCHA solving via 2captcha. Set to False for manual solve.
USE_2CAPTCHA: bool = _env_bool("USE_2CAPTCHA", True)

#: Legacy headless flag (retained for signature compatibility).
EBAY_HEADLESS: bool = _env_bool("EBAY_HEADLESS", False)

# ---------------------------------------------------------------------------
# Shipping (Shippo) configuration
# ---------------------------------------------------------------------------

#: Shippo live API key. Leave blank to fall back to flat-rate estimates.
SHIPPO_API_KEY: str | None = _env_str("SHIPPO_API_KEY", None)

#: Origin ZIP code — where the seller ships packages FROM.
SHIP_FROM_ZIP: str = _env_str("SHIP_FROM_ZIP", "60601")

#: Destination ZIP code — used for rate estimation.
SHIP_TO_ZIP: str = _env_str("SHIP_TO_ZIP", "10001")

#: Preferred service key: "cheapest" | "usps_ground" | "usps_priority" | "ups_ground"
SHIP_SERVICE: str = _env_str("SHIP_SERVICE", "cheapest")

#: When True, use manual dimensions instead of AI-estimated ones.
SHIP_MANUAL_DIMS: bool = _env_bool("SHIP_MANUAL_DIMS", False)

#: Manual dimension overrides (inches / pounds). Only used when SHIP_MANUAL_DIMS=True.
SHIP_MANUAL_LENGTH: float = _env_float("SHIP_MANUAL_LENGTH", 12.0)
SHIP_MANUAL_WIDTH:  float = _env_float("SHIP_MANUAL_WIDTH",  10.0)
SHIP_MANUAL_HEIGHT: float = _env_float("SHIP_MANUAL_HEIGHT",  8.0)
SHIP_MANUAL_WEIGHT: float = _env_float("SHIP_MANUAL_WEIGHT",  3.0)

# ---------------------------------------------------------------------------
# AI provider detection & client initialisation
# ---------------------------------------------------------------------------

GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY")
OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY")

#: Active provider string — "openai" or "gemini".  Set automatically below.
AI_PROVIDER: str

if OPENAI_API_KEY:
    from openai import OpenAI
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    AI_PROVIDER = "openai"
    print("Using OpenAI GPT-4o")
elif GEMINI_API_KEY:
    from google import genai as google_genai
    gemini_client = google_genai.Client(api_key=GEMINI_API_KEY)
    AI_PROVIDER = "gemini"
    print("Using Gemini 2.5 Flash")
else:
    raise ValueError(
        "No AI API key found.  Set GEMINI_API_KEY or OPENAI_API_KEY in your .env file."
    )

# ---------------------------------------------------------------------------
# Shared utility helpers
# ---------------------------------------------------------------------------


def fix_and_parse_json(text: str):
    """
    Parse a JSON string that may be wrapped in markdown code fences or
    contain trailing commas (common in AI-generated output).

    Parameters
    ----------
    text:
        Raw text returned by an AI model.

    Returns
    -------
    dict | list
        Parsed Python object.

    Raises
    ------
    json.JSONDecodeError
        If the text cannot be parsed after cleanup.
    """
    text = re.sub(r"```json|```", "", text).strip()
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return json.loads(text)


def image_to_base64(image_path: str) -> str:
    """
    Convert an image file to a Base64-encoded JPEG data-URI suitable for
    embedding directly in HTML.

    Parameters
    ----------
    image_path:
        Absolute or relative path to the source image.

    Returns
    -------
    str
        Data-URI string (``data:image/jpeg;base64,...``), or an empty
        string if the image cannot be read.
    """
    try:
        img = Image.open(image_path)
        img.thumbnail((450, 450))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""
