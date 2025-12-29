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
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.api import RoomClient, WebSocketClientProtocol
from meshagent.api.room_server_client import DataType
from meshagent.api import RoomException  # or wherever you defined it

app = async_typer.AsyncTyper(help="Manage database tables in a room")


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


@app.async_command("tables")
async def list_tables(
    *,
    project_id: ProjectIdOption = None,
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
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
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("inspect")
async def inspect(
    *,
    project_id: ProjectIdOption = None,
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
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
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("install")
async def install_requirements(
    *,
    project_id: ProjectIdOption = None,
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
                token=connection.jwt,
            )
        ) as client:
            for rt in requirements["tables"]:
                rt = RequiredTable.from_json(rt)
                print(f"installing table {rt.name} in namespace {rt.namespace}")
                await install_required_table(room=client, table=rt)

    except RoomException as e:
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("create")
async def create_table(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    mode: Annotated[
        str, typer.Option("--mode", help="create | overwrite | create_if_not_exists")
    ] = "create",
    namespace: NamespaceOption = None,
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
    """
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )

        schema_obj = _parse_json_arg(schema_json, name="--schema-json")
        schema_obj = (
            schema_obj
            if schema_obj is not None
            else _load_json_file(schema_file, name="--schema-file")
        )

        data_obj = _parse_json_arg(data_json, name="--data-json")
        data_obj = (
            data_obj
            if data_obj is not None
            else _load_json_file(data_file, name="--data-file")
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
                token=connection.jwt,
            )
        ) as client:
            # Build DataType objects from json if schema provided
            schema = None
            if schema_obj is not None:
                schema = {
                    k: DataType.from_json(v) for k, v in schema_obj.items()
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

    except RoomException as e:
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("drop")
async def drop_table(
    *,
    project_id: ProjectIdOption = None,
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
                token=connection.jwt,
            )
        ) as client:
            await client.database.drop_table(
                name=table, ignore_missing=ignore_missing, namespace=_ns(namespace)
            )
            print(f"[bold green]Dropped table:[/bold green] {table}")

    except RoomException as e:
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("add-columns")
async def add_columns(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t")],
    namespace: NamespaceOption = None,
    columns_json: Annotated[
        str, typer.Option(..., "--columns-json", help="JSON object of new columns")
    ] = None,
):
    """
    Add columns. JSON supports either:
      - DataType JSON: {"col":{"type":"text"}}
      - or server default SQL expr strings: {"col":"'default'"}
    """
    account_client = await get_client()
    try:
        cols_obj = _parse_json_arg(columns_json, name="--columns-json")
        if not isinstance(cols_obj, dict):
            raise typer.BadParameter("--columns-json must be a JSON object")

        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
                token=connection.jwt,
            )
        ) as client:
            # Convert DataType json objects into DataType instances; pass strings through.
            new_cols = {}
            for k, v in cols_obj.items():
                if isinstance(v, dict) and "type" in v:
                    new_cols[k] = DataType.from_json(v)
                else:
                    new_cols[k] = v

            await client.database.add_columns(
                table=table, new_columns=new_cols, namespace=_ns(namespace)
            )
            print(f"[bold green]Added columns to[/bold green] {table}")

    except (RoomException, typer.BadParameter) as e:
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("drop-columns")
async def drop_columns(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t")],
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
                token=connection.jwt,
            )
        ) as client:
            await client.database.drop_columns(
                table=table, columns=columns, namespace=_ns(namespace)
            )
            print(f"[bold green]Dropped columns from[/bold green] {table}")

    except RoomException as e:
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("insert")
async def insert(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t")],
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
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
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("merge")
async def merge(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t")],
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
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
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("update")
async def update(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t")],
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
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
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("delete")
async def delete(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t")],
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
                token=connection.jwt,
            )
        ) as client:
            await client.database.delete(
                table=table, where=where, namespace=_ns(namespace)
            )
            print(f"[bold green]Deleted[/bold green] from {table} where {where}")

    except RoomException as e:
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("search")
async def search(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t")],
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
    limit: Annotated[Optional[int], typer.Option("--limit")] = None,
    offset: Annotated[Optional[int], typer.Option("--offset")] = None,
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
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
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("optimize")
async def optimize(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t")],
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
                token=connection.jwt,
            )
        ) as client:
            await client.database.optimize(table=table, namespace=_ns(namespace))
            print(f"[bold green]Optimized[/bold green] {table}")

    except RoomException as e:
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("versions")
async def list_versions(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t")],
    namespace: NamespaceOption = None,
    pretty: Annotated[bool, typer.Option("--pretty/--no-pretty")] = True,
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
                token=connection.jwt,
            )
        ) as client:
            versions = await client.database.list_versions(
                table=table, namespace=_ns(namespace)
            )
            out = [v.model_dump(mode="json") for v in versions]
            print(_json.dumps(out, indent=2 if pretty else None))

    except RoomException as e:
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("checkout")
async def checkout(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t")],
    version: Annotated[int, typer.Option(..., "--version", "-v")],
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
                token=connection.jwt,
            )
        ) as client:
            await client.database.checkout(
                table=table, version=version, namespace=_ns(namespace)
            )
            print(f"[bold green]Checked out[/bold green] {table} @ version {version}")

    except RoomException as e:
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("restore")
async def restore(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t")],
    version: Annotated[int, typer.Option(..., "--version", "-v")],
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
                token=connection.jwt,
            )
        ) as client:
            await client.database.restore(
                table=table, version=version, namespace=_ns(namespace)
            )
            print(f"[bold green]Restored[/bold green] {table} to version {version}")

    except RoomException as e:
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("indexes")
async def list_indexes(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t")],
    namespace: NamespaceOption = None,
    pretty: Annotated[bool, typer.Option("--pretty/--no-pretty")] = True,
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
                token=connection.jwt,
            )
        ) as client:
            idxs = await client.database.list_indexes(
                table=table, namespace=_ns(namespace)
            )
            out = [i.model_dump(mode="json") for i in idxs]
            print(_json.dumps(out, indent=2 if pretty else None))

    except RoomException as e:
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("index-create")
async def create_index(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t")],
    column: Annotated[str, typer.Option(..., "--column", "-c")],
    kind: Annotated[
        str, typer.Option(..., "--kind", help="vector | scalar | fts")
    ] = "scalar",
    replace: Annotated[Optional[bool], typer.Option("--replace/--no-replace")] = None,
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
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
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("index-drop")
async def drop_index(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    table: Annotated[str, typer.Option(..., "--table", "-t")],
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
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
                token=connection.jwt,
            )
        ) as client:
            await client.database.drop_index(
                table=table, name=name, namespace=_ns(namespace)
            )
            print(f"[bold green]Dropped index[/bold green] {name} on {table}")

    except RoomException as e:
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()
