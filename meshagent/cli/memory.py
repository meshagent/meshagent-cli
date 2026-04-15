import asyncio
import base64
import json as _json
import mimetypes
import pathlib
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator, List, Optional

import typer
from pydantic import ValidationError
from rich import print

from meshagent.api import RoomClient, RoomException, WebSocketClientProtocol
from meshagent.api.helpers import websocket_room_url
from meshagent.api.room_server_client import (
    MemoryEntityRecord,
    MemoryIngestStrategy,
    MemoryRelationshipSelector,
    MemoryRelationshipRecord,
)
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import get_client, resolve_project_id, resolve_room

app = async_typer.AsyncTyper(help="Manage room memories")


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

TableNamespaceOption = Annotated[
    Optional[List[str]],
    typer.Option(
        "--table-namespace",
        help="Namespace path segments for the source table (repeatable)",
    ),
]


def _parse_json_arg(json_str: Optional[str], *, name: str) -> Any:
    if json_str is None:
        return None
    try:
        return _json.loads(json_str)
    except _json.JSONDecodeError as ex:
        raise typer.BadParameter(f"Invalid JSON for {name}: {ex}") from ex


def _load_json_file(path: Optional[str], *, name: str) -> Any:
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return _json.load(handle)
    except OSError as ex:
        raise typer.BadParameter(f"Unable to read {name} from {path}: {ex}") from ex
    except _json.JSONDecodeError as ex:
        raise typer.BadParameter(f"Invalid JSON in {name} {path}: {ex}") from ex


def _read_text_file(path: str, *, name: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as ex:
        raise typer.BadParameter(f"Unable to read {name} from {path}: {ex}") from ex


def _read_binary_file(path: str, *, name: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as ex:
        raise typer.BadParameter(f"Unable to read {name} from {path}: {ex}") from ex


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "encoding": "base64",
            "data": base64.b64encode(value).decode("ascii"),
        }
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any, *, pretty: bool) -> str:
    return _json.dumps(
        value, indent=2 if pretty else None, default=_json_default, sort_keys=False
    )


def _parse_string_map(
    json_str: Optional[str], *, name: str
) -> Optional[dict[str, str]]:
    if json_str is None:
        return None
    obj = _parse_json_arg(json_str, name=name)
    if not isinstance(obj, dict):
        raise typer.BadParameter(f"{name} must be a JSON object")

    parsed: dict[str, str] = {}
    for key, value in obj.items():
        if not isinstance(key, str):
            raise typer.BadParameter(f"{name} keys must be strings")
        if not isinstance(value, str):
            raise typer.BadParameter(f"{name} values must be strings")
        parsed[key] = value
    return parsed


def _parse_record_list(records_obj: Any, *, name: str) -> list[dict[str, Any]]:
    if not isinstance(records_obj, list):
        raise typer.BadParameter(f"{name} must be a JSON array of objects")

    rows: list[dict[str, Any]] = []
    for idx, value in enumerate(records_obj):
        if not isinstance(value, dict):
            raise typer.BadParameter(f"{name}[{idx}] must be a JSON object")
        row = {str(key): column for key, column in value.items()}
        rows.append(row)
    return rows


def _load_records(
    *,
    records_json: Optional[str],
    records_file: Optional[str],
) -> list[dict[str, Any]]:
    if records_json is not None and records_file is not None:
        raise typer.BadParameter("Use --records-json or --records-file, not both")
    obj = _parse_json_arg(records_json, name="--records-json")
    if obj is None:
        obj = _load_json_file(records_file, name="--records-file")
    if obj is None:
        raise typer.BadParameter("Provide --records-json or --records-file")
    return _parse_record_list(obj, name="records")


_DEFAULT_FALKOR_IMAGE = "falkordb/falkordb:latest"
_FALKOR_RDB_CONTAINER_PATH = "/var/lib/falkordb/data/dump.rdb"
_GRAPH_KEY_TYPE = "graphdata"
_DEFAULT_GRAPH_NAME = "default_db"


class _MemoryImportError(RuntimeError):
    pass


def _resolve_rdb_file_path(path: pathlib.Path) -> pathlib.Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise typer.BadParameter(f"RDB file does not exist: {resolved}")
    if not resolved.is_file():
        raise typer.BadParameter(f"RDB file must be a file path: {resolved}")
    return resolved


async def _run_local_command(command: list[str]) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as ex:
        missing_binary = command[0] if len(command) > 0 else "<empty command>"
        raise _MemoryImportError(
            f"Required command is not installed: {missing_binary}"
        ) from ex

    stdout_bytes, stderr_bytes = await process.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    return process.returncode, stdout, stderr


async def _run_local_command_checked(command: list[str], *, context: str) -> str:
    returncode, stdout, stderr = await _run_local_command(command)
    if returncode != 0:
        detail = stderr.strip() or stdout.strip() or f"exit code {returncode}"
        raise _MemoryImportError(f"Unable to {context}: {detail}")
    return stdout


async def _wait_for_falkor_ready(
    *, container_name: str, timeout_seconds: float
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        returncode, stdout, _stderr = await _run_local_command(
            ["docker", "exec", container_name, "redis-cli", "PING"]
        )
        if returncode == 0 and "PONG" in stdout:
            return
        await asyncio.sleep(0.5)

    _returncode, logs_stdout, logs_stderr = await _run_local_command(
        ["docker", "logs", "--tail", "40", container_name]
    )
    details = logs_stdout.strip() or logs_stderr.strip()
    if details:
        raise _MemoryImportError(
            "Timed out waiting for FalkorDB to become ready. "
            f"Latest container logs:\n{details}"
        )
    raise _MemoryImportError("Timed out waiting for FalkorDB to become ready.")


@asynccontextmanager
async def _running_falkor_container(
    *, rdb_file: pathlib.Path, docker_image: str, startup_timeout_seconds: float
) -> AsyncIterator[str]:
    container_name = f"meshagent-memory-import-{uuid.uuid4().hex[:12]}"
    mount_spec = f"{rdb_file}:{_FALKOR_RDB_CONTAINER_PATH}:ro"
    await _run_local_command_checked(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            container_name,
            "-v",
            mount_spec,
            docker_image,
        ],
        context="start temporary FalkorDB container",
    )
    try:
        await _wait_for_falkor_ready(
            container_name=container_name,
            timeout_seconds=startup_timeout_seconds,
        )
        yield container_name
    finally:
        await _run_local_command(["docker", "rm", "-f", container_name])


async def _list_graph_names(*, container_name: str) -> list[str]:
    keys_output = await _run_local_command_checked(
        ["docker", "exec", container_name, "redis-cli", "--raw", "--scan"],
        context="scan keys from FalkorDB",
    )
    keys = sorted({line.strip() for line in keys_output.splitlines() if line.strip()})

    graph_names: list[str] = []
    for key in keys:
        key_type = (
            await _run_local_command_checked(
                ["docker", "exec", container_name, "redis-cli", "--raw", "TYPE", key],
                context=f"inspect Redis key type for '{key}'",
            )
        ).strip()
        if key_type == _GRAPH_KEY_TYPE:
            graph_names.append(key)
    return graph_names


def _select_graph_name(
    *, graph_names: list[str], requested_graph_name: Optional[str]
) -> str:
    if requested_graph_name is not None:
        if requested_graph_name not in graph_names:
            available = ", ".join(graph_names) if graph_names else "<none>"
            raise _MemoryImportError(
                f"Graph '{requested_graph_name}' was not found. "
                f"Available graphs: {available}"
            )
        return requested_graph_name

    if len(graph_names) == 0:
        raise _MemoryImportError(
            "No FalkorDB graph keys were found in the provided dump.rdb file."
        )
    if len(graph_names) == 1:
        return graph_names[0]

    non_default_graphs = [
        graph_name for graph_name in graph_names if graph_name != _DEFAULT_GRAPH_NAME
    ]
    if len(non_default_graphs) == 1:
        return non_default_graphs[0]

    available = ", ".join(graph_names)
    raise _MemoryImportError(
        f"Multiple FalkorDB graphs were found ({available}). "
        "Pass --graph-name to choose one."
    )


def _parse_graph_query_response(payload: Any) -> list[list[Any]]:
    if not isinstance(payload, list) or len(payload) < 2:
        raise _MemoryImportError(
            "Unexpected GRAPH.QUERY response format from redis-cli"
        )

    columns = payload[0]
    rows = payload[1]
    if not isinstance(columns, list):
        raise _MemoryImportError("Unexpected GRAPH.QUERY column payload")
    if not isinstance(rows, list):
        raise _MemoryImportError("Unexpected GRAPH.QUERY row payload")
    for row in rows:
        if not isinstance(row, list):
            raise _MemoryImportError("Unexpected GRAPH.QUERY row item payload")
    return rows


async def _query_graph_rows(
    *, container_name: str, graph_name: str, statement: str
) -> list[list[Any]]:
    query_output = await _run_local_command_checked(
        [
            "docker",
            "exec",
            container_name,
            "redis-cli",
            "--json",
            "GRAPH.QUERY",
            graph_name,
            statement,
        ],
        context=f"query graph '{graph_name}'",
    )
    try:
        payload = _json.loads(query_output)
    except _json.JSONDecodeError as ex:
        raise _MemoryImportError(
            "Unable to parse GRAPH.QUERY JSON output from redis-cli"
        ) from ex
    return _parse_graph_query_response(payload)


def _coerce_optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _coerce_required_string(value: Any, *, field_name: str) -> str:
    text = _coerce_optional_string(value)
    if text is None:
        raise _MemoryImportError(f"Missing required value for {field_name}")
    return text


def _coerce_optional_float(value: Any, *, field_name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _MemoryImportError(
            f"Invalid boolean value for {field_name}; expected a number."
        )
    if isinstance(value, (int, float)):
        return float(value)

    text = _coerce_optional_string(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError as ex:
        raise _MemoryImportError(
            f"Invalid numeric value for {field_name}: {value}"
        ) from ex


def _entity_record_from_row(row: list[Any]) -> MemoryEntityRecord:
    if len(row) != 7:
        raise _MemoryImportError(
            f"Unexpected entity row width {len(row)} (expected 7 columns)"
        )
    return MemoryEntityRecord(
        entity_id=_coerce_required_string(row[0], field_name="entity_id"),
        name=_coerce_required_string(row[1], field_name="name"),
        entity_type=_coerce_optional_string(row[2]),
        context=_coerce_optional_string(row[3]),
        confidence=_coerce_optional_float(row[4], field_name="confidence"),
        created_at=_coerce_optional_string(row[5]),
        valid_at=_coerce_optional_string(row[6]),
    )


def _relationship_record_from_row(row: list[Any]) -> MemoryRelationshipRecord:
    if len(row) != 11:
        raise _MemoryImportError(
            f"Unexpected relationship row width {len(row)} (expected 11 columns)"
        )

    relationship_type = _coerce_optional_string(row[2]) or "RELATED_TO"
    return MemoryRelationshipRecord(
        source_entity_id=_coerce_required_string(row[0], field_name="source_entity_id"),
        target_entity_id=_coerce_required_string(row[1], field_name="target_entity_id"),
        relationship_type=relationship_type,
        description=_coerce_optional_string(row[3]),
        confidence=_coerce_optional_float(row[4], field_name="confidence"),
        created_at=_coerce_optional_string(row[5]),
        valid_at=_coerce_optional_string(row[6]),
        expired_at=_coerce_optional_string(row[7]),
        invalid_at=_coerce_optional_string(row[8]),
        source_entity_name=_coerce_optional_string(row[9]),
        target_entity_name=_coerce_optional_string(row[10]),
    )


def _entity_batch_statement(*, skip: int, limit: int) -> str:
    return (
        "MATCH (n) "
        "RETURN "
        "coalesce(n.uuid, tostring(id(n))), "
        "coalesce(n.name, n.title, coalesce(n.uuid, tostring(id(n)))), "
        "CASE "
        "WHEN size(labels(n)) = 0 THEN '' "
        "WHEN labels(n)[0] = 'Entity' AND size(labels(n)) > 1 THEN labels(n)[1] "
        "ELSE labels(n)[0] "
        "END, "
        "coalesce(n.context, n.summary, n.content, ''), "
        "n.confidence, "
        "n.created_at, "
        "n.valid_at "
        "ORDER BY id(n) "
        f"SKIP {skip} LIMIT {limit}"
    )


def _relationship_batch_statement(*, skip: int, limit: int) -> str:
    return (
        "MATCH (a)-[r]->(b) "
        "RETURN "
        "coalesce(a.uuid, tostring(id(a))), "
        "coalesce(b.uuid, tostring(id(b))), "
        "coalesce(r.name, type(r), 'RELATED_TO'), "
        "coalesce(r.fact, r.description, r.context, ''), "
        "r.confidence, "
        "r.created_at, "
        "r.valid_at, "
        "r.expired_at, "
        "r.invalid_at, "
        "coalesce(a.name, coalesce(a.uuid, tostring(id(a)))), "
        "coalesce(b.name, coalesce(b.uuid, tostring(id(b)))) "
        "ORDER BY id(r) "
        f"SKIP {skip} LIMIT {limit}"
    )


async def _import_entities_from_graph(
    *,
    client: RoomClient,
    memory_name: str,
    namespace: Optional[list[str]],
    graph_name: str,
    container_name: str,
    batch_size: int,
    merge: bool,
) -> int:
    imported = 0
    skip = 0
    while True:
        rows = await _query_graph_rows(
            container_name=container_name,
            graph_name=graph_name,
            statement=_entity_batch_statement(skip=skip, limit=batch_size),
        )
        if len(rows) == 0:
            break

        records = [_entity_record_from_row(row) for row in rows]
        await client.memory.upsert_nodes(
            name=memory_name,
            records=records,
            merge=merge,
            namespace=namespace,
        )
        imported += len(records)
        skip += len(rows)
        if len(rows) < batch_size:
            break
    return imported


async def _import_relationships_from_graph(
    *,
    client: RoomClient,
    memory_name: str,
    namespace: Optional[list[str]],
    graph_name: str,
    container_name: str,
    batch_size: int,
    merge: bool,
) -> int:
    imported = 0
    skip = 0
    while True:
        rows = await _query_graph_rows(
            container_name=container_name,
            graph_name=graph_name,
            statement=_relationship_batch_statement(skip=skip, limit=batch_size),
        )
        if len(rows) == 0:
            break

        records = [_relationship_record_from_row(row) for row in rows]
        await client.memory.upsert_relationships(
            name=memory_name,
            records=records,
            merge=merge,
            namespace=namespace,
        )
        imported += len(records)
        skip += len(rows)
        if len(rows) < batch_size:
            break
    return imported


async def _ensure_memory_exists(
    *,
    client: RoomClient,
    memory_name: str,
    namespace: Optional[list[str]],
    create_if_missing: bool,
) -> bool:
    memories = await client.memory.list(namespace=namespace)
    if memory_name in memories:
        return False
    if not create_if_missing:
        namespace_text = "/".join(namespace) if namespace else "<root>"
        raise _MemoryImportError(
            f"Memory '{memory_name}' does not exist in namespace '{namespace_text}'. "
            "Use --create-memory to create it automatically."
        )

    await client.memory.create(name=memory_name, namespace=namespace, overwrite=False)
    return True


@asynccontextmanager
async def _connected_room_client(
    *, project_id: Optional[str], room: Optional[str]
) -> AsyncIterator[RoomClient]:
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        if room_name is None:
            raise typer.BadParameter(
                "Room name not specified. Pass --room or set MESHAGENT_ROOM."
            )
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            yield client
    finally:
        await account_client.close()


@app.async_command("list", help="List memories in a room namespace.")
async def list_memories(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    namespace: NamespaceOption = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    try:
        async with _connected_room_client(project_id=project_id, room=room) as client:
            memories = await client.memory.list(namespace=_ns(namespace))
            print(_json_dumps({"memories": memories}, pretty=pretty))
    except (RoomException, typer.BadParameter) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command("create", help="Create a room memory store.")
async def create_memory(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    namespace: NamespaceOption = None,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Overwrite existing memory")
    ] = False,
):
    try:
        async with _connected_room_client(project_id=project_id, room=room) as client:
            await client.memory.create(
                name=name, namespace=_ns(namespace), overwrite=overwrite
            )
            print(f"[bold green]Created memory:[/bold green] {name}")
    except (RoomException, typer.BadParameter) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command("drop", help="Drop a room memory store.")
async def drop_memory(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    namespace: NamespaceOption = None,
    ignore_missing: Annotated[
        bool, typer.Option("--ignore-missing", help="Ignore missing memory")
    ] = False,
):
    try:
        async with _connected_room_client(project_id=project_id, room=room) as client:
            await client.memory.drop(
                name=name,
                namespace=_ns(namespace),
                ignore_missing=ignore_missing,
            )
            print(f"[bold green]Dropped memory:[/bold green] {name}")
    except (RoomException, typer.BadParameter) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command("inspect", help="Inspect metadata and datasets for a memory.")
async def inspect_memory(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    namespace: NamespaceOption = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    try:
        async with _connected_room_client(project_id=project_id, room=room) as client:
            details = await client.memory.inspect(name=name, namespace=_ns(namespace))
            print(_json_dumps(details.model_dump(mode="json"), pretty=pretty))
    except (RoomException, typer.BadParameter, ValidationError) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command("query", help="Run a SQL-like query against memory datasets.")
async def query_memory(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    statement: Annotated[
        str,
        typer.Option(
            ...,
            "--statement",
            "-s",
            help='Statement, e.g. "MATCH (e:Entity) RETURN e.name LIMIT 10"',
        ),
    ],
    namespace: NamespaceOption = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    try:
        async with _connected_room_client(project_id=project_id, room=room) as client:
            results = await client.memory.query(
                name=name,
                statement=statement,
                namespace=_ns(namespace),
            )
            print(_json_dumps({"results": results}, pretty=pretty))
    except (RoomException, typer.BadParameter) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command("upsert-table", help="Upsert records from JSON into memory tables.")
async def upsert_table(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Table name")],
    records_json: Annotated[
        Optional[str],
        typer.Option("--records-json", help="JSON array of records"),
    ] = None,
    records_file: Annotated[
        Optional[str],
        typer.Option("--records-file", help="Path to JSON file with records"),
    ] = None,
    namespace: NamespaceOption = None,
    merge: Annotated[
        bool, typer.Option("--merge/--no-merge", help="Merge with existing records")
    ] = True,
):
    try:
        records = _load_records(records_json=records_json, records_file=records_file)
        async with _connected_room_client(project_id=project_id, room=room) as client:
            await client.memory.upsert_table(
                name=name,
                table=table,
                records=records,
                merge=merge,
                namespace=_ns(namespace),
            )
            print(
                f"[bold green]Upserted[/bold green] {len(records)} record(s) into memory table {table}"
            )
    except (RoomException, typer.BadParameter) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command("upsert-nodes", help="Upsert entity nodes into memory.")
async def upsert_nodes(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    records_json: Annotated[
        Optional[str],
        typer.Option("--records-json", help="JSON array of MemoryEntityRecord objects"),
    ] = None,
    records_file: Annotated[
        Optional[str],
        typer.Option("--records-file", help="Path to JSON file with entity records"),
    ] = None,
    namespace: NamespaceOption = None,
    merge: Annotated[
        bool, typer.Option("--merge/--no-merge", help="Merge with existing nodes")
    ] = True,
):
    try:
        rows = _load_records(records_json=records_json, records_file=records_file)
        records = [MemoryEntityRecord.model_validate(row) for row in rows]
        async with _connected_room_client(project_id=project_id, room=room) as client:
            await client.memory.upsert_nodes(
                name=name,
                records=records,
                merge=merge,
                namespace=_ns(namespace),
            )
            print(f"[bold green]Upserted[/bold green] {len(records)} node record(s)")
    except (RoomException, typer.BadParameter, ValidationError) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command(
    "upsert-relationships", help="Upsert relationship edges into memory."
)
async def upsert_relationships(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    records_json: Annotated[
        Optional[str],
        typer.Option(
            "--records-json", help="JSON array of MemoryRelationshipRecord objects"
        ),
    ] = None,
    records_file: Annotated[
        Optional[str],
        typer.Option(
            "--records-file", help="Path to JSON file with relationship records"
        ),
    ] = None,
    namespace: NamespaceOption = None,
    merge: Annotated[
        bool,
        typer.Option("--merge/--no-merge", help="Merge with existing relationships"),
    ] = True,
):
    try:
        rows = _load_records(records_json=records_json, records_file=records_file)
        records = [MemoryRelationshipRecord.model_validate(row) for row in rows]
        async with _connected_room_client(project_id=project_id, room=room) as client:
            await client.memory.upsert_relationships(
                name=name,
                records=records,
                merge=merge,
                namespace=_ns(namespace),
            )
            print(
                f"[bold green]Upserted[/bold green] {len(records)} relationship record(s)"
            )
    except (RoomException, typer.BadParameter, ValidationError) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command(
    "import",
    help="Import entities and relationships from a FalkorDB dump.rdb file.",
)
async def import_falkordb_memory(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    rdb_file: Annotated[
        pathlib.Path,
        typer.Option(
            ...,
            "--rdb-file",
            "-f",
            help="Path to FalkorDB dump.rdb file",
        ),
    ],
    graph_name: Annotated[
        Optional[str],
        typer.Option(
            "--graph-name",
            help="FalkorDB graph key name. Auto-detected when unambiguous.",
        ),
    ] = None,
    namespace: NamespaceOption = None,
    merge: Annotated[
        bool,
        typer.Option("--merge/--no-merge", help="Merge with existing records"),
    ] = True,
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            min=1,
            help="Rows per GRAPH.QUERY batch while importing",
        ),
    ] = 250,
    docker_image: Annotated[
        str,
        typer.Option(
            "--docker-image",
            help="Docker image to use for temporary FalkorDB container",
        ),
    ] = _DEFAULT_FALKOR_IMAGE,
    startup_timeout_seconds: Annotated[
        float,
        typer.Option(
            "--startup-timeout-seconds",
            min=1.0,
            help="Max seconds to wait for FalkorDB startup",
        ),
    ] = 30.0,
    create_memory: Annotated[
        bool,
        typer.Option(
            "--create-memory/--no-create-memory",
            help="Create the target memory when missing",
        ),
    ] = True,
):
    try:
        resolved_rdb_file = _resolve_rdb_file_path(rdb_file)
        namespace_value = _ns(namespace)
        async with _connected_room_client(project_id=project_id, room=room) as client:
            created = await _ensure_memory_exists(
                client=client,
                memory_name=name,
                namespace=namespace_value,
                create_if_missing=create_memory,
            )
            if created:
                print(f"[bold green]Created memory:[/bold green] {name}")

            print(f"[dim]Starting FalkorDB import from[/dim] {resolved_rdb_file}")
            async with _running_falkor_container(
                rdb_file=resolved_rdb_file,
                docker_image=docker_image,
                startup_timeout_seconds=startup_timeout_seconds,
            ) as container_name:
                graph_names = await _list_graph_names(container_name=container_name)
                selected_graph_name = _select_graph_name(
                    graph_names=graph_names,
                    requested_graph_name=graph_name,
                )
                print(f"[dim]Using FalkorDB graph:[/dim] {selected_graph_name}")

                entities_imported = await _import_entities_from_graph(
                    client=client,
                    memory_name=name,
                    namespace=namespace_value,
                    graph_name=selected_graph_name,
                    container_name=container_name,
                    batch_size=batch_size,
                    merge=merge,
                )
                relationships_imported = await _import_relationships_from_graph(
                    client=client,
                    memory_name=name,
                    namespace=namespace_value,
                    graph_name=selected_graph_name,
                    container_name=container_name,
                    batch_size=batch_size,
                    merge=merge,
                )

                print(
                    "[bold green]Import complete:[/bold green] "
                    f"{entities_imported} entities, {relationships_imported} relationships"
                )
    except (
        RoomException,
        typer.BadParameter,
        ValidationError,
        _MemoryImportError,
    ) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command("ingest-text", help="Extract memory from input text.")
async def ingest_text(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    text: Annotated[
        Optional[str], typer.Option("--text", help="Text content to ingest")
    ] = None,
    file: Annotated[
        Optional[str], typer.Option("--file", "-f", help="Path to UTF-8 text file")
    ] = None,
    namespace: NamespaceOption = None,
    strategy: Annotated[
        MemoryIngestStrategy,
        typer.Option("--strategy", help="Ingest strategy: heuristic or llm"),
    ] = "heuristic",
    llm_model: Annotated[
        Optional[str], typer.Option("--llm-model", help="Model name for llm strategy")
    ] = None,
    llm_temperature: Annotated[
        Optional[float],
        typer.Option("--llm-temperature", help="Temperature for llm strategy"),
    ] = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    try:
        if text is not None and file is not None:
            raise typer.BadParameter("Use --text or --file, not both")
        if text is None and file is None:
            raise typer.BadParameter("Provide --text or --file")
        if file is not None:
            text = _read_text_file(file, name="--file")
        assert text is not None

        async with _connected_room_client(project_id=project_id, room=room) as client:
            result = await client.memory.ingest_text(
                name=name,
                text=text,
                namespace=_ns(namespace),
                strategy=strategy,
                llm_model=llm_model,
                llm_temperature=llm_temperature,
            )
            print(_json_dumps(result.model_dump(mode="json"), pretty=pretty))
    except (RoomException, typer.BadParameter, ValidationError) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command("ingest-image", help="Extract memory from an image.")
async def ingest_image(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    file: Annotated[
        Optional[str], typer.Option("--file", "-f", help="Path to image file")
    ] = None,
    caption: Annotated[
        Optional[str], typer.Option("--caption", help="Optional image caption")
    ] = None,
    mime_type: Annotated[
        Optional[str], typer.Option("--mime-type", help="Image mime type")
    ] = None,
    source: Annotated[
        Optional[str], typer.Option("--source", help="Source identifier/path")
    ] = None,
    annotations_json: Annotated[
        Optional[str],
        typer.Option(
            "--annotations-json",
            help='Optional JSON map of string annotations, e.g. {"origin":"camera"}',
        ),
    ] = None,
    namespace: NamespaceOption = None,
    strategy: Annotated[
        MemoryIngestStrategy,
        typer.Option("--strategy", help="Ingest strategy: heuristic or llm"),
    ] = "heuristic",
    llm_model: Annotated[
        Optional[str], typer.Option("--llm-model", help="Model name for llm strategy")
    ] = None,
    llm_temperature: Annotated[
        Optional[float],
        typer.Option("--llm-temperature", help="Temperature for llm strategy"),
    ] = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    try:
        if caption is None and file is None:
            raise typer.BadParameter("Provide --caption and/or --file")

        image_data: Optional[bytes] = None
        if file is not None:
            image_data = _read_binary_file(file, name="--file")
            if mime_type is None:
                guessed, _ = mimetypes.guess_type(file)
                mime_type = guessed
            if source is None:
                source = file

        annotations = _parse_string_map(annotations_json, name="--annotations-json")

        async with _connected_room_client(project_id=project_id, room=room) as client:
            result = await client.memory.ingest_image(
                name=name,
                caption=caption,
                data=image_data,
                mime_type=mime_type,
                source=source,
                annotations=annotations,
                namespace=_ns(namespace),
                strategy=strategy,
                llm_model=llm_model,
                llm_temperature=llm_temperature,
            )
            print(_json_dumps(result.model_dump(mode="json"), pretty=pretty))
    except (RoomException, typer.BadParameter, ValidationError) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command("ingest-file", help="Extract memory from local file/text content.")
async def ingest_file(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    path: Annotated[
        Optional[str],
        typer.Option("--path", help="File path available to the room server"),
    ] = None,
    text: Annotated[
        Optional[str], typer.Option("--text", help="Inline text content")
    ] = None,
    text_file: Annotated[
        Optional[str], typer.Option("--text-file", help="Path to local UTF-8 text file")
    ] = None,
    mime_type: Annotated[
        Optional[str], typer.Option("--mime-type", help="Optional mime type")
    ] = None,
    namespace: NamespaceOption = None,
    strategy: Annotated[
        MemoryIngestStrategy,
        typer.Option("--strategy", help="Ingest strategy: heuristic or llm"),
    ] = "heuristic",
    llm_model: Annotated[
        Optional[str], typer.Option("--llm-model", help="Model name for llm strategy")
    ] = None,
    llm_temperature: Annotated[
        Optional[float],
        typer.Option("--llm-temperature", help="Temperature for llm strategy"),
    ] = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    try:
        if text is not None and text_file is not None:
            raise typer.BadParameter("Use --text or --text-file, not both")
        if text_file is not None:
            text = _read_text_file(text_file, name="--text-file")
        if path is None and text is None:
            raise typer.BadParameter("Provide --path, --text, or --text-file")

        async with _connected_room_client(project_id=project_id, room=room) as client:
            result = await client.memory.ingest_file(
                name=name,
                path=path,
                text=text,
                mime_type=mime_type,
                namespace=_ns(namespace),
                strategy=strategy,
                llm_model=llm_model,
                llm_temperature=llm_temperature,
            )
            print(_json_dumps(result.model_dump(mode="json"), pretty=pretty))
    except (RoomException, typer.BadParameter, ValidationError) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command("ingest-from-table", help="Extract memory from table rows.")
async def ingest_from_table(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    table: Annotated[str, typer.Option(..., "--table", "-t", help="Source table name")],
    text_column: Annotated[
        Optional[List[str]],
        typer.Option("--text-column", help="Source text columns (repeatable)"),
    ] = None,
    table_namespace: TableNamespaceOption = None,
    limit: Annotated[
        Optional[int], typer.Option("--limit", help="Maximum source rows")
    ] = None,
    namespace: NamespaceOption = None,
    strategy: Annotated[
        MemoryIngestStrategy,
        typer.Option("--strategy", help="Ingest strategy: heuristic or llm"),
    ] = "heuristic",
    llm_model: Annotated[
        Optional[str], typer.Option("--llm-model", help="Model name for llm strategy")
    ] = None,
    llm_temperature: Annotated[
        Optional[float],
        typer.Option("--llm-temperature", help="Temperature for llm strategy"),
    ] = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    try:
        async with _connected_room_client(project_id=project_id, room=room) as client:
            result = await client.memory.ingest_from_table(
                name=name,
                table=table,
                text_columns=list(text_column) if text_column else None,
                table_namespace=_ns(table_namespace),
                limit=limit,
                namespace=_ns(namespace),
                strategy=strategy,
                llm_model=llm_model,
                llm_temperature=llm_temperature,
            )
            print(_json_dumps(result.model_dump(mode="json"), pretty=pretty))
    except (RoomException, typer.BadParameter, ValidationError) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command(
    "ingest-from-storage", help="Extract memory from room storage paths."
)
async def ingest_from_storage(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    path: Annotated[
        List[str],
        typer.Option(
            ..., "--path", "-p", help="Room storage path to ingest (repeatable)"
        ),
    ],
    namespace: NamespaceOption = None,
    strategy: Annotated[
        MemoryIngestStrategy,
        typer.Option("--strategy", help="Ingest strategy: heuristic or llm"),
    ] = "heuristic",
    llm_model: Annotated[
        Optional[str], typer.Option("--llm-model", help="Model name for llm strategy")
    ] = None,
    llm_temperature: Annotated[
        Optional[float],
        typer.Option("--llm-temperature", help="Temperature for llm strategy"),
    ] = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    try:
        async with _connected_room_client(project_id=project_id, room=room) as client:
            result = await client.memory.ingest_from_storage(
                name=name,
                paths=list(path),
                namespace=_ns(namespace),
                strategy=strategy,
                llm_model=llm_model,
                llm_temperature=llm_temperature,
            )
            print(_json_dumps(result.model_dump(mode="json"), pretty=pretty))
    except (RoomException, typer.BadParameter, ValidationError) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command("recall", help="Recall entities and relationships from memory.")
async def recall(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    query: Annotated[str, typer.Option(..., "--query", "-q", help="Recall query text")],
    limit: Annotated[int, typer.Option("--limit", help="Max entities to return")] = 5,
    include_relationships: Annotated[
        bool,
        typer.Option(
            "--include-relationships/--no-include-relationships",
            help="Include related edges for recalled entities",
        ),
    ] = True,
    namespace: NamespaceOption = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    try:
        async with _connected_room_client(project_id=project_id, room=room) as client:
            result = await client.memory.recall(
                name=name,
                query=query,
                namespace=_ns(namespace),
                limit=limit,
                include_relationships=include_relationships,
            )
            print(_json_dumps(result.model_dump(mode="json"), pretty=pretty))
    except (RoomException, typer.BadParameter, ValidationError) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command(
    "delete-entities", help="Delete entities (and related edges) from memory."
)
async def delete_entities(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    entity_id: Annotated[
        List[str],
        typer.Option(
            ...,
            "--entity-id",
            "-e",
            help="Entity ID to delete (repeatable)",
        ),
    ],
    namespace: NamespaceOption = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    try:
        async with _connected_room_client(project_id=project_id, room=room) as client:
            result = await client.memory.delete_entities(
                name=name,
                entity_ids=list(entity_id),
                namespace=_ns(namespace),
            )
            print(_json_dumps(result.model_dump(mode="json"), pretty=pretty))
    except (RoomException, typer.BadParameter, ValidationError) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command(
    "delete-relationships", help="Delete relationship edges from memory."
)
async def delete_relationships(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    records_json: Annotated[
        Optional[str],
        typer.Option(
            "--records-json",
            help="JSON array of MemoryRelationshipSelector objects",
        ),
    ] = None,
    records_file: Annotated[
        Optional[str],
        typer.Option(
            "--records-file",
            help="Path to JSON file with relationship selectors",
        ),
    ] = None,
    namespace: NamespaceOption = None,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    try:
        rows = _load_records(records_json=records_json, records_file=records_file)
        selectors = [MemoryRelationshipSelector.model_validate(row) for row in rows]
        async with _connected_room_client(project_id=project_id, room=room) as client:
            result = await client.memory.delete_relationships(
                name=name,
                relationships=selectors,
                namespace=_ns(namespace),
            )
            print(_json_dumps(result.model_dump(mode="json"), pretty=pretty))
    except (RoomException, typer.BadParameter, ValidationError) as ex:
        print(ex)
        raise typer.Exit(1)


@app.async_command("optimize", help="Optimize memory datasets.")
async def optimize(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., "--name", "-m", help="Memory name")],
    namespace: NamespaceOption = None,
    compact: Annotated[
        bool, typer.Option("--compact/--no-compact", help="Compact dataset fragments")
    ] = True,
    cleanup: Annotated[
        bool,
        typer.Option("--cleanup/--no-cleanup", help="Cleanup old dataset versions"),
    ] = True,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print JSON")
    ] = True,
):
    try:
        async with _connected_room_client(project_id=project_id, room=room) as client:
            result = await client.memory.optimize(
                name=name,
                namespace=_ns(namespace),
                compact=compact,
                cleanup=cleanup,
            )
            print(_json_dumps(result.model_dump(mode="json"), pretty=pretty))
    except (RoomException, typer.BadParameter, ValidationError) as ex:
        print(ex)
        raise typer.Exit(1)
