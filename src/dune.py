"""Dune Analytics client.

Dune is the secondary source with three jobs:
  1. Wallet registry of record  -> extract the hardcoded address set per client
     from each client's "Moved funds" query SQL (for the config drift check).
  2. Reconciliation             -> onchain current balances per client to compare
     against the vaults.fyi-derived value (coverage-gap closure).
  3. Complementary KPI          -> cumulative "funds moved to date" from the
     aggregator query (never summed into AUM).

Dune is secondary: every method here degrades gracefully (returns None / empty +
a log note) instead of raising, so a Dune hiccup never blocks the daily publish.
The hard publish gates are driven by vaults.fyi data, not Dune.

API notes (Dune REST v1):
  base        https://api.dune.com/api/v1
  auth header X-Dune-API-Key
  query SQL   GET  /query/{id}           -> { query_sql, ... }
  latest      GET  /query/{id}/results   -> last cached execution (no credits spent)

Result-row column names vary by query, so the parsers below sniff likely columns
(address / total_usd / value) defensively rather than hardcoding a brittle schema.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

log = logging.getLogger("dune")

ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
CHAIN_KEYWORDS = [
    "ethereum", "mainnet", "gnosis", "xdai", "arbitrum", "optimism",
    "base", "polygon", "avalanche", "bsc", "celo", "linea",
]


class DuneClient:
    def __init__(self, api_key: str, base_url: str = "https://api.dune.com/api/v1", timeout: int = 60):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({"X-Dune-API-Key": api_key})

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        if not self.api_key:
            log.warning("Dune API key not set; skipping %s", path)
            return None
        try:
            resp = self.session.get(f"{self.base_url}/{path.lstrip('/')}", params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log.warning("Dune GET %s failed: %s", path, exc)
            return None

    # ---- 1. Wallet registry of record -------------------------------------

    def get_query_sql(self, query_id: int) -> str | None:
        payload = self._get(f"/query/{query_id}")
        if not payload:
            return None
        # The SQL may live under a few keys depending on API version.
        for key in ("query_sql", "sql", "query"):
            if isinstance(payload.get(key), str):
                return payload[key]
        return None

    def get_registry(self, query_id: int) -> dict | None:
        """Extract the hardcoded {addresses, chains} a client's query pins.
        Returns None if the SQL is unavailable (drift check then skipped)."""
        sql = self.get_query_sql(query_id)
        if sql is None:
            return None
        addresses = sorted({m.lower() for m in ADDRESS_RE.findall(sql)})
        low = sql.lower()
        chains = sorted({c for c in CHAIN_KEYWORDS if c in low})
        return {"addresses": addresses, "chains": chains}

    # ---- 2. Reconciliation: onchain current balances ----------------------

    def get_onchain_balances(self, balances_query_id: int | None, client_name: str | None = None) -> list[dict] | None:
        """Latest cached results of a current-balances query keyed on the shared
        wallet registry. Returns rows, or None when no query is configured
        (caller then skips reconciliation with a log note)."""
        if not balances_query_id:
            return None
        payload = self._get(f"/query/{balances_query_id}/results")
        if not payload:
            return None
        return _rows(payload)

    # ---- 3. Complementary KPI: funds moved to date ------------------------

    def get_moved_funds(self, aggregator_query_id: int) -> dict | None:
        """Cumulative deployment volume (a flow, never AUM). Returns
        {total_usd, per_client: {name: usd}} best-effort, or None."""
        payload = self._get(f"/query/{aggregator_query_id}/results")
        if not payload:
            return None
        rows = _rows(payload)
        if not rows:
            return None
        total = 0.0
        per_client: dict[str, float] = {}
        grand: float | None = None
        for row in rows:
            name = _sniff(row, ["client", "name", "dao", "entity"])
            usd = _sniff_num(row, ["total_usd", "moved_usd", "volume_usd", "usd", "amount_usd", "value"])
            if usd is None:
                continue
            if name and str(name).strip().lower() in {"total", "grand total", "all", "firm"}:
                grand = usd
                continue
            if name:
                per_client[str(name)] = usd
            total += usd
        return {"total_usd": (grand if grand is not None else round(total, 2)), "per_client": per_client}


# ---- defensive row helpers (schema sniffers) ------------------------------

def _rows(payload: dict) -> list[dict]:
    result = payload.get("result") or {}
    rows = result.get("rows")
    if isinstance(rows, list):
        return rows
    return []


def _sniff(row: dict, candidates: list[str]) -> Any:
    lower = {k.lower(): v for k, v in row.items()}
    for c in candidates:
        if c in lower and lower[c] not in (None, ""):
            return lower[c]
    return None


def _sniff_num(row: dict, candidates: list[str]) -> float | None:
    val = _sniff(row, candidates)
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None
