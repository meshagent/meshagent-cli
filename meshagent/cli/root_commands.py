import asyncio

import click
from rich import print

from meshagent.api.client import User
from meshagent.cli.version import __version__

_SETUP_WELCOME_PROMPT = (
    "welcome {user_name} to meshagent and let them know they can use "
    "'meshagent ask' to ask questions about meshagent"
)


def _setup_welcome_prompt(*, user_name: str | None) -> str:
    if user_name is None or user_name.strip() == "":
        return (
            "welcome them to meshagent and let them know they can use "
            "'meshagent ask' to ask questions about meshagent"
        )

    return _SETUP_WELCOME_PROMPT.format(user_name=user_name.strip())


def _display_name_from_profile(profile: User) -> str | None:
    parts = []
    if profile.first_name is not None and profile.first_name.strip() != "":
        parts.append(profile.first_name.strip())
    if profile.last_name is not None and profile.last_name.strip() != "":
        parts.append(profile.last_name.strip())

    full_name = " ".join(parts)
    if full_name != "":
        return full_name

    if profile.email.strip() != "":
        return profile.email.strip()

    return None


def _run_async(coro):
    asyncio.run(coro)


@click.command(
    "version",
    help="Print the version",
)
def version_command():
    print(__version__)


@click.command("setup")
@click.option(
    "--api-url",
    type=str,
    default=None,
    help="Persist this API URL on the saved profile and use it for setup login.",
)
def setup_command(api_url: str | None = None):
    """Perform initial login and project/api key activation."""

    async def runner():
        from meshagent.cli import api_keys, ask as ask_module, auth_async, projects
        from meshagent.cli.helper import (
            CustomMeshagentClient,
            get_active_api_key,
            get_active_project,
            get_client,
        )
        from meshagent.cli.local_settings import get_active_profile, resolve_api_url
        from meshagent.cli.tool_integrations import (
            CODEX_DEFAULT_PROFILE_ID,
            configure_codex_integration,
            find_existing_codex_profiles,
            has_claude_code_cli,
            has_codex_cli,
            launch_claude_code,
        )
        from meshagent.cli.tui.setup import (
            SetupProject,
            run_setup_wizard_tui,
        )

        async def list_setup_projects() -> list[SetupProject]:
            client = await get_client()
            try:
                response = await client.list_projects()
            finally:
                await client.close()

            if not isinstance(response, dict):
                return []

            project_rows = response.get("projects", [])
            if not isinstance(project_rows, list):
                return []

            setup_projects: list[SetupProject] = []
            for row in project_rows:
                if not isinstance(row, dict):
                    continue
                project_id = row.get("id")
                if not isinstance(project_id, str) or project_id.strip() == "":
                    continue
                project_name = row.get("name")
                resolved_name = (
                    project_name
                    if isinstance(project_name, str) and project_name.strip() != ""
                    else project_id
                )
                setup_projects.append(SetupProject(id=project_id, name=resolved_name))

            return setup_projects

        async def create_project_from_name(project_name: str) -> str:
            client = await get_client()
            try:
                created = await client.create_project(project_name)
            finally:
                await client.close()

            created_project_id = (
                created.get("id") if isinstance(created, dict) else None
            )
            if (
                not isinstance(created_project_id, str)
                or created_project_id.strip() == ""
            ):
                raise RuntimeError("Project creation did not return a valid id.")
            return created_project_id

        async def activate_project(project_id: str) -> str:
            activated_project_id = await projects.activate(
                project_id, interactive=False, return_project_id=True
            )
            if activated_project_id is None:
                raise RuntimeError("Unable to activate selected project.")
            return activated_project_id

        async def has_active_api_key(project_id: str) -> bool:
            return await get_active_api_key(project_id=project_id) is not None

        async def create_and_activate_api_key(
            project_id: str, api_key_name: str
        ) -> None:
            await api_keys.create(
                project_id=project_id,
                activate=True,
                silent=True,
                name=api_key_name,
            )

        async def configure_codex_profile(profile_id: str) -> None:
            configure_codex_integration(
                profile_id=profile_id,
                api_url=resolve_api_url(),
            )

        async def list_existing_codex_profiles(project_id: str) -> list[str]:
            return find_existing_codex_profiles(
                project_id=project_id,
                api_url=resolve_api_url(),
            )

        current_user_name: str | None = None
        has_authenticated_session = False
        if await auth_async.get_access_token() is not None:
            has_authenticated_session = True
            active_profile = get_active_profile()
            if active_profile is not None:
                current_user_name = active_profile.display_name()
                if active_profile.email is not None:
                    resolved_email = active_profile.email.strip()
                    if resolved_email != "" and resolved_email != current_user_name:
                        current_user_name = f"{current_user_name} ({resolved_email})"
        codex_available = has_codex_cli()
        claude_code_available = has_claude_code_cli()

        result = await run_setup_wizard_tui(
            login_operation=lambda status_handler: auth_async.login(
                status_handler=status_handler,
                print_status=False,
                api_url=api_url,
            ),
            list_projects_operation=list_setup_projects,
            create_project_operation=create_project_from_name,
            activate_project_operation=activate_project,
            has_active_api_key_operation=has_active_api_key,
            create_api_key_operation=create_and_activate_api_key,
            active_project_id=await get_active_project(),
            has_authenticated_session=has_authenticated_session,
            authenticated_user_name=current_user_name,
            has_codex_cli=codex_available,
            has_claude_code_cli=claude_code_available,
            list_existing_codex_profiles_operation=(
                list_existing_codex_profiles if codex_available else None
            ),
            configure_codex_profile_operation=(
                configure_codex_profile if codex_available else None
            ),
            default_codex_profile_name=CODEX_DEFAULT_PROFILE_ID,
        )

        if result.status != "completed" and result.message is not None:
            print(result.message)
            return

        if result.status == "completed":
            profile: User | None = None
            access_token = await auth_async.get_access_token()
            if access_token is not None:
                client = CustomMeshagentClient(
                    base_url=resolve_api_url(),
                    token=access_token,
                )
                try:
                    profile = User.model_validate(await client.get_user_profile("me"))
                except Exception:
                    profile = None
                finally:
                    await client.close()
            await ask_module.ask(
                project_id=None,
                message=_setup_welcome_prompt(
                    user_name=(
                        _display_name_from_profile(profile)
                        if profile is not None
                        else None
                    ),
                ),
            )
            if result.launch_claude_code:
                exit_code = launch_claude_code(
                    project_id=result.project_id,
                    api_url=resolve_api_url(),
                )
                if exit_code != 0:
                    print(f"Claude Code exited with status {exit_code}.")

    _run_async(runner())
