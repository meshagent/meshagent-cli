import typer

from meshagent.cli import async_typer
from meshagent.cli import auth_async
from meshagent.cli.helper import get_active_project, get_client

app = async_typer.AsyncTyper(help="Authenticate to meshagent")


@app.async_command("login")
async def login():
    await auth_async.login()

    project_id = await get_active_project()
    if project_id is None:
        print(
            "You have been logged in, but you haven"
            't activated a project yet, list your projects with "meshagent project list" and then activate one with "meshagent project activate PROJECT_ID"'
        )


@app.async_command("logout")
async def logout():
    await auth_async.logout()


@app.async_command("whoami")
async def whoami():
    access_token = await auth_async.get_access_token()
    if access_token is None:
        typer.echo("Not logged in")
        return

    client = await get_client()
    try:
        profile = await client.get_user_profile("me")
    finally:
        await client.close()

    first_name = profile.get("first_name")
    last_name = profile.get("last_name")
    email = profile.get("email")

    full_name = " ".join(
        part
        for part in (
            first_name.strip() if isinstance(first_name, str) else None,
            last_name.strip() if isinstance(last_name, str) else None,
        )
        if part
    )

    if full_name and isinstance(email, str) and email.strip():
        typer.echo(f"{full_name} <{email.strip()}>")
    elif full_name:
        typer.echo(full_name)
    elif isinstance(email, str) and email.strip():
        typer.echo(email.strip())
    else:
        typer.echo(str(profile.get("id", "Authenticated")))
