import os
from core.financials import calc_financials

print("Testing financials...")
item = {
    "ai": {
        "item_name": "Test Item",
        "ai_value_low": 100.0,
        "ai_value_high": 200.0,
        "confidence": 85,
        "exact_model_identified": True,
        "pkg_length_in": 12,
        "pkg_width_in": 10,
        "pkg_height_in": 8,
        "pkg_weight_lb": 3
    },
    "comps": {
        "low": "N/A",
        "mean": "N/A",
        "high": "N/A",
        "count": 0,
        "link": "",
        "links": []
    }
}
res = calc_financials(item)
print("Result for 0 comps:", res)
