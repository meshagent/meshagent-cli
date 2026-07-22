from datetime import datetime, timezone

import pytest

from meshagent.api.client import Feed, FeedsPage
from meshagent.cli import feeds


def _feed(feed_id: str) -> Feed:
    return Feed(
        id=feed_id,
        project_id="project-1",
        created_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        name=feed_id,
    )


@pytest.mark.asyncio
async def test_feed_list_project_uses_continuation_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed_1 = _feed("feed-1")
    feed_2 = _feed("feed-2")
    feed_3 = _feed("feed-3")
    pages = [
        FeedsPage(feeds=[feed_1, feed_2], continuation_token="next-page"),
        FeedsPage(feeds=[feed_3], continuation_token=None),
    ]
    calls: list[dict[str, object]] = []
    printed: list[list[dict[str, object]]] = []

    class FakeClient:
        async def list_feeds_page(
            self,
            *,
            project_id: str,
            page_size: int,
            continuation_token: str | None,
            filter: str | None,
        ) -> FeedsPage:
            calls.append(
                {
                    "project_id": project_id,
                    "page_size": page_size,
                    "continuation_token": continuation_token,
                    "filter": filter,
                }
            )
            return pages.pop(0)

        async def close(self) -> None:
            calls.append({"closed": True})

    async def fake_get_client() -> FakeClient:
        return FakeClient()

    async def fake_resolve_project_id(project_id=None):
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.delenv("MESHAGENT_ROOM", raising=False)
    monkeypatch.setattr(feeds, "get_client", fake_get_client)
    monkeypatch.setattr(feeds, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(
        feeds,
        "print_json_table",
        lambda records, *columns: printed.append(records),
    )

    await feeds.feed_list(
        project_id="project-1",
        room=None,
        filter="daily",
        count=2,
        offset=1,
        o="table",
    )

    assert calls == [
        {
            "project_id": "resolved-project",
            "page_size": 3,
            "continuation_token": None,
            "filter": "daily",
        },
        {
            "project_id": "resolved-project",
            "page_size": 1,
            "continuation_token": "next-page",
            "filter": "daily",
        },
        {"closed": True},
    ]
    assert [[record["id"] for record in records] for records in printed] == [
        ["feed-2", "feed-3"]
    ]


@pytest.mark.asyncio
async def test_feed_list_room_keeps_offset_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeClient:
        async def list_room_feeds(
            self,
            *,
            project_id: str,
            room_name: str,
            count: int,
            offset: int,
            filter: str | None,
        ) -> list[Feed]:
            calls.append(
                {
                    "project_id": project_id,
                    "room_name": room_name,
                    "count": count,
                    "offset": offset,
                    "filter": filter,
                }
            )
            return [_feed("feed-1")]

        async def close(self) -> None:
            calls.append({"closed": True})

    async def fake_get_client() -> FakeClient:
        return FakeClient()

    async def fake_resolve_project_id(project_id=None):
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(feeds, "get_client", fake_get_client)
    monkeypatch.setattr(feeds, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(feeds, "print_json_table", lambda records, *columns: None)

    await feeds.feed_list(
        project_id="project-1",
        room="room-1",
        filter="daily",
        count=25,
        offset=10,
        o="table",
    )

    assert calls == [
        {
            "project_id": "resolved-project",
            "room_name": "room-1",
            "count": 25,
            "offset": 10,
            "filter": "daily",
        },
        {"closed": True},
    ]
