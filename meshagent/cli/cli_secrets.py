# --------------------------------------------------------------------------
#  Imports
# --------------------------------------------------------------------------
import typer
from rich import print
from typing import Annotated, Dict
from meshagent.cli.common_options import ProjectIdOption

from meshagent.cli import async_typer
from meshagent.cli.helper import get_client, print_json_table, resolve_project_id
from meshagent.api.client import (
    PullSecret,
    KeysSecret,
    SecretLike,
)  # or wherever you defined them

# --------------------------------------------------------------------------
#  App Definition
# --------------------------------------------------------------------------
secrets_app = async_typer.AsyncTyper(help="Manage secrets for your project.")


# --------------------------------------------------------------------------
#  Utility helpers
# --------------------------------------------------------------------------


def _parse_kv_inline(source: str | None) -> Dict[str, str]:
    """
    Parse a space-separated list of `key=value` tokens into a dict.
    """
    if source is None:
        return {}
    tokens = source.strip().split()
    kv: Dict[str, str] = {}
    for t in tokens:
        if "=" not in t:
            raise typer.BadParameter(f"Expected key=value, got '{t}'")
        k, v = t.split("=", 1)
        kv[k] = v
    return kv


# --------------------------------------------------------------------------
#  Subcommand group: "keys"
#     e.g.: meshagent secrets keys create --name <NAME> --data ...
# --------------------------------------------------------------------------
keys_app = async_typer.AsyncTyper(
    help="Create or update environment-based key-value secrets."
)


@keys_app.async_command("create")
async def create_keys_secret(
    *,
    project_id: ProjectIdOption = None,
    name: Annotated[str, typer.Option(help="Secret name")],
    data: Annotated[
        str,
        typer.Option(
            "--data",
            help="Format: key=value key2=value  (space-separated)",
        ),
    ],
):
    """
    Create a new 'keys' secret (opaque env-vars).
    """
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        data_dict = _parse_kv_inline(data)

        secret_obj = KeysSecret(
            id="",
            name=name,
            data=data_dict,
        )
        secret_id = await client.create_secret(project_id=project_id, secret=secret_obj)
        print(f"[green]Created keys secret:[/] {secret_id}")

    finally:
        await client.close()


@keys_app.async_command("update")
async def update_keys_secret(
    *,
    project_id: ProjectIdOption = None,
    secret_id: Annotated[str, typer.Option(help="Existing secret ID")],
    name: Annotated[str, typer.Option(help="Secret name")],
    data: Annotated[
        str,
        typer.Option(
            "--data",
            help="Format: key=value key2=value  (space-separated)",
        ),
    ],
):
    """
    Update an existing 'keys' secret (opaque env-vars).
    """
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        data_dict = _parse_kv_inline(data)

        secret_obj = KeysSecret(
            id=secret_id,
            name=name,
            data=data_dict,
        )
        await client.update_secret(project_id=project_id, secret=secret_obj)
        print(f"[green]Keys secret {secret_id} updated.[/]")
    finally:
        await client.close()


# --------------------------------------------------------------------------
#  Subcommand group: "docker"
#     e.g.: meshagent secrets docker create --name myregistry --server ...
# --------------------------------------------------------------------------
docker_app = async_typer.AsyncTyper(
    help="Create or update a Docker registry pull secret."
)


@docker_app.async_command("create")
async def create_docker_secret(
    *,
    project_id: ProjectIdOption = None,
    name: Annotated[str, typer.Option(help="Secret name")],
    server: Annotated[
        str, typer.Option(help="Docker registry server, e.g. index.docker.io")
    ],
    username: Annotated[str, typer.Option(help="Registry user name")],
    password: Annotated[str, typer.Option(help="Registry password")],
    email: Annotated[
        str,
        typer.Option(
            "--email", help="User email for Docker config", show_default=False
        ),
    ] = "none@example.com",
):
    """
    Create a new Docker pull secret (generic).
    """
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)

        secret_obj = PullSecret(
            id="",
            name=name,
            server=server,
            username=username,
            password=password,
            email=email,
        )
        secret_id = await client.create_secret(project_id=project_id, secret=secret_obj)
        print(f"[green]Created Docker pull secret:[/] {secret_id}")
    finally:
        await client.close()


@docker_app.async_command("update")
async def update_docker_secret(
    *,
    project_id: ProjectIdOption = None,
    secret_id: Annotated[str, typer.Option(help="Existing secret ID")],
    name: Annotated[str, typer.Option(help="Secret name")],
    server: Annotated[str, typer.Option(help="Docker registry server")],
    username: Annotated[str, typer.Option(help="Registry user name")],
    password: Annotated[str, typer.Option(help="Registry password")],
    email: Annotated[
        str,
        typer.Option(
            "--email", help="User email for Docker config", show_default=False
        ),
    ] = "none@example.com",
):
    """
    Update an existing Docker pull secret (generic).
    """
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        secret_obj = PullSecret(
            id=secret_id,
            name=name,
            server=server,
            username=username,
            password=password,
            email=email,
        )
        await client.update_secret(project_id=project_id, secret=secret_obj)
        print(f"[green]Docker pull secret {secret_id} updated.[/]")
    finally:
        await client.close()


# --------------------------------------------------------------------------
#  Subcommand group: "acr"
#     e.g.: meshagent secrets acr create --name <NAME> --server <REG>.azurecr.io ...
# --------------------------------------------------------------------------
acr_app = async_typer.AsyncTyper(
    help="Create or update an Azure Container Registry pull secret."
)


@acr_app.async_command("create")
async def create_acr_secret(
    *,
    project_id: ProjectIdOption = None,
    name: Annotated[str, typer.Option(help="Secret name")],
    server: Annotated[str, typer.Option(help="ACR server, e.g. myregistry.azurecr.io")],
    username: Annotated[str, typer.Option(help="Service principal ID")],
    password: Annotated[str, typer.Option(help="Service principal secret/password")],
):
    """
    Create a new ACR pull secret (defaults email to 'none@microsoft.com').
    """
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)

        secret_obj = PullSecret(
            id="",
            name=name,
            server=server,
            username=username,
            password=password,
            email="none@microsoft.com",  # Set a default for ACR usage
        )
        secret_id = await client.create_secret(project_id=project_id, secret=secret_obj)
        print(f"[green]Created ACR pull secret:[/] {secret_id}")
    finally:
        await client.close()


@acr_app.async_command("update")
async def update_acr_secret(
    *,
    project_id: ProjectIdOption = None,
    secret_id: Annotated[str, typer.Option(help="Existing secret ID")],
    name: Annotated[str, typer.Option(help="Secret name")],
    server: Annotated[str, typer.Option(help="ACR server, e.g. myregistry.azurecr.io")],
    username: Annotated[str, typer.Option(help="Service principal ID")],
    password: Annotated[str, typer.Option(help="Service principal secret/password")],
):
    """
    Update an existing ACR pull secret (defaults email to 'none@microsoft.com').
    """
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        secret_obj = PullSecret(
            id=secret_id,
            name=name,
            server=server,
            username=username,
            password=password,
            email="none@microsoft.com",
        )
        await client.update_secret(project_id=project_id, secret=secret_obj)
        print(f"[green]ACR pull secret {secret_id} updated.[/]")
    finally:
        await client.close()


# --------------------------------------------------------------------------
#  Subcommand group: "gar"
#     e.g.: meshagent secrets gar create --name <NAME> --server ...
#           (Typically sets email='none@google.com' and username='_json_key')
# --------------------------------------------------------------------------
gar_app = async_typer.AsyncTyper(
    help="Create or update a Google Artifact Registry pull secret."
)


@gar_app.async_command("create")
async def create_gar_secret(
    *,
    project_id: ProjectIdOption = None,
    name: Annotated[str, typer.Option(help="Secret name")],
    server: Annotated[str, typer.Option(help="GAR host, e.g. us-west1-docker.pkg.dev")],
    json_key: Annotated[
        str, typer.Option(help="Entire GCP service account JSON as string")
    ],
):
    """
    Create a new Google Artifact Registry pull secret.

    By default, sets email='none@google.com', username='_json_key'
    """
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)

        secret_obj = PullSecret(
            id="",
            name=name,
            server=server,
            username="_json_key",
            password=json_key,
            email="none@google.com",
        )
        secret_id = await client.create_secret(project_id=project_id, secret=secret_obj)
        print(f"[green]Created GAR pull secret:[/] {secret_id}")
    finally:
        await client.close()


@gar_app.async_command("update")
async def update_gar_secret(
    *,
    project_id: ProjectIdOption = None,
    secret_id: Annotated[str, typer.Option(help="Existing secret ID")],
    name: Annotated[str, typer.Option(help="Secret name")],
    server: Annotated[str, typer.Option(help="GAR host, e.g. us-west1-docker.pkg.dev")],
    json_key: Annotated[
        str, typer.Option(help="Entire GCP service account JSON as string")
    ],
):
    """
    Update an existing Google Artifact Registry pull secret.
    """
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        secret_obj = PullSecret(
            id=secret_id,
            name=name,
            server=server,
            username="_json_key",
            password=json_key,
            email="none@google.com",
        )
        await client.update_secret(project_id=project_id, secret=secret_obj)
        print(f"[green]GAR pull secret {secret_id} updated.[/]")
    finally:
        await client.close()


# --------------------------------------------------------------------------
#  Additional commands (shared by all secrets): list, delete
# --------------------------------------------------------------------------


@secrets_app.async_command("list")
async def secret_list(*, project_id: ProjectIdOption = None):
    """List all secrets in the project (typed as Docker/ACR/GAR or Keys secrets)."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)

        secrets: list[SecretLike] = await client.list_secrets(project_id)

        # Convert each secret → plain dict for tabular output
        rows = []
        for s in secrets:
            row = {
                "id": s.id,
                "name": s.name,
                "type": s.type,
            }
            # If it's a KeysSecret, row["data_keys"] = ...
            if hasattr(s, "data"):
                # For Docker-ish secrets, 'data' typically has server/username/password
                if isinstance(s, PullSecret):
                    row["data_keys"] = "server, username, password"
                else:
                    # KeysSecret
                    row["data_keys"] = ", ".join(s.data.keys())
            rows.append(row)

        print_json_table(rows, "id", "type", "name", "data_keys")

    finally:
        await client.close()


@secrets_app.async_command("delete")
async def secret_delete(
    *,
    project_id: ProjectIdOption = None,
    secret_id: Annotated[str, typer.Argument(help="ID of the secret to delete")],
):
    """Delete a secret."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        await client.delete_secret(project_id=project_id, secret_id=secret_id)
        print(f"[green]Secret {secret_id} deleted.[/]")
    finally:
        await client.close()


# --------------------------------------------------------------------------
#  Wire up sub-apps
# --------------------------------------------------------------------------
secrets_app.add_typer(keys_app, name="keys")
secrets_app.add_typer(docker_app, name="docker")
secrets_app.add_typer(acr_app, name="acr")
secrets_app.add_typer(gar_app, name="gar")

app = secrets_app


# If you want to attach `secrets_app` to your main CLI app, do something like:
# main_app = async_typer.AsyncTyper()
# main_app.add_typer(secrets_app, name="secrets")
# if __name__ == "__main__":
#     main_app()
