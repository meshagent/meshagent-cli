import json

import pytest
from click.testing import CliRunner

from meshagent.api.client import ProjectRoomGrant, Room
from meshagent.api.participant_token import ApiScope
from meshagent.cli import async_typer, cli, rooms


class _FakeRoomsClient:
    def __init__(
        self,
        *,
        rooms_result: list[Room],
        room_grants_result: list[ProjectRoomGrant] | None = None,
    ) -> None:
        self.rooms_result = rooms_result
        self.room_grants_result = room_grants_result
        if self.room_grants_result is None:
            self.room_grants_result = [
                ProjectRoomGrant(
                    room=room,
                    user_id="user-1",
                    permissions=ApiScope(admin={}),
                )
                for room in rooms_result
            ]
        self.closed = False
        self.list_rooms_calls: list[dict[str, object]] = []
        self.list_room_grants_by_user_calls: list[dict[str, object]] = []
        self.create_room_calls: list[dict[str, object]] = []

    async def list_rooms(
        self,
        *,
        project_id: str,
        limit: int,
        offset: int,
        order_by: str,
        filter: str | None = None,
    ) -> list[Room]:
        self.list_rooms_calls.append(
            {
                "project_id": project_id,
                "limit": limit,
                "offset": offset,
                "order_by": order_by,
                "filter": filter,
            }
        )
        return self.rooms_result

    async def list_room_grants_by_user(
        self,
        *,
        project_id: str,
        user_id: str,
        limit: int,
        offset: int,
        order_by: str,
        filter: str | None = None,
    ) -> list[ProjectRoomGrant]:
        self.list_room_grants_by_user_calls.append(
            {
                "project_id": project_id,
                "user_id": user_id,
                "limit": limit,
                "offset": offset,
                "order_by": order_by,
                "filter": filter,
            }
        )
        return self.room_grants_result

    async def create_room(
        self,
        *,
        project_id: str,
        name: str,
        if_not_exists: bool = False,
        metadata: dict[str, object] | None = None,
        annotations: dict[str, str] | None = None,
        permissions: dict[str, ApiScope] | None = None,
    ) -> Room:
        self.create_room_calls.append(
            {
                "project_id": project_id,
                "name": name,
                "if_not_exists": if_not_exists,
                "metadata": metadata,
                "annotations": annotations,
                "permissions": permissions,
            }
        )
        return Room(
            id="room-created",
            name=name,
            metadata=metadata or {},
            annotations=annotations or {},
        )

    async def close(self) -> None:
        self.closed = True


def _sample_room() -> Room:
    return Room(
        id="room-1",
        name="demo",
        metadata={"team": "core"},
        annotations={"meshagent.storage.class": "ephemeral"},
    )


def _patch_room_list_command(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: _FakeRoomsClient,
) -> None:
    async def fake_get_client() -> _FakeRoomsClient:
        return client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(rooms, "get_client", fake_get_client)
    monkeypatch.setattr(rooms, "resolve_project_id", fake_resolve_project_id)


def test_rooms_create_help_does_not_mention_deploy_prerequisite() -> None:
    result = CliRunner().invoke(
        async_typer.get_command(cli.app),
        ["rooms", "create", "--help"],
    )

    assert result.exit_code == 0
    assert "Create a room in the project." in result.output
    assert "Use this before meshagent deploy --room" not in result.output
    assert "meshagent deploy PATH --room" not in result.output


def test_rooms_create_help_uses_positional_name_and_no_owner_flag() -> None:
    result = CliRunner().invoke(
        async_typer.get_command(cli.app),
        ["rooms", "create", "--help"],
    )

    assert result.exit_code == 0
    normalized = " ".join(result.output.split())
    assert "NAME" in normalized
    assert "--no-owner" in result.output
    assert "--name" not in result.output


@pytest.mark.asyncio
async def test_room_create_adds_active_user_as_owner_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRoomsClient(rooms_result=[])
    _patch_room_list_command(monkeypatch, client=client)
    monkeypatch.setattr(rooms, "get_active_user_id", lambda: "user-active")
    monkeypatch.setattr(rooms, "print", lambda *args, **kwargs: None)

    await rooms.room_create_command(
        "demo",
        project_id="project-1",
        if_not_exists=True,
        metadata='{"team":"core"}',
        annotations='{"meshagent.storage.class":"ephemeral"}',
    )

    assert len(client.create_room_calls) == 1
    call = client.create_room_calls[0]
    assert call["project_id"] == "resolved-project"
    assert call["name"] == "demo"
    assert call["if_not_exists"] is True
    assert call["metadata"] == {"team": "core"}
    assert call["annotations"] == {"meshagent.storage.class": "ephemeral"}
    assert call["permissions"] == {"user-active": ApiScope.full()}
    assert client.closed is True


@pytest.mark.asyncio
async def test_room_create_no_owner_skips_owner_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRoomsClient(rooms_result=[])
    _patch_room_list_command(monkeypatch, client=client)
    monkeypatch.setattr(rooms, "get_active_user_id", lambda: "user-active")
    monkeypatch.setattr(rooms, "print", lambda *args, **kwargs: None)

    await rooms.room_create_command(
        "demo",
        project_id="project-1",
        owner=False,
    )

    assert client.create_room_calls[0]["permissions"] is None


def test_route_create_help_mentions_short_domains_and_service_id_annotation() -> None:
    result = CliRunner().invoke(
        async_typer.get_command(cli.app),
        ["route", "create", "--help"],
    )

    assert result.exit_code == 0
    normalized_output = " ".join(result.output.split())
    assert "Use a short, DNS-safe domain name" in normalized_output
    assert "long room-name-derived" in normalized_output
    assert "domains may be rejected" in normalized_output
    assert "meshagent.service.id" in result.output


@pytest.mark.asyncio
async def test_room_list_defaults_to_table_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRoomsClient(rooms_result=[_sample_room()])
    printed: list[tuple[list[dict[str, object]], tuple[str, ...]]] = []
    _patch_room_list_command(monkeypatch, client=client)

    def fake_print_json_table(
        records: list[dict[str, object]],
        *cols: str,
    ) -> None:
        printed.append((records, cols))

    monkeypatch.setattr(rooms, "print_json_table", fake_print_json_table)
    monkeypatch.setattr(rooms, "print", lambda *args, **kwargs: None)

    await rooms.room_list_command(project_id="project-1")

    assert client.list_rooms_calls == []
    assert client.list_room_grants_by_user_calls == [
        {
            "project_id": "resolved-project",
            "user_id": "me",
            "limit": 100,
            "offset": 0,
            "order_by": "room_name",
            "filter": None,
        }
    ]
    assert printed == [
        (
            [
                {
                    "id": "room-1",
                    "name": "demo",
                    "metadata": {"team": "core"},
                    "annotations": {"meshagent.storage.class": "ephemeral"},
                }
            ],
            ("id", "name"),
        )
    ]
    assert client.closed is True


@pytest.mark.asyncio
async def test_room_list_passes_count_and_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRoomsClient(rooms_result=[])
    _patch_room_list_command(monkeypatch, client=client)
    monkeypatch.setattr(rooms, "print", lambda *args, **kwargs: None)

    await rooms.room_list_command(project_id="project-1", count=25, filter="demo")

    assert client.list_rooms_calls == []
    assert client.list_room_grants_by_user_calls == [
        {
            "project_id": "resolved-project",
            "user_id": "me",
            "limit": 25,
            "offset": 0,
            "order_by": "room_name",
            "filter": "demo",
        }
    ]


@pytest.mark.asyncio
async def test_room_list_json_output_skips_table_printer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRoomsClient(rooms_result=[_sample_room()])
    printed: list[str] = []
    _patch_room_list_command(monkeypatch, client=client)

    monkeypatch.setattr(
        rooms,
        "print_json_table",
        lambda records, *cols: (_ for _ in ()).throw(
            AssertionError("table printer should not be used for json output")
        ),
    )
    monkeypatch.setattr(rooms, "print", lambda value: printed.append(value))

    await rooms.room_list_command(project_id="project-1", o="json")

    assert client.closed is True
    assert len(printed) == 1
    assert json.loads(printed[0]) == [
        {
            "id": "room-1",
            "name": "demo",
            "metadata": {"team": "core"},
            "annotations": {"meshagent.storage.class": "ephemeral"},
        }
    ]


@pytest.mark.asyncio
async def test_room_list_all_uses_project_room_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRoomsClient(rooms_result=[_sample_room()])
    _patch_room_list_command(monkeypatch, client=client)

    monkeypatch.setattr(rooms, "print_json_table", lambda records, *cols: None)
    monkeypatch.setattr(rooms, "print", lambda *args, **kwargs: None)

    await rooms.room_list_command(project_id="project-1", show_all=True)

    assert client.list_room_grants_by_user_calls == []
    assert client.list_rooms_calls == [
        {
            "project_id": "resolved-project",
            "limit": 100,
            "offset": 0,
            "order_by": "room_name",
            "filter": None,
        }
    ]


@pytest.mark.asyncio
async def test_room_list_table_output_handles_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRoomsClient(rooms_result=[])
    printed: list[str] = []
    _patch_room_list_command(monkeypatch, client=client)

    monkeypatch.setattr(
        rooms,
        "print_json_table",
        lambda records, *cols: (_ for _ in ()).throw(
            AssertionError("table printer should not be used for empty results")
        ),
    )
    monkeypatch.setattr(rooms, "print", lambda value: printed.append(value))

    await rooms.room_list_command(project_id="project-1")

    assert printed == ["No rooms found."]
    assert client.closed is True
