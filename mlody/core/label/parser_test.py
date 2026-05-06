"""Tests for mlody.core.label.parser — parse_label() acceptance scenarios.

Every test class traces to a named requirement in
openspec/changes/mlody-label-parsing-3-parser/specs/label-parser/spec.md.
"""

from __future__ import annotations

import pytest

from mlody.core.label.errors import (
    AttributeParseError,
    EntityParseError,
    LabelParseError,
    WorkspaceParseError,
)
from mlody.core.label.parser import parse_label


class TestEmptyLabelRejection:
    """Requirement: parse_label raises LabelParseError on empty input."""

    def test_empty_string_raises_label_parse_error(self) -> None:
        # Scenario: Empty label raises LabelParseError
        with pytest.raises(LabelParseError):
            parse_label("")


class TestDisambiguationRule1:
    """Requirement: Rule 1 — pipe present splits workspace from entity+attr."""

    def test_pipe_with_empty_workspace_and_entity(self) -> None:
        # Scenario: "|//foo/bar" -> workspace=None, entity.path="foo/bar"
        result = parse_label("|//foo/bar")
        assert result.workspace is None
        assert result.entity is not None
        assert result.entity.path == "foo/bar"
        assert result.attribute_path is None

    def test_pipe_with_sha_like_workspace(self) -> None:
        result = parse_label("deadbeef|//foo/bar")
        assert result.workspace == "deadbeef"
        assert result.entity is not None
        assert result.entity.path == "foo/bar"

    def test_pipe_with_branch_workspace(self) -> None:
        result = parse_label("my-branch|//foo/bar")
        assert result.workspace == "my-branch"
        assert result.entity is not None
        assert result.entity.path == "foo/bar"

    def test_pipe_with_entity_and_attribute(self) -> None:
        # Scenario: Full label with all three parts
        result = parse_label("my-branch|//foo/bar:task-a'outputs.model")
        assert result.workspace == "my-branch"
        assert result.entity is not None
        assert result.entity.path == "foo/bar"
        assert result.entity.name == "task-a"
        assert result.attribute_path == ("outputs", "model")


class TestDisambiguationRule2:
    """Requirement: Rule 2 — no pipe, starts with '//' or '@'."""

    def test_double_slash_gives_entity_only(self) -> None:
        # Scenario: Rule 2 — no pipe, starts with "//"
        result = parse_label("//foo/bar")
        assert result.workspace is None
        assert result.entity is not None
        assert result.entity.path == "foo/bar"
        assert result.attribute_path is None

    def test_at_root_gives_entity_only(self) -> None:
        # Scenario: Rule 2 — no pipe, starts with "@"
        result = parse_label("@root//foo/bar")
        assert result.workspace is None
        assert result.entity is not None
        assert result.entity.root == "root"
        assert result.entity.path == "foo/bar"

    def test_double_slash_with_attribute(self) -> None:
        result = parse_label("//foo/bar'outputs.model")
        assert result.workspace is None
        assert result.entity is not None
        assert result.entity.path == "foo/bar"
        assert result.attribute_path == ("outputs", "model")


class TestDisambiguationRule3:
    """Requirement: Rule 3 — no pipe, no '//'/'@'."""

    def test_tick_prefix_gives_cwd_attribute(self) -> None:
        # Scenario: CWD attribute access (no workspace, no entity)
        result = parse_label("'info")
        assert result.workspace is None
        assert result.entity is None
        assert result.attribute_path == ("info",)

    def test_branch_tick_attr_gives_workspace_and_attribute(self) -> None:
        # Scenario: Rule 3 — with "'" -> workspace+attribute
        result = parse_label("my-branch'info")
        assert result.workspace == "my-branch"
        assert result.entity is None
        assert result.attribute_path == ("info",)

    def test_workspace_only_no_tick(self) -> None:
        # Scenario: Workspace-only label
        result = parse_label("my-branch")
        assert result.workspace == "my-branch"
        assert result.entity is None
        assert result.attribute_path is None


class TestWorkspaceQueryCapture:
    """Requirement: Workspace query is captured from workspace spec."""

    def test_workspace_query_captured(self) -> None:
        # Scenario: Workspace query captured
        result = parse_label("my-branch[git:author=mav]|//foo")
        assert result.workspace == "my-branch"
        assert result.workspace_query == "git:author=mav"
        assert result.entity is not None
        assert result.entity.path == "foo"

    def test_cwd_workspace_query_only(self) -> None:
        # Scenario: [bar]|entity — empty workspace name with query → CWD workspace
        result = parse_label("[bar]|@common//sandbox/...")
        assert result.workspace is None
        assert result.workspace_query == "bar"
        assert result.entity is not None
        assert result.entity.root == "common"
        assert result.entity.path == "sandbox"
        assert result.entity.wildcard is True

    def test_branch_with_entity_query(self) -> None:
        # Scenario: bar|entity[query] — branch workspace with entity query
        result = parse_label("bar|@common//sandbox/...[foo]")
        assert result.workspace == "bar"
        assert result.workspace_query is None
        assert result.entity is not None
        assert result.entity.root == "common"
        assert result.entity.path == "sandbox"
        assert result.entity.wildcard is True
        assert result.entity_query == "foo"

    def test_cwd_workspace_query_with_entity_query(self) -> None:
        # Scenario: [bar]|entity[foo] — both workspace query and entity query
        result = parse_label("[bar]|@common//sandbox/...[foo]")
        assert result.workspace is None
        assert result.workspace_query == "bar"
        assert result.entity is not None
        assert result.entity.root == "common"
        assert result.entity.path == "sandbox"
        assert result.entity.wildcard is True
        assert result.entity_query == "foo"

    def test_unclosed_workspace_bracket_raises(self) -> None:
        # Scenario: Unclosed workspace query raises WorkspaceParseError
        # Note: the "[" has no "|" after it so the bracket scan treats
        # "my-branch[git:author=mav" as workspace fragment
        with pytest.raises(WorkspaceParseError):
            parse_label("my-branch[git:author=mav|//foo")


class TestEntitySpecFull:
    """Requirement: Entity spec is fully parsed from entity fragment."""

    def test_root_path_and_name(self) -> None:
        # Scenario: Root, path, and name
        result = parse_label("@planning//foo/bar:task-a")
        assert result.entity is not None
        assert result.entity.root == "planning"
        assert result.entity.path == "foo/bar"
        assert result.entity.name == "task-a"
        assert result.entity.wildcard is False

    def test_root_and_path_no_name(self) -> None:
        result = parse_label("@planning//foo/bar")
        assert result.entity is not None
        assert result.entity.root == "planning"
        assert result.entity.path == "foo/bar"
        assert result.entity.name is None

    def test_path_and_name_no_root(self) -> None:
        result = parse_label("//foo/bar:task-a")
        assert result.entity is not None
        assert result.entity.root is None
        assert result.entity.path == "foo/bar"
        assert result.entity.name == "task-a"

    def test_path_only(self) -> None:
        result = parse_label("//foo/bar")
        assert result.entity is not None
        assert result.entity.path == "foo/bar"
        assert result.entity.name is None

    def test_at_root_without_double_slash_is_valid(self) -> None:
        # Scenario: Bare "@root" with no "//" is a valid root-only entity reference
        result = parse_label("@root-only")
        assert result.entity is not None
        assert result.entity.root == "root-only"
        assert result.entity.path is None
        assert result.entity.name is None
        assert result.entity.field_path is None

    def test_empty_path_raises(self) -> None:
        # Scenario: Empty path raises EntityParseError
        with pytest.raises(EntityParseError):
            parse_label("//")

    def test_empty_name_raises(self) -> None:
        # "//foo:" — colon present but name is empty
        with pytest.raises(EntityParseError):
            parse_label("//foo:")


class TestEntitySpecWildcard:
    """Requirement: Wildcard path is parsed from entity fragment."""

    def test_wildcard_strips_and_sets_flag(self) -> None:
        # Scenario: Wildcard path — //foo/... -> path="foo", wildcard=True
        result = parse_label("//foo/...")
        assert result.entity is not None
        assert result.entity.path == "foo"
        assert result.entity.wildcard is True

    def test_root_wildcard_no_path_allowed(self) -> None:
        # Scenario: //... is valid — wildcard with no path prefix means "search everywhere"
        result = parse_label("//...")
        assert result.entity is not None
        assert result.entity.path is None
        assert result.entity.wildcard is True

    def test_root_wildcard_with_root_name_and_attr_path(self) -> None:
        # Scenario: @common//...:downloader'outputs.model
        result = parse_label("@common//...:downloader'outputs.model")
        assert result.entity is not None
        assert result.entity.root == "common"
        assert result.entity.path is None
        assert result.entity.wildcard is True
        assert result.entity.name == "downloader"
        assert result.attribute_path == ("outputs", "model")


class TestEntitySpecQuery:
    """Requirement: Entity query is captured and stored on Label."""

    def test_entity_query_captured(self) -> None:
        # Scenario: Entity query captured
        result = parse_label("//foo/bar[kind=action]")
        assert result.entity_query == "kind=action"
        assert result.entity is not None
        assert result.entity.path == "foo/bar"

    def test_unclosed_entity_bracket_raises(self) -> None:
        # Scenario: Unclosed entity query raises EntityParseError
        with pytest.raises(EntityParseError):
            parse_label("//foo/bar[kind=action")

    def test_query_only_wildcard_entity_is_allowed(self) -> None:
        result = parse_label("//...:[@mlody _.kind == 'task']")
        assert result.entity is not None
        assert result.entity.wildcard is True
        assert result.entity.name is None
        assert result.entity_query == "@mlody _.kind == 'task'"
        assert result.format_inner() == "//...:[@mlody _.kind == 'task']"

    def test_query_only_wildcard_entity_with_field_path_is_allowed(self) -> None:
        result = parse_label('//...:[@mlody _.kind == "action"].sha')
        assert result.entity is not None
        assert result.entity.wildcard is True
        assert result.entity.name is None
        assert result.entity.field_path == ("sha",)
        assert result.entity_query == '@mlody _.kind == "action"'
        assert result.format_inner() == '//...:[@mlody _.kind == "action"].sha'


class TestAttributePath:
    """Requirement: Attribute path is parsed after the tick (')."""

    def test_single_segment(self) -> None:
        result = parse_label("'info")
        assert result.attribute_path == ("info",)

    def test_multi_segment(self) -> None:
        # Scenario: Multi-segment attribute path
        result = parse_label("'outputs.model")
        assert result.attribute_path == ("outputs", "model")

    def test_attribute_query_captured(self) -> None:
        # Scenario: Attribute query captured
        result = parse_label("'info[git:author=mav]")
        assert result.attribute_path == ("info",)
        assert result.attribute_query == "git:author=mav"

    def test_trailing_dot_raises(self) -> None:
        # Scenario: Trailing dot raises AttributeParseError
        with pytest.raises(AttributeParseError):
            parse_label("'outputs.")

    def test_unclosed_attribute_bracket_raises(self) -> None:
        # Scenario: Unclosed attribute query raises AttributeParseError
        with pytest.raises(AttributeParseError):
            parse_label("'info[git:author=mav")


class TestEntityFieldPath:
    """Requirement: Dot-separated suffix after entity name is split into field_path."""

    def test_bare_entity_name_has_no_field_path(self) -> None:
        # Scenario: :pretrain with no dots → name="pretrain", field_path=None
        result = parse_label("@lexica//diamond:pretrain")
        assert result.entity is not None
        assert result.entity.name == "pretrain"
        assert result.entity.field_path is None

    def test_dotted_entity_name_splits_into_field_path(self) -> None:
        # Scenario: :pretrain.outputs.backbone_weights
        # → name="pretrain", field_path=("outputs", "backbone_weights")
        result = parse_label("@lexica//diamond:pretrain.outputs.backbone_weights")
        assert result.entity is not None
        assert result.entity.name == "pretrain"
        assert result.entity.field_path == ("outputs", "backbone_weights")

    def test_single_dot_suffix_gives_one_element_field_path(self) -> None:
        # Scenario: :pretrain.outputs → name="pretrain", field_path=("outputs",)
        result = parse_label("@lexica//diamond:pretrain.outputs")
        assert result.entity is not None
        assert result.entity.name == "pretrain"
        assert result.entity.field_path == ("outputs",)

    def test_field_path_combined_with_tick_attribute_path(self) -> None:
        # Scenario: both entity field_path and tick attribute_path coexist
        result = parse_label("@lexica//diamond:pretrain.outputs'metadata")
        assert result.entity is not None
        assert result.entity.name == "pretrain"
        assert result.entity.field_path == ("outputs",)
        assert result.attribute_path == ("metadata",)

    def test_no_entity_name_has_no_field_path(self) -> None:
        # Scenario: entity without colon → name=None, field_path=None
        result = parse_label("@lexica//diamond")
        assert result.entity is not None
        assert result.entity.name is None
        assert result.entity.field_path is None

    def test_inline_bracket_in_field_path_does_not_raise(self) -> None:
        # Scenario: slice notation embedded mid-path, e.g. .valid[1:4].Bald
        # The bracket is NOT a trailing entity_query; it must parse without error
        # and land in the field_path tuple.
        result = parse_label("@pixelle//...:celebA-dataset-bald.valid[1:4].Bald")
        assert result.entity is not None
        assert result.entity.name == "celebA-dataset-bald"
        assert result.entity.field_path == ("valid[1:4]", "Bald")
        assert result.entity_query is None

    def test_trailing_bracket_still_parsed_as_entity_query(self) -> None:
        # Regression: a bracket at the very end of the entity spec is still
        # captured as entity_query (existing behaviour must not change).
        result = parse_label("@pixelle//...:celebA-dataset-bald.valid[1:4]")
        assert result.entity is not None
        assert result.entity.name == "celebA-dataset-bald"
        assert result.entity.field_path == ("valid",)
        assert result.entity_query == "1:4"

    def test_truly_unclosed_bracket_still_raises(self) -> None:
        with pytest.raises(EntityParseError):
            parse_label("//foo/bar[kind=action")


class TestExtractSqlQuery:
    """Requirement: Parse @sql dialect tag from entity query suffix.

    Traces to openspec/changes/value-source-query/specs/target-addressing/spec.md
    """

    def test_where_fragment_extracts_dialect_and_fragment(self) -> None:
        # Scenario: @sql prefix extracts dialect and fragment
        from mlody.core.label.parser import extract_sql_query

        result = parse_label("@root//pkg:name[@sql WHERE split='train']")
        assert result.entity_query == "@sql WHERE split='train'"
        assert extract_sql_query(result.entity_query) == ("duckdb", "WHERE split='train'")

    def test_select_fragment_extracts_dialect_and_fragment(self) -> None:
        # Scenario: @sql with SELECT fragment
        from mlody.core.label.parser import extract_sql_query

        result = parse_label("@root//pkg:name[@sql SELECT col1, col2]")
        assert extract_sql_query(result.entity_query) == ("duckdb", "SELECT col1, col2")

    def test_non_sql_bracket_content_returns_none(self) -> None:
        # Scenario: non-sql query suffix is not extracted
        from mlody.core.label.parser import extract_sql_query

        assert extract_sql_query("1") is None

    def test_none_entity_query_returns_none(self) -> None:
        # Scenario: None entity_query returns None
        from mlody.core.label.parser import extract_sql_query

        assert extract_sql_query(None) is None

    def test_at_sql_without_trailing_space_returns_none(self) -> None:
        # Scenario: @sql prefix without trailing space is not extracted
        from mlody.core.label.parser import extract_sql_query

        assert extract_sql_query("@sqlSELECT *") is None

    def test_existing_bracket_expression_unaffected(self) -> None:
        # Scenario: existing bracket expression on entity is preserved
        from mlody.core.label.parser import extract_sql_query

        result = parse_label("@root//pkg:name[1]")
        assert result.entity_query == "1"
        assert extract_sql_query("1") is None

    def test_label_without_bracket_has_none_entity_query(self) -> None:
        # Scenario: label without bracket suffix has None entity_query
        from mlody.core.label.parser import extract_sql_query

        result = parse_label("@root//pkg:name")
        assert result.entity_query is None
        assert extract_sql_query(None) is None
