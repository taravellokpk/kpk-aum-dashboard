"""Bundle dashboard/index.html + dashboard/data.js into ONE self-contained HTML
file you can email / Slack / Drive to colleagues for quick feedback.

    python build_standalone.py

Output: kpk-aum-dashboard.html in the project root. It opens by double-click in
any browser (no server, no build step). It contains the treasury snapshot but NO
secrets (the API key lives only in configurator.json, which is never bundled).
Re-run it after each `python -m src.pipeline` to refresh the shared copy.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
data = (ROOT / "dashboard" / "data.js").read_text(encoding="utf-8")

# Auto-detect the source dashboard HTML (survives renames like index.html ->
# Institutional_overview.html): the one that pulls in data.js.
src = None
for p in sorted((ROOT / "dashboard").glob("*.html")):
    txt = p.read_text(encoding="utf-8")
    if 'src="data.js"' in txt or "__AUM_DATA__" in txt:
        src, html = p, txt
        break
if src is None:
    raise SystemExit("No source dashboard HTML found in dashboard/ (expected one referencing data.js).")
print(f"Source: dashboard/{src.name}")

# Replace the external <script src="data.js"> with the data inlined, so the file
# is fully standalone (lambda replacement avoids backslash interpretation).
inline = "<script>\n" + data + "\n</script>"
pattern = re.compile(r'<script src="data\.js"[^>]*></script>')
if pattern.search(html):
    html = pattern.sub(lambda _m: inline, html, count=1)
else:  # fallback: inject before </body>
    html = html.replace("</body>", inline + "\n</body>")

out = ROOT / "kpk-aum-dashboard.html"
out.write_text(html, encoding="utf-8")
print(f"Wrote {out}  ({len(html.encode('utf-8')) // 1024} KB)")
