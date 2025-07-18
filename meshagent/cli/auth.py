import typer

from meshagent.cli import async_typer
from meshagent.cli import auth_async
from meshagent.cli.helper import get_active_project

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
    _, s = await auth_async.session()
    typer.echo(s.user.email if s else "Not logged in")
