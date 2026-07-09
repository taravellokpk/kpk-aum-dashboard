"""Clean reconstructed AUM history: drop points that are clearly incomplete
reconstructions (the vaults.fyi /alpha/historical-balances endpoint occasionally
undercounts positions at a given timestamp, producing implausible single-week
spikes/dips). Principled rule: a point is dropped if its firm total deviates
more than THRESHOLD from the median of its nearest neighbours — i.e. it is
inconsistent with the surrounding trend. The raw file is preserved.

    python clean_history.py

Live daily snapshots (full pipeline runs) are authoritative and never dropped.
"""

from __future__ import annotations

import csv
import shutil
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HIST = ROOT / "data" / "history.csv"
RAW = ROOT / "data" / "history_raw.csv"
WINDOW = 3          # neighbours each side
THRESHOLD = 0.22    # >22% off local median => likely incomplete reconstruction
# Authoritative daily snapshots (never dropped, even if they look off vs noisy neighbours).
LIVE = {"2026-06-15", "2026-06-24", "2026-06-30", "2026-07-06"}


def main() -> None:
    rows = list(csv.DictReader(open(HIST, encoding="utf-8")))
    rows.sort(key=lambda r: r["date"])
    if not RAW.exists():
        shutil.copyfile(HIST, RAW)   # keep the raw reconstruction once
    vals = [float(r["firm_total_usd"]) for r in rows]

    kept, dropped = [], []
    for i, r in enumerate(rows):
        v = vals[i]
        if r["date"] in LIVE:
            kept.append(r); continue
        neigh = [vals[j] for j in range(max(0, i - WINDOW), min(len(vals), i + WINDOW + 1)) if j != i]
        med = statistics.median(neigh) if neigh else v
        if med > 0 and abs(v - med) / med > THRESHOLD:
            dropped.append((r["date"], v, med))
        else:
            kept.append(r)

    # rewrite with the union of columns from the kept rows
    fields: list[str] = []
    for r in kept:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(HIST, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in kept:
            w.writerow(r)

    for d, v, m in dropped:
        print("dropped {}  ${:,.0f}  (local median ${:,.0f})".format(d, v, m))
    print("kept {} of {} points ({} -> {}).".format(len(kept), len(rows), kept[0]["date"], kept[-1]["date"]))


if __name__ == "__main__":
    main()
