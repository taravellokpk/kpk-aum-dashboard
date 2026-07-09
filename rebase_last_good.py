"""Outage stopgap: regenerate the snapshot from the last-good vaults.fyi data
plus LIVE on-chain supplements.

Used when vaults.fyi is down but the supplement layer (public RPC + CoinGecko)
is up: reconstructs positions from the previous snapshot's holdings, appends
freshly-read supplemental balances, re-aggregates, and republishes. Benchmarks
and reference prices are carried over from the last snapshot. The next
successful full pipeline run overwrites all of it with fresh data.

    python rebase_last_good.py
"""

from __future__ import annotations

import datetime as dt
import json

from src import aggregate
from src.onchain import build_positions_supplement
from src.pipeline import (LATEST, load_env, load_config, load_configurator, apply_configurator,
                          assemble_snapshot, write_outputs, read_history, write_history,
                          build_history_row, build_history_series, compute_wallet_counts, ROOT)


def positions_from_snapshot(snap: dict) -> list[dict]:
    """Reconstruct the flat normalized position list from a snapshot's holdings."""
    out = []
    for c in snap["clients"]:
        for w in c["wallets"]:
            for h in w["holdings"]:
                if h.get("apy_excluded"):
                    continue  # never carry over old supplements; they are re-read live
                out.append({
                    "client": c["name"], "address": w["address"], "chain": h.get("chain"),
                    "network": h.get("chain"), "symbol": h.get("symbol"), "name": h.get("name"),
                    "protocol": h.get("protocol") or "", "value_usd": h["value_usd"],
                    "native_balance": 0.0, "asset_price_usd": 0.0,
                    "apy_pct": h.get("apy_pct", 0.0), "unclaimed_usd": 0.0,
                    "is_idle": bool(h.get("is_idle")), "source": "vaultsfyi",
                })
    return out


def main() -> None:
    load_env()
    config = apply_configurator(load_config(str(ROOT / "config.yaml")), load_configurator())
    prev = json.loads(LATEST.read_text(encoding="utf-8"))
    positions = positions_from_snapshot(prev)
    n0 = len(positions)
    positions += build_positions_supplement(config, positions)
    print(f"positions: {n0} carried over + {len(positions) - n0} live supplements")

    refs = {"eth_usd": prev["reference_prices"].get("eth_usd"),
            "eur_usd": prev["reference_prices"].get("eur_usd")}
    roster = [c["name"] for c in config.get("clients", [])]
    counts = compute_wallet_counts(config, positions, sample=False)
    core = aggregate.aggregate(positions, config, refs, client_names=roster, wallet_counts=counts)

    now = dt.datetime.now(dt.timezone.utc)
    snapshot = assemble_snapshot(core, config, "live", None, prev.get("benchmarks", {}), now)
    snapshot["coverage_note"] = ("vaults.fyi positions as of " + prev["updated_at"] +
                                 " (provider outage); supplemental on-chain balances live as of " +
                                 snapshot["updated_at"] + ".")

    history_rows = [r for r in read_history() if r.get("date") != snapshot["date"]]
    history_rows.append(build_history_row(snapshot))
    snapshot["history"] = build_history_series(
        history_rows, snapshot, int(config.get("settings", {}).get("history_points", 30)))
    write_outputs(snapshot)
    write_history(history_rows)
    f = snapshot["firm"]
    print("Rebased: total ${:,.0f} (deployed {:,.0f} / idle {:,.0f})".format(
        f["total_usd"], f["deployed_usd"], f["idle_usd"]))
    for c in snapshot["clients"]:
        print("  {:14} ${:,.0f}".format(c["name"], c["total_usd"]))


if __name__ == "__main__":
    main()
