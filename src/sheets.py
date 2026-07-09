"""Optional Google Sheets mirror + manual adjustments reader (off critical path).

Manual adjustments add positions the API cannot see (OTC, raw LPs, locked /
governance tokens). Two sources, controlled by config:
  - local data/adjustments.csv          (default; used when google_sheets disabled)
  - the Sheets "Adjustments" worksheet  (when google_sheets.enabled is true)

Sheets is ONLY a mirror and an optional input. No aggregation or transformation
logic lives here. gspread/google-auth are imported lazily so the pipeline runs
without them installed.

Adjustment row schema:
  client, label, denomination, value_usd, native_value(opt), native_unit(opt),
  apy_pct(opt), note
"""

from __future__ import annotations

import csv
import logging
import os

log = logging.getLogger("sheets")


def _to_float(v, default=0.0):
    if v in (None, ""):
        return default
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return default


def _row_to_position(row: dict) -> dict | None:
    client = (row.get("client") or "").strip()
    bucket = (row.get("denomination") or "").strip().upper()
    if not client or bucket not in ("USD", "ETH", "EUR"):
        log.warning("Skipping adjustment with missing client/denomination: %r", row)
        return None
    label = (row.get("label") or "manual").strip()
    return {
        "client": client,
        "address": "manual",
        "chain": "manual",
        "network": "manual",
        "symbol": label,
        "name": (row.get("note") or label).strip(),
        "value_usd": _to_float(row.get("value_usd")),
        "native_balance": _to_float(row.get("native_value")),
        "asset_price_usd": 0.0,
        "apy_pct": _to_float(row.get("apy_pct")),
        "unclaimed_usd": 0.0,
        "is_idle": False,
        "source": "manual",
        "bucket": bucket,  # explicit denomination override
    }


def read_adjustments(config: dict) -> list[dict]:
    """Return manual adjustments as normalized position records."""
    gs = config.get("google_sheets", {})
    if gs.get("enabled"):
        rows = _read_sheets_adjustments(config)
    else:
        rows = _read_csv_adjustments(config)
    positions = [p for p in (_row_to_position(r) for r in rows) if p]
    if positions:
        log.info("Loaded %d manual adjustment(s).", len(positions))
    return positions


def _read_csv_adjustments(config: dict) -> list[dict]:
    path = config.get("adjustments", {}).get("csv_path", "data/adjustments.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _read_sheets_adjustments(config: dict) -> list[dict]:
    try:
        import gspread  # noqa: F401
        from google.oauth2.service_account import Credentials  # noqa: F401
    except ImportError:
        log.warning("google_sheets.enabled but gspread/google-auth not installed; skipping adjustments.")
        return []
    try:
        client = _gspread_client()
        sh = client.open_by_key(config["google_sheets"]["spreadsheet_id"])
        ws = sh.worksheet(config["google_sheets"].get("adjustments_worksheet", "Adjustments"))
        return ws.get_all_records()
    except Exception as exc:  # never let the optional path break the run
        log.warning("Failed to read Sheets adjustments: %s", exc)
        return []


def mirror_snapshot(snapshot: dict, config: dict) -> None:
    """Append a flat QA row to the mirror worksheet. Off the critical path:
    failures are logged and swallowed."""
    gs = config.get("google_sheets", {})
    if not gs.get("enabled"):
        return
    try:
        import gspread  # noqa: F401
    except ImportError:
        log.warning("google_sheets.enabled but gspread not installed; skipping mirror.")
        return
    try:
        client = _gspread_client()
        sh = client.open_by_key(gs["spreadsheet_id"])
        try:
            ws = sh.worksheet(gs.get("mirror_worksheet", "AUM_log"))
        except Exception:
            ws = sh.add_worksheet(title=gs.get("mirror_worksheet", "AUM_log"), rows=1000, cols=12)
            ws.append_row(["date", "firm_total_usd", "USD", "ETH", "EUR", "clients", "wallets", "positions"])
        firm = snapshot["firm"]
        ws.append_row([
            snapshot["date"], firm["total_usd"],
            firm["buckets"]["USD"]["value_usd"], firm["buckets"]["ETH"]["value_usd"],
            firm["buckets"]["EUR"]["value_usd"], firm["clients"], firm["wallets"], firm["positions"],
        ])
    except Exception as exc:
        log.warning("Failed to mirror snapshot to Sheets: %s", exc)


def _gspread_client():
    import gspread
    from google.oauth2.service_account import Credentials
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    return gspread.authorize(creds)
