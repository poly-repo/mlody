from __future__ import annotations

from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal

from mlody.teams.hooli.basics.runtime import (
    build_continent_stats_frame,
    build_population_change_model,
    check_country_stats_frame,
    load_country_stats_frame,
    read_continent_stats_frame,
    read_country_stats_frame,
    read_population_change_model,
    write_continent_stats_csv,
    write_country_stats_csv,
    write_population_change_model,
)

_SAMPLE_HTML = """
<html>
  <body>
    <table>
      <tr>
        <th>Country</th>
        <th>Population (1 July 2022)</th>
        <th>Population (1 July 2023)</th>
        <th>Change</th>
        <th>UN Continental Region[1]</th>
        <th>UN Statistical Subregion[1]</th>
      </tr>
      <tr>
        <td>Freedonia</td>
        <td>1,000</td>
        <td>1,100</td>
        <td>10.0%</td>
        <td>Europe</td>
        <td>Western Europe</td>
      </tr>
      <tr>
        <td>Sylvania</td>
        <td>2,000</td>
        <td>2,040</td>
        <td>2.0%</td>
        <td>Europe</td>
        <td>Western Europe</td>
      </tr>
      <tr>
        <td>Elbonia</td>
        <td>3,000</td>
        <td>3,180</td>
        <td>6.0%</td>
        <td>Asia</td>
        <td>Central Asia</td>
      </tr>
    </table>
  </body>
</html>
"""


def test_load_country_stats_frame_extracts_rows(tmp_path: Path) -> None:
    source_path = tmp_path / "countries.html"
    source_path.write_text(_SAMPLE_HTML, encoding="utf-8")
    frame = load_country_stats_frame(str(source_path))
    assert frame["country"].tolist() == ["Freedonia", "Sylvania", "Elbonia"]
    assert frame.iloc[0]["pop_2022"] == 1000
    assert frame.iloc[0]["pop_2023"] == 1100
    assert frame.iloc[0]["pop_change"] == 10.0


def test_check_country_stats_frame_reports_success(tmp_path: Path) -> None:
    source_path = tmp_path / "countries.html"
    source_path.write_text(_SAMPLE_HTML, encoding="utf-8")
    frame = load_country_stats_frame(str(source_path))
    result = check_country_stats_frame(frame)
    assert result.passed is True
    assert result.row_count == 3
    assert "country" in result.checked_columns


def test_model_and_continent_stats_round_trip(tmp_path: Path) -> None:
    source_path = tmp_path / "countries.html"
    source_path.write_text(_SAMPLE_HTML, encoding="utf-8")
    frame = load_country_stats_frame(str(source_path))

    country_stats_path = tmp_path / "country_stats.csv"
    model_path = tmp_path / "change_model.json"
    continent_stats_path = tmp_path / "continent_stats.csv"

    write_country_stats_csv(frame, country_stats_path)
    loaded_frame = read_country_stats_frame(country_stats_path)
    assert_frame_equal(loaded_frame, frame)

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
