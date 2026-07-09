"""Slack webhook alerting. Never logs, prints, or echoes the webhook URL."""

from __future__ import annotations

import json
import logging

import requests

log = logging.getLogger("alerts")


def send_alert(webhook_url: str | None, title: str, lines: list[str], severity: str = "warning") -> bool:
    """Post a Slack message. Returns True on success. Missing webhook -> no-op
    (logged). The URL itself is never logged."""
    if not webhook_url:
        log.info("No Slack webhook configured; alert not sent: %s", title)
        return False

    emoji = {"error": ":rotating_light:", "warning": ":warning:", "info": ":information_source:"}.get(severity, ":warning:")
    body = "\n".join(f"- {ln}" for ln in lines) if lines else "(no detail)"
    text = f"{emoji} *{title}*\n{body}"
    try:
        resp = requests.post(webhook_url, data=json.dumps({"text": text}),
                             headers={"Content-Type": "application/json"}, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        # Log the failure WITHOUT the URL.
        log.warning("Slack alert failed to send (%s)", type(exc).__name__)
        return False
