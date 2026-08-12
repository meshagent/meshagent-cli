from pathlib import Path

import pytest
from typer.testing import CliRunner

from meshagent.api.client import Route, RoutesPage
from meshagent.api.specs.service import RouteSpec
from meshagent.cli import routes


class _FakeRoutesClient:
    def __init__(self) -> None:
        self.closed = False
        self.list_routes_page_calls: list[dict[str, object]] = []
        self.list_routes_calls: list[dict[str, object]] = []
        self.pages = [
            RoutesPage(
                routes=[
                    Route.model_validate(
                        {
                            "domain": "a.meshagent.app",
                            "room_name": "room-a",
                            "port": "8080",
                        }
                    ),
                    Route.model_validate(
                        {
                            "domain": "b.meshagent.app",
                            "room_name": "room-b",
                            "port": "8081",
                        }
                    ),
                ],
                continuation_token="next",
            ),
            RoutesPage(
                routes=[
                    Route.model_validate(
                        {
                            "domain": "c.meshagent.app",
                            "room_name": "room-c",
                            "port": "8082",
                        }
                    )
                ],
                continuation_token=None,
            ),
        ]

    async def list_routes_page(
        self,
        *,
        project_id: str,
        page_size: int,
        continuation_token: str | None = None,
        filter: str | None = None,
    ) -> RoutesPage:
        self.list_routes_page_calls.append(
            {
                "project_id": project_id,
                "page_size": page_size,
                "continuation_token": continuation_token,
                "filter": filter,
            }
        )
        return self.pages.pop(0)

    async def list_routes(self, **kwargs):
        self.list_routes_calls.append(kwargs)
        raise AssertionError("route_list should use list_routes_page for pagination")

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_route_list_without_room_uses_paged_routes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRoutesClient()
    printed: list[object] = []

    async def fake_get_client() -> _FakeRoutesClient:
        return client

    async def fake_resolve_project_id(project_id: str | None = None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(routes, "get_client", fake_get_client)
    monkeypatch.setattr(routes, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(routes, "resolve_room", lambda room=None: room)
    monkeypatch.setattr(routes, "print", printed.append)

    await routes.route_list(
        project_id="project-1",
        count=2,
        offset=1,
        filter="mesh",
        o="json",
    )

    assert client.list_routes_calls == []
    assert client.list_routes_page_calls == [
        {
            "project_id": "resolved-project",
            "page_size": 3,
            "continuation_token": None,
            "filter": "mesh",
        },
        {
            "project_id": "resolved-project",
            "page_size": 1,
            "continuation_token": "next",
            "filter": "mesh",
        },
    ]
    assert client.closed
    assert len(printed) == 1
    assert [route["domain"] for route in printed[0]["routes"]] == [
        "b.meshagent.app",
        "c.meshagent.app",
    ]


class _FakeRouteMutationClient:
    def __init__(self, route: Route | None = None) -> None:
        self.route = route
        self.created_spec: RouteSpec | None = None
        self.updated_spec: RouteSpec | None = None
        self.closed = False

    async def create_route(self, *, project_id: str, spec: RouteSpec) -> None:
        assert project_id == "resolved-project"
        self.created_spec = spec

    async def get_route(self, *, project_id: str, domain: str) -> Route:
        assert project_id == "resolved-project"
        assert domain == "docs.meshagent.app"
        assert self.route is not None
        return self.route

    async def update_route(
        self, *, project_id: str, domain: str, spec: RouteSpec
    ) -> None:
        assert project_id == "resolved-project"
        assert domain == "docs.meshagent.app"
        self.updated_spec = spec

    async def close(self) -> None:
        self.closed = True


async def _resolved_project_id(project_id: str | None = None) -> str:
    assert project_id == "project-1"
    return "resolved-project"


@pytest.mark.asyncio
async def test_route_create_supports_room_content_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRouteMutationClient()

    async def fake_get_client() -> _FakeRouteMutationClient:
        return client

    monkeypatch.setattr(routes, "get_client", fake_get_client)
    monkeypatch.setattr(routes, "resolve_project_id", _resolved_project_id)
    monkeypatch.setattr(routes, "resolve_room", lambda room=None: room)
    monkeypatch.setattr(routes, "print", lambda *_args, **_kwargs: None)

    await routes.route_create(
        project_id="project-1",
        domain="docs.meshagent.app",
        room="docs-room",
        path="/docs",
        content_path="web/docs",
        cors='[{"allowedOrigins":["https://app.example.com"]}]',
        index=True,
        iap=True,
        compression="gzip",
    )

    assert client.closed
    assert client.created_spec is not None
    route_path = client.created_spec.paths[0]
    assert route_path.path == "/docs"
    assert route_path.targetPort is None
    assert route_path.targetContent is not None
    assert route_path.targetContent.subpath == "web/docs"
    assert route_path.targetContent.index is True
    assert route_path.targetContent.iap is True
    assert route_path.targetContent.compression == "gzip"
    assert route_path.targetContent.cors[0].allowedOrigins == [
        "https://app.example.com"
    ]


@pytest.mark.asyncio
async def test_route_create_loads_routespec_yaml_from_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    route_file = tmp_path / "route.yaml"
    route_file.write_text(
        """\
kind: Route
version: v1
metadata:
  name: docs.meshagent.app
domain: docs.meshagent.app
backend:
  room:
    name: docs-room
paths:
  - path: /
    pathType: prefix
    targetContent:
      subpath: web/docs
      index: true
      compression: brotli
""",
        encoding="utf-8",
    )
    client = _FakeRouteMutationClient()

    async def fake_get_client() -> _FakeRouteMutationClient:
        return client

    monkeypatch.setattr(routes, "get_client", fake_get_client)
    monkeypatch.setattr(routes, "resolve_project_id", _resolved_project_id)
    monkeypatch.setattr(routes, "print", lambda *_args, **_kwargs: None)

    await routes.route_create(project_id="project-1", file=str(route_file))

    assert client.created_spec is not None
    assert client.created_spec.domain == "docs.meshagent.app"
    content = client.created_spec.paths[0].targetContent
    assert content is not None
    assert content.subpath == "web/docs"
    assert content.index is True
    assert content.compression == "brotli"


def test_route_create_help_exposes_short_file_option() -> None:
    result = CliRunner().invoke(routes.app, ["create", "--help"])

    assert result.exit_code == 0
    assert "--file" in result.stdout
    assert "-f" in result.stdout


@pytest.mark.asyncio
async def test_route_update_preserves_unspecified_content_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = Route.model_validate(
        {
            "domain": "docs.meshagent.app",
            "spec": {
                "metadata": {"name": "docs.meshagent.app", "annotations": {}},
                "domain": "docs.meshagent.app",
                "backend": {"room": {"name": "docs-room"}},
                "paths": [
                    {
                        "path": "/",
                        "pathType": "prefix",
                        "targetContent": {
                            "subpath": "web/docs",
                            "index": True,
                            "iap": False,
                            "compression": "brotli",
                            "cors": [{"allowedOrigins": ["*"]}],
                        },
                    }
                ],
            },
        }
    )
    client = _FakeRouteMutationClient(current)

    async def fake_get_client() -> _FakeRouteMutationClient:
        return client

    monkeypatch.setattr(routes, "get_client", fake_get_client)
    monkeypatch.setattr(routes, "resolve_project_id", _resolved_project_id)
    monkeypatch.setattr(routes, "resolve_room", lambda room=None: room)
    monkeypatch.setattr(routes, "print", lambda *_args, **_kwargs: None)

    await routes.route_update(
        project_id="project-1",
        domain="docs.meshagent.app",
        iap=True,
    )

    assert client.updated_spec is not None
    content = client.updated_spec.paths[0].targetContent
    assert content is not None
    assert content.subpath == "web/docs"
    assert content.index is True
    assert content.iap is True
    assert content.compression == "brotli"
    assert content.cors[0].allowedOrigins == ["*"]


def test_route_table_row_exposes_content_route_details() -> None:
    route = Route.model_validate(
        {
            "domain": "docs.meshagent.app",
            "spec": {
                "metadata": {"name": "docs.meshagent.app"},
                "domain": "docs.meshagent.app",
                "backend": {"room": {"name": "docs-room"}},
                "paths": [
                    {
                        "path": "/docs",
                        "targetContent": {
                            "subpath": "web/docs",
                            "index": True,
                            "iap": True,
                            "compression": "brotli",
                            "cors": [
                                {
                                    "allowedOrigins": [
                                        "https://app.example.com",
                                        "https://admin.example.com",
                                    ]
                                }
                            ],
                        },
                    }
                ],
            },
        }
    )

    assert routes._route_table_row(route) == {
        "domain": "docs.meshagent.app",
        "backend": "docs-room",
        "path": "/docs",
        "port": "",
        "content_path": "web/docs",
        "index": "true",
        "iap": "true",
        "compression": "brotli",
        "cors": (
            '[{"allowedOrigins":["https://app.example.com",'
            '"https://admin.example.com"],"allowedMethods":["GET","HEAD"],'
            '"allowedHeaders":[],"exposeHeaders":[],"maxAgeSeconds":3600,'
            '"allowCredentials":false}]'
        ),
    }
