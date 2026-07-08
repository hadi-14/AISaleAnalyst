"""
deduplication.py
================
Two-stage deduplication pipeline for AISaleAnalyst.

Stage 1 — Fuzzy match (:func:`deduplicate_fuzzy`)
    Groups items whose ``item_name`` labels are sufficiently similar
    (controlled by ``FUZZY_THRESHOLD`` in :mod:`config`).

Stage 2 — AI vision pass (:func:`deduplicate_ai`)
    Shows all remaining candidate images to the AI in a single request and
    asks it to group indices that depict the *exact same physical object*.
    Falls back to stage-1 results on any error.

Use :func:`deduplicate` to run both stages in sequence.
"""

import re
from difflib import SequenceMatcher

from .config import (
    AI_PROVIDER,
    USE_AI_DEDUP,
    USE_VISION_DEDUP,
    fix_and_parse_json,
)

if AI_PROVIDER == "openai":
    from .config import openai_client
else:
    from .config import gemini_client

# ---------------------------------------------------------------------------
# Internal scoring helpers
# ---------------------------------------------------------------------------

#: Words that indicate a detail/interior/partial shot/accessory — used to penalise
#: less-descriptive representatives when picking the best item in a group.
_DETAIL_WORDS = {
    "interior", "detail", "view", "part", "parts", "accessory",
    "accessories", "close", "inside", "engine", "motor", "dashboard",
    "wheel", "seating", "seat", "controls", "plug", "plugs", "latch",
    "stereo", "speaker", "speakers", "remote", "cable", "cables",
    "attachment", "attachments", "keyboard", "monitor", "screen",
    "charger", "battery", "batteries", "headset", "headphones",
}




def _normalize(s: str) -> str:
    """Lowercase, strip, and collapse internal whitespace."""
    return re.sub(r"\s+", " ", s.lower().strip())


def _similarity(a: str, b: str) -> float:
    """Return a 0–1 similarity ratio between two strings."""
    return SequenceMatcher(None, a, b).ratio()


def _get_item_descriptive_score(item: dict) -> float:
    """
    Score how *descriptive* and *primary* an item record is so we can pick the best
    representative from a deduplication group.

    Higher scores favour:
    - High AI confidence
    - Presence of digits (model numbers, years)
    - Title-cased words (brand names)

    Lower scores penalise:
    - Names or groups containing interior/detail/part/accessory words (preferring
      the main parent asset as the group representative).
    """
    ai         = item["ai"]
    name       = (ai.get("item_name") or "").lower()
    group      = (ai.get("item_group") or "").lower()
    confidence = float(ai.get("confidence", 0))
    score      = confidence

    # Penalise detail / partial-shot / accessory names and groups
    for word in _DETAIL_WORDS:
        if word in name or word in group:
            score -= 50

    # Reward model-number specificity
    if any(ch.isdigit() for ch in name):
        score += 15

    # Reward capitalised (brand/model) words
    orig_name = ai.get("item_name") or ""
    score += sum(1 for w in orig_name.split() if w.istitle()) * 2

    return score


def _best_in_group(items: list) -> dict:
    """Return the most descriptive item from a deduplication group, attaching other photos."""
    best = max(items, key=_get_item_descriptive_score)
    
    other_thumbs = []
    for item in items:
        if item is not best:
            if "thumb" in item:
                other_thumbs.append(item["thumb"])
            # Also pull in any nested thumbs if this was previously grouped
            if "other_thumbs" in item:
                other_thumbs.extend(item["other_thumbs"])
                
    if "other_thumbs" not in best:
        best["other_thumbs"] = []
    best["other_thumbs"].extend(other_thumbs)
    
    return best




# ---------------------------------------------------------------------------
# Stage 2: AI deduplication
# ---------------------------------------------------------------------------


_AI_BATCH_PROMPT = (
    "You are an estate sale photo organizer. Below are numbered items from an estate sale.\n"
    "The photos were taken in order as the photographer walked through the house.\n\n"
    "YOUR TASK: Group indices that represent the SAME buying opportunity.\n\n"
    "GROUP these together (they are the same buying opportunity):\n"
    "- Multiple angles of the SAME object (wide shot + close-up + detail + back view)\n"
    "- A photo of an item + a photo of its price tag, label, or maker's mark\n"
    "- A generic description + a specific name for the same item\n"
    "  Example: 'Floral Painting Print' near 'Jean Robie A Still Life of Roses' = SAME painting\n"
    "- Built-in components of a single unit\n"
    "  Example: 'Magnavox Turntable' + 'Magnavox Radio' + 'Stereo Console Cabinet' = all ONE console\n"
    "- Identical matching multiples (e.g. 4 matching dining chairs)\n\n"
    "DO NOT group these (they are different buying opportunities):\n"
    "- Two different pieces of furniture that happen to be near each other\n"
    "- Two different paintings or art pieces (even if both are floral)\n"
    "- Items that are merely the same category but are clearly separate objects\n\n"
    "KEY INSIGHT: Estate sale photographers typically take 2-5 photos per item in sequence:\n"
    "first a wide shot, then close-ups of details/labels/tags. Look for these sequential clusters.\n\n"
    "Every index must appear in exactly one group.\n"
    "Return ONLY a JSON array of arrays, e.g.: [[0,1,2],[3,4],[5]]\n"
    "No explanation, no markdown."
)

def deduplicate_ai(results: list, batch_size: int = 30) -> list:
    """
    Use the AI model to group items that depict the same physical object,
    then return one representative per group.
    """
    import time

    if len(results) <= 1:
        return results

    def _make_manifest(batch: list, offset: int) -> str:
        lines = []
        for local_i, r in enumerate(batch):
            ai    = r["ai"]
            name  = ai.get("item_name", "Unknown")
            group = ai.get("item_group", "")
            notes = ai.get("condition_notes", "")
            lines.append(f"{offset + local_i}: {name} [{group}] | Cond: {notes}")
        return "\n".join(lines)

    def _call_ai(manifest: str, images: list[str] = None, offset: int = 0) -> str:
        base_prompt = _AI_BATCH_PROMPT + "\n\nItems:"

        if AI_PROVIDER == "openai":
            content = [{"type": "text", "text": base_prompt}]
            if images:
                for i, img_b64 in enumerate(images):
                    content.append({"type": "text", "text": f"\nItem {offset + i}:"})
                    if img_b64:
                        content.append({"type": "image_url", "image_url": {"url": img_b64}})
            content.append({"type": "text", "text": "\n" + manifest})
            
            response = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": content}],
                max_tokens=500,
            )
            return response.choices[0].message.content.strip()
        else:  # gemini
            contents = [base_prompt]
            if images:
                import base64
                from io import BytesIO
                from PIL import Image
                for i, img_b64 in enumerate(images):
                    contents.append(f"\nItem {offset + i}:")
                    if img_b64:
                        try:
                            b64_data = img_b64.split(",", 1)[1] if "," in img_b64 else img_b64
                            img_bytes = base64.b64decode(b64_data)
                            img = Image.open(BytesIO(img_bytes))
                            contents.append(img)
                        except Exception:
                            pass
            contents.append("\n" + manifest)

            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
            )
            return response.text.strip()

    def _call_with_retry(manifest: str, images: list[str] = None, offset: int = 0, max_retries: int = 4) -> str:
        delay = 15
        for attempt in range(max_retries):
            try:
                return _call_ai(manifest, images=images, offset=offset)
            except Exception as exc:
                exc_str = str(exc).lower()
                if ("429" in exc_str or "rate" in exc_str or "tpm" in exc_str or "limit" in exc_str) and attempt < max_retries - 1:
                    print(f"  [AI dedup] Rate limited — waiting {delay}s before retry...")
                    time.sleep(delay)
                    continue
                print(f"  [AI dedup] Batch error: {exc}")
                raise

    parent: list[int] = list(range(len(results)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    num_batches = (len(results) + batch_size - 1) // batch_size
    any_batch_succeeded = False

    for batch_num in range(num_batches):
        start  = batch_num * batch_size
        end    = min(start + batch_size, len(results))
        batch  = results[start:end]

        manifest = _make_manifest(batch, offset=start)
        
        images = []
        if USE_VISION_DEDUP:
            for r in batch:
                images.append(r.get("thumb", ""))
                
        try:
            text   = _call_with_retry(manifest, images=images if USE_VISION_DEDUP else None, offset=start)
            from .config import fix_and_parse_json
            groups = fix_and_parse_json(text)

            seen: set[int] = set()
            for g in groups:
                if not g:
                    continue
                root_global = start + g[0]
                for local_idx in g[1:]:
                    global_idx = start + local_idx
                    if not (0 <= local_idx < len(batch)):
                        continue
                    union(root_global, global_idx)
                    seen.add(global_idx)
                seen.add(root_global)

            any_batch_succeeded = True
            print(f"  [AI dedup] Batch {batch_num + 1}/{num_batches} processed ({len(batch)} items)")

        except Exception as exc:
            print(f"  [AI dedup] Batch {batch_num + 1}/{num_batches} failed — items kept as-is")

    if not any_batch_succeeded:
        print("  [AI dedup] All batches failed — keeping fuzzy results")
        return results

    group_map: dict[int, list] = {}
    for i, item in enumerate(results):
        root = find(i)
        group_map.setdefault(root, []).append(item)

    # Log multi-item groups for debugging
    for members in group_map.values():
        if len(members) > 1:
            names = [m["ai"].get("item_name", "?") for m in members]
            print(f"  [AI dedup] Grouped: {' + '.join(names)}")

    deduped = [_best_in_group(members) for members in group_map.values()]
    print(f"  [AI dedup]    {len(results)} images -> {len(deduped)} unique items")
    return deduped


# ---------------------------------------------------------------------------
# Combined pipeline
# ---------------------------------------------------------------------------


def deduplicate(results: list) -> list:
    """
    Run the deduplication pipeline (AI pass only).

    Parameters
    ----------
    results:
        Raw item list from the AI vision pass.

    Returns
    -------
    list
        Deduplicated item list ready for eBay scraping.
    """
    if USE_AI_DEDUP and len(results) > 1:
        print("\n-- Deduplication (AI only) --")
        return deduplicate_ai(results)
    
    print("\n-- Deduplication bypassed (USE_AI_DEDUP=False) --")
    return results
