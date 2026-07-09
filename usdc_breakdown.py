"""USDC positions breakdown across all KPK-managed DAOs.

Standalone from the main AUM dashboard, but reuses its config, configurator
(addresses + API key) and vaults.fyi client. It fetches every managed wallet's
positions + idle assets, keeps only USDC-family assets, and writes:

  * data/usdc_latest.json        the snapshot (total + per-client + per-position)
  * usdc-breakdown.html          a self-contained, shareable dashboard (data inlined)

    python usdc_breakdown.py

Notes: value per position uses the same corrected field logic as the main
pipeline (lpToken.balanceUsd / positionValueInAsset), so figures reconcile with
the main dashboard. "USDC-family" = USDC plus its bridged variants (USDC.e, USDbC).
"""

from __future__ import annotations

import datetime as dt
import html as _html
import json
import os
from pathlib import Path

from src.pipeline import load_env, load_config, load_configurator, apply_configurator, _is_real_address
from src.vaultsfyi import VaultsFyiClient, extract_position, extract_idle_asset, to_network

ROOT = Path(__file__).resolve().parent
USDC_SYMBOLS = {"USDC", "USDC.E", "USDBC"}
GRAYS = ["#1f2123", "#3f4245", "#5a5d60", "#76797c", "#9a9d9f", "#bcbec0"]


def _is_usdc(symbol: str) -> bool:
    return str(symbol).strip().upper() in USDC_SYMBOLS


# --------------------------------------------------------------------------
# fetch + aggregate
# --------------------------------------------------------------------------

def build_snapshot() -> dict:
    load_env()
    config = apply_configurator(load_config(str(ROOT / "config.yaml")), load_configurator())
    vcfg = config.get("vaultsfyi", {})
    api_key = os.environ.get(vcfg.get("api_key_env", "VAULTSFYI_API_KEY"), "")
    if not api_key:
        raise SystemExit("No vaults.fyi API key found (set vaultsfyi_api_key in configurator.json).")

    client = VaultsFyiClient(
        api_key=api_key,
        base_url=vcfg.get("base_url", "https://api.vaults.fyi/v2"),
        networks=vcfg.get("networks"),
        apy_window=config.get("settings", {}).get("apy_window", "7day"),
        min_usd_value_threshold=vcfg.get("min_usd_value_threshold", 0.5),
    )

    clients_out: list[dict] = []
    grand_total = 0.0
    for c in config.get("clients", []):
        holdings: list[dict] = []
        for addr_entry in (c.get("addresses") or []):
            addr = addr_entry.get("address")
            if not addr or not _is_real_address(addr):
                continue
            networks = [to_network(x) for x in (addr_entry.get("chains") or [])] or vcfg.get("networks")
            fallback = (addr_entry.get("chains") or ["ethereum"])[0]
            try:
                for raw in client.get_positions(addr, networks=networks):
                    p = extract_position(raw, c["name"], addr, fallback)
                    if _is_usdc(p["symbol"]) and p["value_usd"] > 0:
                        holdings.append({
                            "protocol": (p.get("protocol") or p.get("name") or "—"),
                            "name": p.get("name") or "",
                            "chain": p["chain"],
                            "value_usd": round(p["value_usd"], 2),
                            "apy_pct": round(p.get("apy_pct", 0.0), 4),
                            "is_idle": False,
                        })
                for raw in client.get_idle_assets(addr, networks=networks):
                    p = extract_idle_asset(raw, c["name"], addr, fallback)
                    if _is_usdc(p["symbol"]) and p["value_usd"] > 0:
                        holdings.append({
                            "protocol": "Idle (in wallet)",
                            "name": "Uninvested USDC",
                            "chain": p["chain"],
                            "value_usd": round(p["value_usd"], 2),
                            "apy_pct": 0.0,
                            "is_idle": True,
                        })
            except Exception as exc:  # noqa: BLE001 — never abort the whole run for one wallet
                print(f"  WARN fetch failed for {c['name']} {addr[:10]}: {type(exc).__name__}")

        holdings.sort(key=lambda h: (h["is_idle"], -h["value_usd"]))
        total = round(sum(h["value_usd"] for h in holdings), 2)
        deployed = round(sum(h["value_usd"] for h in holdings if not h["is_idle"]), 2)
        idle = round(sum(h["value_usd"] for h in holdings if h["is_idle"]), 2)
        grand_total += total
        clients_out.append({
            "name": c["name"], "total_usd": total, "deployed_usd": deployed,
            "idle_usd": idle, "positions": len(holdings), "holdings": holdings,
        })

    grand_total = round(grand_total, 2)
    clients_out.sort(key=lambda c: -c["total_usd"])
    for c in clients_out:
        c["share_pct"] = round(c["total_usd"] / grand_total * 100, 2) if grand_total else 0.0

    now = dt.datetime.now(dt.timezone.utc)
    return {
        "asset": "USDC",
        "date": now.strftime("%Y-%m-%d"),
        "updated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "total_usd": grand_total,
        "deployed_usd": round(sum(c["deployed_usd"] for c in clients_out), 2),
        "idle_usd": round(sum(c["idle_usd"] for c in clients_out), 2),
        "positions": sum(c["positions"] for c in clients_out),
        "clients": clients_out,
    }


# --------------------------------------------------------------------------
# render (self-contained static HTML, neutral monochrome)
# --------------------------------------------------------------------------

def _esc(s) -> str:
    return _html.escape("" if s is None else str(s))


def _money(n) -> str:
    return "${:,.0f}".format(n)


def _pct(n) -> str:
    return "{:.2f}%".format(n)


HEAD = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>KPK - USDC Positions</title>
<style>
  :root{
    --bg:#f7f7f8;--surface:#fff;--surface-2:#f3f4f4;--raised:#e9eaeb;--hairline:#e5e6e7;--hairline-2:#eff0f1;
    --text:#1a1b1d;--muted:#6a6d70;--muted-2:#9b9ea1;--idle:#b6b8bb;
    --mono:ui-monospace,"SFMono-Regular","JetBrains Mono",Menlo,Consolas,monospace;
    --sans:"Inter",ui-sans-serif,"Helvetica Neue",Arial,sans-serif;--maxw:1040px;
    --shadow:0 1px 2px rgba(20,21,23,.05),0 8px 24px -16px rgba(20,21,23,.16);
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.5;
    font-feature-settings:"tnum" 1,"lnum" 1;-webkit-font-smoothing:antialiased;}
  .num,.mono{font-family:var(--mono);font-variant-numeric:tabular-nums lining-nums;}
  .wrap{max-width:var(--maxw);margin:0 auto;padding:30px 24px 90px;}
  .topbar{display:flex;align-items:baseline;justify-content:space-between;gap:16px;
    padding-bottom:20px;border-bottom:1px solid var(--hairline);}
  .brand{display:flex;align-items:baseline;gap:13px;}
  .logo{font-family:var(--mono);font-weight:700;font-size:22px;letter-spacing:.5px;}
  .sub{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:2.4px;}
  .meta{color:var(--muted);font-size:12px;text-align:right;}
  .hero{padding:42px 0 8px;}
  .eyebrow{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:3px;}
  .total{font-family:var(--mono);font-size:clamp(40px,7vw,72px);font-weight:600;line-height:1;margin:14px 0 6px;letter-spacing:-1.5px;}
  .total .cur{color:var(--muted-2);font-size:.44em;vertical-align:middle;margin-right:8px;}
  .rule{width:54px;height:3px;background:var(--text);margin:16px 0 22px;border-radius:2px;}
  .stats{display:flex;flex-wrap:wrap;gap:26px 46px;padding-top:22px;border-top:1px solid var(--hairline);}
  .stat .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1.5px;}
  .stat .v{font-family:var(--mono);font-size:20px;margin-top:5px;}
  .section{margin-top:50px;}
  .section>h2{font-size:12px;text-transform:uppercase;letter-spacing:2.4px;color:var(--muted);font-weight:600;
    margin:0 0 16px;padding-bottom:11px;border-bottom:1px solid var(--hairline);}
  .breakdown{background:var(--surface);border:1px solid var(--hairline);border-radius:14px;padding:22px 24px;box-shadow:var(--shadow);}
  .bar{display:flex;height:16px;border-radius:999px;overflow:hidden;background:var(--raised);margin-bottom:16px;}
  .bar i{height:100%;}
  .legend{display:flex;flex-direction:column;}
  .lrow{display:grid;grid-template-columns:14px 1fr auto auto;align-items:center;gap:13px;font-size:13.5px;
    padding:10px 0;border-bottom:1px solid var(--hairline-2);}
  .lrow:last-child{border-bottom:none;}
  .ldot{width:11px;height:11px;border-radius:3px;}
  .lval{font-family:var(--mono);} .lpct{font-family:var(--mono);color:var(--muted);min-width:52px;text-align:right;}
  .card{background:var(--surface);border:1px solid var(--hairline);border-radius:14px;padding:20px 22px;box-shadow:var(--shadow);margin-bottom:14px;}
  .ch{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:12px;}
  .nm{font-size:16px;font-weight:650;}
  .card .meta{color:var(--muted);font-size:12px;margin-top:3px;text-align:left;}
  .tot{font-family:var(--mono);font-size:21px;letter-spacing:-.4px;}
  .tbl{width:100%;border-collapse:collapse;}
  .tbl th{text-align:left;padding:9px 10px;font-size:9.5px;text-transform:uppercase;letter-spacing:1.1px;
    color:var(--muted-2);font-weight:600;border-bottom:1px solid var(--hairline);}
  .tbl th.r,.tbl td.r{text-align:right;}
  .tbl td{padding:10px;font-size:13px;border-bottom:1px solid var(--hairline-2);}
  .tbl tr:last-child td{border-bottom:none;}
  .tbl tr.idle td{background:var(--surface-2);}
  .tbl .prot{font-weight:500;text-transform:capitalize;} .tbl .chain{color:var(--muted);text-transform:capitalize;font-size:12px;}
  .tbl td.val{font-family:var(--mono);} .tbl td.apy{font-family:var(--mono);color:var(--muted);}
  .empty{color:var(--muted-2);font-style:italic;}
  .foot{margin-top:48px;padding-top:18px;border-top:1px solid var(--hairline);color:var(--muted);font-size:11.5px;max-width:760px;}
  @media(max-width:620px){.ch{flex-direction:column}}
</style></head><body><div class="wrap">"""


def render_html(snap: dict) -> str:
    clients = snap["clients"]
    total = snap["total_usd"] or 1

    segs = "".join(
        '<i style="width:{:.4f}%;background:{}" title="{}: {} ({})"></i>'.format(
            c["total_usd"] / total * 100, GRAYS[i % len(GRAYS)], _esc(c["name"]),
            _money(c["total_usd"]), _pct(c["share_pct"]))
        for i, c in enumerate(clients) if c["total_usd"] > 0
    )
    legend = "".join(
        '<div class="lrow"><span class="ldot" style="background:{}"></span>'
        '<span class="llab">{}</span><span class="lval">{}</span><span class="lpct">{}</span></div>'.format(
            GRAYS[i % len(GRAYS)], _esc(c["name"]), _money(c["total_usd"]), _pct(c["share_pct"]))
        for i, c in enumerate(clients)
    )

    cards = ""
    for c in clients:
        rows = "".join(
            '<tr class="{idle}"><td class="prot">{prot}</td><td class="chain">{chain}</td>'
            '<td class="r val">{val}</td><td class="r apy">{apy}</td></tr>'.format(
                idle="idle" if h["is_idle"] else "",
                prot=_esc(h["protocol"]), chain=_esc(h["chain"]),
                val=_money(h["value_usd"]),
                apy="idle" if h["is_idle"] else _pct(h["apy_pct"]))
            for h in c["holdings"]
        ) or '<tr><td colspan="4" class="empty">no USDC positions</td></tr>'
        idle_note = " &middot; idle " + _money(c["idle_usd"]) if c["idle_usd"] else ""
        cards += (
            '<section class="card"><div class="ch"><div><div class="nm">' + _esc(c["name"]) + '</div>'
            '<div class="meta">' + _pct(c["share_pct"]) + " of USDC &middot; " + str(c["positions"]) +
            " position" + ("" if c["positions"] == 1 else "s") + idle_note + '</div></div>'
            '<div class="tot">' + _money(c["total_usd"]) + '</div></div>'
            '<table class="tbl"><thead><tr><th>Protocol / venue</th><th>Chain</th>'
            '<th class="r">USDC value</th><th class="r">APY</th></tr></thead><tbody>' + rows + '</tbody></table></section>'
        )

    body = (
        '<div class="topbar"><div class="brand"><span class="logo">KPK</span>'
        '<span class="sub">USDC Positions across DAOs</span></div>'
        '<div class="meta">Updated <span class="num">' + _esc(snap["updated_at"]) + '</span></div></div>'
        '<section class="hero"><div class="eyebrow">Total USDC under management</div>'
        '<div class="total"><span class="cur">$</span>' + "{:,.0f}".format(snap["total_usd"]) + '</div>'
        '<div class="rule"></div><div class="stats">'
        '<div class="stat"><div class="k">DAOs / clients</div><div class="v">' + str(len(clients)) + '</div></div>'
        '<div class="stat"><div class="k">USDC positions</div><div class="v">' + str(snap["positions"]) + '</div></div>'
        '<div class="stat"><div class="k">Deployed</div><div class="v">' + _money(snap["deployed_usd"]) + '</div></div>'
        '<div class="stat"><div class="k">Idle</div><div class="v">' + _money(snap["idle_usd"]) + '</div></div>'
        '</div></section>'
        '<section class="section"><h2>Breakdown by DAO</h2><div class="breakdown">'
        '<div class="bar">' + segs + '</div><div class="legend">' + legend + '</div></div></section>'
        '<section class="section"><h2>USDC positions by DAO</h2>' + cards + '</section>'
        '<div class="foot">USDC-family holdings (USDC plus bridged USDC.e / USDbC) across all KPK-managed client wallets, '
        'from vaults.fyi. Values are per-position (deployed protocol positions plus uninvested wallet USDC, marked idle). '
        'APY is the ' + "current-window" + ' protocol-reported yield, gross of fees. Point-in-time; not investment advice.</div>'
    )
    return HEAD + body + "</div></body></html>"


def main() -> None:
    snap = build_snapshot()
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "usdc_latest.json").write_text(json.dumps(snap, indent=2), encoding="utf-8")
    (ROOT / "usdc-breakdown.html").write_text(render_html(snap), encoding="utf-8")
    print("USDC total ${:,.0f} across {} DAOs, {} positions -> usdc-breakdown.html".format(
        snap["total_usd"], len(snap["clients"]), snap["positions"]))


if __name__ == "__main__":
    main()
