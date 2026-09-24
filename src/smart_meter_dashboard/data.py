from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

import pandas as pd

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None


ID_TOKENS = ("id", "esiid", "meter", "premise", "account", "service", "identifier")
TIME_VALUE_PATTERN = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s*$")


@dataclass(frozen=True)
class DatasetSchema:
    detected_datetime_column: str | None
    detected_primary_metric_column: str | None
    available_group_columns: list[str]
    available_filter_columns: list[str]
    entity_columns: list[str]
    numeric_columns: list[str]
    display_labels: dict[str, str]
    date_source_column: str | None
    time_source_column: str | None
    datetime_parse_success_rate: float
    datetime_invalid_count: int

    def label_for(self, column_name: str | None) -> str:
        if column_name is None:
            return "None"
        return self.display_labels.get(column_name, column_name)


@dataclass(frozen=True)
class DashboardBundle:
    source_path: str
    dataframe: pd.DataFrame
    schema: DatasetSchema
    warnings: list[str]


@dataclass(frozen=True)
class DashboardSourceConfig:
    source: str
    sqlite_path: str
    sqlite_table: str


SUPPORTED_SOURCES = ("sqlite",)
DEFAULT_CONFIG = {
    "data": {
        "source": "sqlite",
    },
    "sqlite": {
        "path": "data/smt_interval_usage_history.sqlite",
        "table": "interval_usage",
    },
}


def default_config_path(data_dir: Path) -> Path:
    candidates = [
        data_dir.parent / "config.toml",
        data_dir.parent / "application" / "config.toml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def parse_config_toml(text: str) -> dict[str, Any]:
    if tomllib is not None:
        return tomllib.loads(text)

    parsed: dict[str, Any] = {}
    current_section: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1].strip()
            current_section = parsed.setdefault(section_name, {})
            continue
        if current_section is None or "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        if value.startswith("[") and value.endswith("]"):
            items = [item.strip().strip('"').strip("'") for item in value[1:-1].split(",") if item.strip()]
            current_section[key] = items
        else:
            current_section[key] = value.strip('"').strip("'")
    return parsed


def load_dashboard_config(data_dir: Path, config_path: Path | None = None) -> DashboardSourceConfig:
    resolved_config_path = config_path or default_config_path(data_dir)
    raw: dict[str, Any] = {}
    if resolved_config_path.exists():
        raw = parse_config_toml(resolved_config_path.read_text(encoding="utf-8"))

    data_config = {**DEFAULT_CONFIG["data"], **raw.get("data", {})}
    sqlite_config = {**DEFAULT_CONFIG["sqlite"], **raw.get("sqlite", {})}
    source = str(os.environ.get("SMT_DASHBOARD_SOURCE") or data_config["source"]).strip().lower()

    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"Unsupported dashboard source: {source}")

    return DashboardSourceConfig(
        source=source,
        sqlite_path=str(sqlite_config["path"]),
        sqlite_table=str(sqlite_config.get("table") or "interval_usage"),
    )


def load_bundle_from_sqlite(data_dir: Path, config: DashboardSourceConfig) -> tuple[str, pd.DataFrame, list[str]]:
    db_path = (data_dir.parent / config.sqlite_path).resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite source not found: {db_path}")

    order_by = ""
    if config.sqlite_table == "interval_usage":
        order_by = " ORDER BY date(substr(USAGE_DATE, 7, 4) || '-' || substr(USAGE_DATE, 1, 2) || '-' || substr(USAGE_DATE, 4, 2)), INTERVAL_INDEX, ESIID"
    query = f"SELECT * FROM {config.sqlite_table}{order_by}"
    with sqlite3.connect(db_path) as connection:
        dataframe = pd.read_sql_query(query, connection)
    return str(db_path), dataframe, []


def normalize_columns(columns: list[str]) -> tuple[list[str], dict[str, str]]:
    normalized: list[str] = []
    display_labels: dict[str, str] = {}
    seen: dict[str, int] = {}

    for original in columns:
        base = re.sub(r"[^0-9a-zA-Z]+", "_", original.strip().lower()).strip("_")
        if not base:
            base = "column"

        counter = seen.get(base, 0)
        seen[base] = counter + 1
        name = f"{base}_{counter + 1}" if counter else base

        normalized.append(name)
        display_labels[name] = original

    return normalized, display_labels


def load_dashboard_bundle(data_dir: Path, config_path: Path | None = None) -> DashboardBundle:
    config = load_dashboard_config(data_dir, config_path=config_path)
    warnings: list[str] = []
    try:
        source_label, dataframe, source_warnings = load_bundle_from_sqlite(data_dir, config)
        warnings.extend(source_warnings)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Unable to load dashboard data from SQLite. {exc}.") from exc

    normalized_columns, display_labels = normalize_columns(list(dataframe.columns))
    dataframe.columns = normalized_columns

    schema, prepared_dataframe, schema_warnings = detect_schema(dataframe, display_labels)
    warnings.extend(schema_warnings)
    return DashboardBundle(
        source_path=source_label,
        dataframe=prepared_dataframe,
        schema=schema,
        warnings=warnings,
    )


def detect_schema(
    dataframe: pd.DataFrame, display_labels: dict[str, str]
) -> tuple[DatasetSchema, pd.DataFrame, list[str]]:
    prepared = dataframe.copy()
    warnings: list[str] = []

    datetime_column, date_source_column, time_source_column, parse_success_rate, invalid_count = (
        detect_datetime_column(prepared)
    )
    numeric_columns = detect_numeric_columns(prepared)
    primary_metric_column = detect_primary_metric_column(prepared, numeric_columns)
    entity_columns = detect_entity_columns(prepared)
    available_filter_columns = detect_filter_columns(prepared)
    available_group_columns = [
        column
        for column in available_filter_columns
        if prepared[column].nunique(dropna=True) > 1 and prepared[column].nunique(dropna=True) <= 24
    ]

    if datetime_column is None:
        warnings.append(
            "No parseable date or datetime column was detected. Time-based charts will be disabled."
        )
    if primary_metric_column is None:
        warnings.append(
            "No numeric metric column was detected. Metric summary cards and charts will be limited."
        )

    schema = DatasetSchema(
        detected_datetime_column=datetime_column,
        detected_primary_metric_column=primary_metric_column,
        available_group_columns=available_group_columns,
        available_filter_columns=available_filter_columns,
        entity_columns=entity_columns,
        numeric_columns=numeric_columns,
        display_labels=display_labels,
        date_source_column=date_source_column,
        time_source_column=time_source_column,
        datetime_parse_success_rate=parse_success_rate,
        datetime_invalid_count=invalid_count,
    )
    return schema, prepared, warnings


def parse_mixed_datetime(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", format="mixed", utc=True)
    if pd.api.types.is_datetime64tz_dtype(parsed):
        return parsed.dt.tz_convert(None)
    return parsed



def detect_datetime_column(
    dataframe: pd.DataFrame,
) -> tuple[str | None, str | None, str | None, float, int]:
    candidates: list[tuple[str, pd.Series, float, bool]] = []

    for column in dataframe.columns:
        series = dataframe[column]
        if pd.api.types.is_numeric_dtype(series):
            continue

        cleaned = series.astype("string").str.strip()
        non_null = cleaned.dropna()
        if non_null.empty:
            continue
        if non_null.str.match(TIME_VALUE_PATTERN).mean() >= 0.8:
            continue

        parsed = parse_mixed_datetime(cleaned)
        success_rate = parsed.notna().mean()
        has_time_component = False
        if parsed.notna().any() and pd.api.types.is_datetime64_any_dtype(parsed):
            valid = parsed[parsed.notna()]
            has_time_component = bool(
                ((valid.dt.hour != 0) | (valid.dt.minute != 0) | (valid.dt.second != 0)).any()
            )

        if success_rate >= 0.6:
            candidates.append((column, parsed, float(success_rate), has_time_component))

    if not candidates:
        return None, None, None, 0.0, 0

    candidates.sort(key=lambda item: (item[2], item[3]), reverse=True)
    base_column, parsed_series, success_rate, has_time_component = candidates[0]

    chosen_column = base_column
    date_source_column = base_column
    time_source_column: str | None = None
    invalid_count = int(parsed_series.isna().sum() - dataframe[base_column].isna().sum())
    dataframe[base_column] = parsed_series

    if not has_time_component:
        time_candidates = detect_time_columns(dataframe, excluded_columns={base_column})
        if time_candidates:
            best_time_column = time_candidates[0]
            combined = parse_mixed_datetime(
                dataframe[base_column].dt.strftime("%Y-%m-%d").fillna("")
                + " "
                + dataframe[best_time_column].astype("string").fillna("")
            )
            combined_success = combined.notna().mean()
            if combined_success >= success_rate:
                chosen_column = "__detected_datetime"
                dataframe[chosen_column] = combined
                time_source_column = best_time_column
                invalid_count = int(combined.isna().sum() - dataframe[base_column].isna().sum())
                success_rate = float(combined_success)

    return chosen_column, date_source_column, time_source_column, success_rate, max(invalid_count, 0)


def detect_time_columns(dataframe: pd.DataFrame, excluded_columns: set[str]) -> list[str]:
    candidates: list[tuple[str, float]] = []
    for column in dataframe.columns:
        if column in excluded_columns:
            continue

        series = dataframe[column]
        if pd.api.types.is_numeric_dtype(series):
            continue

        cleaned = series.astype("string").str.strip()
        non_null = cleaned.dropna()
        if non_null.empty:
            continue

        match_rate = non_null.str.match(TIME_VALUE_PATTERN).mean()
        if match_rate >= 0.8:
            candidates.append((column, float(match_rate)))

    candidates.sort(key=lambda item: item[1], reverse=True)
    return [column for column, _ in candidates]


def detect_numeric_columns(dataframe: pd.DataFrame) -> list[str]:
    numeric_columns: list[str] = []
    for column in dataframe.columns:
        series = dataframe[column]
        if pd.api.types.is_numeric_dtype(series):
            numeric_columns.append(column)
            continue

        cleaned = series.astype("string").str.replace(",", "", regex=False)
        parsed = pd.to_numeric(cleaned, errors="coerce")
        success_rate = parsed.notna().mean()
        if success_rate >= 0.95:
            dataframe[column] = parsed
            numeric_columns.append(column)

    return numeric_columns


def detect_primary_metric_column(dataframe: pd.DataFrame, numeric_columns: list[str]) -> str | None:
    ranked_candidates: list[tuple[int, float, int, str]] = []

    preferred_tokens = (
        "usage_kwh",
        "kwh",
        "usage",
        "consumption",
        "demand",
        "load",
        "reading",
        "value",
        "amount",
        "total",
    )
    discouraged_tokens = (
        "index",
        "order",
        "rank",
        "count",
        "year",
        "month",
        "day",
    )

    for column in numeric_columns:
        series = dataframe[column]
        non_null_rate = float(series.notna().mean())
        id_like_penalty = 0 if not is_id_like(column, series) else 1
        lowered = column.lower()
        preference_score = 0
        if any(token in lowered for token in preferred_tokens):
            preference_score += 2
        if any(token in lowered for token in discouraged_tokens):
            preference_score -= 2
        ranked_candidates.append((preference_score, non_null_rate, -id_like_penalty, column))

    if not ranked_candidates:
        return None

    ranked_candidates.sort(reverse=True)
    return ranked_candidates[0][3]


def is_id_like(column_name: str, series: pd.Series) -> bool:
    lowered = column_name.lower()
    if any(token in lowered for token in ID_TOKENS):
        return True

    non_null = series.dropna()
    if non_null.empty:
        return False

    unique_ratio = non_null.nunique() / max(len(non_null), 1)
    is_integer_like = pd.api.types.is_integer_dtype(series) or (
        pd.api.types.is_float_dtype(series)
        and (non_null.astype(float).round() == non_null.astype(float)).all()
    )
    return bool(is_integer_like and unique_ratio > 0.9 and non_null.nunique() > 20)


def detect_entity_columns(dataframe: pd.DataFrame) -> list[str]:
    columns: list[str] = []
    for column in dataframe.columns:
        series = dataframe[column]
        if pd.api.types.is_numeric_dtype(series):
            continue
        if any(token in column.lower() for token in ID_TOKENS):
            columns.append(column)
    return columns


def detect_filter_columns(dataframe: pd.DataFrame) -> list[str]:
    columns: list[str] = []
    row_count = max(len(dataframe), 1)

    for column in dataframe.columns:
        series = dataframe[column]
        if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_datetime64_any_dtype(series):
            continue

        unique_count = series.nunique(dropna=True)
        unique_ratio = unique_count / row_count
        if unique_count <= 200 and (
            unique_ratio <= 0.5 or unique_count <= 24 or any(token in column for token in ID_TOKENS)
        ):
            columns.append(column)

    return columns


def apply_dashboard_filters(
    dataframe: pd.DataFrame,
    schema: DatasetSchema,
    *,
    date_range: tuple[pd.Timestamp, pd.Timestamp] | None,
    entity_column: str | None,
    entity_values: list[str] | None,
    additional_filter_column: str | None,
    additional_filter_values: list[str] | None,
) -> pd.DataFrame:
    filtered = dataframe.copy()

    if schema.detected_datetime_column and date_range:
        start, end = date_range
        mask = filtered[schema.detected_datetime_column].between(start, end, inclusive="both")
        filtered = filtered.loc[mask]

    if entity_column and entity_values:
        filtered = filtered[filtered[entity_column].astype("string").isin(entity_values)]

    if additional_filter_column and additional_filter_values:
        filtered = filtered[
            filtered[additional_filter_column].astype("string").isin(additional_filter_values)
        ]

    return filtered


def summarize_metric(dataframe: pd.DataFrame, metric_column: str | None) -> dict[str, Any]:
    if metric_column is None or metric_column not in dataframe.columns:
        return {
            "sum": None,
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
            "std": None,
        }

    series = dataframe[metric_column].dropna()
    if series.empty:
        return {
            "sum": 0.0,
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
            "std": None,
        }

    return {
        "sum": float(series.sum()),
        "mean": float(series.mean()),
        "median": float(series.median()),
        "min": float(series.min()),
        "max": float(series.max()),
        "std": float(series.std()) if len(series) > 1 else 0.0,
    }


def summarize_dataset(
    dataframe: pd.DataFrame, schema: DatasetSchema, metric_column: str | None
) -> dict[str, Any]:
    summary = {
        "total_rows": int(len(dataframe)),
        "duplicate_rows": int(dataframe.duplicated().sum()),
        "selected_metric": summarize_metric(dataframe, metric_column),
    }

    if schema.detected_datetime_column and schema.detected_datetime_column in dataframe.columns:
        dt_series = dataframe[schema.detected_datetime_column].dropna()
        if not dt_series.empty:
            summary.update(
                {
                    "date_min": dt_series.min(),
                    "date_max": dt_series.max(),
                    "covered_days": int(dt_series.dt.normalize().nunique()),
                    "latest_timestamp": dt_series.max(),
                }
            )
        else:
            summary.update(
                {"date_min": None, "date_max": None, "covered_days": 0, "latest_timestamp": None}
            )
    else:
        summary.update({"date_min": None, "date_max": None, "covered_days": 0, "latest_timestamp": None})

    unique_counts: dict[str, int] = {}
    for column in schema.entity_columns[:3]:
        unique_counts[column] = int(dataframe[column].nunique(dropna=True))
    summary["unique_counts"] = unique_counts

    return summary


def build_null_summary(dataframe: pd.DataFrame, schema: DatasetSchema) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    row_count = max(len(dataframe), 1)

    for column in dataframe.columns:
        missing_count = int(dataframe[column].isna().sum())
        rows.append(
            {
                "column": column,
                "display_column": schema.label_for(column),
                "dtype": str(dataframe[column].dtype),
                "missing_count": missing_count,
                "missing_rate": round(missing_count / row_count, 4),
            }
        )

    return pd.DataFrame(rows).sort_values(
        by=["missing_count", "display_column"], ascending=[False, True]
    )


def build_schema_report(dataframe: pd.DataFrame, schema: DatasetSchema) -> pd.DataFrame:
    role_map: dict[str, str] = {}
    if schema.detected_datetime_column:
        role_map[schema.detected_datetime_column] = "datetime"
    if schema.detected_primary_metric_column:
        role_map[schema.detected_primary_metric_column] = "primary_metric"
    for column in schema.available_group_columns:
        role_map.setdefault(column, "group")
    for column in schema.available_filter_columns:
        role_map.setdefault(column, "filter")
    for column in schema.entity_columns:
        role_map.setdefault(column, "entity")

    rows: list[dict[str, Any]] = []
    for column in dataframe.columns:
        rows.append(
            {
                "column": column,
                "display_column": schema.label_for(column),
                "dtype": str(dataframe[column].dtype),
                "non_null_count": int(dataframe[column].notna().sum()),
                "unique_count": int(dataframe[column].nunique(dropna=True)),
                "role": role_map.get(column, "other"),
            }
        )

    return pd.DataFrame(rows).sort_values(by=["role", "display_column"])


def compute_outlier_count(dataframe: pd.DataFrame, metric_column: str | None) -> int:
    if metric_column is None or metric_column not in dataframe.columns:
        return 0

    series = dataframe[metric_column].dropna()
    if len(series) < 4:
        return 0

    q1 = series.quantile(0.25)
    q3 = series.quantile(0.75)
    iqr = q3 - q1
    if iqr == 0:
        return 0

    lower_bound = q1 - (1.5 * iqr)
    upper_bound = q3 + (1.5 * iqr)
    return int(((series < lower_bound) | (series > upper_bound)).sum())


def build_time_series(
    dataframe: pd.DataFrame,
    *,
    datetime_column: str | None,
    metric_column: str | None,
    frequency: str,
    group_column: str | None = None,
) -> pd.DataFrame:
    if datetime_column is None or metric_column is None:
        return pd.DataFrame()

    if datetime_column not in dataframe.columns or metric_column not in dataframe.columns:
        return pd.DataFrame()

    columns = [datetime_column, metric_column]
    if group_column:
        columns.append(group_column)

    working = dataframe[columns].dropna(subset=[datetime_column, metric_column])
    if working.empty:
        return pd.DataFrame()

    working = working.sort_values(by=datetime_column)
    frequency_map = {
        "Interval": None,
        "Daily": "D",
        "Weekly": "W",
        "Monthly": "M",
    }
    freq = frequency_map[frequency]

    if freq is None:
        if group_column:
            grouped = working.groupby([datetime_column, group_column], as_index=False)[metric_column].sum()
        else:
            grouped = working.groupby(datetime_column, as_index=False)[metric_column].sum()
        return grouped

    working = working.copy()
    working[datetime_column] = working[datetime_column].dt.to_period(freq).dt.to_timestamp()

    if group_column:
        grouped = working.groupby([datetime_column, group_column], as_index=False)[metric_column].sum()
    else:
        grouped = working.groupby(datetime_column, as_index=False)[metric_column].sum()
    return grouped.dropna(subset=[datetime_column]).sort_values(datetime_column)


def build_period_totals(
    dataframe: pd.DataFrame,
    *,
    datetime_column: str | None,
    metric_column: str | None,
) -> dict[str, pd.DataFrame]:
    return {
        "daily": build_time_series(
            dataframe,
            datetime_column=datetime_column,
            metric_column=metric_column,
            frequency="Daily",
        ),
        "weekly": build_time_series(
            dataframe,
            datetime_column=datetime_column,
            metric_column=metric_column,
            frequency="Weekly",
        ),
        "monthly": build_time_series(
            dataframe,
            datetime_column=datetime_column,
            metric_column=metric_column,
            frequency="Monthly",
        ),
    }


def build_descriptive_stats(dataframe: pd.DataFrame, metric_column: str | None) -> dict[str, Any]:
    if metric_column is None or metric_column not in dataframe.columns:
        return {}

    series = dataframe[metric_column].dropna()
    if series.empty:
        return {}

    q1 = float(series.quantile(0.25))
    q3 = float(series.quantile(0.75))
    mean_value = float(series.mean())
    std_value = float(series.std()) if len(series) > 1 else 0.0
    return {
        "count": int(series.count()),
        "sum": float(series.sum()),
        "mean": mean_value,
        "median": float(series.median()),
        "min": float(series.min()),
        "max": float(series.max()),
        "range": float(series.max() - series.min()),
        "std": std_value,
        "variance": float(series.var()) if len(series) > 1 else 0.0,
        "p10": float(series.quantile(0.10)),
        "q1": q1,
        "p75": q3,
        "p90": float(series.quantile(0.90)),
        "iqr": float(q3 - q1),
        "cv": float(std_value / mean_value) if mean_value else None,
        "zero_count": int((series == 0).sum()),
    }


def build_top_bottom_periods(
    dataframe: pd.DataFrame,
    *,
    datetime_column: str | None,
    metric_column: str | None,
    limit: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    daily = build_time_series(
        dataframe,
        datetime_column=datetime_column,
        metric_column=metric_column,
        frequency="Daily",
    )
    if daily.empty or metric_column is None:
        return pd.DataFrame(), pd.DataFrame()

    highest = daily.nlargest(limit, metric_column).copy()
    lowest = daily.nsmallest(limit, metric_column).copy()
    return highest, lowest


def _build_daily_totals(
    dataframe: pd.DataFrame,
    *,
    datetime_column: str | None,
    metric_column: str | None,
) -> pd.DataFrame:
    if datetime_column is None or metric_column is None:
        return pd.DataFrame()
    if datetime_column not in dataframe.columns or metric_column not in dataframe.columns:
        return pd.DataFrame()

    working = dataframe[[datetime_column, metric_column]].dropna(subset=[datetime_column, metric_column]).copy()
    if working.empty:
        return pd.DataFrame()

    working["date"] = working[datetime_column].dt.normalize()
    return working.groupby("date", as_index=False)[metric_column].sum()


def build_weekday_distribution(
    dataframe: pd.DataFrame,
    *,
    datetime_column: str | None,
    metric_column: str | None,
) -> pd.DataFrame:
    daily_totals = _build_daily_totals(
        dataframe,
        datetime_column=datetime_column,
        metric_column=metric_column,
    )
    if daily_totals.empty:
        return pd.DataFrame()

    daily_totals["weekday"] = daily_totals["date"].dt.day_name()
    daily_totals["weekday_order"] = daily_totals["date"].dt.dayofweek
    return daily_totals.sort_values(["weekday_order", "date"])


def build_weekday_profile(
    dataframe: pd.DataFrame,
    *,
    datetime_column: str | None,
    metric_column: str | None,
) -> pd.DataFrame:
    daily_totals = build_weekday_distribution(
        dataframe,
        datetime_column=datetime_column,
        metric_column=metric_column,
    )
    if daily_totals.empty:
        return pd.DataFrame()

    profile = (
        daily_totals.groupby(["weekday_order", "weekday"], as_index=False)[metric_column]
        .agg(["mean", "min", "max"])
        .reset_index()
        .sort_values("weekday_order")
    )
    profile["range"] = profile["max"] - profile["min"]
    profile["error_minus"] = profile["mean"] - profile["min"]
    profile["error_plus"] = profile["max"] - profile["mean"]
    return profile


def build_month_distribution(
    dataframe: pd.DataFrame,
    *,
    datetime_column: str | None,
    metric_column: str | None,
) -> pd.DataFrame:
    daily_totals = _build_daily_totals(
        dataframe,
        datetime_column=datetime_column,
        metric_column=metric_column,
    )
    if daily_totals.empty:
        return pd.DataFrame()

    daily_totals["month"] = daily_totals["date"].dt.month_name().str.slice(stop=3)
    daily_totals["month_order"] = daily_totals["date"].dt.month
    return daily_totals.sort_values(["month_order", "date"])


def build_month_profile(
    dataframe: pd.DataFrame,
    *,
    datetime_column: str | None,
    metric_column: str | None,
) -> pd.DataFrame:
    daily_totals = build_month_distribution(
        dataframe,
        datetime_column=datetime_column,
        metric_column=metric_column,
    )
    if daily_totals.empty:
        return pd.DataFrame()

    profile = (
        daily_totals.groupby(["month_order", "month"], as_index=False)[metric_column]
        .agg(["mean", "min", "max"])
        .reset_index()
        .sort_values("month_order")
    )
    profile["range"] = profile["max"] - profile["min"]
    profile["error_minus"] = profile["mean"] - profile["min"]
    profile["error_plus"] = profile["max"] - profile["mean"]
    return profile


def format_metric_value(value: float | int | None, *, decimals: int = 2) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{value:,.{decimals}f}"
