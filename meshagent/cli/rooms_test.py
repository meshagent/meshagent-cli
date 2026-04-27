import json

import pytest
from click.testing import CliRunner

from meshagent.api.client import Room
from meshagent.cli import async_typer, cli, rooms


class _FakeRoomsClient:
    def __init__(self, *, rooms_result: list[Room]) -> None:
        self.rooms_result = rooms_result
        self.closed = False
        self.list_rooms_calls: list[dict[str, object]] = []

    async def list_rooms(
        self,
        *,
        project_id: str,
        limit: int,
        offset: int,
        order_by: str,
    ) -> list[Room]:
        self.list_rooms_calls.append(
            {
                "project_id": project_id,
                "limit": limit,
                "offset": offset,
                "order_by": order_by,
            }
        )
        return self.rooms_result

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


def test_route_create_help_mentions_short_domains_and_service_id_annotation() -> None:
    result = CliRunner().invoke(
        async_typer.get_command(cli.app),
        ["route", "create", "--help"],
    )

    assert result.exit_code == 0
    assert "Use a short, DNS-safe domain name" in result.output
    assert "room-name-derived domains may be rejected" in result.output
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

    assert client.list_rooms_calls == [
        {
            "project_id": "resolved-project",
            "limit": 50,
            "offset": 0,
            "order_by": "room_name",
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
