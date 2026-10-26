"""
Builds the STATIC demo dashboard (data embedded, no server needed).
For the LIVE version that polls real NTES data, run server.py instead.
"""

import json
from dashboard_template import render_html, static_bootstrap

with open("output/live_trip_demo.json", encoding="utf-8") as f:
    data = json.load(f)

html = render_html(static_bootstrap(data))

with open("output/railway_eta_dashboard.html", "w", encoding="utf-8") as f:
    f.write(html)

print("Written -> output/railway_eta_dashboard.html")
print(f"Size: {len(html)/1024:.1f} KB")
