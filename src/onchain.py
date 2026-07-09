"""On-chain coverage supplement.

vaults.fyi is a vault-yield API, not a portfolio API: it misses plain ERC-20
holdings it doesn't index (weETH, osGNO, COW, NXM, wstETH, ...), which a
DeBank-style full portfolio view includes. Reconciliation on 2026-07-07 showed
a ~17% AUM undercount, most of it plain token balances sitting in the managed
safes. This module reads those balances directly from public RPCs (eth_call
balanceOf) and prices them via CoinGecko, emitting normalized positions.

Soundness rules:
  * Value counts toward AUM (they are assets in the managed safes).
  * They are EXCLUDED from every APY/alpha figure (apy_excluded flag) — we do
    not measure their yield, and fabricating one would corrupt the alpha story.
  * Dedup guard: a supplement is dropped if vaults.fyi already returned that
    (wallet, symbol) with value > 0, so future indexing improvements upstream
    can never double count.
  * Best-effort: any RPC/pricing failure skips that entry with a log line and
    never blocks the publish.

Config (config.yaml):
  supplements:
    enabled: true
    tokens:
      - { client: ENS, wallet: "0x...", chain: ethereum, symbol: weETH,
          address: "0x...", decimals: 18, coingecko: wrapped-eeth,
          protocol: ether.fi, name: "ether.fi weETH" }
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("onchain")

RPCS = {
    "ethereum": ["https://ethereum.publicnode.com", "https://eth.drpc.org", "https://cloudflare-eth.com"],
    "gnosis": ["https://gnosis.publicnode.com", "https://gnosis.drpc.org", "https://rpc.gnosischain.com"],
}


def _balance_of(chain: str, token: str, wallet: str) -> float | None:
    """ERC-20 balanceOf via raw eth_call. Returns raw units (pre-decimals)."""
    data = "0x70a08231" + wallet.lower().replace("0x", "").rjust(64, "0")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
               "params": [{"to": token, "data": data}, "latest"]}
    for rpc in RPCS.get(chain, []):
        try:
            r = requests.post(rpc, json=payload, timeout=15)
            result = r.json().get("result")
            if result and result != "0x":
                return float(int(result, 16))
        except Exception:  # noqa: BLE001 — try next RPC
            continue
    return None


def _prices(ids: list[str]) -> dict[str, float]:
    """CoinGecko spot prices (USD) for a list of coin ids. Best-effort."""
    if not ids:
        return {}
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": ",".join(sorted(set(ids))), "vs_currencies": "usd"},
                         timeout=20)
        r.raise_for_status()
        return {k: v.get("usd") for k, v in r.json().items() if v.get("usd")}
    except Exception as exc:  # noqa: BLE001
        log.warning("CoinGecko pricing failed: %s", type(exc).__name__)
        return {}


def build_positions_supplement(config: dict, existing: list[dict]) -> list[dict]:
    """Fetch configured supplemental token balances. `existing` = positions
    already produced by vaults.fyi, used for the double-count guard."""
    scfg = config.get("supplements", {})
    if not scfg.get("enabled"):
        return []
    entries = scfg.get("tokens", [])
    covered = {(str(p.get("address", "")).lower(), str(p.get("symbol", "")).upper())
               for p in existing if p.get("value_usd", 0) > 0}
    prices = _prices([e["coingecko"] for e in entries if e.get("coingecko")])

    out: list[dict] = []
    for e in entries:
        wallet, symbol = str(e.get("wallet", "")).lower(), str(e.get("symbol", "")).upper()
        # Dedup guard applies to balanceOf entries only: a wallet-level token read
        # duplicates vaults.fyi coverage of the same token. fixed_native entries
        # are manually curated distinct positions (staking pools, LP NFTs) and may
        # legitimately share a symbol with indexed positions.
        if e.get("fixed_native") is None and (wallet, symbol) in covered:
            log.info("Supplement %s/%s already covered by vaults.fyi; skipped.", e.get("client"), symbol)
            continue
        px = prices.get(e.get("coingecko"))
        if not px:
            log.warning("No price for supplement %s (%s); skipped.", symbol, e.get("coingecko"))
            continue
        if e.get("fixed_native") is not None:
            # manually tracked quantity (position locked in a protocol contract)
            native = float(e["fixed_native"])
        else:
            raw = _balance_of(e.get("chain", "ethereum"), e["address"], e["wallet"])
            if raw is None:
                log.warning("balanceOf failed for %s %s; skipped.", e.get("client"), symbol)
                continue
            native = raw / (10 ** int(e.get("decimals", 18)))
        value = native * px
        if value < float(config.get("vaultsfyi", {}).get("min_usd_value_threshold", 0.5)):
            continue
        out.append({
            "client": e["client"], "address": e["wallet"], "chain": e.get("chain", "ethereum"),
            "network": e.get("chain", "ethereum"), "symbol": e.get("symbol"),
            "name": e.get("name") or e.get("symbol"), "protocol": e.get("protocol") or "",
            "value_usd": round(value, 2), "native_balance": native, "asset_price_usd": px,
            "apy_pct": 0.0, "unclaimed_usd": 0.0, "is_idle": bool(e.get("idle", False)),
            "apy_excluded": True,               # never enters APY / alpha math
            "source": "onchain-supplement",
        })
        log.info("Supplement %s %s: $%s", e["client"], symbol, format(round(value), ","))
    return out
