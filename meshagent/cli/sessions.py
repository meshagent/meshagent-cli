from meshagent.cli import async_typer
from meshagent.cli.helper import get_client, print_json_table, resolve_project_id

app = async_typer.AsyncTyper()


@app.async_command("list")
async def list(*, project_id: str = None):
    client = await get_client()
    sessions = await client.list_recent_sessions(
        project_id=await resolve_project_id(project_id=project_id)
    )
    print_json_table(sessions["sessions"])
    await client.close()


@app.async_command("show")
async def show(*, project_id: str = None, session_id: str):
    client = await get_client()
    events = await client.list_session_events(
        project_id=await resolve_project_id(project_id=project_id),
        session_id=session_id,
    )
    print_json_table(events["events"], "type", "data")
    await client.close()
