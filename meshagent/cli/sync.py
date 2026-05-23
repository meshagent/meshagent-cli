from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import uuid
from pathlib import Path
from typing import Any, Optional

import typer
from rich import print

from meshagent.api import RoomClient, RoomException, WebSocketClientProtocol
from meshagent.api.helpers import websocket_room_url
from meshagent.api.runtime import RuntimeDocument
from meshagent.api.schema import MeshSchema
from meshagent.api.schema_document import Element
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import get_client, resolve_project_id, resolve_room

app = async_typer.LazyTyper(help="Inspect and update mesh documents in a room")
import_app = async_typer.AsyncTyper(help="Import external exports into room documents")
app.add_lazy_command(
    name="import",
    module="meshagent.cli.sync",
    attribute="import_app",
    help="Import external exports into room documents",
)


def _parse_json_arg(json_str: Optional[str], *, name: str) -> Any:
    if json_str is None:
        return None
    try:
        return json.loads(json_str)
    except Exception as exc:
        raise typer.BadParameter(f"Invalid JSON for {name}: {exc}") from exc


def _load_json_file(path: Optional[Path], *, name: str) -> Any:
    if path is None:
        return None
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        raise typer.BadParameter(f"Unable to read {name} from {path}: {exc}") from exc


def _resolve_local_file(path: Path, *, name: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise typer.BadParameter(f"{name} file does not exist: {resolved}")
    if not resolved.is_file():
        raise typer.BadParameter(f"{name} path must be a file: {resolved}")
    return resolved


def _conversation_guid_for_thread_name(*, conversation_id: str) -> str:
    normalized_id = conversation_id.strip()
    if normalized_id == "":
        raise typer.BadParameter("conversation id was empty")
    try:
        return str(uuid.UUID(normalized_id))
    except ValueError:
        # Produce a stable GUID for non-UUID source ids.
        return str(uuid.uuid5(uuid.NAMESPACE_URL, normalized_id))


def _thread_path_for_conversation(
    *,
    thread_dir: str,
    conversation_id: str,
    occurrence: int = 1,
) -> str:
    normalized_dir = thread_dir.strip().rstrip("/")
    if normalized_dir == "":
        raise typer.BadParameter("--thread-dir must not be empty")
    if occurrence < 1:
        raise typer.BadParameter("occurrence must be >= 1")
    normalized_name = _conversation_guid_for_thread_name(
        conversation_id=conversation_id
    )

    if occurrence == 1:
        return f"{normalized_dir}/{normalized_name}.thread"
    return f"{normalized_dir}/{normalized_name} {occurrence}.thread"


def _thread_list_path_for_thread_dir(*, thread_dir: str) -> str:
    normalized_dir = thread_dir.strip().rstrip("/")
    if normalized_dir == "":
        raise typer.BadParameter("--thread-dir must not be empty")
    return f"{normalized_dir}/index.threadl"


def _thread_list_entry_by_path(
    *, path: str, entries: list[Element]
) -> Optional[Element]:
    for entry in entries:
        if entry.tag_name != "thread":
            continue
        if entry.get_attribute("path") == path:
            return entry
    return None


def _upsert_thread_list_entry(
    *,
    thread_list_root: Element,
    path: str,
    name: str,
    created_at: str,
    modified_at: str,
) -> None:
    entries = thread_list_root.get_children()
    existing = _thread_list_entry_by_path(path=path, entries=entries)
    if existing is None:
        thread_list_root.append_child(
            tag_name="thread",
            attributes={
                "name": name,
                "path": path,
                "created_at": created_at,
                "modified_at": modified_at,
            },
        )
        return

    existing.set_attribute("name", name)
    existing.set_attribute("path", path)
    existing.set_attribute("created_at", created_at)
    existing.set_attribute("modified_at", modified_at)


def _decode_pointer(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise typer.BadParameter(f"Invalid JSON pointer: {path}")
    tokens = path.lstrip("/").split("/")
    return [token.replace("~1", "/").replace("~0", "~") for token in tokens]


def _resolve_target(document: Any, tokens: list[str]) -> Any:
    current = document
    for token in tokens:
        if isinstance(current, list):
            if token == "-":
                raise typer.BadParameter("JSON pointer '-' is not valid here")
            try:
                index = int(token)
            except ValueError as exc:
                raise typer.BadParameter(f"Invalid list index: {token}") from exc
            try:
                current = current[index]
            except IndexError as exc:
                raise typer.BadParameter(f"List index out of range: {token}") from exc
        elif isinstance(current, dict):
            if token not in current:
                raise typer.BadParameter(f"Path not found: {token}")
            current = current[token]
        else:
            raise typer.BadParameter("JSON pointer targets a non-container value")
    return current


def _resolve_parent(document: Any, tokens: list[str]) -> tuple[Any, str]:
    if not tokens:
        raise typer.BadParameter("JSON pointer must target a child value")
    parent = _resolve_target(document, tokens[:-1]) if len(tokens) > 1 else document
    return parent, tokens[-1]


def _add_value(document: Any, tokens: list[str], value: Any) -> Any:
    if not tokens:
        return value
    parent, key = _resolve_parent(document, tokens)
    if isinstance(parent, list):
        if key == "-":
            parent.append(value)
        else:
            try:
                index = int(key)
            except ValueError as exc:
                raise typer.BadParameter(f"Invalid list index: {key}") from exc
            if index < 0 or index > len(parent):
                raise typer.BadParameter(f"List index out of range: {key}")
            parent.insert(index, value)
    elif isinstance(parent, dict):
        parent[key] = value
    else:
        raise typer.BadParameter("JSON pointer targets a non-container value")
    return document


def _replace_value(document: Any, tokens: list[str], value: Any) -> Any:
    if not tokens:
        return value
    parent, key = _resolve_parent(document, tokens)
    if isinstance(parent, list):
        try:
            index = int(key)
        except ValueError as exc:
            raise typer.BadParameter(f"Invalid list index: {key}") from exc
        if index < 0 or index >= len(parent):
            raise typer.BadParameter(f"List index out of range: {key}")
        parent[index] = value
    elif isinstance(parent, dict):
        if key not in parent:
            raise typer.BadParameter(f"Path not found: {key}")
        parent[key] = value
    else:
        raise typer.BadParameter("JSON pointer targets a non-container value")
    return document


def _remove_value(document: Any, tokens: list[str]) -> tuple[Any, Any]:
    parent, key = _resolve_parent(document, tokens)
    if isinstance(parent, list):
        try:
            index = int(key)
        except ValueError as exc:
            raise typer.BadParameter(f"Invalid list index: {key}") from exc
        if index < 0 or index >= len(parent):
            raise typer.BadParameter(f"List index out of range: {key}")
        value = parent.pop(index)
    elif isinstance(parent, dict):
        if key not in parent:
            raise typer.BadParameter(f"Path not found: {key}")
        value = parent.pop(key)
    else:
        raise typer.BadParameter("JSON pointer targets a non-container value")
    return document, value


def _apply_json_patch(document: Any, patch_ops: list[dict[str, Any]]) -> Any:
    updated = copy.deepcopy(document)

    for op in patch_ops:
        operation = op.get("op")
        path = op.get("path")
        if operation is None or path is None:
            raise typer.BadParameter("Patch entries must include 'op' and 'path'")

        tokens = _decode_pointer(path)

        if operation == "add":
            updated = _add_value(updated, tokens, op.get("value"))
        elif operation == "replace":
            updated = _replace_value(updated, tokens, op.get("value"))
        elif operation == "remove":
            if not tokens:
                raise typer.BadParameter("Cannot remove the document root")
            updated, _ = _remove_value(updated, tokens)
        elif operation == "move":
            from_path = op.get("from")
            if from_path is None:
                raise typer.BadParameter("Move operations require 'from'")
            from_tokens = _decode_pointer(from_path)
            updated, value = _remove_value(updated, from_tokens)
            updated = _add_value(updated, tokens, value)
        elif operation == "copy":
            from_path = op.get("from")
            if from_path is None:
                raise typer.BadParameter("Copy operations require 'from'")
            from_tokens = _decode_pointer(from_path)
            value = copy.deepcopy(_resolve_target(updated, from_tokens))
            updated = _add_value(updated, tokens, value)
        elif operation == "test":
            expected = op.get("value")
            actual = _resolve_target(updated, tokens)
            if actual != expected:
                raise typer.BadParameter(f"Test operation failed at {path}")
        else:
            raise typer.BadParameter(f"Unsupported patch op: {operation}")

    return updated


def _extract_element_payload(element_json: dict) -> tuple[str, dict]:
    if len(element_json) != 1:
        raise typer.BadParameter("Element JSON must have a single key")
    tag_name = next(iter(element_json))
    payload = element_json[tag_name] or {}
    if not isinstance(payload, dict):
        raise typer.BadParameter("Element payload must be an object")
    return tag_name, payload


def _apply_element_json(element: Element, element_json: dict) -> None:
    tag_name, payload = _extract_element_payload(element_json)
    if tag_name != element.tag_name:
        raise typer.BadParameter(
            f"Patch root tag '{tag_name}' does not match document root '{element.tag_name}'"
        )

    child_property = element.schema.child_property_name
    children_json = []
    if child_property is not None and child_property in payload:
        children_json = payload.get(child_property) or []
        if not isinstance(children_json, list):
            raise typer.BadParameter("Child property must be a list")

    attributes = {key: value for key, value in payload.items() if key != child_property}

    for key in list(element._data["attributes"].keys()):
        if key == "$id":
            continue
        if key not in attributes:
            element._remove_attribute(key)

    for key, value in attributes.items():
        element.set_attribute(key, value)

    if child_property is None:
        if children_json:
            raise typer.BadParameter("Element does not support children")
        return

    for child in list(element.get_children()):
        if isinstance(child, Element):
            child.delete()

    for child_json in children_json:
        element.append_json(child_json)


def _apply_document_json(doc: RuntimeDocument, updated_json: dict) -> None:
    _apply_element_json(doc.root, updated_json)


def _render_json(payload: Any, pretty: bool) -> None:
    indent = 2 if pretty else None
    print(json.dumps(payload, indent=indent))


async def _connect_room(project_id: ProjectIdOption, room: RoomOption):
    account_client = await get_client()
    room_name = resolve_room(room)
    if not room_name:
        print("[red]Room name is required.[/red]")
        raise typer.Exit(1)

    try:
        project_id = await resolve_project_id(project_id=project_id)
        connection = await account_client.connect_room(
            project_id=project_id, room=room_name
        )
        client = RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        )
        await client.__aenter__()
        return account_client, client
    except Exception:
        await account_client.close()
        raise


@app.async_command("get", help="Print the full document JSON")
async def sync_get(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    path: str,
    include_ids: bool = typer.Option(
        False, "--include-ids", help="Include $id attributes in output"
    ),
    pretty: bool = typer.Option(True, "--pretty/--compact", help="Pretty-print JSON"),
):
    account_client, client = await _connect_room(project_id, room)
    try:
        doc = await client.sync.open(path=path, create=False)
        try:
            payload = doc.root.to_json(include_ids=include_ids)
            _render_json(payload, pretty)
        finally:
            await client.sync.close(path=path)
    except RoomException as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@app.async_command("grep", help="Search the document for matching content")
async def sync_grep(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    path: str,
    pattern: str = typer.Argument(..., help="Regex pattern to match"),
    ignore_case: bool = typer.Option(False, "--ignore-case", help="Ignore case"),
    before: int = typer.Option(0, "--before", min=0, help="Include siblings before"),
    after: int = typer.Option(0, "--after", min=0, help="Include siblings after"),
    include_ids: bool = typer.Option(
        False, "--include-ids", help="Include $id attributes in output"
    ),
    pretty: bool = typer.Option(True, "--pretty/--compact", help="Pretty-print JSON"),
):
    account_client, client = await _connect_room(project_id, room)
    try:
        doc = await client.sync.open(path=path, create=False)
        try:
            matches = doc.root.grep(
                pattern, ignore_case=ignore_case, before=before, after=after
            )
            payload = [match.to_json(include_ids=include_ids) for match in matches]
            _render_json(payload, pretty)
        finally:
            await client.sync.close(path=path)
    except RoomException as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@app.async_command("inspect", help="Print the document schema JSON")
async def sync_inspect(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    path: str,
    pretty: bool = typer.Option(True, "--pretty/--compact", help="Pretty-print JSON"),
):
    account_client, client = await _connect_room(project_id, room)
    try:
        doc = await client.sync.open(path=path, create=False)
        try:
            _render_json(doc.schema.to_json(), pretty)
        finally:
            await client.sync.close(path=path)
    except RoomException as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@app.async_command("create", help="Create a new document at a path")
async def sync_create(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    path: str,
    schema: Path = typer.Option(..., "--schema", help="Schema JSON file"),
    json_payload: Optional[str] = typer.Option(
        None, "--json", help="Initial JSON payload"
    ),
    json_file: Optional[Path] = typer.Option(
        None, "--json-file", help="Path to initial JSON payload"
    ),
):
    initial_json = _load_json_file(json_file, name="json")
    if initial_json is None:
        initial_json = _parse_json_arg(json_payload, name="json")

    schema_json = _load_json_file(schema, name="schema")
    if schema_json is None:
        raise typer.BadParameter("--schema is required")

    account_client, client = await _connect_room(project_id, room)
    try:
        if await client.storage.exists(path=path):
            print(f"[red]Document already exists at {path}.[/red]")
            raise typer.Exit(1)

        await client.sync.open(
            path=path,
            create=True,
            initial_json=initial_json,
            schema=MeshSchema.from_json(schema_json),
        )
        await client.sync.close(path=path)
        print(f"[green]Created document at {path}[/green]")
    except RoomException as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@app.async_command("update", help="Apply a JSON patch to a document")
async def sync_update(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    path: str,
    patch: Optional[str] = typer.Option(None, "--patch", help="JSON patch array"),
    patch_file: Optional[Path] = typer.Option(
        None, "--patch-file", help="Path to JSON patch array"
    ),
):
    patch_ops = _load_json_file(patch_file, name="patch")
    if patch_ops is None:
        patch_ops = _parse_json_arg(patch, name="patch")
    if patch_ops is None:
        raise typer.BadParameter("Provide --patch or --patch-file")
    if not isinstance(patch_ops, list):
        raise typer.BadParameter("Patch must be a JSON array")

    account_client, client = await _connect_room(project_id, room)
    try:
        doc = await client.sync.open(path=path, create=False)
        try:
            current_json = doc.root.to_json()
            updated_json = _apply_json_patch(current_json, patch_ops)
            if not isinstance(updated_json, dict):
                raise typer.BadParameter("Patch must produce a JSON object")
            _apply_document_json(doc, updated_json)
            print(f"[green]Updated document at {path}[/green]")
        finally:
            await client.sync.close(path=path)
    except RoomException as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@import_app.async_command(
    "threads",
    help="Import Anthropic Claude GDPR conversations into room thread documents.",
)
async def sync_import_threads(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    file: Path = typer.Option(
        ...,
        "--file",
        "-f",
        help="Path to Claude GDPR conversations.json",
    ),
    thread_dir: str = typer.Option(
        ".threads/anthropic",
        "--thread-dir",
        help="Directory where imported .thread files will be stored",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Overwrite existing imported thread files",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        min=1,
        help="Maximum number of conversations to import",
    ),
    user_name: str = typer.Option(
        "human",
        "--user-name",
        "--human-name",
        help="Author name used for user/human messages",
    ),
    assistant_name: str = typer.Option(
        "assistant",
        "--assistant-name",
        help="Author name used for assistant messages",
    ),
    include_empty_messages: bool = typer.Option(
        False,
        "--include-empty-messages",
        help="Include messages that normalize to empty text",
    ),
):
    try:
        try:
            from meshagent.agents import thread_list_schema, thread_schema
            from meshagent.anthropic import AnthropicThreadAdapter
        except Exception as exc:
            print(
                "[red]This command requires meshagent-agents and meshagent-anthropic to be installed.[/red]"
            )
            raise typer.Exit(1) from exc

        source_file = _resolve_local_file(file, name="Conversations")
        payload = _load_json_file(source_file, name="conversations")
        if not isinstance(payload, list):
            raise typer.BadParameter(
                f"Conversations export must be a JSON array: {source_file}"
            )

        adapter = AnthropicThreadAdapter(
            human_author_name=user_name,
            assistant_author_name=assistant_name,
            include_empty_messages=include_empty_messages,
        )

        account_client, client = await _connect_room(project_id, room)
        imported = 0
        skipped_existing = 0
        skipped_invalid = 0
        processed = 0
        thread_list_path = _thread_list_path_for_thread_dir(thread_dir=thread_dir)
        thread_list_doc = None
        path_occurrence_by_base = dict[str, int]()

        try:
            thread_list_doc = await client.sync.open(
                path=thread_list_path,
                schema=thread_list_schema,
            )
            for index, raw_conversation in enumerate(payload):
                if limit is not None and processed >= limit:
                    break
                processed += 1

                if not isinstance(raw_conversation, dict):
                    skipped_invalid += 1
                    print(
                        f"[yellow]Skipping conversations[{index}] because it is not an object.[/yellow]"
                    )
                    continue

                try:
                    conversation = adapter.conversation_from_export(
                        raw_conversation=raw_conversation
                    )
                except ValueError as exc:
                    skipped_invalid += 1
                    print(
                        f"[yellow]Skipping conversations[{index}] due to invalid structure: {exc}[/yellow]"
                    )
                    continue

                thread_guid = _conversation_guid_for_thread_name(
                    conversation_id=conversation.conversation_id
                )
                base_thread_path = _thread_path_for_conversation(
                    thread_dir=thread_dir,
                    conversation_id=conversation.conversation_id,
                )
                occurrence = path_occurrence_by_base.get(base_thread_path, 0) + 1
                path_occurrence_by_base[base_thread_path] = occurrence
                thread_path = _thread_path_for_conversation(
                    thread_dir=thread_dir,
                    conversation_id=conversation.conversation_id,
                    occurrence=occurrence,
                )

                if await client.storage.exists(path=thread_path):
                    if not overwrite:
                        skipped_existing += 1
                        continue
                    await client.storage.delete(thread_path)

                thread_json = adapter.to_thread_json(conversation=conversation)
                thread = thread_json.get("thread")
                if isinstance(thread, dict):
                    thread["name"] = thread_guid
                await client.sync.create(
                    path=thread_path,
                    json=thread_json,
                    schema=thread_schema,
                )
                now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                source_created_at = conversation.created_at.strip()
                source_modified_at = conversation.updated_at.strip()
                created_at = source_created_at if source_created_at != "" else now
                modified_at = (
                    source_modified_at if source_modified_at != "" else created_at
                )
                source_name = conversation.name.strip()
                list_name = source_name if source_name != "" else thread_guid
                _upsert_thread_list_entry(
                    thread_list_root=thread_list_doc.root,
                    path=thread_path,
                    name=list_name,
                    created_at=created_at,
                    modified_at=modified_at,
                )
                imported += 1
        finally:
            if thread_list_doc is not None:
                await client.sync.close(path=thread_list_path)
            await client.__aexit__(None, None, None)
            await account_client.close()

        print(
            (
                "[green]Imported[/green] "
                f"{imported} thread(s); "
                f"skipped existing: {skipped_existing}; "
                f"skipped invalid: {skipped_invalid}."
            )
        )
    except (RoomException, typer.BadParameter) as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
