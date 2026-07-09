"""Benchmarks module.

Pulls vaults.fyi network benchmark APYs (USD + ETH; there is no EUR benchmark
upstream) and builds a pocket-vs-benchmark comparison across the 1/7/30-day
windows. "Pocket" APY is the TVL-weighted yield of kpk's DEPLOYED positions in
that denomination; the benchmark is vaults.fyi's reference rate for the same
denomination on the reference network (mainnet — Gnosis has no benchmark).
Enabled via config benchmarks.enabled. Never fails the run.
"""

from __future__ import annotations

import logging

from .aggregate import build_denomination_index, classify
from .vaultsfyi import VaultsFyiClient, extract_benchmark_total, extract_position, to_network

log = logging.getLogger("benchmarks")

WINDOWS = ["1day", "7day", "30day"]


def fetch_benchmarks(client: VaultsFyiClient, config: dict) -> dict:
    bcfg = config.get("benchmarks", {})
    if not bcfg.get("enabled"):
        return {}
    window = config.get("settings", {}).get("apy_window", "7day")
    out: dict[str, dict] = {}
    for network in bcfg.get("networks", []):
        entry: dict[str, float | None] = {}
        for code in ("usd", "eth"):
            try:
                raw = client.get_benchmark(network, code=code)
                entry[code] = extract_benchmark_total(raw, window=window)
            except Exception as exc:  # benchmarks are optional; never fail the run
                log.warning("Benchmark fetch failed for %s/%s: %s", network, code, exc)
                entry[code] = None
        out[network] = entry
    return out


def _benchmark_all_windows(client: VaultsFyiClient, network: str, code: str) -> dict:
    """All windows {1day,7day,30day} -> percent for one benchmark code."""
    try:
        raw = client.get_benchmark(network, code=code)
        apy = (raw or {}).get("apy") or {}
        return {w: extract_benchmark_total({"apy": apy}, window=w) for w in WINDOWS}
    except Exception as exc:
        log.warning("Benchmark fetch failed for %s/%s: %s", network, code, exc)
        return {w: None for w in WINDOWS}


def build_benchmark_comparison(client: VaultsFyiClient, config: dict) -> dict:
    """Pocket (deployed, TVL-weighted) APY vs vaults.fyi benchmark for USD and ETH
    across 1/7/30-day windows. EUR is intentionally excluded (no upstream
    benchmark)."""
    bcfg = config.get("benchmarks", {})
    if not bcfg.get("enabled"):
        return {}
    ref_net = bcfg.get("reference_network", "mainnet")
    index = build_denomination_index(config)
    default_nets = config.get("vaultsfyi", {}).get("networks")

    targets: list[tuple[str, str, list[str]]] = []  # (client_name, addr, nets)
    for c in config.get("clients", []):
        for a in (c.get("addresses") or []):
            addr = str(a.get("address") or "")
            if not (addr.lower().startswith("0x") and len(addr) == 42):
                continue
            nets = [to_network(x) for x in (a.get("chains") or [])] or default_nets
            targets.append((c["name"], addr, nets))

    comp = {
        "enabled": True,
        "windows": WINDOWS,
        "current_window": config.get("settings", {}).get("apy_window", "7day"),
        "reference_network": ref_net,
        # by_client[name][window] = that client's deployed-weighted pocket APY (%)
        "USD": {"pocket": {}, "by_client": {}, "benchmark": _benchmark_all_windows(client, ref_net, "usd")},
        "ETH": {"pocket": {}, "by_client": {}, "benchmark": _benchmark_all_windows(client, ref_net, "eth")},
    }

    # Pocket APY per window, firm-wide AND per client (one positions fetch per wallet
    # per window). Per-client series powers the interactive client selector.
    for win in WINDOWS:
        num = {"USD": 0.0, "ETH": 0.0}
        den = {"USD": 0.0, "ETH": 0.0}
        cnum: dict[str, dict[str, list[float]]] = {}
        for cname, addr, nets in targets:
            try:
                rows = client.get_positions(addr, networks=nets, apy_window=win)
            except Exception as exc:
                log.warning("Pocket-APY fetch failed for %s @ %s: %s", addr[:10], win, type(exc).__name__)
                continue
            for raw in rows:
                p = extract_position(raw, "", addr, (nets or ["mainnet"])[0])
                b = classify(p, index)
                if b in ("USD", "ETH") and p["value_usd"]:
                    num[b] += p["value_usd"] * p["apy_pct"]
                    den[b] += p["value_usd"]
                    cc = cnum.setdefault(cname, {"USD": [0.0, 0.0], "ETH": [0.0, 0.0]})
                    cc[b][0] += p["value_usd"] * p["apy_pct"]
                    cc[b][1] += p["value_usd"]
        for b in ("USD", "ETH"):
            comp[b]["pocket"][win] = round(num[b] / den[b], 4) if den[b] else None
            for cname, cc in cnum.items():
                bc = comp[b]["by_client"].setdefault(cname, {})
                bc[win] = round(cc[b][0] / cc[b][1], 4) if cc[b][1] else None

    # Convenience: current-window spread (pocket - benchmark) = realised alpha.
    cw = comp["current_window"]
    for b in ("USD", "ETH"):
        pk = comp[b]["pocket"].get(cw)
        bm = comp[b]["benchmark"].get(cw)
        comp[b]["spread_pct"] = (round(pk - bm, 4) if (pk is not None and bm is not None) else None)
    return comp
