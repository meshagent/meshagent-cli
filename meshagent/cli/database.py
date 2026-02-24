from pydantic import ValidationError
import json as _json
from typing import Annotated, Optional, List, Any
from urllib.parse import urlparse
from urllib.request import urlopen

import typer
from rich import print

from meshagent.api import RequiredTable
from meshagent.agents.agent import install_required_table

from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli import async_typer
from meshagent.cli.helper import resolve_project_id, resolve_room, get_client
from meshagent.api.helpers import websocket_room_url
from meshagent.api import RoomClient, WebSocketClientProtocol
from meshagent.api.room_server_client import _data_type_adapter, SqlTableReference
from meshagent.api.sql import ALLOWED_DATA_TYPES, SchemaParseError, parse_table_schema
from meshagent.api import RoomException  # or wherever you defined it

app = async_typer.AsyncTyper(help="Manage database tables in a room")

COLUMN_DEFINITIONS_HELP = (
    "Comma-separated column definitions. Example: "
    '"names vector(20) null, tags list(text), meta struct(owner text, score float)". '
    f"Allowed types: {', '.join(ALLOWED_DATA_TYPES)}. "
    "Vector syntax: vector(size[, element_type]). "
    "List syntax: list(element_type). "
    "Struct syntax: struct(field_name type[, ...])."
)


# ---------------------------
# Helpers
# ---------------------------


def _parse_json_arg(json_str: Optional[str], *, name: str) -> Any:
    if json_str is None:
        return None
    try:
        return _json.loads(json_str)
    except Exception as e:
        raise typer.BadParameter(f"Invalid JSON for {name}: {e}")


def _load_json_file(path: Optional[str], *, name: str) -> Any:
    """
    Load JSON from a local file path or an HTTP(S) URL.
    """
    if path is None:
        return None

    try:
        parsed = urlparse(path)

        # URL case
        if parsed.scheme in ("http", "https"):
            with urlopen(path) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                data = resp.read().decode(charset)
                return _json.loads(data)

        # Local file case
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)

    except Exception as e:
        raise typer.BadParameter(f"Unable to read {name} from {path}: {e}")


def _ns(namespace: Optional[List[str]]) -> Optional[List[str]]:
    return namespace or None


NamespaceOption = Annotated[
    Optional[List[str]],
    typer.Option(
        "--namespace",
        "-n",
        help="Namespace path segments (repeatable). Example: -n prod -n analytics",
    ),
]


# ---------------------------
# Commands
# ---------------------------


@app.async_command("tables", help="List database tables in a room.")
async def list_tables(
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
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            tables = await client.database.list_tables(namespace=_ns(namespace))
            if not tables:
                print("[bold yellow]No tables found.[/bold yellow]")
            else:
                for t in tables:
                    print(t)

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("inspect", help="Inspect a table schema in a room database.")
async def inspect(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
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
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            schema = await client.database.inspect(
                table=table, namespace=_ns(namespace)
            )

            if json:
                # schema values are DataType objects; they expose to_json()
                out = {k: v.to_json() for k, v in schema.items()}
                print(_json.dumps(out, indent=2))
            else:
                print(f"[bold]{table}[/bold]")
                for k, v in schema.items():
                    print(f"  [cyan]{k}[/cyan]: {v.to_json()}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command(
    "install", help="Install required tables from a requirements JSON file."
)
async def install_requirements(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    file: Annotated[
        Optional[str], typer.Option("--file", help="Path to requirements JSON file")
    ] = None,
):
    """
    Create a database from a json file containing a list of RequiredTables.
    """
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )

        requirements = _load_json_file(file, name="--file")

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            for rt in requirements["tables"]:
                rt = RequiredTable.from_json(rt)
                print(f"installing table {rt.name} in namespace {rt.namespace}")
                await install_required_table(room=client, table=rt)

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command(
    "create", help="Create a room database table with optional schema and seed data."
)
async def create_table(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    mode: Annotated[
        str, typer.Option("--mode", help="create | overwrite | create_if_not_exists")
    ] = "create",
    namespace: NamespaceOption = None,
    columns: Annotated[
        Optional[str],
        typer.Option(
            "--columns",
            "-c",
            help=COLUMN_DEFINITIONS_HELP,
        ),
    ] = None,
    schema_json: Annotated[
        Optional[str], typer.Option("--schema-json", help="Schema JSON as a string")
    ] = None,
    schema_file: Annotated[
        Optional[str], typer.Option("--schema-file", help="Path to schema JSON file")
    ] = None,
    data_json: Annotated[
        Optional[str], typer.Option("--data-json", help="Initial rows (JSON list)")
    ] = None,
    data_file: Annotated[
        Optional[str],
        typer.Option("--data-file", help="Path to JSON file with initial rows"),
    ] = None,
):
    """
    Create a table with optional schema + optional initial data.

    Schema JSON format matches your DataType.to_json() structure, e.g.:
      {"id":{"type":"int"}, "body":{"type":"text"}, "embedding":{"type":"vector","size":1536,"element_type":{"type":"float"}}}

    Column definitions via --columns/-c use SQL-like syntax:
      names vector(20) null, tags list(text), meta struct(owner text, score float)

    Allowed types: int, bool, date, timestamp, float, text, binary, vector, list, struct.
    Vector syntax: vector(size[, element_type]).
    List syntax: list(element_type).
    Struct syntax: struct(field_name type[, ...]).
    """
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )

        if columns and (schema_json is not None or schema_file is not None):
            raise typer.BadParameter(
                "Use --columns or --schema-json/--schema-file, not both"
            )

        schema_obj = None
        if schema_json is not None:
            schema_obj = _parse_json_arg(schema_json, name="--schema-json")
        elif schema_file is not None:
            schema_obj = _load_json_file(schema_file, name="--schema-file")

        data_obj = _parse_json_arg(data_json, name="--data-json")
        data_obj = (
            data_obj
            if data_obj is not None
            else _load_json_file(data_file, name="--data-file")
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            # Build DataType objects from json if schema provided
            schema = None
            if columns is not None:
                try:
                    schema = parse_table_schema(columns)
                except SchemaParseError as e:
                    raise typer.BadParameter(str(e))
            elif schema_obj is not None:
                schema = {
                    k: _data_type_adapter.validate_python(v)
                    for k, v in schema_obj.items()
                }  # hacky but local import-safe

            if schema is not None:
                await client.database.create_table_with_schema(
                    name=table,
                    schema=schema,
                    data=data_obj,
                    mode=mode,  # type: ignore
                    namespace=_ns(namespace),
                )
            else:
                await client.database.create_table_from_data(
                    name=table,
                    data=data_obj,
                    mode=mode,  # type: ignore
                    namespace=_ns(namespace),
                )

            print(f"[bold green]Created table:[/bold green] {table}")

    except (RoomException, ValidationError) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("drop", help="Drop a room database table.")
async def drop_table(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
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
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            await client.database.drop_table(
                name=table, ignore_missing=ignore_missing, namespace=_ns(namespace)
            )
            print(f"[bold green]Dropped table:[/bold green] {table}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("add-columns", help="Add columns to a room database table.")
async def add_columns(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    columns: Annotated[
        Optional[str],
        typer.Option(
            "--columns",
            "-c",
            help=COLUMN_DEFINITIONS_HELP,
        ),
    ] = None,
    columns_json: Annotated[
        Optional[str],
        typer.Option(
            "--columns-json",
            help=(
                "JSON object of new columns. Supports DataType JSON including "
                "list/struct (e.g. "
                '\'{"meta":{"type":"struct","fields":{"owner":{"type":"text"}}}}\''
                ")."
            ),
        ),
    ] = None,
):
    """
    Add columns. JSON supports either:
      - DataType JSON: {"col":{"type":"text"}}
      - or server default SQL expr strings: {"col":"'default'"}

    Column definitions via --columns/-c use SQL-like syntax:
      names vector(20) null, tags list(text), meta struct(owner text, score float)

    Allowed types: int, bool, date, timestamp, float, text, binary, vector, list, struct.
    Vector syntax: vector(size[, element_type]).
    List syntax: list(element_type).
    Struct syntax: struct(field_name type[, ...]).
    """
    account_client = await get_client()
    try:
        if columns and columns_json:
            raise typer.BadParameter("Use --columns or --columns-json, not both")
        if columns is None and columns_json is None:
            raise typer.BadParameter("Provide --columns or --columns-json")

        cols_obj = _parse_json_arg(columns_json, name="--columns-json")
        if columns_json is not None and not isinstance(cols_obj, dict):
            raise typer.BadParameter("--columns-json must be a JSON object")

        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            if columns is not None:
                try:
                    new_cols = parse_table_schema(columns)
                except SchemaParseError as e:
                    raise typer.BadParameter(str(e))
            else:
                # Convert DataType json objects into DataType instances; pass strings through.
                new_cols = {}
                for k, v in cols_obj.items():
                    if isinstance(v, dict) and "type" in v:
                        new_cols[k] = _data_type_adapter.validate_python(v)
                    else:
                        new_cols[k] = v

            await client.database.add_columns(
                table=table, new_columns=new_cols, namespace=_ns(namespace)
            )
            print(f"[bold green]Added columns to[/bold green] {table}")

    except (RoomException, typer.BadParameter, ValidationError) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("drop-columns", help="Drop columns from a room database table.")
async def drop_columns(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    columns: Annotated[
        List[str],
        typer.Option(..., "--column", "-c", help="Column to drop (repeatable)"),
    ] = None,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            await client.database.drop_columns(
                table=table, columns=columns, namespace=_ns(namespace)
            )
            print(f"[bold green]Dropped columns from[/bold green] {table}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("insert", help="Insert records into a room database table.")
async def insert(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
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
        records = _parse_json_arg(json, name="--json")
        records = (
            records if records is not None else _load_json_file(file, name="--file")
        )
        if not isinstance(records, list):
            raise typer.BadParameter("insert expects a JSON list of records")

        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            await client.database.insert(
                table=table, records=records, namespace=_ns(namespace)
            )
            print(
                f"[bold green]Inserted[/bold green] {len(records)} record(s) into {table}"
            )

    except (RoomException, typer.BadParameter) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("merge", help="Upsert records into a room database table.")
async def merge(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    on: Annotated[str, typer.Option(..., "--on", help="Column to match for upsert")],
    namespace: NamespaceOption = None,
    json: Annotated[
        Optional[str], typer.Option("--json", help="JSON records (list)")
    ] = None,
    file: Annotated[
        Optional[str], typer.Option("--file", "-f", help="Path to JSON file (list)")
    ] = None,
):
    account_client = await get_client()
    try:
        records = _parse_json_arg(json, name="--json")
        records = (
            records if records is not None else _load_json_file(file, name="--file")
        )
        if not isinstance(records, list):
            raise typer.BadParameter("merge expects a JSON list of records")

        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            await client.database.merge(
                table=table, on=on, records=records, namespace=_ns(namespace)
            )
            print(
                f"[bold green]Merged[/bold green] {len(records)} record(s) into {table} on {on}"
            )

    except (RoomException, typer.BadParameter) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("update", help="Update rows in a room database table.")
async def update(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    where: Annotated[
        str, typer.Option(..., "--where", help='SQL WHERE clause, e.g. "id = 1"')
    ],
    namespace: NamespaceOption = None,
    values_json: Annotated[
        Optional[str],
        typer.Option("--values-json", help="JSON object of literal values"),
    ] = None,
    values_sql_json: Annotated[
        Optional[str],
        typer.Option("--values-sql-json", help="JSON object of SQL expressions"),
    ] = None,
):
    account_client = await get_client()
    try:
        values = _parse_json_arg(values_json, name="--values-json")
        values_sql = _parse_json_arg(values_sql_json, name="--values-sql-json")
        if values is None and values_sql is None:
            raise typer.BadParameter("Provide --values-json and/or --values-sql-json")

        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            await client.database.update(
                table=table,
                where=where,
                values=values,
                values_sql=values_sql,
                namespace=_ns(namespace),
            )
            print(f"[bold green]Updated[/bold green] {table} where {where}")

    except (RoomException, typer.BadParameter) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("delete", help="Delete rows from a room database table.")
async def delete(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    where: Annotated[str, typer.Option(..., "--where", help="SQL WHERE clause")],
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
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            await client.database.delete(
                table=table, where=where, namespace=_ns(namespace)
            )
            print(f"[bold green]Deleted[/bold green] from {table} where {where}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("search", help="Search rows in a room database table.")
async def search(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    text: Annotated[
        Optional[str], typer.Option("--text", help="Full-text query")
    ] = None,
    vector_json: Annotated[
        Optional[str], typer.Option("--vector-json", help="Vector JSON array")
    ] = None,
    where: Annotated[
        Optional[str], typer.Option("--where", help="SQL WHERE clause")
    ] = None,
    where_json: Annotated[
        Optional[str],
        typer.Option("--where-json", help="JSON object converted to equality ANDs"),
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
):
    account_client = await get_client()
    try:
        vec = _parse_json_arg(vector_json, name="--vector-json")
        if vec is not None and not isinstance(vec, list):
            raise typer.BadParameter("--vector-json must be a JSON array")
        wj = _parse_json_arg(where_json, name="--where-json")
        if wj is not None and not isinstance(wj, dict):
            raise typer.BadParameter("--where-json must be a JSON object")

        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            results = await client.database.search(
                table=table,
                text=text,
                vector=vec,
                where=(wj if wj is not None else where),
                select=list(select) if select else None,
                limit=limit,
                offset=offset,
                namespace=_ns(namespace),
            )
            print(_json.dumps(results, indent=2 if pretty else None))

    except (RoomException, typer.BadParameter) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("sql", help="Execute SQL against room database tables.")
async def sql(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    query: Annotated[
        str,
        typer.Option(
            ...,
            "--query",
            "-q",
            help="SQL query to execute",
        ),
    ],
    table: Annotated[
        Optional[List[str]],
        typer.Option(
            "--table",
            "-t",
            help="Table name to register in SQL context (repeatable)",
        ),
    ] = None,
    namespace: NamespaceOption = None,
    tables_json: Annotated[
        Optional[str],
        typer.Option(
            "--tables-json",
            help=(
                "JSON array of table refs "
                '(e.g. \'[{"name":"users","alias":"u","namespace":["prod"]}]\')'
            ),
        ),
    ] = None,
    tables_file: Annotated[
        Optional[str],
        typer.Option(
            "--tables-file",
            help="Path/URL to JSON array of table refs (same format as --tables-json)",
        ),
    ] = None,
    params_json: Annotated[
        Optional[str],
        typer.Option(
            "--params-json",
            help="JSON object of SQL parameters for DataFusion param binding",
        ),
    ] = None,
    params_file: Annotated[
        Optional[str],
        typer.Option(
            "--params-file",
            help="Path/URL to JSON object of SQL parameters",
        ),
    ] = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    """
    Execute SQL against one or more room database tables.

    You can pass table names with --table/-t, and optionally provide detailed table
    references with --tables-json/--tables-file for alias/namespace control.
    """
    account_client = await get_client()
    try:
        if query.strip() == "":
            raise typer.BadParameter("--query cannot be empty")

        if tables_json is not None and tables_file is not None:
            raise typer.BadParameter("Use --tables-json or --tables-file, not both")
        if params_json is not None and params_file is not None:
            raise typer.BadParameter("Use --params-json or --params-file, not both")

        tables_obj = _parse_json_arg(tables_json, name="--tables-json")
        tables_obj = (
            tables_obj
            if tables_obj is not None
            else _load_json_file(tables_file, name="--tables-file")
        )
        if tables_obj is not None and not isinstance(tables_obj, list):
            raise typer.BadParameter(
                "--tables-json/--tables-file must be a JSON array of table references"
            )

        params_obj = _parse_json_arg(params_json, name="--params-json")
        params_obj = (
            params_obj
            if params_obj is not None
            else _load_json_file(params_file, name="--params-file")
        )
        if params_obj is not None and not isinstance(params_obj, dict):
            raise typer.BadParameter(
                "--params-json/--params-file must be a JSON object"
            )

        resolved_namespace = _ns(namespace)
        table_refs: list[SqlTableReference] = []

        if table is not None:
            for table_name in table:
                table_refs.append(
                    SqlTableReference(name=table_name, namespace=resolved_namespace)
                )

        if tables_obj is not None:
            for idx, entry in enumerate(tables_obj):
                if not isinstance(entry, dict):
                    raise typer.BadParameter(
                        f"table reference at index {idx} must be a JSON object"
                    )

                table_ref_payload = dict(entry)
                if (
                    resolved_namespace is not None
                    and "namespace" not in table_ref_payload
                ):
                    table_ref_payload["namespace"] = resolved_namespace

                table_refs.append(SqlTableReference.model_validate(table_ref_payload))

        if len(table_refs) == 0:
            raise typer.BadParameter(
                "Provide at least one table via --table or --tables-json/--tables-file"
            )

        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            results = await client.database.sql(
                query=query,
                tables=table_refs,
                params=params_obj,
            )
            print(_json.dumps(results, indent=2 if pretty else None))

    except (RoomException, typer.BadParameter, ValidationError) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("optimize", help="Optimize a room database table.")
async def optimize(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
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
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            await client.database.optimize(table=table, namespace=_ns(namespace))
            print(f"[bold green]Optimized[/bold green] {table}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("versions", help="List versions for a room database table.")
async def list_versions(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            versions = await client.database.list_versions(
                table=table, namespace=_ns(namespace)
            )
            out = [v.model_dump(mode="json") for v in versions]
            print(_json.dumps(out, indent=2 if pretty else None))

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command(
    "checkout", help="Check out a room database table at a specific version."
)
async def checkout(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    version: Annotated[int, typer.Option(..., "--version", "-v", help="Table version")],
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
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            await client.database.checkout(
                table=table, version=version, namespace=_ns(namespace)
            )
            print(f"[bold green]Checked out[/bold green] {table} @ version {version}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command(
    "restore", help="Restore a room database table to a specific version."
)
async def restore(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    version: Annotated[int, typer.Option(..., "--version", "-v", help="Table version")],
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
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            await client.database.restore(
                table=table, version=version, namespace=_ns(namespace)
            )
            print(f"[bold green]Restored[/bold green] {table} to version {version}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("indexes", help="List indexes on a room database table.")
async def list_indexes(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            idxs = await client.database.list_indexes(
                table=table, namespace=_ns(namespace)
            )
            out = [i.model_dump(mode="json") for i in idxs]
            print(_json.dumps(out, indent=2 if pretty else None))

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("index-create", help="Create an index on a room database table.")
async def create_index(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    column: Annotated[str, typer.Option(..., "--column", "-c", help="Column name")],
    kind: Annotated[
        str, typer.Option(..., "--kind", help="vector | scalar | fts")
    ] = "scalar",
    replace: Annotated[
        Optional[bool],
        typer.Option(
            "--replace/--no-replace",
            help="Replace existing index if it already exists",
        ),
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
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            if kind == "vector":
                await client.database.create_vector_index(
                    table=table,
                    column=column,
                    replace=replace,
                    namespace=_ns(namespace),
                )
            elif kind == "scalar":
                await client.database.create_scalar_index(
                    table=table,
                    column=column,
                    replace=replace,
                    namespace=_ns(namespace),
                )
            elif kind in ("fts", "full_text", "full-text"):
                await client.database.create_full_text_search_index(
                    table=table,
                    column=column,
                    replace=replace,
                    namespace=_ns(namespace),
                )
            else:
                raise typer.BadParameter("--kind must be one of: vector, scalar, fts")

            print(f"[bold green]Created[/bold green] {kind} index on {table}.{column}")

    except (RoomException, typer.BadParameter) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("index-drop", help="Drop an index from a room database table.")
async def drop_index(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    name: Annotated[str, typer.Option(..., "--name", help="Index name")],
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
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        ) as client:
            await client.database.drop_index(
                table=table, name=name, namespace=_ns(namespace)
            )
            print(f"[bold green]Dropped index[/bold green] {name} on {table}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()
