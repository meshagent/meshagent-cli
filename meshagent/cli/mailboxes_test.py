import pytest

from meshagent.api.client import Mailbox, MailboxesPage
from meshagent.cli import async_typer, mailboxes
from meshagent.cli.testing import CliRunner


def _mailbox(index: int) -> Mailbox:
    return Mailbox(
        address=f"mailbox-{index}@example.com",
        room="inbox",
        queue="messages",
        public=False,
        annotations={},
    )


class _FakeClient:
    def __init__(self, *, project_pages: list[MailboxesPage] | None = None) -> None:
        self.project_pages = list(project_pages or [])
        self.list_mailboxes_page_calls: list[dict[str, object]] = []
        self.list_room_mailboxes_calls: list[dict[str, object]] = []
        self.room_mailboxes: list[Mailbox] = []
        self.closed = False

    async def list_mailboxes_page(self, **kwargs) -> MailboxesPage:
        self.list_mailboxes_page_calls.append(kwargs)
        if not self.project_pages:
            raise AssertionError("No project mailbox page configured")
        return self.project_pages.pop(0)

    async def list_room_mailboxes(self, **kwargs) -> list[Mailbox]:
        self.list_room_mailboxes_calls.append(kwargs)
        return self.room_mailboxes

    async def close(self) -> None:
        self.closed = True


def _patch_mailbox_list(
    monkeypatch: pytest.MonkeyPatch, *, client: _FakeClient
) -> None:
    async def fake_get_client() -> _FakeClient:
        return client

    async def fake_resolve_project_id(project_id=None) -> str:
        return project_id or "project-1"

    monkeypatch.setattr(mailboxes, "get_client", fake_get_client)
    monkeypatch.setattr(mailboxes, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(mailboxes, "resolve_room", lambda room: room)


@pytest.mark.parametrize(
    ("args", "option"),
    [
        (["--count", "0"], "--count"),
        (["--offset", "-1"], "--offset"),
    ],
)
def test_mailbox_list_rejects_invalid_pagination_values(
    args: list[str], option: str
) -> None:
    result = CliRunner().invoke(
        async_typer.get_command(mailboxes.app),
        ["list", "--project-id", "project-1", *args],
    )

    assert result.exit_code == 2
    assert option in result.output


@pytest.mark.asyncio
async def test_mailbox_list_project_pages_count_and_applies_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        project_pages=[
            MailboxesPage(
                mailboxes=[_mailbox(index) for index in range(100)],
                continuation_token="cursor-1",
            ),
            MailboxesPage(
                mailboxes=[_mailbox(index) for index in range(100, 160)],
                continuation_token=None,
            ),
        ]
    )
    printed: list[dict[str, object]] = []
    _patch_mailbox_list(monkeypatch, client=client)
    monkeypatch.setattr(mailboxes, "print", lambda value: printed.append(value))

    await mailboxes.mailbox_list(
        project_id="project-1",
        room=None,
        filter="support",
        count=150,
        offset=10,
        o="json",
    )

    assert client.list_mailboxes_page_calls == [
        {
            "project_id": "project-1",
            "page_size": 100,
            "continuation_token": None,
            "filter": "support",
        },
        {
            "project_id": "project-1",
            "page_size": 60,
            "continuation_token": "cursor-1",
            "filter": "support",
        },
    ]
    records = printed[0]["mailboxes"]
    assert isinstance(records, list)
    assert len(records) == 150
    assert records[0]["address"] == "mailbox-10@example.com"
    assert records[-1]["address"] == "mailbox-159@example.com"
    assert client.closed is True


@pytest.mark.asyncio
async def test_mailbox_list_room_preserves_offset_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient()
    client.room_mailboxes = [_mailbox(12)]
    _patch_mailbox_list(monkeypatch, client=client)
    monkeypatch.setattr(mailboxes, "print", lambda value: None)

    await mailboxes.mailbox_list(
        project_id="project-1",
        room="inbox",
        filter="support",
        count=25,
        offset=9,
        o="json",
    )

    assert client.list_room_mailboxes_calls == [
        {
            "project_id": "project-1",
            "room_name": "inbox",
            "count": 25,
            "offset": 9,
            "filter": "support",
        }
    ]
    assert client.list_mailboxes_page_calls == []
    assert client.closed is True
