"""Pipeline orchestrator.

  python -m src.pipeline --sample     # offline, no key, bundled mock payloads
  python -m src.pipeline              # live run (needs VAULTSFYI_API_KEY)

Flow: build positions -> merge manual adjustments -> idle filter -> reference
prices -> aggregate (asserts invariants) -> Dune drift + reconcile + moved funds
-> validate against last published snapshot -> on hard failure hold last good,
alert, exit non-zero; otherwise write latest.json / data.js / history.csv.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

import yaml

from . import aggregate, reconcile as reconcile_mod, validate as validate_mod
from .alerts import send_alert
from .aggregate import InvariantError
from .benchmarks import fetch_benchmarks
from .sheets import read_adjustments, mirror_snapshot
from .vaultsfyi import VaultsFyiClient, extract_position, extract_idle_asset, to_network

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pipeline")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DASHBOARD = ROOT / "dashboard"
LATEST = DATA / "latest.json"
HISTORY_CSV = DATA / "history.csv"
DATA_JS = DASHBOARD / "data.js"
CONFIGURATOR = ROOT / "configurator.json"


# --------------------------------------------------------------------------
# env + config
# --------------------------------------------------------------------------

def load_env() -> None:
    """Load .env (gitignored) into os.environ without overriding real env vars.
    Never logs values."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = val.strip().strip('"').strip("'")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_configurator(path: Path | None = None) -> dict:
    """Load the gitignored JSON configurator (API keys + managed client roster
    with their wallet addresses). Returns {} when absent or unreadable, so the
    pipeline falls back cleanly to config.yaml."""
    p = path or CONFIGURATOR
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read configurator %s: %s", p.name, type(exc).__name__)
        return {}


def apply_configurator(config: dict, configurator: dict) -> dict:
    """Overlay configurator.json onto config.yaml. The configurator is the single
    control surface for secrets and the managed client roster:

      * API keys populate the env vars named in config.yaml, but only when the
        variable is not already set, so real env vars / CI secrets always win.
      * a non-empty `clients` list REPLACES config.yaml's roster, so the dashboard
        shows exactly the entities pasted into the configurator. A client may carry
        several addresses, each spanning several chains; placeholder addresses
        (anything that is not a real 0x + 40-hex address) are dropped, so a client
        with no real address yet simply contributes zero rather than erroring.
    """
    if not configurator:
        return config

    key_map = {
        "vaultsfyi_api_key": config.get("vaultsfyi", {}).get("api_key_env", "VAULTSFYI_API_KEY"),
        "dune_api_key": config.get("dune", {}).get("api_key_env", "DUNE_API_KEY"),
        "slack_webhook_url": config.get("alerts", {}).get("slack_webhook_env", "SLACK_WEBHOOK_URL"),
    }
    for json_key, env_name in key_map.items():
        val = str(configurator.get(json_key, "") or "").strip()
        if val and env_name and not os.environ.get(env_name):
            os.environ[env_name] = val

    clients = configurator.get("clients")
    if clients:
        cleaned = []
        for c in clients:
            entry = dict(c)
            entry["addresses"] = [
                a for a in (c.get("addresses") or [])
                if a.get("address") and _is_real_address(a["address"])
            ]
            cleaned.append(entry)
        config["clients"] = cleaned
    return config


def _is_real_address(addr: str) -> bool:
    """True only for a real EVM address (0x + 40 hex, not all-zero). Rejects the
    PASTE_... placeholders shipped in the configurator template."""
    s = str(addr).strip().lower()
    if not (s.startswith("0x") and len(s) == 42):
        return False
    body = s[2:]
    return all(ch in "0123456789abcdef" for ch in body) and body.strip("0") != ""


# --------------------------------------------------------------------------
# position sourcing
# --------------------------------------------------------------------------

def build_positions_sample(sample_path: Path) -> tuple[list[dict], list[str]]:
    """Load bundled mock payloads and run them through the SAME extractors as
    live, so the offline path exercises real parsing code."""
    payload = json.loads(sample_path.read_text(encoding="utf-8"))
    positions: list[dict] = []
    for entry in payload.get("wallets", []):
        client = entry["client"]
        wallet = entry["address"]
        chain = (entry.get("chains") or ["ethereum"])[0]
        for raw in entry.get("positions", []):
            positions.append(extract_position(raw, client, wallet, chain))
        for raw in entry.get("idle_assets", []):
            positions.append(extract_idle_asset(raw, client, wallet, chain))
    return positions, []


def build_positions_live(config: dict) -> tuple[list[dict], list[str]]:
    """Fetch positions + idle assets for every configured wallet. A wallet that
    raises after retries is recorded as a fetch failure (hard gate)."""
    vcfg = config.get("vaultsfyi", {})
    api_key = os.environ.get(vcfg.get("api_key_env", "VAULTSFYI_API_KEY"), "")
    client = VaultsFyiClient(
        api_key=api_key,
        base_url=vcfg.get("base_url", "https://api.vaults.fyi/v2"),
        networks=vcfg.get("networks"),
        apy_window=config.get("settings", {}).get("apy_window", "7day"),
        min_usd_value_threshold=vcfg.get("min_usd_value_threshold", 0.5),
    )
    positions: list[dict] = []
    failures: list[str] = []
    for c in config.get("clients", []):
        for addr_entry in (c.get("addresses") or []):
            wallet = addr_entry.get("address")
            if not wallet or _is_placeholder(wallet):
                continue
            networks = [to_network(ch) for ch in (addr_entry.get("chains") or [])] or vcfg.get("networks")
            fallback_chain = (addr_entry.get("chains") or ["ethereum"])[0]
            try:
                for raw in client.get_positions(wallet, networks=networks):
                    positions.append(extract_position(raw, c["name"], wallet, fallback_chain))
                for raw in client.get_idle_assets(wallet, networks=networks):
                    positions.append(extract_idle_asset(raw, c["name"], wallet, fallback_chain))
            except Exception as exc:  # noqa: BLE001 - any failure is a hard gate
                log.error("Fetch failed for %s %s: %s", c["name"], wallet, type(exc).__name__)
                failures.append(f"{c['name']} {wallet}")
    return positions, failures


def _is_placeholder(addr: str) -> bool:
    stripped = str(addr).lower().replace("0x", "").strip("0")
    return stripped == "" or len(stripped) <= 1


# --------------------------------------------------------------------------
# reference prices
# --------------------------------------------------------------------------

def resolve_reference_prices(positions: list[dict], config: dict, sample: bool) -> dict:
    refs = aggregate.derive_reference_prices(positions)
    if sample or not config.get("settings", {}).get("allow_coingecko", True):
        return refs
    if refs.get("eth_usd") and refs.get("eur_usd"):
        return refs
    cg = _coingecko_prices()
    if cg:
        refs["eth_usd"] = refs.get("eth_usd") or cg.get("eth_usd")
        refs["eur_usd"] = refs.get("eur_usd") or cg.get("eur_usd")
    return refs


def _coingecko_prices() -> dict | None:
    """Fallback ETH/USD and EUR/USD via one CoinGecko call. Best-effort."""
    try:
        import requests
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum", "vs_currencies": "usd,eur"},
            timeout=15,
        )
        resp.raise_for_status()
        eth = resp.json().get("ethereum", {})
        eth_usd, eth_eur = eth.get("usd"), eth.get("eur")
        eur_usd = (eth_usd / eth_eur) if (eth_usd and eth_eur) else None
        return {"eth_usd": eth_usd, "eur_usd": eur_usd}
    except Exception as exc:  # noqa: BLE001
        log.warning("CoinGecko fallback failed: %s", type(exc).__name__)
        return None


# --------------------------------------------------------------------------
# history
# --------------------------------------------------------------------------

def read_history() -> list[dict]:
    if not HISTORY_CSV.exists():
        return []
    with open(HISTORY_CSV, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_history(rows: list[dict]) -> None:
    fieldnames: list[str] = []
    for r in rows:
        for k in r:
            if k not in fieldnames:
                fieldnames.append(k)
    DATA.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def build_history_row(snapshot: dict) -> dict:
    firm = snapshot["firm"]
    row = {
        "date": snapshot["date"],
        "firm_total_usd": firm["total_usd"],
        "USD": firm["buckets"]["USD"]["value_usd"],
        "ETH": firm["buckets"]["ETH"]["value_usd"],
        "EUR": firm["buckets"]["EUR"]["value_usd"],
    }
    for c in snapshot["clients"]:
        row[f"client:{c['name']}"] = c["total_usd"]
    # Alpha inputs (per denomination): deployed capital, deployed-weighted APY, and
    # the benchmark rate — persisted so a sound cumulative-alpha series can accrue
    # forward (integral of daily excess-yield spread × capital). These cannot be
    # reconstructed from value history, so they only exist from the first run that
    # writes them onward.
    b = snapshot.get("benchmarks") or {}
    if b.get("enabled"):
        cur = b.get("current_window")
        for code in ("USD", "ETH"):
            bk = firm["buckets"][code]
            row[f"{code}_dep"] = bk["deployed_usd"]
            row[f"{code}_apy"] = bk["avg_apy_pct"]
            row[f"{code}_bench"] = (b.get(code, {}).get("benchmark", {}) or {}).get(cur)
    return row


def build_history_series(rows: list[dict], snapshot: dict, points: int) -> dict:
    rows = rows[-points:]
    client_names = [c["name"] for c in snapshot["clients"]]

    def series(col: str) -> list[dict]:
        out = []
        for r in rows:
            if r.get(col) not in (None, ""):
                try:
                    out.append({"date": r["date"], "value": round(float(r[col]), 2)})
                except (ValueError, TypeError):
                    continue
        return out

    return {
        "firm_total": series("firm_total_usd"),
        "by_denom": {"USD": series("USD"), "ETH": series("ETH"), "EUR": series("EUR")},
        "by_client": {name: series(f"client:{name}") for name in client_names},
        "cumulative_alpha": _cumulative_alpha_series(rows),
    }


def _cumulative_alpha_series(rows: list[dict]) -> list[dict]:
    """Sound cumulative excess return ($) vs benchmark, accrued from the daily
    per-denomination spread: for each interval, Σ_denom deployed × (APY − benchmark)
    /100 × (days/365). Rows lacking the alpha-input columns (older rows written
    before this was added) are skipped, so the series begins when those inputs
    first appear. NOT derived from AUM changes (which are flow/price contaminated)."""
    out: list[dict] = []
    cum = 0.0
    prev: dt.date | None = None
    for r in rows:
        try:
            denom = {c: (float(r[f"{c}_dep"]), float(r[f"{c}_apy"]), float(r[f"{c}_bench"]))
                     for c in ("USD", "ETH")}
            day = dt.date.fromisoformat(r["date"])
        except (KeyError, ValueError, TypeError):
            continue  # row predates alpha inputs, or malformed
        if prev is not None:
            days = (day - prev).days or 1
            for dep, apy, bench in denom.values():
                cum += dep * (apy - bench) / 100.0 * (days / 365.0)
        prev = day
        out.append({"date": r["date"], "value": round(cum, 2)})
    return out


# --------------------------------------------------------------------------
# snapshot assembly + writers
# --------------------------------------------------------------------------

def assemble_snapshot(core: dict, config: dict, mode: str, moved_funds: dict | None,
                      benchmarks: dict, now: dt.datetime) -> dict:
    snapshot = {
        "date": now.strftime("%Y-%m-%d"),
        "updated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "mode": mode,
        "apy_window": config.get("settings", {}).get("apy_window", "7day"),
        "reference_prices": core["reference_prices"],
        "firm": core["firm"],
        "clients": core["clients"],
        "history": {"firm_total": [], "by_denom": {"USD": [], "ETH": [], "EUR": []}, "by_client": {}},
        "moved_funds": {},
        "benchmarks": benchmarks or {},
    }
    mf_cfg = config.get("moved_funds", {})
    src_qid = mf_cfg.get("source_query_id", config.get("dune", {}).get("aggregator_query_id"))
    snapshot["moved_funds"] = {
        "total_usd": (moved_funds.get("total_usd", 0) if moved_funds else 0),
        "label": mf_cfg.get("label", "Funds moved to date"),
        "source": f"dune:{src_qid}",
        "show_on_dashboard": bool(mf_cfg.get("show_on_dashboard", False)),
    }
    return snapshot


def write_outputs(snapshot: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    DASHBOARD.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    DATA_JS.write_text("window.__AUM_DATA__ = " + json.dumps(snapshot, indent=2) + ";\n", encoding="utf-8")
    log.info("Wrote %s and %s", LATEST.name, DATA_JS.name)


def load_previous() -> dict | None:
    if not LATEST.exists():
        return None
    try:
        return json.loads(LATEST.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run(sample: bool, config_path: str) -> int:
    load_env()
    config = load_config(config_path)
    config = apply_configurator(config, load_configurator())
    settings = config.get("settings", {})
    webhook = os.environ.get(config.get("alerts", {}).get("slack_webhook_env", "SLACK_WEBHOOK_URL"))
    now = dt.datetime.now(dt.timezone.utc)
    mode = "sample" if sample else "live"

    # 1. positions
    if sample:
        positions, failures = build_positions_sample(DATA / "sample_positions.json")
    else:
        positions, failures = build_positions_live(config)

    # 1b. on-chain coverage supplement (live only): plain token balances the
    #     vaults.fyi indexer misses but which sit in the managed safes. Counted
    #     in AUM, excluded from APY/alpha (apy_excluded). Deduped vs step 1.
    if not sample:
        from .onchain import build_positions_supplement
        try:
            positions += build_positions_supplement(config, positions)
        except Exception as exc:  # noqa: BLE001 — never block the publish
            log.warning("Supplement step failed: %s", type(exc).__name__)

    # 2. manual adjustments
    positions += read_adjustments(config)

    # 2b. drop sub-threshold dust: the API returns some vault rows valued at ~$0
    #     (priced on fields it doesn't populate). They add noise to counts and the
    #     detailed view without changing any total.
    dust = float(config.get("vaultsfyi", {}).get("min_usd_value_threshold", 0.5))
    positions = [p for p in positions if p.get("source") == "manual" or p.get("value_usd", 0.0) >= dust]

    # 3. idle handling: idle assets are KEPT (shown + counted in total holdings)
    #    but flagged is_idle so the aggregator excludes them from APY. Setting
    #    include_idle_assets false reverts to deployed-only AUM.
    positions = aggregate.filter_positions(positions, settings.get("include_idle_assets", True))

    # 4. reference prices
    refs = resolve_reference_prices(positions, config, sample)

    # 5. roster + wallet counts
    config_clients = [c["name"] for c in config.get("clients", [])]
    roster = list(dict.fromkeys(config_clients + sorted({p["client"] for p in positions})))
    wallet_counts = compute_wallet_counts(config, positions, sample)

    # 6. aggregate (asserts invariants)
    try:
        core = aggregate.aggregate(positions, config, refs, client_names=roster, wallet_counts=wallet_counts)
    except InvariantError as exc:
        return _fail(webhook, ["Aggregation invariant failed: " + str(exc)])

    # 7. Dune: drift, reconcile, moved funds
    extra_warnings: list[str] = []
    moved_funds = None
    dcfg = config.get("dune", {})
    if dcfg.get("enabled") and not sample:
        from .dune import DuneClient
        dune = DuneClient(os.environ.get(dcfg.get("api_key_env", "DUNE_API_KEY"), ""))
        if dcfg.get("registry_drift_check"):
            extra_warnings += reconcile_mod.registry_drift(config, dune)
        if dcfg.get("reconcile"):
            rows = dune.get_onchain_balances(dcfg.get("balances_query_id"))
            extra_warnings += reconcile_mod.reconcile(core, rows, config)
        if config.get("moved_funds", {}).get("source_query_id"):
            moved_funds = dune.get_moved_funds(config["moved_funds"]["source_query_id"])

    # 8. benchmarks: pocket vs vaults.fyi benchmark across windows (USD + ETH)
    benchmarks = {}
    if config.get("benchmarks", {}).get("enabled") and not sample:
        from .benchmarks import build_benchmark_comparison
        vcfg = config.get("vaultsfyi", {})
        bclient = VaultsFyiClient(os.environ.get(vcfg.get("api_key_env", "VAULTSFYI_API_KEY"), ""),
                                  base_url=vcfg.get("base_url", "https://api.vaults.fyi/v2"),
                                  networks=vcfg.get("networks"),
                                  apy_window=config.get("settings", {}).get("apy_window", "7day"),
                                  min_usd_value_threshold=vcfg.get("min_usd_value_threshold", 0.5))
        try:
            benchmarks = build_benchmark_comparison(bclient, config)
        except Exception as exc:  # benchmarks never block the publish
            log.warning("Benchmark comparison failed: %s", type(exc).__name__)

    snapshot = assemble_snapshot(core, config, mode, moved_funds, benchmarks, now)

    # 9. validate against last published
    prev = load_previous()
    hard, soft = validate_mod.validate(snapshot, prev, config, failures, extra_warnings)
    if hard:
        return _fail(webhook, hard, soft)

    # 10. history (read existing, append today, embed series)
    history_rows = [r for r in read_history() if r.get("date") != snapshot["date"]]
    history_rows.append(build_history_row(snapshot))
    snapshot["history"] = build_history_series(history_rows, snapshot, int(settings.get("history_points", 30)))

    # 11. write
    write_outputs(snapshot)
    write_history(history_rows)
    mirror_snapshot(snapshot, config)

    if soft:
        send_alert(webhook, "kpk AUM dashboard: published with warnings", soft, severity="warning")
        for w in soft:
            log.warning("SOFT: %s", w)
    log.info("Published firm total $%s across %d clients (%s mode).",
             f"{snapshot['firm']['total_usd']:,.0f}", snapshot["firm"]["clients"], mode)
    return 0


def compute_wallet_counts(config: dict, positions: list[dict], sample: bool) -> dict[str, int]:
    """Live: count configured non-placeholder addresses (so zero-position wallets
    still count). Sample: distinct real addresses seen in the mock payloads."""
    if sample:
        counts: dict[str, set] = {}
        for p in positions:
            if p.get("address") and p["address"] != "manual":
                counts.setdefault(p["client"], set()).add(p["address"])
        return {k: len(v) for k, v in counts.items()}
    out: dict[str, int] = {}
    for c in config.get("clients", []):
        addrs = {str(a.get("address")).lower() for a in (c.get("addresses") or [])
                 if a.get("address") and not _is_placeholder(a["address"])}
        out[c["name"]] = len(addrs)
    return out


def _fail(webhook: str | None, hard: list[str], soft: list[str] | None = None) -> int:
    """Hard failure: do NOT overwrite outputs, alert, exit non-zero."""
    lines = ["HARD FAILURE - holding last good snapshot, nothing published."] + hard
    if soft:
        lines += ["(also: " + s + ")" for s in soft]
    for h in hard:
        log.error("HARD: %s", h)
    send_alert(webhook, "kpk AUM dashboard: publish BLOCKED", lines, severity="error")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="kpk treasury AUM dashboard pipeline")
    parser.add_argument("--sample", action="store_true", help="run offline with bundled mock data")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"), help="path to config.yaml")
    args = parser.parse_args()
    sys.exit(run(sample=args.sample, config_path=args.config))


if __name__ == "__main__":
    main()
