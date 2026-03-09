import asyncio

import pytest

from meshagent.cli import containers


class _FakeLogStream:
    def __init__(self) -> None:
        self.cancel_calls = 0
        self.cancelled = asyncio.Event()

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self.cancelled.set()


class _FakeContainers:
    def __init__(self, *, stream: _FakeLogStream, exit_code: int) -> None:
        self._stream = stream
        self._exit_code = exit_code
        self.log_calls: list[tuple[str, bool]] = []
        self.wait_calls: list[str] = []

    def logs(self, *, container_id: str, follow: bool = False) -> _FakeLogStream:
        self.log_calls.append((container_id, follow))
        return self._stream

    async def wait_for_exit(self, *, container_id: str) -> int:
        self.wait_calls.append(container_id)
        return self._exit_code


class _FakeClient:
    def __init__(self, *, stream: _FakeLogStream, exit_code: int) -> None:
        self.containers = _FakeContainers(stream=stream, exit_code=exit_code)


@pytest.mark.asyncio
async def test_stream_container_job_logs_and_wait_for_exit_cancels_follow_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _FakeLogStream()
    client = _FakeClient(stream=stream, exit_code=17)
    drain_started = asyncio.Event()
    drain_cancelled = asyncio.Event()

    async def _fake_drain(log_stream, *, show_progress: bool) -> None:
        assert log_stream is stream
        assert show_progress is False
        drain_started.set()
        await stream.cancelled.wait()
        drain_cancelled.set()

    monkeypatch.setattr(containers, "_drain_stream_plain", _fake_drain)

    exit_code = await containers._stream_container_job_logs_and_wait_for_exit(
        client=client, container_id="container-1"
    )

    assert exit_code == 17
    assert client.containers.log_calls == [("container-1", True)]
    assert client.containers.wait_calls == ["container-1"]
    assert drain_started.is_set()
    assert stream.cancel_calls == 1
    assert drain_cancelled.is_set()
