from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Smart Meter Texas interval refresh on a daily schedule.")
    parser.add_argument("--time", default=os.environ.get("SMT_REFRESH_TIME", "11:00"), help="Daily run time HH:MM.")
    parser.add_argument("--timezone", default=os.environ.get("SMT_TIMEZONE", "America/Chicago"))
    parser.add_argument("--overlap-days", type=int, default=int(os.environ.get("SMT_OVERLAP_DAYS", "7")))
    parser.add_argument("--run-on-start", action="store_true", default=os.environ.get("SMT_RUN_ON_START", "0") in {"1", "true", "TRUE", "yes"})
    parser.add_argument("--once", action="store_true", help="Run once and exit.")
    return parser.parse_args()


def next_run(now: datetime, hhmm: str) -> datetime:
    hour_text, minute_text = hhmm.split(":", 1)
    candidate = now.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def run_refresh(overlap_days: int) -> int:
    command = [
        sys.executable,
        "-m",
        "smart_meter_texas.intervals",
        "--overlap-days",
        str(overlap_days),
        "--db-path",
        "data/smt_interval_usage_history.sqlite",
        "--state-path",
        "data/smt_interval_sync_state.json",
        "--raw-payload-dir",
        "logs/raw-payloads",
    ]
    print(json.dumps({"event": "refresh_start", "command": command, "ts": datetime.now().isoformat()}), flush=True)
    result = subprocess.run(command)
    print(json.dumps({"event": "refresh_end", "returncode": result.returncode, "ts": datetime.now().isoformat()}), flush=True)
    return result.returncode


def main() -> None:
    args = parse_args()
    timezone = ZoneInfo(args.timezone)

    if args.once or args.run_on_start:
        code = run_refresh(args.overlap_days)
        if args.once:
            raise SystemExit(code)

    while True:
        now = datetime.now(timezone)
        target = next_run(now, args.time)
        sleep_seconds = max(1, int((target - now).total_seconds()))
        print(json.dumps({"event": "sleep", "until": target.isoformat(), "seconds": sleep_seconds}), flush=True)
        time.sleep(sleep_seconds)
        run_refresh(args.overlap_days)


if __name__ == "__main__":
    main()
