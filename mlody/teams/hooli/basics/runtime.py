"""Runtime helpers for the Hooli basics MLody demo."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

COUNTRY_STATS_SOURCE_URL = "https://tinyurl.com/mry64ebh"

COUNTRY_STATS_FIELDS = (
    "country",
    "pop_2022",
    "pop_2023",
    "continent",
    "region",
    "pop_change",
)

CONTINENT_STATS_FIELDS = (
    "continent",
    "pop_2022",
    "pop_2023",
    "pop_change",
    "pop_change_factor",
)

MODEL_REQUIRED_KEYS = (
    "intercept",
    "coefficients",
    "r2_score",
    "rmse",
    "mae",
    "num_training_samples",
)

COUNTRY_TABLE_HEADERS = {
    "country": "country",
    "population (1 july 2022)": "pop_2022",
    "population (1 july 2023)": "pop_2023",
    "un continental region[1]": "continent",
    "un statistical subregion[1]": "region",
}


@dataclass(frozen=True)
class CountryStatsCheckResult:
    passed: bool
    row_count: int
    checked_columns: list[str]
    message: str


@dataclass(frozen=True)
class ContinentCoefficient:
    continent: str
    coefficient: float


@dataclass(frozen=True)
class PopulationChangeModel:
    intercept: float
    coefficients: list[ContinentCoefficient]
    r2_score: float
    rmse: float
    mae: float
    num_training_samples: int


def _round4(value: float) -> float:
    return round(float(value), 4)


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _normalize_headers(columns: pd.Index) -> list[str]:
    return [str(column).strip().lower() for column in columns]


def load_country_stats_frame(source: str) -> pd.DataFrame:
    table = pd.read_html(source, flavor="html5lib")[0].copy()
    normalized_headers = _normalize_headers(table.columns)
    header_map = {
        original: COUNTRY_TABLE_HEADERS[normalized]
        for original, normalized in zip(table.columns, normalized_headers)
        if normalized in COUNTRY_TABLE_HEADERS
    }
    if set(header_map.values()) != {"country", "pop_2022", "pop_2023", "continent", "region"}:
        raise ValueError("could not find the expected country population columns")

    table = table.rename(columns=header_map)
    table = table[["country", "pop_2022", "pop_2023", "continent", "region"]]
    for column in ("country", "continent", "region"):
        table[column] = table[column].astype(str).str.strip()
    for column in ("pop_2022", "pop_2023"):
        table[column] = pd.to_numeric(
            table[column].astype(str).str.replace(r"[^0-9]", "", regex=True)
        )
    table = table.dropna(subset=["country", "continent", "region"])
    table["pop_change"] = ((table["pop_2023"] / table["pop_2022"]) - 1.0) * 100.0
    table["pop_change"] = table["pop_change"].round(4)
    table = table[list(COUNTRY_STATS_FIELDS)].reset_index(drop=True)
    if table.empty:
        raise ValueError("country stats source did not yield any rows")
    return table


def write_country_stats_csv(frame: pd.DataFrame, output_path: Path) -> None:
    _ensure_parent_dir(output_path)
    frame.to_csv(output_path, index=False)


def read_country_stats_frame(input_path: Path) -> pd.DataFrame:
    return pd.read_csv(input_path)


def check_country_stats_frame(frame: pd.DataFrame) -> CountryStatsCheckResult:
    if frame.empty:
        return CountryStatsCheckResult(
            passed=False,
            row_count=0,
            checked_columns=list(COUNTRY_STATS_FIELDS),
            message="country stats is empty",
        )
    passed = bool(
        set(COUNTRY_STATS_FIELDS) <= set(frame.columns)
        and frame["continent"].notna().all()
        and frame["region"].notna().all()
    )
    return CountryStatsCheckResult(
        passed=passed,
        row_count=len(frame.index),
        checked_columns=list(COUNTRY_STATS_FIELDS),
        message="country stats table looks usable" if passed else "missing continent or region values",
    )


def write_check_result(result: CountryStatsCheckResult, output_path: Path) -> None:
    _ensure_parent_dir(output_path)
    output_path.write_text(
        json.dumps(asdict(result), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_population_change_model(frame: pd.DataFrame) -> PopulationChangeModel:
    if frame.empty:
        raise ValueError("cannot build a model from an empty country stats table")

    data = frame.dropna(subset=["continent", "pop_change"]).copy()
    dummies = pd.get_dummies(data["continent"], dtype=float)
    targets = data["pop_change"].to_numpy(dtype=float)

    design_with_bias = np.column_stack(
        [np.ones(len(data.index), dtype=float), dummies.to_numpy(dtype=float)]
    )
    weights, _, _, _ = np.linalg.lstsq(design_with_bias, targets, rcond=None)

    predictions = design_with_bias @ weights
    residuals = targets - predictions
    residual_sum = float(np.sum(residuals ** 2))
    total_sum = float(np.sum((targets - np.mean(targets)) ** 2))
    r2_score = 1.0 if math.isclose(total_sum, 0.0) else 1.0 - (residual_sum / total_sum)
    rmse = float(np.sqrt(np.mean(residuals ** 2)))
    mae = float(np.mean(np.abs(residuals)))

    return PopulationChangeModel(
        intercept=_round4(weights[0]),
        coefficients=[
            ContinentCoefficient(
                continent=str(continent),
                coefficient=_round4(weights[index + 1]),
            )
            for index, continent in enumerate(dummies.columns.tolist())
        ],
        r2_score=_round4(r2_score),
        rmse=_round4(rmse),
        mae=_round4(mae),
        num_training_samples=len(data.index),
    )


def write_population_change_model(model: PopulationChangeModel, output_path: Path) -> None:
    _ensure_parent_dir(output_path)
    output_path.write_text(
        json.dumps(asdict(model), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_population_change_model(input_path: Path) -> PopulationChangeModel:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    missing = [key for key in MODEL_REQUIRED_KEYS if key not in payload]
    if missing:
        raise ValueError(f"model file is missing keys: {missing}")
    return PopulationChangeModel(
        intercept=float(payload["intercept"]),
        coefficients=[
            ContinentCoefficient(
                continent=item["continent"],
                coefficient=float(item["coefficient"]),
            )
            for item in payload["coefficients"]
        ],
        r2_score=float(payload["r2_score"]),
        rmse=float(payload["rmse"]),
        mae=float(payload["mae"]),
        num_training_samples=int(payload["num_training_samples"]),
    )


def build_continent_stats_frame(
    frame: pd.DataFrame,
    model: PopulationChangeModel,
) -> pd.DataFrame:
    coefficients = {
        item.continent: item.coefficient
        for item in model.coefficients
    }
    grouped = (
        frame.groupby("continent", dropna=False)[["pop_2022", "pop_2023", "pop_change"]]
        .sum(numeric_only=True)
        .reset_index()
    )
    grouped["pop_change"] = grouped["pop_change"].round(4)
    grouped["pop_change_factor"] = (
        grouped["continent"].map(coefficients).fillna(0.0).astype(float).round(4)
    )
    return grouped[list(CONTINENT_STATS_FIELDS)]


def write_continent_stats_csv(frame: pd.DataFrame, output_path: Path) -> None:
    _ensure_parent_dir(output_path)
    frame.to_csv(output_path, index=False)


def read_continent_stats_frame(input_path: Path) -> pd.DataFrame:
    return pd.read_csv(input_path)


def run_country_stats(*, source: str, output_path: Path) -> None:
    frame = load_country_stats_frame(source)
    write_country_stats_csv(frame, output_path)


def run_check_country_stats(*, input_path: Path, output_path: Path) -> None:
    frame = read_country_stats_frame(input_path)
    result = check_country_stats_frame(frame)
    write_check_result(result, output_path)


def run_change_model(*, input_path: Path, output_path: Path) -> None:
    frame = read_country_stats_frame(input_path)
    model = build_population_change_model(frame)
    write_population_change_model(model, output_path)


def run_continent_stats(*, input_path: Path, model_path: Path, output_path: Path) -> None:
    frame = read_country_stats_frame(input_path)
    model = read_population_change_model(model_path)
    continent_frame = build_continent_stats_frame(frame, model)
    write_continent_stats_csv(continent_frame, output_path)


__all__ = [
    "COUNTRY_STATS_SOURCE_URL",
    "ContinentCoefficient",
    "CountryStatsCheckResult",
    "PopulationChangeModel",
    "build_continent_stats_frame",
    "build_population_change_model",
    "check_country_stats_frame",
    "load_country_stats_frame",
    "read_continent_stats_frame",
    "read_country_stats_frame",
    "read_population_change_model",
    "run_change_model",
    "run_check_country_stats",
    "run_continent_stats",
    "run_country_stats",
    "write_check_result",
    "write_continent_stats_csv",
    "write_country_stats_csv",
    "write_population_change_model",
]
