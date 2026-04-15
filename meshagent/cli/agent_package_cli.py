from pathlib import Path

import asyncio
import tempfile

import typer
from rich import print
from rich.text import Text
from typing import Annotated

from meshagent.agents import Package, deploy_package, run_package
import meshagent.agents.package as package_module
from meshagent.api import RoomClient, WebSocketClientProtocol
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.containers import _stream_container_job_logs_and_wait_for_exit
from meshagent.cli.helper import get_client, resolve_project_id, resolve_room
from meshagent.cli.multi import import_from_path


app = async_typer.AsyncTyper(help="Build, run, and deploy packaged services.")


def _load_package(*, module_path: str, export_name: str) -> Package:
    resolved_module_path = Path(module_path).expanduser().resolve()
    with package_module._agent_base_path_scope(resolved_module_path.parent):
        module = import_from_path(str(resolved_module_path))

        if export_name not in module.__dict__:
            raise ImportError(f"{resolved_module_path} does not define {export_name}")

        exported = module.__dict__[export_name]
        if isinstance(exported, Package):
            exported._bind_module_path(module_path=resolved_module_path)
            exported._bind_module_export(
                export_name=export_name,
                export_is_factory=False,
            )
            return exported

        if callable(exported):
            built = exported()
            if isinstance(built, Package):
                built._bind_module_path(module_path=resolved_module_path)
                built._bind_module_export(
                    export_name=export_name,
                    export_is_factory=True,
                )
                return built
            raise TypeError(
                f"{resolved_module_path}:{export_name} returned {type(built).__name__}, expected meshagent.agents.Package"
            )

        raise TypeError(
            f"{resolved_module_path}:{export_name} is {type(exported).__name__}, expected meshagent.agents.Package or a zero-argument callable returning one"
        )


def _print_packaged_files(*, package: Package) -> None:
    module_path = package._resolved_runtime_module_path(module_path=None)
    deploy_assets = package._resolve_deploy_assets()
    with tempfile.TemporaryDirectory(prefix="meshagent-package-verbose-") as temp_dir:
        _, runtime_assets = package._runtime_module_deploy_assets(
            module_path=module_path,
            temp_dir=Path(temp_dir),
        )

    file_entries = package._packaged_file_entries(
        deploy_assets=deploy_assets,
        runtime_assets=runtime_assets,
    )
    print(Text("Packaged files:", style="bold"))
    if len(file_entries) == 0:
        print(Text("  (none)", style="dim"))
        return

    for entry in file_entries:
        print(
            Text(
                f"  [{entry.category}] {entry.source} -> {entry.dest.as_posix()}",
            )
        )


def _verbose_status_printer(message: str) -> None:
    print(Text(message, style="dim"))


@app.async_command("deploy", help="Deploy a packaged service into a room.")
async def deploy(
    *,
    module: Annotated[
        str, typer.Argument(help="Path to a Python file exporting a Package")
    ],
    room: RoomOption,
    project_id: ProjectIdOption,
    name: Annotated[
        str, typer.Option(help="Export name in the Python module")
    ] = "main",
    builder_name: Annotated[
        str | None,
        typer.Option(
            "--builder-name",
            help="Optional reusable builder name for package image builds.",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Print all files that will be added to the package.",
        ),
    ] = False,
) -> None:
    try:
        package = _load_package(module_path=module, export_name=name)
        if verbose:
            _print_packaged_files(package=package)
        service_id = await deploy_package(
            package=package,
            room=room,
            project_id=project_id,
            builder_name=builder_name,
            status_callback=_verbose_status_printer if verbose else None,
        )
        print(f"[green]Deployed service:[/] {service_id}")
    except Exception as exc:
        print(Text(str(exc), style="red"))
        raise typer.Exit(1)


@app.async_command("run", help="Run a packaged service container in a room.")
async def run(
    *,
    module: Annotated[
        str, typer.Argument(help="Path to a Python file exporting a Package")
    ],
    room: RoomOption,
    project_id: ProjectIdOption,
    name: Annotated[
        str, typer.Option(help="Export name in the Python module")
    ] = "main",
    builder_name: Annotated[
        str | None,
        typer.Option(
            "--builder-name",
            help="Optional reusable builder name for package image builds.",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Print all files that will be added to the package.",
        ),
    ] = False,
) -> None:
    try:
        package = _load_package(module_path=module, export_name=name)
        if verbose:
            _print_packaged_files(package=package)
        container_id = await run_package(
            package=package,
            room=room,
            project_id=project_id,
            builder_name=builder_name,
            status_callback=_verbose_status_printer if verbose else None,
        )
        print(f"[green]Started container:[/] {container_id}")
        account_client = await get_client()
        try:
            resolved_project_id = await resolve_project_id(project_id=project_id)
            resolved_room = resolve_room(room)
            if resolved_room is None:
                raise ValueError("room is required")
            connection = await account_client.connect_room(
                project_id=resolved_project_id,
                room=resolved_room,
            )
            async with RoomClient(
                protocol_factory=WebSocketClientProtocol(
                    url=connection.room_url,
                    token=connection.jwt,
                ).create_factory()
            ) as client:
                try:
                    exit_code = await _stream_container_job_logs_and_wait_for_exit(
                        client=client,
                        container_id=container_id,
                    )
                except (KeyboardInterrupt, asyncio.CancelledError):
                    print(
                        "[bold yellow]Stopping and deleting container...[/bold yellow]"
                    )
                    try:
                        await client.containers.stop(
                            container_id=container_id,
                            force=True,
                        )
                    except Exception as exc:
                        print(
                            Text(
                                f"Unable to stop container before delete: {exc}",
                                style="yellow",
                            )
                        )
                    try:
                        await client.containers.delete(container_id=container_id)
                    except Exception as exc:
                        print(
                            Text(
                                f"Unable to delete container after interrupt: {exc}",
                                style="yellow",
                            )
                        )
                    raise typer.Exit(code=130)
                if exit_code != 0:
                    raise typer.Exit(code=exit_code)
        finally:
            await account_client.close()
    except Exception as exc:
        if isinstance(exc, typer.Exit):
            raise
        print(Text(str(exc), style="red"))
        raise typer.Exit(1)
