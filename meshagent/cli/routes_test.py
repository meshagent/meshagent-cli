import pytest

from meshagent.api.client import Route, RoutesPage
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
