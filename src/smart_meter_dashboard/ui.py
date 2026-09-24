from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import streamlit as st

from .data import (
    DashboardBundle,
    DatasetSchema,
    apply_dashboard_filters,
    build_null_summary,
    build_schema_report,
    compute_outlier_count,
    load_dashboard_bundle,
)


NONE_OPTION = "__none__"


def repo_root() -> Path:
    configured = os.environ.get("SMT_PROJECT_ROOT")
    return Path(configured).resolve() if configured else Path.cwd().resolve()


def data_dir() -> Path:
    root = repo_root()
    candidates = [root / "data", root.parent / "data"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def config_path() -> Path:
    root = repo_root()
    candidates = [root / "config.toml", root / "application" / "config.toml"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


@st.cache_data(show_spinner="Loading smart meter data...")
def get_dashboard_bundle(data_directory: str, config_file: str) -> DashboardBundle:
    return load_dashboard_bundle(Path(data_directory), config_path=Path(config_file))


@st.cache_data(show_spinner=False)
def get_dataset_quality_snapshot(data_directory: str, config_file: str) -> dict[str, Any]:
    bundle = load_dashboard_bundle(Path(data_directory), config_path=Path(config_file))
    metric_column = bundle.schema.detected_primary_metric_column
    return {
        "null_summary": build_null_summary(bundle.dataframe, bundle.schema),
        "schema_report": build_schema_report(bundle.dataframe, bundle.schema),
        "duplicate_rows": int(bundle.dataframe.duplicated().sum()),
        "outlier_count": compute_outlier_count(bundle.dataframe, metric_column),
    }


def configure_page(page_title: str) -> None:
    st.set_page_config(page_title=page_title, layout="wide")


def show_bundle_messages(bundle: DashboardBundle) -> None:
    for message in bundle.warnings:
        st.warning(message)


def render_sidebar(bundle: DashboardBundle) -> dict[str, Any]:
    schema = bundle.schema
    dataframe = bundle.dataframe
    initialize_sidebar_state(schema)

    with st.sidebar:
        st.title("Smart Meter Dashboard")
        st.caption(Path(bundle.csv_path).name)

        if st.button("Reset filters", use_container_width=True):
            reset_sidebar_state()
            st.rerun()

        metric_options = schema.numeric_columns or [NONE_OPTION]
        if st.session_state.selected_metric not in metric_options:
            st.session_state.selected_metric = metric_options[0]
        metric_column = st.selectbox(
            "Primary metric",
            options=metric_options,
            format_func=lambda column: schema.label_for(column) if column != NONE_OPTION else "None",
            key="selected_metric",
        )

        group_options = [NONE_OPTION, *schema.available_group_columns]
        if st.session_state.selected_group not in group_options:
            st.session_state.selected_group = group_options[0]
        group_column = st.selectbox(
            "Group field",
            options=group_options,
            format_func=lambda column: schema.label_for(column) if column != NONE_OPTION else "None",
            key="selected_group",
        )

        entity_options = [NONE_OPTION, *schema.entity_columns]
        if st.session_state.selected_entity_column not in entity_options:
            st.session_state.selected_entity_column = entity_options[0]
        entity_column = st.selectbox(
            "Entity field",
            options=entity_options,
            format_func=lambda column: schema.label_for(column) if column != NONE_OPTION else "None",
            key="selected_entity_column",
        )

        entity_values: list[str] = []
        if entity_column != NONE_OPTION and entity_column in dataframe.columns:
            entity_choices = sorted(dataframe[entity_column].dropna().astype("string").unique().tolist())
            if len(entity_choices) > 1:
                entity_values = st.multiselect(
                    "Entity values",
                    options=entity_choices,
                    default=st.session_state.selected_entity_values,
                    key="selected_entity_values",
                )

        filter_options = [NONE_OPTION, *schema.available_filter_columns]
        if st.session_state.selected_filter_column not in filter_options:
            st.session_state.selected_filter_column = filter_options[0]
        additional_filter_column = st.selectbox(
            "Additional filter",
            options=filter_options,
            format_func=lambda column: schema.label_for(column) if column != NONE_OPTION else "None",
            key="selected_filter_column",
        )

        additional_filter_values: list[str] = []
        if additional_filter_column != NONE_OPTION and additional_filter_column in dataframe.columns:
            filter_choices = sorted(
                dataframe[additional_filter_column].dropna().astype("string").unique().tolist()
            )
            additional_filter_values = st.multiselect(
                "Filter values",
                options=filter_choices,
                default=[
                    value for value in st.session_state.selected_filter_values if value in filter_choices
                ],
                key="selected_filter_values",
            )

        date_range = None
        if schema.detected_datetime_column and schema.detected_datetime_column in dataframe.columns:
            dt_series = dataframe[schema.detected_datetime_column].dropna()
            if not dt_series.empty:
                min_date = dt_series.min().date()
                max_date = dt_series.max().date()
                selected_dates = st.date_input(
                    "Date range",
                    value=st.session_state.selected_date_range or (min_date, max_date),
                    min_value=min_date,
                    max_value=max_date,
                    key="selected_date_range",
                )
                if isinstance(selected_dates, tuple) and len(selected_dates) == 2:
                    date_range = (
                        normalize_start_timestamp(selected_dates[0]),
                        normalize_end_timestamp(selected_dates[1]),
                    )

        st.divider()
        st.subheader("Dataset status")
        st.caption(f"Datetime: {schema.label_for(schema.detected_datetime_column)}")
        st.caption(f"Metric: {schema.label_for(schema.detected_primary_metric_column)}")
        st.caption(
            "Groups: "
            + (", ".join(schema.label_for(column) for column in schema.available_group_columns) or "None")
        )
        if schema.time_source_column:
            st.caption(f"Time source: {schema.label_for(schema.time_source_column)}")

    return {
        "metric_column": metric_column if metric_column != NONE_OPTION else None,
        "group_column": group_column if group_column != NONE_OPTION else None,
        "entity_column": entity_column if entity_column != NONE_OPTION else None,
        "entity_values": entity_values,
        "additional_filter_column": (
            additional_filter_column if additional_filter_column != NONE_OPTION else None
        ),
        "additional_filter_values": additional_filter_values,
        "date_range": date_range,
    }


def filtered_dataframe(bundle: DashboardBundle, controls: dict[str, Any]):
    return apply_dashboard_filters(
        bundle.dataframe,
        bundle.schema,
        date_range=controls["date_range"],
        entity_column=controls["entity_column"],
        entity_values=controls["entity_values"],
        additional_filter_column=controls["additional_filter_column"],
        additional_filter_values=controls["additional_filter_values"],
    )


def initialize_sidebar_state(schema: DatasetSchema) -> None:
    if "selected_metric" not in st.session_state:
        st.session_state.selected_metric = schema.detected_primary_metric_column or NONE_OPTION
    if "selected_group" not in st.session_state:
        st.session_state.selected_group = (
            schema.available_group_columns[0] if schema.available_group_columns else NONE_OPTION
        )
    if "selected_entity_column" not in st.session_state:
        st.session_state.selected_entity_column = (
            schema.entity_columns[0] if schema.entity_columns else NONE_OPTION
        )
    if "selected_entity_values" not in st.session_state:
        st.session_state.selected_entity_values = []
    if "selected_filter_column" not in st.session_state:
        st.session_state.selected_filter_column = NONE_OPTION
    if "selected_filter_values" not in st.session_state:
        st.session_state.selected_filter_values = []
    if "selected_date_range" not in st.session_state:
        st.session_state.selected_date_range = None


def reset_sidebar_state() -> None:
    for key in (
        "selected_metric",
        "selected_group",
        "selected_entity_column",
        "selected_entity_values",
        "selected_filter_column",
        "selected_filter_values",
        "selected_date_range",
    ):
        st.session_state.pop(key, None)


def normalize_start_timestamp(value) -> Any:
    return st.session_state.get("_start_cache") or __import__("pandas").Timestamp(value)


def normalize_end_timestamp(value) -> Any:
    pandas = __import__("pandas")
    return pandas.Timestamp(value) + pandas.Timedelta(days=1) - pandas.Timedelta(microseconds=1)
