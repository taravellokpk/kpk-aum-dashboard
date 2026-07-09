"""Backfill the AUM track record from vaults.fyi historical balances.

Reconstructs historical AUM for every managed wallet by sampling the vaults.fyi
`/alpha/historical-balances/{wallet}/{timestamp}` endpoint at weekly timestamps,
aggregating to firm total, per-denomination and per-DAO, and MERGING into
data/history.csv — the same store the live daily pipeline appends to.

    python backfill_history.py [weeks]      # default 26 (~6 months), weekly

Notes / soundness:
  * Same source as the live dashboard (vaults.fyi), so no reconciliation gap.
  * Uses the same corrected per-position value logic (extract_position).
  * The endpoint returns DEPLOYED vault positions; historical idle wallet balances
    are not available, so reconstructed points reflect deployed capital. Live rows
    (deployed + idle) are left untouched — existing dates always win on merge.
  * Alpha endpoint: experimental. Failures for a wallet/date are skipped, never fatal.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time
from pathlib import Path

import requests

from src.pipeline import load_config, load_configurator, apply_configurator, _is_real_address, read_history, write_history
from src.vaultsfyi import extract_position
from src.aggregate import build_denomination_index, classify

ROOT = Path(__file__).resolve().parent
BASE = "https://api.vaults.fyi/alpha/historical-balances"


def main() -> None:
    weeks = int(sys.argv[1]) if len(sys.argv) > 1 else 26
    config = apply_configurator(load_config(str(ROOT / "config.yaml")), load_configurator())
    key = os.environ.get(config.get("vaultsfyi", {}).get("api_key_env", "VAULTSFYI_API_KEY"), "")
    if not key:
        raise SystemExit("No vaults.fyi API key (set it in configurator.json).")
    sess = requests.Session()
    sess.headers.update({"x-api-key": key, "accept": "application/json"})
    index = build_denomination_index(config)
    clients = config.get("clients", [])
    now = dt.datetime.now(dt.timezone.utc)

    added = 0
    for w in range(weeks, 0, -1):
        when = now - dt.timedelta(weeks=w)
        date = when.strftime("%Y-%m-%d")
        # Re-read each iteration so the run is resumable: already-saved dates skip.
        if date in {r["date"] for r in read_history()}:
            continue
        ts = int(when.timestamp())
        firm = 0.0
        denom = {"USD": 0.0, "ETH": 0.0, "EUR": 0.0}
        per_client: dict[str, float] = {}
        any_ok = False
        for c in clients:
            ctot = 0.0
            for a in (c.get("addresses") or []):
                addr = a.get("address")
                if not addr or not _is_real_address(addr):
                    continue
                try:
                    r = sess.get(f"{BASE}/{addr}/{ts}", timeout=45)
                except requests.RequestException:
                    continue
                if r.status_code != 200:
                    continue
                any_ok = True
                for raw in (r.json().get("data") or []):
                    p = extract_position(raw, c["name"], addr, (a.get("chains") or ["ethereum"])[0])
                    v = p["value_usd"]
                    if v <= 0:
                        continue
                    ctot += v
                    firm += v
                    b = classify(p, index)
                    if b in denom:
                        denom[b] += v
                time.sleep(0.12)  # be polite to the alpha endpoint
            per_client[c["name"]] = round(ctot, 2)
        if not any_ok:
            print(f"  {date}: no data, skipped")
            continue
        row = {
            "date": date,
            "firm_total_usd": round(firm, 2),
            "USD": round(denom["USD"], 2), "ETH": round(denom["ETH"], 2), "EUR": round(denom["EUR"], 2),
        }
        for name, v in per_client.items():
            row[f"client:{name}"] = v
        # Persist immediately (incremental + resumable): existing dates always win.
        hist = read_history()
        if date not in {r["date"] for r in hist}:
            hist.append(row)
            hist.sort(key=lambda r: r["date"])
            write_history(hist)
            added += 1
            print(f"  {date}: firm ${firm:,.0f}  (saved — {len(hist)} rows)", flush=True)

    hist = read_history()
    print(f"\nBackfilled {added} new points this run. history.csv now has {len(hist)} rows "
          f"({hist[0]['date']} → {hist[-1]['date']}).")


if __name__ == "__main__":
    main()
