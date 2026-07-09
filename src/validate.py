"""Validation gates: the integrity layer. Runs before publishing.

On any HARD failure the pipeline must NOT overwrite latest.json / data.js: it
keeps the last good snapshot live, alerts, and exits non-zero. SOFT warnings are
published but alerted on.

The first live run has no previous snapshot; comparison gates are skipped (the
pipeline passes prev=None) and only structural checks apply.
"""

from __future__ import annotations

from .aggregate import BUCKET_ORDER, assert_invariants, InvariantError


def validate(
    snapshot: dict,
    prev: dict | None,
    config: dict,
    fetch_failures: list[str] | None = None,
    extra_warnings: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return (hard_failures, soft_warnings)."""
    settings = config.get("settings", {})
    firm = snapshot["firm"]
    hard: list[str] = []
    soft: list[str] = list(extra_warnings or [])
    fetch_failures = fetch_failures or []

    # ---- HARD ------------------------------------------------------------

    # 1. Any configured wallet failed to return data.
    if fetch_failures:
        hard.append(
            "Wallet data fetch failed for: " + ", ".join(fetch_failures)
            + ". Refusing to publish a partial firm total."
        )

    # 4. Reconcile invariants (defensive re-check; aggregate also asserts).
    try:
        assert_invariants(snapshot)
    except InvariantError as exc:
        hard.append(f"Reconciliation invariant failed: {exc}")

    total = firm["total_usd"]
    prev_total = prev["firm"]["total_usd"] if prev else None

    if prev_total is not None:
        # 3. Empty/zero now but previously non-zero.
        if total <= 0 and prev_total > 0:
            hard.append(f"Firm total is {total} but previous snapshot was ${prev_total:,.0f}.")
        # 2. Day-over-day variance beyond the guard band.
        elif prev_total > 0:
            move_pct = abs(total - prev_total) / prev_total * 100.0
            band = float(settings.get("variance_alert_pct", 15))
            if move_pct > band:
                hard.append(
                    f"Firm total moved {move_pct:.1f}% (${prev_total:,.0f} -> ${total:,.0f}), "
                    f"exceeding the {band}% guard band."
                )
    else:
        soft.append("First live run: no previous snapshot, comparison gates skipped.")

    # ---- SOFT ------------------------------------------------------------

    # Unclassified share.
    if total > 0:
        unc_pct = firm["unclassified"]["value_usd"] / total * 100.0
        max_unc = float(settings.get("max_unclassified_pct", 2))
        if unc_pct > max_unc:
            soft.append(
                f"Unclassified value is {unc_pct:.1f}% of firm total (> {max_unc}%); "
                f"a new asset symbol is likely missing from the denomination map."
            )

    # APY sanity band per bucket.
    lo = float(settings.get("apy_sane_min_pct", -0.5))
    hi = float(settings.get("apy_sane_max_pct", 60))
    for b in BUCKET_ORDER:
        bk = firm["buckets"][b]
        if bk["value_usd"] > 0 and not (lo <= bk["avg_apy_pct"] <= hi):
            soft.append(f"{b} weighted APY {bk['avg_apy_pct']:.2f}% is outside the sane band [{lo}, {hi}].")

    # Missing reference price for a non-empty native bucket.
    refs = snapshot.get("reference_prices", {})
    if firm["buckets"]["ETH"]["value_usd"] > 0 and not refs.get("eth_usd"):
        soft.append("ETH reference price missing; ETH native-unit value unavailable.")
    if firm["buckets"]["EUR"]["value_usd"] > 0 and not refs.get("eur_usd"):
        soft.append("EUR reference price missing; EUR native-unit value unavailable.")

    # Manual adjustments share.
    if total > 0:
        max_manual = float(config.get("adjustments", {}).get("max_share_pct", 25))
        manual_pct = firm.get("manual_adjustments_usd", 0) / total * 100.0
        if manual_pct > max_manual:
            soft.append(f"Manual adjustments are {manual_pct:.1f}% of firm total (> {max_manual}%).")

    return hard, soft
