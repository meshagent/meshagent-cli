from meshagent.cli import async_typer
from meshagent.api.client import RoomSession
from meshagent.cli.helper import get_client, print_json_table, resolve_project_id
from meshagent.cli.common_options import ProjectIdOption

app = async_typer.AsyncTyper(help="Inspect recent sessions and events")


@app.async_command("list", help="List recent sessions")
async def list(*, project_id: ProjectIdOption):
    client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id=project_id)
        sessions: list[RoomSession] = await client.list_recent_sessions(
            project_id=resolved_project_id
        )
        print_json_table([session.model_dump(mode="json") for session in sessions])
    finally:
        await client.close()


@app.async_command("show", help="Show events for a session")
async def show(*, project_id: ProjectIdOption, session_id: str):
    client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id=project_id)
        events = await client.list_session_events(
            project_id=resolved_project_id,
            session_id=session_id,
        )
        print_json_table(events, "type", "data")
    finally:
        await client.close()
