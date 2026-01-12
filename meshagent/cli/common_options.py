import typer
from typing import Annotated, Optional
import os
from meshagent.cli.helper import _load_settings

OutputFormatOption = Annotated[
    str,
    typer.Option("--output", "-o", help="output format [json|table]"),
]

if os.getenv("MESHAGENT_CLI_BUILD"):
    default_project_id = None
else:
    settings = _load_settings()
    if settings is None:
        default_project_id = None
    else:
        default_project_id = settings.active_project


def get_default_project_id():
    return os.getenv("MESHAGENT_PROJECT_ID") or default_project_id


ProjectIdOption = Annotated[
    Optional[str],
    typer.Option(
        "--project-id",
        help="A MeshAgent project id. If empty, the activated project will be used.",
        default_factory=get_default_project_id(),
    ),
]

RoomOption = Annotated[
    Optional[str],
    typer.Option(
        "--room", help="Room name", default_factory=os.getenv("MESHAGENT_ROOM")
    ),
]

RoomCreateOption = Annotated[
    bool,
    typer.Option(
        "--create",
        help="Room name",
    ),
]
