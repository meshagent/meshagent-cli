import pytest

from meshagent.cli import mailboxes


@pytest.mark.asyncio
async def test_mailbox_list_project_uses_page_size_client_argument(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    class FakeClient:
        async def list_mailboxes(self, **kwargs):
            captured["list_mailboxes"] = kwargs
            return []

        async def close(self):
            captured["closed"] = True

    async def fake_get_client():
        return FakeClient()

    async def fake_resolve_project_id(project_id=None):
        return project_id or "project-1"

    monkeypatch.setattr(mailboxes, "get_client", fake_get_client)
    monkeypatch.setattr(mailboxes, "resolve_project_id", fake_resolve_project_id)

    await mailboxes.mailbox_list(
        project_id="project-1",
        room=None,
        filter="support",
        count=25,
        offset=9,
        o="json",
    )

    assert captured["list_mailboxes"] == {
        "project_id": "project-1",
        "page_size": 25,
        "filter": "support",
    }
    assert captured["closed"] is True
