import typer
from typing import Annotated, Optional

OutputFormatOption = Annotated[
    str,
    typer.Option("--output", "-o", help="output format [json|table]"),
]

ProjectIdOption = Annotated[
    Optional[str],
    typer.Option("--project-id", help="MeshAgent project id. If empty, the activated project will be used."),
]
