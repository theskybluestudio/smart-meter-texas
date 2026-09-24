from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from smart_meter_texas.intervals import determine_window, upsert_to_sqlite


def test_determine_window_uses_default_overlap_for_incremental_runs(tmp_path: Path) -> None:
    db_path = tmp_path / "intervals.sqlite"
    upsert_to_sqlite(db_path, pd.DataFrame({
        "ESIID": ["'100", "'100"],
        "USAGE_DATE": ["07/09/2026", "07/10/2026"],
        "REVISION_DATE": ["", ""],
        "INTERVAL_INDEX": ["0", "0"],
        "INTERVAL_START_TS": ["", ""],
        "USAGE_START_TIME": ["", ""],
        "USAGE_END_TIME": ["", ""],
        "USAGE_KWH": ["1", "1"],
        "ESTIMATED_ACTUAL": ["A", "A"],
        "INTERVAL_STATUS": ["PRESENT", "PRESENT"],
        "CONSUMPTION_SURPLUSGENERATION": ["Consumption", "Consumption"],
    }))

    start, end = determine_window(
        db_path=db_path,
        state_path=tmp_path / "state.json",
        timezone_name="America/Chicago",
        bootstrap_days=7,
        overlap_days=2,
        explicit_start_date=None,
        explicit_end_date="2026-07-11",
    )

    assert start.isoformat() == "2026-07-04"
    assert end.isoformat() == "2026-07-11"


def test_determine_window_prefers_earliest_incomplete_date(tmp_path: Path) -> None:
    db_path = tmp_path / "intervals.sqlite"
    upsert_to_sqlite(db_path, pd.DataFrame({
        "ESIID": ["'100", "'100", "'100"],
        "USAGE_DATE": ["07/09/2026", "07/10/2026", "07/11/2026"],
        "REVISION_DATE": ["", "", ""],
        "INTERVAL_INDEX": ["0", "0", "0"],
        "INTERVAL_START_TS": ["", "", ""],
        "USAGE_START_TIME": ["", "", ""],
        "USAGE_END_TIME": ["", "", ""],
        "USAGE_KWH": ["1", "1", "1"],
        "ESTIMATED_ACTUAL": ["A", "A", "A"],
        "INTERVAL_STATUS": ["PRESENT", "PRESENT", "PRESENT"],
        "CONSUMPTION_SURPLUSGENERATION": ["Consumption", "Consumption", "Consumption"],
    }))
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "last_complete_date": "2026-07-11",
                "incomplete_dates": ["2026-07-08", "2026-07-10"],
            }
        ),
        encoding="utf-8",
    )

    start, end = determine_window(
        db_path=db_path,
        state_path=state_path,
        timezone_name="America/Chicago",
        bootstrap_days=7,
        overlap_days=7,
        explicit_start_date=None,
        explicit_end_date="2026-07-12",
    )

    assert start.isoformat() == "2026-07-05"
    assert end.isoformat() == "2026-07-12"


def test_determine_window_respects_explicit_start_date(tmp_path: Path) -> None:
    start, end = determine_window(
        db_path=tmp_path / "intervals.sqlite",
        state_path=tmp_path / "state.json",
        timezone_name="America/Chicago",
        bootstrap_days=7,
        overlap_days=7,
        explicit_start_date="2026-07-10",
        explicit_end_date="2026-07-11",
    )

    assert start.isoformat() == "2026-07-10"
    assert end.isoformat() == "2026-07-11"
