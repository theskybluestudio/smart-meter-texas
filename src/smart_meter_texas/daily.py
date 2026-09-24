from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import requests

BASE_HOST = "https://www.smartmetertexas.com"
BASE_API_URL = f"{BASE_HOST}/api"
AUTH_ENDPOINT = f"{BASE_HOST}/commonapi/user/authenticate"
METER_ENDPOINT = f"{BASE_API_URL}/meter"
DAILY_USAGE_ENDPOINT = f"{BASE_API_URL}/usage/daily"
DEFAULT_TIMEZONE = "America/Chicago"
ENV_FILENAME = ".env"
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Content-Type": "application/json",
    "Origin": BASE_HOST,
    "Pragma": "no-cache",
    "Referer": f"{BASE_HOST}/home",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}
CSV_COLUMNS = [
    "ESIID",
    "USAGE_DATE",
    "USAGE_START_TIME",
    "USAGE_END_TIME",
    "START_READING_KWH",
    "END_READING_KWH",
    "USAGE_KWH",
    "UNIT_NAME",
    "TDSP_DUNS_NO",
    "STATUS_MESSAGE",
]
DEDUPLICATE_KEY = ["ESIID", "USAGE_DATE"]


class SmartMeterTexasError(RuntimeError):
    pass


@dataclass
class SyncResult:
    esiid: str
    start_date: str
    end_date: str
    fetched_rows: int
    inserted_rows: int
    csv_path: Path
    db_path: Path
    state_path: Path


class SmartMeterTexasClient:
    def __init__(self, username: str, password: str, timeout: int = 30) -> None:
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.token: str | None = None

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(url, json=payload, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("error"):
            raise SmartMeterTexasError(str(data["error"]))
        if isinstance(data, dict) and data.get("errormessage"):
            raise SmartMeterTexasError(str(data["errormessage"]))
        return data

    def authenticate(self) -> None:
        payload = {"username": self.username, "password": self.password, "rememberMe": "true"}
        data = self._post(AUTH_ENDPOINT, payload)
        token = data.get("token")
        if not token:
            raise SmartMeterTexasError("Authentication succeeded but no token was returned.")
        self.token = str(token)
        self.session.headers["Authorization"] = f"Bearer {self.token}"

    def list_meters(self) -> list[dict[str, Any]]:
        data = self._post(METER_ENDPOINT, {"esiid": "*"})
        meters = data.get("data", data)
        if not isinstance(meters, list):
            raise SmartMeterTexasError("Unexpected meter response shape.")
        return meters

    def get_daily_usage(self, esiid: str, start_date: str, end_date: str) -> dict[str, Any]:
        payload = {"esiid": esiid, "startDate": start_date, "endDate": end_date}
        return self._post(DAILY_USAGE_ENDPOINT, payload)


def load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync Smart Meter Texas historical usage into local CSV and SQLite files."
    )
    parser.add_argument("--username", default=os.getenv("SMT_USERNAME"))
    parser.add_argument("--password", default=os.getenv("SMT_PASSWORD"))
    parser.add_argument("--esiid", default=os.getenv("SMT_ESIID"))
    parser.add_argument("--start-date", help="Start date in YYYY-MM-DD. Defaults from state/history.")
    parser.add_argument("--end-date", help="End date in YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--bootstrap-days", type=int, default=30)
    parser.add_argument("--discover-meters", action="store_true")
    parser.add_argument(
        "--csv-path",
        default="data/smt_daily_usage_history.csv",
        help="Append-only CSV output path, relative to the project folder.",
    )
    parser.add_argument(
        "--db-path",
        default="data/smt_daily_usage_history.sqlite",
        help="SQLite output path, relative to the project folder.",
    )
    parser.add_argument(
        "--state-path",
        default="data/smt_sync_state.json",
        help="State file path, relative to the project folder.",
    )
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--timeout", type=int, default=30)
    return parser.parse_args()


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def today_in_timezone(timezone_name: str) -> date:
    return datetime.now(ZoneInfo(timezone_name)).date()


def coerce_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def to_api_date(value: date) -> str:
    return value.strftime("%m/%d/%Y")


def canonical_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def flatten_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        flat: list[dict[str, Any]] = []
        for item in payload:
            flat.extend(flatten_records(item))
        return flat

    if not isinstance(payload, dict):
        return []

    nested_records: list[dict[str, Any]] = []
    for value in payload.values():
        if isinstance(value, list):
            nested_records.extend(flatten_records(value))

    scalar_values = {
        key: value
        for key, value in payload.items()
        if not isinstance(value, (list, dict))
    }

    if nested_records:
        return [merge_dicts(scalar_values, record) for record in nested_records]
    return [scalar_values] if scalar_values else []


def merge_dicts(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    merged = dict(parent)
    for key, value in child.items():
        if key not in merged or merged[key] in (None, ""):
            merged[key] = value
    return merged


def first_value(record: dict[str, Any], *keys: str) -> Any:
    lookup = {canonical_key(key): value for key, value in record.items()}
    for key in keys:
        value = lookup.get(canonical_key(key))
        if value not in (None, ""):
            return value
    return None


def normalize_usage_payload(payload: dict[str, Any], fallback_esiid: str) -> pd.DataFrame:
    root = payload.get("dailyData") or payload.get("data") or payload
    records = flatten_records(root)
    normalized_rows: list[dict[str, Any]] = []

    for record in records:
        usage_kwh = first_value(record, "usage_kwh", "usagekwh", "kwh", "consumptionkwh", "reading")
        usage_date = first_value(record, "usage_date", "usagedate", "date", "start_date")
        if usage_kwh in (None, "") or usage_date in (None, ""):
            continue

        normalized_rows.append(
            {
                "ESIID": normalize_esiid(first_value(record, "esiid", "esid", "esi_id") or fallback_esiid),
                "USAGE_DATE": normalize_date_text(usage_date),
                "USAGE_START_TIME": normalize_time_text(
                    first_value(record, "usage_start_time", "usagestarttime", "starttime", "start_at")
                ),
                "USAGE_END_TIME": normalize_time_text(
                    first_value(record, "usage_end_time", "usageendtime", "endtime", "end_at")
                ),
                "START_READING_KWH": normalize_decimal_text(
                    first_value(record, "startreading", "start_reading", "startingreading")
                ),
                "END_READING_KWH": normalize_decimal_text(
                    first_value(record, "endreading", "end_reading", "endingreading")
                ),
                "USAGE_KWH": normalize_decimal_text(usage_kwh),
                "UNIT_NAME": str(first_value(record, "unitname", "unit_name") or "KWH").strip(),
                "TDSP_DUNS_NO": str(first_value(record, "tdspdunsno", "tdsp_duns_no") or "").strip(),
                "STATUS_MESSAGE": str(first_value(record, "errmsg", "message", "status") or "").strip(),
            }
        )

    dataframe = pd.DataFrame(normalized_rows, columns=CSV_COLUMNS)
    if dataframe.empty:
        return dataframe

    dataframe = dataframe.drop_duplicates(subset=DEDUPLICATE_KEY, keep="last").sort_values(
        by=DEDUPLICATE_KEY
    )
    return dataframe.reset_index(drop=True)


def normalize_esiid(value: Any) -> str:
    text = str(value).strip()
    return text if text.startswith("'") else f"'{text}"


def normalize_date_text(value: Any) -> str:
    text = str(value).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    try:
        return pd.to_datetime(text).strftime("%m/%d/%Y")
    except Exception as exc:  # pragma: no cover - defensive fallback
        raise SmartMeterTexasError(f"Unable to parse usage date: {value}") from exc


def normalize_time_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.strftime("%H:%M")
        except ValueError:
            continue
    try:
        return pd.to_datetime(text).strftime("%H:%M")
    except Exception:
        return text


def normalize_decimal_text(value: Any) -> str:
    return f"{float(value):.3f}".rstrip("0").rstrip(".") if value not in (None, "") else ""


def ensure_sqlite_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_usage (
            ESIID TEXT NOT NULL,
            USAGE_DATE TEXT NOT NULL,
            USAGE_START_TIME TEXT,
            USAGE_END_TIME TEXT,
            START_READING_KWH REAL,
            END_READING_KWH REAL,
            USAGE_KWH REAL,
            UNIT_NAME TEXT,
            TDSP_DUNS_NO TEXT,
            STATUS_MESSAGE TEXT,
            PRIMARY KEY (ESIID, USAGE_DATE)
        )
        """
    )
    connection.commit()


def append_to_csv(path: Path, rows: pd.DataFrame) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = pd.read_csv(path, dtype=str) if path.exists() else pd.DataFrame(columns=CSV_COLUMNS)
    merged = pd.concat([existing, rows.astype(str)], ignore_index=True)
    merged = merged.drop_duplicates(subset=DEDUPLICATE_KEY, keep="last").sort_values(by=DEDUPLICATE_KEY)
    inserted_rows = len(merged) - len(existing.drop_duplicates(subset=DEDUPLICATE_KEY, keep="last"))
    merged.to_csv(path, index=False, quoting=csv.QUOTE_MINIMAL)
    return max(inserted_rows, 0)


def upsert_to_sqlite(path: Path, rows: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        ensure_sqlite_table(connection)
        connection.executemany(
            """
            INSERT INTO daily_usage (
                ESIID, USAGE_DATE, USAGE_START_TIME,
                USAGE_END_TIME, START_READING_KWH, END_READING_KWH,
                USAGE_KWH, UNIT_NAME, TDSP_DUNS_NO, STATUS_MESSAGE
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ESIID, USAGE_DATE)
            DO UPDATE SET
                USAGE_START_TIME=excluded.USAGE_START_TIME,
                USAGE_END_TIME=excluded.USAGE_END_TIME,
                START_READING_KWH=excluded.START_READING_KWH,
                END_READING_KWH=excluded.END_READING_KWH,
                USAGE_KWH=excluded.USAGE_KWH,
                UNIT_NAME=excluded.UNIT_NAME,
                TDSP_DUNS_NO=excluded.TDSP_DUNS_NO,
                STATUS_MESSAGE=excluded.STATUS_MESSAGE
            """,
            [tuple(row[column] for column in CSV_COLUMNS) for _, row in rows.iterrows()],
        )
        connection.commit()


def determine_window(
    *,
    csv_path: Path,
    state_path: Path,
    timezone_name: str,
    bootstrap_days: int,
    explicit_start_date: str | None,
    explicit_end_date: str | None,
) -> tuple[date, date]:
    if explicit_start_date:
        start = coerce_date(explicit_start_date)
    else:
        latest_date = find_latest_date(csv_path)
        if latest_date is None:
            state = load_state(state_path)
            latest_saved = state.get("last_usage_date")
            latest_date = coerce_date(latest_saved) if latest_saved else None
        start = latest_date + timedelta(days=1) if latest_date else today_in_timezone(timezone_name) - timedelta(days=bootstrap_days)

    end = coerce_date(explicit_end_date) if explicit_end_date else today_in_timezone(timezone_name)
    if end < start:
        raise SmartMeterTexasError("End date cannot be earlier than start date.")
    return start, end


def find_latest_date(csv_path: Path) -> date | None:
    if not csv_path.exists():
        return None
    dataframe = pd.read_csv(csv_path, usecols=["USAGE_DATE"], dtype=str)
    if dataframe.empty:
        return None
    parsed = pd.to_datetime(dataframe["USAGE_DATE"], format="%m/%d/%Y", errors="coerce").dropna()
    if parsed.empty:
        return None
    return parsed.max().date()


def default_project_root() -> Path:
    configured = os.environ.get("SMT_PROJECT_ROOT")
    return Path(configured).resolve() if configured else Path.cwd().resolve()


def choose_esiid(client: SmartMeterTexasClient, requested_esiid: str | None) -> str:
    meters = client.list_meters()
    if requested_esiid:
        normalized = normalize_esiid(requested_esiid)
        for meter in meters:
            meter_esiid = normalize_esiid(meter.get("esiid") or meter.get("ESIID") or "")
            if meter_esiid == normalized:
                return normalized
        raise SmartMeterTexasError(f"ESIID {requested_esiid} was not found on this account.")

    if not meters:
        raise SmartMeterTexasError("No meters were returned by Smart Meter Texas.")
    if len(meters) > 1:
        choices = ", ".join(str(meter.get("esiid")) for meter in meters)
        raise SmartMeterTexasError(f"Multiple meters found. Re-run with --esiid. Choices: {choices}")
    return normalize_esiid(meters[0].get("esiid") or meters[0].get("ESIID") or "")


def sync_usage(args: argparse.Namespace) -> SyncResult:
    if not args.username or not args.password:
        raise SmartMeterTexasError("Set SMT_USERNAME and SMT_PASSWORD or pass --username/--password.")

    project_root = default_project_root()
    csv_path = project_root / args.csv_path
    db_path = project_root / args.db_path
    state_path = project_root / args.state_path

    client = SmartMeterTexasClient(args.username, args.password, timeout=args.timeout)
    client.authenticate()

    if args.discover_meters:
        meters = client.list_meters()
        print(json.dumps(meters, indent=2))
        raise SystemExit(0)

    esiid = choose_esiid(client, args.esiid)
    start, end = determine_window(
        csv_path=csv_path,
        state_path=state_path,
        timezone_name=args.timezone,
        bootstrap_days=args.bootstrap_days,
        explicit_start_date=args.start_date,
        explicit_end_date=args.end_date,
    )

    payload = client.get_daily_usage(esiid.lstrip("'"), to_api_date(start), to_api_date(end))
    rows = normalize_usage_payload(payload, fallback_esiid=esiid)

    if rows.empty:
        raise SmartMeterTexasError("No usage rows were returned for the requested window.")

    inserted_rows = append_to_csv(csv_path, rows)
    upsert_to_sqlite(db_path, rows)

    latest_usage_date = pd.to_datetime(rows["USAGE_DATE"], format="%m/%d/%Y").max().date().isoformat()
    save_state(
        state_path,
        {
            "esiid": esiid,
            "last_run_at": datetime.now(tz=ZoneInfo(args.timezone)).isoformat(),
            "last_usage_date": latest_usage_date,
            "last_requested_start_date": start.isoformat(),
            "last_requested_end_date": end.isoformat(),
            "last_fetched_rows": int(len(rows)),
        },
    )

    return SyncResult(
        esiid=esiid,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        fetched_rows=int(len(rows)),
        inserted_rows=int(inserted_rows),
        csv_path=csv_path,
        db_path=db_path,
        state_path=state_path,
    )


def main() -> None:
    load_dotenv_file(default_project_root() / ENV_FILENAME)
    args = parse_args()
    result = sync_usage(args)
    print(json.dumps(result.__dict__, indent=2, default=str))


if __name__ == "__main__":
    main()
