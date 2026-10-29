from meshagent.cli.testing import CliRunner
from datetime import datetime, timezone
import json
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import zipfile

from meshagent.cli.dataset import (
    _import_source,
    _sql_display_records,
    _write_sql_query_output,
    app,
)


def test_datasets_help_groups_branch_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "branch" in result.output
    assert "branch-create" not in result.output
    assert "branch-delete" not in result.output


def test_datasets_branch_help_lists_branch_subcommands() -> None:
    result = CliRunner().invoke(app, ["branch", "--help"])

    assert result.exit_code == 0
    assert "list" in result.output
    assert "create" in result.output
    assert "delete" in result.output


def test_datasets_sql_help_lists_output_format_options() -> None:
    result = CliRunner().invoke(app, ["sql", "--help"])

    assert result.exit_code == 0
    assert "--format" in result.output
    assert "excel" in result.output
    assert "--output" in result.output


def test_datasets_import_help_lists_formats_and_modes() -> None:
    result = CliRunner().invoke(app, ["import", "--help"])

    assert result.exit_code == 0
    assert "--format" in result.output
    assert "parquet" in result.output
    assert "excel" in result.output
    assert "--mode" in result.output
    assert "merge" in result.output


async def _single_table_stream(table: pa.Table):
    yield table


async def _collect_tables(source) -> list[pa.Table]:
    tables = list[pa.Table]()
    async for batch in source.batches:
        if isinstance(batch, pa.RecordBatch):
            tables.append(pa.Table.from_batches([batch]))
        else:
            tables.append(batch)
    return tables


@pytest.mark.asyncio
async def test_write_sql_query_output_arrow_preserves_typed_table(tmp_path) -> None:
    table = pa.table(
        {
            "url": ["https://example.com"],
            "images": [[{"src": "image.jpg", "alt": "Example"}]],
        }
    )
    output = tmp_path / "result.arrow"

    await _write_sql_query_output(
        batches=_single_table_stream(table),
        schema=table.schema,
        output_format="arrow",
        output_path=str(output),
        pretty=False,
    )

    restored = pa.ipc.open_file(output).read_all()
    assert restored.equals(table)


@pytest.mark.asyncio
async def test_write_sql_query_output_parquet_preserves_typed_table(tmp_path) -> None:
    table = pa.table({"url": ["https://example.com"], "text": ["hello"]})
    output = tmp_path / "result.parquet"

    await _write_sql_query_output(
        batches=_single_table_stream(table),
        schema=table.schema,
        output_format="parquet",
        output_path=str(output),
        pretty=False,
    )

    restored = pq.read_table(output)
    assert restored.equals(table)


@pytest.mark.asyncio
async def test_write_sql_query_output_json_decodes_utf8_binary(tmp_path) -> None:
    table = pa.table({"data": [b'{"type":"meshagent.agent.turn.start"}']})
    output = tmp_path / "result.json"

    await _write_sql_query_output(
        batches=_single_table_stream(table),
        schema=table.schema,
        output_format="json",
        output_path=str(output),
        pretty=False,
    )

    assert json.loads(output.read_text(encoding="utf-8")) == [
        {"data": '{"type":"meshagent.agent.turn.start"}'}
    ]


@pytest.mark.asyncio
async def test_write_sql_query_output_json_decodes_arrow_json_extension(
    tmp_path,
) -> None:
    schema = pa.schema(
        [
            pa.field(
                "data",
                pa.string(),
                metadata={b"ARROW:extension:name": b"arrow.json"},
            )
        ]
    )
    table = pa.Table.from_arrays(
        [pa.array(['{"type":"meshagent.agent.turn.start","sequence":1}'])],
        schema=schema,
    )
    output = tmp_path / "result.json"

    await _write_sql_query_output(
        batches=_single_table_stream(table),
        schema=table.schema,
        output_format="json",
        output_path=str(output),
        pretty=False,
    )

    assert json.loads(output.read_text(encoding="utf-8")) == [
        {"data": {"type": "meshagent.agent.turn.start", "sequence": 1}}
    ]


def test_sql_display_records_decodes_arrow_json_extension() -> None:
    schema = pa.schema(
        [
            pa.field(
                "data",
                pa.string(),
                metadata={b"ARROW:extension:name": b"arrow.json"},
            )
        ]
    )
    table = pa.Table.from_arrays(
        [pa.array(['{"type":"meshagent.agent.turn.start","sequence":1}'])],
        schema=schema,
    )

    assert _sql_display_records(table) == [
        {"data": {"type": "meshagent.agent.turn.start", "sequence": 1}}
    ]


def test_sql_display_records_serializes_datetimes_for_json_output() -> None:
    table = pa.table({"timestamp": [datetime(2026, 5, 9, 18, 30, tzinfo=timezone.utc)]})

    assert _sql_display_records(table) == [{"timestamp": "2026-05-09T18:30:00+00:00"}]


@pytest.mark.asyncio
async def test_write_sql_query_output_json_encodes_non_utf8_binary(tmp_path) -> None:
    table = pa.table({"data": [b"\xff\x00"]})
    output = tmp_path / "result.json"

    await _write_sql_query_output(
        batches=_single_table_stream(table),
        schema=table.schema,
        output_format="json",
        output_path=str(output),
        pretty=False,
    )

    assert json.loads(output.read_text(encoding="utf-8")) == [
        {"data": {"base64": "/wA="}}
    ]


@pytest.mark.asyncio
async def test_write_sql_query_output_csv_stringifies_nested_columns(tmp_path) -> None:
    table = pa.table(
        {
            "url": ["https://example.com"],
            "images": [[{"src": "image.jpg", "alt": "Example"}]],
        }
    )
    output = tmp_path / "result.csv"

    await _write_sql_query_output(
        batches=_single_table_stream(table),
        schema=table.schema,
        output_format="csv",
        output_path=str(output),
        pretty=False,
    )

    text = output.read_text(encoding="utf-8")
    assert text.splitlines()[0] == '"url","images"'
    assert (
        '"https://example.com","[{""alt"":""Example"",""src"":""image.jpg""}]"' in text
    )


@pytest.mark.asyncio
async def test_write_sql_query_output_excel_writes_xlsx(tmp_path) -> None:
    table = pa.table(
        {
            "url": ["https://example.com"],
            "images": [[{"src": "image.jpg", "alt": "Example"}]],
            "long": ["x" * 40000],
        }
    )
    output = tmp_path / "result.xlsx"

    await _write_sql_query_output(
        batches=_single_table_stream(table),
        schema=table.schema,
        output_format="excel",
        output_path=str(output),
        pretty=False,
    )

    with zipfile.ZipFile(output) as workbook:
        sheet_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")

    assert "<t>url</t>" in sheet_xml
    assert "<t>images</t>" in sheet_xml
    assert "<t>https://example.com</t>" in sheet_xml
    image_cell = next(
        cell.split("</t>", 1)[0]
        for cell in sheet_xml.split("<t>")
        if "image.jpg" in cell
    )
    assert json.loads(image_cell) == [{"src": "image.jpg", "alt": "Example"}]
    assert f"<t>{'x' * 32767}</t>" in sheet_xml
    assert f"<t>{'x' * 32768}</t>" not in sheet_xml


@pytest.mark.asyncio
async def test_import_source_streams_json_array_in_batches(tmp_path) -> None:
    path = tmp_path / "records.json"
    path.write_text(
        '[{"id":1,"name":"a"},{"id":2,"name":"b"},{"id":3,"name":"c"}]',
        encoding="utf-8",
    )

    source = _import_source(path=str(path), import_format="json", batch_size=2)
    tables = await _collect_tables(source)

    assert source.schema is None
    assert [table.num_rows for table in tables] == [2, 1]
    assert pa.concat_tables(tables).to_pylist() == [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
        {"id": 3, "name": "c"},
    ]


@pytest.mark.asyncio
async def test_import_source_streams_json_lines_in_batches(tmp_path) -> None:
    path = tmp_path / "records.json"
    path.write_text(
        '{"id":1,"name":"a"}\n{"id":2,"name":"b"}\n{"id":3,"name":"c"}\n',
        encoding="utf-8",
    )

    source = _import_source(path=str(path), import_format="json", batch_size=2)
    tables = await _collect_tables(source)

    assert source.schema is None
    assert [table.num_rows for table in tables] == [2, 1]
    assert pa.concat_tables(tables).to_pylist() == [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
        {"id": 3, "name": "c"},
    ]


@pytest.mark.asyncio
async def test_import_source_streams_arrow_batches(tmp_path) -> None:
    table = pa.table({"id": [1, 2], "name": ["a", "b"]})
    path = tmp_path / "records.arrow"
    with pa.ipc.new_file(path, table.schema) as writer:
        writer.write_table(table)

    source = _import_source(path=str(path), import_format="auto")
    tables = await _collect_tables(source)

    assert source.schema == table.schema
    assert pa.concat_tables(tables).equals(table)


@pytest.mark.asyncio
async def test_import_source_streams_csv_batches(tmp_path) -> None:
    path = tmp_path / "records.csv"
    path.write_text("id,name\n1,a\n2,b\n", encoding="utf-8")

    source = _import_source(path=str(path), import_format="csv")
    tables = await _collect_tables(source)

    assert [field.name for field in source.schema] == ["id", "name"]
    assert pa.concat_tables(tables).to_pylist() == [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
    ]


@pytest.mark.asyncio
async def test_import_source_streams_parquet_batches(tmp_path) -> None:
    table = pa.table({"id": [1, 2, 3], "name": ["a", "b", "c"]})
    path = tmp_path / "records.parquet"
    pq.write_table(table, path)

    source = _import_source(path=str(path), import_format="auto", batch_size=2)
    tables = await _collect_tables(source)

    assert source.schema == table.schema
    assert [table.num_rows for table in tables] == [2, 1]
    assert pa.concat_tables(tables).equals(table)


@pytest.mark.asyncio
async def test_import_source_streams_excel_rows(tmp_path) -> None:
    table = pa.table({"id": [1, 2], "name": ["a", "b"]})
    path = tmp_path / "records.xlsx"
    await _write_sql_query_output(
        batches=_single_table_stream(table),
        schema=table.schema,
        output_format="excel",
        output_path=str(path),
        pretty=False,
    )

    source = _import_source(path=str(path), import_format="excel", batch_size=1)
    tables = await _collect_tables(source)

    assert source.schema is None
    assert [table.num_rows for table in tables] == [1, 1]
    assert pa.concat_tables(tables).to_pylist() == [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
    ]


def test_external_dataset_connection_help():
    result = CliRunner().invoke(app, ["connection", "--help"])
    assert result.exit_code == 0
    for command in ("list", "create", "update", "delete"):
        assert command in result.output
    result = CliRunner().invoke(app, ["register", "--help"])
    assert result.exit_code == 0
    assert "--connection-id" in result.output
    assert "--path" in result.output


@pytest.mark.asyncio
async def test_connection_create_never_prints_credentials(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    import typer
    from types import SimpleNamespace
    from meshagent.api.storage_connections import StorageConnection
    from meshagent.cli import dataset

    printed = []
    monkeypatch.setattr(dataset, "print", lambda value: printed.append(str(value)))
    connection_id = "7bb11459-c29b-4a54-9bae-a85bbf6c1d70"
    room_id = "2081d3e0-07ba-49db-a355-d617747abc2f"
    payload = {
        "name": "analytics",
        "config": {"provider": "s3", "bucket": "test-bucket", "region": "us-east-1"},
        "credentials": {
            "provider": "s3",
            "access_key_id": "test-key",
            "secret_access_key": "never-print-this",
        },
    }
    file = tmp_path / "connection.json"
    file.write_text(json.dumps(payload))
    result_model = StorageConnection.model_validate(
        {
            "id": connection_id,
            "room_id": room_id,
            "name": "analytics",
            "config": payload["config"],
            "revision": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )
    client = SimpleNamespace(
        create_room_storage_connection=AsyncMock(return_value=result_model),
        close=AsyncMock(),
    )
    monkeypatch.setattr(dataset, "get_client", AsyncMock(return_value=client))
    monkeypatch.setattr(
        dataset, "resolve_project_id", AsyncMock(return_value="project")
    )
    monkeypatch.setattr(dataset, "resolve_room", lambda room: room)
    await dataset.create_connection(project_id="project", room="room", file=str(file))
    assert connection_id in "\n".join(printed)
    assert "never-print-this" not in "\n".join(printed)
    assert (
        client.create_room_storage_connection.await_args.kwargs[
            "connection"
        ].credentials.secret_access_key
        == "never-print-this"
    )
    client.close.assert_awaited_once()
    payload["credentials"]["secret_access_key"] = {"secret": "never-print-this"}
    file.write_text(json.dumps(payload))
    with pytest.raises(typer.BadParameter) as error:
        await dataset.create_connection(
            project_id="project", room="room", file=str(file)
        )
    assert "never-print-this" not in str(error.value)
    assert client.create_room_storage_connection.await_count == 1
