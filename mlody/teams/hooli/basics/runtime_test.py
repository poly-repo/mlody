from __future__ import annotations

from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal

from mlody.teams.hooli.basics.runtime import (
    build_continent_stats_frame,
    build_population_change_model,
    check_country_stats_frame,
    read_continent_stats_frame,
    read_country_stats_frame,
    read_population_change_model,
    write_continent_stats_csv,
    write_population_change_model,
)

def _sample_source_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Country or territory": ["Freedonia", "Sylvania", "Elbonia"],
            "Population (1 July 2022)": ["1,000", "2,000", "3,000"],
            "Population (1 July 2023)": ["1,100", "2,040", "3,180"],
            "Change (%)": ["+10.0%", "+2.0%", "+6.0%"],
            "UN Continental Region[1]": ["Europe", "Europe", "Asia"],
            "UN Statistical Subregion[1]": [
                "Western Europe",
                "Western Europe",
                "Central Asia",
            ],
        }
    )


def test_read_country_stats_frame_normalizes_source_columns(tmp_path: Path) -> None:
    source_path = tmp_path / "country_stats.csv"
    _sample_source_frame().to_csv(source_path, index=False)
    frame = read_country_stats_frame(source_path)
    assert frame["country"].tolist() == ["Freedonia", "Sylvania", "Elbonia"]
    assert frame.iloc[0]["pop_2022"] == 1000
    assert frame.iloc[0]["pop_2023"] == 1100
    assert frame.iloc[0]["pop_change"] == 10.0


def test_check_country_stats_frame_reports_success(tmp_path: Path) -> None:
    source_path = tmp_path / "country_stats.csv"
    _sample_source_frame().to_csv(source_path, index=False)
    frame = read_country_stats_frame(source_path)
    result = check_country_stats_frame(frame)
    assert result.passed is True
    assert result.row_count == 3
    assert "country" in result.checked_columns


def test_model_and_continent_stats_round_trip(tmp_path: Path) -> None:
    source_path = tmp_path / "country_stats.csv"
    _sample_source_frame().to_csv(source_path, index=False)
    frame = read_country_stats_frame(source_path)

    model_path = tmp_path / "change_model.json"
    continent_stats_path = tmp_path / "continent_stats.csv"

    model = build_population_change_model(frame)
    assert model.num_training_samples == 3
    assert {item.continent for item in model.coefficients} == {"Asia", "Europe"}
    assert model.rmse >= 0.0

    write_population_change_model(model, model_path)
    loaded_model = read_population_change_model(model_path)
    assert loaded_model == model

    continent_stats = build_continent_stats_frame(frame, loaded_model)
    assert continent_stats["continent"].tolist() == ["Asia", "Europe"]
    assert continent_stats.iloc[1]["pop_2022"] == 3000
    assert continent_stats.iloc[1]["pop_2023"] == 3140

    write_continent_stats_csv(continent_stats, continent_stats_path)
    assert_frame_equal(read_continent_stats_frame(continent_stats_path), continent_stats)
