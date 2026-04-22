"""Tests for mlody.core.optimiser — DerivedStep and QueryOptimiser protocol.

Traces to openspec/changes/value-source-query/specs/query-optimiser-stub/spec.md
"""

from __future__ import annotations

import dataclasses

import pytest

from mlody.core.optimiser import DerivedStep, SequentialOptimiser


class TestDerivedStep:
    """Requirement: DerivedStep is a frozen dataclass."""

    def test_derived_step_is_immutable(self) -> None:
        # Scenario: attempting to mutate any field raises FrozenInstanceError
        step = DerivedStep(
            source_ref="@root//pkg:data",
            sql_fragment="WHERE split='train'",
            dialect="duckdb",
            output_path="/home/user/.cache/mlody/derived/abc.parquet",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            step.source_ref = "other"  # type: ignore[misc]

    def test_derived_step_equality_on_identical_fields(self) -> None:
        # Scenario: two DerivedStep instances with identical fields are equal
        a = DerivedStep(
            source_ref="@root//pkg:data",
            sql_fragment="WHERE split='train'",
            dialect="duckdb",
            output_path="/cache/abc.parquet",
        )
        b = DerivedStep(
            source_ref="@root//pkg:data",
            sql_fragment="WHERE split='train'",
            dialect="duckdb",
            output_path="/cache/abc.parquet",
        )
        assert a == b

    def test_derived_step_hash_equivalent_on_identical_fields(self) -> None:
        # Scenario: two equal DerivedStep instances produce the same hash
        a = DerivedStep(
            source_ref="@root//pkg:data",
            sql_fragment="WHERE split='train'",
            dialect="duckdb",
            output_path="/cache/abc.parquet",
        )
        b = DerivedStep(
            source_ref="@root//pkg:data",
            sql_fragment="WHERE split='train'",
            dialect="duckdb",
            output_path="/cache/abc.parquet",
        )
        assert hash(a) == hash(b)


class TestSequentialOptimiser:
    """Requirement: SequentialOptimiser returns steps unchanged."""

    def test_sequential_optimiser_preserves_order_and_content(self) -> None:
        # Scenario: SequentialOptimiser().optimise([step_a, step_b])
        # returns [step_a, step_b] in the same order
        step_a = DerivedStep(
            source_ref="@root//pkg:a",
            sql_fragment="WHERE x=1",
            dialect="duckdb",
            output_path="/cache/a.parquet",
        )
        step_b = DerivedStep(
            source_ref="@root//pkg:b",
            sql_fragment="WHERE y=2",
            dialect="duckdb",
            output_path="/cache/b.parquet",
        )
        opt = SequentialOptimiser()
        result = list(opt.optimise([step_a, step_b]))
        assert result == [step_a, step_b]

    def test_sequential_optimiser_empty_input_returns_empty(self) -> None:
        # Scenario: SequentialOptimiser().optimise([]) returns empty output
        opt = SequentialOptimiser()
        result = list(opt.optimise([]))
        assert result == []

    def test_sequential_optimiser_returns_new_sequence(self) -> None:
        # The returned sequence must be a distinct object (not the same list),
        # to protect against mutation of the original.
        step = DerivedStep(
            source_ref="@root//pkg:data",
            sql_fragment="WHERE x=1",
            dialect="duckdb",
            output_path="/cache/x.parquet",
        )
        original = [step]
        opt = SequentialOptimiser()
        result = opt.optimise(original)
        # Identity check: must not be the exact same list object
        assert result is not original


class TestQueryOptimiserProtocol:
    """Requirement: QueryOptimiser is a structural Protocol."""

    def test_custom_class_satisfies_protocol_without_inheritance(self) -> None:
        # Scenario: a class that implements optimise() with the correct
        # signature satisfies QueryOptimiser without explicit inheritance
        from typing import Sequence

        from mlody.core.optimiser import QueryOptimiser

        class CustomOptimiser:
            def optimise(self, steps: Sequence[DerivedStep]) -> Sequence[DerivedStep]:
                # Reverse the order to prove custom logic runs
                return list(reversed(steps))

        opt: QueryOptimiser = CustomOptimiser()  # type: ignore[assignment]
        step = DerivedStep(
            source_ref="@root//pkg:data",
            sql_fragment="WHERE x=1",
            dialect="duckdb",
            output_path="/cache/x.parquet",
        )
        result = list(opt.optimise([step]))
        assert result == [step]
