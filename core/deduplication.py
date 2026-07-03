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

#: System prompt for the AI deduplication pass.
_AI_DEDUP_PROMPT = (
    "Below are numbered items from an estate sale, each identified by AI from a photo.\n\n"
    "Task: Group the INDICES (0-based) that depict the EXACT SAME physical object shown "
    "from different angles or distances, OR accessories/components that physically belong "
    "to one main asset being sold as a bundle.\n\n"
    "THE CORE RULE — memorise this:\n"
    "  Group = same physical object, multiple photos.\n"
    "  Do NOT group = different objects, even if same type, brand, material, or style.\n\n"
    "Specific rules:\n"
    "- SAME OBJECT / DIFFERENT VIEW: Only group indices when every photo in the group "
    "literally shows the same physical object from a different angle, distance, or lighting. "
    "Ask yourself: 'Could I place all these photos in the same eBay listing for one item?' "
    "If yes, group them. If not, they are separate groups.\n"
    "- ACCESSORIES & ATTACHMENTS: Group any accessory, attachment, component, or installed "
    "part WITH its single main parent asset.\n"
    "- USE CONDITION NOTES: The condition notes often contain specific visual details. If two items have similar names but clearly different visual condition notes (e.g., one is in a box, one is rusty), they are DIFFERENT items. Do NOT group them.\n"
    "- STRICTNESS IS REQUIRED: If you are not 100% certain two items are the exact same physical object based on their names and conditions, DO NOT group them! Over-grouping destroys data. It is perfectly fine if every item remains in its own group of 1.\n"
    "- Every index must appear in exactly one group.\n\n"
    "Return ONLY a JSON array of arrays, e.g.: [[0,1,2,5],[3,4],[6]]\n"
    "No explanation, no markdown."
)


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


def deduplicate_ai(results: list, batch_size: int = 30) -> list:
    """
    Use the AI model to group items that depict the same physical object,
    then return one representative per group.

    Items are processed in text-only batches (item name + group label) to
    stay well under token-per-minute limits.  Images are NOT sent — the
    AI-generated labels already carry enough signal for deduplication.
    Falls back to returning ``results`` unchanged if every batch fails.

    Parameters
    ----------
    results:
        Output of :func:`deduplicate_fuzzy`.
    batch_size:
        Maximum number of items per AI call (default 30 keeps well under
        the 30 K TPM limit even with a large system prompt).

    Returns
    -------
    list
        Further-deduplicated list (one representative per physical object).
    """
    import time

    if len(results) <= 1:
        return results

    # ------------------------------------------------------------------ #
    # Build a compact text manifest — no images, just names & groups      #
    # ------------------------------------------------------------------ #
    def _make_manifest(batch: list, offset: int) -> str:
        lines = []
        for local_i, r in enumerate(batch):
            ai    = r["ai"]
            name  = ai.get("item_name", "Unknown")
            group = ai.get("item_group", "")
            notes = ai.get("condition_notes", "")
            lines.append(f"{offset + local_i}: {name} [{group}] | Cond: {notes}")
        return "\n".join(lines)

    def _call_ai(manifest: str) -> str:
        """Send one batch to the active AI provider; returns raw response text."""
        prompt = _AI_DEDUP_PROMPT + "\n\nItems:\n" + manifest

        if AI_PROVIDER == "openai":
            response = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400,
            )
            return response.choices[0].message.content.strip()
        else:  # gemini
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[prompt],
            )
            return response.text.strip()

    def _call_with_retry(manifest: str, max_retries: int = 5) -> str:
        delay = 65
        for attempt in range(max_retries):
            try:
                return _call_ai(manifest)
            except Exception as exc:
                exc_str = str(exc).lower()
                if ("429" in exc_str or "rate" in exc_str or "tpm" in exc_str or "limit" in exc_str) and attempt < max_retries - 1:
                    print(f"  [AI dedup] Rate limited — waiting {delay}s before retry {attempt + 2}/{max_retries}...")
                    time.sleep(delay)
                    continue
                raise

    # ------------------------------------------------------------------ #
    # Process in batches; collect (global_index → group_id) mapping       #
    # ------------------------------------------------------------------ #
    # global_group_id for each item — items that should merge share the   #
    # same id.  Start with each item in its own group.                    #
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
        try:
            text   = _call_with_retry(manifest)
            groups = fix_and_parse_json(text)

            # Validate and union-find within this batch
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
            print(f"  [AI dedup] Batch {batch_num + 1}/{num_batches} failed ({exc}) — items kept as-is")

    if not any_batch_succeeded:
        print("  [AI dedup] All batches failed — keeping fuzzy results")
        return results

    # ------------------------------------------------------------------ #
    # Collapse union-find groups → pick best representative per group     #
    # ------------------------------------------------------------------ #
    group_map: dict[int, list] = {}
    for i, item in enumerate(results):
        root = find(i)
        group_map.setdefault(root, []).append(item)

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
