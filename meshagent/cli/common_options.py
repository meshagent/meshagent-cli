import typer
from typing import Annotated, Optional

OutputFormatOption = Annotated[
    str,
    typer.Option("--output", "-o", help="output format [json|table]"),
]

ProjectIdOption = Annotated[
    Optional[str],
    typer.Option(
        "--project-id",
        help="A MeshAgent project id. If empty, the activated project will be used.",
    ),
]

ApiKeyIdOption = Annotated[
    Optional[str],
    typer.Option(
        "--api-key-id",
        help="A MeshAgent project API key id. If empty, the activated api key will be used.",
    ),
]

RoomOption = Annotated[
    Optional[str],
    typer.Option(
        "--room",
        help="Room name. If empty, the MESHAGENT_ROOM environment variable will be used.",
    ),
]
