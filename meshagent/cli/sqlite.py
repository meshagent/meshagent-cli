import json as _json
from typing import Annotated, Optional, List, Any

import pyarrow as pa
import typer
from pydantic import ValidationError
from rich import print

from meshagent.api import RoomClient, RoomException, WebSocketClientProtocol
from meshagent.api.helpers import websocket_room_url
from meshagent.api.room_server_client import SqliteSqlStatement
from meshagent.api.sql import SchemaParseError, parse_table_schema
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption, RoomOption
from meshagent.cli.dataset import (
    COLUMN_DEFINITIONS_HELP,
    DATASET_IMPORT_BATCH_SIZE,
    NamespaceOption,
    _coerce_record_list,
    _import_source,
    _load_json_file,
    _normalize_dataset_import_format,
    _normalize_sql_output_format,
    _ns,
    _parse_json_arg,
    _print_row_batches,
    _write_sql_query_output,
    _write_sql_statement_output,
)
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
)

app = async_typer.AsyncTyper(help="Manage SQLite databases and tables in a room")
database_app = async_typer.AsyncTyper(help="Manage SQLite databases in a room")
app.add_typer(database_app, name="database", help="Manage SQLite databases in a room")


SqliteImportMode = Annotated[
    str,
    typer.Option("--mode", help="Import mode: create, replace"),
]


def _normalize_sqlite_import_mode(value: str) -> str:
    normalized = value.lower()
    if normalized not in {"create", "replace"}:
        raise typer.BadParameter("--mode must be one of: create|replace")
    return normalized


async def _load_json_option(
    *,
    json_value: Optional[str],
    file_value: Optional[str],
    json_name: str,
    file_name: str,
) -> Any:
    if json_value is not None and file_value is not None:
        raise typer.BadParameter(f"Use {json_name} or {file_name}, not both")
    parsed = _parse_json_arg(json_value, name=json_name)
    return (
        parsed
        if parsed is not None
        else await _load_json_file(file_value, name=file_name)
    )


@database_app.async_command("list", help="List SQLite databases in a room namespace.")
async def list_databases(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    namespace: NamespaceOption = None,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            databases = await client.sqlite.list_databases(namespace=_ns(namespace))
            if not databases:
                print("[bold yellow]No databases found.[/bold yellow]")
            else:
                for database in databases:
                    print(database)
    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@database_app.async_command("create", help="Create a SQLite database in a room.")
async def create_database(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    mode: Annotated[
        str, typer.Option("--mode", help="create | overwrite | create_if_not_exists")
    ] = "create",
    namespace: NamespaceOption = None,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            await client.sqlite.create_database(
                name=database,
                mode=mode,
                namespace=_ns(namespace),
            )
            print(f"[bold green]Created database:[/bold green] {database}")
    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@database_app.async_command("drop", help="Drop a SQLite database in a room.")
async def drop_database(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    namespace: NamespaceOption = None,
    ignore_missing: Annotated[
        bool, typer.Option("--ignore-missing", help="Ignore missing database")
    ] = False,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            await client.sqlite.drop_database(
                name=database,
                ignore_missing=ignore_missing,
                namespace=_ns(namespace),
            )
            print(f"[bold green]Dropped database:[/bold green] {database}")
    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@database_app.async_command("inspect", help="Inspect a SQLite database in a room.")
async def inspect_database(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    namespace: NamespaceOption = None,
    o: OutputFormatOption = "table",
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            details = await client.sqlite.inspect_database(
                name=database,
                namespace=_ns(namespace),
            )
            payload = details.model_dump(mode="json")
            if o == "json":
                print(_json.dumps(payload, indent=2))
            else:
                print_json_table([payload], "name", "namespace", "tables", "size_bytes")
    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("tables", help="List SQLite tables in a room database.", hidden=True)
@app.async_command("table", help="List SQLite tables in a room database.")
async def list_tables(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    namespace: NamespaceOption = None,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            tables = await client.sqlite.list_tables(
                database=database,
                namespace=_ns(namespace),
            )
            if not tables:
                print("[bold yellow]No tables found.[/bold yellow]")
            else:
                for table in tables:
                    print(table)
    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("inspect", help="Inspect a SQLite table schema in a room database.")
async def inspect(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    json: Annotated[
        bool, typer.Option("--json", help="Output raw schema JSON")
    ] = False,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            schema = await client.sqlite.inspect(
                database=database,
                table=table,
                namespace=_ns(namespace),
            )
            if json:
                print(schema.to_string())
            else:
                print(f"[bold]{database}.{table}[/bold]")
                print(schema.to_string())
    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command(
    "create",
    help="Create a SQLite table with optional Arrow schema and seed data.",
)
async def create_table(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    mode: Annotated[
        str, typer.Option("--mode", help="create | overwrite | create_if_not_exists")
    ] = "create",
    namespace: NamespaceOption = None,
    columns: Annotated[
        Optional[str],
        typer.Option("--columns", "-c", help=COLUMN_DEFINITIONS_HELP),
    ] = None,
    data_json: Annotated[
        Optional[str], typer.Option("--data-json", help="Initial rows (JSON list)")
    ] = None,
    data_file: Annotated[
        Optional[str],
        typer.Option("--data-file", help="Path to JSON file with initial rows"),
    ] = None,
):
    account_client = await get_client()
    try:
        data_obj = await _load_json_option(
            json_value=data_json,
            file_value=data_file,
            json_name="--data-json",
            file_name="--data-file",
        )
        arrow_schema: pa.Schema | None = None
        if columns is not None:
            try:
                arrow_schema = parse_table_schema(columns)
            except SchemaParseError as e:
                raise typer.BadParameter(str(e))

        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            if data_obj is not None:
                records = _coerce_record_list(value=data_obj, name="create")
                await client.sqlite.create_table_with_schema(
                    database=database,
                    name=table,
                    schema=arrow_schema,
                    data=(
                        pa.Table.from_pylist(records, schema=arrow_schema)
                        if arrow_schema is not None
                        else pa.Table.from_pylist(records)
                    ),
                    mode=mode,  # type: ignore[arg-type]
                    namespace=_ns(namespace),
                )
            elif arrow_schema is not None:
                await client.sqlite.create_table_with_schema(
                    database=database,
                    name=table,
                    schema=arrow_schema,
                    mode=mode,  # type: ignore[arg-type]
                    namespace=_ns(namespace),
                )
            else:
                raise typer.BadParameter(
                    "Provide --columns for an Arrow schema or --data-json/--data-file for JSON data"
                )
            print(f"[bold green]Created table:[/bold green] {database}.{table}")
    except (RoomException, typer.BadParameter, ValidationError) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command(
    "import",
    help="Import a local Arrow, CSV, TSV, Parquet, JSON, or Excel file into a SQLite table.",
)
async def import_table(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    file: Annotated[
        str,
        typer.Option(..., "--file", "-f", help="Local file to import"),
    ],
    mode: SqliteImportMode = "create",
    import_format: Annotated[
        str,
        typer.Option(
            "--format",
            help="Input format: auto, json, arrow, csv, tsv, parquet, excel",
        ),
    ] = "auto",
    sheet: Annotated[
        Optional[str], typer.Option("--sheet", help="Excel worksheet name")
    ] = None,
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            help="Rows per imported batch for Parquet, JSON, and Excel",
        ),
    ] = DATASET_IMPORT_BATCH_SIZE,
    namespace: NamespaceOption = None,
):
    account_client = await get_client()
    try:
        mode = _normalize_sqlite_import_mode(mode)
        import_format = _normalize_dataset_import_format(import_format)
        if batch_size <= 0:
            raise typer.BadParameter("--batch-size must be greater than zero")
        source = _import_source(
            path=file,
            import_format=import_format,
            sheet=sheet,
            batch_size=batch_size,
        )

        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            await client.sqlite.create_table_with_schema(
                database=database,
                name=table,
                schema=source.schema,
                data=source.batches,
                mode="overwrite" if mode == "replace" else "create",
                namespace=_ns(namespace),
            )
            action = "Replaced" if mode == "replace" else "Created"
            print(
                f"[bold green]{action} table[/bold green] {database}.{table} from {file}"
            )
    except (RoomException, typer.BadParameter, ValidationError, OSError, KeyError) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("drop", help="Drop a SQLite table.")
async def drop_table(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    ignore_missing: Annotated[
        bool, typer.Option("--ignore-missing", help="Ignore missing table")
    ] = False,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            await client.sqlite.drop_table(
                database=database,
                name=table,
                ignore_missing=ignore_missing,
                namespace=_ns(namespace),
            )
            print(f"[bold green]Dropped table:[/bold green] {database}.{table}")
    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("rename", help="Rename a SQLite table.")
async def rename_table(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    new_name: Annotated[str, typer.Option(..., "--new-name", help="New table name")],
    namespace: NamespaceOption = None,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            await client.sqlite.rename_table(
                database=database,
                name=table,
                new_name=new_name,
                namespace=_ns(namespace),
            )
            print(
                f"[bold green]Renamed table:[/bold green] {database}.{table} -> {new_name}"
            )
    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("add-columns", help="Add columns to a SQLite table.")
async def add_columns(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    columns: Annotated[
        str,
        typer.Option("--columns", "-c", help=COLUMN_DEFINITIONS_HELP),
    ],
    namespace: NamespaceOption = None,
):
    account_client = await get_client()
    try:
        try:
            new_cols = parse_table_schema(columns)
        except SchemaParseError as e:
            raise typer.BadParameter(str(e))

        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            await client.sqlite.add_columns(
                database=database,
                table=table,
                new_columns=new_cols,
                namespace=_ns(namespace),
            )
            print(f"[bold green]Added columns to[/bold green] {database}.{table}")
    except (RoomException, typer.BadParameter, ValidationError) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("drop-columns", help="Drop columns from a SQLite table.")
async def drop_columns(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    columns: Annotated[
        List[str],
        typer.Option(..., "--column", "-c", help="Column to drop (repeatable)"),
    ] = None,
    namespace: NamespaceOption = None,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            await client.sqlite.drop_columns(
                database=database,
                table=table,
                columns=columns,
                namespace=_ns(namespace),
            )
            print(f"[bold green]Dropped columns from[/bold green] {database}.{table}")
    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("insert", help="Insert records into a SQLite table.")
async def insert(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    json: Annotated[
        Optional[str], typer.Option("--json", help="JSON list of records")
    ] = None,
    file: Annotated[
        Optional[str],
        typer.Option("--file", "-f", help="Path to JSON file (list of records)"),
    ] = None,
):
    account_client = await get_client()
    try:
        records = await _load_json_option(
            json_value=json,
            file_value=file,
            json_name="--json",
            file_name="--file",
        )
        records = _coerce_record_list(value=records, name="insert")

        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            await client.sqlite.insert(
                database=database,
                table=table,
                records=records,
                namespace=_ns(namespace),
            )
            print(
                f"[bold green]Inserted[/bold green] {len(records)} record(s) into {database}.{table}"
            )
    except (RoomException, typer.BadParameter) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("update", help="Update rows in a SQLite table.")
async def update(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    where: Annotated[
        str, typer.Option(..., "--where", help='SQL WHERE clause, e.g. "id = ?"')
    ],
    values_json: Annotated[
        str,
        typer.Option("--values-json", help="JSON object of update values"),
    ],
    params_json: Annotated[
        Optional[str], typer.Option("--params-json", help="JSON SQL parameters")
    ] = None,
    namespace: NamespaceOption = None,
):
    account_client = await get_client()
    try:
        values = _parse_json_arg(values_json, name="--values-json")
        if not isinstance(values, dict):
            raise typer.BadParameter("--values-json must be a JSON object")
        params = _parse_json_arg(params_json, name="--params-json")

        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            rows = await client.sqlite.update(
                database=database,
                table=table,
                where=where,
                values=values,
                params=params,
                namespace=_ns(namespace),
            )
            print(
                f"[bold green]Updated[/bold green] {rows} row(s) in {database}.{table}"
            )
    except (RoomException, typer.BadParameter) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("delete", help="Delete rows from a SQLite table.")
async def delete(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    where: Annotated[str, typer.Option(..., "--where", help="SQL WHERE clause")],
    params_json: Annotated[
        Optional[str], typer.Option("--params-json", help="JSON SQL parameters")
    ] = None,
    namespace: NamespaceOption = None,
):
    account_client = await get_client()
    try:
        params = _parse_json_arg(params_json, name="--params-json")
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            rows = await client.sqlite.delete(
                database=database,
                table=table,
                where=where,
                params=params,
                namespace=_ns(namespace),
            )
            print(
                f"[bold green]Deleted[/bold green] {rows} row(s) from {database}.{table}"
            )
    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("search", help="Search rows in a SQLite table.")
async def search(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    where: Annotated[
        Optional[str], typer.Option("--where", help="SQL WHERE clause")
    ] = None,
    where_json: Annotated[
        Optional[str],
        typer.Option("--where-json", help="JSON object converted to equality ANDs"),
    ] = None,
    params_json: Annotated[
        Optional[str], typer.Option("--params-json", help="JSON SQL parameters")
    ] = None,
    select: Annotated[
        Optional[List[str]],
        typer.Option("--select", help="Columns to select (repeatable)"),
    ] = None,
    limit: Annotated[
        Optional[int], typer.Option("--limit", help="Max rows to return")
    ] = None,
    offset: Annotated[
        Optional[int], typer.Option("--offset", help="Rows to skip")
    ] = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
    namespace: NamespaceOption = None,
):
    account_client = await get_client()
    try:
        wj = _parse_json_arg(where_json, name="--where-json")
        if wj is not None and not isinstance(wj, dict):
            raise typer.BadParameter("--where-json must be a JSON object")
        params = _parse_json_arg(params_json, name="--params-json")

        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            await _print_row_batches(
                batches=client.sqlite.search_stream(
                    database=database,
                    table=table,
                    where=(wj if wj is not None else where),
                    params=params,
                    select=list(select) if select else None,
                    limit=limit,
                    offset=offset,
                    namespace=_ns(namespace),
                ),
                pretty=pretty,
            )
    except (RoomException, typer.BadParameter) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("count", help="Count rows in a SQLite table.")
async def count(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    where: Annotated[
        Optional[str], typer.Option("--where", help="SQL WHERE clause")
    ] = None,
    where_json: Annotated[
        Optional[str],
        typer.Option("--where-json", help="JSON object converted to equality ANDs"),
    ] = None,
    params_json: Annotated[
        Optional[str], typer.Option("--params-json", help="JSON SQL parameters")
    ] = None,
    namespace: NamespaceOption = None,
):
    account_client = await get_client()
    try:
        wj = _parse_json_arg(where_json, name="--where-json")
        if wj is not None and not isinstance(wj, dict):
            raise typer.BadParameter("--where-json must be a JSON object")
        params = _parse_json_arg(params_json, name="--params-json")
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            row_count = await client.sqlite.count(
                database=database,
                table=table,
                where=(wj if wj is not None else where),
                params=params,
                namespace=_ns(namespace),
            )
            print(row_count)
    except (RoomException, typer.BadParameter) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("sql", help="Execute SQL against a room SQLite database.")
async def sql(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    database: Annotated[
        str, typer.Option(..., "--database", "-d", help="Database name")
    ],
    query: Annotated[
        str,
        typer.Option(..., "--query", "-q", help="SQL query to execute"),
    ],
    params_json: Annotated[
        Optional[str], typer.Option("--params-json", help="JSON SQL parameters")
    ] = None,
    params_file: Annotated[
        Optional[str], typer.Option("--params-file", help="Path/URL to JSON parameters")
    ] = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
    output_format: Annotated[
        str,
        typer.Option(
            "--format",
            help="Output format: table, json, arrow, csv, tsv, parquet, excel",
        ),
    ] = "table",
    output: Annotated[
        Optional[str],
        typer.Option(
            "--output",
            "-o",
            help="Write output to this file path instead of stdout",
        ),
    ] = None,
    namespace: NamespaceOption = None,
):
    account_client = await get_client()
    try:
        if query.strip() == "":
            raise typer.BadParameter("--query cannot be empty")
        output_format = _normalize_sql_output_format(output_format)
        params = await _load_json_option(
            json_value=params_json,
            file_value=params_file,
            json_name="--params-json",
            file_name="--params-file",
        )

        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            result = await client.sqlite.execute_sql(
                database=database,
                query=query,
                params=params,
                namespace=_ns(namespace),
            )
            if isinstance(result, SqliteSqlStatement):
                _write_sql_statement_output(
                    rows_affected=result.rows_affected,
                    output_format=output_format,
                    output_path=output,
                    pretty=pretty,
                )
            else:
                try:
                    await _write_sql_query_output(
                        batches=client.sqlite.read_sql_query(query_id=result.query_id),
                        schema=result.schema,
                        output_format=output_format,
                        output_path=output,
                        pretty=pretty,
                    )
                finally:
                    await client.sqlite.close_sql_query(query_id=result.query_id)
    except (RoomException, typer.BadParameter, ValidationError) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()
