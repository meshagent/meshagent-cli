import typer
from typing import Annotated, Optional
import os
from meshagent.cli.helper import get_active_project_sync

OutputFormatOption = Annotated[
    str,
    typer.Option("--output", "-o", help="output format [json|table]"),
]


def get_default_project_id():
    if os.getenv("MESHAGENT_CLI_BUILD"):
        return os.getenv("MESHAGENT_PROJECT_ID")

    return os.getenv("MESHAGENT_PROJECT_ID") or get_active_project_sync()


ProjectIdOption = Annotated[
    Optional[str],
    typer.Option(
        "--project-id",
        help="A MeshAgent project id. If empty, the activated project will be used.",
        default_factory=get_default_project_id,
    ),
]

RoomOption = Annotated[
    Optional[str],
    typer.Option(
        "--room", help="Room name", default_factory=lambda: os.getenv("MESHAGENT_ROOM")
    ),
]

StartingUrlOption = Annotated[
    Optional[str],
    typer.Option(
        "--starting-url",
        help="Initial URL to open when starting a computer-use browser session",
    ),
]

ShellRoomMountOption = Annotated[
    list[str],
    typer.Option(
        "--shell-room-mount",
        help="Mount room storage as <source>:<mount>[:ro|rw]",
    ),
]

ShellRoomMountLegacyOption = Annotated[
    list[str],
    typer.Option(
        "--shell-tool-room-path",
        help="Mount room storage as <source>:<mount>[:ro|rw]",
        hidden=True,
    ),
]

ShellEmptyDirMountOption = Annotated[
    list[str],
    typer.Option(
        "--shell-empty-dir-mount",
        help="Mount empty dir at <mount>[:ro|rw]",
    ),
]

ShellEmptyDirMountLegacyOption = Annotated[
    list[str],
    typer.Option(
        "--shell-tool-empty-dir",
        help="Mount empty dir at <mount>[:ro|rw]",
        hidden=True,
    ),
]

ShellConfigMountOption = Annotated[
    list[str],
    typer.Option(
        "--shell-tool-config-mount",
        help="Mount meshagent runtime config files read-only into <mount>",
    ),
]

AllowGotoUrlOption = Annotated[
    bool,
    typer.Option(
        "--allow-goto-url",
        help="Expose the goto URL helper tool for computer use",
    ),
]

RoomCreateOption = Annotated[
    bool,
    typer.Option(
        "--create",
        help="Room name",
    ),
]
