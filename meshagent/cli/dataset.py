from collections.abc import AsyncIterable, AsyncIterator
from pydantic import ValidationError
import json as _json
from typing import Annotated, Optional, List, Any
from urllib.parse import urlparse

import typer
import pyarrow as pa
from rich import print

from meshagent.api import RequiredTable
from meshagent.api.http import new_client_session
from meshagent.agents.agent import install_required_table

from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli import async_typer
from meshagent.cli.helper import resolve_project_id, resolve_room, get_client
from meshagent.api.helpers import websocket_room_url
from meshagent.api import RoomClient, WebSocketClientProtocol
from meshagent.api.room_server_client import DatasetSqlStatement, SqlTableReference
from meshagent.api.sql import ALLOWED_DATA_TYPES, SchemaParseError, parse_table_schema
from meshagent.api import RoomException  # or wherever you defined it

app = async_typer.AsyncTyper(help="Manage dataset tables in a room")
branch_app = async_typer.AsyncTyper(help="Manage dataset branches in a room namespace.")
app.add_typer(
    branch_app,
    name="branch",
    help="Manage dataset branches in a room namespace.",
)

COLUMN_DEFINITIONS_HELP = (
    "Comma-separated column definitions. Example: "
    '"names vector(20) null, tags list(text), meta struct(owner text, score float)". '
    f"CLI shorthand types: {', '.join(ALLOWED_DATA_TYPES)}. "
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


async def _load_json_file(path: Optional[str], *, name: str) -> Any:
    """
    Load JSON from a local file path or an HTTP(S) URL.
    """
    if path is None:
        return None

    try:
        parsed = urlparse(path)

        # URL case
        if parsed.scheme in ("http", "https"):
            async with new_client_session() as session:
                async with session.get(path) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status}: {body}")
                    charset = resp.charset or "utf-8"
                    data = (await resp.read()).decode(charset)
                return _json.loads(data)

        # Local file case
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)

    except Exception as e:
        raise typer.BadParameter(f"Unable to read {name} from {path}: {e}")


def _ns(namespace: Optional[List[str]]) -> Optional[List[str]]:
    return namespace or None


def _build_sql_table_refs(
    *,
    table: Optional[List[str]],
    tables_obj: Any,
    namespace: Optional[list[str]],
    branch: Optional[str],
    version: Optional[int],
) -> list[SqlTableReference]:
    table_refs: list[SqlTableReference] = []

    if table is not None:
        for table_name in table:
            table_refs.append(
                SqlTableReference(
                    name=table_name,
                    namespace=namespace,
                    branch=branch,
                    version=version,
                )
            )

    if tables_obj is not None:
        for idx, entry in enumerate(tables_obj):
            if not isinstance(entry, dict):
                raise typer.BadParameter(
                    f"table reference at index {idx} must be a JSON object"
                )

            table_ref_payload = dict(entry)
            if namespace is not None and "namespace" not in table_ref_payload:
                table_ref_payload["namespace"] = namespace
            if branch is not None and "branch" not in table_ref_payload:
                table_ref_payload["branch"] = branch
            if version is not None and "version" not in table_ref_payload:
                table_ref_payload["version"] = version

            table_refs.append(SqlTableReference.model_validate(table_ref_payload))

    return table_refs


def _sql_params_table(params_obj: Any) -> pa.Table | None:
    if params_obj is None:
        return None
    return pa.table({key: [value] for key, value in params_obj.items()})


def _coerce_record_list(*, value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise typer.BadParameter(f"{name} expects a JSON list of records")

    records = list[dict[str, Any]]()
    for index, record in enumerate(value):
        if not isinstance(record, dict):
            raise typer.BadParameter(
                f"{name} expects record {index} to be a JSON object"
            )
        records.append(dict(record))
    return records


async def _record_chunks(
    records: list[dict[str, Any]], *, rows_per_chunk: int = 128
) -> AsyncIterator[list[dict[str, Any]]]:
    if rows_per_chunk <= 0:
        raise ValueError("rows_per_chunk must be greater than zero")
    for index in range(0, len(records), rows_per_chunk):
        yield records[index : index + rows_per_chunk]


def _indent_block(text: str, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" for line in text.splitlines())


async def _print_row_batches(
    *,
    batches: AsyncIterable[Any],
    pretty: bool,
) -> None:
    typer.echo("[", nl=False)
    first = True
    async for batch in batches:
        rows = batch.to_pylist() if isinstance(batch, pa.Table) else batch
        for row in rows:
            if first:
                if pretty:
                    typer.echo("\n", nl=False)
            else:
                typer.echo(",\n" if pretty else ",", nl=False)

            payload = _json.dumps(row, indent=2 if pretty else None)
            typer.echo(_indent_block(payload, "  ") if pretty else payload, nl=False)
            first = False

    if pretty and not first:
        typer.echo("\n", nl=False)
    typer.echo("]")


NamespaceOption = Annotated[
    Optional[List[str]],
    typer.Option(
        "--namespace",
        "-n",
        help="Namespace path segments (repeatable). Example: -n prod -n analytics",
    ),
]

BranchOption = Annotated[
    Optional[str],
    typer.Option("--branch", help="Dataset branch name (defaults to main)"),
]

VersionOption = Annotated[
    Optional[int],
    typer.Option(
        "--version",
        "-v",
        help="Historical table version to read (defaults to latest on the branch)",
    ),
]


# ---------------------------
# Commands
# ---------------------------


@app.async_command("tables", help="List dataset tables in a room.", hidden=True)
@app.async_command("table", help="List dataset tables in a room.")
async def list_tables(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
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
            tables = await client.datasets.list_tables(
                namespace=_ns(namespace),
                branch=branch,
            )
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


@app.async_command("inspect", help="Inspect a table schema in a room dataset.")
async def inspect(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
    version: VersionOption = None,
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
            schema = await client.datasets.inspect(
                table=table,
                namespace=_ns(namespace),
                branch=branch,
                version=version,
            )

            if json:
                print(schema.to_string())
            else:
                print(f"[bold]{table}[/bold]")
                print(schema.to_string())

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
    Create a dataset from a json file containing a list of RequiredTables.
    """
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )

        requirements = await _load_json_file(file, name="--file")

        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
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
    "create",
    help="Create a room dataset table with optional Arrow schema and seed data.",
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
    branch: BranchOption = None,
    columns: Annotated[
        Optional[str],
        typer.Option(
            "--columns",
            "-c",
            help=COLUMN_DEFINITIONS_HELP,
        ),
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
    Create a table with optional Arrow schema + optional initial data.

    Column definitions via --columns/-c use SQL-like syntax:
      names vector(20) null, tags list(text), meta struct(owner text, score float)

    Allowed types: int, bool, date, timestamp, float, text, json, binary, uuid, vector, list, struct.
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

        data_obj = _parse_json_arg(data_json, name="--data-json")
        data_obj = (
            data_obj
            if data_obj is not None
            else await _load_json_file(data_file, name="--data-file")
        )

        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            arrow_schema: pa.Schema | None = None
            if columns is not None:
                try:
                    arrow_schema = parse_table_schema(columns)
                except SchemaParseError as e:
                    raise typer.BadParameter(str(e))

            if data_obj is not None:
                records = _coerce_record_list(value=data_obj, name="create")
                if arrow_schema is not None:
                    await client.datasets.create_table_with_schema(
                        name=table,
                        schema=arrow_schema,
                        data=pa.Table.from_pylist(records, schema=arrow_schema),
                        mode=mode,  # type: ignore
                        namespace=_ns(namespace),
                        branch=branch,
                    )
                else:
                    await client.datasets.create_table_from_json_data(
                        name=table,
                        data=records,
                        mode=mode,  # type: ignore
                        namespace=_ns(namespace),
                        branch=branch,
                    )
            elif arrow_schema is not None:
                await client.datasets.create_table_with_schema(
                    name=table,
                    schema=arrow_schema,
                    mode=mode,  # type: ignore
                    namespace=_ns(namespace),
                    branch=branch,
                )
            else:
                raise typer.BadParameter(
                    "Provide --columns for an Arrow schema or --data-json/--data-file for JSON data"
                )

            print(f"[bold green]Created table:[/bold green] {table}")

    except (RoomException, ValidationError) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("drop", help="Drop a room dataset table.")
async def drop_table(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
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
            await client.datasets.drop_table(
                name=table,
                ignore_missing=ignore_missing,
                namespace=_ns(namespace),
                branch=branch,
            )
            print(f"[bold green]Dropped table:[/bold green] {table}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("add-columns", help="Add columns to a room dataset table.")
async def add_columns(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
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
                "JSON object of new columns mapped to SQL default expressions "
                '(e.g. \'{"created_at":"now()"}\').'
            ),
        ),
    ] = None,
):
    """
    Add columns. JSON supports server default SQL expression strings:
      {"col":"'default'"}

    Column definitions via --columns/-c use SQL-like syntax:
      names vector(20) null, tags list(text), meta struct(owner text, score float)

    Allowed types: int, bool, date, timestamp, float, text, json, binary, uuid, vector, list, struct.
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
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            if columns is not None:
                try:
                    new_cols = parse_table_schema(columns)
                except SchemaParseError as e:
                    raise typer.BadParameter(str(e))
            else:
                new_cols = {}
                for k, v in cols_obj.items():
                    if not isinstance(v, str):
                        raise typer.BadParameter(
                            "--columns-json values must be SQL expression strings"
                        )
                    new_cols[k] = v

            await client.datasets.add_columns(
                table=table,
                new_columns=new_cols,
                namespace=_ns(namespace),
                branch=branch,
            )
            print(f"[bold green]Added columns to[/bold green] {table}")

    except (RoomException, typer.BadParameter, ValidationError) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("drop-columns", help="Drop columns from a room dataset table.")
async def drop_columns(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
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
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            await client.datasets.drop_columns(
                table=table,
                columns=columns,
                namespace=_ns(namespace),
                branch=branch,
            )
            print(f"[bold green]Dropped columns from[/bold green] {table}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("insert", help="Insert records into a room dataset table.")
async def insert(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
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
            records
            if records is not None
            else await _load_json_file(file, name="--file")
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
            await client.datasets.insert_stream(
                table=table,
                chunks=[pa.Table.from_pylist(records)],
                namespace=_ns(namespace),
                branch=branch,
            )
            print(
                f"[bold green]Inserted[/bold green] {len(records)} record(s) into {table}"
            )

    except (RoomException, typer.BadParameter) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("merge", help="Upsert records into a room dataset table.")
async def merge(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    on: Annotated[str, typer.Option(..., "--on", help="Column to match for upsert")],
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
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
            records
            if records is not None
            else await _load_json_file(file, name="--file")
        )
        records = _coerce_record_list(value=records, name="merge")

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
            await client.datasets.merge_stream(
                table=table,
                on=on,
                chunks=[pa.Table.from_pylist(records)],
                namespace=_ns(namespace),
                branch=branch,
            )
            print(
                f"[bold green]Merged[/bold green] {len(records)} record(s) into {table} on {on}"
            )

    except (RoomException, typer.BadParameter) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("update", help="Update rows in a room dataset table.")
async def update(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    where: Annotated[
        str, typer.Option(..., "--where", help='SQL WHERE clause, e.g. "id = 1"')
    ],
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
    values_json: Annotated[
        str,
        typer.Option(
            "--values-json",
            help='JSON object of update values; use {"column":{"expression":"..."}} for expressions',
        ),
    ],
):
    account_client = await get_client()
    try:
        values = _parse_json_arg(values_json, name="--values-json")

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
            await client.datasets.update(
                table=table,
                where=where,
                values=values,
                namespace=_ns(namespace),
                branch=branch,
            )
            print(f"[bold green]Updated[/bold green] {table} where {where}")

    except (RoomException, typer.BadParameter) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("delete", help="Delete rows from a room dataset table.")
async def delete(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    where: Annotated[str, typer.Option(..., "--where", help="SQL WHERE clause")],
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
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
            await client.datasets.delete(
                table=table,
                where=where,
                namespace=_ns(namespace),
                branch=branch,
            )
            print(f"[bold green]Deleted[/bold green] from {table} where {where}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("search", help="Search rows in a room dataset table.")
async def search(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
    version: VersionOption = None,
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
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            await _print_row_batches(
                batches=client.datasets.search_stream(
                    table=table,
                    text=text,
                    vector=vec,
                    where=(wj if wj is not None else where),
                    select=list(select) if select else None,
                    limit=limit,
                    offset=offset,
                    namespace=_ns(namespace),
                    branch=branch,
                    version=version,
                ),
                pretty=pretty,
            )

    except (RoomException, typer.BadParameter) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("sql", help="Execute SQL against room dataset tables.")
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
    branch: BranchOption = None,
    version: VersionOption = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    """
    Execute SQL against one or more room dataset tables.

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
            else await _load_json_file(tables_file, name="--tables-file")
        )
        if tables_obj is not None and not isinstance(tables_obj, list):
            raise typer.BadParameter(
                "--tables-json/--tables-file must be a JSON array of table references"
            )

        params_obj = _parse_json_arg(params_json, name="--params-json")
        params_obj = (
            params_obj
            if params_obj is not None
            else await _load_json_file(params_file, name="--params-file")
        )
        if params_obj is not None and not isinstance(params_obj, dict):
            raise typer.BadParameter(
                "--params-json/--params-file must be a JSON object"
            )

        resolved_namespace = _ns(namespace)
        table_refs = _build_sql_table_refs(
            table=table,
            tables_obj=tables_obj,
            namespace=resolved_namespace,
            branch=branch,
            version=version,
        )
        params_table = _sql_params_table(params_obj)

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
            result = await client.datasets.execute_sql(
                query=query,
                tables=table_refs or None,
                params=params_table,
                namespace=resolved_namespace,
                branch=branch,
            )
            if isinstance(result, DatasetSqlStatement):
                print(
                    _json.dumps(
                        {"rows_affected": result.rows_affected},
                        indent=2 if pretty else None,
                    )
                )
            else:
                try:
                    await _print_row_batches(
                        batches=client.datasets.read_sql_query(
                            query_id=result.query_id,
                        ),
                        pretty=pretty,
                    )
                finally:
                    await client.datasets.close_sql_query(query_id=result.query_id)

    except (RoomException, typer.BadParameter, ValidationError) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command(
    "sql-exec",
    help="Execute a SQL statement that does not return rows.",
)
async def sql_exec(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    query: Annotated[
        str,
        typer.Option(
            ...,
            "--query",
            "-q",
            help="SQL statement to execute",
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
        typer.Option("--tables-json", help="JSON array of table refs"),
    ] = None,
    tables_file: Annotated[
        Optional[str],
        typer.Option("--tables-file", help="Path/URL to JSON array of table refs"),
    ] = None,
    params_json: Annotated[
        Optional[str],
        typer.Option(
            "--params-json",
            help="JSON object of SQL parameters, sent as one Arrow parameter row",
        ),
    ] = None,
    params_file: Annotated[
        Optional[str],
        typer.Option("--params-file", help="Path/URL to JSON object of SQL parameters"),
    ] = None,
    branch: BranchOption = None,
    version: VersionOption = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    """
    Execute SQL that returns an affected-row count instead of result rows.
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
            else await _load_json_file(tables_file, name="--tables-file")
        )
        if tables_obj is not None and not isinstance(tables_obj, list):
            raise typer.BadParameter(
                "--tables-json/--tables-file must be a JSON array of table references"
            )

        params_obj = _parse_json_arg(params_json, name="--params-json")
        params_obj = (
            params_obj
            if params_obj is not None
            else await _load_json_file(params_file, name="--params-file")
        )
        if params_obj is not None and not isinstance(params_obj, dict):
            raise typer.BadParameter(
                "--params-json/--params-file must be a JSON object"
            )

        resolved_namespace = _ns(namespace)
        table_refs = _build_sql_table_refs(
            table=table,
            tables_obj=tables_obj,
            namespace=resolved_namespace,
            branch=branch,
            version=version,
        )
        params_table = _sql_params_table(params_obj)

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
            rows_affected = await client.datasets.execute_sql_statement(
                query=query,
                tables=table_refs or None,
                params=params_table,
                namespace=resolved_namespace,
                branch=branch,
            )
            print(
                _json.dumps(
                    {"rows_affected": rows_affected},
                    indent=2 if pretty else None,
                )
            )

    except (RoomException, typer.BadParameter, ValidationError) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("optimize", help="Optimize a room dataset table.")
async def optimize(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
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
            await client.datasets.optimize(
                table=table,
                namespace=_ns(namespace),
                branch=branch,
            )
            print(f"[bold green]Optimized[/bold green] {table}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command(
    "versions",
    help="List versions for a room dataset table.",
    hidden=True,
)
@app.async_command("version", help="List versions for a room dataset table.")
async def list_versions(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
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
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            versions = await client.datasets.list_versions(
                table=table,
                namespace=_ns(namespace),
                branch=branch,
            )
            out = [v.model_dump(mode="json") for v in versions]
            print(_json.dumps(out, indent=2 if pretty else None))

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@branch_app.async_command("list", help="List dataset branches in a room namespace.")
async def list_branches(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
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
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            branches = await client.datasets.list_branches(namespace=_ns(namespace))
            out = [branch.model_dump(mode="json") for branch in branches]
            print(_json.dumps(out, indent=2 if pretty else None))

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@branch_app.async_command("create", help="Create a dataset branch.")
async def create_branch(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    branch: Annotated[str, typer.Option(..., "--branch", help="New branch name")],
    from_branch: Annotated[
        Optional[str],
        typer.Option("--from-branch", help="Source branch to branch from"),
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
            await client.datasets.create_branch(
                branch=branch,
                from_branch=from_branch,
                namespace=_ns(namespace),
            )
            if from_branch is None:
                print(f"[bold green]Created branch[/bold green] {branch} from main")
            else:
                print(
                    f"[bold green]Created branch[/bold green] {branch} from {from_branch}"
                )

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@branch_app.async_command("delete", help="Delete a dataset branch.")
async def delete_branch(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    branch: Annotated[str, typer.Option(..., "--branch", help="Branch name")],
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
            await client.datasets.delete_branch(
                branch=branch,
                namespace=_ns(namespace),
            )
            print(f"[bold green]Deleted branch[/bold green] {branch}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command(
    "restore", help="Restore a room dataset table to a specific version."
)
async def restore(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    version: Annotated[int, typer.Option(..., "--version", "-v", help="Table version")],
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
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
            await client.datasets.restore(
                table=table,
                version=version,
                namespace=_ns(namespace),
                branch=branch,
            )
            print(f"[bold green]Restored[/bold green] {table} to version {version}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command(
    "indexes",
    help="List indexes on a room dataset table.",
    hidden=True,
)
@app.async_command("index", help="List indexes on a room dataset table.")
async def list_indexes(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
    version: VersionOption = None,
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
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            idxs = await client.datasets.list_indexes(
                table=table,
                namespace=_ns(namespace),
                branch=branch,
                version=version,
            )
            out = [i.model_dump(mode="json") for i in idxs]
            print(_json.dumps(out, indent=2 if pretty else None))

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("index-create", help="Create an index on a room dataset table.")
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
    branch: BranchOption = None,
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
            if kind == "vector":
                await client.datasets.create_vector_index(
                    table=table,
                    column=column,
                    replace=replace,
                    namespace=_ns(namespace),
                    branch=branch,
                )
            elif kind == "scalar":
                await client.datasets.create_scalar_index(
                    table=table,
                    column=column,
                    replace=replace,
                    namespace=_ns(namespace),
                    branch=branch,
                )
            elif kind in "fts":
                await client.datasets.create_full_text_search_index(
                    table=table,
                    column=column,
                    replace=replace,
                    namespace=_ns(namespace),
                    branch=branch,
                )
            else:
                raise typer.BadParameter("--kind must be one of: vector, scalar, fts")

            print(f"[bold green]Created[/bold green] {kind} index on {table}.{column}")

    except (RoomException, typer.BadParameter) as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("index-drop", help="Drop an index from a room dataset table.")
async def drop_index(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    name: Annotated[str, typer.Option(..., "--name", help="Index name")],
    namespace: NamespaceOption = None,
    branch: BranchOption = None,
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
            await client.datasets.drop_index(
                table=table,
                name=name,
                namespace=_ns(namespace),
                branch=branch,
            )
            print(f"[bold green]Dropped index[/bold green] {name} on {table}")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()
