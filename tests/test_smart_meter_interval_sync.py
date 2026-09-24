from __future__ import annotations

from pathlib import Path

import pandas as pd

from smart_meter_texas.intervals import (
    MISSING_INTERVAL_STATUS,
    PRESENT_INTERVAL_STATUS,
    prepare_sqlite_write,
    audit_rows,
    capture_raw_payload,
    daily_interval_slots,
    interval_label,
    normalize_interval_payload,
)


def test_interval_label_maps_quarters() -> None:
    assert interval_label(0) == ("00:00", "00:15")
    assert interval_label(1) == ("00:15", "00:30")
    assert interval_label(95) == ("23:45", "00:00")


def test_normalize_interval_payload_preserves_missing_intervals() -> None:
    payload = {
        "data": {
            "esiid": "TEST_ESIID_0001",
            "energyData": [
                {
                    "DT": "07/10/2026",
                    "RevTS": "07/10/2026 10:45:27",
                    "RT": "C",
                    "RD": "0.404-A,0.686-A,,0.466-E",
                }
            ],
        }
    }

    rows = normalize_interval_payload(payload, fallback_esiid="'TEST_ESIID_0001")

    assert len(rows) == 96
    assert rows.iloc[0].to_dict() == {
        "ESIID": "'TEST_ESIID_0001",
        "USAGE_DATE": "07/10/2026",
        "REVISION_DATE": "07/10/2026 10:45:27",
        "INTERVAL_INDEX": "0",
        "INTERVAL_START_TS": "2026-07-10T00:00:00-05:00",
        "USAGE_START_TIME": "00:00",
        "USAGE_END_TIME": "00:15",
        "USAGE_KWH": "0.404",
        "ESTIMATED_ACTUAL": "A",
        "INTERVAL_STATUS": PRESENT_INTERVAL_STATUS,
        "CONSUMPTION_SURPLUSGENERATION": "Consumption",
    }
    assert rows.iloc[2]["INTERVAL_STATUS"] == MISSING_INTERVAL_STATUS
    assert rows.iloc[2]["USAGE_KWH"] == ""
    assert rows.iloc[3]["USAGE_START_TIME"] == "00:45"
    assert rows.iloc[3]["ESTIMATED_ACTUAL"] == "E"


def test_prepare_sqlite_write_deduplicates_interval_rows_by_interval_index(tmp_path: Path) -> None:
    db_path = tmp_path / "intervals.sqlite"
    initial = pd.DataFrame(
        [
            {
                "ESIID": "'100",
                "USAGE_DATE": "07/10/2026",
                "REVISION_DATE": "07/10/2026 10:45:27",
                "INTERVAL_INDEX": "0",
                "INTERVAL_START_TS": "2026-07-10T00:00:00-05:00",
                "USAGE_START_TIME": "00:00",
                "USAGE_END_TIME": "00:15",
                "USAGE_KWH": "0.404",
                "ESTIMATED_ACTUAL": "A",
                "INTERVAL_STATUS": PRESENT_INTERVAL_STATUS,
                "CONSUMPTION_SURPLUSGENERATION": "Consumption",
            }
        ]
    )
    from smart_meter_texas.intervals import upsert_to_sqlite

    upsert_to_sqlite(db_path, initial)

    new_rows = pd.DataFrame(
        [
            {
                "ESIID": "'100",
                "USAGE_DATE": "07/10/2026",
                "REVISION_DATE": "07/10/2026 11:00:00",
                "INTERVAL_INDEX": "0",
                "INTERVAL_START_TS": "2026-07-10T00:00:00-05:00",
                "USAGE_START_TIME": "00:00",
                "USAGE_END_TIME": "00:15",
                "USAGE_KWH": "0.405",
                "ESTIMATED_ACTUAL": "A",
                "INTERVAL_STATUS": PRESENT_INTERVAL_STATUS,
                "CONSUMPTION_SURPLUSGENERATION": "Consumption",
            },
            {
                "ESIID": "'100",
                "USAGE_DATE": "07/10/2026",
                "REVISION_DATE": "07/10/2026 11:00:00",
                "INTERVAL_INDEX": "1",
                "INTERVAL_START_TS": "2026-07-10T00:15:00-05:00",
                "USAGE_START_TIME": "00:15",
                "USAGE_END_TIME": "00:30",
                "USAGE_KWH": "0.686",
                "ESTIMATED_ACTUAL": "A",
                "INTERVAL_STATUS": PRESENT_INTERVAL_STATUS,
                "CONSUMPTION_SURPLUSGENERATION": "Consumption",
            },
        ]
    )

    summary = prepare_sqlite_write(db_path, new_rows)

    assert summary.inserted_rows == 1
    assert summary.updated_rows == 1
    assert summary.revised_rows == 1
    assert len(summary.merged_rows) == 2
    assert summary.merged_rows.iloc[0]["USAGE_KWH"] == "0.405"


def test_audit_rows_marks_missing_intervals_incomplete() -> None:
    rows = pd.DataFrame(
        [
            {
                "ESIID": "'100",
                "USAGE_DATE": "07/10/2026",
                "REVISION_DATE": "07/10/2026 10:45:27",
                "INTERVAL_INDEX": str(index),
                "INTERVAL_START_TS": f"2026-07-10T{interval_label(index)[0]}:00-05:00",
                "USAGE_START_TIME": interval_label(index)[0],
                "USAGE_END_TIME": interval_label(index)[1],
                "USAGE_KWH": "0.5" if index != 8 else "",
                "ESTIMATED_ACTUAL": "A" if index != 8 else "",
                "INTERVAL_STATUS": PRESENT_INTERVAL_STATUS if index != 8 else MISSING_INTERVAL_STATUS,
                "CONSUMPTION_SURPLUSGENERATION": "Consumption",
            }
            for index in range(96)
        ]
    )

    summary = audit_rows(rows)

    assert summary.total_days == 1
    assert summary.complete_days == 0
    assert summary.incomplete_days == 1
    assert summary.incomplete_dates == ["2026-07-10"]
    assert summary.issues[0].missing_slots == ["02:00"]


def test_audit_rows_allows_spring_forward_gap() -> None:
    slots = daily_interval_slots("03/08/2026")
    rows = pd.DataFrame(
        [
            {
                "ESIID": "'100",
                "USAGE_DATE": "03/08/2026",
                "REVISION_DATE": "03/08/2026 10:45:27",
                "INTERVAL_INDEX": slot["INTERVAL_INDEX"],
                "INTERVAL_START_TS": slot["INTERVAL_START_TS"],
                "USAGE_START_TIME": slot["USAGE_START_TIME"],
                "USAGE_END_TIME": slot["USAGE_END_TIME"],
                "USAGE_KWH": "0.5",
                "ESTIMATED_ACTUAL": "A",
                "INTERVAL_STATUS": PRESENT_INTERVAL_STATUS,
                "CONSUMPTION_SURPLUSGENERATION": "Consumption",
            }
            for slot in slots
        ]
    )

    summary = audit_rows(rows)

    assert len(slots) == 92
    assert summary.complete_days == 1
    assert summary.incomplete_days == 0


def test_daily_interval_slots_handles_fall_back_day() -> None:
    slots = daily_interval_slots("11/02/2025")

    assert len(slots) == 100
    assert slots[4]["USAGE_START_TIME"] == "01:00"
    assert slots[8]["USAGE_START_TIME"] == "01:00"


def test_normalize_interval_payload_allows_more_than_96_tokens() -> None:
    payload = {
        "data": {
            "esiid": "TEST_ESIID_0001",
            "energyData": [
                {
                    "DT": "07/04/2026",
                    "RevTS": "07/04/2026 11:39:13",
                    "RT": "C",
                    "RD": "1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,,,,,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A,1.0-A"
                }
            ],
        }
    }

    rows = normalize_interval_payload(payload, fallback_esiid="'TEST_ESIID_0001")

    assert len(rows) == 100
    assert rows.iloc[8]["INTERVAL_STATUS"] == MISSING_INTERVAL_STATUS
    assert rows.iloc[99]["INTERVAL_INDEX"] == "99"


def test_audit_rows_accepts_100_slot_days_when_96_are_populated() -> None:
    rows = pd.DataFrame(
        [
            {
                "ESIID": "'100",
                "USAGE_DATE": "07/04/2026",
                "REVISION_DATE": "07/04/2026 11:39:13",
                "INTERVAL_INDEX": str(index),
                "INTERVAL_START_TS": f"2026-07-04T00:00:00-05:00",
                "USAGE_START_TIME": "00:00",
                "USAGE_END_TIME": "00:15",
                "USAGE_KWH": "1.0" if index not in {8, 9, 10, 11} else "",
                "ESTIMATED_ACTUAL": "A" if index not in {8, 9, 10, 11} else "",
                "INTERVAL_STATUS": PRESENT_INTERVAL_STATUS if index not in {8, 9, 10, 11} else MISSING_INTERVAL_STATUS,
                "CONSUMPTION_SURPLUSGENERATION": "Consumption",
            }
            for index in range(100)
        ]
    )

    summary = audit_rows(rows)

    assert summary.complete_days == 1
    assert summary.incomplete_days == 0


def test_capture_raw_payload_writes_json(tmp_path: Path) -> None:
    payload = {"data": {"energyData": [{"DT": "07/04/2026", "RD": "1.0-A"}]}}
    path = capture_raw_payload(payload, tmp_path, start=pd.Timestamp("2026-07-04").date(), end=pd.Timestamp("2026-07-04").date())

    assert path.exists()
    assert '07/04/2026' in path.read_text(encoding='utf-8')
