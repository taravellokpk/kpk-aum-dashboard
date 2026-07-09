"""Classification + wallet -> client -> firm aggregation.

All money math lives here and only here (never in a spreadsheet). Every level is
computed directly from the flat list of normalized position records, so the
firm-level weighted APY is genuinely recomputed across all positions rather than
averaged from client APYs.

Normalized position record (produced by vaultsfyi extractors / manual adjustments):
  {
    client, address, chain, network, symbol, name,
    value_usd, native_balance, asset_price_usd, apy_pct,
    is_idle, source, bucket(optional explicit override)
  }
"""

from __future__ import annotations

from typing import Any, Iterable

BUCKET_ORDER = ["USD", "ETH", "EUR"]
NATIVE_UNIT = {"USD": "USD", "ETH": "ETH", "EUR": "EUR"}
EPS = 0.5  # USD rounding tolerance for invariant assertions


class InvariantError(AssertionError):
    """Raised when an aggregation reconciliation invariant fails."""


def build_denomination_index(config: dict) -> dict[str, str]:
    """symbol (upper) -> bucket name. Case-insensitive."""
    index: dict[str, str] = {}
    for bucket, spec in (config.get("denominations") or {}).items():
        for sym in (spec.get("symbols") or []):
            index[str(sym).strip().upper()] = bucket
    return index


def classify(position: dict, index: dict[str, str]) -> str | None:
    """Return the denomination bucket for a position, or None if unclassified.
    An explicit `bucket` on the record (manual adjustments) wins."""
    explicit = position.get("bucket")
    if explicit in BUCKET_ORDER:
        return explicit
    return index.get(str(position.get("symbol", "")).strip().upper())


def _weighted_apy(positions: list[dict]) -> float:
    """TVL-weighted APY (%) across a set of positions. Positions flagged
    apy_excluded (on-chain supplements whose yield we don't measure) are left
    out of BOTH numerator and denominator, so they neither dilute nor inflate.
    0 when empty."""
    measured = [p for p in positions if not p.get("apy_excluded")]
    value = sum(p["value_usd"] for p in measured)
    if not value:
        return 0.0
    return round(sum(p["value_usd"] * p["apy_pct"] for p in measured) / value, 4)


def _firm_stats(positions: list[dict]) -> dict:
    """Headline analytics for the KPI band: blended deployed yield, estimated
    annualized yield in USD, protocol/chain spread, and the largest holdings."""
    deployed = [p for p in positions if not p.get("is_idle")]
    deployed_value = sum(p["value_usd"] for p in deployed)
    blended = _weighted_apy(deployed)
    est_annual = round(sum(p["value_usd"] * p["apy_pct"] / 100.0 for p in deployed), 2)
    protocols = sorted({(p.get("protocol") or "").strip() for p in deployed if (p.get("protocol") or "").strip()})
    chains = sorted({p.get("chain") for p in positions if p.get("chain")})
    top = sorted(deployed, key=lambda p: -p["value_usd"])[:5]
    top_holdings = [{
        "symbol": p.get("symbol"), "name": p.get("name") or p.get("symbol"),
        "protocol": p.get("protocol") or "", "chain": p.get("chain"),
        "client": p.get("client"), "value_usd": round(p["value_usd"], 2),
        "apy_pct": round(p.get("apy_pct", 0.0), 4),
    } for p in top]
    return {
        "deployed_apy_pct": blended,
        "est_annual_yield_usd": est_annual,
        "protocol_count": len(protocols),
        "protocols": protocols,
        "chain_count": len(chains),
        "chains": chains,
        "top_holdings": top_holdings,
    }


def _ref_price_for(bucket: str, reference_prices: dict) -> float | None:
    if bucket == "USD":
        return 1.0
    if bucket == "ETH":
        return reference_prices.get("eth_usd")
    if bucket == "EUR":
        return reference_prices.get("eur_usd")
    return None


def _bucket_stats(positions: list[dict], bucket: str, reference_prices: dict) -> dict:
    """Deployed/idle value + weighted-APY + native-unit stats for one bucket.

    `value_usd` is total holdings (deployed + idle). The weighted APY is computed
    over DEPLOYED positions only, so idle cash never dilutes (or inflates) the
    reported yield. Idle value is still surfaced via `idle_usd`."""
    deployed = [p for p in positions if not p.get("is_idle")]
    idle = [p for p in positions if p.get("is_idle")]
    value = sum(p["value_usd"] for p in positions)
    deployed_value = sum(p["value_usd"] for p in deployed)
    idle_value = sum(p["value_usd"] for p in idle)
    # APY over measured deployed positions only (apy_excluded supplements are
    # counted in value but never in yield — their APY is not observed).
    measured = [p for p in deployed if not p.get("apy_excluded")]
    measured_value = sum(p["value_usd"] for p in measured)
    weighted_num = sum(p["value_usd"] * p["apy_pct"] for p in measured)
    avg_apy = (weighted_num / measured_value) if measured_value else 0.0
    ref = _ref_price_for(bucket, reference_prices)
    native = (value / ref) if (ref not in (None, 0)) else None
    return {
        "value_usd": round(value, 2),
        "deployed_usd": round(deployed_value, 2),
        "measured_usd": round(measured_value, 2),  # deployed with observed APY (alpha base)
        "idle_usd": round(idle_value, 2),
        "avg_apy_pct": round(avg_apy, 4),       # measured-deployed-only
        "native_value": (round(native, 6) if native is not None else None),
        "native_unit": NATIVE_UNIT[bucket],
        "positions": len(positions),
        "deployed_positions": len(deployed),
        "idle_positions": len(idle),
    }


def _buckets_for(positions: list[dict], index: dict, reference_prices: dict) -> tuple[dict, dict]:
    """Return (buckets dict, unclassified dict) for a set of positions."""
    by_bucket: dict[str, list[dict]] = {b: [] for b in BUCKET_ORDER}
    unclassified: list[dict] = []
    for p in positions:
        b = classify(p, index)
        if b is None:
            unclassified.append(p)
        else:
            by_bucket[b].append(p)
    buckets = {b: _bucket_stats(by_bucket[b], b, reference_prices) for b in BUCKET_ORDER}
    unclass = {
        "value_usd": round(sum(p["value_usd"] for p in unclassified), 2),
        "positions": len(unclassified),
    }
    return buckets, unclass


def derive_reference_prices(positions: Iterable[dict]) -> dict:
    """Derive ETH/USD and EUR/USD from the positions' own asset prices.
    Prefers ETH/WETH for the ETH ref and EURe-family for the EUR ref."""
    eth_candidates = [
        p["asset_price_usd"] for p in positions
        if str(p.get("symbol", "")).upper() in {"ETH", "WETH"} and p.get("asset_price_usd")
    ]
    eur_candidates = [
        p["asset_price_usd"] for p in positions
        if str(p.get("symbol", "")).upper() in {"EURE", "EURC", "EURS", "AGEUR", "EURA", "STEUR"}
        and p.get("asset_price_usd")
    ]
    return {
        "eth_usd": (sum(eth_candidates) / len(eth_candidates)) if eth_candidates else None,
        "eur_usd": (sum(eur_candidates) / len(eur_candidates)) if eur_candidates else None,
    }


def filter_positions(positions: list[dict], include_idle: bool) -> list[dict]:
    """Respect include_idle_assets consistently across totals and counts.
    Manual adjustments are never treated as idle."""
    if include_idle:
        return list(positions)
    return [p for p in positions if not p.get("is_idle")]


def aggregate(
    positions: list[dict],
    config: dict,
    reference_prices: dict,
    client_names: list[str] | None = None,
    wallet_counts: dict[str, int] | None = None,
) -> dict:
    """Build the firm + per-client snapshot (without history / reconciliation /
    moved-funds, which the pipeline layers on). `positions` must already be
    idle-filtered. `wallet_counts` maps client -> configured wallet count."""
    index = build_denomination_index(config)
    wallet_counts = wallet_counts or {}

    # Roster: every configured client appears even with zero positions.
    roster = client_names if client_names is not None else sorted({p["client"] for p in positions})

    by_client: dict[str, list[dict]] = {name: [] for name in roster}
    for p in positions:
        by_client.setdefault(p["client"], []).append(p)

    clients_out: list[dict] = []
    firm_total = 0.0
    for name in roster:
        cps = by_client.get(name, [])
        c_buckets, c_unclass = _buckets_for(cps, index, reference_prices)
        c_total = round(sum(p["value_usd"] for p in cps), 2)
        c_deployed = round(sum(p["value_usd"] for p in cps if not p.get("is_idle")), 2)
        c_idle = round(sum(p["value_usd"] for p in cps if p.get("is_idle")), 2)
        c_dep_apy = _weighted_apy([p for p in cps if not p.get("is_idle")])
        firm_total += c_total

        # Per-wallet rollup: one entry per distinct address (an address held on
        # several chains is a single wallet entry; positions still summed across
        # all chains for that address). Each wallet also carries its individual
        # holdings (one row per position) so the dashboard can expand to a
        # detailed, denomination-tagged breakdown, and a deployed/idle split.
        wallets: dict[str, dict] = {}
        for p in cps:
            w = wallets.setdefault(p["address"], {"address": p["address"], "total_usd": 0.0,
                                                  "deployed_usd": 0.0, "idle_usd": 0.0,
                                                  "positions": 0, "holdings": []})
            w["total_usd"] += p["value_usd"]
            if p.get("is_idle"):
                w["idle_usd"] += p["value_usd"]
            else:
                w["deployed_usd"] += p["value_usd"]
            w["positions"] += 1
            w["holdings"].append({
                "symbol": p.get("symbol") or "UNKNOWN",
                "name": p.get("name") or p.get("symbol") or "",
                "protocol": p.get("protocol") or "",
                "chain": p.get("chain"),
                "bucket": classify(p, index) or "—",
                "value_usd": round(p["value_usd"], 2),
                "apy_pct": round(p.get("apy_pct", 0.0), 4),
                "is_idle": bool(p.get("is_idle")),
                "apy_excluded": bool(p.get("apy_excluded")),
            })
        wallet_list = [
            {
                "address": w["address"],
                "total_usd": round(w["total_usd"], 2),
                "deployed_usd": round(w["deployed_usd"], 2),
                "idle_usd": round(w["idle_usd"], 2),
                "positions": w["positions"],
                # idle holdings sort to the bottom; otherwise by value.
                "holdings": sorted(w["holdings"], key=lambda h: (h["is_idle"], -h["value_usd"])),
            }
            for w in sorted(wallets.values(), key=lambda x: -x["total_usd"])
        ]

        clients_out.append({
            "name": name,
            "total_usd": c_total,
            "deployed_usd": c_deployed,
            "idle_usd": c_idle,
            "deployed_apy_pct": c_dep_apy,
            "share_pct": 0.0,  # filled after firm_total known
            "wallet_count": wallet_counts.get(name, len(wallet_list)),
            "positions": len(cps),
            "buckets": c_buckets,
            "unclassified": c_unclass,
            "reconciliation": {"onchain_usd": 0, "coverage_gap_pct": 0},
            "wallets": wallet_list,
        })

    firm_total = round(firm_total, 2)
    for c in clients_out:
        c["share_pct"] = round((c["total_usd"] / firm_total * 100.0), 4) if firm_total else 0.0
    clients_out.sort(key=lambda c: -c["total_usd"])

    firm_buckets, firm_unclass = _buckets_for(positions, index, reference_prices)
    manual_total = round(sum(p["value_usd"] for p in positions if p.get("source") == "manual"), 2)
    firm_deployed = round(sum(p["value_usd"] for p in positions if not p.get("is_idle")), 2)
    firm_idle = round(sum(p["value_usd"] for p in positions if p.get("is_idle")), 2)

    firm = {
        "name": config.get("firm", {}).get("name", "kpk"),
        "total_usd": firm_total,
        "deployed_usd": firm_deployed,
        "idle_usd": firm_idle,
        "deployed_apy_pct": _weighted_apy([p for p in positions if not p.get("is_idle")]),
        "clients": sum(1 for c in clients_out if c["total_usd"] > 0 or c["positions"] > 0),
        "wallets": sum(c["wallet_count"] for c in clients_out),
        "positions": len(positions),
        "deployed_positions": sum(1 for p in positions if not p.get("is_idle")),
        "idle_positions": sum(1 for p in positions if p.get("is_idle")),
        "buckets": firm_buckets,
        "unclassified": firm_unclass,
        "manual_adjustments_usd": manual_total,
        "stats": _firm_stats(positions),
        "reconciliation": {"onchain_usd": 0, "coverage_gap_pct": 0, "source": "dune"},
    }

    snapshot = {
        "reference_prices": {
            "eth_usd": (round(reference_prices["eth_usd"], 2) if reference_prices.get("eth_usd") else None),
            "eur_usd": (round(reference_prices["eur_usd"], 4) if reference_prices.get("eur_usd") else None),
        },
        "firm": firm,
        "clients": clients_out,
    }
    assert_invariants(snapshot)
    return snapshot


def assert_invariants(snapshot: dict) -> None:
    """Hard reconciliation guarantees. Raise InvariantError on any failure."""
    firm = snapshot["firm"]
    clients = snapshot["clients"]

    # firm.total == sum(client.total)
    sum_clients = sum(c["total_usd"] for c in clients)
    if abs(firm["total_usd"] - sum_clients) > EPS:
        raise InvariantError(f"firm.total_usd {firm['total_usd']} != sum(clients) {sum_clients}")

    # total == deployed + idle at firm level, and firm split == sum(client split)
    if abs(firm["total_usd"] - (firm["deployed_usd"] + firm["idle_usd"])) > EPS:
        raise InvariantError(
            f"firm.total_usd {firm['total_usd']} != deployed {firm['deployed_usd']} + idle {firm['idle_usd']}")
    sum_dep = sum(c["deployed_usd"] for c in clients)
    sum_idle = sum(c["idle_usd"] for c in clients)
    if abs(firm["deployed_usd"] - sum_dep) > EPS:
        raise InvariantError(f"firm.deployed_usd {firm['deployed_usd']} != sum(clients) {sum_dep}")
    if abs(firm["idle_usd"] - sum_idle) > EPS:
        raise InvariantError(f"firm.idle_usd {firm['idle_usd']} != sum(clients) {sum_idle}")

    # per denom: firm bucket == sum(client bucket)
    for b in BUCKET_ORDER:
        sub = sum(c["buckets"][b]["value_usd"] for c in clients)
        if abs(firm["buckets"][b]["value_usd"] - sub) > EPS:
            raise InvariantError(f"firm.buckets[{b}] {firm['buckets'][b]['value_usd']} != sum(clients) {sub}")

    # firm unclassified == sum(client unclassified)
    sub_unc = sum(c["unclassified"]["value_usd"] for c in clients)
    if abs(firm["unclassified"]["value_usd"] - sub_unc) > EPS:
        raise InvariantError(f"firm.unclassified {firm['unclassified']['value_usd']} != sum(clients) {sub_unc}")

    # shares sum to 100 (when firm total > 0)
    if firm["total_usd"] > 0:
        share_sum = sum(c["share_pct"] for c in clients)
        if abs(share_sum - 100.0) > 0.5:
            raise InvariantError(f"sum(share_pct) {share_sum} != 100")

    # per client: total == sum(wallet totals)  AND  total == sum(buckets)+unclassified
    for c in clients:
        wsum = sum(w["total_usd"] for w in c["wallets"])
        if abs(c["total_usd"] - wsum) > EPS:
            raise InvariantError(f"client {c['name']} total {c['total_usd']} != sum(wallets) {wsum}")
        if abs(c["total_usd"] - (c["deployed_usd"] + c["idle_usd"])) > EPS:
            raise InvariantError(
                f"client {c['name']} total {c['total_usd']} != deployed {c['deployed_usd']} + idle {c['idle_usd']}")
        bsum = sum(c["buckets"][b]["value_usd"] for b in BUCKET_ORDER) + c["unclassified"]["value_usd"]
        if abs(c["total_usd"] - bsum) > EPS:
            raise InvariantError(f"client {c['name']} total {c['total_usd']} != buckets+unclassified {bsum}")
