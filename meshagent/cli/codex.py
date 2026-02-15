import os
import shlex
from pathlib import Path
from typing import Annotated, List, Optional

import typer
from rich import print

from meshagent.agents.config import RulesConfig
from meshagent.api import (
    ApiScope,
    ParticipantToken,
    RequiredSchema,
    RequiredToolkit,
    RoomClient,
    WebSocketClientProtocol,
)
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    get_client,
    resolve_key,
    resolve_project_id,
    resolve_room,
)
from meshagent.cli.host import get_deferred

app = async_typer.AsyncTyper(help="Join a codex chatbot to a room")


def _room_openai_base_url(*, room_name: str) -> str:
    room_url = websocket_room_url(room_name=room_name)

    if room_url.startswith("wss:"):
        room_url = "https:" + room_url.removeprefix("wss:")
    elif room_url.startswith("ws:"):
        room_url = "http:" + room_url.removeprefix("ws:")

    return f"{room_url}/openai/v1"


def build_codex_chatbot(
    *,
    model: str,
    rule: list[str],
    toolkit: list[str],
    schema: list[str],
    rules_file: Optional[list[str]] = None,
    skill_dirs: Optional[list[str]] = None,
    command: Optional[str] = None,
    ws_url: Optional[str] = None,
    working_directory: Optional[str] = None,
    approval_policy: Optional[str] = None,
    sandbox_policy: Optional[str] = None,
    app_server_env: Optional[dict[str, str]] = None,
):
    try:
        from meshagent.codex import CodexChatBot
    except ImportError as exc:
        raise typer.BadParameter(
            "meshagent-codex is required for this command. Install the package and try again."
        ) from exc

    requirements = []

    for toolkit_name in toolkit:
        requirements.append(RequiredToolkit(name=toolkit_name))

    for schema_name in schema:
        requirements.append(RequiredSchema(name=schema_name))

    client_rules: dict[str, list[str]] = {}
    if rules_file is not None:
        for rules_path in rules_file:
            try:
                with open(Path(os.path.expanduser(rules_path)).resolve(), "r") as f:
                    rules_config = RulesConfig.parse(f.read())
                    if rules_config.rules is not None:
                        rule.extend(rules_config.rules)
                    if rules_config.client_rules is not None:
                        client_rules.update(rules_config.client_rules)
            except FileNotFoundError:
                print(f"[yellow]rules file not found at {rules_path}[/yellow]")

    class CustomCodexChatBot(CodexChatBot):
        def __init__(self):
            super().__init__(
                requires=requirements,
                rules=rule if len(rule) > 0 else None,
                client_rules=client_rules if len(client_rules) > 0 else None,
                skill_dirs=skill_dirs if len(skill_dirs or []) > 0 else None,
                model=model,
                command=command,
                ws_url=ws_url,
                cwd=working_directory,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
                app_server_env=app_server_env,
            )

        async def create_thread_context(
            self,
            *,
            path: str,
            thread,
            participants,
            event_handler,
        ):
            from meshagent.cli.helper import init_context_from_spec

            context = await super().create_thread_context(
                path=path,
                thread=thread,
                participants=participants,
                event_handler=event_handler,
            )
            await init_context_from_spec(context.chat)
            return context

    return CustomCodexChatBot


@app.async_command("join")
async def join(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: Annotated[str, typer.Option(..., help="Role to use for the agent token")] = (
        "agent"
    ),
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to run")
    ] = None,
    token_from_env: Annotated[
        Optional[str],
        typer.Option(
            "--token-from-env",
            help="Name of environment variable containing a MeshAgent token",
        ),
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    key: Annotated[
        Optional[str], typer.Option("--key", help="An api key to sign the token with")
    ] = None,
):
    if command is not None and ws_url is not None:
        print(
            "[yellow]Both --command and --ws-url were provided. Using --ws-url and ignoring --command.[/yellow]"
        )

    key = await resolve_key(project_id=project_id, key=key)

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)

        token_env = token_from_env or "MESHAGENT_TOKEN"
        jwt = os.getenv(token_env)
        if jwt is None:
            if token_from_env:
                print(
                    f"[bold red]{token_env} environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)
            if agent_name is None:
                print(
                    f"[bold red]--agent-name must be specified when the {token_env} environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)

            token = ParticipantToken(name=agent_name)
            token.add_api_grant(ApiScope.agent_default())
            token.add_role_grant(role=role)
            token.add_room_grant(room_name)
            jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]", flush=True)

        app_server_env = None
        if ws_url is None:
            if command is None:
                openai_base_url = _room_openai_base_url(room_name=room_name)
                command = (
                    "codex app-server "
                    "-c model_providers.openai.name='OpenAI' "
                    "-c "
                    f"model_providers.openai.base_url={shlex.quote(openai_base_url)}"
                )

            app_server_env = {"OPENAI_API_KEY": jwt}

        CustomCodexChatBot = build_codex_chatbot(
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            working_directory=working_directory,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
        )
        bot = CustomCodexChatBot()

        if get_deferred():
            from meshagent.cli.host import agents

            agents.append((bot, jwt))
        else:
            async with RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(room_name=room_name),
                    token=jwt,
                )
            ) as client:
                await bot.start(room=client)
                try:
                    print(
                        f"[bold green]Open the studio to interact with your agent: {meshagent_base_url().replace('api.', 'studio.')}/projects/{project_id}/rooms/{client.room_name}[/bold green]",
                        flush=True,
                    )
                    await client.protocol.wait_for_close()
                except KeyboardInterrupt:
                    await bot.stop()
    finally:
        await account_client.close()
