import json
import os
from typing import Annotated, Any, Awaitable, Callable, Literal, Optional

import typer
from rich import print

from meshagent.api import RoomException
from meshagent.api.client import NotFoundError
from meshagent.api.managed_agents import ManagedAgentSpec
from meshagent.agents.chat_client import ChatThreadSession, WebSocketChatClient
from meshagent.agents.messages import (
    AgentMessage,
    AgentModelChanged,
    AgentTextContentDelta,
    ModelsResponse,
    StartThread,
)
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import get_client, print_json_table, resolve_project_id

app = async_typer.AsyncTyper(
    help="Create, list, and manage managed agents in a project"
)
secret_app = async_typer.AsyncTyper(help="Manage secrets for a managed agent")
app.add_typer(secret_app, name="secret", help="Manage secrets for a managed agent")


def _maybe_parse_json_object(label: str, value: Optional[str]) -> Optional[dict]:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RoomException(f"Invalid {label} JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RoomException(f"Invalid {label} JSON: expected an object")
    return parsed


def _maybe_parse_string_dict_json(
    label: str, value: Optional[str]
) -> Optional[dict[str, str]]:
    parsed = _maybe_parse_json_object(label, value)
    if parsed is None:
        return None

    output: dict[str, str] = {}
    for key, item in parsed.items():
        if not isinstance(key, str):
            raise RoomException(f"Invalid {label} JSON: all keys must be strings")
        if not isinstance(item, str):
            raise RoomException(f"Invalid {label} JSON: all values must be strings")
        output[key] = item
    return output


def _parse_secret_data(data: str) -> bytes:
    parsed = _maybe_parse_json_object("data", data)
    return json.dumps(parsed, separators=(",", ":")).encode("utf-8")


async def _resolve_agent_id_or_fail(
    account_client,
    *,
    project_id: str,
    agent_id: Optional[str],
    agent_name: Optional[str],
) -> str:
    if agent_id:
        return agent_id
    if not agent_name:
        raise RoomException("You must provide either --id or --name.")
    agent = await account_client.get_agent(project_id=project_id, name=agent_name)
    return agent.id


async def _close_agent_use_chat_session(session: ChatThreadSession | None) -> None:
    if session is None:
        return
    try:
        await session.__aexit__(None, None, None)
    except Exception:
        pass


class _AgentUseSession:
    def __init__(
        self,
        *,
        chat_client: ChatThreadSession,
        current_working_directory: str | None = None,
    ) -> None:
        self._chat_client = chat_client
        self._current_working_directory = os.path.abspath(
            current_working_directory or os.getcwd()
        )
        self._thread_generation = 0
        current_model = chat_client.current_model
        self._output_modalities: tuple[Literal["text", "audio"], ...] = (
            tuple(
                output
                for output in current_model.output_modalities
                if output in ("text", "audio")
            )
            if current_model is not None
            else ("text",)
        )
        if len(self._output_modalities) == 0:
            self._output_modalities = ("text",)
        self._session = self._build_session()
        self._sync_turn_output_modalities()

    def _build_session(self):
        from meshagent.cli import ask as ask_module

        return ask_module._AgentMessageSession(
            client=self._chat_client,
            model=None,
            current_working_directory=self._current_working_directory,
            local_participant_name=self._chat_client.local_participant_name,
            model_provider=lambda: self._chat_client.current_model,
            start_thread_callback=self._start_thread,
        )

    @property
    def thread_generation(self) -> int:
        return self._thread_generation

    @property
    def current_working_directory(self) -> str:
        return self._session.current_working_directory

    @property
    def thread_status_text(self) -> str | None:
        return self._session.thread_status_text

    @property
    def current_model(self) -> AgentModelChanged | None:
        return self._chat_client.current_model

    @property
    def thread_id(self) -> str:
        return self._chat_client.thread_path

    @property
    def has_thread_path(self) -> bool:
        return self._chat_client.has_thread_path

    @property
    def models_response(self) -> ModelsResponse | None:
        return self._chat_client.models_response

    @property
    def output_modalities(self) -> tuple[Literal["text", "audio"], ...]:
        return self._output_modalities

    @property
    def output_modalities_label(self) -> str:
        return "+".join(self._output_modalities)

    @property
    def queued_message_labels(self) -> tuple[str, ...]:
        return self._session.queued_message_labels

    @property
    def image_dataset_client(self):
        return self._session.image_dataset_client

    @property
    def messages(self):
        return self._session.messages

    async def _start_thread(self, start_thread: StartThread) -> ChatThreadSession:
        pending_session = self._chat_client
        await pending_session.close(close_client=False)

        def _adopt_pending_session(new_session: ChatThreadSession) -> None:
            self._chat_client = new_session
            self._session.replace_client(new_session)
            self._sync_turn_output_modalities()

        new_session = await pending_session.client.start_thread(
            start_thread,
            local_participant_name=pending_session.local_participant_name,
            close_client_on_close=True,
            on_pending_session=_adopt_pending_session,
        )
        self._chat_client = new_session
        self._session.replace_client(new_session)
        self._sync_turn_output_modalities()
        return new_session

    async def new_thread(self) -> None:
        pending_session = self._chat_client
        await self._session.close(close_client=False)
        await pending_session.close(close_client=False)
        new_session = ChatThreadSession(
            client=pending_session.client,
            thread_path=None,
            local_participant_name=pending_session.local_participant_name,
            close_client_on_close=True,
        )
        self._chat_client = new_session
        self._session = self._build_session()
        self._sync_turn_output_modalities()
        self._thread_generation += 1

    async def close(self) -> None:
        await self._session.close(close_client=False)
        await self._chat_client.close(close_client=True)

    async def ask(
        self,
        *,
        prompt: str,
        on_message: Callable[[AgentMessage], Awaitable[None] | None] | None = None,
    ) -> str:
        return await self._session.ask(prompt=prompt, on_message=on_message)

    def steer(
        self,
        *,
        prompt: str,
        on_accepted: Callable[[], Awaitable[None] | None] | None = None,
        on_applied: Callable[[], Awaitable[None] | None] | None = None,
        on_rejected: Callable[[RoomException], Awaitable[None] | None] | None = None,
    ) -> str | None:
        return self._session.steer(
            prompt=prompt,
            on_accepted=on_accepted,
            on_applied=on_applied,
            on_rejected=on_rejected,
        )

    def interrupt(self) -> bool:
        return self._session.interrupt()

    async def request_models(self) -> ModelsResponse:
        response = await self._chat_client.request_models()
        self._sync_turn_output_modalities()
        return response

    def select_model(self, model: AgentModelChanged) -> None:
        self._chat_client.select_model(model)
        self._output_modalities = self._supported_selected_output_modalities(
            tuple(
                output
                for output in model.output_modalities
                if output in ("text", "audio")
            )
        )
        self._sync_turn_output_modalities()

    def set_output_modalities(
        self, output_modalities: tuple[Literal["text", "audio"], ...]
    ) -> None:
        self._output_modalities = self._supported_selected_output_modalities(
            output_modalities
        )
        self._sync_turn_output_modalities()

    def toggle_output_modalities(self) -> tuple[Literal["text", "audio"], ...]:
        modalities = self._selected_model_modalities()
        if self._output_modalities == ("text",) and "audio" in modalities:
            self._output_modalities = ("audio",)
        else:
            self._output_modalities = ("text",)
        self._output_modalities = self._supported_selected_output_modalities(
            self._output_modalities
        )
        self._sync_turn_output_modalities()
        return self._output_modalities

    def _sync_turn_output_modalities(self) -> None:
        output_modalities = self._supported_selected_output_modalities(
            self._output_modalities
        )
        if output_modalities == self._output_modalities:
            self._session.set_output_modalities(output_modalities)
            current_model = self._chat_client.current_model
            if current_model is not None:
                self._chat_client.select_model(
                    current_model.model_copy(
                        update={"output_modalities": list(self._output_modalities)}
                    )
                )
            return
        self._session.set_output_modalities(None)

    def _selected_model_modalities(self) -> tuple[Literal["text", "audio"], ...]:
        from meshagent.cli import process as process_module

        model_info = process_module._model_info_for_current_selection(
            response=self.models_response,
            current_model=self.current_model,
        )
        if model_info is None:
            return ("text",)
        modalities = tuple(
            output for output in model_info.modalities if output in ("text", "audio")
        )
        return modalities or ("text",)

    def _supported_selected_output_modalities(
        self, output_modalities: tuple[Literal["text", "audio"], ...]
    ) -> tuple[Literal["text", "audio"], ...]:
        supported = self._selected_model_modalities()
        selected = tuple(output for output in output_modalities if output in supported)
        if len(selected) == 0:
            return ("text",)
        return (selected[0],)

    async def change_model(
        self,
        *,
        provider: str | None,
        model: str | None,
        voice: str | None = None,
    ) -> AgentModelChanged:
        changed = await self._chat_client.change_model(
            provider=provider,
            model=model,
            voice=voice,
        )
        self._output_modalities = self._supported_selected_output_modalities(
            tuple(
                output
                for output in changed.output_modalities
                if output in ("text", "audio")
            )
        )
        self._sync_turn_output_modalities()
        return changed


async def _open_agent_use_chat_session(
    *,
    account_client: Any,
    project_id: str,
    agent_name: str,
    thread_path: str | None,
    load: bool,
    since_turn: str | None,
) -> ChatThreadSession:
    connection = await account_client.connect_agent(
        project_id=project_id,
        agent=agent_name,
    )
    websocket_client = WebSocketChatClient(
        url=connection.agent_url,
        headers={"Authorization": f"Bearer {connection.jwt}"},
    )
    chat_session: ChatThreadSession | None = None
    try:
        await websocket_client.__aenter__()
        normalized_thread_path = thread_path.strip() if thread_path is not None else ""
        if normalized_thread_path != "":
            chat_session = ChatThreadSession(
                client=websocket_client,
                thread_path=normalized_thread_path,
                local_participant_name="you",
                close_client_on_close=True,
            )
            await chat_session.open(
                load=True if load else None,
                since_turn=since_turn,
            )
        else:
            chat_session = ChatThreadSession(
                client=websocket_client,
                thread_path=None,
                local_participant_name="you",
                close_client_on_close=True,
            )
        return chat_session
    except Exception:
        await _close_agent_use_chat_session(chat_session)
        if chat_session is None:
            await websocket_client.__aexit__(None, None, None)
        raise


async def _run_agent_use_tui(
    *,
    account_client: Any,
    project_id: str,
    agent_name: str,
    thread_path: str | None,
    message: str | None,
    load: bool,
    since_turn: str | None,
    output: Literal["text", "json"],
) -> None:
    from meshagent.cli import ask as ask_module
    from meshagent.cli import process as process_module

    async def _handle_model_command(command: str) -> str | None:
        if session is None:
            raise RoomException("agent use session not started")
        if command.strip().split()[0:1] != ["/new"] and not session.has_thread_path:
            return "Start the conversation before changing models."
        return await process_module._handle_process_model_command(
            command,
            session=session,
        )

    chat_session: ChatThreadSession | None = None
    session: _AgentUseSession | None = None
    try:
        chat_session = await _open_agent_use_chat_session(
            account_client=account_client,
            project_id=project_id,
            agent_name=agent_name,
            thread_path=thread_path,
            load=load,
            since_turn=since_turn,
        )
        session = _AgentUseSession(chat_client=chat_session)
        if chat_session.has_thread_path:
            await process_module._request_initial_models(session=session)

        if message is not None:

            def _write_message(agent_message: AgentMessage) -> None:
                if isinstance(agent_message, AgentTextContentDelta):
                    typer.echo(agent_message.text, nl=False)

            response_text = await session.ask(
                prompt=message,
                on_message=_write_message if output == "text" else None,
            )
            if output == "json":
                typer.echo(
                    json.dumps(
                        {
                            "thread_path": session.thread_id,
                            "response": response_text,
                        },
                        indent=2,
                    )
                )
            else:
                typer.echo()
            return

        await ask_module._run_ask_tui(
            model="remote",
            session=session,
            title=f"meshagent agents use: {agent_name}",
            assistant_name=agent_name,
            command_handler=_handle_model_command,
            model_label_provider=lambda: process_module._current_model_label(
                current_model=session.current_model if session is not None else None,
                fallback="remote",
            ),
            command_options_provider=lambda prompt: (
                process_module._process_command_options(
                    prompt,
                    response=session.models_response if session is not None else None,
                    current_model=session.current_model
                    if session is not None
                    else None,
                    current_output_modalities=(
                        session.output_modalities if session is not None else ("text",)
                    ),
                )
            ),
            output_label_provider=lambda: (
                session.output_modalities_label if session is not None else "text"
            ),
        )
    finally:
        if session is not None:
            await session.close()
        await _close_agent_use_chat_session(chat_session)


@app.async_command("create", help="Create a managed agent in the project.")
async def agent_create_command(
    *,
    project_id: ProjectIdOption,
    configuration: Annotated[
        str, typer.Option(..., "--configuration", "-c", help="ManagedAgentSpec JSON")
    ],
    thread_isolation: Annotated[
        Optional[Literal["global", "participant"]],
        typer.Option(
            "--thread-isolation",
            help="Thread isolation mode for the managed agent",
        ),
    ] = None,
    if_not_exists: Annotated[
        bool, typer.Option(help="Do not error if the agent already exists")
    ] = False,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        configuration_obj = ManagedAgentSpec.model_validate(
            _maybe_parse_json_object("configuration", configuration)
        )
        if thread_isolation is not None:
            configuration_obj = configuration_obj.model_copy(
                update={"thread_isolation": thread_isolation}
            )

        print(f"[bold green]Creating agent {configuration_obj.name}[/bold green]")
        agent = await account_client.create_agent(
            project_id=project_id,
            configuration=configuration_obj,
            if_not_exists=if_not_exists,
        )
        print(json.dumps(agent.model_dump(mode="json"), indent=2))
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("delete", help="Delete a managed agent from the project.")
async def agent_delete_command(
    *,
    project_id: ProjectIdOption,
    id: Annotated[Optional[str], typer.Option(help="Agent ID (preferred)")] = None,
    name: Annotated[Optional[str], typer.Option(help="Agent name")] = None,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        agent_id = await _resolve_agent_id_or_fail(
            account_client, project_id=project_id, agent_id=id, agent_name=name
        )
        print(f"[bold yellow]Deleting agent id={agent_id}...[/bold yellow]")
        await account_client.delete_agent(project_id=project_id, agent_id=agent_id)
        print("[bold cyan]Agent deleted.[/bold cyan]")
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("update", help="Update a managed agent configuration.")
async def agent_update_command(
    *,
    project_id: ProjectIdOption,
    id: Annotated[Optional[str], typer.Option(help="Agent ID (preferred)")] = None,
    name: Annotated[Optional[str], typer.Option(help="Current agent name")] = None,
    configuration: Annotated[
        str,
        typer.Option(..., "--configuration", "-c", help="ManagedAgentSpec JSON"),
    ],
    thread_isolation: Annotated[
        Optional[Literal["global", "participant"]],
        typer.Option(
            "--thread-isolation",
            help="Thread isolation mode for the managed agent",
        ),
    ] = None,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        agent_id = await _resolve_agent_id_or_fail(
            account_client, project_id=project_id, agent_id=id, agent_name=name
        )
        configuration_obj = ManagedAgentSpec.model_validate(
            _maybe_parse_json_object("configuration", configuration)
        )
        if thread_isolation is not None:
            configuration_obj = configuration_obj.model_copy(
                update={"thread_isolation": thread_isolation}
            )

        print(f"[bold green]Updating agent id={agent_id}...[/bold green]")
        await account_client.update_agent(
            project_id=project_id,
            agent_id=agent_id,
            configuration=configuration_obj,
        )
        print("[bold cyan]Agent updated.[/bold cyan]")
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("list", help="List managed agents in the project.")
async def agent_list_command(
    *,
    project_id: ProjectIdOption,
    o: OutputFormatOption = "table",
    count: Annotated[
        int, typer.Option("--count", help="Max agents to return", min=1, max=500)
    ] = 100,
    limit: Annotated[
        Optional[int],
        typer.Option(
            "--limit", help="Max agents to return", min=1, max=500, hidden=True
        ),
    ] = None,
    offset: Annotated[int, typer.Option(help="Offset for pagination", min=0)] = 0,
    order_by: Annotated[
        str, typer.Option(help='Order by column (e.g. "agent_name", "created_at")')
    ] = "agent_name",
    filter: Annotated[
        Optional[str],
        typer.Option("--filter", help="Lowercase contains filter for agent names"),
    ] = None,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        agents = await account_client.list_agents(
            project_id=project_id,
            limit=limit if limit is not None else count,
            offset=offset,
            order_by=order_by,
            filter=filter,
        )
        output = [
            {
                "id": agent.id,
                "name": agent.name,
                "configuration": agent.configuration.model_dump(mode="json"),
            }
            for agent in agents
        ]
        if o == "json":
            print(json.dumps(output, indent=2))
        elif output:
            print_json_table(output, "id", "name")
        else:
            print("No agents found.")
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("get", help="Get a managed agent configuration.")
async def agent_get_command(
    *,
    project_id: ProjectIdOption,
    name: Annotated[str, typer.Option(..., help="Agent name")],
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        print(f"[bold green]Fetching agent '{name}'...[/bold green]")
        agent = await account_client.get_agent(project_id=project_id, name=name)
        print(json.dumps(agent.model_dump(mode="json"), indent=2))
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("use", help="Use a managed agent over its websocket connection.")
async def agent_use_command(
    agent: Annotated[Optional[str], typer.Argument(help="Agent name")] = None,
    *,
    project_id: ProjectIdOption,
    name: Annotated[Optional[str], typer.Option("--name", help="Agent name")] = None,
    thread_path: Annotated[
        Optional[str],
        typer.Option("--thread-path", help="Thread path to open"),
    ] = None,
    load: Annotated[
        bool,
        typer.Option("--load", help="Replay persisted thread messages when opening"),
    ] = False,
    since_turn: Annotated[
        Optional[str],
        typer.Option(
            "--since-turn",
            help="Replay persisted thread messages starting with this turn id",
        ),
    ] = None,
    message: Annotated[
        Optional[str],
        typer.Option(
            "--message", "-m", help="Send one message without opening the TUI"
        ),
    ] = None,
    o: Annotated[
        Literal["text", "json"],
        typer.Option("--output", "-o", help="Output format for one-shot messages"),
    ] = "text",
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        agent_name = name if name is not None else agent
        if agent_name is None or agent_name.strip() == "":
            print("[bold red]Agent name required. Pass AGENT or --name.[/bold red]")
            raise typer.Exit(1)
        await _run_agent_use_tui(
            account_client=account_client,
            project_id=project_id,
            agent_name=agent_name.strip(),
            thread_path=thread_path,
            message=message,
            load=load,
            since_turn=since_turn,
            output=o,
        )
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@secret_app.async_command("list")
async def agent_secret_list_command(
    *,
    project_id: ProjectIdOption,
    id: Annotated[Optional[str], typer.Option(help="Agent ID (preferred)")] = None,
    name: Annotated[Optional[str], typer.Option(help="Agent name")] = None,
    o: OutputFormatOption = "table",
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        agent_id = await _resolve_agent_id_or_fail(
            account_client, project_id=project_id, agent_id=id, agent_name=name
        )
        secrets = await account_client.list_agent_secrets(
            project_id=project_id, agent_id=agent_id
        )
        output = [secret.model_dump(mode="json") for secret in secrets]
        if o == "json":
            print(json.dumps(output, indent=2))
        elif output:
            print_json_table(output, "id", "name", "type")
        else:
            print("No agent secrets found.")
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@secret_app.async_command("get")
async def agent_secret_get_command(
    secret_id: Annotated[str, typer.Argument(help="Secret ID")],
    *,
    project_id: ProjectIdOption,
    id: Annotated[Optional[str], typer.Option(help="Agent ID (preferred)")] = None,
    name: Annotated[Optional[str], typer.Option(help="Agent name")] = None,
    o: OutputFormatOption = "json",
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        agent_id = await _resolve_agent_id_or_fail(
            account_client, project_id=project_id, agent_id=id, agent_name=name
        )
        secret = await account_client.get_agent_secret(
            project_id=project_id, agent_id=agent_id, secret_id=secret_id
        )
        payload = secret.model_dump(mode="json")
        if o == "json":
            print(json.dumps(payload, indent=2))
        else:
            print_json_table([payload], "id", "name", "type")
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@secret_app.async_command("set")
async def agent_secret_set_command(
    secret_id: Annotated[str, typer.Argument(help="Secret ID")],
    *,
    project_id: ProjectIdOption,
    id: Annotated[Optional[str], typer.Option(help="Agent ID (preferred)")] = None,
    name: Annotated[Optional[str], typer.Option(help="Agent name")] = None,
    secret_name: Annotated[
        Optional[str], typer.Option("--secret-name", help="Display name")
    ] = None,
    type: Annotated[str, typer.Option("--type", help="Secret content type")] = "keys",
    data: Annotated[
        str,
        typer.Option(
            "--data",
            help='Secret data as a JSON object, for example \'{"OPENAI_API_KEY":"..."}\'',
        ),
    ],
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        agent_id = await _resolve_agent_id_or_fail(
            account_client, project_id=project_id, agent_id=id, agent_name=name
        )
        secret_data = _parse_secret_data(data)
        try:
            await account_client.update_agent_secret(
                project_id=project_id,
                agent_id=agent_id,
                secret_id=secret_id,
                name=secret_name or secret_id,
                type=type,
                data=secret_data,
            )
            print(f"[green]Updated agent secret:[/] {secret_id}")
        except NotFoundError:
            created_id = await account_client.create_agent_secret(
                project_id=project_id,
                agent_id=agent_id,
                secret_id=secret_id,
                name=secret_name or secret_id,
                type=type,
                data=secret_data,
            )
            print(f"[green]Created agent secret:[/] {created_id}")
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@secret_app.async_command("delete")
async def agent_secret_delete_command(
    secret_id: Annotated[str, typer.Argument(help="Secret ID")],
    *,
    project_id: ProjectIdOption,
    id: Annotated[Optional[str], typer.Option(help="Agent ID (preferred)")] = None,
    name: Annotated[Optional[str], typer.Option(help="Agent name")] = None,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        agent_id = await _resolve_agent_id_or_fail(
            account_client, project_id=project_id, agent_id=id, agent_name=name
        )
        await account_client.delete_agent_secret(
            project_id=project_id, agent_id=agent_id, secret_id=secret_id
        )
        print("[bold cyan]Agent secret deleted.[/bold cyan]")
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()
