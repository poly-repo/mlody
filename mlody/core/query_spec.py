"""Small shared query-spec types used across core and tabular code."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuerySpec:
    """A query fragment plus the dialect used to interpret it."""

    sql: str
    dialect: str = "duckdb"
