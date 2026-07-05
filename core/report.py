"""
report.py
=========
HTML report generator for AISaleAnalyst.

Generates a self-contained HTML file ranking estate-sale items by the
configured sort metric (ROI by default).  Each row includes a thumbnail,
item name, estimated resale price, profit, ROI percentage, AI confidence
bar, platform badge, and a link to eBay sold comps.

Public API
----------
generate_report(items, output_path)
    Write the HTML report to *output_path*.
"""

from datetime import datetime

from .config import AI_PROVIDER, SORT_BY, TOP_N
from .financials import calc_financials, get_sort_key, tier

# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------


def build_row(rank: int, item: dict) -> str:
    """
    Render a single HTML table row for *item*.

    Parameters
    ----------
    rank:
        1-based display rank number.
    item:
        Fully-calculated item dict containing ``"ai"``, ``"comps"``,
        ``"financials"``, and optionally ``"thumb"`` keys.

    Returns
    -------
    str
        An HTML ``<tr>...</tr>`` string.
    """
    ai        = item["ai"]
    comps     = item["comps"]
    fin       = item["financials"]
    
    sell_price          = fin["sell_price"]
    ebay_fee            = fin["ebay_fee"]
    shipping            = fin["shipping"]
    net_after_fees      = fin["net_after_fees"]
    recommended_max_buy = fin["recommended_max_buy"]
    buy_price           = fin["buy_price"]
    profit              = fin["profit"]
    roi                 = fin["roi"]
    
    conf      = fin.get("adjusted_confidence", ai.get("confidence", 0))
    bar_width = int(conf * 0.6)           # max bar ≈ 60 px at 100 %
    t_cls, _  = tier(roi)
    comp_link = comps.get("link", "#")
    thumb = item.get("thumb", "")
    other_thumbs = item.get("other_thumbs", [])
    
    if thumb:
        thumbs_html = f'<div class="img-wrapper main"><img src="{thumb}"></div>'
        for ot in other_thumbs[:4]: # Cap at 4 additional thumbs to keep UI clean
            thumbs_html += f'<div class="img-wrapper sec"><img src="{ot}"></div>'
        
        img_tag = f'<div class="thumb-gallery">{thumbs_html}</div>'
    else:
        img_tag = "—"

    # Condition badge
    cond_text = ai.get("ebay_condition") or "Used"
    cond_badge = f'<span style="display:inline-block;background:#f3f4f6;color:#374151;font-size:10px;font-weight:bold;padding:2px 6px;border-radius:4px;margin-right:8px;">{cond_text}</span>'

    # Badges for match status
    badges = []
    
    # Multi-item flag
    if ai.get("multi_item_detected"):
        badges.append('<span style="display:inline-block;background:#fce7f3;color:#be185d;font-size:10px;font-weight:bold;padding:2px 6px;border-radius:4px;margin-top:6px;margin-right:6px;">📦 Multi-Item Photo</span>')

    # Check if exact model was not identified
    if ai.get("exact_model_identified") is False:
        badges.append('<span style="display:inline-block;background:#fef3c7;color:#d97706;font-size:10px;font-weight:bold;padding:2px 6px;border-radius:4px;margin-top:6px;margin-right:6px;">⚠️ Exact model details not identified</span>')

    # Check if fallback query was used
    if comps.get("fallback_used"):
        badges.append('<span style="display:inline-block;background:#eff6ff;color:#1d4ed8;font-size:10px;font-weight:bold;padding:2px 6px;border-radius:4px;margin-top:6px;margin-right:6px;">ℹ️ Based on similar model</span>')
        
    # Check if confidence is low or zero comps found
    if comps.get("count", 0) == 0:
        badges.append('<span style="display:inline-block;background:#fef2f2;color:#b91c1c;font-size:10px;font-weight:bold;padding:2px 6px;border-radius:4px;margin-top:6px;margin-right:6px;">⚠️ Valuation estimate only (0 comps)</span>')
    elif conf < 70:
        badges.append('<span style="display:inline-block;background:#fffbeb;color:#b45309;font-size:10px;font-weight:bold;padding:2px 6px;border-radius:4px;margin-top:6px;margin-right:6px;">⚠️ Low confidence match</span>')
        
    badge_html = "".join(badges)

    # Comps verification links
    link_buttons = []
    
    if comp_link and comp_link != "#":
        link_buttons.append(f'<a href="{comp_link}" target="_blank" style="display:inline-block;margin:3px;font-size:11px;color:#ffffff;text-decoration:none;background:#2563eb;padding:4px 8px;border-radius:4px;font-weight:bold;">All Comps</a>')
        
    link_html = "".join(link_buttons) if link_buttons else "—"

    # Number of sold listings
    count_text = f"Based on {comps['count']} sold listings" if comps['count'] > 0 else "No comps found"

    # Color class for profit
    profit_color = "#1c7a3a" if profit >= 0 else "#b91c1c"

    # Extract resale reasons & search query
    resale_reasons = ai.get("resale_reasons") or ""
    query_used     = comps.get("query_used") or ai.get("ebay_search_query") or ""

    resale_html = f'<div style="font-size:11px;color:#047857;font-weight:bold;margin-top:6px;">💡 Resale Appeal: {resale_reasons}</div>' if resale_reasons else ''
    query_html  = f'<div style="font-size:11px;color:#4b5563;margin-top:4px;">🔍 Search Query: <span style="font-family:monospace;background:#f3f4f6;padding:2px 6px;border-radius:3px;">{query_used}</span></div>' if query_used else ''

    # Package dimensions display
    pkg_l  = ai.get("pkg_length_in")
    pkg_w  = ai.get("pkg_width_in")
    pkg_h  = ai.get("pkg_height_in")
    pkg_wt = ai.get("pkg_weight_lb")
    dims_html = ""
    if pkg_l is not None and pkg_w is not None and pkg_h is not None and pkg_wt is not None:
        if pkg_l > 0 or pkg_w > 0 or pkg_h > 0 or pkg_wt > 0:
            dims_html = f'<div style="font-size:11px;color:#4b5563;margin-top:4px;">📦 Package: <span style="font-family:monospace;background:#f3f4f6;padding:2px 6px;border-radius:3px;">{pkg_l}x{pkg_w}x{pkg_h} in | {pkg_wt} lbs</span></div>'

    shipping_carrier = fin.get("shipping_carrier", "Estimated")
    shipping_service = fin.get("shipping_service", "")
    shipping_est_days = fin.get("shipping_est_days")
    
    # Format a nice sub-label under shipping
    shipping_desc = ""
    if shipping_carrier and shipping_service:
        days_str = f" ({shipping_est_days}d)" if shipping_est_days else ""
        shipping_desc = f'<div style="font-size:10px;color:#6b7280;text-align:left;margin-top:2px;">{shipping_carrier} {shipping_service}{days_str}</div>'

    ai_low = ai.get('ai_value_low')
    ai_high = ai.get('ai_value_high')
    ai_val_html = ""
    if ai_low is not None and ai_high is not None:
        ai_val_html = f'<div style="font-size:11px;color:#4b5563;margin-top:6px;padding-top:4px;border-top:1px dashed #e5e7eb;" title="Raw estimate from the vision AI">AI Est: <span style="font-weight:bold;">${ai_low} – ${ai_high}</span></div>'

    return f"""
    <tr>
      <td class="rank">{rank}</td>
      <td class="center">{img_tag}</td>
      <td>
        <div class="item-name" style="font-size:14px; margin-bottom:4px;">{ai.get('item_name', 'Unknown')}</div>
        <div style="margin-top:4px;">{cond_badge}<span class="item-notes" style="color:#666;font-size:12px;">{ai.get('condition_notes', '')}</span></div>
        {resale_html}
        {query_html}
        {dims_html}
        <div>{badge_html}</div>
      </td>
      <td class="center">
        <div style="font-weight:bold;font-size:14px;">${sell_price:.0f}</div>
        <div style="font-size:11px;color:#888;margin-top:4px;">eBay: {comps['low']} – {comps['high']}</div>
        <div style="font-size:10px;color:#999;margin-top:2px;">{count_text}</div>
        {ai_val_html}
      </td>
      <td class="center">
        <div style="font-size:12px;color:#555;text-align:left;">eBay Fee: <span style="font-weight:bold;float:right;">-${ebay_fee:.2f}</span></div>
        <div style="font-size:12px;color:#555;text-align:left;margin-top:3px;">Shipping: <span style="font-weight:bold;float:right;">-${shipping:.2f}</span></div>
        {shipping_desc}
        <div style="font-size:12px;font-weight:bold;color:#111;text-align:left;margin-top:4px;border-top:1px dashed #ddd;padding-top:4px;">Net: <span style="float:right;">${net_after_fees:.2f}</span></div>
      </td>
      <td class="center">
        <div style="font-weight:bold;font-size:14px;color:#b45309;">${recommended_max_buy:.0f}</div>
        <div style="font-size:11px;color:#888;margin-top:4px;">Est. Buy: ${buy_price:.0f}</div>
      </td>
      <td class="center">
        <div style="font-weight:bold;font-size:14px;color:{profit_color};">${profit:.0f}</div>
        <div style="font-weight:bold;font-size:12px;color:#a07000;margin-top:4px;">{roi:.0f}% ROI</div>
      </td>
      <td class="center">
        <div class="conf-wrap">
          <span class="conf-val">{conf}%</span>
          <div class="bar-bg"><div class="bar-fill" style="width:{bar_width}px"></div></div>
        </div>
      </td>
      <td class="center">
        <div style="display:flex;flex-direction:column;align-items:center;gap:3px;">
          {link_html}
        </div>
      </td>
    </tr>"""


def build_skipped_row(rank: int, item: dict) -> str:
    """
    Render a single HTML table row for a skipped image.
    """
    from pathlib import Path
    
    ai          = item.get("ai", {})
    image_path  = item.get("image", "Unknown")
    file_name   = Path(image_path).name if image_path != "Unknown" else "Unknown"
    thumb       = item.get("thumb", "")
    
    img_tag = (
        f'<div class="thumb-gallery"><div class="img-wrapper main"><img src="{thumb}"></div></div>'
        if thumb else "—"
    )
    
    notes = ai.get("ai_value_notes") or ai.get("condition_notes") or "Skipped (blurry, dark, empty, or structural view)"
    
    return f"""
    <tr>
      <td class="rank">{rank}</td>
      <td class="center">{img_tag}</td>
      <td>
        <div style="font-weight:bold;font-size:13px;">{file_name}</div>
        <div style="font-size:11px;color:#888;margin-top:2px;">{image_path}</div>
      </td>
      <td>
        <div style="display:inline-block;background:#fef2f2;color:#b91c1c;font-size:9px;font-weight:bold;padding:1px 5px;border-radius:3px;">📷 Photo Skipped</div>
        <div style="font-size:11px;color:#555;margin-top:4px;">{notes}</div>
      </td>
    </tr>"""


# ---------------------------------------------------------------------------
# Report generator
# ---------------------------------------------------------------------------

_THEAD_SKIPPED = """\
<tr>
  <th class="center" style="width:36px">#</th>
  <th class="center" style="width:90px">Photo</th>
  <th>File Path</th>
  <th>Reason / AI Assessment</th>
</tr>"""

_THEAD = """\
<tr>
  <th class="center" style="width:40px">#</th>
  <th class="center" style="width:100px">Photo</th>
  <th>Item Details</th>
  <th class="center" style="width:120px">Expected Resale</th>
  <th class="center" style="width:180px">Fees & Shipping</th>
  <th class="center" style="width:140px">Recommended Buy Limit</th>
  <th class="center" style="width:120px">Expected Net Return</th>
  <th class="center" style="width:120px">Match Confidence</th>
  <th class="center" style="width:140px">Verify Comps</th>
</tr>"""

_CSS = """\
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: Arial, sans-serif; font-size: 13px; background: #f9fafb; color: #1c1c1c; }
.header { background: #1c1c1c; color: #fff; padding: 24px 32px; }
.header h1 { font-size: 20px; font-weight: bold; letter-spacing: 0.5px; }
.header p { font-size: 12px; color: #aaa; margin-top: 6px; }
.summary { display: flex; border-bottom: 1px solid #e0e0e0; background: #fff; }
.stat { flex: 1; padding: 18px 24px; border-right: 1px solid #e0e0e0; }
.stat:last-child { border-right: none; }
.stat .val { font-size: 24px; font-weight: bold; color: #111; }
.stat .lbl { font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.6px; margin-top: 4px; }
.section-title { background: #f3f4f6; padding: 16px 32px; font-size: 13px; font-weight: bold;
  text-transform: uppercase; letter-spacing: 1px; color: #4b5563; border-bottom: 1px solid #e5e7eb;
  border-top: 2px solid #1c1c1c; margin-top: 32px; }
.section-title.gold { border-top-color: #a07000; color: #a07000; background: #fefcf8; }
.section-title.skipped { border-top-color: #b91c1c; color: #b91c1c; background: #fef2f2; }
.sort-note { padding: 12px 32px; font-size: 12px; color: #6b7280; background: #fff; border-bottom: 1px solid #e5e7eb; }
.thumb-gallery { display: flex; flex-wrap: wrap; gap: 4px; justify-content: center; align-items: center; max-width: 90px; margin: 0 auto; }
.img-wrapper { border-radius: 4px; position: relative; }
.img-wrapper img { width: 100%; height: 100%; object-fit: cover; border-radius: 4px; cursor: zoom-in; transition: transform 0.2s cubic-bezier(0.25, 0.46, 0.45, 0.94); z-index: 1; position: relative; transform-origin: left center; }
.img-wrapper.main { width: 80px; height: 70px; }
.img-wrapper.sec { width: 38px; height: 38px; }
.img-wrapper img:hover { transform: scale(4.5); z-index: 1000; box-shadow: 0 15px 35px rgba(0,0,0,0.3); border-radius: 2px; }
table { width: 100%; border-collapse: collapse; background: #fff; }
thead th { background: #1f2937; color: #f9fafb; padding: 14px 16px; text-align: left;
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; font-weight: bold;
  position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 4px rgba(0,0,0,0.25); }
thead th.center { text-align: center; }
tbody tr { border-bottom: 1px solid #e5e7eb; }
tbody tr:nth-child(even) { background: #fafafa; }
tbody tr:hover { background: #f3f4f6; }
tbody td { padding: 18px 16px; vertical-align: middle; }
tbody td.center { text-align: center; }
.item-name { font-weight: bold; font-size: 14px; color: #111; }
.item-notes { font-size: 12px; color: #6b7280; margin-top: 2px; }
.rank { color: #d1d5db; font-weight: bold; font-size: 18px; text-align: center; }
.tier { font-size: 12px; font-weight: bold; }
.tier.high { color: #047857; }
.tier.med  { color: #a07000; }
.tier.low  { color: #9ca3af; }
.conf-wrap { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.conf-val { font-size: 13px; font-weight: bold; color: #374151; }
.bar-bg { width: 64px; height: 4px; background: #e5e7eb; border-radius: 2px; }
.bar-fill { height: 4px; background: #1f2937; border-radius: 2px; }
.footer { padding: 20px 32px; font-size: 11px; color: #9ca3af; border-top: 1px solid #e5e7eb; margin-top: 32px; text-align: center; }"""


def generate_report(items: list, output_path: str, skipped_items: list = None) -> None:
    """
    Calculate financials, sort by the configured key, and write a
    self-contained HTML report.

    Items that fail financial calculation or row rendering are skipped
    with a console warning rather than crashing the whole report.

    Parameters
    ----------
    items:
        List of item dicts.  Each must have ``"ai"`` and ``"comps"`` keys.
        A fallback N/A comps dict is injected for any item that is missing
        the ``"comps"`` key.
    output_path:
        File path where the HTML report will be written.
    skipped_items:
        List of image dicts that were skipped by the AI vision pass.
    """
    date_str = datetime.now().strftime("%B %d, %Y")
    skipped_items = skipped_items or []
    print(f"  [report] Building report for {len(items)} items ({len(skipped_items)} skipped)...")

    # --- Ensure every item has a comps dict (guard against scraper failures)
    _NA_COMPS = {"low": "N/A", "mean": "N/A", "high": "N/A", "count": 0, "link": ""}
    for item in items:
        if "comps" not in item:
            name = item.get("ai", {}).get("item_name", "?")
            print(f"  [report] Warning: missing comps for '{name}' - using N/A")
            item["comps"] = _NA_COMPS.copy()

    # --- Calculate financials with per-item error handling
    good_items: list = []
    for item in items:
        try:
            item["financials"] = calc_financials(item)
            good_items.append(item)
        except Exception as exc:
            name = item.get("ai", {}).get("item_name", "?")
            print(f"  [report] Skipping '{name}' - financials error: {exc}")

    items = sorted(good_items, key=get_sort_key, reverse=True)
    total = len(items)

    high_count   = sum(1 for i in items if tier(i["financials"]["roi"])[0] == "high")
    med_count    = sum(1 for i in items if tier(i["financials"]["roi"])[0] == "med")
    low_count    = sum(1 for i in items if tier(i["financials"]["roi"])[0] == "low")
    total_profit = sum(i["financials"]["profit"] for i in items)

    # --- Row rendering with per-row error handling
    def _safe_row(rank: int, item: dict) -> str:
        try:
            return build_row(rank, item)
        except Exception as exc:
            name = item.get("ai", {}).get("item_name", "?")
            print(f"  [report] Row error for '{name}': {exc}")
            return ""

    top_rows = "".join(_safe_row(i + 1, item) for i, item in enumerate(items[:TOP_N]))
    all_rows = "".join(_safe_row(i + 1, item) for i, item in enumerate(items))

    # --- Skipped rows rendering
    skipped_rows = ""
    if skipped_items:
        skipped_rows = "".join(build_skipped_row(i + 1, item) for i, item in enumerate(skipped_items))
        skipped_table = f"""
<div class="section-title skipped">📷 Skipped / Unidentified Photos ({len(skipped_items)})</div>
<table><thead>{_THEAD_SKIPPED}</thead><tbody>{skipped_rows}</tbody></table>
"""
    else:
        skipped_table = ""

    # --- Assemble HTML
    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Estate Sale Analysis Report</title>
<style>
{_CSS}
</style>
</head>
<body>
<div class="header">
  <h1>ESTATE SALE ANALYSIS REPORT</h1>
  <p>Date: {date_str} &nbsp;·&nbsp; Items: {total} &nbsp;·&nbsp; Sorted by: {SORT_BY.upper()} &nbsp;·&nbsp; AI: {AI_PROVIDER.upper()}</p>
</div>
<div class="summary">
  <div class="stat"><div class="val">{total}</div><div class="lbl">Total Items</div></div>
  <div class="stat"><div class="val">{high_count}</div><div class="lbl">High ROI</div></div>
  <div class="stat"><div class="val">{med_count}</div><div class="lbl">Medium ROI</div></div>
  <div class="stat"><div class="val">{low_count}</div><div class="lbl">Low ROI</div></div>
  <div class="stat"><div class="val">${total_profit:.0f}</div><div class="lbl">Est. Total Profit</div></div>
</div>

<div class="section-title gold">⭐ Top {TOP_N} Flip Opportunities — Ranked by {SORT_BY.upper()}</div>
<div class="sort-note">Buy price estimated at typical estate sale rate (10–30% of resale value)</div>
<table><thead>{_THEAD}</thead><tbody>{top_rows}</tbody></table>

<div class="section-title">Full Inventory — All {total} Items</div>
<table><thead>{_THEAD}</thead><tbody>{all_rows}</tbody></table>

{skipped_table}

<div class="footer">
  Generated by Estate Sale AI Analyzer &nbsp;·&nbsp; eBay comps from completed/sold listings only &nbsp;·&nbsp; Buy prices are estimates only
</div>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"\n[OK] Report saved: {output_path}")
