"""Reconcile vaults.fyi-derived value against Dune onchain balances, and check
the config wallet registry for drift versus the Dune registry of record.

vaults.fyi misses OTC / raw LP / locked positions; Dune reads raw transfers and
sees everything. The gap quantifies coverage so it is visible, not hidden, and
feeds the soft validation warnings.
"""

from __future__ import annotations

import logging

from .dune import _sniff, _sniff_num

log = logging.getLogger("reconcile")


def _client_address_map(config: dict) -> dict[str, str]:
    """address(lower) -> client name, from config."""
    out: dict[str, str] = {}
    for c in config.get("clients", []):
        for a in (c.get("addresses") or []):
            addr = str(a.get("address", "")).lower()
            if addr:
                out[addr] = c["name"]
    return out


def _onchain_by_client(rows: list[dict], config: dict) -> dict[str, float]:
    """Aggregate onchain USD per client from balances-query rows. Matches a
    client column when present, else maps row addresses back to the registry."""
    addr_map = _client_address_map(config)
    by_client: dict[str, float] = {}
    for row in rows:
        usd = _sniff_num(row, ["balance_usd", "total_usd", "usd_value", "value_usd", "usd", "value"])
        if usd is None:
            continue
        name = _sniff(row, ["client", "name", "dao", "entity"])
        if not name:
            addr = _sniff(row, ["address", "wallet", "account", "holder"])
            if addr:
                name = addr_map.get(str(addr).lower())
        if not name:
            continue
        by_client[str(name)] = by_client.get(str(name), 0.0) + usd
    return by_client


def _gap_pct(onchain: float, vaultsfyi: float) -> float:
    """Signed coverage gap as a percent of onchain. Positive => vaults.fyi
    undercounts versus onchain balances."""
    if not onchain:
        return 0.0
    return round((onchain - vaultsfyi) / onchain * 100.0, 2)


def reconcile(snapshot: dict, balances_rows: list[dict] | None, config: dict) -> list[str]:
    """Populate reconciliation blocks on firm + clients and return soft-warning
    strings for any gap exceeding the configured threshold. Skips gracefully
    (with a log note) when no balances data is available."""
    if not balances_rows:
        log.info("No Dune balances available; reconciliation skipped.")
        return []

    threshold = float(config.get("dune", {}).get("reconcile_gap_alert_pct", 10))
    by_client = _onchain_by_client(balances_rows, config)
    warnings: list[str] = []

    firm_onchain = 0.0
    for c in snapshot["clients"]:
        oc = round(by_client.get(c["name"], 0.0), 2)
        gap = _gap_pct(oc, c["total_usd"])
        c["reconciliation"] = {"onchain_usd": oc, "coverage_gap_pct": gap}
        firm_onchain += oc
        if oc and abs(gap) > threshold:
            warnings.append(
                f"Reconciliation gap for {c['name']}: vaults.fyi ${c['total_usd']:,.0f} vs "
                f"onchain ${oc:,.0f} ({gap:+.1f}%)"
            )

    firm = snapshot["firm"]
    firm_gap = _gap_pct(firm_onchain, firm["total_usd"])
    firm["reconciliation"] = {"onchain_usd": round(firm_onchain, 2), "coverage_gap_pct": firm_gap, "source": "dune"}
    if firm_onchain and abs(firm_gap) > threshold:
        warnings.append(
            f"Reconciliation gap for firm: vaults.fyi ${firm['total_usd']:,.0f} vs "
            f"onchain ${firm_onchain:,.0f} ({firm_gap:+.1f}%)"
        )
    return warnings


def registry_drift(config: dict, dune_client) -> list[str]:
    """Compare each client's config addresses against the Dune query registry.
    Returns warning strings for any divergence. Skips clients whose Dune SQL
    cannot be read (logged, not failed)."""
    warnings: list[str] = []
    for c in config.get("clients", []):
        qid = c.get("dune_source_query_id")
        if not qid:
            continue
        registry = dune_client.get_registry(qid)
        if registry is None:
            log.info("Could not read Dune registry for %s (query %s); drift check skipped.", c["name"], qid)
            continue
        config_addrs = {str(a.get("address", "")).lower() for a in (c.get("addresses") or []) if a.get("address")}
        dune_addrs = set(registry["addresses"])
        # Placeholder addresses (all-zero-ish) are not real config entries.
        config_addrs = {a for a in config_addrs if not _is_placeholder(a)}
        missing_in_config = dune_addrs - config_addrs
        extra_in_config = config_addrs - dune_addrs
        if missing_in_config or extra_in_config:
            parts = []
            if missing_in_config:
                parts.append(f"{len(missing_in_config)} in Dune not in config")
            if extra_in_config:
                parts.append(f"{len(extra_in_config)} in config not in Dune")
            warnings.append(f"Registry drift for {c['name']} (query {qid}): " + "; ".join(parts))
    return warnings


def _is_placeholder(addr: str) -> bool:
    stripped = addr.lower().replace("0x", "").strip("0")
    return stripped == "" or len(stripped) <= 1
