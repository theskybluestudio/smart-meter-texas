from __future__ import annotations

import streamlit as st

from smart_meter_dashboard.data import build_null_summary, build_schema_report, compute_outlier_count, summarize_dataset
from smart_meter_dashboard.ui import (
    config_path,
    configure_page,
    data_dir,
    filtered_dataframe,
    get_dashboard_bundle,
    get_dataset_quality_snapshot,
    render_sidebar,
    show_bundle_messages,
)


configure_page("Smart Meter QC")

bundle = get_dashboard_bundle(str(data_dir()), str(config_path()))
quality_snapshot = get_dataset_quality_snapshot(str(data_dir()), str(config_path()))
show_bundle_messages(bundle)
controls = render_sidebar(bundle)
filtered = filtered_dataframe(bundle, controls)
summary = summarize_dataset(filtered, bundle.schema, controls["metric_column"])
metric_label = bundle.schema.label_for(controls["metric_column"])
null_summary = build_null_summary(filtered, bundle.schema)
outlier_count = compute_outlier_count(filtered, controls["metric_column"])

date_window = "N/A"
if summary["date_min"] is not None and summary["date_max"] is not None:
    date_window = f"{summary['date_min']:%Y-%m-%d} to {summary['date_max']:%Y-%m-%d}"

st.title("QC")
st.caption("Quality control view for validating the currently loaded smart meter dataset.")

st.subheader("Current dataset")
st.write(
    {
        "source": bundle.csv_path,
        "datetime_column": bundle.schema.label_for(bundle.schema.detected_datetime_column),
        "primary_metric": bundle.schema.label_for(bundle.schema.detected_primary_metric_column),
        "group_fields": [bundle.schema.label_for(column) for column in bundle.schema.available_group_columns],
    }
)

quality_metrics = st.columns(5)
quality_metrics[0].metric("Filtered rows", f"{summary['total_rows']:,}")
quality_metrics[1].metric("Date window", date_window)
quality_metrics[2].metric("Duplicate rows", f"{summary['duplicate_rows']:,}")
quality_metrics[3].metric("Invalid dates", f"{bundle.schema.datetime_invalid_count:,}")
quality_metrics[4].metric("Freshness", summary["latest_timestamp"].strftime("%Y-%m-%d %H:%M") if summary["latest_timestamp"] is not None else "N/A")

secondary_metrics = st.columns(4)
secondary_metrics[0].metric("Covered days", f"{summary['covered_days']:,}")
secondary_metrics[1].metric("Outliers (IQR)", f"{outlier_count:,}")
secondary_metrics[2].metric("Datetime parse success", f"{bundle.schema.datetime_parse_success_rate * 100:.1f}%")
secondary_metrics[3].metric(f"{metric_label} total", f"{summary['selected_metric']['sum'] or 0:,.2f}" if controls["metric_column"] else "N/A")

st.subheader("Recent data")
preview = filtered.copy()
if bundle.schema.detected_datetime_column and bundle.schema.detected_datetime_column in preview.columns:
    preview = preview.sort_values(by=bundle.schema.detected_datetime_column, ascending=False)
st.dataframe(
    preview.head(100).rename(columns=bundle.schema.display_labels),
    width="stretch",
    hide_index=True,
)

left, right = st.columns((2, 1))
with left:
    st.subheader("Null summary")
    st.dataframe(
        null_summary[["display_column", "dtype", "missing_count", "missing_rate"]],
        width="stretch",
        hide_index=True,
    )
with right:
    st.subheader("Dataset status")
    st.metric("Full duplicate rows", f"{quality_snapshot['duplicate_rows']:,}")
    st.metric("Full outliers (IQR)", f"{quality_snapshot['outlier_count']:,}")
    st.metric("Selected metric", metric_label)

st.subheader("Schema report")
schema_report = build_schema_report(filtered, bundle.schema)
st.dataframe(
    schema_report[["display_column", "dtype", "non_null_count", "unique_count", "role"]],
    width="stretch",
    hide_index=True,
)
