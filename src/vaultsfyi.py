"""vaults.fyi V2 API client.

Primary source for yield and covered balances.

Endpoints and field names below were verified against the live docs
(https://docs.vaults.fyi/llms.txt and the api-reference pages) on 2026-06-15:

  positions   GET /v2/portfolio/positions/{userAddress}
  idle assets GET /v2/portfolio/idle-assets/{userAddress}
  benchmarks  GET /v2/benchmarks/{network}?code=usd|eth

Both portfolio endpoints return  { "data": [ ... ], "errors": { ... } }
(no cursor / nextPage pagination on these two).

ALL schema-coupled parsing lives in the small extractor functions at the bottom
of this file (extract_position / extract_idle_asset / extract_benchmark_total).
If vaults.fyi renames a field, fix it there and nowhere else.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

import requests

# Map config chain aliases -> vaults.fyi network identifiers.
# Ethereum is "mainnet" upstream; everything else is identity.
CHAIN_ALIASES = {
    "ethereum": "mainnet",
    "eth": "mainnet",
    "mainnet": "mainnet",
    "gnosis": "gnosis",
    "xdai": "gnosis",
    "base": "base",
    "arbitrum": "arbitrum",
    "arbitrum_one": "arbitrum",
    "optimism": "optimism",
    "polygon": "polygon",
}

# vaults.fyi network id -> friendly display chain name (for output).
NETWORK_DISPLAY = {
    "mainnet": "ethereum",
    "gnosis": "gnosis",
    "base": "base",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "polygon": "polygon",
}


def to_network(chain_alias: str) -> str:
    return CHAIN_ALIASES.get(str(chain_alias).strip().lower(), str(chain_alias).strip().lower())


def _to_float(value: Any, default: float = 0.0) -> float:
    """Coerce numeric-or-string-or-None into float. Upstream returns some
    numbers as strings (idle assets do); never trust the type."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return default


class VaultsFyiClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.vaults.fyi/v2",
        networks: Iterable[str] | None = None,
        apy_window: str = "7day",
        min_usd_value_threshold: float = 0.5,
        max_retries: int = 5,
        timeout: int = 30,
        session: requests.Session | None = None,
    ):
        if not api_key:
            raise ValueError("VaultsFyiClient requires an API key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        # Default upstream excludes gnosis; always send an explicit allow list.
        self.networks = list(networks) if networks else ["mainnet", "gnosis", "base", "arbitrum", "optimism", "polygon"]
        self.apy_window = apy_window
        self.min_usd_value_threshold = min_usd_value_threshold
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"x-api-key": api_key, "accept": "application/json"})

    # ---- HTTP with exponential backoff on 429 / 5xx -----------------------

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        backoff = 1.0
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:  # network error
                last_exc = exc
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                # Respect Retry-After when present.
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if (retry_after and retry_after.isdigit()) else backoff
                time.sleep(wait)
                backoff = min(backoff * 2, 30)
                continue
            resp.raise_for_status()
            return resp.json()
        if last_exc:
            raise last_exc
        raise RuntimeError(f"vaults.fyi GET {path} failed after {self.max_retries} retries")

    # ---- Public methods ---------------------------------------------------

    def get_positions(self, address: str, networks: Iterable[str] | None = None,
                      apy_window: str | None = None) -> list[dict]:
        """Active vault positions for a wallet across the allowed networks.
        `apy_window` overrides the client default for this call (used to fetch
        pocket APY at several windows for the benchmark comparison)."""
        params = {
            "allowedNetworks": list(networks) if networks else self.networks,
            "apyInterval": apy_window or self.apy_window,
            "minUsdAssetValueThreshold": self.min_usd_value_threshold,
        }
        payload = self._get(f"/portfolio/positions/{address}", params=params)
        return list(payload.get("data", []) or [])

    def get_idle_assets(self, address: str, networks: Iterable[str] | None = None) -> list[dict]:
        """Uninvested (idle) wallet token balances across allowed networks."""
        params = {
            "allowedNetworks": list(networks) if networks else self.networks,
            "minUsdAssetValueThreshold": self.min_usd_value_threshold,
        }
        payload = self._get(f"/portfolio/idle-assets/{address}", params=params)
        return list(payload.get("data", []) or [])

    def get_benchmark(self, network: str, code: str = "usd") -> dict:
        """Benchmark APYs for a network. code is 'usd' or 'eth'."""
        return self._get(f"/benchmarks/{network}", params={"code": code})


# ---------------------------------------------------------------------------
# EXTRACTORS  (the ONLY schema-coupled code; a field rename is a one-line fix)
# ---------------------------------------------------------------------------

def _apy_total_pct(apy_obj: Any) -> float:
    """Positions return apy as {base, reward, total} for the selected
    apyInterval window. Raw decimal -> percent. Missing -> 0."""
    if not isinstance(apy_obj, dict):
        return 0.0
    # Already-flattened single-window shape (what /positions returns).
    if "total" in apy_obj:
        return _to_float(apy_obj.get("total")) * 100.0
    return 0.0


def _position_value_usd(asset: dict, lp: dict) -> float:
    """The PER-POSITION USD value (the vault-share value).

    Field-mapping is subtle and getting it wrong multi-counts AUM:
      * `lpToken.balanceUsd` is the value of THIS vault position — the source of
        truth when present.
      * `asset.positionValueInAsset` (raw underlying units / 10^decimals ×
        assetPriceInUsd) is the equivalent and matches lpToken.balanceUsd.
      * `asset.balanceUsd` is the wallet's TOTAL balance of the underlying asset,
        REPEATED on every position that holds it (e.g. four USDC vaults all show
        the same asset.balanceUsd). It is therefore a LAST resort only — used for
        the offline mock payloads, which carry no lpToken/positionValueInAsset."""
    v = _to_float(lp.get("balanceUsd"))
    if v:
        return v
    pva = asset.get("positionValueInAsset")
    if pva not in (None, ""):
        return _position_native(asset, lp) * _to_float(asset.get("assetPriceInUsd"))
    return _to_float(asset.get("balanceUsd"))


def _position_native(asset: dict, lp: dict) -> float:
    """Native-unit balance of THIS position, in underlying-asset units.
    Prefers positionValueInAsset (per-position) over asset.balanceNative (which,
    like asset.balanceUsd, is the repeated wallet-level asset total)."""
    pva = asset.get("positionValueInAsset")
    if pva not in (None, ""):
        try:
            decimals = int(asset.get("decimals", 18))
        except (TypeError, ValueError):
            decimals = 18
        return _to_float(pva) / (10 ** decimals)
    n = _to_float(asset.get("balanceNative"))
    if n:
        return n
    return _to_float(lp.get("balanceNative"))


def extract_position(raw: dict, client: str, wallet: str, chain_alias: str) -> dict:
    """Normalize one /portfolio/positions item into the internal record."""
    asset = raw.get("asset") or {}
    lp = raw.get("lpToken") or {}
    network = raw.get("network") or {}
    api_network = network.get("name") or to_network(chain_alias)
    return {
        "client": client,
        "address": wallet,
        "chain": NETWORK_DISPLAY.get(api_network, chain_alias),
        "network": api_network,
        "symbol": asset.get("symbol") or raw.get("name") or "UNKNOWN",
        "name": raw.get("name") or asset.get("name") or "",
        "protocol": (raw.get("protocol") or {}).get("name") or "",
        "value_usd": _position_value_usd(asset, lp),
        "native_balance": _position_native(asset, lp),
        "asset_price_usd": _to_float(asset.get("assetPriceInUsd")),
        "apy_pct": _apy_total_pct(raw.get("apy")),
        "unclaimed_usd": _to_float(asset.get("unclaimedUsd")),
        "is_idle": False,
        "source": "vaultsfyi",
    }


def extract_idle_asset(raw: dict, client: str, wallet: str, chain_alias: str) -> dict:
    """Normalize one /portfolio/idle-assets item. Idle tokens earn no yield."""
    network = raw.get("network") or {}
    api_network = network.get("name") or to_network(chain_alias)
    return {
        "client": client,
        "address": wallet,
        "chain": NETWORK_DISPLAY.get(api_network, chain_alias),
        "network": api_network,
        "symbol": raw.get("symbol") or "UNKNOWN",
        "name": raw.get("name") or "",
        "protocol": "",
        "value_usd": _to_float(raw.get("balanceUsd")),
        "native_balance": _to_float(raw.get("balanceNative")),
        "asset_price_usd": _to_float(raw.get("assetPriceInUsd")),
        "apy_pct": 0.0,
        "unclaimed_usd": 0.0,
        "is_idle": True,
        "source": "vaultsfyi",
    }


def extract_benchmark_total(raw: dict, window: str = "7day") -> float | None:
    """Pull the total benchmark APY (percent) for a window from a /benchmarks
    response: { apy: { '1day': {base,reward,total}, '7day': {...}, ... } }."""
    apy = (raw or {}).get("apy")
    if not isinstance(apy, dict):
        return None
    window_obj = apy.get(window)
    if not isinstance(window_obj, dict) or window_obj.get("total") is None:
        return None
    return _to_float(window_obj.get("total")) * 100.0
