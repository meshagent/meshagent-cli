# meshagent/cli/containers.py
from __future__ import annotations

import asyncio
import io
import os
import sys
import tarfile
import time
from pathlib import Path

import typer
from rich import print
from typing import Annotated, Optional, List, Dict

from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, ApiKeyIdOption, RoomOption
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_api_key,
    resolve_room,
)
from meshagent.api import (
    RoomClient,
    ParticipantToken,
    WebSocketClientProtocol,
    ApiScope,
)
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.api.room_server_client import (
    BuildSource,
    BuildSourceGit,
    BuildSourceContext,
    BuildSourceRoom,
    DockerSecret,
)

app = async_typer.AsyncTyper(help="Manage containers and images inside a room")

# -------------------------
# Helpers
# -------------------------


def _parse_keyvals(items: List[str]) -> Dict[str, str]:
    """
    Parse ["KEY=VAL", "FOO=BAR"] -> {"KEY":"VAL", "FOO":"BAR"}
    """
    out: Dict[str, str] = {}
    for s in items or []:
        if "=" not in s:
            raise typer.BadParameter(f"Expected KEY=VALUE, got: {s}")
        k, v = s.split("=", 1)
        out[k] = v
    return out


def _parse_ports(items: List[str]) -> Dict[int, int]:
    """
    Parse ["8080:3000", "9999:9999"] as CONTAINER:HOST -> {8080:3000, 9999:9999}
    (Matches server's expectation: container_port -> host_port.)
    """
    out: Dict[int, int] = {}
    for s in items or []:
        if ":" not in s:
            raise typer.BadParameter(f"Expected CONTAINER:HOST, got: {s}")
        c, h = s.split(":", 1)
        try:
            cp, hp = int(c), int(h)
        except ValueError:
            raise typer.BadParameter(f"Ports must be integers, got: {s}")
        out[cp] = hp
    return out


def _parse_creds(items: List[str]) -> List[DockerSecret]:
    """
    Parse creds given as:
      --cred username,password
      --cred registry,username,password
    """
    creds: List[DockerSecret] = []
    for s in items or []:
        parts = [p.strip() for p in s.split(",")]
        if len(parts) == 2:
            u, p = parts
            creds.append(DockerSecret(username=u, password=p))
        elif len(parts) == 3:
            r, u, p = parts
            creds.append(DockerSecret(registry=r, username=u, password=p))
        else:
            raise typer.BadParameter(
                f"Invalid --cred format: {s}. Use username,password or registry,username,password"
            )
    return creds


def _tarfilter_strip_macos(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """
    Filter to make Linux-friendly tarballs:
    - Drop AppleDouble files (._*)
    - Reset uid/gid/uname/gname
    - Clear pax headers
    """
    base = os.path.basename(ti.name)
    if base.startswith("._"):
        return None
    ti.uid = 0
    ti.gid = 0
    ti.uname = ""
    ti.gname = ""
    ti.pax_headers = {}
    # Preserve mode & type; set a stable-ish mtime
    if ti.mtime is None:
        ti.mtime = int(time.time())
    return ti


def _make_targz_from_dir(path: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(path, arcname=".", filter=_tarfilter_strip_macos)
    return buf.getvalue()


def _make_targz_with_dockerfile_text(text: str) -> bytes:
    b = text.encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        ti = tarfile.TarInfo("Dockerfile")
        ti.size = len(b)
        ti.mtime = int(time.time())
        ti.mode = 0o644
        tar.addfile(ti, io.BytesIO(b))
    return buf.getvalue()


async def _drain_stream_plain(stream, *, show_progress: bool = True):
    async def _logs():
        async for line in stream.logs():
            # Server emits plain lines; print as-is
            if line is not None:
                print(line)

    async def _prog():
        if not show_progress:
            async for _ in stream.progress():
                pass
            return
        async for p in stream.progress():
            if p is None:
                return
            msg = p.message or ""
            # Show progress if we have numbers, else just the message.
            if p.current is not None and p.total:
                print(f"[cyan]{msg} ({p.current}/{p.total})[/cyan]")
            elif msg:
                print(f"[cyan]{msg}[/cyan]")

    t1 = asyncio.create_task(_logs())
    t2 = asyncio.create_task(_prog())
    try:
        return await stream
    finally:
        await asyncio.gather(t1, t2, return_exceptions=True)


async def _drain_stream_pretty(stream):
    import asyncio
    import math
    from rich.table import Column
    from rich.live import Live
    from rich.panel import Panel
    from rich.console import Group
    from rich.text import Text
    from rich.progress import (
        Progress,
        TextColumn,
        BarColumn,
        TimeElapsedColumn,
        ProgressColumn,
        SpinnerColumn,
    )

    class MaybeMofN(ProgressColumn):
        def render(self, task):
            import math
            from rich.text import Text

            def _fmt_bytes(n):
                if n is None:
                    return ""
                n = float(n)
                units = ["B", "KiB", "MiB", "GiB", "TiB"]
                i = 0
                while n >= 1024 and i < len(units) - 1:
                    n /= 1024
                    i += 1
                return f"{n:.1f} {units[i]}"

            if task.total == 0 or math.isinf(task.total):
                return Text("")
            return Text(f"{_fmt_bytes(task.completed)} / {_fmt_bytes(task.total)}")

    class MaybeBarColumn(BarColumn):
        def __init__(
            self,
            *,
            bar_width: int | None = 28,
            hide_when_unknown: bool = False,
            column_width: int | None = None,
            **kwargs,
        ):
            # bar_width controls the drawn bar size; None = flex
            super().__init__(bar_width=bar_width, **kwargs)
            self.hide_when_unknown = hide_when_unknown
            self.column_width = column_width  # fix the table column if set

        def get_table_column(self) -> Column:
            if self.column_width is None:
                # default behavior (may flex depending on layout)
                return Column(no_wrap=True)
            return Column(
                width=self.column_width,
                min_width=self.column_width,
                max_width=self.column_width,
                no_wrap=True,
                overflow="crop",
                justify="left",
            )

        def render(self, task):
            if task.total is None or task.total == 0 or math.isinf(task.total):
                return Text("")  # hide bar for indeterminate tasks
            return super().render(task)

    class MaybeETA(ProgressColumn):
        """Show ETA only if total is known."""

        _elapsed = TimeElapsedColumn()

        def render(self, task):
            # You can swap this to a TimeRemainingColumn() if you prefer,
            # but hide when total is unknown.
            if task.total == 0 or math.isinf(task.total):
                return Text("")
            return self._elapsed.render(task)  # or TimeRemainingColumn().render(task)

    progress = Progress(
        SpinnerColumn(),
        TextColumn(
            "[bold]{task.description}",
            table_column=Column(ratio=8, no_wrap=True, overflow="ellipsis"),
        ),
        MaybeMofN(table_column=Column(ratio=2, no_wrap=True, overflow="ellipsis")),
        MaybeETA(table_column=Column(ratio=1, no_wrap=True, overflow="ellipsis")),
        MaybeBarColumn(pulse_style="cyan", bar_width=20, hide_when_unknown=True),
        # pulses automatically if total=None
        transient=False,  # we’re inside Live; we’ll hide tasks ourselves
        expand=True,
    )

    logs_tail: list[str] = []
    tasks: dict[str, int] = {}  # layer -> task_id

    def render():
        tail = "\n".join(logs_tail[-12:]) or "waiting…"
        return Group(
            progress,
            Panel(tail, title="logs", border_style="cyan", height=12),
        )

    async def _logs():
        async for line in stream.logs():
            if line:
                logs_tail.append(line.strip())

    async def _prog():
        async for p in stream.progress():
            layer = p.layer or "overall"
            if layer not in tasks:
                tasks[layer] = progress.add_task(
                    p.message or layer, total=p.total if p.total is not None else 0
                )
            task_id = tasks[layer]

            updates = {}
            # Keep total=None for pulsing; only set if we get a real number.
            if p.total is not None and not math.isinf(p.total):
                updates["total"] = p.total
            if p.current is not None:
                updates["completed"] = p.current
            if p.message:
                updates["description"] = p.message
            if updates:
                progress.update(task_id, **updates)

    with Live(render(), refresh_per_second=10) as live:

        async def _refresh():
            while True:
                live.update(render())
                await asyncio.sleep(0.1)

        t_logs = asyncio.create_task(_logs())
        t_prog = asyncio.create_task(_prog())
        t_ui = asyncio.create_task(_refresh())
        try:
            result = await stream
            return result
        finally:
            # Hide any still-visible tasks (e.g., indeterminate ones with total=None)
            for tid in list(tasks.values()):
                progress.update(tid, visible=False)
            live.update(render())

            for t in (t_logs, t_prog):
                await t

            t_ui.cancel()


async def _with_client(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    api_key_id: ApiKeyIdOption,
    name: str,
    role: str,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)
        room = resolve_room(room)

        key = (
            await account_client.decrypt_project_api_key(
                project_id=project_id, id=api_key_id
            )
        )["token"]

        token = ParticipantToken(
            name=name, project_id=project_id, api_key_id=api_key_id
        )
        token.add_api_grant(ApiScope.agent_default())
        token.add_role_grant(role=role)
        token.add_room_grant(room)

        print("[bold green]Connecting to room...[/bold green]", flush=True)
        proto = WebSocketClientProtocol(
            url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
            token=token.to_jwt(token=key),
        )
        client_cm = RoomClient(protocol=proto)
        await client_cm.__aenter__()
        return account_client, client_cm
    except Exception:
        await account_client.close()
        raise


# -------------------------
# Top-level: ps / stop / logs / run
# -------------------------


@app.async_command("ps")
async def list_containers(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption = None,
    api_key_id: ApiKeyIdOption = None,
    name: Annotated[str, typer.Option(...)] = "cli",
    role: Annotated[str, typer.Option(...)] = "user",
    output: Annotated[Optional[str], typer.Option(help="json | table")] = "json",
):
    account_client, client = await _with_client(
        project_id=project_id, room=room, api_key_id=api_key_id, name=name, role=role
    )
    try:
        containers = await client.containers.list()
        if output == "table":
            from rich.table import Table
            from rich.console import Console

            table = Table(title="Containers")
            table.add_column("ID", style="cyan")
            table.add_column("Image")
            table.add_column("Status")
            for c in containers:
                table.add_row(c.id, c.image or "", c.status or "")
            Console().print(table)
        else:
            # default json-ish
            print([c.model_dump() for c in containers])
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@app.async_command("stop")
async def stop_container(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption = None,
    api_key_id: ApiKeyIdOption = None,
    id: Annotated[str, typer.Option(..., help="Container ID")],
    name: Annotated[str, typer.Option(...)] = "cli",
    role: Annotated[str, typer.Option(...)] = "user",
):
    account_client, client = await _with_client(
        project_id=project_id, room=room, api_key_id=api_key_id, name=name, role=role
    )
    try:
        await client.containers.stop(container_id=id)
        print("[green]Stopped[/green]")
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@app.async_command("logs")
async def container_logs(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption = None,
    api_key_id: ApiKeyIdOption = None,
    id: Annotated[str, typer.Option(..., help="Container ID")],
    follow: Annotated[bool, typer.Option("--follow/--no-follow")] = False,
    name: Annotated[str, typer.Option(...)] = "cli",
    role: Annotated[str, typer.Option(...)] = "user",
):
    account_client, client = await _with_client(
        project_id=project_id, room=room, api_key_id=api_key_id, name=name, role=role
    )
    try:
        stream = client.containers.logs(container_id=id, follow=follow)
        await _drain_stream_plain(stream)
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


# -------------------------
# Run (detached) and run-attached
# -------------------------


@app.async_command("run")
async def run_container(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption = None,
    api_key_id: ApiKeyIdOption = None,
    image: Annotated[str, typer.Option(..., help="Image to run")],
    command: Annotated[Optional[str], typer.Option(...)] = None,
    env: Annotated[List[str], typer.Option("--env", "-e", help="KEY=VALUE")] = [],
    port: Annotated[
        List[str], typer.Option("--port", "-p", help="CONTAINER:HOST")
    ] = [],
    var: Annotated[
        List[str],
        typer.Option("--var", help="Template variable KEY=VALUE (optional)"),
    ] = [],
    cred: Annotated[
        List[str],
        typer.Option(
            "--cred",
            help="Docker creds (username,password) or (registry,username,password)",
        ),
    ] = [],
    mount_path: Annotated[Optional[str], typer.Option()] = None,
    mount_subpath: Annotated[Optional[str], typer.Option()] = None,
    participant_name: Annotated[Optional[str], typer.Option()] = None,
    role: Annotated[str, typer.Option(...)] = "user",
    name: Annotated[str, typer.Option(...)] = "cli",
):
    account_client, client = await _with_client(
        project_id=project_id, room=room, api_key_id=api_key_id, name=name, role=role
    )
    try:
        creds = _parse_creds(cred)
        env_map = _parse_keyvals(env)
        ports_map = _parse_ports(port)
        vars_map = _parse_keyvals(var)

        stream = client.containers.run(
            image=image,
            command=command,
            env=env_map,
            mount_path=mount_path,
            mount_subpath=mount_subpath,
            role=role,
            participant_name=participant_name,
            ports=ports_map,
            credentials=creds,
            variables=vars_map or None,
        )
        result = await _drain_stream_plain(stream)
        print(result.model_dump() if hasattr(result, "model_dump") else result)
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@app.async_command("run-attached")
async def run_attached(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption = None,
    api_key_id: ApiKeyIdOption = None,
    image: Annotated[str, typer.Option(..., help="Image to run")],
    command: Annotated[Optional[str], typer.Option(...)] = None,
    tty: Annotated[bool, typer.Option("--tty/--no-tty")] = False,
    env: Annotated[List[str], typer.Option("--env", "-e", help="KEY=VALUE")] = [],
    port: Annotated[
        List[str], typer.Option("--port", "-p", help="CONTAINER:HOST")
    ] = [],
    send: Annotated[
        List[str],
        typer.Option(
            "--send",
            help="Optional lines to send to container stdin (each becomes a line)",
        ),
    ] = [],
    name: Annotated[str, typer.Option(...)] = "cli",
    role: Annotated[str, typer.Option(...)] = "user",
):
    account_client, client = await _with_client(
        project_id=project_id, room=room, api_key_id=api_key_id, name=name, role=role
    )
    try:
        env_map = _parse_keyvals(env)
        ports_map = _parse_ports(port)

        tty_obj = client.containers.run_attached(
            image=image,
            command=command,
            env=env_map,
            ports=ports_map,
            tty=tty,
            role=role,
            participant_name=name,
        )

        # Output reader
        async def _read():
            async for b in tty_obj.output():
                if not b:
                    continue
                try:
                    sys.stdout.buffer.write(b)
                    sys.stdout.flush()
                except Exception:
                    # fallback printing
                    print(b.decode(errors="ignore"), end="")

        # Optional sender (from --send args)
        async def _preload():
            for line in send:
                await tty_obj.write(line.encode("utf-8") + b"\n")

        readers = asyncio.gather(_read(), _preload())
        status = await tty_obj.result
        await readers
        if status is not None:
            print(f"\n[green]Exit status:[/green] {status}")
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


# -------------------------
# Images sub-commands
# -------------------------

images_app = async_typer.AsyncTyper(help="Image operations")
app.add_typer(images_app, name="images")


@images_app.async_command("list")
async def images_list(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption = None,
    api_key_id: ApiKeyIdOption = None,
    name: Annotated[str, typer.Option(...)] = "cli",
    role: Annotated[str, typer.Option(...)] = "user",
):
    account_client, client = await _with_client(
        project_id=project_id, room=room, api_key_id=api_key_id, name=name, role=role
    )
    try:
        imgs = await client.containers.list_images()
        print([i.model_dump() for i in imgs])
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@images_app.async_command("delete")
async def images_delete(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption = None,
    api_key_id: ApiKeyIdOption = None,
    image: Annotated[str, typer.Option(..., help="Image ref/tag to delete")],
    name: Annotated[str, typer.Option(...)] = "cli",
    role: Annotated[str, typer.Option(...)] = "user",
):
    account_client, client = await _with_client(
        project_id=project_id, room=room, api_key_id=api_key_id, name=name, role=role
    )
    try:
        await client.containers.delete_image(image=image)
        print("[green]Deleted[/green]")
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@images_app.async_command("pull")
async def images_pull(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption = None,
    api_key_id: ApiKeyIdOption = None,
    tag: Annotated[str, typer.Option(..., help="Image tag/ref to pull")],
    cred: Annotated[
        List[str],
        typer.Option(
            "--cred",
            help="Docker creds (username,password) or (registry,username,password)",
        ),
    ] = [],
    name: Annotated[str, typer.Option(...)] = "cli",
    role: Annotated[str, typer.Option(...)] = "user",
):
    account_client, client = await _with_client(
        project_id=project_id, room=room, api_key_id=api_key_id, name=name, role=role
    )
    try:
        stream = client.containers.pull_image(tag=tag, credentials=_parse_creds(cred))
        result = await _drain_stream_plain(stream)
        print(result.model_dump() if hasattr(result, "model_dump") else result)
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


# -------------------------
# Build sub-commands
# -------------------------

build_app = async_typer.AsyncTyper(help="Build images")
app.add_typer(build_app, name="build")


@build_app.async_command("git")
async def build_git(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption = None,
    api_key_id: ApiKeyIdOption = None,
    tag: Annotated[str, typer.Option(..., help="Resulting image tag")],
    url: Annotated[str, typer.Option(..., help="Git URL")],
    ref: Annotated[str, typer.Option(..., help="Git ref/branch/tag")],
    cred: Annotated[
        List[str],
        typer.Option(
            "--cred",
            help="Docker creds (username,password) or (registry,username,password)",
        ),
    ] = [],
    name: Annotated[str, typer.Option(...)] = "cli",
    role: Annotated[str, typer.Option(...)] = "user",
    pretty: Annotated[bool, typer.Option(...)] = True,
):
    account_client, client = await _with_client(
        project_id=project_id, room=room, api_key_id=api_key_id, name=name, role=role
    )
    try:
        source = BuildSource(git=BuildSourceGit(url=url, ref=ref))
        stream = client.containers.build(
            tag=tag, source=source, credentials=_parse_creds(cred)
        )
        await _drain_stream_pretty(stream) if pretty else await _drain_stream_plain(
            stream
        )
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@build_app.async_command("context")
async def build_context(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption = None,
    api_key_id: ApiKeyIdOption = None,
    tag: Annotated[str, typer.Option(..., help="Resulting image tag")],
    from_dir: Annotated[
        Optional[str],
        typer.Option(help="Directory to tar.gz as build context"),
    ] = None,
    dockerfile: Annotated[
        Optional[str],
        typer.Option(help="Path to a Dockerfile; sends just this file as context"),
    ] = None,
    dockerfile_inline: Annotated[
        Optional[str],
        typer.Option(help="Inline Dockerfile text; sends only this as context"),
    ] = None,
    tgz: Annotated[
        Optional[str],
        typer.Option(help="Use an existing .tar.gz file as the context"),
    ] = None,
    cred: Annotated[
        List[str],
        typer.Option(
            "--cred",
            help="Docker creds (username,password) or (registry,username,password)",
        ),
    ] = [],
    name: Annotated[str, typer.Option(...)] = "cli",
    role: Annotated[str, typer.Option(...)] = "user",
    pretty: Annotated[bool, typer.Option(...)] = True,
):
    # Validate mutually exclusive inputs
    specified = [x for x in [from_dir, dockerfile, dockerfile_inline, tgz] if x]
    if len(specified) != 1:
        raise typer.BadParameter(
            "Specify exactly one of --from-dir, --dockerfile, --dockerfile-inline, or --tgz"
        )

    # Prepare context bytes
    if from_dir:
        ctx_bytes = _make_targz_from_dir(Path(from_dir).resolve())
    elif dockerfile_inline:
        ctx_bytes = _make_targz_with_dockerfile_text(dockerfile_inline)
    elif dockerfile:
        text = Path(dockerfile).read_text(encoding="utf-8")
        ctx_bytes = _make_targz_with_dockerfile_text(text)
    else:
        ctx_bytes = Path(tgz).read_bytes()

    account_client, client = await _with_client(
        project_id=project_id, room=room, api_key_id=api_key_id, name=name, role=role
    )
    try:
        source = BuildSource(context=BuildSourceContext(encoding="gzip"))
        stream = client.containers.build(
            tag=tag,
            source=source,
            context_bytes=ctx_bytes,
            credentials=_parse_creds(cred),
        )
        await _drain_stream_pretty(stream) if pretty else await _drain_stream_plain(
            stream
        )
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@build_app.async_command("room")
async def build_room(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption = None,
    api_key_id: ApiKeyIdOption = None,
    tag: Annotated[str, typer.Option(..., help="Resulting image tag")],
    path: Annotated[str, typer.Option(..., help="Room path to a .tar.gz context")],
    cred: Annotated[
        List[str],
        typer.Option(
            "--cred",
            help="Docker creds (username,password) or (registry,username,password)",
        ),
    ] = [],
    name: Annotated[str, typer.Option(...)] = "cli",
    role: Annotated[str, typer.Option(...)] = "user",
    pretty: Annotated[bool, typer.Option(...)] = True,
):
    account_client, client = await _with_client(
        project_id=project_id, room=room, api_key_id=api_key_id, name=name, role=role
    )
    try:
        source = BuildSource(room=BuildSourceRoom(path=path))
        stream = client.containers.build(
            tag=tag, source=source, credentials=_parse_creds(cred)
        )
        await _drain_stream_pretty(stream) if pretty else await _drain_stream_plain(
            stream
        )
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


# -------------------------
# Build admin: list/stop
# -------------------------

builds_app = async_typer.AsyncTyper(help="Inspect or manage running builds")
app.add_typer(builds_app, name="builds")


@builds_app.async_command("list")
async def list_builds(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption = None,
    api_key_id: ApiKeyIdOption = None,
    name: Annotated[str, typer.Option(...)] = "cli",
    role: Annotated[str, typer.Option(...)] = "user",
):
    account_client, client = await _with_client(
        project_id=project_id, room=room, api_key_id=api_key_id, name=name, role=role
    )
    try:
        builds = await client.containers.list_builds()
        print([b.model_dump() for b in builds])
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@builds_app.async_command("stop")
async def stop_build(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption = None,
    api_key_id: ApiKeyIdOption = None,
    request_id: Annotated[str, typer.Option(..., help="Build request_id to stop")],
    name: Annotated[str, typer.Option(...)] = "cli",
    role: Annotated[str, typer.Option(...)] = "user",
):
    account_client, client = await _with_client(
        project_id=project_id, room=room, api_key_id=api_key_id, name=name, role=role
    )
    try:
        await client.containers.stop_build(request_id=request_id)
        print("[green]Stopped[/green]")
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()
