from __future__ import annotations

import copy
import json
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

app = async_typer.AsyncTyper(help="Inspect and update mesh documents in a room")


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
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            )
        )
        await client.__aenter__()
        return account_client, client
    except Exception:
        await account_client.close()
        raise


@app.async_command("show", help="Print the full document JSON")
async def sync_show(
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
