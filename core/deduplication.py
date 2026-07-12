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
    USE_NAME_DEDUP,
    NAME_DEDUP_THRESHOLD,
    USE_VISUAL_VERIFY,
    OPENAI_MODEL,
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
    "interior",
    "detail",
    "view",
    "part",
    "parts",
    "accessory",
    "accessories",
    "close",
    "inside",
    "engine",
    "motor",
    "dashboard",
    "wheel",
    "seating",
    "seat",
    "controls",
    "plug",
    "plugs",
    "latch",
    "stereo",
    "speaker",
    "speakers",
    "remote",
    "cable",
    "cables",
    "attachment",
    "attachments",
    "keyboard",
    "monitor",
    "screen",
    "charger",
    "battery",
    "batteries",
    "headset",
    "headphones",
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
    ai = item["ai"]
    name = (ai.get("item_name") or "").lower()
    group = (ai.get("item_group") or "").lower()
    confidence = float(ai.get("confidence", 0))
    score = confidence

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
    "- SAME object photographed under different lighting conditions (flash vs ambient,\n"
    "  warm vs cool light, daylight vs artificial). Color shifts from lighting do NOT\n"
    "  make items different — a sauna that looks golden in one photo and brown in\n"
    "  another due to lighting is still the SAME sauna.\n"
    "- SAME object from a similar angle but with different white balance or exposure\n"
    "- A photo of an item + a photo of its price tag, label, or maker's mark\n"
    "- A generic description + a specific name for the same item\n"
    "  Example: 'Floral Painting Print' near 'Jean Robie A Still Life of Roses' = SAME painting\n"
    "- Built-in components of a single unit\n"
    "  Example: 'Magnavox Turntable' + 'Magnavox Radio' + 'Stereo Console Cabinet' = all ONE console\n"
    "- Identical matching multiples (e.g. 4 matching dining chairs)\n"
    "- Items with the same or very similar names that also share the same room/background\n\n"
    "DO NOT group these (they are different buying opportunities):\n"
    "- Two different pieces of furniture that happen to be near each other\n"
    "- Two different paintings or art pieces (even if both are floral)\n"
    "- Items that are merely the same category but are clearly separate objects\n"
    "- Two items with similar names but in DIFFERENT rooms or with clearly different features\n\n"
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
            ai = r["ai"]
            name = ai.get("item_name", "Unknown")
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
                        content.append(
                            {"type": "image_url", "image_url": {"url": img_b64}}
                        )
            content.append({"type": "text", "text": "\n" + manifest})

            response = openai_client.chat.completions.create(
                model=OPENAI_MODEL,
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
                            b64_data = (
                                img_b64.split(",", 1)[1] if "," in img_b64 else img_b64
                            )
                            img_bytes = base64.b64decode(b64_data)
                            img = Image.open(BytesIO(img_bytes))
                            contents.append(img)
                        except Exception:
                            pass
            contents.append("\n" + manifest)

            response = gemini_client.models.generate_content(
                model="gemini-3.5-flash",
                contents=contents,
            )
            return response.text.strip()

    def _call_with_retry(
        manifest: str, images: list[str] = None, offset: int = 0, max_retries: int = 4
    ) -> str:
        delay = 15
        for attempt in range(max_retries):
            try:
                return _call_ai(manifest, images=images, offset=offset)
            except Exception as exc:
                exc_str = str(exc).lower()
                if (
                    "429" in exc_str
                    or "rate" in exc_str
                    or "tpm" in exc_str
                    or "limit" in exc_str
                ) and attempt < max_retries - 1:
                    print(
                        f"  [AI dedup] Rate limited — waiting {delay}s before retry..."
                    )
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

    # Use overlapping batches so items at boundaries get compared.
    # The union-find handles redundant union operations gracefully.
    overlap = min(5, batch_size // 4)
    stride = batch_size - overlap

    for batch_num in range(num_batches):
        start = batch_num * stride
        end = min(start + batch_size, len(results))
        if start >= len(results):
            break
        batch = results[start:end]

        manifest = _make_manifest(batch, offset=start)

        images = []
        if USE_VISION_DEDUP:
            for r in batch:
                images.append(r.get("thumb", ""))

        try:
            text = _call_with_retry(
                manifest, images=images if USE_VISION_DEDUP else None, offset=start
            )
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
            print(
                f"  [AI dedup] Batch {batch_num + 1}/{num_batches} processed ({len(batch)} items)"
            )

        except Exception as exc:
            print(
                f"  [AI dedup] Batch {batch_num + 1}/{num_batches} failed — items kept as-is"
            )

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


# ---------------------------------------------------------------------------
# Post-dedup: name-similarity candidate detection
# ---------------------------------------------------------------------------


def _deep_normalize(name: str) -> str:
    """
    Aggressively normalize an item name for candidate detection.

    Strips style, colour, material, and size adjectives so that items like
    ``"Vintage Wooden Kitchen Hutch"`` and ``"Wooden Kitchen Hutch Cabinet"``
    share the same normalised key.  Words are alpha-sorted for
    order-independent matching.
    """
    name = name.lower().strip()
    # Strip common style/era prefixes
    name = re.sub(
        r"\b(vintage|modern|mid-century|antique|retro|classic|contemporary)\b",
        "", name,
    )
    # Strip size words
    name = re.sub(r"\b(large|small|big|little|tall|short|mini|giant)\b", "", name)
    # Strip colour words
    name = re.sub(
        r"\b(white|black|red|blue|green|gray|grey|gold|silver|brass|chrome|"
        r"pink|yellow|brown|beige|navy|amber|purple|orange|clear|frosted)\b",
        "", name,
    )
    # Strip material words
    name = re.sub(
        r"\b(wooden|wood|metal|iron|glass|ceramic|crystal|porcelain|stoneware|"
        r"woven|fabric|leather|velvet|rattan|jute|plastic|wrought|bamboo|teak|"
        r"marble|granite|concrete|steel|copper|tin|brass|chrome|wire|wicker)\b",
        "", name,
    )
    # Remove non-alpha characters (digits, punctuation) and collapse whitespace
    name = re.sub(r"[^a-z\s]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Sort words for order-independent matching
    return " ".join(sorted(name.split()))


def find_name_candidates(results: list) -> list[list[int]]:
    """
    Identify groups of items with similar names (candidate merge groups).

    This function does **NOT** merge — it only produces candidates for
    :func:`verify_candidates_visually` to confirm or reject.

    Parameters
    ----------
    results:
        The post-AI-dedup item list.

    Returns
    -------
    list[list[int]]
        Each inner list contains indices into *results* that have similar
        names and should be visually verified.
    """
    from collections import defaultdict

    norms = [(i, _deep_normalize(r["ai"].get("item_name", ""))) for i, r in enumerate(results)]

    # --- Exact normalised match ---
    exact_groups: dict[str, list[int]] = defaultdict(list)
    for i, norm in norms:
        if norm:
            exact_groups[norm].append(i)

    # Collect groups with 2+ members; mark those indices as "grouped"
    grouped_indices: set[int] = set()
    candidate_groups: list[list[int]] = []
    for key, indices in exact_groups.items():
        if len(indices) >= 2:
            candidate_groups.append(indices)
            grouped_indices.update(indices)

    # --- Fuzzy match on remaining items ---
    ungrouped = [(i, norm) for i, norm in norms if i not in grouped_indices and norm]

    for idx_a in range(len(ungrouped)):
        i_a, norm_a = ungrouped[idx_a]
        if i_a in grouped_indices:
            continue
        fuzzy_group = [i_a]
        for idx_b in range(idx_a + 1, len(ungrouped)):
            i_b, norm_b = ungrouped[idx_b]
            if i_b in grouped_indices:
                continue
            sim = _similarity(norm_a, norm_b)
            if sim >= NAME_DEDUP_THRESHOLD:
                fuzzy_group.append(i_b)
        if len(fuzzy_group) >= 2:
            candidate_groups.append(fuzzy_group)
            grouped_indices.update(fuzzy_group)

    return candidate_groups


# ---------------------------------------------------------------------------
# Post-dedup: AI visual verification of name-matched candidates
# ---------------------------------------------------------------------------

_VERIFY_PROMPT = (
    "You are checking whether these estate sale photos show the SAME physical "
    "item or DIFFERENT items that happen to have similar names.\n\n"
    "SAME ITEM indicators:\n"
    "- Same shape, proportions, and distinctive features\n"
    "- Same room/background (even if lighting or angle differs)\n"
    "- One is a detail/close-up/tag of the other\n"
    "- Same colour/finish/hardware despite different camera white balance\n\n"
    "DIFFERENT ITEM indicators:\n"
    "- Different rooms, walls, or floor backgrounds\n"
    "- Different proportions, hardware, handles, or distinctive marks\n"
    "- Clearly two separate pieces of furniture/objects placed in different locations\n\n"
    "Items shown:\n{manifest}\n\n"
    "Return ONLY a JSON object with a 'subgroups' key — an array of arrays.\n"
    "Each inner array contains item indices that depict the SAME physical item.\n"
    "Items that are unique should appear alone in their own sub-array.\n"
    "Example: {{\"subgroups\": [[0, 2], [1], [3, 4]]}}\n"
    "No explanation, no markdown."
)


def _call_verify_ai(manifest: str, images: list[str] | None = None) -> str:
    """Send a verification request to the AI provider."""
    import time

    prompt = _VERIFY_PROMPT.format(manifest=manifest)
    delay = 15

    for attempt in range(4):
        try:
            if AI_PROVIDER == "openai":
                content = [{"type": "text", "text": prompt}]
                if images:
                    for idx, img_b64 in enumerate(images):
                        content.append({"type": "text", "text": f"\nItem {idx}:"})
                        if img_b64:
                            content.append(
                                {"type": "image_url", "image_url": {"url": img_b64}}
                            )
                response = openai_client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[{"role": "user", "content": content}],
                    max_tokens=300,
                )
                return response.choices[0].message.content.strip()
            else:  # gemini
                import base64 as b64mod
                from io import BytesIO
                from PIL import Image

                contents = [prompt]
                if images:
                    for idx, img_b64 in enumerate(images):
                        contents.append(f"\nItem {idx}:")
                        if img_b64:
                            try:
                                b64_data = (
                                    img_b64.split(",", 1)[1]
                                    if "," in img_b64
                                    else img_b64
                                )
                                img_bytes = b64mod.b64decode(b64_data)
                                img = Image.open(BytesIO(img_bytes))
                                contents.append(img)
                            except Exception:
                                pass
                response = gemini_client.models.generate_content(
                    model="gemini-3.5-flash",
                    contents=contents,
                )
                return response.text.strip()

        except Exception as exc:
            exc_str = str(exc).lower()
            if (
                "429" in exc_str
                or "rate" in exc_str
                or "tpm" in exc_str
                or "limit" in exc_str
            ) and attempt < 3:
                print(f"  [verify] Rate limited — waiting {delay}s before retry...")
                time.sleep(delay)
                delay *= 2
                continue
            raise


def verify_candidates_visually(
    results: list,
    candidate_groups: list[list[int]],
) -> tuple[list, list[dict], list[dict]]:
    """
    Visually verify name-matched candidate groups with the AI.

    For each candidate group the AI decides whether the items are the
    *same physical object* or *different objects with similar names*.

    Parameters
    ----------
    results:
        Full item list (indices in *candidate_groups* point into this).
    candidate_groups:
        Groups of indices from :func:`find_name_candidates`.

    Returns
    -------
    (merged_results, merge_log, similar_flags)
        *merged_results* — final item list with confirmed duplicates merged.
        *merge_log* — list of dicts describing each merge performed.
        *similar_flags* — list of dicts describing items flagged as similar
        but kept separate.
    """
    import time

    merge_log: list[dict] = []
    similar_flags: list[dict] = []

    # Track which indices have been merged away
    merged_away: set[int] = set()
    # Track merge-into relationships: merged_into[victim_idx] = survivor_idx
    merged_into: dict[int, int] = {}

    for group_idx, group in enumerate(candidate_groups):
        if len(group) < 2:
            continue

        group_items = [results[i] for i in group]
        group_names = [
            item["ai"].get("item_name", "Unknown") for item in group_items
        ]

        # Build manifest for the AI
        manifest_lines = []
        for local_i, (global_i, item) in enumerate(zip(group, group_items)):
            name = item["ai"].get("item_name", "Unknown")
            group_label = item["ai"].get("item_group", "")
            manifest_lines.append(f"{local_i}: {name} [{group_label}]")
        manifest = "\n".join(manifest_lines)

        if USE_VISUAL_VERIFY:
            # Send thumbnails + manifest to AI for verification
            images = [item.get("thumb", "") for item in group_items]
            try:
                text = _call_verify_ai(manifest, images=images)
                parsed = fix_and_parse_json(text)
                subgroups = parsed.get("subgroups", [])

                # Process each verified subgroup
                for sg in subgroups:
                    if len(sg) < 2:
                        continue
                    # Map local indices back to global indices
                    global_indices = []
                    for local_idx in sg:
                        if 0 <= local_idx < len(group):
                            global_indices.append(group[local_idx])
                    if len(global_indices) < 2:
                        continue

                    # Merge: pick best representative, absorb others
                    sg_items = [results[gi] for gi in global_indices]
                    survivor = _best_in_group(sg_items)
                    survivor_idx = global_indices[
                        sg_items.index(survivor)
                    ]

                    # Track the merge count for the badge
                    survivor["_post_dedup_grouped"] = (
                        survivor.get("_post_dedup_grouped", 0)
                        + len(global_indices)
                        - 1
                    )

                    for gi in global_indices:
                        if gi != survivor_idx:
                            merged_away.add(gi)
                            merged_into[gi] = survivor_idx

                    merged_names = [results[gi]["ai"].get("item_name", "?") for gi in global_indices]
                    merge_log.append({
                        "names": merged_names,
                        "survivor_idx": survivor_idx,
                        "merged_count": len(global_indices),
                    })
                    print(f"  [post-dedup] Merged (AI verified): {' + '.join(merged_names)}")

                # Items in the group that ended up in singleton subgroups
                # → flag as "similar but different"
                singleton_global = []
                for sg in subgroups:
                    if len(sg) == 1 and 0 <= sg[0] < len(group):
                        singleton_global.append(group[sg[0]])

                if len(singleton_global) >= 2:
                    for gi in singleton_global:
                        other_names = [
                            results[ogi]["ai"].get("item_name", "?")
                            for ogi in singleton_global
                            if ogi != gi
                        ]
                        if gi not in merged_away:
                            results[gi].setdefault("_similar_items", []).extend(other_names)
                    similar_flags.append({
                        "names": [results[gi]["ai"].get("item_name", "?") for gi in singleton_global],
                    })
                    print(
                        f"  [post-dedup] Flagged similar (kept separate): "
                        f"{[results[gi]['ai'].get('item_name', '?') for gi in singleton_global]}"
                    )

            except Exception as exc:
                print(f"  [post-dedup] Verify error for group {group_idx}: {exc}")
                # On error, flag all as similar but don't merge (safe mode)
                for gi in group:
                    other_names = [
                        results[ogi]["ai"].get("item_name", "?")
                        for ogi in group
                        if ogi != gi
                    ]
                    results[gi].setdefault("_similar_items", []).extend(other_names)
                similar_flags.append({"names": group_names, "error": str(exc)})

        else:
            # Visual verification disabled — flag all candidates, never merge
            for gi in group:
                other_names = [
                    results[ogi]["ai"].get("item_name", "?")
                    for ogi in group
                    if ogi != gi
                ]
                results[gi].setdefault("_similar_items", []).extend(other_names)
            similar_flags.append({"names": group_names, "mode": "flag_only"})
            print(
                f"  [post-dedup] Flagged (no visual verify): "
                f"{group_names}"
            )

    # Build final result list excluding merged-away items
    merged_results = [
        item for i, item in enumerate(results)
        if i not in merged_away
    ]

    return merged_results, merge_log, similar_flags


# ---------------------------------------------------------------------------
# Post-dedup entry point
# ---------------------------------------------------------------------------


def post_dedup_verify(results: list) -> tuple[list, list[dict], list[dict]]:
    """
    Run the post-deduplication name-similarity + visual verification pipeline.

    Parameters
    ----------
    results:
        Item list after the primary AI vision dedup pass.

    Returns
    -------
    (results, merge_log, similar_flags)
        *results* — final item list with confirmed duplicates merged.
        *merge_log* — list of dicts describing each merge.
        *similar_flags* — list of dicts describing items flagged as similar.
    """
    if not USE_NAME_DEDUP or len(results) <= 1:
        print("\n-- Post-dedup name grouping bypassed --")
        return results, [], []

    print(f"\n-- Post-dedup name-similarity pass ({len(results)} items) --")

    # Stage 1: Find candidates by name similarity
    candidates = find_name_candidates(results)
    total_candidate_items = sum(len(g) for g in candidates)
    print(
        f"  [post-dedup] Found {len(candidates)} candidate groups "
        f"({total_candidate_items} items)"
    )

    if not candidates:
        return results, [], []

    # Stage 2: Visual verification (or flag-only if disabled)
    results, merge_log, similar_flags = verify_candidates_visually(
        results, candidates,
    )

    print(
        f"  [post-dedup] Result: {len(merge_log)} merges, "
        f"{len(similar_flags)} flagged similar"
    )

    return results, merge_log, similar_flags
