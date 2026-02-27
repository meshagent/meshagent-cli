import pytest

from meshagent.cli.memory import (
    _MemoryImportError,
    _entity_batch_statement,
    _entity_record_from_row,
    _parse_graph_query_response,
    _relationship_batch_statement,
    _relationship_record_from_row,
    _select_graph_name,
)


def test_select_graph_name_uses_requested_name() -> None:
    assert (
        _select_graph_name(
            graph_names=["default_db", "eli-memory"],
            requested_graph_name="default_db",
        )
        == "default_db"
    )


def test_select_graph_name_prefers_single_non_default() -> None:
    assert (
        _select_graph_name(
            graph_names=["default_db", "eli-memory"],
            requested_graph_name=None,
        )
        == "eli-memory"
    )


def test_select_graph_name_requires_flag_when_ambiguous() -> None:
    with pytest.raises(_MemoryImportError):
        _select_graph_name(
            graph_names=["default_db", "alpha", "beta"],
            requested_graph_name=None,
        )


def test_parse_graph_query_response_returns_rows() -> None:
    rows = _parse_graph_query_response(
        [
            ["column_a", "column_b"],
            [["value_a", 1], ["value_b", 2]],
            ["stats"],
        ]
    )

    assert rows == [["value_a", 1], ["value_b", 2]]


def test_parse_graph_query_response_rejects_invalid_payload() -> None:
    with pytest.raises(_MemoryImportError):
        _parse_graph_query_response({"columns": [], "rows": []})


def test_entity_record_from_row_maps_values() -> None:
    record = _entity_record_from_row(
        [
            "entity-1",
            "Entity One",
            "Topic",
            "Some context",
            "0.42",
            "2026-01-01T12:00:00Z",
            "2026-01-01T12:05:00Z",
        ]
    )

    assert record.entity_id == "entity-1"
    assert record.name == "Entity One"
    assert record.entity_type == "Topic"
    assert record.context == "Some context"
    assert record.confidence == 0.42
    assert record.created_at == "2026-01-01T12:00:00Z"
    assert record.valid_at == "2026-01-01T12:05:00Z"


def test_relationship_record_from_row_defaults_relationship_type() -> None:
    record = _relationship_record_from_row(
        [
            "source-1",
            "target-1",
            "",
            "description",
            None,
            "2026-01-01T12:00:00Z",
            "2026-01-01T12:01:00Z",
            "2026-01-10T12:00:00Z",
            "2026-01-11T12:00:00Z",
            "Source Name",
            "Target Name",
        ]
    )

    assert record.source_entity_id == "source-1"
    assert record.target_entity_id == "target-1"
    assert record.relationship_type == "RELATED_TO"
    assert record.description == "description"
    assert record.confidence is None
    assert record.created_at == "2026-01-01T12:00:00Z"
    assert record.valid_at == "2026-01-01T12:01:00Z"
    assert record.expired_at == "2026-01-10T12:00:00Z"
    assert record.invalid_at == "2026-01-11T12:00:00Z"


def test_entity_batch_statement_queries_all_nodes() -> None:
    statement = _entity_batch_statement(skip=10, limit=25)
    assert "MATCH (n) " in statement
    assert "MATCH (n:Entity) " not in statement
    assert "SKIP 10 LIMIT 25" in statement


def test_relationship_batch_statement_queries_all_edges() -> None:
    statement = _relationship_batch_statement(skip=5, limit=50)
    assert "MATCH (a)-[r]->(b) " in statement
    assert "MATCH (a:Entity)-[r]->(b:Entity) " not in statement
    assert "SKIP 5 LIMIT 50" in statement
