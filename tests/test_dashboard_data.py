from __future__ import annotations

from pathlib import Path
import sqlite3

import pandas as pd

from smart_meter_dashboard.data import (
    DashboardSourceConfig,
    build_null_summary,
    compute_outlier_count,
    detect_schema,
    load_dashboard_bundle,
    load_bundle_from_sqlite,
    normalize_columns,
    summarize_dataset,
)


def create_interval_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE interval_usage (
                ESIID TEXT,
                USAGE_DATE TEXT,
                REVISION_DATE TEXT,
                INTERVAL_INDEX INTEGER,
                INTERVAL_START_TS TEXT,
                USAGE_START_TIME TEXT,
                USAGE_END_TIME TEXT,
                USAGE_KWH REAL,
                ESTIMATED_ACTUAL TEXT,
                INTERVAL_STATUS TEXT,
                CONSUMPTION_SURPLUSGENERATION TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO interval_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("1001", "01/01/2026", "01/01/2026 12:00:00", 0, "2026-01-01T00:00:00", "00:00", "00:15", 1.0, "A", "PRESENT", "Consumption"),
                ("1001", "01/01/2026", "01/01/2026 12:00:00", 1, "2026-01-01T00:15:00", "00:15", "00:30", 2.5, "A", "PRESENT", "Consumption"),
                ("1002", "01/02/2026", "01/02/2026 12:00:00", 0, "2026-01-02T00:00:00", "00:00", "00:15", 4.0, "E", "PRESENT", "Surplus Generation"),
            ],
        )
        connection.commit()


def test_normalize_columns_preserves_display_mapping() -> None:
    normalized, labels = normalize_columns(["Usage Date", "Usage Date", "USAGE-KWH"])

    assert normalized == ["usage_date", "usage_date_2", "usage_kwh"]
    assert labels["usage_date"] == "Usage Date"
    assert labels["usage_date_2"] == "Usage Date"
    assert labels["usage_kwh"] == "USAGE-KWH"


def test_load_bundle_detects_datetime_and_primary_metric_from_sqlite(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    create_interval_db(data_dir / "smt_interval_usage_history.sqlite")

    bundle = load_dashboard_bundle(data_dir)

    assert bundle.source_path.endswith("smt_interval_usage_history.sqlite")
    assert bundle.schema.detected_datetime_column == "__detected_datetime"
    assert bundle.schema.detected_primary_metric_column == "usage_kwh"
    assert bundle.schema.time_source_column == "usage_start_time"
    assert "estimated_actual" in bundle.schema.available_group_columns


def test_detect_schema_excludes_numeric_id_like_columns() -> None:
    dataframe = pd.DataFrame(
        {
            "meter_id": [1001, 1002, 1003, 1004],
            "usage_kwh": [1.5, 2.0, 1.0, 3.0],
            "usage_date": ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"],
        }
    )
    labels = {column: column for column in dataframe.columns}

    schema, prepared, warnings = detect_schema(dataframe, labels)

    assert prepared["usage_date"].dtype.kind == "M"
    assert schema.detected_primary_metric_column == "usage_kwh"
    assert not warnings


def test_summary_and_quality_metrics_are_computed() -> None:
    dataframe = pd.DataFrame(
        {
            "usage_kwh": [1.0, 2.0, 2.0, 100.0],
            "meter_id": ["a", "a", "b", "b"],
            "usage_date": pd.to_datetime(["2026-01-01", "2026-01-01", "2026-01-02", "2026-01-02"]),
        }
    )
    labels = {column: column for column in dataframe.columns}
    schema, _, _ = detect_schema(dataframe, labels)

    summary = summarize_dataset(dataframe, schema, "usage_kwh")
    null_summary = build_null_summary(dataframe, schema)
    outliers = compute_outlier_count(dataframe, "usage_kwh")

    assert summary["total_rows"] == 4
    assert summary["covered_days"] == 2
    assert summary["selected_metric"]["sum"] == 105.0
    assert null_summary["missing_count"].sum() == 0
    assert outliers == 1


def test_load_bundle_from_sqlite_reads_interval_usage(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    create_interval_db(data_dir / "intervals.sqlite")

    config = DashboardSourceConfig(
        source="sqlite",
        sqlite_path="data/intervals.sqlite",
        sqlite_table="interval_usage",
    )

    source_label, dataframe, warnings = load_bundle_from_sqlite(data_dir, config)

    assert source_label.endswith("intervals.sqlite")
    assert warnings == []
    assert len(dataframe) == 3
    assert dataframe.iloc[0]["USAGE_KWH"] == 1.0
