from __future__ import annotations

from pathlib import Path

import pandas as pd

from smart_meter_texas.daily import append_to_csv, find_latest_date, normalize_usage_payload


def test_normalize_usage_payload_handles_daily_data() -> None:
    payload = {
        "dailyData": [
            {
                "tdspdunsno": "957877905",
                "unitname": "KWH",
                "starttime": "12:00am",
                "endtime": "12:00am",
                "date": "07/10/2026",
                "startreading": "773.694",
                "endreading": "871.068",
                "reading": 97.371,
                "errmsg": "Success",
            }
        ]
    }

    rows = normalize_usage_payload(payload, fallback_esiid="'TEST_ESIID_0001")

    assert list(rows.columns) == [
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
    assert len(rows) == 1
    assert rows.iloc[0].to_dict() == {
        "ESIID": "'TEST_ESIID_0001",
        "USAGE_DATE": "07/10/2026",
        "USAGE_START_TIME": "00:00",
        "USAGE_END_TIME": "00:00",
        "START_READING_KWH": "773.694",
        "END_READING_KWH": "871.068",
        "USAGE_KWH": "97.371",
        "UNIT_NAME": "KWH",
        "TDSP_DUNS_NO": "957877905",
        "STATUS_MESSAGE": "Success",
    }


def test_append_to_csv_deduplicates_existing_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "usage.csv"
    initial = pd.DataFrame(
        [
            {
                "ESIID": "'100",
                "USAGE_DATE": "07/09/2026",
                "USAGE_START_TIME": "00:00",
                "USAGE_END_TIME": "00:00",
                "START_READING_KWH": "1.0",
                "END_READING_KWH": "2.0",
                "USAGE_KWH": "1.0",
                "UNIT_NAME": "KWH",
                "TDSP_DUNS_NO": "957877905",
                "STATUS_MESSAGE": "Success",
            }
        ]
    )
    initial.to_csv(csv_path, index=False)

    new_rows = pd.DataFrame(
        [
            {
                "ESIID": "'100",
                "USAGE_DATE": "07/09/2026",
                "USAGE_START_TIME": "00:00",
                "USAGE_END_TIME": "00:00",
                "START_READING_KWH": "1.1",
                "END_READING_KWH": "2.2",
                "USAGE_KWH": "1.1",
                "UNIT_NAME": "KWH",
                "TDSP_DUNS_NO": "957877905",
                "STATUS_MESSAGE": "Success",
            },
            {
                "ESIID": "'100",
                "USAGE_DATE": "07/10/2026",
                "USAGE_START_TIME": "00:00",
                "USAGE_END_TIME": "00:00",
                "START_READING_KWH": "2.2",
                "END_READING_KWH": "3.1",
                "USAGE_KWH": "0.9",
                "UNIT_NAME": "KWH",
                "TDSP_DUNS_NO": "957877905",
                "STATUS_MESSAGE": "Success",
            },
        ]
    )

    inserted = append_to_csv(csv_path, new_rows)
    written = pd.read_csv(csv_path, dtype=str)

    assert inserted == 1
    assert len(written) == 2
    assert written.iloc[0]["USAGE_KWH"] == "1.1"


def test_find_latest_date_reads_existing_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "usage.csv"
    csv_path.write_text(
        "USAGE_DATE\n07/09/2026\n07/11/2026\n",
        encoding="utf-8",
    )

    latest = find_latest_date(csv_path)

    assert latest.isoformat() == "2026-07-11"
