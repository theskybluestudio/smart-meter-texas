from __future__ import annotations

from pathlib import Path

import pandas as pd

from smart_meter_dashboard.data import (
    DashboardSourceConfig,
    build_null_summary,
    compute_outlier_count,
    detect_schema,
    find_primary_csv,
    load_dashboard_bundle,
    load_bundle_from_sqlite,
    normalize_columns,
    resolve_source_order,
    summarize_dataset,
)


def write_csv(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_find_primary_csv_prefers_master_interval_history_when_present(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_csv(data_dir / "z_file.csv", "value\n1\n")
    write_csv(data_dir / "a_file.csv", "value\n2\n")
    write_csv(data_dir / "smt_interval_usage_history.csv", "value\n3\n")

    csv_path, warnings = find_primary_csv(data_dir)

    assert csv_path.name == "smt_interval_usage_history.csv"
    assert warnings


def test_normalize_columns_preserves_display_mapping() -> None:
    normalized, labels = normalize_columns(["Usage Date", "Usage Date", "USAGE-KWH"])

    assert normalized == ["usage_date", "usage_date_2", "usage_kwh"]
    assert labels["usage_date"] == "Usage Date"
    assert labels["usage_date_2"] == "Usage Date"
    assert labels["usage_kwh"] == "USAGE-KWH"


def test_load_bundle_detects_datetime_and_primary_metric(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_csv(
        data_dir / "interval.csv",
        "\n".join(
            [
                "ESIID,Usage Date,Usage Start Time,Usage KWH,Status,Flow",
                "1001,2026-01-01,00:00,1.0,A,Consumption",
                "1001,2026-01-01,00:15,2.5,A,Consumption",
                "1002,2026-01-02,00:00,4.0,E,Generation",
            ]
        ),
    )

    bundle = load_dashboard_bundle(data_dir)

    assert bundle.schema.detected_datetime_column == "__detected_datetime"
    assert bundle.schema.detected_primary_metric_column == "usage_kwh"
    assert bundle.schema.time_source_column == "usage_start_time"
    assert "status" in bundle.schema.available_group_columns


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


def test_resolve_source_order_deduplicates_entries() -> None:
    config = DashboardSourceConfig(
        source="sqlite",
        fallback_sources=["csv", "sqlite"],
        csv_path="data/example.csv",
        sqlite_path="data/example.sqlite",
        sqlite_table="interval_usage",
    )

    assert resolve_source_order(config) == ["sqlite", "csv"]


def test_load_bundle_from_sqlite_reads_interval_usage(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "intervals.sqlite"

    with __import__("sqlite3").connect(db_path) as connection:
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
        connection.execute(
            """
            INSERT INTO interval_usage VALUES
            ('1001', '08/01/2026', '08/01/2026 12:00:00', 0, '2026-08-01T00:00:00', '00:00', '00:15', 1.25, 'A', 'PRESENT', 'Consumption')
            """
        )
        connection.commit()

    config = DashboardSourceConfig(
        source="sqlite",
        fallback_sources=["csv"],
        csv_path="data/example.csv",
        sqlite_path="data/intervals.sqlite",
        sqlite_table="interval_usage",
    )

    source_label, dataframe, warnings = load_bundle_from_sqlite(data_dir, config)

    assert source_label.endswith("intervals.sqlite")
    assert warnings == []
    assert len(dataframe) == 1
    assert dataframe.iloc[0]["USAGE_KWH"] == 1.25
