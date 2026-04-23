import sys
import typer
from typing import Annotated

from meshagent.api.client import User
from meshagent.cli import async_typer
from meshagent.cli import auth_async
from meshagent.cli.helper import CustomMeshagentClient, get_active_project
from meshagent.cli.local_settings import (
    DEFAULT_API_URL,
    SavedProfileRecord,
    list_saved_profiles,
    resolve_api_url,
    switch_active_profile,
)

app = async_typer.AsyncTyper(help="Authenticate to meshagent")


@app.async_command("login")
async def login(
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            help="Persist this API URL on the saved profile and use it for this login.",
        ),
    ] = None,
):
    await auth_async.login(api_url=api_url)

    project_id = await get_active_project()
    if project_id is None:
        print(
            "You have been logged in, but you haven"
            't activated a project yet, list your projects with "meshagent project list" and then activate one with "meshagent project activate PROJECT_ID"'
        )


@app.async_command("logout")
async def logout():
    await auth_async.logout()


def _format_user_identity(profile: User) -> str:
    parts = []
    first_name = profile.first_name.strip() if profile.first_name is not None else ""
    last_name = profile.last_name.strip() if profile.last_name is not None else ""
    if first_name != "":
        parts.append(first_name)
    if last_name != "":
        parts.append(last_name)

    full_name = " ".join(parts)
    if full_name != "" and profile.email.strip() != "":
        return f"{full_name} <{profile.email.strip()}>"
    if full_name != "":
        return full_name
    if profile.email.strip() != "":
        return profile.email.strip()
    return profile.id


def _format_saved_profile(profile: SavedProfileRecord) -> str:
    active_marker = "*" if profile.is_active else " "
    api_url = profile.api_url or DEFAULT_API_URL
    return (
        f"{active_marker} {profile.profile.display_name()} "
        f"[{profile.user_id}] @ {api_url}"
    )


def _should_launch_switch_tui(
    *,
    profile: str | None,
    stdin_is_tty: bool,
    stdout_is_tty: bool,
) -> bool:
    return profile is None and stdin_is_tty and stdout_is_tty


async def _run_auth_switch_tui(
    *,
    saved_profiles: list[SavedProfileRecord],
):
    from meshagent.cli.tui.auth_switch import run_auth_switch_tui

    return await run_auth_switch_tui(saved_profiles=saved_profiles)


@app.async_command("switch")
async def switch(
    profile: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Saved profile user id or email. If omitted, saved profiles are "
                "listed or an interactive picker is shown in a TTY."
            ),
        ),
    ] = None,
):
    selected_profile_selector = profile
    if selected_profile_selector is None:
        saved_profiles = list_saved_profiles()
        if len(saved_profiles) == 0:
            typer.echo("No saved local profiles.")
            return

        if _should_launch_switch_tui(
            profile=selected_profile_selector,
            stdin_is_tty=sys.stdin.isatty(),
            stdout_is_tty=sys.stdout.isatty(),
        ):
            result = await _run_auth_switch_tui(saved_profiles=saved_profiles)
            if result.status != "completed" or result.selected_profile is None:
                if result.message is not None:
                    typer.echo(result.message)
                return

            selected_profile_selector = result.selected_profile.user_id
        else:
            for saved_profile in saved_profiles:
                typer.echo(_format_saved_profile(saved_profile))
            return

    try:
        selected_profile = switch_active_profile(selected_profile_selector)
    except LookupError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(f"Active profile: {_format_saved_profile(selected_profile)}")


@app.async_command("whoami")
async def whoami():
    access_token = await auth_async.get_access_token()
    if access_token is None:
        typer.echo("Not logged in")
        return

    client = CustomMeshagentClient(
        base_url=resolve_api_url(),
        token=access_token,
    )
    try:
        profile = User.model_validate(await client.get_user_profile("me"))
    finally:
        await client.close()

    typer.echo(_format_user_identity(profile))


@app.async_command("token")
async def token():
    access_token = await auth_async.get_access_token()
    if access_token is None:
        typer.echo("Not logged in", err=True)
        raise typer.Exit(code=1)

    typer.echo(access_token)
