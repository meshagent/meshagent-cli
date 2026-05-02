from __future__ import annotations

from pathlib import Path
import textwrap

import click


SOURCE_SUFFIXES = {
    ".cs",
    ".dart",
    ".go",
    ".js",
    ".jsx",
    ".py",
    ".rb",
    ".ts",
    ".tsx",
}
PROJECT_MARKER_NAMES = {
    "Containerfile",
    "Dockerfile",
    "Gemfile",
    "go.mod",
    "meshagent.yaml",
    "meshagent.yml",
    "package.json",
    "pubspec.yaml",
    "pyproject.toml",
    "requirements.txt",
    "tsconfig.json",
}
IGNORED_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    "bin",
    "build",
    "dist",
    "node_modules",
    "obj",
    "target",
    "venv",
}
IGNORED_FILE_NAMES = {
    ".DS_Store",
    ".gitkeep",
}


SERVER_PY = '''\
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_text(200, "ok\\n")
            return
        if self.path == "/":
            self._send_text(200, "hello from meshagent init\\n")
            return
        self._send_text(404, "not found\\n")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_text(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
'''

DOCKERFILE = """\
FROM python:3.13-slim
WORKDIR /app
COPY server.py .
EXPOSE 8080
CMD ["python", "server.py"]
"""

DOCKERIGNORE = """\
__pycache__/
*.pyc
.venv/
venv/
.git/
.DS_Store
"""


def _has_existing_project_content(root: Path) -> bool:
    for path in sorted(root.rglob("*")):
        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in IGNORED_DIR_NAMES for part in relative_parts[:-1]):
            continue
        if path.is_dir():
            continue
        if path.name in IGNORED_FILE_NAMES:
            continue
        if path.name in PROJECT_MARKER_NAMES:
            return True
        if path.suffix.lower() in SOURCE_SUFFIXES:
            return True
    return False


def _write_file(path: Path, contents: str) -> None:
    path.write_text(textwrap.dedent(contents).lstrip(), encoding="utf-8")


@click.command(
    "init",
    help="Create a minimal deployable Python hello world project.",
)
@click.argument(
    "path",
    required=False,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
)
def init_command(path: Path | None = None) -> None:
    """Create a minimal Python project that can be deployed on MeshAgent."""

    root = (path or Path.cwd()).resolve()
    root.mkdir(parents=True, exist_ok=True)

    click.echo("meshagent init")
    click.echo(f"Project: {root}")

    if _has_existing_project_content(root):
        click.echo("")
        click.echo("Existing application code or deployment metadata was detected.")
        click.echo("No files were written.")
        click.echo("")
        click.echo("Recommended next step for existing projects:")
        click.echo("  meshagent doctor")
        return

    files = {
        "server.py": SERVER_PY,
        "Dockerfile": DOCKERFILE,
        ".dockerignore": DOCKERIGNORE,
    }
    for name, contents in files.items():
        _write_file(root / name, contents)

    click.echo("")
    click.echo("Created a minimal deployable Python hello world project:")
    for name in files:
        click.echo(f"  {name}")
    click.echo("")
    click.echo("Next steps:")
    click.echo("  meshagent doctor")
    click.echo(
        "  meshagent deploy . --tag <repository>:<tag> --public "
        "--liveness /health --wait"
    )
