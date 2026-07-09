# KPK treasury AUM dashboard

A daily-refreshed marketing dashboard for the total onchain value managed by KPK, broken down by client and by denomination (USD, ETH, EUR), with the
TVL-weighted average yield earned in each denomination. It is built to publish a
public number unattended, with automated integrity gates instead of a daily human
review.

Two reporting levels are first class: per-client statistics and the firm-level
aggregate. Every figure rolls up from positions to wallet to client to firm and
reconciles exactly at every level.

## How it works

```
vaults.fyi API  ─┐
                 ├─> pipeline (Python) ──> data/latest.json   (source of truth)
Dune API (wallet ┘                    ├──> data/history.csv   (per-day history)
registry +                            └──> dashboard/data.js  (for the static page)
reconciliation +
moved-funds KPI)     dashboard/index.html ──reads──> data.js / latest.json
```

- **Source of truth** is the pipeline output. All aggregation happens in Python.
- **Marketing face** is a branded static page that reads the snapshot. No server,
  no build step, embeddable, works over `file://`.
- **Integrity** is enforced by validation gates in the pipeline, not by a human.

### Data sources

- **vaults.fyi V2** (primary): yield and covered balances. Verified endpoints:
  - `GET /v2/portfolio/positions/{address}` active vault positions
  - `GET /v2/portfolio/idle-assets/{address}` uninvested wallet tokens
  - `GET /v2/benchmarks/{network}?code=usd|eth` network benchmark APYs (optional)
  - Auth header `x-api-key`. APY values are raw decimals (displayed times 100).
  - Note: the portfolio API defaults to `[base, mainnet, arbitrum, optimism]` and
    excludes Gnosis, so the pipeline always sends an explicit `allowedNetworks`
    that includes `gnosis`.
- **Dune** (secondary): the wallet registry of record (drift check), onchain
  balances for reconciliation, and the complementary "funds moved" KPI. Dune is
  off the hard-failure path; if it is unavailable the daily publish still runs.

### Metric distinction (never conflated)

- **AUM** is a stock: the current USD value managed right now. This is the primary
  number, computed from vaults.fyi positions plus the Dune reconciliation.
- **Funds moved** is a flow: cumulative deployment volume over time. It is shown,
  when enabled, as a separate clearly labeled stat and is never summed with AUM.

## Setup

Requirements: Python 3.11+ (CI uses 3.12).

```bash
pip install -r requirements.txt
cp configurator.example.json configurator.json   # paste API key + client addresses
```

### The configurator (`configurator.json`)

`configurator.json` is the single place to paste the things that change per
deployment: the **vaults.fyi API key** and **every managed client's wallet
addresses**. It is gitignored (it holds a secret); commit only
`configurator.example.json`. At runtime its `clients` list **overrides** the
roster in `config.yaml`, so the dashboard shows exactly the entities listed here.

```jsonc
{
  "vaultsfyi_api_key": "vfyi_...",      // also dune_api_key, slack_webhook_url
  "clients": [
    { "name": "CoW DAO", "dune_source_query_id": 7401664,
      "addresses": [                     // a client may hold MANY addresses,
        { "address": "0xabc...", "chains": ["ethereum", "gnosis"] },
        { "address": "0xdef...", "chains": ["ethereum"] }
      ] }
    // Balancer DAO, ENS, Nexus Mutual ...
  ]
}
```

- Add as many `addresses` per client as you need; one address may span several
  chains (it still counts as a single wallet). Placeholder/non-`0x` addresses are
  ignored, so a client with no real address yet simply shows zero.
- API keys here populate the env vars below **only if they are not already set**,
  so a real environment variable or CI secret always wins.

Secrets can alternatively be read from the environment or a gitignored `.env`
(`cp .env.example .env`):

- `VAULTSFYI_API_KEY` (required for live runs)
- `DUNE_API_KEY` (registry drift check, reconciliation, moved-funds KPI)
- `SLACK_WEBHOOK_URL` (validation alerts; optional but recommended)

Keys are never printed, logged, or committed. In CI, set them as repository
secrets rather than using a file.

## Run

```bash
# Offline, no key, bundled multi-wallet mock data. Produces data/latest.json,
# dashboard/data.js and a rendering dashboard with a "sample data" badge.
python -m src.pipeline --sample

# Live run (needs VAULTSFYI_API_KEY).
python -m src.pipeline

# Tests (no pytest needed): invariants + every validation gate.
python -m tests.test_pipeline
```

### View the dashboard

Open `dashboard/index.html` directly (it loads `data.js` over `file://`), or serve
the folder:

```bash
python -m http.server 4317 --directory dashboard
# then open http://localhost:4317
```

## Configuration

Two files, by how often they change:

- **`configurator.json`** (per-deployment, gitignored) — API keys and the managed
  client roster with their addresses. See [The configurator](#the-configurator-configuratorjson)
  above. This is where you add/remove clients and paste addresses.
- **`config.yaml`** (tuning, committed) — everything below. Its `clients[]` list
  is the fallback roster used when no configurator is present, and currently
  mirrors the four managed clients (CoW DAO, Balancer DAO, ENS, Nexus Mutual).

### `config.yaml`

- `clients[]`: each client carries its `dune_source_query_id` and an `addresses`
  list (the offline fallback; `configurator.json` overrides it). A startup drift
  check warns when the roster differs from the Dune registry of record. A client
  may hold several addresses across several chains; one address used on multiple
  chains is a single wallet entry.
- `settings`: `apy_window` (1day/7day/30day), `include_idle_assets`
  (false = deployed AUM, true = total holdings), `allow_coingecko`,
  `history_points`, and the validation thresholds (`variance_alert_pct`,
  `max_unclassified_pct`, APY sanity band).
- `denominations`: symbol to bucket map (USD / ETH / EUR), case-insensitive and
  editable. Add a new asset symbol here when the unclassified warning fires.
- `dune`: drift check, reconciliation, and `balances_query_id` (a current-balances
  query keyed on the shared registry; reconciliation is skipped gracefully if
  unset).
- `moved_funds`, `benchmarks`, `google_sheets`: optional, off by default.

## Validation gates (the integrity layer)

The pipeline computes the new snapshot, then validates it against the last
published snapshot before overwriting anything.

**Hard failures** (hold last good snapshot, send Slack alert, exit non-zero,
commit nothing):

- Any configured wallet failed to return data (never publish a partial total).
- Firm total moved more than `variance_alert_pct` versus the last published total.
- Empty or zero firm total when the previous snapshot was non-zero.
- A reconciliation invariant failed.

**Soft warnings** (publish, but alert):

- Unclassified value exceeds `max_unclassified_pct` of the firm total.
- A bucket weighted APY falls outside the sanity band.
- An ETH or EUR reference price is missing while that bucket is non-empty.
- Manual adjustments exceed 25% of the firm total.
- A Dune reconciliation gap exceeds `reconcile_gap_alert_pct`.
- The config registry differs from the Dune registry (drift).

The first live run has no previous snapshot, so the comparison gates are skipped
with a log note.

Invariants asserted on every run: `firm.total == sum(client totals)`; per
denomination `firm.bucket == sum(client buckets)`; `sum(share_pct) == 100`; per
client `total == sum(wallet totals)`; and the firm weighted APY is recomputed
across all positions, never averaged from client APYs.

## Manual adjustments (optional, to close the coverage gap)

vaults.fyi does not see OTC holdings, raw LP positions, or locked tokens. Add them
via `data/adjustments.csv` (see `data/adjustments.example.csv`) or the Google
Sheets adjustments worksheet when `google_sheets.enabled` is true. Each adjustment
merges into the matching client and denomination, is tagged `manual`, and is
reported as `manual_adjustments_usd` at firm level.

## Output contract (`data/latest.json`)

Stable JSON consumed by the dashboard: `firm` (total, counts, per-denomination
buckets with `value_usd`, `avg_apy_pct`, `native_value`, `native_unit`,
`positions`; `unclassified`; `manual_adjustments_usd`; `reconciliation`),
`clients[]` (total, `share_pct`, per-denomination buckets, `wallet_count`,
`wallets[]`, `reconciliation`), `history` (firm / by-denom / by-client series for
sparklines), `moved_funds`, and `benchmarks`. The same object is written to
`dashboard/data.js` as `window.__AUM_DATA__` and one row per day is appended to
`data/history.csv`.

## Daily refresh (CI)

`.github/workflows/refresh.yml` runs daily at 06:00 UTC (plus manual dispatch):
checkout, set up Python 3.12, install requirements, run the pipeline live with the
three secrets, and on success commit `data/latest.json`, `data/history.csv`, and
`dashboard/data.js`. On a hard validation failure the run exits non-zero, nothing
is committed, the last good snapshot stays live, and the Slack alert has already
fired.

## Deploy

Host the `dashboard/` folder on any static host. For GitHub Pages, point Pages at
the `dashboard/` directory (or copy it to `/docs`); the daily workflow keeps
`data.js` fresh in place.

## Go-live checklist

1. Set `VAULTSFYI_API_KEY`, `DUNE_API_KEY`, `SLACK_WEBHOOK_URL` as repo secrets.
2. Run `python -m src.pipeline` once locally with a real key and confirm the firm
   total and per-client breakdown look right.
3. Confirm the Dune drift check is quiet (config addresses match the registry).
   Populate each client's `addresses` from its `dune_source_query_id` query, then
   re-run; resolve any drift warnings.
4. If reconciliation is wanted, set `dune.balances_query_id` to a current-balances
   query keyed on the shared registry and confirm the firm coverage gap is small.
5. Verify a Slack alert arrives on a forced failure (for example, point a client
   at a bad address and run once).
6. Enable the GitHub Actions workflow and run it via manual dispatch once.
7. Publish the `dashboard/` folder and confirm the page renders the committed
   snapshot.

## Notes on schema coupling

All vaults.fyi field parsing is isolated in the extractor functions at the bottom
of `src/vaultsfyi.py` (`extract_position`, `extract_idle_asset`,
`extract_benchmark_total`). If a field is renamed upstream, fix it there and
nowhere else. The Dune result parsers in `src/dune.py` sniff likely column names
defensively because saved-query column names vary. Confirm the live Dune column
names against your `aggregator_query_id` / `balances_query_id` on first live run.
