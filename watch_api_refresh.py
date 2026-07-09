"""One-shot recovery watcher: poll vaults.fyi until it responds, then run the
pipeline and rebuild the shareable file. Exits after success or ~6 hours.

    python watch_api_refresh.py     (or launched detached via Start-Process)

Log: logs/watch.log
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "logs" / "watch.log"
LOG.parent.mkdir(exist_ok=True)


def log(msg: str) -> None:
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {msg}\n")


def api_up() -> bool:
    try:
        r = requests.get("https://api.vaults.fyi/v2/benchmarks/mainnet",
                         params={"code": "usd"}, timeout=15)
        return r.status_code == 200
    except requests.RequestException:
        return False


def main() -> None:
    log("watcher started")
    deadline = time.time() + 6 * 3600
    while time.time() < deadline:
        if api_up():
            log("API recovered — running pipeline")
            r1 = subprocess.run([sys.executable, "-m", "src.pipeline"], cwd=ROOT,
                                capture_output=True, text=True)
            log(f"pipeline exit {r1.returncode}: {(r1.stdout + r1.stderr).strip()[-300:]}")
            if r1.returncode == 0:
                r2 = subprocess.run([sys.executable, "build_standalone.py"], cwd=ROOT,
                                    capture_output=True, text=True)
                log(f"standalone exit {r2.returncode}")
                log("DONE — dashboard refreshed with per-client window data")
                return
            # pipeline failed even though benchmark ping worked; wait and retry
        time.sleep(300)
    log("gave up after 6h (API still down)")


if __name__ == "__main__":
    main()
