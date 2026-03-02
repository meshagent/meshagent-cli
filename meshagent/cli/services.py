# ---------------------------------------------------------------------------
#  Imports
# ---------------------------------------------------------------------------
import typer
from rich import print
from typing import Annotated, Literal, Optional
from meshagent.cli.common_options import ProjectIdOption
from aiohttp import ClientResponseError
import pathlib
import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen
import yaml
from meshagent.cli import async_typer
from meshagent.api.services import well_known_service_path
from meshagent.api.oauth import OAuthClientConfig
from meshagent.api.specs.service import (
    ANNOTATION_SERVICE_ID,
    AgentTemplateSpec,
    ContainerTemplateSpec,
    ExternalServiceSpec,
    ExternalServiceTemplateSpec,
    MCPEndpointSpec,
    EndpointSpec,
    PortSpec,
    ServiceMetadata,
    ServiceSpec,
    ServiceTemplateContainerMountSpec,
    ServiceTemplateMetadata,
    ServiceTemplateSpec,
    TemplateEnvironmentVariable,
)
from meshagent.api.keys import parse_api_key

import asyncio
import shlex

import os
import signal
import atexit
import ctypes
import sys


from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
    resolve_key,
)
from meshagent.api import (
    ParticipantToken,
    ApiScope,
)
from meshagent.cli.common_options import OutputFormatOption

from pydantic import RootModel
from pydantic_yaml import parse_yaml_raw_as


from meshagent.cli.call import _make_call


app = async_typer.AsyncTyper(help="Manage services for your project")


class ServiceTemplateValues(RootModel[dict[str, str]]):
    pass


@dataclass(slots=True)
class _DiscoveredOAuthEndpoints:
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: Optional[str]
    no_pkce: Optional[bool]


_SpecFormat = Literal["service", "template"]


def _load_template_values(
    values_file: Optional[str],
    values: Optional[list[str]] = None,
) -> dict[str, str]:
    template_values: dict[str, str] = {}

    if values_file is not None:
        with open(str(pathlib.Path(values_file).expanduser().resolve()), "rb") as f:
            template_values = parse_yaml_raw_as(ServiceTemplateValues, f.read()).root

    if values:
        for item in values:
            if "=" not in item:
                raise typer.BadParameter("Template values must be key=value")
            key, value = item.split("=", 1)
            if not key:
                raise typer.BadParameter("Template values must include a key")
            template_values[key] = value

    return template_values


def _load_yaml_bytes(*, file: Optional[str], url: Optional[str], name: str) -> bytes:
    if file and url:
        raise typer.BadParameter("Provide only one of --file or --url")
    if not file and not url:
        raise typer.BadParameter("Provide --file or --url")

    if url:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise typer.BadParameter("URL must start with http:// or https://")
        try:
            with urlopen(url) as resp:
                return resp.read()
        except Exception as exc:
            raise typer.BadParameter(f"Unable to read {name} from {url}: {exc}")

    try:
        with open(str(pathlib.Path(file).expanduser().resolve()), "rb") as f:
            return f.read()
    except Exception as exc:
        raise typer.BadParameter(f"Unable to read {name} from {file}: {exc}")


def _load_yaml_text(*, file: Optional[str], url: Optional[str], name: str) -> str:
    if file and url:
        raise typer.BadParameter("Provide only one of --file or --url")
    if not file and not url:
        raise typer.BadParameter("Provide --file or --url")

    if url:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise typer.BadParameter("URL must start with http:// or https://")
        try:
            with urlopen(url) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset)
        except Exception as exc:
            raise typer.BadParameter(f"Unable to read {name} from {url}: {exc}")

    try:
        with open(
            str(pathlib.Path(file).expanduser().resolve()), "r", encoding="utf-8"
        ) as f:
            return f.read()
    except Exception as exc:
        raise typer.BadParameter(f"Unable to read {name} from {file}: {exc}")


def _ensure_single_source(
    *, file: Optional[str], url: Optional[str], mcp: Optional[str], name: str
) -> None:
    provided = [file is not None, url is not None, mcp is not None]
    if sum(provided) != 1:
        raise typer.BadParameter(
            f"Provide exactly one of --file, --url, or --mcp for {name}"
        )


def _normalize_http_url(value: str) -> str:
    trimmed = value.strip()
    if trimmed == "":
        raise typer.BadParameter("MCP server URL cannot be empty")

    parsed = urlparse(trimmed)
    if parsed.scheme == "":
        parsed = urlparse(f"https://{trimmed}")

    if parsed.scheme not in ("http", "https"):
        raise typer.BadParameter("MCP server URL must use http:// or https://")
    if parsed.netloc == "":
        raise typer.BadParameter("MCP server URL must include a hostname")
    if parsed.query != "" or parsed.fragment != "":
        raise typer.BadParameter(
            "MCP server URL must not include query parameters or a fragment"
        )

    path = parsed.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _oauth_discovery_urls(*, server_url: str) -> list[str]:
    parsed = urlparse(server_url)
    path = parsed.path or ""
    stripped_path = path.rstrip("/")
    if stripped_path == "":
        stripped_path = ""

    candidates = [
        urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                f"/.well-known/oauth-authorization-server{stripped_path}",
                "",
                "",
                "",
            )
        ),
        urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                f"/.well-known/openid-configuration{stripped_path}",
                "",
                "",
                "",
            )
        ),
        urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                "/.well-known/oauth-authorization-server",
                "",
                "",
                "",
            )
        ),
        urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                "/.well-known/openid-configuration",
                "",
                "",
                "",
            )
        ),
    ]

    ordered: list[str] = []
    for candidate in candidates:
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _normalize_discovered_endpoint(
    *, metadata_url: str, value: object
) -> Optional[str]:
    if not isinstance(value, str):
        return None
    endpoint = value.strip()
    if endpoint == "":
        return None
    return urljoin(metadata_url, endpoint)


def _fetch_json_url(url: str) -> dict[str, object]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "meshagent-cli/0.28",
        },
    )
    with urlopen(request, timeout=10) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        payload = json.loads(resp.read().decode(charset))

    if not isinstance(payload, dict):
        raise ValueError("metadata payload is not an object")
    return payload


async def _discover_oauth_endpoints_for_mcp(
    *, server_url: str
) -> Optional[_DiscoveredOAuthEndpoints]:
    for metadata_url in _oauth_discovery_urls(server_url=server_url):
        try:
            payload = await asyncio.to_thread(_fetch_json_url, metadata_url)
        except Exception:
            continue

        authorization_endpoint = _normalize_discovered_endpoint(
            metadata_url=metadata_url,
            value=payload.get("authorization_endpoint"),
        )
        token_endpoint = _normalize_discovered_endpoint(
            metadata_url=metadata_url,
            value=payload.get("token_endpoint"),
        )
        registration_endpoint = _normalize_discovered_endpoint(
            metadata_url=metadata_url,
            value=payload.get("registration_endpoint"),
        )

        if authorization_endpoint is None or token_endpoint is None:
            continue

        no_pkce: Optional[bool] = None
        methods = payload.get("code_challenge_methods_supported")
        if isinstance(methods, list):
            normalized_methods = {
                method.strip().upper()
                for method in methods
                if isinstance(method, str) and method.strip() != ""
            }
            if len(normalized_methods) > 0:
                no_pkce = not (
                    "S256" in normalized_methods or "PLAIN" in normalized_methods
                )

        return _DiscoveredOAuthEndpoints(
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            registration_endpoint=registration_endpoint,
            no_pkce=no_pkce,
        )

    return None


def _slugify_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if slug == "":
        return "mcp-service"
    if not slug[0].isalpha():
        return f"mcp-{slug}"
    return slug


def _default_mcp_service_name(*, server_url: str) -> str:
    parsed = urlparse(server_url)
    host = parsed.hostname or "mcp-service"
    if host.startswith("www."):
        host = host[4:]

    host_parts = [part for part in host.split(".") if part != ""]
    host_token = host_parts[0] if len(host_parts) > 0 else "mcp"
    if host_token == "mcp" and len(host_parts) > 1:
        host_token = host_parts[1]

    segments = [segment for segment in parsed.path.split("/") if segment != ""]
    path_token: Optional[str] = None
    if len(segments) > 0:
        last = segments[-1]
        if last.lower() == "mcp" and len(segments) > 1:
            path_token = segments[-2]
        elif last.lower() != "mcp":
            path_token = last

    if path_token is None or path_token.lower() == host_token.lower():
        return _slugify_name(host_token)
    return _slugify_name(f"{host_token}-{path_token}")


def _build_external_mcp_service_spec(
    *,
    mcp_url: str,
    oauth: Optional[OAuthClientConfig],
) -> ServiceSpec:
    parsed = urlparse(mcp_url)
    endpoint_path = parsed.path or "/"
    if endpoint_path != "/" and endpoint_path.endswith("/"):
        endpoint_path = endpoint_path.rstrip("/")

    external_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    if parsed.port is not None:
        port_num = parsed.port
    elif parsed.scheme == "http":
        port_num = 80
    else:
        port_num = 443

    service_name = _default_mcp_service_name(server_url=mcp_url)

    return ServiceSpec(
        kind="Service",
        version="v1",
        metadata=ServiceMetadata(
            name=service_name,
            description=f"External MCP service at {mcp_url}",
        ),
        external=ExternalServiceSpec(url=external_url),
        ports=[
            PortSpec(
                num=port_num,
                type="http",
                endpoints=[
                    EndpointSpec(
                        path=endpoint_path,
                        mcp=MCPEndpointSpec(
                            label=service_name,
                            description=f"MCP tools exposed by {mcp_url}",
                            oauth=oauth,
                        ),
                    )
                ],
            )
        ],
    )


async def _load_service_spec(
    *, file: Optional[str], url: Optional[str], mcp: Optional[str]
) -> ServiceSpec:
    _ensure_single_source(file=file, url=url, mcp=mcp, name="service definition")
    if mcp is None:
        spec_bytes = _load_yaml_bytes(file=file, url=url, name="service definition")
        return parse_yaml_raw_as(ServiceSpec, spec_bytes)

    normalized_mcp_url = _normalize_http_url(mcp)
    discovered = await _discover_oauth_endpoints_for_mcp(server_url=normalized_mcp_url)
    if discovered is None:
        # Server does not advertise OAuth metadata; treat as an unauthenticated MCP.
        return _build_external_mcp_service_spec(
            mcp_url=normalized_mcp_url,
            oauth=None,
        )

    registration_endpoint = discovered.registration_endpoint

    if (
        not isinstance(registration_endpoint, str)
        or registration_endpoint.strip() == ""
    ):
        raise typer.BadParameter(
            "The MCP server does not support OAuth dynamic client registration "
            "(missing registration_endpoint in discovered metadata)."
        )

    return _build_external_mcp_service_spec(
        mcp_url=normalized_mcp_url,
        # Keep oauth present but empty so room-side resolution can discover
        # provider metadata and perform dynamic client registration at runtime.
        oauth=OAuthClientConfig(
            no_pkce=discovered.no_pkce if discovered.no_pkce is not None else False
        ),
    )


def _service_spec_to_template_spec(spec: ServiceSpec) -> ServiceTemplateSpec:
    if spec.external is not None and spec.external.url is None:
        raise typer.BadParameter(
            "Cannot convert a service spec with external.url unset into template format."
        )

    container_template: Optional[ContainerTemplateSpec] = None
    if spec.container is not None:
        template_env: Optional[list[TemplateEnvironmentVariable]] = None
        if spec.container.environment is not None:
            template_env = [
                TemplateEnvironmentVariable(
                    name=env.name,
                    value=env.value,
                    token=env.token,
                    secret=env.secret,
                )
                for env in spec.container.environment
            ]

        template_storage: Optional[ServiceTemplateContainerMountSpec] = None
        if spec.container.storage is not None:
            template_storage = ServiceTemplateContainerMountSpec(
                room=spec.container.storage.room,
                project=spec.container.storage.project,
                images=spec.container.storage.images,
                files=spec.container.storage.files,
            )

        container_template = ContainerTemplateSpec(
            environment=template_env,
            image=spec.container.image,
            command=spec.container.command,
            working_dir=spec.container.working_dir,
            storage=template_storage,
            on_demand=spec.container.on_demand,
            writable_root_fs=spec.container.writable_root_fs,
            private=spec.container.private,
        )

    return ServiceTemplateSpec(
        version=spec.version,
        kind="ServiceTemplate",
        metadata=ServiceTemplateMetadata(
            name=spec.metadata.name,
            description=spec.metadata.description,
            repo=spec.metadata.repo,
            icon=spec.metadata.icon,
            annotations=spec.metadata.annotations,
        ),
        agents=[
            AgentTemplateSpec(
                name=agent.name,
                description=agent.description,
                annotations=agent.annotations,
            )
            for agent in spec.agents
        ]
        if spec.agents is not None
        else None,
        ports=spec.ports,
        container=container_template,
        external=ExternalServiceTemplateSpec(url=spec.external.url)
        if spec.external is not None
        else None,
    )


def _apply_service_id_annotation(
    *, model: ServiceSpec | ServiceTemplateSpec, service_id: str
) -> ServiceSpec | ServiceTemplateSpec:
    normalized_id = service_id.strip()
    if normalized_id == "":
        raise typer.BadParameter("--service-id cannot be empty")

    if isinstance(model, ServiceSpec):
        annotations = dict(model.metadata.annotations or {})
        annotations[ANNOTATION_SERVICE_ID] = normalized_id
        return model.model_copy(
            update={
                "metadata": model.metadata.model_copy(
                    update={"annotations": annotations}
                )
            }
        )

    annotations = dict(model.metadata.annotations or {})
    annotations[ANNOTATION_SERVICE_ID] = normalized_id
    return model.model_copy(
        update={
            "metadata": model.metadata.model_copy(update={"annotations": annotations})
        }
    )


def _dump_model_yaml(model: ServiceSpec | ServiceTemplateSpec) -> str:
    return yaml.safe_dump(
        model.model_dump(mode="json", exclude_none=True),
        sort_keys=False,
        allow_unicode=False,
    )


def _load_input_as_service_spec(
    *, file: Optional[str], url: Optional[str]
) -> ServiceSpec:
    spec_bytes = _load_yaml_bytes(file=file, url=url, name="service definition")
    try:
        return parse_yaml_raw_as(ServiceSpec, spec_bytes)
    except Exception:
        template_text = _load_yaml_text(file=file, url=url, name="service template")
        template = parse_yaml_raw_as(ServiceTemplateSpec, template_text)
        return template.to_service_spec()


def _load_input_as_template_spec(
    *, file: Optional[str], url: Optional[str]
) -> ServiceTemplateSpec:
    template_text = _load_yaml_text(file=file, url=url, name="service template")
    try:
        return parse_yaml_raw_as(ServiceTemplateSpec, template_text)
    except Exception:
        service = parse_yaml_raw_as(ServiceSpec, template_text.encode("utf-8"))
        return _service_spec_to_template_spec(service)


async def _load_spec_output(
    *,
    file: Optional[str],
    url: Optional[str],
    mcp: Optional[str],
    format: _SpecFormat,
) -> ServiceSpec | ServiceTemplateSpec:
    _ensure_single_source(file=file, url=url, mcp=mcp, name="service definition")

    if mcp is not None:
        service_spec = await _load_service_spec(file=None, url=None, mcp=mcp)
    else:
        service_spec = _load_input_as_service_spec(file=file, url=url)

    if format == "service":
        return service_spec
    return _service_spec_to_template_spec(service_spec)


@app.async_command("spec")
async def service_spec(
    *,
    file: Annotated[
        Optional[str],
        typer.Option("--file", "-f", help="File path to a service definition"),
    ] = None,
    url: Annotated[
        Optional[str],
        typer.Option("--url", help="URL to a service definition"),
    ] = None,
    mcp: Annotated[
        Optional[str],
        typer.Option(
            "--mcp",
            help=(
                "MCP server URL. Auto-discovers metadata and generates a service spec without creating it."
            ),
        ),
    ] = None,
    format: Annotated[
        _SpecFormat,
        typer.Option(
            "--format",
            help="Output format. 'service' emits Service YAML. 'template' emits ServiceTemplate YAML.",
        ),
    ] = "service",
):
    """Render a service or template YAML spec without creating a service."""
    model = await _load_spec_output(file=file, url=url, mcp=mcp, format=format)
    print(_dump_model_yaml(model), end="")


@app.async_command("service-id")
async def service_add_service_id(
    *,
    file: Annotated[
        Optional[str],
        typer.Option("--file", "-f", help="File path to a service definition"),
    ] = None,
    url: Annotated[
        Optional[str],
        typer.Option("--url", help="URL to a service definition"),
    ] = None,
    mcp: Annotated[
        Optional[str],
        typer.Option(
            "--mcp",
            help=(
                "MCP server URL. Auto-discovers metadata and generates a spec before adding meshagent.service.id."
            ),
        ),
    ] = None,
    service_id: Annotated[
        Optional[str],
        typer.Option(
            "--service-id",
            help=(
                "Value for meshagent.service.id. Defaults to metadata.name when omitted."
            ),
        ),
    ] = None,
    format: Annotated[
        _SpecFormat,
        typer.Option(
            "--format",
            help="Output format. 'service' emits Service YAML. 'template' emits ServiceTemplate YAML.",
        ),
    ] = "service",
):
    """Render YAML with meshagent.service.id annotation applied."""
    model = await _load_spec_output(file=file, url=url, mcp=mcp, format=format)
    effective_service_id = service_id or model.metadata.name
    model_with_id = _apply_service_id_annotation(
        model=model, service_id=effective_service_id
    )
    print(_dump_model_yaml(model_with_id), end="")


@app.async_command("create")
async def service_create(
    *,
    project_id: ProjectIdOption,
    file: Annotated[
        Optional[str],
        typer.Option("--file", "-f", help="File path to a service definition"),
    ] = None,
    url: Annotated[
        Optional[str],
        typer.Option("--url", help="URL to a service definition"),
    ] = None,
    mcp: Annotated[
        Optional[str],
        typer.Option(
            "--mcp",
            help=(
                "MCP server URL. Auto-discovers OAuth metadata and creates an external MCP service "
                "configured for dynamic client registration."
            ),
        ),
    ] = None,
    room: Annotated[
        Optional[str], typer.Option("--room", help="Room name")
    ] = os.getenv("MESHAGENT_ROOM"),
):
    """Create a service attached to the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        spec = await _load_service_spec(file=file, url=url, mcp=mcp)

        if spec.id is not None:
            print("[red]id cannot be set when creating a service[/red]")
            raise typer.Exit(code=1)

        try:
            if room is None:
                new_id = await client.create_service(
                    project_id=project_id, service=spec
                )
            else:
                new_id = await client.create_room_service(
                    project_id=project_id, service=spec, room_name=room
                )
        except ClientResponseError as exc:
            if exc.status == 409:
                print(f"[red]Service name already in use: {spec.metadata.name}[/red]")
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Created service:[/] {new_id}")

    finally:
        await client.close()


@app.async_command("update")
async def service_update(
    *,
    project_id: ProjectIdOption,
    id: Optional[str] = None,
    file: Annotated[
        Optional[str],
        typer.Option("--file", "-f", help="File path to a service definition"),
    ] = None,
    url: Annotated[
        Optional[str],
        typer.Option("--url", help="URL to a service definition"),
    ] = None,
    mcp: Annotated[
        Optional[str],
        typer.Option(
            "--mcp",
            help=(
                "MCP server URL. Auto-discovers OAuth metadata and builds an external MCP service "
                "configured for dynamic client registration."
            ),
        ),
    ] = None,
    create: Annotated[
        Optional[bool],
        typer.Option(
            help="create the service if it does not exist",
        ),
    ] = False,
    room: Annotated[
        Optional[str], typer.Option("--room", help="Room name")
    ] = os.getenv("MESHAGENT_ROOM"),
):
    """Create a service attached to the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        spec = await _load_service_spec(file=file, url=url, mcp=mcp)
        if spec.id is not None:
            id = spec.id

        try:
            if id is None:
                if room is None:
                    services = await client.list_services(project_id=project_id)
                else:
                    services = await client.list_room_services(
                        project_id=project_id, room_name=room
                    )

                for s in services:
                    if s.metadata.name == spec.metadata.name:
                        id = s.id

            if id is None and not create:
                print("[red]pass a service id or specify --create[/red]")
                raise typer.Exit(code=1)

            if id is None:
                if room is None:
                    id = await client.create_service(
                        project_id=project_id, service=spec
                    )
                else:
                    id = await client.create_room_service(
                        project_id=project_id, service=spec, room_name=room
                    )

            else:
                spec.id = id
                if room is None:
                    await client.update_service(
                        project_id=project_id, service_id=id, service=spec
                    )
                else:
                    await client.update_room_service(
                        project_id=project_id,
                        service_id=id,
                        service=spec,
                        room_name=room,
                    )

        except ClientResponseError as exc:
            if exc.status == 409:
                print(f"[red]Service name already in use: {spec.metadata.name}[/red]")
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Updated service:[/] {id}")

    finally:
        await client.close()


@app.async_command("validate")
async def service_validate(
    *,
    file: Annotated[
        str,
        typer.Option("--file", "-f", help="File path to a service definition"),
    ],
):
    """Validate a service spec from a YAML file."""
    try:
        with open(str(pathlib.Path(file).expanduser().resolve()), "rb") as f:
            spec = parse_yaml_raw_as(ServiceSpec, f.read())
    except Exception as exc:
        print(f"[red]Invalid service spec: {exc}[/red]")
        raise typer.Exit(code=1)

    print(f"[green]Service spec is valid:[/] {spec.metadata.name}")


@app.async_command("create-template")
async def service_create_template(
    *,
    project_id: ProjectIdOption,
    file: Annotated[
        Optional[str],
        typer.Option("--file", "-f", help="File path to a service template"),
    ] = None,
    url: Annotated[
        Optional[str],
        typer.Option("--url", help="URL to a service template"),
    ] = None,
    values: Annotated[
        Optional[str],
        typer.Option("--values-file", help="File path to template values"),
    ] = None,
    value: Annotated[
        Optional[list[str]],
        typer.Option(
            "--value",
            "-v",
            help="Template value override (key=value)",
        ),
    ] = None,
    room: Annotated[
        Optional[str], typer.Option("--room", help="Room name")
    ] = os.getenv("MESHAGENT_ROOM"),
):
    """Create a service from a ServiceTemplate spec."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)

        template_text = _load_yaml_text(file=file, url=url, name="service template")
        template_spec = parse_yaml_raw_as(ServiceTemplateSpec, template_text)

        template_values = _load_template_values(values, value)

        try:
            if room is None:
                service = await client.create_service_from_template(
                    project_id=project_id,
                    template=template_text,
                    values=template_values,
                )
            else:
                service = await client.create_room_service_from_template(
                    project_id=project_id,
                    template=template_text,
                    values=template_values,
                    room_name=room,
                )
        except ClientResponseError as exc:
            if exc.status == 409:
                print(
                    f"[red]Service name already in use: {template_spec.metadata.name}[/red]"
                )
                raise typer.Exit(code=1)
            raise
        else:
            service_id = service.id or ""
            print(f"[green]Created service:[/] {service_id}")

    finally:
        await client.close()


@app.async_command("update-template")
async def service_update_template(
    *,
    project_id: ProjectIdOption,
    id: Optional[str] = None,
    file: Annotated[
        Optional[str],
        typer.Option("--file", "-f", help="File path to a service template"),
    ] = None,
    url: Annotated[
        Optional[str],
        typer.Option("--url", help="URL to a service template"),
    ] = None,
    values: Annotated[
        Optional[str],
        typer.Option("--values-file", help="File path to template values"),
    ] = None,
    value: Annotated[
        Optional[list[str]],
        typer.Option(
            "--value",
            "-v",
            help="Template value override (key=value)",
        ),
    ] = None,
    create: Annotated[
        Optional[bool],
        typer.Option(
            help="create the service if it does not exist",
        ),
    ] = False,
    room: Annotated[
        Optional[str], typer.Option("--room", help="Room name")
    ] = os.getenv("MESHAGENT_ROOM"),
):
    """Update a service using a ServiceTemplate spec."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)

        template_text = _load_yaml_text(file=file, url=url, name="service template")
        template_spec = parse_yaml_raw_as(ServiceTemplateSpec, template_text)

        template_values = _load_template_values(values, value)

        try:
            if id is None:
                if room is None:
                    services = await client.list_services(project_id=project_id)
                else:
                    services = await client.list_room_services(
                        project_id=project_id, room_name=room
                    )

                for s in services:
                    if s.metadata.name == template_spec.metadata.name:
                        id = s.id

            if id is None and not create:
                print("[red]pass a service id or specify --create[/red]")
                raise typer.Exit(code=1)

            if id is None:
                if room is None:
                    service = await client.create_service_from_template(
                        project_id=project_id,
                        template=template_text,
                        values=template_values,
                    )
                else:
                    service = await client.create_room_service_from_template(
                        project_id=project_id,
                        template=template_text,
                        values=template_values,
                        room_name=room,
                    )
                id = service.id
            else:
                if room is None:
                    service = await client.update_service_from_template(
                        project_id=project_id,
                        service_id=id,
                        template=template_text,
                        values=template_values,
                    )
                else:
                    service = await client.update_room_service_from_template(
                        project_id=project_id,
                        service_id=id,
                        template=template_text,
                        values=template_values,
                        room_name=room,
                    )
                if service.id is not None:
                    id = service.id

        except ClientResponseError as exc:
            if exc.status == 409:
                print(
                    f"[red]Service name already in use: {template_spec.metadata.name}[/red]"
                )
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Updated service:[/] {id}")

    finally:
        await client.close()


@app.async_command("validate-template")
async def service_validate_template(
    *,
    file: Annotated[
        str,
        typer.Option("--file", "-f", help="File path to a service template"),
    ],
):
    """Validate a service template from a YAML file."""
    try:
        with open(str(pathlib.Path(file).expanduser().resolve()), "rb") as f:
            template = parse_yaml_raw_as(ServiceTemplateSpec, f.read())
    except Exception as exc:
        print(f"[red]Invalid service template: {exc}[/red]")
        raise typer.Exit(code=1)

    print(f"[green]Service template is valid:[/] {template.metadata.name}")


@app.async_command(
    "run",
    help="Run a local command and register it as a temporary room service.",
)
async def service_run(
    *,
    project_id: ProjectIdOption,
    command: str,
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help=(
                "a port number to run the agent on (will set MESHAGENT_PORT environment variable when launching the service)"
            ),
        ),
    ] = None,
    room: Annotated[
        Optional[str], typer.Option("--room", help="Room name")
    ] = os.getenv("MESHAGENT_ROOM"),
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
):
    key = await resolve_key(project_id=project_id, key=key)

    if port is None:
        import socket

        def find_free_port():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("", 0))  # Bind to a free port provided by the host.
                s.listen(1)
                return s.getsockname()[1]

        port = find_free_port()

    my_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        room = resolve_room(room)

        if room is None:
            print("[bold red]Room was not set[/bold red]")
            raise typer.Exit(1)

        try:
            parsed_key = parse_api_key(key)
            token = ParticipantToken(
                name="cli", project_id=project_id, api_key_id=parsed_key.id
            )
            token.add_api_grant(ApiScope.agent_default())
            token.add_role_grant("user")
            token.add_room_grant(room)

            print("[bold green]Connecting to room...[/bold green]")

            run_tasks = []

            async def run_service(port: int):
                if command.endswith(".py"):
                    code, output = await _run_process(
                        cmd=shlex.split("python3 " + command),
                        log=True,
                        env={**os.environ, "MESHAGENT_PORT": str(port)},
                    )

                elif command.endswith(".dart"):
                    code, output = await _run_process(
                        cmd=shlex.split("dart run " + command),
                        log=True,
                        env={**os.environ, "MESHAGENT_PORT": str(port)},
                    )

                else:
                    code, output = await _run_process(
                        cmd=shlex.split(command),
                        log=True,
                        env={**os.environ, "MESHAGENT_PORT": str(port)},
                    )

                if code != 0:
                    print(f"[red]{output}[/red]")

            run_tasks.append(asyncio.create_task(run_service(port)))

            async def get_spec(port: int, attempt=0) -> ServiceSpec:
                from meshagent.api.http import new_client_session

                max_attempts = 10

                url = f"http://localhost:{port}{well_known_service_path}"

                async with new_client_session() as session:
                    try:
                        res = await session.get(url=url)
                        res.raise_for_status()

                        spec_json = await res.json()

                        return ServiceSpec.model_validate(spec_json)

                    except Exception:
                        if attempt < max_attempts:
                            backoff = 0.1 * pow(2, attempt)
                            await asyncio.sleep(backoff)
                            return await get_spec(port, attempt + 1)
                        else:
                            print("[red]unable to read service spec[/red]")
                            raise typer.Exit(-1)

            print(f"getting spec {port}", flush=True)
            spec = await get_spec(port)

            sys.stdout.write("\n")

            for p in spec.ports or []:
                print(f"[bold green]Connecting port {p.num}...[/bold green]")

                for endpoint in p.endpoints:
                    print(
                        f"[bold green]Connecting endpoint {endpoint.path}...[/bold green]"
                    )

                    run_tasks.append(
                        asyncio.create_task(
                            _make_call(
                                room=room,
                                project_id=project_id,
                                participant_name=endpoint.meshagent.identity,
                                url=f"http://localhost:{p.num}{endpoint.path}",
                                arguments={},
                                key=key,
                                permissions=endpoint.meshagent.api,
                            )
                        )
                    )

            await asyncio.gather(*run_tasks)

        except ClientResponseError as exc:
            if exc.status == 409:
                print(f"[red]Room already in use: {room}[/red]")
                raise typer.Exit(code=1)
            raise

        except Exception as e:
            print(e)
            raise typer.Exit(code=1)

    finally:
        await my_client.close()


@app.async_command("show")
async def service_show(
    *,
    project_id: ProjectIdOption,
    service_id: Annotated[str, typer.Argument(help="ID of the service to show")],
    room: Annotated[
        Optional[str], typer.Option("--room", help="Room name")
    ] = os.getenv("MESHAGENT_ROOM"),
):
    """Show a services for the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        if room is not None:
            service = await client.get_room_service(
                project_id=project_id, service_id=service_id, room_name=room
            )  # → List[Service]
        else:
            service = await client.get_service(
                project_id=project_id, service_id=service_id
            )  # → List[Service]
        print(service.model_dump(mode="json"))
    finally:
        await client.close()


@app.async_command("list")
async def service_list(
    *,
    project_id: ProjectIdOption,
    o: OutputFormatOption = "table",
    room: Annotated[
        Optional[str], typer.Option("--room", help="Room name")
    ] = os.getenv("MESHAGENT_ROOM"),
):
    """List all services for the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        services: list[ServiceSpec] = (
            (await client.list_services(project_id=project_id))
            if room is None
            else (
                await client.list_room_services(project_id=project_id, room_name=room)
            )
        )

        if o == "json":
            print({"services": [svc.model_dump(mode="json") for svc in services]})
        else:
            print_json_table(
                [
                    {
                        "id": svc.id,
                        "name": svc.metadata.name,
                        "image": svc.container.image
                        if svc.container is not None
                        else None,
                    }
                    for svc in services
                ],
                "id",
                "name",
                "image",
            )
    finally:
        await client.close()


@app.async_command("delete")
async def service_delete(
    *,
    project_id: ProjectIdOption,
    service_id: Annotated[str, typer.Argument(help="ID of the service to delete")],
    room: Annotated[
        Optional[str], typer.Option("--room", help="Room name")
    ] = os.getenv("MESHAGENT_ROOM"),
):
    """Delete a service."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        if room is not None:
            await client.delete_room_service(
                project_id=project_id, service_id=service_id, room_name=room
            )
        else:
            await client.delete_service(project_id=project_id, service_id=service_id)
        print(f"[green]Service {service_id} deleted.[/]")
    finally:
        await client.close()


async def _run_process(
    cmd: list[str], cwd=None, env=None, timeout: float | None = None, log: bool = False
) -> tuple[int, str]:
    """
    Spawn a process, stream its output line-by-line as it runs, and return its exit code.
    stdout+stderr are merged to preserve ordering.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        preexec_fn=_preexec_fn,
    )

    _spawned.append(proc)

    output = []
    try:
        # Stream lines as they appear
        assert proc.stdout is not None
        while True:
            line = (
                await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                if timeout
                else await proc.stdout.readline()
            )
            if not line:
                break
            ln = line.decode(errors="replace").rstrip()
            if log:
                print(ln, flush=True)
            output.append(ln)  # or send to a logger/queue

        return await proc.wait(), "".join(output)
    except asyncio.TimeoutError:
        # Graceful shutdown on timeout
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        raise


# Linux-only: send SIGTERM to child if parent dies
_PRCTL_AVAILABLE = sys.platform.startswith("linux")
if _PRCTL_AVAILABLE:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    PR_SET_PDEATHSIG = 1


def _preexec_fn():
    # Make child the leader of a new session/process group
    os.setsid()
    # On Linux, ensure child gets SIGTERM if parent dies unexpectedly
    if _PRCTL_AVAILABLE:
        if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM) != 0:
            err = ctypes.get_errno()
            raise OSError(err, "prctl(PR_SET_PDEATHSIG) failed")


_spawned = []


def _cleanup():
    # Kill each child's process group (created by setsid)
    for p in _spawned:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            pass


atexit.register(_cleanup)
