from __future__ import annotations

import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
import streamlit as st

from smart_meter_dashboard.data import (
    build_descriptive_stats,
    build_month_distribution,
    build_month_profile,
    build_top_bottom_periods,
    build_weekday_distribution,
    build_weekday_profile,
    format_metric_value,
)
from smart_meter_dashboard.ui import (
    config_path,
    configure_page,
    data_dir,
    filtered_dataframe,
    get_dashboard_bundle,
    render_sidebar,
    show_bundle_messages,
)


configure_page("Smart Meter Statistics")

bundle = get_dashboard_bundle(str(data_dir()), str(config_path()))
show_bundle_messages(bundle)
controls = render_sidebar(bundle)
filtered = filtered_dataframe(bundle, controls)
metric_column = controls["metric_column"]
metric_label = bundle.schema.label_for(metric_column)
datetime_column = bundle.schema.detected_datetime_column
stats = build_descriptive_stats(filtered, metric_column)
highest_days, lowest_days = build_top_bottom_periods(
    filtered,
    datetime_column=datetime_column,
    metric_column=metric_column,
)
weekday_profile = build_weekday_profile(
    filtered,
    datetime_column=datetime_column,
    metric_column=metric_column,
)
weekday_distribution = build_weekday_distribution(
    filtered,
    datetime_column=datetime_column,
    metric_column=metric_column,
)
month_profile = build_month_profile(
    filtered,
    datetime_column=datetime_column,
    metric_column=metric_column,
)
month_distribution = build_month_distribution(
    filtered,
    datetime_column=datetime_column,
    metric_column=metric_column,
)

st.title("Statistics")
st.caption("Descriptive statistics and simple analytics for the filtered dataset.")

if not stats or metric_column is None:
    st.info("Select a numeric metric to view descriptive statistics.")
else:
    top_metrics = st.columns(5)
    top_metrics[0].metric("Mean", format_metric_value(stats["mean"]))
    top_metrics[1].metric("Median", format_metric_value(stats["median"]))
    top_metrics[2].metric("Range", format_metric_value(stats["range"]))
    top_metrics[3].metric("Std dev", format_metric_value(stats["std"]))
    top_metrics[4].metric("P90", format_metric_value(stats["p90"]))

    stats_table = pd.DataFrame(
        [
            {"Statistic": "Count", "Value": stats["count"]},
            {"Statistic": "Sum", "Value": stats["sum"]},
            {"Statistic": "Mean", "Value": stats["mean"]},
            {"Statistic": "Median", "Value": stats["median"]},
            {"Statistic": "Min", "Value": stats["min"]},
            {"Statistic": "Max", "Value": stats["max"]},
            {"Statistic": "Range", "Value": stats["range"]},
            {"Statistic": "Variance", "Value": stats["variance"]},
            {"Statistic": "Std dev", "Value": stats["std"]},
            {"Statistic": "P10", "Value": stats["p10"]},
            {"Statistic": "Q1", "Value": stats["q1"]},
            {"Statistic": "Q3", "Value": stats["p75"]},
            {"Statistic": "P90", "Value": stats["p90"]},
            {"Statistic": "IQR", "Value": stats["iqr"]},
            {"Statistic": "Coeff. of variation", "Value": stats["cv"]},
            {"Statistic": "Zero count", "Value": stats["zero_count"]},
        ]
    )

    left, right = st.columns(2)
    with left:
        st.subheader("Statistics table")
        st.dataframe(stats_table, width="stretch", hide_index=True)
    with right:
        st.subheader("Distribution")
        histogram = px.histogram(
            filtered,
            x=metric_column,
            nbins=40,
            labels={metric_column: metric_label},
        )
        histogram.update_layout(margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(histogram, use_container_width=True)

    lower_left, lower_right = st.columns(2)
    with lower_left:
        st.subheader("Overall box plot")
        box_plot = px.box(filtered, y=metric_column, labels={metric_column: metric_label})
        box_plot.update_layout(margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(box_plot, use_container_width=True)
    with lower_right:
        if not weekday_profile.empty and not weekday_distribution.empty:
            st.subheader("Weekday distribution with average")
            weekday_chart = go.Figure()
            weekday_chart.add_trace(
                go.Box(
                    x=weekday_distribution["weekday"],
                    y=weekday_distribution[metric_column],
                    name="Distribution",
                    boxpoints=False,
                    marker_color="rgba(99, 110, 250, 0.35)",
                    line_color="rgba(99, 110, 250, 0.6)",
                    showlegend=False,
                )
            )
            weekday_chart.add_scatter(
                x=weekday_profile["weekday"],
                y=weekday_profile["mean"],
                name=f"Average {metric_label}",
                mode="lines+markers",
                line=dict(color="rgb(239, 85, 59)", width=3),
            )
            weekday_chart.update_layout(
                margin=dict(l=10, r=10, t=20, b=10),
                xaxis=dict(
                    title="Weekday",
                    categoryorder="array",
                    categoryarray=weekday_profile["weekday"].tolist(),
                ),
                yaxis=dict(title=f"Daily total {metric_label}"),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            st.plotly_chart(weekday_chart, use_container_width=True)

    month_left, month_right = st.columns(2)
    with month_left:
        if not month_profile.empty and not month_distribution.empty:
            st.subheader("Monthly distribution with average and range")
            month_chart = go.Figure()
            month_chart.add_trace(
                go.Box(
                    x=month_distribution["month"],
                    y=month_distribution[metric_column],
                    name="Distribution",
                    boxpoints=False,
                    marker_color="rgba(99, 110, 250, 0.35)",
                    line_color="rgba(99, 110, 250, 0.6)",
                    showlegend=False,
                )
            )
            month_chart.add_scatter(
                x=month_profile["month"],
                y=month_profile["mean"],
                name=f"Average {metric_label}",
                mode="lines+markers",
                line=dict(color="rgb(239, 85, 59)", width=3),
            )
            month_chart.update_layout(
                margin=dict(l=10, r=10, t=20, b=10),
                xaxis=dict(
                    title="Month",
                    categoryorder="array",
                    categoryarray=month_profile["month"].tolist(),
                ),
                yaxis=dict(title=f"Daily total {metric_label}"),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            st.plotly_chart(month_chart, use_container_width=True)
    with month_right:
        if not month_profile.empty:
            month_summary = month_profile[["month", "mean", "min", "max", "range"]].copy()
            month_summary.columns = ["Month", "Average", "Min", "Max", "Range"]
            st.subheader("Month profile summary")
            st.dataframe(month_summary, width="stretch", hide_index=True)

    top_bottom_left, top_bottom_right = st.columns(2)
    with top_bottom_left:
        st.subheader("Top 10 highest days")
        if highest_days.empty:
            st.info("No daily aggregates available.")
        else:
            st.dataframe(
                highest_days.rename(columns=bundle.schema.display_labels),
                width="stretch",
                hide_index=True,
            )
    with top_bottom_right:
        st.subheader("Top 10 lowest days")
        if lowest_days.empty:
            st.info("No daily aggregates available.")
        else:
            st.dataframe(
                lowest_days.rename(columns=bundle.schema.display_labels),
                width="stretch",
                hide_index=True,
            )
