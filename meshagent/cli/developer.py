from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import typer
from rich import print
from meshagent.api import (
    RoomClient,
    WebSocketClientProtocol,
)
from meshagent.api.helpers import websocket_room_url
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_room,
)

app = async_typer.AsyncTyper(help="Developer utilities for a room")


def _otlp_any_value_to_python(value: Any) -> Any:
    if not isinstance(value, dict):
        return value

    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "arrayValue" in value:
        values = value["arrayValue"].get("values", [])
        return [_otlp_any_value_to_python(item) for item in values]
    if "kvlistValue" in value:
        values = value["kvlistValue"].get("values", [])
        return {
            item["key"]: _otlp_any_value_to_python(item.get("value"))
            for item in values
            if isinstance(item, dict) and isinstance(item.get("key"), str)
        }
    if "bytesValue" in value:
        return value["bytesValue"]

    return value


def _otlp_body_to_text(body: Any) -> str:
    value = _otlp_any_value_to_python(body)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _format_otel_timestamp(time_unix_nano: Any) -> str:
    try:
        value = int(str(time_unix_nano))
    except (TypeError, ValueError):
        return "-"

    timestamp = datetime.fromtimestamp(
        value / 1_000_000_000,
        tz=timezone.utc,
    ).astimezone()
    return timestamp.strftime("%Y-%m-%d %H:%M:%S")


def _plain_event_lines(event: Any) -> list[str]:
    if event.type == "otel.log":
        resource_logs = event.data.get("resourceLogs")
        if not isinstance(resource_logs, list):
            return []

        lines: list[str] = []
        for resource_log in resource_logs:
            if not isinstance(resource_log, dict):
                continue
            scope_logs = resource_log.get("scopeLogs", [])
            if not isinstance(scope_logs, list):
                continue
            for scope_log in scope_logs:
                if not isinstance(scope_log, dict):
                    continue
                log_records = scope_log.get("logRecords", [])
                if not isinstance(log_records, list):
                    continue
                for record in log_records:
                    if not isinstance(record, dict):
                        continue
                    timestamp = _format_otel_timestamp(record.get("timeUnixNano"))
                    severity = str(record.get("severityText") or "INFO")
                    body = _otlp_body_to_text(record.get("body"))
                    lines.append(f"{timestamp} {severity:<7} {body}")
        return lines

    return []


@app.async_command("watch", help="Stream developer logs from a room")
async def watch_logs(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    output_format: Annotated[
        Literal["plain", "json"],
        typer.Option("--format", help="Output format"),
    ] = "plain",
):
    """
    Watch logs from the developer feed in the specified room.
    """

    account_client = await get_client()
    try:
        # Resolve project ID (or fetch from the active project if not provided)
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        connection = await account_client.connect_room(project_id=project_id, room=room)

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=connection.jwt,
            )
        ) as client:
            print("[bold cyan]watching enabled. Press Ctrl+C to stop.[/bold cyan]")

            try:
                async for event in client.developer.logs():
                    if output_format == "json":
                        print(
                            f"[magenta]{event.type}[/magenta]: "
                            f"{json.dumps(event.data, indent=2, ensure_ascii=False)}"
                        )
                        continue

                    for line in _plain_event_lines(event):
                        print(line)
            except KeyboardInterrupt:
                print("[bold red]Stopping watch...[/bold red]")

    finally:
        await account_client.close()
