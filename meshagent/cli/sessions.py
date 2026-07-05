import builtins
import json
from datetime import datetime
from typing import Annotated, Any, Optional

import typer
from rich import print
from rich.console import Console

from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import get_client, print_json_table, resolve_project_id

app = async_typer.AsyncTyper(help="Inspect recent sessions and events")
_tree_console = Console(soft_wrap=True)


def _parse_span_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _span_sort_key(span: dict[str, Any]) -> tuple[datetime, str]:
    parsed = _parse_span_time(span.get("created_at"))
    return (parsed or datetime.max, str(span.get("span_id") or ""))


def _format_span_time(value: Any) -> str:
    parsed = _parse_span_time(value)
    if parsed is None:
        return "-"
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _format_span_duration(value: Any) -> str:
    duration_ns = _span_duration_ns(value)
    if duration_ns is None:
        return "-"
    if duration_ns < 1_000:
        return f"{duration_ns}ns"
    if duration_ns < 1_000_000:
        return f"{duration_ns / 1_000:.1f}us"
    if duration_ns < 1_000_000_000:
        return f"{duration_ns / 1_000_000:.1f}ms"
    return f"{duration_ns / 1_000_000_000:.2f}s"


def _span_duration_ns(value: Any) -> int | None:
    try:
        duration_ns = int(value)
    except (TypeError, ValueError):
        return None
    return duration_ns


def _parse_duration_ns(value: str | None) -> int | None:
    if value is None:
        return None
    stripped = value.strip().lower()
    multipliers = {
        "ns": 1,
        "us": 1_000,
        "µs": 1_000,
        "ms": 1_000_000,
        "s": 1_000_000_000,
    }
    for suffix, multiplier in multipliers.items():
        if stripped.endswith(suffix):
            number = stripped[: -len(suffix)].strip()
            return int(float(number) * multiplier)
    return int(float(stripped))


def _span_attributes(span: dict[str, Any]) -> str:
    attributes = span.get("span_attributes")
    if not isinstance(attributes, dict) or not attributes:
        return ""
    return ", ".join(f"{key}={value}" for key, value in sorted(attributes.items()))


def _format_attributes(attributes: Any) -> str:
    if not isinstance(attributes, dict) or not attributes:
        return ""
    return ", ".join(f"{key}={value}" for key, value in sorted(attributes.items()))


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, default=_json_default))


def _format_number(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _format_metric_buckets(metric: dict[str, Any]) -> str:
    bucket_counts = metric.get("bucket_counts")
    explicit_bounds = metric.get("explicit_bounds")
    if not isinstance(bucket_counts, builtins.list) or not bucket_counts:
        return ""
    if not isinstance(explicit_bounds, builtins.list):
        explicit_bounds = []

    bucket_parts: list[str] = []
    for index, count in enumerate(bucket_counts):
        if index < len(explicit_bounds):
            label = f"<={_format_number(explicit_bounds[index])}"
        else:
            label = "+Inf"
        bucket_parts.append(f"{label}:{count}")
    return ", ".join(bucket_parts)


def _format_metric_value(metric: dict[str, Any], *, include_buckets: bool) -> str:
    kind = metric.get("kind")
    if kind == "histogram":
        count = metric.get("count")
        total = metric.get("sum")
        parts = [
            f"count={_format_number(count)}",
            f"sum={_format_number(total)}",
        ]
        if (
            isinstance(count, int | float)
            and count > 0
            and isinstance(total, int | float)
        ):
            parts.append(f"avg={_format_number(total / count)}")
        if metric.get("min") is not None:
            parts.append(f"min={_format_number(metric.get('min'))}")
        if metric.get("max") is not None:
            parts.append(f"max={_format_number(metric.get('max'))}")
        buckets = _format_metric_buckets(metric)
        if include_buckets and buckets:
            parts.append(f"buckets={buckets}")
        return " ".join(parts)
    return _format_number(metric.get("value"))


def _metric_rows(
    metrics: list[dict[str, Any]],
    *,
    include_attrs: bool = False,
    include_buckets: bool = False,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for metric in metrics:
        row = {
            "time": _format_span_time(
                metric.get("ended_at") or metric.get("created_at")
            ),
            "kind": str(metric.get("kind") or "sum"),
            "name": str(metric.get("metric_name") or metric.get("name") or "-"),
            "value": _format_metric_value(metric, include_buckets=include_buckets),
            "unit": str(metric.get("metric_unit") or metric.get("unit") or ""),
        }
        if include_attrs:
            row["attrs"] = _format_attributes(metric.get("metric_attributes"))
        rows.append(row)
    return rows


def _event_log_message(event: dict[str, Any]) -> str:
    data = event.get("data")
    if not isinstance(data, dict):
        return "" if data is None else str(data)
    log = data.get("log")
    if log is None:
        log = data.get("message")
    if log is None:
        log = data.get("body")
    return "" if log is None else str(log)


def _event_log_severity(event: dict[str, Any]) -> str:
    data = event.get("data")
    if not isinstance(data, dict):
        return str(event.get("type") or "-")
    severity = data.get("severity_text")
    if severity is None or severity == "":
        severity = data.get("severity_number")
    if severity is None or severity == "":
        severity = event.get("type")
    return str(severity or "-")


def _event_log_scope(event: dict[str, Any]) -> str:
    data = event.get("data")
    if not isinstance(data, dict):
        return ""
    return str(data.get("scope_name") or "")


def _event_log_attrs(event: dict[str, Any]) -> str:
    data = event.get("data")
    if not isinstance(data, dict):
        return ""
    log_attributes = data.get("log_attributes")
    resource_attributes = data.get("resource_attributes")
    parts = [
        _format_attributes(log_attributes),
        _format_attributes(resource_attributes),
    ]
    return ", ".join(part for part in parts if part)


def _log_rows(
    events: list[dict[str, Any]], *, include_attrs: bool = False
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for event in events:
        row = {
            "time": _format_span_time(event.get("created_at")),
            "severity": _event_log_severity(event),
            "scope": _event_log_scope(event),
            "message": _event_log_message(event),
        }
        if include_attrs:
            row["attrs"] = _event_log_attrs(event)
        rows.append(row)
    return rows


async def _resolve_session_id(
    client: Any,
    *,
    project_id: str,
    session_arg: str | None,
    session_id: str | None,
    room_name: str | None,
) -> str | None:
    if session_arg is not None and session_id is not None and session_arg != session_id:
        raise typer.BadParameter(
            "session argument and --session-id must match when both are provided"
        )
    resolved_session_id = session_id or session_arg
    if resolved_session_id is not None:
        return resolved_session_id

    room_id: str | None = None
    resolved_room_name: str | None = None
    fetch_limit = 25
    if room_name is not None:
        room = await client.get_room(project_id=project_id, name=room_name)
        room_id = room.id
        resolved_room_name = room.name
        fetch_limit = 1000
    recent_sessions = await client.list_recent_sessions(
        project_id=project_id,
        limit=fetch_limit,
        room_id=room_id,
    )
    if resolved_room_name is not None:
        recent_sessions = [
            session
            for session in recent_sessions
            if session.room_name == resolved_room_name
        ]
    if not recent_sessions:
        if resolved_room_name is not None:
            print(f"No recent sessions found for room {resolved_room_name}")
        else:
            print("No recent sessions found")
        return None
    return recent_sessions[0].id


def _span_tree_rows(
    spans: list[dict[str, Any]],
    *,
    trace_id: str | None = None,
    name_filter: str | None = None,
    min_duration_ns: int | None = None,
    include_attrs: bool = False,
) -> list[tuple[str, str, str, str]]:
    normalized_name_filter = name_filter.lower() if name_filter is not None else None
    indexed_spans = [
        (index, span)
        for index, span in enumerate(spans)
        if trace_id is None or span.get("trace_id") == trace_id
    ]
    if normalized_name_filter is not None:
        indexed_spans = [
            (index, span)
            for index, span in indexed_spans
            if normalized_name_filter
            in str(span.get("span_name") or span.get("name") or "").lower()
        ]
    if min_duration_ns is not None:
        indexed_spans = [
            (index, span)
            for index, span in indexed_spans
            if (_span_duration_ns(span.get("duration")) or 0) >= min_duration_ns
        ]
    span_keys = {
        (str(span.get("trace_id") or ""), str(span.get("span_id") or index))
        for index, span in indexed_spans
    }
    children: dict[tuple[str, str], list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []

    for index, span in indexed_spans:
        span_key = (str(span.get("trace_id") or ""), str(span.get("span_id") or index))
        parent_span_id = span.get("parent_span_id")
        parent_key = (
            (span_key[0], str(parent_span_id)) if parent_span_id is not None else None
        )
        if parent_key is not None and parent_key in span_keys:
            children.setdefault(parent_key, []).append(span)
        else:
            roots.append(span)

    for child_spans in children.values():
        child_spans.sort(key=_span_sort_key)
    roots.sort(key=_span_sort_key)

    rows: list[tuple[str, str, str, str]] = []
    visited: set[tuple[str, str]] = set()

    def visit(span: dict[str, Any], depth: int) -> None:
        span_key = (
            str(span.get("trace_id") or ""),
            str(span.get("span_id") or len(visited)),
        )
        if span_key in visited:
            return
        visited.add(span_key)
        rows.append(
            (
                f"{'  ' * depth}{span.get('span_name') or span.get('name') or '-'}",
                _format_span_time(span.get("created_at")),
                _format_span_duration(span.get("duration")),
                _span_attributes(span) if include_attrs else "",
            )
        )
        for child in children.get(span_key, []):
            visit(child, depth + 1)

    for root in roots:
        visit(root, 0)

    return rows


def _span_tree_lines(
    spans: list[dict[str, Any]],
    *,
    trace_id: str | None = None,
    name_filter: str | None = None,
    min_duration_ns: int | None = None,
    include_attrs: bool = False,
) -> list[str]:
    rows = _span_tree_rows(
        spans,
        trace_id=trace_id,
        name_filter=name_filter,
        min_duration_ns=min_duration_ns,
        include_attrs=include_attrs,
    )
    if not rows:
        return []

    name_width = max(len("name"), *(len(row[0]) for row in rows))
    time_width = max(len("time"), *(len(row[1]) for row in rows))
    if include_attrs:
        duration_width = max(len("duration"), *(len(row[2]) for row in rows))
        lines = [
            f"{'name':<{name_width}}  {'time':<{time_width}}  "
            f"{'duration':<{duration_width}}  attrs"
        ]
        lines.extend(
            f"{name:<{name_width}}  {time:<{time_width}}  "
            f"{duration:<{duration_width}}  {attrs}"
            for name, time, duration, attrs in rows
        )
    else:
        lines = [f"{'name':<{name_width}}  {'time':<{time_width}}  duration"]
        lines.extend(
            f"{name:<{name_width}}  {time:<{time_width}}  {duration}"
            for name, time, duration, _ in rows
        )
    return lines


def _print_tree_line(line: str) -> None:
    _tree_console.print(line)


@app.async_command("list", help="List recent sessions")
async def list(
    *,
    project_id: ProjectIdOption,
    limit: Annotated[
        int,
        typer.Option(min=1, help="Maximum sessions to return (server max 1000)"),
    ] = 25,
    room_name: Annotated[
        Optional[str],
        typer.Option("--room", help="Only include sessions for the given room name"),
    ] = None,
    output: OutputFormatOption = "table",
):
    client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id=project_id)
        room_id: str | None = None
        resolved_room_name: str | None = None
        fetch_limit = limit
        if room_name is not None:
            room = await client.get_room(project_id=resolved_project_id, name=room_name)
            room_id = room.id
            resolved_room_name = room.name
            # Older servers ignore room_id on this endpoint, so fetch a larger
            # window and filter client-side for compatibility while the
            # server-side change rolls out.
            fetch_limit = 1000
        sessions = await client.list_recent_sessions(
            project_id=resolved_project_id,
            limit=fetch_limit,
            room_id=room_id,
        )
        if resolved_room_name is not None:
            sessions = [
                session
                for session in sessions
                if session.room_name == resolved_room_name
            ][:limit]
        if not sessions and resolved_room_name is not None:
            print(f"No recent sessions found for room {resolved_room_name}")
            return
        session_rows = [session.model_dump(mode="json") for session in sessions]
        if output == "json":
            _print_json({"sessions": session_rows})
            return
        print_json_table(session_rows)
    finally:
        await client.close()


@app.async_command("get", help="Get events for a session")
async def get(*, project_id: ProjectIdOption, session_id: str):
    client = await get_client()
    try:
        events = await client.list_session_events(
            project_id=await resolve_project_id(project_id=project_id),
            session_id=session_id,
        )
        print_json_table(events, "type", "data")
    finally:
        await client.close()


@app.async_command("kill", help="Forcefully kill a running session instance")
async def kill(
    *,
    project_id: ProjectIdOption,
    session_id: Annotated[
        str,
        typer.Argument(help="Session id to kill"),
    ],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt"),
    ] = False,
):
    if not yes and not typer.confirm(
        f"Forcefully kill session {session_id}?", default=False
    ):
        raise typer.Exit(code=0)

    client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id=project_id)
        await client.kill_session(
            project_id=resolved_project_id,
            session_id=session_id,
        )
        print(f"Kill requested for session {session_id}")
    finally:
        await client.close()


@app.async_command("logs", help="List logs for a session")
async def logs(
    *,
    project_id: ProjectIdOption,
    session_arg: Annotated[
        Optional[str],
        typer.Argument(help="Session id to inspect"),
    ] = None,
    session_id: Annotated[
        Optional[str],
        typer.Option("--session-id", help="Session id to inspect"),
    ] = None,
    room_name: Annotated[
        Optional[str],
        typer.Option("--room", help="Use the most recent session for this room"),
    ] = None,
    output: OutputFormatOption = "table",
    include_attrs: Annotated[
        bool,
        typer.Option("--attrs", help="Include log and resource attributes"),
    ] = False,
):
    client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id=project_id)
        resolved_session_id = await _resolve_session_id(
            client,
            project_id=resolved_project_id,
            session_arg=session_arg,
            session_id=session_id,
            room_name=room_name,
        )
        if resolved_session_id is None:
            return

        events = await client.list_session_events(
            project_id=resolved_project_id,
            session_id=resolved_session_id,
        )
        logs = [event for event in events if event.get("type") == "otel.log"]
        if output == "json":
            _print_json({"logs": logs})
            return
        rows = _log_rows(logs, include_attrs=include_attrs)
        if not rows:
            print(f"No logs found for session {resolved_session_id}")
            return
        cols = (
            ("time", "severity", "scope", "message", "attrs")
            if include_attrs
            else (
                "time",
                "severity",
                "scope",
                "message",
            )
        )
        print_json_table(rows, *cols)
    finally:
        await client.close()


@app.async_command("metrics", help="List metrics for a session")
async def metrics(
    *,
    project_id: ProjectIdOption,
    session_arg: Annotated[
        Optional[str],
        typer.Argument(help="Session id to inspect"),
    ] = None,
    session_id: Annotated[
        Optional[str],
        typer.Option("--session-id", help="Session id to inspect"),
    ] = None,
    room_name: Annotated[
        Optional[str],
        typer.Option("--room", help="Use the most recent session for this room"),
    ] = None,
    output: OutputFormatOption = "table",
    include_attrs: Annotated[
        bool,
        typer.Option("--attrs", help="Include metric attributes"),
    ] = False,
    include_buckets: Annotated[
        bool,
        typer.Option("--buckets", help="Include histogram bucket counts"),
    ] = False,
):
    client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id=project_id)
        resolved_session_id = await _resolve_session_id(
            client,
            project_id=resolved_project_id,
            session_arg=session_arg,
            session_id=session_id,
            room_name=room_name,
        )
        if resolved_session_id is None:
            return

        session_metrics = await client.list_session_metrics(
            project_id=resolved_project_id,
            session_id=resolved_session_id,
        )
        if output == "json":
            _print_json({"metrics": session_metrics})
            return
        rows = _metric_rows(
            session_metrics,
            include_attrs=include_attrs,
            include_buckets=include_buckets,
        )
        if not rows:
            print(f"No metrics found for session {resolved_session_id}")
            return
        cols = (
            ("time", "kind", "name", "value", "unit", "attrs")
            if include_attrs
            else (
                "time",
                "kind",
                "name",
                "value",
                "unit",
            )
        )
        print_json_table(rows, *cols)
    finally:
        await client.close()


@app.async_command("traces", help="List trace spans for a session as a tree")
async def traces(
    *,
    project_id: ProjectIdOption,
    session_arg: Annotated[
        Optional[str],
        typer.Argument(help="Session id to inspect"),
    ] = None,
    session_id: Annotated[
        Optional[str],
        typer.Option("--session-id", help="Session id to inspect"),
    ] = None,
    room_name: Annotated[
        Optional[str],
        typer.Option("--room", help="Use the most recent session for this room"),
    ] = None,
    trace_id: Annotated[
        Optional[str],
        typer.Option("--trace-id", help="Only include spans from this trace"),
    ] = None,
    name_filter: Annotated[
        Optional[str],
        typer.Option("--name", help="Only include spans whose name contains this text"),
    ] = None,
    min_duration: Annotated[
        Optional[str],
        typer.Option(
            "--min-duration",
            help="Only include spans at or above this duration, e.g. 500ms or 2s",
        ),
    ] = None,
    include_attrs: Annotated[
        bool,
        typer.Option("--attrs", help="Include span attributes"),
    ] = False,
    output: OutputFormatOption = "table",
):
    client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id=project_id)
        resolved_session_id = await _resolve_session_id(
            client,
            project_id=resolved_project_id,
            session_arg=session_arg,
            session_id=session_id,
            room_name=room_name,
        )
        if resolved_session_id is None:
            return

        spans = await client.list_session_spans(
            project_id=resolved_project_id,
            session_id=resolved_session_id,
        )
        if output == "json":
            _print_json({"spans": spans})
            return
        lines = _span_tree_lines(
            spans,
            trace_id=trace_id,
            name_filter=name_filter,
            min_duration_ns=_parse_duration_ns(min_duration),
            include_attrs=include_attrs,
        )
        if not lines:
            print(f"No spans found for session {resolved_session_id}")
            return
        for line in lines:
            _print_tree_line(line)
    finally:
        await client.close()
