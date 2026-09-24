from __future__ import annotations

import plotly.express as px
import streamlit as st

from smart_meter_dashboard.data import build_period_totals, format_metric_value, summarize_dataset
from smart_meter_dashboard.ui import (
    config_path,
    configure_page,
    data_dir,
    filtered_dataframe,
    get_dashboard_bundle,
    render_sidebar,
    show_bundle_messages,
)


configure_page("Smart Meter Dashboard")

bundle = get_dashboard_bundle(str(data_dir()), str(config_path()))
show_bundle_messages(bundle)
controls = render_sidebar(bundle)
filtered = filtered_dataframe(bundle, controls)
summary = summarize_dataset(filtered, bundle.schema, controls["metric_column"])
metric_column = controls["metric_column"]
metric_label = bundle.schema.label_for(metric_column)
datetime_column = bundle.schema.detected_datetime_column
period_totals = build_period_totals(
    filtered,
    datetime_column=datetime_column,
    metric_column=metric_column,
)

daily_df = period_totals["daily"].copy()
weekly_df = period_totals["weekly"].copy()
monthly_df = period_totals["monthly"].copy()

if not daily_df.empty and metric_column:
    daily_df["rolling_7d"] = daily_df[metric_column].rolling(7, min_periods=1).mean()

st.title("Dashboard")
st.caption("Operational view of daily, weekly, and monthly usage totals.")

kpis = st.columns(4)
kpis[0].metric(f"{metric_label} total", format_metric_value(summary["selected_metric"]["sum"]))
kpis[1].metric(f"{metric_label} avg", format_metric_value(summary["selected_metric"]["mean"]))
kpis[2].metric(
    "Peak day",
    format_metric_value(daily_df[metric_column].max()) if not daily_df.empty and metric_column else "N/A",
)
kpis[3].metric(
    "Lowest day",
    format_metric_value(daily_df[metric_column].min()) if not daily_df.empty and metric_column else "N/A",
)

if daily_df.empty or weekly_df.empty or monthly_df.empty or metric_column is None or datetime_column is None:
    st.info("A parseable datetime column and numeric metric are required for the dashboard charts.")
else:
    st.subheader("Daily totals")
    daily_chart = px.line(
        daily_df,
        x=datetime_column,
        y=[metric_column, "rolling_7d"],
        labels={datetime_column: "Date", "value": metric_label, "variable": "Series"},
        title=f"Daily total {metric_label} with 7-day rolling average",
    )
    daily_chart.update_layout(margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(daily_chart, use_container_width=True)

    weekly_monthly_left, weekly_monthly_right = st.columns(2)
    with weekly_monthly_left:
        st.subheader("Weekly totals")
        weekly_chart = px.bar(
            weekly_df,
            x=datetime_column,
            y=metric_column,
            labels={datetime_column: "Week", metric_column: metric_label},
        )
        weekly_chart.update_layout(margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(weekly_chart, use_container_width=True)

    with weekly_monthly_right:
        st.subheader("Monthly totals")
        monthly_chart = px.bar(
            monthly_df,
            x=datetime_column,
            y=metric_column,
            labels={datetime_column: "Month", metric_column: metric_label},
        )
        monthly_chart.update_layout(margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(monthly_chart, use_container_width=True)
