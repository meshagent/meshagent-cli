import pytest
import typer
import json

from meshagent.cli import services


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


def test_fetch_json_url_sets_user_agent_header(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    class _Headers:
        @staticmethod
        def get_content_charset():
            return "utf-8"

    class _Response:
        headers = _Headers()

        def read(self) -> bytes:
            return json.dumps({"ok": True}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    def _fake_urlopen(request, timeout):
        seen["headers"] = dict(request.header_items())
        seen["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(services, "urlopen", _fake_urlopen)

    payload = services._fetch_json_url(
        "https://example.com/.well-known/oauth-authorization-server"
    )

    assert payload == {"ok": True}
    assert seen["timeout"] == 10
    assert seen["headers"].get("User-agent") == "meshagent-cli/0.28"


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
