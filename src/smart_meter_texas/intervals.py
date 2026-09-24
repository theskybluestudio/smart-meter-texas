from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time as time_module
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .daily import (
    AUTH_ENDPOINT,
    DEFAULT_HEADERS,
    ENV_FILENAME,
    METER_ENDPOINT,
    SmartMeterTexasError,
    choose_esiid,
    coerce_date,
    default_project_root,
    load_dotenv_file,
    load_state,
    save_state,
    today_in_timezone,
    to_api_date,
)

INTERVAL_ENDPOINT = "https://www.smartmetertexas.com/api/adhoc/intervalsynch"
DEFAULT_TIMEZONE = "America/Chicago"
DEFAULT_OVERLAP_DAYS = 7
EXPECTED_POPULATED_INTERVALS = 96
MISSING_INTERVAL_STATUS = "MISSING"
PRESENT_INTERVAL_STATUS = "PRESENT"
CSV_COLUMNS = [
    "ESIID",
    "USAGE_DATE",
    "REVISION_DATE",
    "INTERVAL_INDEX",
    "INTERVAL_START_TS",
    "USAGE_START_TIME",
    "USAGE_END_TIME",
    "USAGE_KWH",
    "ESTIMATED_ACTUAL",
    "INTERVAL_STATUS",
    "CONSUMPTION_SURPLUSGENERATION",
]
DEDUPLICATE_KEY = ["ESIID", "USAGE_DATE", "INTERVAL_INDEX"]
READ_TYPE_MAP = {
    "C": "Consumption",
    "G": "Surplus Generation",
}


@dataclass
class AuditIssue:
    usage_date: str
    total_intervals: int
    populated_intervals: int
    missing_intervals: int
    duplicate_keys: int
    missing_slots: list[str]


@dataclass
class AuditSummary:
    total_days: int
    complete_days: int
    incomplete_days: int
    duplicate_keys: int
    incomplete_dates: list[str]
    issues: list[AuditIssue]
    revised_rows: int
    csv_row_count: int
    sqlite_row_count: int


@dataclass
class WriteSummary:
    inserted_rows: int
    updated_rows: int
    revised_rows: int
    merged_rows: pd.DataFrame


@dataclass
class IntervalSyncResult:
    esiid: str
    start_date: str
    end_date: str
    fetched_rows: int
    inserted_rows: int
    updated_rows: int
    revised_rows: int
    complete_days: int
    incomplete_days: int
    incomplete_dates: list[str]
    csv_path: Path
    db_path: Path
    state_path: Path


class SmartMeterTexasIntervalClient:
    def __init__(self, username: str, password: str, timeout: int = 60, max_retries: int = 3) -> None:
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.token: str | None = None
        self.max_retries = max_retries

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.post(url, json=payload, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and data.get("error"):
                    raise SmartMeterTexasError(str(data["error"]))
                if isinstance(data, dict) and data.get("errormessage"):
                    raise SmartMeterTexasError(str(data["errormessage"]))
                return data
            except requests.HTTPError as exc:
                last_error = exc
                status_code = exc.response.status_code if exc.response is not None else None
                if attempt < self.max_retries and status_code in {429, 500, 502, 503, 504}:
                    time_module.sleep(2 * attempt)
                    continue
                raise
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time_module.sleep(2 * attempt)
                    continue
                raise
        assert last_error is not None
        raise last_error

    def authenticate(self) -> None:
        data = self._post(
            AUTH_ENDPOINT,
            {"username": self.username, "password": self.password, "rememberMe": "true"},
        )
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

    def get_interval_usage(self, esiid: str, start_date: str, end_date: str) -> dict[str, Any]:
        return self._post(
            INTERVAL_ENDPOINT,
            {
                "startDate": start_date,
                "endDate": end_date,
                "reportFormat": "JSON",
                "ESIID": [esiid],
                "versionDate": None,
                "readDate": None,
                "versionNum": None,
                "dataType": None,
            },
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync Smart Meter Texas 15-minute interval usage into local CSV and SQLite files."
    )
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--esiid")
    parser.add_argument("--start-date", help="Start date in YYYY-MM-DD. Defaults from state/history.")
    parser.add_argument("--end-date", help="End date in YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--bootstrap-days", type=int, default=7)
    parser.add_argument(
        "--overlap-days",
        type=int,
        default=DEFAULT_OVERLAP_DAYS,
        help="When running incrementally, re-fetch at least this many already-synced days to catch revisions.",
    )
    parser.add_argument("--discover-meters", action="store_true")
    parser.add_argument(
        "--csv-path",
        default="data/smt_interval_usage_history.csv",
        help="CSV output path, relative to the project folder.",
    )
    parser.add_argument(
        "--db-path",
        default="data/smt_interval_usage_history.sqlite",
        help="SQLite output path, relative to the project folder.",
    )
    parser.add_argument(
        "--state-path",
        default="data/smt_interval_sync_state.json",
        help="State file path, relative to the project folder.",
    )
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument(
        "--raw-payload-dir",
        default="logs/raw-payloads",
        help="Directory for saving fetched raw interval payloads, relative to the project folder.",
    )
    return parser.parse_args()


def normalize_esiid(value: str) -> str:
    text = str(value).strip()
    return text if text.startswith("'") else f"'{text}"


def interval_label(index: int) -> tuple[str, str]:
    start_minutes = index * 15
    start_time = (datetime.combine(date.today(), time(0, 0)) + timedelta(minutes=start_minutes)).time()
    end_time = (datetime.combine(date.today(), start_time) + timedelta(minutes=15)).time()
    return start_time.strftime("%H:%M"), end_time.strftime("%H:%M")


def daily_interval_slots(usage_date: str, timezone_name: str = DEFAULT_TIMEZONE) -> list[dict[str, str]]:
    usage_day = datetime.strptime(usage_date, "%m/%d/%Y").date()
    timezone = ZoneInfo(timezone_name)
    utc = ZoneInfo("UTC")
    start_local = datetime.combine(usage_day, time(0, 0), tzinfo=timezone)
    end_local = start_local + timedelta(days=1)
    current_utc = start_local.astimezone(utc)
    end_utc = end_local.astimezone(utc)

    slots: list[dict[str, str]] = []
    index = 0
    while current_utc < end_utc:
        start_dt = current_utc.astimezone(timezone)
        end_dt = (current_utc + timedelta(minutes=15)).astimezone(timezone)
        slots.append(
            {
                "INTERVAL_INDEX": str(index),
                "INTERVAL_START_TS": start_dt.isoformat(timespec="seconds"),
                "USAGE_START_TIME": start_dt.strftime("%H:%M"),
                "USAGE_END_TIME": end_dt.strftime("%H:%M"),
            }
        )
        current_utc += timedelta(minutes=15)
        index += 1
    return slots


def build_interval_slot(start_dt: datetime, index: int) -> dict[str, str]:
    end_dt = start_dt + timedelta(minutes=15)
    return {
        "INTERVAL_INDEX": str(index),
        "INTERVAL_START_TS": start_dt.isoformat(timespec="seconds"),
        "USAGE_START_TIME": start_dt.strftime("%H:%M"),
        "USAGE_END_TIME": end_dt.strftime("%H:%M"),
    }


def extend_interval_slots(slots: list[dict[str, str]], target_count: int) -> list[dict[str, str]]:
    if len(slots) >= target_count:
        return slots
    extended = list(slots)
    current = datetime.fromisoformat(extended[-1]["INTERVAL_START_TS"])
    while len(extended) < target_count:
        current = current + timedelta(minutes=15)
        extended.append(build_interval_slot(current, len(extended)))
    return extended


def interval_start_timestamp(usage_date: str, index: int) -> str:
    slots = daily_interval_slots(usage_date)
    if index >= len(slots):
        slots = extend_interval_slots(slots, index + 1)
    return slots[index]["INTERVAL_START_TS"]


def allowed_missing_indexes_for_date(usage_date: str, timezone_name: str = DEFAULT_TIMEZONE) -> set[int]:
    usage_day = datetime.strptime(usage_date, "%m/%d/%Y").date()
    timezone = ZoneInfo(timezone_name)
    start_offset = datetime.combine(usage_day, time(0, 0), tzinfo=timezone).utcoffset()
    next_offset = datetime.combine(usage_day + timedelta(days=1), time(0, 0), tzinfo=timezone).utcoffset()
    if start_offset is not None and next_offset is not None and next_offset > start_offset:
        return {8, 9, 10, 11}
    return set()


def interval_index_from_start_time(start_time: str) -> int:
    parsed = datetime.strptime(start_time, "%H:%M")
    return parsed.hour * 4 + (parsed.minute // 15)


def normalize_revision_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return text
    return parsed.strftime("%m/%d/%Y %H:%M:%S")


def parse_interval_token(token: str, *, usage_date: str, index: int) -> tuple[str, str, str]:
    cleaned = token.strip()
    if not cleaned:
        return "", "", MISSING_INTERVAL_STATUS
    if "-" in cleaned:
        kwh_text, estimated_actual = cleaned.rsplit("-", 1)
    else:
        kwh_text, estimated_actual = cleaned, ""
    try:
        usage_kwh = f"{float(kwh_text):.3f}".rstrip("0").rstrip(".")
    except ValueError as exc:
        raise SmartMeterTexasError(
            f"Unexpected interval token {cleaned!r} for {usage_date} interval {index}."
        ) from exc
    return usage_kwh, estimated_actual.strip(), PRESENT_INTERVAL_STATUS


def capture_raw_payload(payload: dict[str, Any], destination: Path, *, start: date, end: date) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = destination / f"interval-payload_{start.isoformat()}_{end.isoformat()}_{stamp}.json"
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return target


def normalize_interval_payload(payload: dict[str, Any], fallback_esiid: str) -> pd.DataFrame:
    root = payload.get("data", {})
    energy_data = root.get("energyData", [])
    esiid = normalize_esiid(str(root.get("esiid") or fallback_esiid).strip())
    rows: list[dict[str, str]] = []

    for day_record in energy_data:
        usage_date = str(day_record.get("DT") or "").strip()
        if not usage_date:
            continue
        revision_date = normalize_revision_date(day_record.get("RevTS", ""))
        read_type = str(day_record.get("RT", "")).strip().upper()
        flow = READ_TYPE_MAP.get(read_type, read_type or "Unknown")
        raw_values = str(day_record.get("RD", ""))
        intervals = raw_values.split(",") if raw_values else []

        slots = daily_interval_slots(usage_date)
        if len(intervals) > len(slots):
            slots = extend_interval_slots(slots, len(intervals))

        for idx, slot in enumerate(slots):
            token = intervals[idx] if idx < len(intervals) else ""
            usage_kwh, estimated_actual, interval_status = parse_interval_token(
                token,
                usage_date=usage_date,
                index=idx,
            )
            rows.append(
                {
                    "ESIID": esiid,
                    "USAGE_DATE": usage_date,
                    "REVISION_DATE": revision_date,
                    "INTERVAL_INDEX": slot["INTERVAL_INDEX"],
                    "INTERVAL_START_TS": slot["INTERVAL_START_TS"],
                    "USAGE_START_TIME": slot["USAGE_START_TIME"],
                    "USAGE_END_TIME": slot["USAGE_END_TIME"],
                    "USAGE_KWH": usage_kwh,
                    "ESTIMATED_ACTUAL": estimated_actual,
                    "INTERVAL_STATUS": interval_status,
                    "CONSUMPTION_SURPLUSGENERATION": flow,
                }
            )

    dataframe = pd.DataFrame(rows, columns=CSV_COLUMNS)
    if dataframe.empty:
        return dataframe
    dataframe["USAGE_DATE"] = pd.to_datetime(
        dataframe["USAGE_DATE"], format="%m/%d/%Y", errors="coerce"
    ).dt.strftime("%m/%d/%Y")
    dataframe = dataframe.drop_duplicates(subset=DEDUPLICATE_KEY, keep="last")
    dataframe["INTERVAL_INDEX"] = dataframe["INTERVAL_INDEX"].astype(int)
    dataframe = dataframe.sort_values(by=["ESIID", "USAGE_DATE", "INTERVAL_INDEX"])
    dataframe["INTERVAL_INDEX"] = dataframe["INTERVAL_INDEX"].astype(str)
    return dataframe.reset_index(drop=True)


def assign_legacy_interval_indexes(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe
    normalized = dataframe.copy()
    normalized["__legacy_order"] = range(len(normalized))
    normalized["__interval_index_text"] = normalized["INTERVAL_INDEX"].astype(str).str.strip()
    needs_backfill = normalized["__interval_index_text"] == ""
    if needs_backfill.any():
        sortable = normalized.loc[needs_backfill].copy()
        sortable["__parsed_time"] = pd.to_datetime(sortable["USAGE_START_TIME"], format="%H:%M", errors="coerce")
        sortable = sortable.sort_values(by=["ESIID", "USAGE_DATE", "__parsed_time", "__legacy_order"])
        sortable["__sequence_index"] = sortable.groupby(["ESIID", "USAGE_DATE"]).cumcount().astype(str)
        normalized.loc[sortable.index, "INTERVAL_INDEX"] = sortable["__sequence_index"]
    normalized = normalized.drop(columns=["__legacy_order", "__interval_index_text"], errors="ignore")
    return normalized


def load_existing_rows(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=CSV_COLUMNS)
    existing = pd.read_csv(path, dtype=str).fillna("")
    for column in CSV_COLUMNS:
        if column not in existing.columns:
            existing[column] = ""
    existing = assign_legacy_interval_indexes(existing)
    if "INTERVAL_START_TS" not in existing.columns or existing["INTERVAL_START_TS"].eq("").all():
        existing["INTERVAL_START_TS"] = existing.apply(
            lambda row: interval_start_timestamp(row["USAGE_DATE"], int(row["INTERVAL_INDEX"])),
            axis=1,
        )
    if "INTERVAL_STATUS" not in existing.columns or existing["INTERVAL_STATUS"].eq("").all():
        existing["INTERVAL_STATUS"] = existing["USAGE_KWH"].apply(
            lambda value: PRESENT_INTERVAL_STATUS if str(value).strip() else MISSING_INTERVAL_STATUS
        )
    return existing[CSV_COLUMNS]


def ensure_sqlite_table(connection: sqlite3.Connection) -> None:
    desired_columns = [
        "ESIID",
        "USAGE_DATE",
        "REVISION_DATE",
        "INTERVAL_INDEX",
        "INTERVAL_START_TS",
        "USAGE_START_TIME",
        "USAGE_END_TIME",
        "USAGE_KWH",
        "ESTIMATED_ACTUAL",
        "INTERVAL_STATUS",
        "CONSUMPTION_SURPLUSGENERATION",
    ]
    table_exists = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='interval_usage'"
    ).fetchone()
    existing_columns = []
    if table_exists:
        existing_columns = [row[1] for row in connection.execute("PRAGMA table_info(interval_usage)").fetchall()]

    if existing_columns == desired_columns:
        return

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS interval_usage__new (
            ESIID TEXT NOT NULL,
            USAGE_DATE TEXT NOT NULL,
            REVISION_DATE TEXT,
            INTERVAL_INDEX INTEGER NOT NULL,
            INTERVAL_START_TS TEXT NOT NULL,
            USAGE_START_TIME TEXT NOT NULL,
            USAGE_END_TIME TEXT NOT NULL,
            USAGE_KWH REAL,
            ESTIMATED_ACTUAL TEXT,
            INTERVAL_STATUS TEXT NOT NULL,
            CONSUMPTION_SURPLUSGENERATION TEXT,
            PRIMARY KEY (ESIID, USAGE_DATE, INTERVAL_INDEX)
        )
        """
    )

    if existing_columns:
        legacy = pd.read_sql_query("SELECT * FROM interval_usage", connection).fillna("")
        for column in desired_columns:
            if column not in legacy.columns:
                legacy[column] = ""
        legacy = assign_legacy_interval_indexes(legacy)
        legacy["INTERVAL_START_TS"] = legacy.apply(
            lambda row: row["INTERVAL_START_TS"]
            if str(row["INTERVAL_START_TS"]).strip()
            else interval_start_timestamp(str(row["USAGE_DATE"]), int(row["INTERVAL_INDEX"])),
            axis=1,
        )
        legacy["INTERVAL_STATUS"] = legacy.apply(
            lambda row: row["INTERVAL_STATUS"]
            if str(row["INTERVAL_STATUS"]).strip()
            else (PRESENT_INTERVAL_STATUS if str(row["USAGE_KWH"]).strip() else MISSING_INTERVAL_STATUS),
            axis=1,
        )
        legacy = legacy[desired_columns]
        legacy = legacy.drop_duplicates(subset=DEDUPLICATE_KEY, keep="last")
        connection.executemany(
            """
            INSERT OR REPLACE INTO interval_usage__new (
                ESIID, USAGE_DATE, REVISION_DATE, INTERVAL_INDEX, INTERVAL_START_TS,
                USAGE_START_TIME, USAGE_END_TIME, USAGE_KWH, ESTIMATED_ACTUAL,
                INTERVAL_STATUS, CONSUMPTION_SURPLUSGENERATION
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [tuple(row[column] for column in desired_columns) for _, row in legacy.iterrows()],
        )
        connection.execute("DROP TABLE interval_usage")

    connection.execute("ALTER TABLE interval_usage__new RENAME TO interval_usage")
    connection.commit()


def merge_rows(existing: pd.DataFrame, rows: pd.DataFrame) -> tuple[pd.DataFrame, int, int, int]:
    existing = existing.fillna("")
    rows = rows.fillna("")
    existing_unique = existing.drop_duplicates(subset=DEDUPLICATE_KEY, keep="last") if not existing.empty else existing
    if existing_unique.empty:
        merged = rows.copy()
        return merged, len(rows), 0, 0

    existing_indexed = existing_unique.set_index(DEDUPLICATE_KEY)
    incoming_indexed = rows.set_index(DEDUPLICATE_KEY)
    shared_keys = existing_indexed.index.intersection(incoming_indexed.index)

    updated_rows = 0
    revised_rows = 0
    compare_columns = [column for column in CSV_COLUMNS if column not in DEDUPLICATE_KEY]
    for key in shared_keys:
        current = existing_indexed.loc[key]
        incoming = incoming_indexed.loc[key]
        if isinstance(current, pd.DataFrame):
            current = current.iloc[-1]
        if isinstance(incoming, pd.DataFrame):
            incoming = incoming.iloc[-1]
        if any(str(current[column]) != str(incoming[column]) for column in compare_columns):
            updated_rows += 1
            if str(current["REVISION_DATE"]) != str(incoming["REVISION_DATE"]):
                revised_rows += 1

    inserted_rows = len(incoming_indexed.index.difference(existing_indexed.index))
    merged = pd.concat([existing_unique, rows], ignore_index=True)
    merged = merged.drop_duplicates(subset=DEDUPLICATE_KEY, keep="last")
    merged["INTERVAL_INDEX"] = merged["INTERVAL_INDEX"].astype(int)
    merged = merged.sort_values(by=["ESIID", "USAGE_DATE", "INTERVAL_INDEX"])
    merged["INTERVAL_INDEX"] = merged["INTERVAL_INDEX"].astype(str)
    return merged, int(inserted_rows), int(updated_rows), int(revised_rows)


def append_to_csv(path: Path, rows: pd.DataFrame) -> WriteSummary:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_existing_rows(path)
    merged, inserted_rows, updated_rows, revised_rows = merge_rows(existing, rows.astype(str))
    merged.to_csv(path, index=False, quoting=csv.QUOTE_MINIMAL)
    return WriteSummary(
        inserted_rows=inserted_rows,
        updated_rows=updated_rows,
        revised_rows=revised_rows,
        merged_rows=merged,
    )


def upsert_to_sqlite(path: Path, rows: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        ensure_sqlite_table(connection)
        connection.executemany(
            """
            INSERT INTO interval_usage (
                ESIID, USAGE_DATE, REVISION_DATE, INTERVAL_INDEX, INTERVAL_START_TS,
                USAGE_START_TIME, USAGE_END_TIME, USAGE_KWH, ESTIMATED_ACTUAL,
                INTERVAL_STATUS, CONSUMPTION_SURPLUSGENERATION
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ESIID, USAGE_DATE, INTERVAL_INDEX)
            DO UPDATE SET
                REVISION_DATE=excluded.REVISION_DATE,
                INTERVAL_START_TS=excluded.INTERVAL_START_TS,
                USAGE_START_TIME=excluded.USAGE_START_TIME,
                USAGE_END_TIME=excluded.USAGE_END_TIME,
                USAGE_KWH=excluded.USAGE_KWH,
                ESTIMATED_ACTUAL=excluded.ESTIMATED_ACTUAL,
                INTERVAL_STATUS=excluded.INTERVAL_STATUS,
                CONSUMPTION_SURPLUSGENERATION=excluded.CONSUMPTION_SURPLUSGENERATION
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
    overlap_days: int,
    explicit_start_date: str | None,
    explicit_end_date: str | None,
) -> tuple[date, date]:
    state = load_state(state_path)
    if explicit_start_date:
        start = coerce_date(explicit_start_date)
    else:
        incomplete_dates = sorted(coerce_date(value) for value in state.get("incomplete_dates", []) if value)
        last_complete_date = state.get("last_complete_date")
        latest_csv_date = find_latest_date(csv_path)
        complete_date = coerce_date(last_complete_date) if last_complete_date else latest_csv_date
        safe_overlap = max(overlap_days, DEFAULT_OVERLAP_DAYS, 1)
        overlap_start = complete_date - timedelta(days=safe_overlap - 1) if complete_date else None

        candidates = [candidate for candidate in [*incomplete_dates, overlap_start] if candidate]
        if candidates:
            start = min(candidates)
        else:
            start = today_in_timezone(timezone_name) - timedelta(days=bootstrap_days)

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


def has_expected_populated_intervals(total_intervals: int, populated_intervals: int) -> bool:
    return total_intervals >= EXPECTED_POPULATED_INTERVALS and populated_intervals == EXPECTED_POPULATED_INTERVALS


def audit_rows(rows: pd.DataFrame) -> AuditSummary:
    normalized = rows.fillna("").copy()
    duplicates = int(normalized.duplicated(subset=DEDUPLICATE_KEY, keep=False).sum())
    issues: list[AuditIssue] = []
    complete_days = 0

    for usage_date, group in normalized.groupby("USAGE_DATE", sort=True):
        interval_indexes = pd.to_numeric(group["INTERVAL_INDEX"], errors="coerce")
        valid_indexes = interval_indexes.dropna().astype(int)
        unique_indexes = set(valid_indexes.tolist())
        populated = group[
            (group["USAGE_KWH"].astype(str).str.strip() != "")
            & (group["INTERVAL_STATUS"] != MISSING_INTERVAL_STATUS)
        ]
        expected_slots = daily_interval_slots(usage_date)
        expected_indexes = set(range(len(expected_slots)))
        allowed_missing_indexes = allowed_missing_indexes_for_date(usage_date)
        missing_indexes = sorted((expected_indexes - unique_indexes) - allowed_missing_indexes)
        missing_status_indexes = sorted(
            pd.to_numeric(
                group.loc[group["INTERVAL_STATUS"] == MISSING_INTERVAL_STATUS, "INTERVAL_INDEX"],
                errors="coerce",
            )
            .dropna()
            .astype(int)
            .tolist()
        )
        all_missing_indexes = sorted((set(missing_indexes) | set(missing_status_indexes)) - allowed_missing_indexes)
        issue = AuditIssue(
            usage_date=usage_date,
            total_intervals=len(unique_indexes),
            populated_intervals=int(len(populated)),
            missing_intervals=len(all_missing_indexes),
            duplicate_keys=int(group.duplicated(subset=DEDUPLICATE_KEY, keep=False).sum()),
            missing_slots=[expected_slots[index]["USAGE_START_TIME"] for index in all_missing_indexes if index < len(expected_slots)],
        )
        looks_complete = (
            issue.missing_intervals == 0 and issue.total_intervals == len(expected_slots)
        ) or (
            issue.duplicate_keys == 0
            and has_expected_populated_intervals(issue.total_intervals, issue.populated_intervals)
        )
        if looks_complete:
            complete_days += 1
        else:
            issues.append(issue)

    issues.sort(key=lambda issue: datetime.strptime(issue.usage_date, "%m/%d/%Y"))
    return AuditSummary(
        total_days=normalized["USAGE_DATE"].nunique(),
        complete_days=complete_days,
        incomplete_days=len(issues),
        duplicate_keys=duplicates,
        incomplete_dates=[datetime.strptime(issue.usage_date, "%m/%d/%Y").date().isoformat() for issue in issues],
        issues=issues,
        revised_rows=0,
        csv_row_count=len(normalized),
        sqlite_row_count=0,
    )


def audit_sqlite(path: Path, start: date, end: date, revised_rows: int) -> AuditSummary:
    start_text = start.strftime("%m/%d/%Y")
    end_text = end.strftime("%m/%d/%Y")
    with sqlite3.connect(path) as connection:
        ensure_sqlite_table(connection)
        rows = pd.read_sql_query(
            """
            SELECT *
            FROM interval_usage
            WHERE date(substr(USAGE_DATE, 7, 4) || '-' || substr(USAGE_DATE, 1, 2) || '-' || substr(USAGE_DATE, 4, 2))
                  BETWEEN date(?) AND date(?)
            ORDER BY ESIID, USAGE_DATE, INTERVAL_INDEX
            """,
            connection,
            params=(start.isoformat(), end.isoformat()),
        ).fillna("")
        summary = audit_rows(rows if not rows.empty else pd.DataFrame(columns=CSV_COLUMNS))
        summary.revised_rows = revised_rows
        summary.sqlite_row_count = len(rows)
        return summary


def summarize_audit_failure(summary: AuditSummary) -> str:
    parts = []
    if summary.duplicate_keys:
        parts.append(f"duplicate interval keys={summary.duplicate_keys}")
    if summary.incomplete_dates:
        preview = ", ".join(summary.incomplete_dates[:5])
        parts.append(f"incomplete dates={preview}")
    return "; ".join(parts) or "interval audit failed"


def infer_last_complete_date(start: date, end: date, incomplete_dates: list[str]) -> str | None:
    if not incomplete_dates:
        return end.isoformat()
    earliest_incomplete = min(coerce_date(value) for value in incomplete_dates)
    candidate = earliest_incomplete - timedelta(days=1)
    if candidate < start:
        return None
    return candidate.isoformat()


def sync_usage(args: argparse.Namespace) -> IntervalSyncResult:
    project_root = default_project_root()
    load_dotenv_file(project_root / ENV_FILENAME)
    username = args.username or __import__("os").environ.get("SMT_USERNAME")
    password = args.password or __import__("os").environ.get("SMT_PASSWORD")
    requested_esiid = args.esiid or __import__("os").environ.get("SMT_ESIID")
    if not username or not password:
        raise SmartMeterTexasError("Set SMT_USERNAME and SMT_PASSWORD or pass --username/--password.")

    csv_path = project_root / args.csv_path
    db_path = project_root / args.db_path
    state_path = project_root / args.state_path
    raw_payload_dir = project_root / args.raw_payload_dir
    previous_state = load_state(state_path)

    client = SmartMeterTexasIntervalClient(username, password, timeout=args.timeout)
    client.authenticate()

    if args.discover_meters:
        print(json.dumps(client.list_meters(), indent=2))
        raise SystemExit(0)

    esiid = choose_esiid(client, requested_esiid)
    start, end = determine_window(
        csv_path=csv_path,
        state_path=state_path,
        timezone_name=args.timezone,
        bootstrap_days=args.bootstrap_days,
        overlap_days=args.overlap_days,
        explicit_start_date=args.start_date,
        explicit_end_date=args.end_date,
    )

    payload = client.get_interval_usage(esiid.lstrip("'"), to_api_date(start), to_api_date(end))
    raw_payload_path = capture_raw_payload(payload, raw_payload_dir, start=start, end=end)
    rows = normalize_interval_payload(payload, fallback_esiid=esiid)
    if rows.empty:
        raise SmartMeterTexasError("No interval rows were returned for the requested window.")

    write_summary = append_to_csv(csv_path, rows)
    upsert_to_sqlite(db_path, rows)
    audit = audit_sqlite(db_path, start, end, revised_rows=write_summary.revised_rows)
    audit.csv_row_count = len(write_summary.merged_rows)

    rows_usage_dates = pd.to_datetime(rows["USAGE_DATE"], format="%m/%d/%Y")
    last_returned_usage_date = rows_usage_dates.max().date().isoformat()
    run_timestamp = datetime.now(tz=ZoneInfo(args.timezone)).isoformat()
    if audit.duplicate_keys or audit.incomplete_days:
        inferred_last_complete = infer_last_complete_date(start, end, audit.incomplete_dates)
        previous_complete = previous_state.get("last_complete_date")
        effective_last_complete = inferred_last_complete or previous_complete
        if inferred_last_complete and previous_complete:
            effective_last_complete = max(inferred_last_complete, previous_complete)
        save_state(
            state_path,
            {
                "esiid": esiid,
                "last_run_at": run_timestamp,
                "last_successful_run_at": previous_state.get("last_successful_run_at"),
                "last_requested_start_date": start.isoformat(),
                "last_requested_end_date": end.isoformat(),
                "last_fetched_rows": int(len(rows)),
                "last_usage_date": last_returned_usage_date,
                "last_complete_date": effective_last_complete,
                "incomplete_dates": audit.incomplete_dates,
                "audit": asdict(audit),
                "last_raw_payload_path": str(raw_payload_path),
            },
        )
        raise SmartMeterTexasError(
            "Interval data audit failed after write: " + summarize_audit_failure(audit)
        )

    state_payload = {
        "esiid": esiid,
        "last_run_at": run_timestamp,
        "last_successful_run_at": run_timestamp,
        "last_requested_start_date": start.isoformat(),
        "last_requested_end_date": end.isoformat(),
        "last_fetched_rows": int(len(rows)),
        "last_usage_date": last_returned_usage_date,
        "last_complete_date": last_returned_usage_date,
        "incomplete_dates": audit.incomplete_dates,
        "audit": asdict(audit),
        "last_raw_payload_path": str(raw_payload_path),
    }
    save_state(state_path, state_payload)

    return IntervalSyncResult(
        esiid=esiid,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        fetched_rows=int(len(rows)),
        inserted_rows=write_summary.inserted_rows,
        updated_rows=write_summary.updated_rows,
        revised_rows=write_summary.revised_rows,
        complete_days=audit.complete_days,
        incomplete_days=audit.incomplete_days,
        incomplete_dates=audit.incomplete_dates,
        csv_path=csv_path,
        db_path=db_path,
        state_path=state_path,
    )


def main() -> None:
    args = parse_args()
    result = sync_usage(args)
    print(json.dumps(asdict(result), indent=2, default=str))


if __name__ == "__main__":
    main()
