from click.testing import CliRunner
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import zipfile

from meshagent.cli import async_typer
from meshagent.cli.dataset import _import_source, _write_sql_query_output, app


def test_datasets_help_groups_branch_commands() -> None:
    result = CliRunner().invoke(async_typer.get_command(app), ["--help"])

    assert result.exit_code == 0
    assert "branch" in result.output
    assert "branch-create" not in result.output
    assert "branch-delete" not in result.output


def test_datasets_branch_help_lists_branch_subcommands() -> None:
    result = CliRunner().invoke(async_typer.get_command(app), ["branch", "--help"])

    assert result.exit_code == 0
    assert "list" in result.output
    assert "create" in result.output
    assert "delete" in result.output


def test_datasets_sql_help_lists_output_format_options() -> None:
    result = CliRunner().invoke(async_typer.get_command(app), ["sql", "--help"])

    assert result.exit_code == 0
    assert "--format" in result.output
    assert "excel" in result.output
    assert "--output" in result.output


def test_datasets_import_help_lists_formats_and_modes() -> None:
    result = CliRunner().invoke(async_typer.get_command(app), ["import", "--help"])

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
    assert '<t>[{"alt":"Example","src":"image.jpg"}]</t>' in sheet_xml
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
