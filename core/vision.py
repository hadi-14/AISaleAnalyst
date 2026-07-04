"""
vision.py
=========
AI-powered image analysis for AISaleAnalyst.

Provides :func:`analyze_image`, which sends an estate-sale photo to the
active AI provider (OpenAI GPT-4o or Gemini 2.5 Flash) and returns a
structured dict describing the item in the image.
"""

import base64
import time
from pathlib import Path

from PIL import Image

from .config import AI_PROVIDER, fix_and_parse_json

# Conditionally import whichever client was initialised in config
if AI_PROVIDER == "openai":
    from .config import openai_client
else:
    from .config import gemini_client

# ---------------------------------------------------------------------------
# Vision prompt
# ---------------------------------------------------------------------------

VISION_PROMPT = """\
You are an expert estate sale reseller with 20 years experience flipping \
items on eBay, Etsy, and Depop.
Analyze this image carefully.

BEFORE naming the item, look for ANY of these identifiers in the image:
  • Brand/maker name (on decal, plate, label, or embossed)
  • Model name or number (on badge, sticker, data plate, or casting)
  • Year or generation (from styling cues, serial number format, or visible date stamps)
  • Serial / part number (on metal plates or stickers)
  • Size / capacity / spec (e.g. HP rating, length in feet, cc, oz)

Strictly read the text and decals on the object. If the exact model name/number is not legible or visible, do NOT guess or hallucinate a random model number. Instead, identify key visual specifications and characteristics (e.g., horsepower rating like "8 HP", length like "16 ft", capacity, or engine type) and combine them with the brand to form the item name and query (e.g., "Troy-Bilt 8HP Chipper" or "Princecraft 16ft Boat").

Use every identifier and spec you can read or estimate to build the most specific possible item name and eBay search query.

Return ONLY a valid JSON object with these exact fields:
{
  "skip": false,
  "skip_reason": "If skip is true, explain why (e.g., blurry, structural, no item), otherwise empty string",
  "exact_model_identified": true,
  "multi_item_detected": false,
  "item_name": "Brand Model# Year/Spec — e.g. Princecraft Super Pro 176 Boat 1998 or Troy-Bilt Tomahawk 5HP Chipper",
  "condition_notes": "One line condition assessment",
  "confidence": 88,
  "ebay_condition": "Used — one of: New, Open Box, Used, For parts",
  "ebay_search_query": "Brand Model# — e.g. Princecraft Super Pro 176 or Troy-Bilt Tomahawk (do NOT combine multiple distinct assets like boat and outboard HP, e.g. use 'Princecraft 176' or 'Princecraft Super Pro 176', not 'Princecraft 176 Boat with 115 Outboard Motor')",
  "ebay_fallback_query": "Brand Noun — e.g. Princecraft Boat or Troy-Bilt Chipper (broader query containing ONLY brand and category/noun, used if the specific query returns no results)",
  "ebay_exclusion_keywords": ["manual", "box", "case", "parts", "battery"],
  "platform": "eBay",
  "ebay_category_id": 26429,
  "ai_value_low": 250,
  "ai_value_high": 500,
  "ai_value_notes": "One line reasoning for your estimate",
  "estate_buy_price": 50,
  "item_group": "NOUN-ONLY 1-2 word label for the MAIN object",
  "resale_reasons": "Short keywords/phrase explaining why item holds resale appeal — e.g. Vintage, High demand, Made in USA, Rare collectible",
  "pkg_length_in": 12,
  "pkg_width_in": 8,
  "pkg_height_in": 6,
  "pkg_weight_lb": 2.5
}

Rules:
- Skip Conditions: Set "skip": true ONLY if the image is completely blurry, dark, empty, or is a house structural view (e.g., empty walls, window panes, cracks, doorways, floors with no items).
- Multi-Item Flag: Set "multi_item_detected": true if the photo shows a cluttered scene, shelf, or group of multiple distinct saleable items (e.g., 5 different vases, a box of random tools, a shelf of books). You should still identify the single most prominent/valuable item in the photo, but flagging it alerts the human reviewer to hidden inventory.
- Priority for Model/Product Identification: Prioritize identifying the exact brand and model number/name. If the exact model is not legible or identified, do NOT skip the image. Instead, set "exact_model_identified": false, and identify the general product/item type (e.g., "Princecraft Boat" or just "Boat") by combining the brand with the category/noun or any general visual specifications (e.g., HP rating, length, etc.).
- confidence is 0-100 integer. Base this ONLY on how clearly you can identify the object from the photo. Do NOT artificially lower this score just because an exact model number is missing (we apply penalties for that downstream).
- platform is one of: eBay, Etsy, Depop, Facebook Marketplace
- ebay_condition MUST be one of: New, Open Box, Used, For parts. Evaluate from visual wear, packaging, etc.
- ebay_search_query MUST include brand + model number/name + spec if readable — \
  never use generic terms like "boat" or "tool" alone. Do NOT append the word "sold" or "completed". Keep the search query clean and focused on the primary asset name. Avoid combining boat and outboard motors into one query (e.g. use 'Princecraft 176' or 'Princecraft Super Pro 176').
- ebay_exclusion_keywords MUST be an array of up to 5 single-word lowercase keywords representing parts, accessories, manuals, or boxes that would appear in cheap, unrelated listings and should be EXCLUDED from search results (e.g., ["manual", "carburetor", "blade", "gasket", "box"]). Do NOT include words that are part of the item's actual name.
- ebay_category_id is the numeric eBay Category ID (sacat) for the item. Refer to these common category IDs:
  * Antiques: 20081 | Art: 550 | Books: 267 | Clothing: 11450 | Coins & Paper Money: 11116
  * Collectibles: 1 | Electronics: 293 | Furniture: 20091 | Home & Garden: 11700 | Jewelry: 281
  * Watches: 31387 | Kitchenware: 20625 | Toys: 220 | Sporting Goods: 382 | Tools: 3110
  * Cameras: 625 | Musical Instruments: 619 | Video Games: 1249 | Pottery & Glass: 870 | Stamps: 260
  * Boats: 26429 | Boat Parts: 26443 | Outboard Engines: 152737 | Lawn Mowers: 151756 | Power Tools: 3312
  If you don't know the exact ID, use the closest broad parent category ID.
- ai_value_low and ai_value_high are YOUR expert USD estimate, independent of eBay (set realistic values)
- estate_buy_price is typical estate sale price for this item (10-30% of resale value)

Identifying Standalone/Detachable Equipment:
- If the image focuses on a distinct, detachable, or valuable piece of equipment/accessory (such as a trolling motor, outboard motor, trailer, standalone tool attachment, or generator), identify the item as that specific accessory (e.g., "Minn Kota PowerDrive V2 Trolling Motor"), NOT the larger vehicle/boat it is attached to.
- Only identify the item as the whole vehicle (e.g., "boat" or "car") if the photo shows the entire vehicle or a general view of it.

Identifying Generic vs Specific Items (CRITICAL):
- Generic Items: If the item is a generic household good (e.g., plain wood nightstand, photo album, folding TV tray, unbranded glass jar, generic decor) and you CANNOT identify a specific manufacturer, vintage designer, or unique collectible feature, you MUST either:
  1. Set "skip": true if the item has negligible resale value (e.g. under $10).
  2. Set "confidence" very low (e.g., 20-40) and set "exact_model_identified": false.
- Do NOT use generic nouns for the search query (e.g., never search "Wooden Nightstand" or "Folding TV Tray Table"). Generic searches will match expensive, unrelated designer pieces on eBay and artificially inflate valuations. The search query MUST contain distinguishing features (material, brand, vintage era, unique style) if you decide not to skip it.

item_group rules — READ CAREFULLY:
  • Use the SHORTEST possible noun(s) that name the MAIN physical object in the frame
  • Strip ALL adjectives, colors, brands, eras, conditions — nouns only
  • If the image shows a DETAIL, INTERIOR, or PARTIAL VIEW of a larger object, label it
    as the WHOLE object (e.g. a photo of a car dashboard → "car", boat interior → "boat")
  • EXCEPTIONS: If the detail/part is a valuable standalone accessory or component being sold separately (e.g. trolling motor, outboard motor, trailer), label it as the accessory class (e.g. "trolling motor", "outboard motor", "trailer"), not the vehicle.
  • Subtypes and varieties of the same object class MUST collapse to one label
    (e.g. "pontoon", "bowrider", "speedboat", "fishing boat" → all become "boat")
  • Two photos of the same physical object from different angles MUST produce the
    EXACT same item_group string — imagine you are labeling the object, not the photo
  • When uncertain between a specific subtype and a general noun, always prefer the
    general noun (e.g. "compressor" not "air compressor", "jacket" not "denim jacket")

- Package dimension rules (pkg_length_in, pkg_width_in, pkg_height_in, pkg_weight_lb):
  • Estimate the BOXED shipping dimensions in inches and weight in pounds as if you were packaging this item to ship via USPS or UPS.
  • Include padding/box walls in your dimension estimate (add ~2 inches per side).
  • For local-pickup / freight items (boats, vehicles, large furniture, tractors) set all four values to 0.
  • Be realistic — a wristwatch ships in a ~6x4x3in 0.5lb box; a power drill in a ~14x10x8in 6lb box.
  • Examples to guide your estimates:
      - Smartphone/Watch: 6x4x3 in, 1 lb
      - Shoes: 14x10x6 in, 3 lb
      - Small Appliance (Toaster/Blender): 16x12x10 in, 8 lb
      - Hand Power Tool: 16x12x8 in, 6 lb
      - Desktop Computer: 24x20x12 in, 25 lb
      - Acoustic Guitar: 48x20x8 in, 15 lb
      - Stereo Receiver: 20x18x10 in, 20 lb
  • IMPORTANT: Do NOT overestimate sizes or weights. Overly large dimensions drastically inflate shipping costs and ruin ROI.
  • Keep dimensions as tight and small as safely possible. If uncertain, err on the realistic average rather than the maximum.
  • These values feed a live shipping API, so accuracy matters.

- Return ONLY raw JSON — no markdown, no backticks, no trailing commas, no explanation\
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_image(image_path: str) -> dict:
    """
    Send an image to the active AI provider and extract item metadata.

    The model is instructed to return a structured JSON object.  If the
    image is unusable (blurry, empty, etc.) the model sets ``"skip": true``.

    Parameters
    ----------
    image_path:
        Absolute or relative path to the image file to analyse.

    Returns
    -------
    dict
        Parsed AI response.  Always contains at minimum ``{"skip": True}``
        on any error or unrecognisable image.
    """
    import time
    
    attempt = 1
    while True:
        try:
            if AI_PROVIDER == "openai":
                with open(image_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                ext  = Path(image_path).suffix.lower().replace(".", "")
                mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"

                response = openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text",      "text": VISION_PROMPT},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
                        ]
                    }],
                    max_tokens=500,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                text = response.choices[0].message.content.strip()

            else:  # gemini
                from google.genai import types
                img = Image.open(image_path)
                response = gemini_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[VISION_PROMPT, img],
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        response_mime_type="application/json"
                    )
                )
                text = response.text.strip()

            return fix_and_parse_json(text)

        except Exception as exc:
            exc_str = str(exc).lower()
            # If rate limit occurs, wait 30s and try again until it succeeds
            if "429" in exc_str or "rate" in exc_str or "request" in exc_str or "tpm" in exc_str or "limit" in exc_str:
                print(f"  [Rate Limit] {Path(image_path).name} hit limit - Thread waiting 30s before retry (Attempt {attempt})...")
                time.sleep(30)
                attempt += 1
                continue
            
            print(f"  AI error on {image_path}: {exc}")
            return {"skip": True, "skip_reason": f"AI error: {exc}"}
