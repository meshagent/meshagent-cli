import pytest

from meshagent.cli.messaging import wait_for_messaging_participants


class _FakeMessaging:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)

    def get_participants(self):
        if not self.snapshots:
            return []
        return self.snapshots.pop(0)


class _FakeClient:
    def __init__(self, snapshots):
        self.messaging = _FakeMessaging(snapshots)


@pytest.mark.asyncio
async def test_wait_for_messaging_participants_waits_for_discovery(monkeypatch):
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr("meshagent.cli.messaging.asyncio.sleep", fake_sleep)

    participant = object()
    client = _FakeClient([[], [participant]])

    result = await wait_for_messaging_participants(client, timeout=1)

    assert result == [participant]
    assert sleep_calls == [0.25]
