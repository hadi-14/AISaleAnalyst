import os
from core.ebay import scrape_ebay_comps

print("Testing eBay scraper...")
res = scrape_ebay_comps(
    driver=None,
    query="DeWalt DWS779 Miter Saw",
    ai_val_low=100.0,
    item_name="DeWalt Miter Saw"
)
print("Result:", res)
