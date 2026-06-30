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
    thumb     = item.get("thumb", "")
    img_tag   = (
        f'<img src="{thumb}" style="width:80px;height:70px;object-fit:cover;border-radius:4px;">'
        if thumb else "—"
    )

    # Condition badge
    cond_text = ai.get("ebay_condition") or "Used"
    cond_badge = f'<span style="display:inline-block;background:#f3f4f6;color:#374151;font-size:9px;font-weight:bold;padding:1px 5px;border-radius:3px;margin-right:6px;">{cond_text}</span>'

    # Badges for match status
    badges = []
    
    # Check if exact model was not identified
    if ai.get("exact_model_identified") is False:
        badges.append('<span style="display:inline-block;background:#fef3c7;color:#d97706;font-size:9px;font-weight:bold;padding:1px 5px;border-radius:3px;margin-top:4px;margin-right:4px;">⚠️ Exact model details not identified</span>')

    # Check if fallback query was used
    if comps.get("fallback_used"):
        badges.append('<span style="display:inline-block;background:#eff6ff;color:#1d4ed8;font-size:9px;font-weight:bold;padding:1px 5px;border-radius:3px;margin-top:4px;margin-right:4px;">ℹ️ Based on similar model</span>')
        
    # Check if confidence is low or zero comps found
    if comps.get("count", 0) == 0:
        badges.append('<span style="display:inline-block;background:#fef2f2;color:#b91c1c;font-size:9px;font-weight:bold;padding:1px 5px;border-radius:3px;margin-top:4px;margin-right:4px;">⚠️ Valuation estimate only (0 comps)</span>')
    elif conf < 70:
        badges.append('<span style="display:inline-block;background:#fffbeb;color:#b45309;font-size:9px;font-weight:bold;padding:1px 5px;border-radius:3px;margin-top:4px;margin-right:4px;">⚠️ Low confidence match</span>')
        
    badge_html = "".join(badges)

    # Comps verification links
    link_buttons = []
    verified_links = comps.get("links", [])
    if verified_links:
        for idx, url in enumerate(verified_links, 1):
            link_buttons.append(f'<a href="{url}" target="_blank" style="display:inline-block;margin:2px;font-size:10px;color:#2563eb;text-decoration:none;border:1px solid #bfdbfe;padding:2px 5px;border-radius:3px;background:#f0f9ff;">Comp #{idx}</a>')
    
    if comp_link and comp_link != "#":
        link_buttons.append(f'<a href="{comp_link}" target="_blank" style="display:inline-block;margin:2px;font-size:10px;color:#ffffff;text-decoration:none;background:#2563eb;padding:2px 6px;border-radius:3px;font-weight:bold;">All Comps</a>')
        
    link_html = "".join(link_buttons) if link_buttons else "—"

    # Number of sold listings
    count_text = f"Based on {comps['count']} sold listings" if comps['count'] > 0 else "No comps found"

    # Color class for profit
    profit_color = "#1c7a3a" if profit >= 0 else "#b91c1c"

    # Extract resale reasons & search query
    resale_reasons = ai.get("resale_reasons") or ""
    query_used     = comps.get("query_used") or ai.get("ebay_search_query") or ""

    resale_html = f'<div style="font-size:10px;color:#047857;font-weight:bold;margin-top:3px;">💡 Resale Appeal: {resale_reasons}</div>' if resale_reasons else ''
    query_html  = f'<div style="font-size:10px;color:#4b5563;margin-top:2px;">🔍 Search Query: <span style="font-family:monospace;background:#f3f4f6;padding:1px 4px;border-radius:2px;">{query_used}</span></div>' if query_used else ''

    # Package dimensions display
    pkg_l  = ai.get("pkg_length_in")
    pkg_w  = ai.get("pkg_width_in")
    pkg_h  = ai.get("pkg_height_in")
    pkg_wt = ai.get("pkg_weight_lb")
    dims_html = ""
    if pkg_l is not None and pkg_w is not None and pkg_h is not None and pkg_wt is not None:
        if pkg_l > 0 or pkg_w > 0 or pkg_h > 0 or pkg_wt > 0:
            dims_html = f'<div style="font-size:10px;color:#4b5563;margin-top:2px;">📦 Package: <span style="font-family:monospace;background:#f3f4f6;padding:1px 4px;border-radius:2px;">{pkg_l}x{pkg_w}x{pkg_h} in | {pkg_wt} lbs</span></div>'

    shipping_carrier = fin.get("shipping_carrier", "Estimated")
    shipping_service = fin.get("shipping_service", "")
    shipping_est_days = fin.get("shipping_est_days")
    
    # Format a nice sub-label under shipping
    shipping_desc = ""
    if shipping_carrier and shipping_service:
        days_str = f" ({shipping_est_days}d)" if shipping_est_days else ""
        shipping_desc = f'<div style="font-size:9px;color:#6b7280;text-align:left;margin-top:1px;">{shipping_carrier} {shipping_service}{days_str}</div>'

    return f"""
    <tr>
      <td class="rank">{rank}</td>
      <td class="center">{img_tag}</td>
      <td>
        <div class="item-name">{ai.get('item_name', 'Unknown')}</div>
        <div style="margin-top:3px;">{cond_badge}<span class="item-notes" style="color:#666;font-size:11px;">{ai.get('condition_notes', '')}</span></div>
        {resale_html}
        {query_html}
        {dims_html}
        <div>{badge_html}</div>
      </td>
      <td class="center">
        <div style="font-weight:bold;font-size:13px;">${sell_price:.0f}</div>
        <div style="font-size:10px;color:#888">{comps['low']} – {comps['high']}</div>
        <div style="font-size:9px;color:#999;margin-top:2px;">{count_text}</div>
      </td>
      <td class="center">
        <div style="font-size:11px;color:#555;text-align:left;">eBay Fee: <span style="font-weight:bold;float:right;">-${ebay_fee:.2f}</span></div>
        <div style="font-size:11px;color:#555;text-align:left;margin-top:2px;">Shipping: <span style="font-weight:bold;float:right;">-${shipping:.2f}</span></div>
        {shipping_desc}
        <div style="font-size:11px;font-weight:bold;color:#111;text-align:left;margin-top:3px;border-top:1px dashed #ddd;padding-top:2px;">Net: <span style="float:right;">${net_after_fees:.2f}</span></div>
      </td>
      <td class="center">
        <div style="font-weight:bold;font-size:13px;color:#b45309;">${recommended_max_buy:.0f}</div>
        <div style="font-size:10px;color:#888;margin-top:2px;">Est. Buy: ${buy_price:.0f}</div>
      </td>
      <td class="center">
        <div style="font-weight:bold;font-size:13px;color:{profit_color};">${profit:.0f}</div>
        <div style="font-weight:bold;font-size:11px;color:#a07000;margin-top:2px;">{roi:.0f}% ROI</div>
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
        f'<img src="{thumb}" style="width:80px;height:70px;object-fit:cover;border-radius:4px;">'
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
  <th class="center" style="width:36px">#</th>
  <th class="center" style="width:90px">Photo</th>
  <th>Item Details</th>
  <th class="center">Expected Resale</th>
  <th class="center" style="width:130px">Fees & Shipping</th>
  <th class="center">Recommended Buy Limit</th>
  <th class="center">Expected Net Return</th>
  <th class="center">Match Confidence</th>
  <th class="center">Verify Comps</th>
</tr>"""

_CSS = """\
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: Arial, sans-serif; font-size: 13px; background: #fff; color: #1c1c1c; }
.header { background: #1c1c1c; color: #fff; padding: 20px 32px; }
.header h1 { font-size: 18px; font-weight: bold; letter-spacing: 0.5px; }
.header p { font-size: 11px; color: #aaa; margin-top: 5px; }
.summary { display: flex; border-bottom: 1px solid #e0e0e0; background: #f5f5f5; }
.stat { flex: 1; padding: 14px 20px; border-right: 1px solid #e0e0e0; }
.stat:last-child { border-right: none; }
.stat .val { font-size: 20px; font-weight: bold; }
.stat .lbl { font-size: 10px; color: #888; text-transform: uppercase; letter-spacing: 0.6px; margin-top: 3px; }
.section-title { background: #f0f0f0; padding: 12px 32px; font-size: 12px; font-weight: bold;
  text-transform: uppercase; letter-spacing: 1px; color: #555; border-bottom: 1px solid #e0e0e0;
  border-top: 2px solid #1c1c1c; margin-top: 24px; }
.section-title.gold { border-top-color: #a07000; color: #a07000; }
.section-title.skipped { border-top-color: #b91c1c; color: #b91c1c; background: #fef2f2; }
.sort-note { padding: 8px 32px; font-size: 11px; color: #888; background: #fafafa; border-bottom: 1px solid #eee; }
table { width: 100%; border-collapse: collapse; }
thead th { background: #2c2c2c; color: #fff; padding: 10px 12px; text-align: left;
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px; font-weight: bold; }
thead th.center { text-align: center; }
tbody tr:nth-child(even) { background: #fafafa; }
tbody tr:hover { background: #f0f0f0; }
tbody td { padding: 10px 12px; border-bottom: 1px solid #eeeeee; vertical-align: middle; }
tbody td.center { text-align: center; }
.item-name { font-weight: bold; font-size: 13px; }
.item-notes { font-size: 11px; color: #777; margin-top: 2px; }
.rank { color: #ccc; font-weight: bold; font-size: 15px; text-align: center; }
.tier { font-size: 11px; font-weight: bold; }
.tier.high { color: #1c7a3a; }
.tier.med  { color: #a07000; }
.tier.low  { color: #999; }
.conf-wrap { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.conf-val { font-size: 12px; font-weight: bold; }
.bar-bg { width: 60px; height: 3px; background: #e0e0e0; border-radius: 2px; }
.bar-fill { height: 3px; background: #1c1c1c; border-radius: 2px; }
.footer { padding: 14px 32px; font-size: 10px; color: #aaa; border-top: 1px solid #e0e0e0; margin-top: 24px; }"""


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
    _NA_COMPS = {"low": "N/A", "median": "N/A", "high": "N/A", "count": 0, "link": ""}
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
