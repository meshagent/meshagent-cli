# meshagent/cli/containers.py
from __future__ import annotations

import asyncio
import io
import os
import re
import tarfile
import time
from datetime import datetime
from pathlib import Path

import pathlib
import pathspec

import aiofiles
import aiofiles.ospath
import typer
from rich import print
from rich.console import Console
from rich.table import Table
from typing import Annotated, Literal, Optional, List, Dict

from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_room,
    split_container_mount,
    split_image_mount,
)
from meshagent.api import (
    RoomClient,
    WebSocketClientProtocol,
)
from meshagent.api.room_server_client import (
    DockerSecret,
    ImageDescriptor,
)
from meshagent.api.specs.service import (
    ContainerMountSpec,
    ImageStorageMountSpec,
    ProjectStorageMountSpec,
    RoomStorageMountSpec,
)

import sys

app = async_typer.LazyTyper(help="Manage containers and images inside a room")
_STDERR_CONSOLE = Console(stderr=True)
_LOG_STREAM_SETTLE_TIMEOUT_SECONDS = 1.0
_CRI_LOG_LINE_PATTERN = re.compile(
    r"^(?P<timestamp>\S+)\s+(?P<stream>stdout|stderr)\s+(?P<flags>[FP])\s(?P<message>.*)$"
)

# -------------------------
# Helpers
# -------------------------

ContainerTemplateOption = Literal["agent", "none"]


def _normalize_container_template(*, template: str) -> ContainerTemplateOption:
    normalized = template.strip()
    if normalized not in {"agent", "none"}:
        raise typer.BadParameter("--template must be agent or none")
    return normalized


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


def _parse_image_operation_mounts(
    *,
    mount_room_path: List[str],
    mount_project_path: List[str],
    mount_image: List[str],
) -> ContainerMountSpec:
    room_mounts: list[RoomStorageMountSpec] = []
    project_mounts: list[ProjectStorageMountSpec] = []
    image_mounts: list[ImageStorageMountSpec] = []

    for value in mount_room_path:
        source, mount, read_only = split_container_mount(
            value, "--mount-room-path", False
        )
        subpath = source if source not in {"", ".", "/"} else None
        room_mounts.append(
            RoomStorageMountSpec(path=mount, subpath=subpath, read_only=read_only)
        )

    for value in mount_project_path:
        source, mount, read_only = split_container_mount(
            value, "--mount-project-path", True
        )
        subpath = source if source not in {"", ".", "/"} else None
        project_mounts.append(
            ProjectStorageMountSpec(path=mount, subpath=subpath, read_only=read_only)
        )

    for value in mount_image:
        image, mount, subpath, read_only = split_image_mount(value, "--mount-image")
        image_mounts.append(
            ImageStorageMountSpec(
                image=image,
                path=mount,
                subpath=subpath,
                read_only=read_only,
            )
        )

    mount_spec = ContainerMountSpec(
        room=room_mounts or None,
        project=project_mounts or None,
        images=image_mounts or None,
    )
    if (
        mount_spec.room is None
        and mount_spec.project is None
        and mount_spec.images is None
    ):
        raise typer.BadParameter(
            "At least one mount is required. Use --mount-room-path, --mount-project-path, or --mount-image."
        )

    return mount_spec


def _format_image_timestamp(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _format_image_bytes(value: int | None) -> str:
    if value is None:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.1f} {units[unit_index]}"


def _image_reference(image) -> str:
    if image.preferred_ref:
        return image.preferred_ref
    if len(image.references) > 0:
        return image.references[0]
    return image.id


def _descriptor_table(*, title: str, descriptor: ImageDescriptor | None) -> Table:
    table = Table(title=title, show_header=True, header_style="bold magenta")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    if descriptor is None:
        table.add_row("descriptor", "—")
        return table
    table.add_row("digest", descriptor.digest)
    table.add_row("media_type", descriptor.media_type or "—")
    table.add_row("size", _format_image_bytes(descriptor.size))
    if len(descriptor.annotations) == 0:
        table.add_row("annotations", "—")
    else:
        for key, value in descriptor.annotations.items():
            table.add_row(f"annotation:{key}", value)
    return table


async def _stream_container_job_logs_and_wait_for_exit(
    *,
    client: RoomClient,
    container_id: str,
) -> int:
    stream = client.containers.logs(container_id=container_id, follow=True)
    log_task = asyncio.create_task(_drain_stream_plain(stream, show_progress=False))

    try:
        exit_code = await client.containers.wait_for_exit(container_id=container_id)
    except Exception:
        await asyncio.gather(stream.cancel(), return_exceptions=True)
        await asyncio.gather(log_task, return_exceptions=True)
        raise

    if not log_task.done():
        try:
            await asyncio.wait_for(
                asyncio.shield(log_task),
                timeout=_LOG_STREAM_SETTLE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            if not log_task.done():
                await asyncio.gather(stream.cancel(), return_exceptions=True)
            await log_task

    return exit_code


async def _stream_build_job_logs_and_wait_for_exit(
    *,
    client: RoomClient,
    build_id: str,
) -> int:
    stream = client.containers.get_build_logs(build_id=build_id, follow=True)
    try:
        exit_code = await _drain_stream_plain(stream, show_progress=False)
    except Exception:
        await asyncio.gather(stream.cancel(), return_exceptions=True)
        raise

    if exit_code is None:
        raise RuntimeError("build log stream closed before an exit code was returned")

    if exit_code != 0:
        _STDERR_CONSOLE.print(
            f"Unable to complete build {build_id}: "
            f"build failed with exit code {exit_code}.",
            style="red",
        )

    return exit_code


class DockerIgnore:
    def __init__(self, dockerignore_path: str):
        """
        Load a .dockerignore file and compile its patterns.
        """
        dockerignore_file = pathlib.Path(dockerignore_path)
        if dockerignore_file.exists():
            with dockerignore_file.open("r") as f:
                patterns = f.read().splitlines()
        else:
            patterns = []

        # pathspec with gitwildmatch is the same style used by dockerignore
        self._spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)

    def matches(self, path: str) -> bool:
        """
        Return True if the given path matches a pattern in the .dockerignore file.
        Path can be relative or absolute.
        """
        return self._spec.match_file(path)


async def _make_targz_from_dir(path: Path) -> bytes:
    buf = io.BytesIO()

    docker_ignore = None

    def _tarfilter_strip_macos(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        """
        Filter to make Linux-friendly tarballs:
        - Drop AppleDouble files (._*)
        - Reset uid/gid/uname/gname
        - Clear pax headers
        """

        if docker_ignore is not None:
            if docker_ignore.matches(ti.path):
                return None

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

    docker_ignore_path = path.joinpath(".dockerignore")

    if await aiofiles.ospath.exists(docker_ignore_path):
        docker_ignore = DockerIgnore(docker_ignore_path)

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(path, arcname=".", filter=_tarfilter_strip_macos)
    return buf.getvalue()


async def _drain_stream_plain(stream, *, show_progress: bool = True):
    def _format_line(line: str) -> str:
        has_newline = line.endswith("\n")
        line_without_newline = line[:-1] if has_newline else line
        match = _CRI_LOG_LINE_PATTERN.match(line_without_newline)
        if match is None:
            return line

        message = match.group("message")
        if has_newline:
            return f"{message}\n"
        return message

    async def _logs():
        async for line in stream.logs():
            if line is not None:
                formatted_line = _format_line(line)
                sys.stdout.write(
                    formatted_line
                    if formatted_line.endswith("\n")
                    else f"{formatted_line}\n"
                )
                sys.stdout.flush()

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
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        connection = await account_client.connect_room(project_id=project_id, room=room)

        proto = WebSocketClientProtocol(
            url=connection.room_url,
            token=connection.jwt,
        )
        client_cm = RoomClient(protocol_factory=proto.create_factory())
        await client_cm.__aenter__()
        return account_client, client_cm
    except Exception:
        await account_client.close()
        raise


# -------------------------
# Top-level: list / stop / logs / run
# -------------------------


@app.async_command("list", help="List containers in a room.")
async def list_containers(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    all_containers: Annotated[
        bool,
        typer.Option(
            "--all",
            "-a",
            help="Include exited containers in the listing.",
        ),
    ] = False,
    output: Annotated[Optional[str], typer.Option(help="json | table")] = "json",
):
    account_client, client = await _with_client(
        project_id=project_id,
        room=room,
    )
    try:
        containers = await client.containers.list(all=all_containers)
        if output == "table":
            from rich.table import Table
            from rich.console import Console

            table = Table(title="Containers")
            table.add_column("ID", style="cyan")
            table.add_column("Image")
            table.add_column("Status")
            table.add_column("Name")
            table.add_column("ServiceID")
            for c in containers:
                table.add_row(
                    c.id,
                    c.image or "",
                    c.status or "",
                    c.name or "",
                    c.service_id or "",
                )
            Console().print(table)
        else:
            # default json-ish
            print([c.model_dump() for c in containers])
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@app.async_command("stop", help="Stop a running container in a room.")
async def stop_container(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    id: Annotated[str, typer.Option(..., help="Container ID")],
):
    account_client, client = await _with_client(
        project_id=project_id,
        room=room,
    )
    try:
        await client.containers.stop(container_id=id)
        print("[green]Stopped[/green]")
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@app.async_command("logs", help="Print container logs from a room.", hidden=True)
@app.async_command("log", help="Print container logs from a room.")
async def container_log(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    id: Annotated[str, typer.Option(..., help="Container ID")],
    follow: Annotated[
        bool, typer.Option("--follow/--no-follow", help="Stream logs")
    ] = False,
):
    account_client, client = await _with_client(
        project_id=project_id,
        room=room,
    )
    try:
        stream = client.containers.logs(container_id=id, follow=follow)
        await _drain_stream_plain(stream)
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@app.async_command("exec", help="Execute a command inside a running container.")
async def exec_container(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    container_id: Annotated[str, typer.Option(..., help="container id")],
    command: Annotated[
        Optional[str],
        typer.Option(..., help="Command to execute in the container (quoted string)"),
    ] = None,
):
    account_client, client = await _with_client(
        project_id=project_id,
        room=room,
    )
    result = 1

    try:
        import shlex

        parsed_command = shlex.split(command) if command else None
        container = await client.containers.exec(
            container_id=container_id,
            command=parsed_command,
            tty=False,
        )

        async def write_all(fd, data: bytes) -> None:
            loop = asyncio.get_running_loop()
            mv = memoryview(data)

            while mv:
                try:
                    n = os.write(fd, mv)
                    mv = mv[n:]
                except BlockingIOError:
                    fut = loop.create_future()
                    loop.add_writer(fd, fut.set_result, None)
                    try:
                        await fut
                    finally:
                        loop.remove_writer(fd)

        async def read_stderr():
            async for output in container.stderr():
                await write_all(sys.stderr.fileno(), output)

        async def read_stdout():
            async for output in container.stdout():
                await write_all(sys.stdout.fileno(), output)

        async def read_piped_stdin(bufsize: int = 1024):
            while True:
                chunk = await asyncio.to_thread(sys.stdin.buffer.read, bufsize)

                if not chunk or len(chunk) == 0:
                    await container.close_stdin()
                    break

                await container.write(chunk)

        if sys.stdin.isatty():
            await container.close_stdin()
            await asyncio.gather(read_stdout(), read_stderr())
        else:
            await asyncio.gather(read_stdout(), read_stderr(), read_piped_stdin())

        result = await container.result
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()

    sys.exit(result)


# -------------------------
# Run (detached)
# -------------------------


@app.async_command("run", help="Run a container inside a room.")
async def run_container(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    image: Annotated[str, typer.Option(..., help="Image to run")],
    command: Annotated[
        Optional[str],
        typer.Option(..., help="Command to execute in the container (quoted string)"),
    ] = None,
    working_dir: Annotated[
        Optional[str],
        typer.Option(
            "--working-dir",
            help="Working directory inside the container (must be an absolute path)",
        ),
    ] = None,
    env: Annotated[List[str], typer.Option("--env", "-e", help="KEY=VALUE")] = [],
    port: Annotated[
        List[str], typer.Option("--port", "-p", help="CONTAINER:HOST")
    ] = [],
    cred: Annotated[
        List[str],
        typer.Option(
            "--cred",
            help="Docker creds (username,password) or (registry,username,password)",
        ),
    ] = [],
    mount_path: Annotated[
        Optional[str],
        typer.Option(help="Room storage path to mount into the container"),
    ] = None,
    mount_subpath: Annotated[
        Optional[str],
        typer.Option(help="Subpath within `--mount-path` to mount"),
    ] = None,
    participant_name: Annotated[
        Optional[str], typer.Option(help="Participant name to associate with the run")
    ] = None,
    role: Annotated[
        str, typer.Option(..., help="Role to run the container as")
    ] = "user",
    container_name: Annotated[
        str, typer.Option(..., help="Optional container name")
    ] = None,
    template: Annotated[
        str,
        typer.Option(
            "--template",
            help=(
                "Allowed values: agent, none. agent: MeshAgent mounts room storage "
                "at /data, sets MESHAGENT_TOKEN, OPENAI_API_KEY, and "
                "ANTHROPIC_API_KEY to a container-scoped MeshAgent token. agent "
                "also sets SMTP_PASSWORD to that token, SMTP_USERNAME to the "
                "container name, SMTP_PORT to 587, SMTP_HOSTNAME from "
                "MESHAGENT_MAIL_DOMAIN when available, plus OPENAI_BASE_URL, "
                "ANTHROPIC_BASE_URL, MESHAGENT_API_URL, MESHAGENT_ROOM_URL, "
                "MESHAGENT_ROOM, MESHAGENT_PROJECT_ID, MESHAGENT_SESSION_ID, "
                "OTEL_ENDPOINT, OTEL_PYTHON_LOG_LEVEL, and MESHAGENT_MAIL_DOMAIN "
                "from the room runtime when available. Manual env values win. "
                "none: MeshAgent applies no template defaults."
            ),
        ),
    ] = "none",
):
    account_client, client = await _with_client(
        project_id=project_id,
        room=room,
    )
    try:
        creds = _parse_creds(cred)
        env_map = _parse_keyvals(env)
        ports_map = _parse_ports(port)
        normalized_template = _normalize_container_template(template=template)

        container_id = await client.containers.run(
            name=container_name,
            image=image,
            command=command,
            working_dir=working_dir,
            env=env_map,
            mount_path=mount_path,
            mount_subpath=mount_subpath,
            role=role,
            participant_name=participant_name,
            ports=ports_map,
            credentials=creds,
            template=normalized_template,
        )

        print(f"Container started: {container_id}")
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


# -------------------------
# Images sub-commands
# -------------------------

images_app = async_typer.AsyncTyper(help="Image operations")
app.add_lazy_command(
    name="image",
    module="meshagent.cli.containers",
    attribute="images_app",
    help="Image operations",
)
app.add_lazy_command(
    name="images",
    module="meshagent.cli.containers",
    attribute="images_app",
    help="Image operations",
    hidden=True,
)


@images_app.async_command("list", help="List container images available in a room.")
async def images_list(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    output: Annotated[
        str,
        typer.Option(help="table | json"),
    ] = "table",
):
    account_client, client = await _with_client(
        project_id=project_id,
        room=room,
    )
    try:
        imgs = await client.containers.list_images()
        if output == "json":
            print([i.model_dump(mode="json") for i in imgs])
            return

        table = Table(title="Images")
        table.add_column("Reference", style="cyan")
        table.add_column("ID")
        table.add_column("Updated")
        table.add_column("Created")
        table.add_column("Media Type")
        for image in imgs:
            table.add_row(
                _image_reference(image),
                image.id,
                _format_image_timestamp(image.updated_at),
                _format_image_timestamp(image.created_at),
                image.target_media_type or "—",
            )
        Console().print(table)
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@images_app.async_command(
    "inspect",
    help="Inspect a container image in a room by image ID.",
)
async def images_inspect(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    image_id: Annotated[
        str,
        typer.Option(..., "--image-id", help="Image ID from `meshagent images list`"),
    ],
):
    account_client, client = await _with_client(
        project_id=project_id,
        room=room,
    )
    try:
        inspection = await client.containers.inspect_image(image_id=image_id)
        console = Console()

        summary_table = Table(
            title="Image", show_header=True, header_style="bold magenta"
        )
        summary_table.add_column("Field", style="cyan")
        summary_table.add_column("Value")
        summary_table.add_row("reference", _image_reference(inspection.image))
        summary_table.add_row("id", inspection.image.id)
        summary_table.add_row(
            "references",
            ", ".join(inspection.image.references)
            if inspection.image.references
            else "—",
        )
        summary_table.add_row(
            "created_at",
            _format_image_timestamp(inspection.image.created_at),
        )
        summary_table.add_row(
            "updated_at",
            _format_image_timestamp(inspection.image.updated_at),
        )
        summary_table.add_row(
            "target_media_type",
            inspection.image.target_media_type or "—",
        )
        summary_table.add_row(
            "content_size",
            _format_image_bytes(inspection.content_size),
        )
        if len(inspection.image.labels) == 0:
            summary_table.add_row("labels", "—")
        else:
            for key, value in inspection.image.labels.items():
                summary_table.add_row(f"label:{key}", value)

        manifests_table = Table(
            title="Manifests",
            show_header=True,
            header_style="bold magenta",
        )
        manifests_table.add_column("Digest", style="cyan")
        manifests_table.add_column("Media Type")
        manifests_table.add_column("Size")
        manifests_table.add_column("Platform")
        if len(inspection.manifests) == 0:
            manifests_table.add_row("—", "—", "—", "—")
        else:
            for manifest in inspection.manifests:
                platform = "/".join(
                    part
                    for part in (
                        manifest.platform_os,
                        manifest.platform_architecture,
                        manifest.platform_variant,
                    )
                    if part
                )
                manifests_table.add_row(
                    manifest.descriptor.digest,
                    manifest.descriptor.media_type or "—",
                    _format_image_bytes(manifest.descriptor.size),
                    platform or "—",
                )

        layers_table = Table(
            title="Layers",
            show_header=True,
            header_style="bold magenta",
        )
        layers_table.add_column("Digest", style="cyan")
        layers_table.add_column("Media Type")
        layers_table.add_column("Size")
        if len(inspection.layers) == 0:
            layers_table.add_row("—", "—", "—")
        else:
            for layer in inspection.layers:
                layers_table.add_row(
                    layer.digest,
                    layer.media_type or "—",
                    _format_image_bytes(layer.size),
                )

        console.print(summary_table)
        console.print(_descriptor_table(title="Target", descriptor=inspection.target))
        console.print(
            _descriptor_table(
                title="Selected Manifest",
                descriptor=inspection.selected_manifest,
            )
        )
        console.print(_descriptor_table(title="Config", descriptor=inspection.config))
        console.print(manifests_table)
        console.print(layers_table)
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@images_app.async_command("delete", help="Delete a container image from a room.")
async def images_delete(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    image: Annotated[str, typer.Option(..., help="Image ref/tag to delete")],
):
    account_client, client = await _with_client(
        project_id=project_id,
        room=room,
    )
    try:
        await client.containers.delete_image(image=image)
        print("[green]Deleted[/green]")
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@images_app.async_command("pull", help="Pull a container image into a room.")
async def images_pull(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    tag: Annotated[str, typer.Option(..., help="Image tag/ref to pull")],
    cred: Annotated[
        List[str],
        typer.Option(
            "--cred",
            help="Docker creds (username,password) or (registry,username,password)",
        ),
    ] = [],
):
    account_client, client = await _with_client(
        project_id=project_id,
        room=room,
    )
    try:
        await client.containers.pull_image(tag=tag, credentials=_parse_creds(cred))
        print("Image pulled")
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@images_app.async_command("push", help="Push a container image from a room.")
async def images_push(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    tag: Annotated[str, typer.Option(..., help="Image tag/ref to push")],
    private: Annotated[
        bool,
        typer.Option(
            "--private/--public",
            help="Whether the push container is private to the participant",
        ),
    ] = False,
    cred: Annotated[
        List[str],
        typer.Option(
            "--cred",
            help="Docker creds (username,password) or (registry,username,password)",
        ),
    ] = [],
):
    account_client, client = await _with_client(
        project_id=project_id,
        room=room,
    )
    try:
        container_id = await client.containers.push_image(
            tag=tag,
            credentials=_parse_creds(cred),
            private=private,
        )
        exit_code = await _stream_container_job_logs_and_wait_for_exit(
            client=client, container_id=container_id
        )
        if exit_code != 0:
            raise typer.Exit(code=exit_code)
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@images_app.async_command(
    "load",
    help="Load an OCI image archive from room storage into a room.",
)
async def images_load(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    archive_path: Annotated[
        str,
        typer.Option(
            ...,
            "--archive-path",
            "--image",
            "-i",
            help="Absolute room storage path to the OCI image archive file",
        ),
    ],
):
    if not archive_path.startswith("/"):
        raise typer.BadParameter("--archive-path/--image must be an absolute path")

    account_client, client = await _with_client(
        project_id=project_id,
        room=room,
    )
    try:
        imported_image = await client.containers.load(archive_path=archive_path)
        print(f"Image loaded: {imported_image.resolved_ref}")
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@images_app.async_command("save", help="Save an OCI image archive from a room.")
async def images_save(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    tag: Annotated[str, typer.Option(..., help="Image tag/ref to save")],
    archive_path: Annotated[
        str,
        typer.Option(
            ...,
            "--archive-path",
            help="Path to write OCI archive inside one of the mounted paths (absolute path)",
        ),
    ],
    mount_room_path: Annotated[
        List[str],
        typer.Option(
            "--mount-room-path",
            help=(
                "Room storage mount '<source>:<mount>[:ro|rw]'. "
                "Example '/images:/workspace'"
            ),
        ),
    ] = [],
    mount_project_path: Annotated[
        List[str],
        typer.Option(
            "--mount-project-path",
            help=(
                "Project storage mount '<source>:<mount>[:ro|rw]'. "
                "Example '/shared:/project:ro'"
            ),
        ),
    ] = [],
    mount_image: Annotated[
        List[str],
        typer.Option(
            "--mount-image",
            help=(
                "Image mount '<image>=<mount>[:ro|rw]'. "
                "Example 'alpine:latest=/toolchain:ro'"
            ),
        ),
    ] = [],
    private: Annotated[
        bool,
        typer.Option(
            "--private/--public",
            help="Whether the save container is private to the participant",
        ),
    ] = False,
):
    mount_spec = _parse_image_operation_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
        mount_image=mount_image,
    )
    if not archive_path.startswith("/"):
        raise typer.BadParameter("--archive-path must be an absolute path")

    account_client, client = await _with_client(
        project_id=project_id,
        room=room,
    )
    try:
        container_id = await client.containers.save_image(
            tag=tag,
            mounts=[mount_spec],
            archive_path=archive_path,
            private=private,
        )
        exit_code = await _stream_container_job_logs_and_wait_for_exit(
            client=client, container_id=container_id
        )
        if exit_code != 0:
            raise typer.Exit(code=exit_code)
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()
