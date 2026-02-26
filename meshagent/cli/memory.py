import base64
import json as _json
import mimetypes
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
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
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
