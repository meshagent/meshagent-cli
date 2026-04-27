import pytest
import typer
import json
import pathlib
from typing import Optional
from click.testing import CliRunner

from meshagent.cli import async_typer, cli, services


_SERVICE_YAML = """\
version: v1
kind: Service
metadata:
  name: demo
ports: []
container:
  image: busybox
"""

_SERVICE_WITH_ID_YAML = """\
version: v1
kind: Service
metadata:
  name: demo
  annotations:
    meshagent.service.id: demo.service
ports: []
container:
  image: busybox
"""

_SERVICE_TEMPLATE_YAML = """\
version: v1
kind: ServiceTemplate
metadata:
  name: demo
ports: []
container:
  image: busybox
"""

_SERVICE_TEMPLATE_WITH_ID_YAML = """\
version: v1
kind: ServiceTemplate
metadata:
  name: demo
  annotations:
    meshagent.service.id: demo.service
ports: []
container:
  image: busybox
"""

_JINJA_SERVICE_TEMPLATE_YAML = """\
version: v1
kind: ServiceTemplate
metadata:
  name: assistant
variables:
  - name: email
    type: email
    optional: true
container:
  image: meshagent/cli:default
{% if email %}
  command: >
    /usr/bin/meshagent multi join --email-address={{email}}
{% else %}
  command: >
    /usr/bin/meshagent chatbot join
{% endif %}
"""

_INVALID_SERVICE_YAML = """\
version: v1
kind: Service
ports: []
"""

_INVALID_SERVICE_TEMPLATE_YAML = """\
version: v1
kind: ServiceTemplate
ports: []
"""


def test_service_create_help_mentions_beginner_manifest_flow() -> None:
    result = CliRunner().invoke(
        async_typer.get_command(cli.app),
        ["service", "create", "--help"],
    )

    assert result.exit_code == 0
    assert "meshagent.yaml" in result.output
    assert "does not have a Dockerfile" in result.output
    assert "container.private: false" in result.output
    assert "meshagent.service.id" in result.output


class _UnusedServicesClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def render_template(self, *, template, values):
        try:
            return services.ServiceTemplateSpec.from_yaml(
                yaml=template,
                values=values,
            )
        except Exception as exc:
            raise services.RoomException(str(exc)) from exc

    async def create_service(self, *, project_id, service):
        del project_id, service
        raise AssertionError("create_service should not be called")

    async def create_room_service(self, *, project_id, service, room_name):
        del project_id, service, room_name
        raise AssertionError("create_room_service should not be called")

    async def update_service(self, *, project_id, service_id, service):
        del project_id, service_id, service
        raise AssertionError("update_service should not be called")

    async def update_room_service(self, *, project_id, service_id, service, room_name):
        del project_id, service_id, service, room_name
        raise AssertionError("update_room_service should not be called")

    async def list_services(self, *, project_id):
        del project_id
        raise AssertionError("list_services should not be called")

    async def list_room_services(self, *, project_id, room_name):
        del project_id, room_name
        raise AssertionError("list_room_services should not be called")

    async def create_service_from_template(self, *, project_id, template, values):
        del project_id, template, values
        raise AssertionError("create_service_from_template should not be called")

    async def create_room_service_from_template(
        self, *, project_id, template, values, room_name
    ):
        del project_id, template, values, room_name
        raise AssertionError("create_room_service_from_template should not be called")

    async def update_service_from_template(
        self, *, project_id, service_id, template, values
    ):
        del project_id, service_id, template, values
        raise AssertionError("update_service_from_template should not be called")

    async def update_room_service_from_template(
        self, *, project_id, service_id, template, values, room_name
    ):
        del project_id, service_id, template, values, room_name
        raise AssertionError("update_room_service_from_template should not be called")


class _TemplateCommandClient(_UnusedServicesClient):
    def __init__(self) -> None:
        super().__init__()
        self.render_calls: list[tuple[str, dict[str, str]]] = []
        self.create_calls: list[tuple[str, str, dict[str, str]]] = []
        self.create_room_calls: list[tuple[str, str, str, dict[str, str]]] = []
        self.update_calls: list[tuple[str, str, str, dict[str, str]]] = []
        self.update_room_calls: list[tuple[str, str, str, str, dict[str, str]]] = []
        self.services: list[services.ServiceSpec] = []
        self.room_services: list[services.ServiceSpec] = []

    async def render_template(self, *, template, values):
        self.render_calls.append((template, values))
        return await super().render_template(template=template, values=values)

    async def create_service_from_template(self, *, project_id, template, values):
        self.create_calls.append((project_id, template, values))
        spec = services.ServiceTemplateSpec.from_yaml(yaml=template, values=values)
        service = spec.to_service_spec()
        service.id = "service-1"
        return service

    async def create_room_service_from_template(
        self, *, project_id, template, values, room_name
    ):
        self.create_room_calls.append((project_id, room_name, template, values))
        spec = services.ServiceTemplateSpec.from_yaml(yaml=template, values=values)
        service = spec.to_service_spec()
        service.id = "room-service-1"
        return service

    async def update_service_from_template(
        self, *, project_id, service_id, template, values
    ):
        self.update_calls.append((project_id, service_id, template, values))
        spec = services.ServiceTemplateSpec.from_yaml(yaml=template, values=values)
        service = spec.to_service_spec()
        service.id = service_id
        return service

    async def update_room_service_from_template(
        self, *, project_id, service_id, template, values, room_name
    ):
        self.update_room_calls.append(
            (project_id, room_name, service_id, template, values)
        )
        spec = services.ServiceTemplateSpec.from_yaml(yaml=template, values=values)
        service = spec.to_service_spec()
        service.id = service_id
        return service

    async def list_services(self, *, project_id):
        del project_id
        return [service.model_copy() for service in self.services]

    async def list_room_services(self, *, project_id, room_name):
        del project_id, room_name
        return [service.model_copy() for service in self.room_services]


class _ServiceCommandClient(_UnusedServicesClient):
    def __init__(self) -> None:
        super().__init__()
        self.create_calls: list[tuple[str, services.ServiceSpec]] = []
        self.create_room_calls: list[tuple[str, str, services.ServiceSpec]] = []
        self.update_calls: list[tuple[str, str, services.ServiceSpec]] = []
        self.update_room_calls: list[tuple[str, str, str, services.ServiceSpec]] = []
        self.list_calls: list[str] = []
        self.list_room_calls: list[tuple[str, str]] = []
        self.services: list[services.ServiceSpec] = []
        self.room_services: list[services.ServiceSpec] = []

    async def create_service(self, *, project_id, service):
        self.create_calls.append((project_id, service))
        created = service.model_copy()
        created.id = "service-1"
        return created

    async def create_room_service(self, *, project_id, service, room_name):
        self.create_room_calls.append((project_id, room_name, service))
        return "room-service-1"

    async def update_service(self, *, project_id, service_id, service):
        self.update_calls.append((project_id, service_id, service))
        updated = service.model_copy()
        updated.id = service_id
        return updated

    async def update_room_service(self, *, project_id, room_name, service_id, service):
        self.update_room_calls.append((project_id, room_name, service_id, service))

    async def list_services(self, *, project_id):
        self.list_calls.append(project_id)
        return [service.model_copy() for service in self.services]

    async def list_room_services(self, *, project_id, room_name):
        self.list_room_calls.append((project_id, room_name))
        return [service.model_copy() for service in self.room_services]


def _service_record(
    *,
    id: str,
    name: str,
    service_annotation_id: Optional[str],
) -> services.ServiceSpec:
    annotations: dict[str, str] | None = None
    if service_annotation_id is not None:
        annotations = {services.ANNOTATION_SERVICE_ID: service_annotation_id}
    return services.ServiceSpec(
        version="v1",
        kind="Service",
        id=id,
        metadata=services.ServiceMetadata(name=name, annotations=annotations),
        ports=[],
        container=None,
    )


def _write_yaml(tmp_path: pathlib.Path, name: str, contents: str) -> pathlib.Path:
    path = tmp_path / name
    path.write_text(contents, encoding="utf-8")
    return path


def _capture_prints(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    printed: list[str] = []

    def _fake_print(*args, **kwargs) -> None:
        del kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr(services, "print", _fake_print)
    return printed


def _assert_spec_input_error_output(
    printed: list[str],
    *,
    headline: str,
    description: str,
    detail_fragment: str,
    guidance_lines: tuple[str, ...] = (),
) -> None:
    expected = [
        f"[red]{headline}[/red]",
        "",
        description,
        "",
    ]
    expected.append(f"Validation details: {detail_fragment}")
    if guidance_lines:
        expected.append("")
        expected.extend(guidance_lines)

    assert printed[0] == expected[0]
    assert printed[1] == expected[1]
    assert printed[2] == expected[2]
    assert printed[3] == expected[3]
    assert printed[4].startswith("Validation details:")
    assert detail_fragment in printed[4]
    if guidance_lines:
        assert printed[5:] == expected[5:]
    else:
        assert len(printed) == 5


def _patch_service_command_runtime(
    monkeypatch: pytest.MonkeyPatch,
    client: _UnusedServicesClient | None = None,
) -> _UnusedServicesClient:
    if client is None:
        client = _UnusedServicesClient()

    async def _fake_get_client():
        return client

    async def _fake_resolve_project_id(project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(services, "get_client", _fake_get_client)
    monkeypatch.setattr(services, "resolve_project_id", _fake_resolve_project_id)
    return client


def test_ensure_single_source_accepts_one_source() -> None:
    services._ensure_single_source(
        file="service.yaml",
        url=None,
        mcp=None,
        name="service definition",
    )
    services._ensure_single_source(
        file=None,
        url="https://example.com/service.yaml",
        mcp=None,
        name="service definition",
    )
    services._ensure_single_source(
        file=None,
        url=None,
        mcp="https://mcp.example.com/mcp",
        name="service definition",
    )


@pytest.mark.parametrize(
    ("file", "url", "mcp"),
    [
        (None, None, None),
        ("a.yaml", "https://example.com/s.yaml", None),
        ("a.yaml", None, "https://mcp.example.com/mcp"),
        (None, "https://example.com/s.yaml", "https://mcp.example.com/mcp"),
    ],
)
def test_ensure_single_source_rejects_invalid_combinations(
    file: str | None, url: str | None, mcp: str | None
) -> None:
    with pytest.raises(typer.BadParameter):
        services._ensure_single_source(
            file=file,
            url=url,
            mcp=mcp,
            name="service definition",
        )


def test_oauth_discovery_urls_path_specific_first() -> None:
    urls = services._oauth_discovery_urls(server_url="https://mcp.example.com/v1/mcp")
    assert (
        urls[0]
        == "https://mcp.example.com/.well-known/oauth-authorization-server/v1/mcp"
    )
    assert urls[1] == "https://mcp.example.com/.well-known/openid-configuration/v1/mcp"
    assert "https://mcp.example.com/.well-known/oauth-authorization-server" in urls
    assert "https://mcp.example.com/.well-known/openid-configuration" in urls


def test_build_external_mcp_service_spec_uses_external_base_and_endpoint_path() -> None:
    spec = services._build_external_mcp_service_spec(
        mcp_url="https://mcp.example.com/v1/mcp",
        oauth=services.OAuthClientConfig(
            client_id=None,
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            no_pkce=False,
        ),
    )

    assert spec.kind == "Service"
    assert spec.external is not None
    assert spec.external.url == "https://mcp.example.com"
    assert spec.ports is not None
    assert len(spec.ports) == 1
    assert spec.ports[0].num == 443
    assert len(spec.ports[0].endpoints) == 1
    assert spec.ports[0].endpoints[0].path == "/v1/mcp"
    assert spec.ports[0].endpoints[0].mcp is not None
    assert spec.ports[0].endpoints[0].mcp.oauth is not None
    assert (
        spec.ports[0].endpoints[0].mcp.oauth.authorization_endpoint
        == "https://auth.example.com/authorize"
    )
    assert (
        spec.ports[0].endpoints[0].mcp.oauth.token_endpoint
        == "https://auth.example.com/token"
    )
    assert (
        spec.metadata.annotations[services.ANNOTATION_SERVICE_ID]
        == "https://mcp.example.com/v1/mcp"
    )


@pytest.mark.asyncio
async def test_load_service_spec_mcp_requires_dynamic_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_discovery(*, server_url: str):
        del server_url
        return services._DiscoveredOAuthEndpoints(
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            registration_endpoint=None,
            no_pkce=False,
        )

    monkeypatch.setattr(services, "_discover_oauth_endpoints_for_mcp", _fake_discovery)

    with pytest.raises(typer.BadParameter, match="dynamic client registration"):
        await services._load_service_spec(
            file=None, url=None, mcp="https://mcp.example.com/v1/mcp"
        )


@pytest.mark.asyncio
async def test_load_service_spec_mcp_without_oauth_builds_public_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_discovery(*, server_url: str):
        del server_url
        return None

    monkeypatch.setattr(services, "_discover_oauth_endpoints_for_mcp", _fake_discovery)

    spec = await services._load_service_spec(
        file=None, url=None, mcp="https://mcp.example.com/v1/mcp"
    )

    assert spec.external is not None
    assert spec.external.url == "https://mcp.example.com"
    assert spec.ports is not None
    assert spec.ports[0].endpoints[0].mcp is not None
    assert spec.ports[0].endpoints[0].mcp.oauth is None
    assert (
        spec.metadata.annotations[services.ANNOTATION_SERVICE_ID]
        == "https://mcp.example.com/v1/mcp"
    )


@pytest.mark.asyncio
async def test_load_service_spec_mcp_deepwiki_without_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_discovery(*, server_url: str):
        assert server_url == "https://mcp.deepwiki.com/mcp"
        return None

    monkeypatch.setattr(services, "_discover_oauth_endpoints_for_mcp", _fake_discovery)

    spec = await services._load_service_spec(
        file=None, url=None, mcp="https://mcp.deepwiki.com/mcp"
    )

    assert spec.kind == "Service"
    assert spec.metadata.name == "deepwiki"
    assert spec.external is not None
    assert spec.external.url == "https://mcp.deepwiki.com"
    assert spec.ports is not None
    assert spec.ports[0].endpoints[0].path == "/mcp"
    assert spec.ports[0].endpoints[0].mcp is not None
    assert spec.ports[0].endpoints[0].mcp.oauth is None
    assert (
        spec.metadata.annotations[services.ANNOTATION_SERVICE_ID]
        == "https://mcp.deepwiki.com/mcp"
    )


@pytest.mark.asyncio
async def test_load_service_spec_mcp_builds_external_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_discovery(*, server_url: str):
        del server_url
        return services._DiscoveredOAuthEndpoints(
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            registration_endpoint="https://auth.example.com/register",
            no_pkce=False,
        )

    monkeypatch.setattr(services, "_discover_oauth_endpoints_for_mcp", _fake_discovery)

    spec = await services._load_service_spec(
        file=None, url=None, mcp="https://mcp.example.com/v1/mcp"
    )

    assert spec.external is not None
    assert spec.external.url == "https://mcp.example.com"
    assert spec.ports is not None
    assert spec.ports[0].endpoints[0].mcp is not None
    assert spec.ports[0].endpoints[0].mcp.oauth is not None
    assert spec.ports[0].endpoints[0].mcp.oauth.client_id is None
    assert spec.ports[0].endpoints[0].mcp.oauth.authorization_endpoint is None
    assert spec.ports[0].endpoints[0].mcp.oauth.token_endpoint is None
    assert spec.ports[0].endpoints[0].mcp.oauth.no_pkce is False
    assert (
        spec.metadata.annotations[services.ANNOTATION_SERVICE_ID]
        == "https://mcp.example.com/v1/mcp"
    )


@pytest.mark.asyncio
async def test_load_service_spec_mcp_notion_with_dynamic_registration_adds_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_discovery(*, server_url: str):
        assert server_url == "https://mcp.notion.com/mcp"
        return services._DiscoveredOAuthEndpoints(
            authorization_endpoint="https://api.notion.com/v1/oauth/authorize",
            token_endpoint="https://api.notion.com/v1/oauth/token",
            registration_endpoint="https://api.notion.com/v1/oauth/register",
            no_pkce=False,
        )

    monkeypatch.setattr(services, "_discover_oauth_endpoints_for_mcp", _fake_discovery)

    spec = await services._load_service_spec(
        file=None, url=None, mcp="https://mcp.notion.com/mcp"
    )

    assert spec.kind == "Service"
    assert spec.metadata.name == "notion"
    assert spec.external is not None
    assert spec.external.url == "https://mcp.notion.com"
    assert spec.ports is not None
    assert spec.ports[0].endpoints[0].path == "/mcp"
    assert spec.ports[0].endpoints[0].mcp is not None
    assert spec.ports[0].endpoints[0].mcp.oauth is not None
    assert spec.ports[0].endpoints[0].mcp.oauth.client_id is None
    assert spec.ports[0].endpoints[0].mcp.oauth.authorization_endpoint is None
    assert spec.ports[0].endpoints[0].mcp.oauth.token_endpoint is None
    assert spec.ports[0].endpoints[0].mcp.oauth.no_pkce is False
    assert (
        spec.metadata.annotations[services.ANNOTATION_SERVICE_ID]
        == "https://mcp.notion.com/mcp"
    )


@pytest.mark.asyncio
async def test_fetch_json_url_sets_user_agent_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = {}

    class _Response:
        status = 200
        charset = "utf-8"

        async def read(self) -> bytes:
            return json.dumps({"ok": True}).encode("utf-8")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _Session:
        def get(self, url: str, *, headers=None):
            seen["url"] = url
            seen["headers"] = headers
            return _Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    def _fake_new_client_session(*, timeout):
        seen["timeout"] = timeout
        return _Session()

    monkeypatch.setattr(services, "new_client_session", _fake_new_client_session)

    payload = await services._fetch_json_url(
        "https://example.com/.well-known/oauth-authorization-server"
    )

    assert payload == {"ok": True}
    assert seen["timeout"].total == 10
    assert seen["headers"].get("User-Agent") == "meshagent-cli/0.28"


def test_service_spec_to_template_spec_preserves_mcp_shape() -> None:
    service = services._build_external_mcp_service_spec(
        mcp_url="https://mcp.example.com/v1/mcp",
        oauth=services.OAuthClientConfig(no_pkce=False),
    )

    template = services._service_spec_to_template_spec(service)

    assert template.kind == "ServiceTemplate"
    assert template.external is not None
    assert template.external.url == "https://mcp.example.com"
    assert template.ports is not None
    assert template.ports[0].endpoints[0].mcp is not None
    assert template.ports[0].endpoints[0].mcp.oauth is not None
    assert template.ports[0].endpoints[0].mcp.oauth.no_pkce is False


def test_apply_service_id_annotation_on_service_spec() -> None:
    service = services._build_external_mcp_service_spec(
        mcp_url="https://mcp.example.com/v1/mcp",
        oauth=None,
    )

    updated = services._apply_service_id_annotation(
        model=service,
        service_id="meshagent.connector.mcp.example",
    )

    assert isinstance(updated, services.ServiceSpec)
    assert (
        updated.metadata.annotations[services.ANNOTATION_SERVICE_ID]
        == "meshagent.connector.mcp.example"
    )


def test_apply_service_id_annotation_on_template_spec() -> None:
    service = services._build_external_mcp_service_spec(
        mcp_url="https://mcp.example.com/v1/mcp",
        oauth=services.OAuthClientConfig(no_pkce=False),
    )
    template = services._service_spec_to_template_spec(service)

    updated = services._apply_service_id_annotation(
        model=template,
        service_id="meshagent.connector.mcp.example",
    )

    assert isinstance(updated, services.ServiceTemplateSpec)
    assert (
        updated.metadata.annotations[services.ANNOTATION_SERVICE_ID]
        == "meshagent.connector.mcp.example"
    )


@pytest.mark.asyncio
async def test_load_spec_output_mcp_template_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_load_service_spec(*, file, url, mcp):
        del file, url, mcp
        return services._build_external_mcp_service_spec(
            mcp_url="https://mcp.example.com/v1/mcp",
            oauth=services.OAuthClientConfig(no_pkce=False),
        )

    monkeypatch.setattr(services, "_load_service_spec", _fake_load_service_spec)

    model = await services._load_spec_output(
        file=None,
        url=None,
        mcp="https://mcp.example.com/v1/mcp",
        format="template",
    )

    assert isinstance(model, services.ServiceTemplateSpec)
    assert model.kind == "ServiceTemplate"


@pytest.mark.asyncio
async def test_service_create_template_rejects_service_spec_with_friendly_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "service.yaml", _SERVICE_YAML)
    printed = _capture_prints(monkeypatch)
    client = _patch_service_command_runtime(monkeypatch)

    with pytest.raises(typer.Exit) as exc_info:
        await services.service_create_template(
            project_id=None,
            file=str(spec_path),
            room=None,
            global_=True,
        )

    assert exc_info.value.exit_code == 1
    assert client.closed is False
    _assert_spec_input_error_output(
        printed,
        headline="Invalid service template.",
        description="The input is not in the correct service template format.",
        detail_fragment="kind",
        guidance_lines=(
            "This command must be passed a service template.",
            "It looks like you passed a service definition instead.",
            "Use `meshagent service create` instead.",
        ),
    )


@pytest.mark.asyncio
async def test_service_update_template_rejects_service_spec_with_friendly_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "service.yaml", _SERVICE_YAML)
    printed = _capture_prints(monkeypatch)
    client = _patch_service_command_runtime(monkeypatch)

    with pytest.raises(typer.Exit) as exc_info:
        await services.service_update_template(
            project_id=None,
            file=str(spec_path),
            room=None,
            global_=True,
        )

    assert exc_info.value.exit_code == 1
    assert client.closed is False
    _assert_spec_input_error_output(
        printed,
        headline="Invalid service template.",
        description="The input is not in the correct service template format.",
        detail_fragment="kind",
        guidance_lines=(
            "This command must be passed a service template.",
            "It looks like you passed a service definition instead.",
            "Use `meshagent service update` instead.",
        ),
    )


@pytest.mark.asyncio
async def test_service_create_rejects_service_template_with_friendly_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "template.yaml", _SERVICE_TEMPLATE_YAML)
    printed = _capture_prints(monkeypatch)
    client = _patch_service_command_runtime(monkeypatch)

    with pytest.raises(typer.Exit) as exc_info:
        await services.service_create(
            project_id=None,
            file=str(spec_path),
            room=None,
            global_=True,
        )

    assert exc_info.value.exit_code == 1
    assert client.closed is False
    _assert_spec_input_error_output(
        printed,
        headline="Invalid service definition.",
        description="The input is not in the correct service definition format.",
        detail_fragment="kind",
        guidance_lines=(
            "This command must be passed a service definition.",
            "It looks like you passed a service template instead.",
            "Use `meshagent service create-template` instead.",
        ),
    )


@pytest.mark.asyncio
async def test_service_update_rejects_service_template_with_friendly_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "template.yaml", _SERVICE_TEMPLATE_YAML)
    printed = _capture_prints(monkeypatch)
    client = _patch_service_command_runtime(monkeypatch)

    with pytest.raises(typer.Exit) as exc_info:
        await services.service_update(
            project_id=None,
            file=str(spec_path),
            room=None,
            global_=True,
        )

    assert exc_info.value.exit_code == 1
    assert client.closed is False
    _assert_spec_input_error_output(
        printed,
        headline="Invalid service definition.",
        description="The input is not in the correct service definition format.",
        detail_fragment="kind",
        guidance_lines=(
            "This command must be passed a service definition.",
            "It looks like you passed a service template instead.",
            "Use `meshagent service update-template` instead.",
        ),
    )


@pytest.mark.asyncio
async def test_service_create_prints_friendly_validation_error_for_invalid_spec(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "invalid-service.yaml", _INVALID_SERVICE_YAML)
    printed = _capture_prints(monkeypatch)
    client = _patch_service_command_runtime(monkeypatch)

    with pytest.raises(typer.Exit) as exc_info:
        await services.service_create(
            project_id=None,
            file=str(spec_path),
            room=None,
            global_=True,
        )

    assert exc_info.value.exit_code == 1
    assert client.closed is False
    _assert_spec_input_error_output(
        printed,
        headline="Invalid service definition.",
        description="The input is not in the correct service definition format.",
        detail_fragment="metadata",
    )


@pytest.mark.asyncio
async def test_service_create_template_prints_friendly_validation_error_for_invalid_template(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(
        tmp_path,
        "invalid-template.yaml",
        _INVALID_SERVICE_TEMPLATE_YAML,
    )
    printed = _capture_prints(monkeypatch)
    client = _patch_service_command_runtime(monkeypatch)

    with pytest.raises(typer.Exit) as exc_info:
        await services.service_create_template(
            project_id=None,
            file=str(spec_path),
            room=None,
            global_=True,
        )

    assert exc_info.value.exit_code == 1
    assert client.closed is True
    _assert_spec_input_error_output(
        printed,
        headline="Invalid service template.",
        description="The input is not in the correct service template format.",
        detail_fragment="metadata",
    )


@pytest.mark.asyncio
async def test_service_create_template_uses_render_template_for_jinja_template(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "assistant.yaml", _JINJA_SERVICE_TEMPLATE_YAML)
    printed = _capture_prints(monkeypatch)
    client = _TemplateCommandClient()
    _patch_service_command_runtime(monkeypatch, client=client)

    await services.service_create_template(
        project_id=None,
        file=str(spec_path),
        room=None,
        global_=True,
        value=None,
    )

    assert client.render_calls == [(_JINJA_SERVICE_TEMPLATE_YAML, {})]
    assert client.create_calls == [("project-1", _JINJA_SERVICE_TEMPLATE_YAML, {})]
    assert client.update_calls == []
    assert printed == ["[green]Created service:[/] service-1"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_service_validate_template_uses_render_template_for_jinja_template(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "assistant.yaml", _JINJA_SERVICE_TEMPLATE_YAML)
    printed = _capture_prints(monkeypatch)
    client = _TemplateCommandClient()
    _patch_service_command_runtime(monkeypatch, client=client)

    await services.service_validate_template(
        file=str(spec_path),
        value=["email=a@example.com"],
    )

    assert client.render_calls == [
        (_JINJA_SERVICE_TEMPLATE_YAML, {"email": "a@example.com"})
    ]
    assert printed == ["[green]Service template is valid:[/] assistant"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_service_render_template_prints_rendered_yaml(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "assistant.yaml", _JINJA_SERVICE_TEMPLATE_YAML)
    printed = _capture_prints(monkeypatch)
    client = _TemplateCommandClient()
    _patch_service_command_runtime(monkeypatch, client=client)

    await services.service_render_template(
        file=str(spec_path),
        value=["email=a@example.com"],
    )

    assert client.render_calls == [
        (_JINJA_SERVICE_TEMPLATE_YAML, {"email": "a@example.com"})
    ]
    assert len(printed) == 1
    assert "command:" in printed[0]
    assert "a@example.com" in printed[0]
    assert client.closed is True


@pytest.mark.asyncio
async def test_service_update_template_uses_render_template_for_jinja_template(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "assistant.yaml", _JINJA_SERVICE_TEMPLATE_YAML)
    printed = _capture_prints(monkeypatch)
    client = _TemplateCommandClient()
    _patch_service_command_runtime(monkeypatch, client=client)

    await services.service_update_template(
        project_id=None,
        id="service-1",
        file=str(spec_path),
        room=None,
        global_=True,
        value=["email=a@example.com"],
    )

    assert client.render_calls == [
        (_JINJA_SERVICE_TEMPLATE_YAML, {"email": "a@example.com"})
    ]
    assert client.create_calls == []
    assert client.update_calls == [
        (
            "project-1",
            "service-1",
            _JINJA_SERVICE_TEMPLATE_YAML,
            {"email": "a@example.com"},
        )
    ]
    assert printed == ["[green]Updated service:[/] service-1"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_service_create_template_room_prints_only_created_service_id(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "assistant.yaml", _JINJA_SERVICE_TEMPLATE_YAML)
    printed = _capture_prints(monkeypatch)
    client = _TemplateCommandClient()
    _patch_service_command_runtime(monkeypatch, client=client)

    await services.service_create_template(
        project_id=None,
        file=str(spec_path),
        room="jesse2",
        value=None,
    )

    assert client.render_calls == [(_JINJA_SERVICE_TEMPLATE_YAML, {})]
    assert client.create_calls == []
    assert client.create_room_calls == [
        ("project-1", "jesse2", _JINJA_SERVICE_TEMPLATE_YAML, {})
    ]
    assert printed == ["[green]Created service:[/] room-service-1"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_service_update_template_room_prints_only_updated_service_id(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "assistant.yaml", _JINJA_SERVICE_TEMPLATE_YAML)
    printed = _capture_prints(monkeypatch)
    client = _TemplateCommandClient()
    _patch_service_command_runtime(monkeypatch, client=client)

    await services.service_update_template(
        project_id=None,
        id="room-service-1",
        file=str(spec_path),
        room="jesse2",
        value=["email=a@example.com"],
    )

    assert client.render_calls == [
        (_JINJA_SERVICE_TEMPLATE_YAML, {"email": "a@example.com"})
    ]
    assert client.update_calls == []
    assert client.update_room_calls == [
        (
            "project-1",
            "jesse2",
            "room-service-1",
            _JINJA_SERVICE_TEMPLATE_YAML,
            {"email": "a@example.com"},
        )
    ]
    assert printed == ["[green]Updated service:[/] room-service-1"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_service_create_prints_only_created_service_id(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "service.yaml", _SERVICE_YAML)
    printed = _capture_prints(monkeypatch)
    client = _ServiceCommandClient()
    _patch_service_command_runtime(monkeypatch, client=client)

    await services.service_create(
        project_id=None,
        file=str(spec_path),
        room=None,
        global_=True,
    )

    assert len(client.create_calls) == 1
    assert client.create_calls[0][0] == "project-1"
    assert printed == ["[green]Created service:[/] service-1"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_service_update_with_create_prints_only_created_service_id(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "service.yaml", _SERVICE_YAML)
    printed = _capture_prints(monkeypatch)
    client = _ServiceCommandClient()
    _patch_service_command_runtime(monkeypatch, client=client)

    await services.service_update(
        project_id=None,
        file=str(spec_path),
        room=None,
        global_=True,
        create=True,
    )

    assert client.list_calls == ["project-1"]
    assert len(client.create_calls) == 1
    assert printed == ["[green]Updated service:[/] service-1"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_service_create_requires_explicit_scope(
    tmp_path: pathlib.Path,
) -> None:
    spec_path = _write_yaml(tmp_path, "service.yaml", _SERVICE_YAML)

    with pytest.raises(
        typer.BadParameter,
        match="Pass --room to install in a room or --global to install globally.",
    ):
        await services.service_create(
            project_id=None,
            file=str(spec_path),
        )


@pytest.mark.asyncio
async def test_service_create_rejects_existing_service_id_in_global_scope(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "service.yaml", _SERVICE_WITH_ID_YAML)
    printed = _capture_prints(monkeypatch)
    client = _ServiceCommandClient()
    client.services = [
        _service_record(
            id="existing-1",
            name="existing",
            service_annotation_id="demo.service",
        )
    ]
    _patch_service_command_runtime(monkeypatch, client=client)

    with pytest.raises(typer.Exit) as exc_info:
        await services.service_create(
            project_id=None,
            file=str(spec_path),
            global_=True,
        )

    assert exc_info.value.exit_code == 1
    assert client.list_calls == ["project-1"]
    assert client.create_calls == []
    assert client.update_calls == []
    assert printed == [
        '[red]service already exists with the service id: "demo.service" use --force to ignore or --replace to replace it[/red]'
    ]
    assert client.closed is True


@pytest.mark.asyncio
async def test_service_create_force_ignores_existing_service_id_in_global_scope(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "service.yaml", _SERVICE_WITH_ID_YAML)
    printed = _capture_prints(monkeypatch)
    client = _ServiceCommandClient()
    client.services = [
        _service_record(
            id="existing-1",
            name="existing",
            service_annotation_id="demo.service",
        )
    ]
    _patch_service_command_runtime(monkeypatch, client=client)

    await services.service_create(
        project_id=None,
        file=str(spec_path),
        global_=True,
        force=True,
    )

    assert len(client.create_calls) == 1
    assert client.update_calls == []
    assert printed == ["[green]Created service:[/] service-1"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_service_create_replace_updates_existing_service_id_in_global_scope(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "service.yaml", _SERVICE_WITH_ID_YAML)
    printed = _capture_prints(monkeypatch)
    client = _ServiceCommandClient()
    client.services = [
        _service_record(
            id="existing-1",
            name="existing",
            service_annotation_id="demo.service",
        )
    ]
    _patch_service_command_runtime(monkeypatch, client=client)

    await services.service_create(
        project_id=None,
        file=str(spec_path),
        global_=True,
        replace=True,
    )

    assert client.create_calls == []
    assert len(client.update_calls) == 1
    assert client.update_calls[0][1] == "existing-1"
    assert printed == ["[green]Updated service:[/] existing-1"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_service_update_rejects_existing_service_id_when_target_differs(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(tmp_path, "service.yaml", _SERVICE_WITH_ID_YAML)
    printed = _capture_prints(monkeypatch)
    client = _ServiceCommandClient()
    client.services = [
        _service_record(
            id="existing-1",
            name="existing",
            service_annotation_id="demo.service",
        )
    ]
    _patch_service_command_runtime(monkeypatch, client=client)

    with pytest.raises(typer.Exit) as exc_info:
        await services.service_update(
            project_id=None,
            id="other-1",
            file=str(spec_path),
            global_=True,
        )

    assert exc_info.value.exit_code == 1
    assert client.update_calls == []
    assert printed == [
        '[red]service already exists with the service id: "demo.service" use --force to ignore or --replace to replace it[/red]'
    ]
    assert client.closed is True


@pytest.mark.asyncio
async def test_service_update_template_replace_updates_existing_service_id_in_room(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(
        tmp_path,
        "template.yaml",
        _SERVICE_TEMPLATE_WITH_ID_YAML,
    )
    printed = _capture_prints(monkeypatch)
    client = _TemplateCommandClient()
    client.room_services = [
        _service_record(
            id="room-existing-1",
            name="existing",
            service_annotation_id="demo.service",
        )
    ]
    _patch_service_command_runtime(monkeypatch, client=client)

    await services.service_update_template(
        project_id=None,
        file=str(spec_path),
        room="jesse2",
        replace=True,
    )

    assert client.create_room_calls == []
    assert len(client.update_room_calls) == 1
    assert client.update_room_calls[0][2] == "room-existing-1"
    assert printed == ["[green]Updated service:[/] room-existing-1"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_service_create_template_rejects_existing_service_id_in_room(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = _write_yaml(
        tmp_path,
        "template.yaml",
        _SERVICE_TEMPLATE_WITH_ID_YAML,
    )
    printed = _capture_prints(monkeypatch)
    client = _TemplateCommandClient()
    client.room_services = [
        _service_record(
            id="room-existing-1",
            name="existing",
            service_annotation_id="demo.service",
        )
    ]
    _patch_service_command_runtime(monkeypatch, client=client)

    with pytest.raises(typer.Exit) as exc_info:
        await services.service_create_template(
            project_id=None,
            file=str(spec_path),
            room="jesse2",
        )

    assert exc_info.value.exit_code == 1
    assert client.create_room_calls == []
    assert client.update_room_calls == []
    assert printed == [
        '[red]service already exists with the service id: "demo.service" use --force to ignore or --replace to replace it[/red]'
    ]
    assert client.closed is True
