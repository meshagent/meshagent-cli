import typer
from typing import Annotated

OutputFormatOption = Annotated[
    str,
    typer.Option("--output", "-o", help="output format [json|table]"),
]
