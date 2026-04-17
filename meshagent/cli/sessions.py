from typing import Annotated, Optional

import typer
from rich import print

from meshagent.cli import async_typer
from meshagent.cli.helper import get_client, print_json_table, resolve_project_id
from meshagent.cli.common_options import ProjectIdOption

app = async_typer.AsyncTyper(help="Inspect recent sessions and events")


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
        print_json_table([session.model_dump(mode="json") for session in sessions])
    finally:
        await client.close()


@app.async_command("show", help="Show events for a session")
async def show(*, project_id: ProjectIdOption, session_id: str):
    client = await get_client()
    try:
        events = await client.list_session_events(
            project_id=await resolve_project_id(project_id=project_id),
            session_id=session_id,
        )
        print_json_table(events, "type", "data")
    finally:
        await client.close()
