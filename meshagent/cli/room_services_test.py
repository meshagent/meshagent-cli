import pytest

from meshagent.api.room_server_client import (
    ListServicesResult,
    ServiceRuntimeEvent,
    ServiceRuntimeState,
)
from meshagent.api.specs.service import ContainerSpec, ServiceMetadata, ServiceSpec
from meshagent.cli import room_services


class _FakeServicesClient:
    async def list_with_state(self) -> ListServicesResult:
        return ListServicesResult(
            services=[
                ServiceSpec(
                    version="v1",
                    kind="Service",
                    id="svc-1",
                    metadata=ServiceMetadata(name="whoami"),
                    container=ContainerSpec(image="meshagent/cli:default"),
                )
            ],
            service_states={
                "svc-1": ServiceRuntimeState(
                    service_id="svc-1",
                    state="scheduled",
                    restart_count=1,
                    last_start_error=(
                        "container.environment.token.identity is required"
                    ),
                    last_start_error_at=124.0,
                    events=[
                        ServiceRuntimeEvent(
                            type="Warning",
                            reason="FailedStart",
                            message=(
                                "Unable to start service whoami: "
                                "container.environment.token.identity is required"
                            ),
                            count=1,
                            first_timestamp=123.0,
                            last_timestamp=124.0,
                        )
                    ],
                )
            },
        )


@pytest.mark.asyncio
async def test_room_services_describe_shows_runtime_state_and_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_tables: list[tuple[list[dict[str, object]], tuple[str, ...]]] = []

    async def fake_connect_services_client(*, project_id: str | None, room: str | None):
        assert project_id == "project-1"
        assert room == "room-1"
        return object(), object(), _FakeServicesClient()

    async def fake_close_services_client(account_client, room_client) -> None:
        del account_client, room_client

    def fake_print_json_table(rows, *columns):
        captured_tables.append((rows, columns))

    monkeypatch.setattr(
        room_services, "_connect_services_client", fake_connect_services_client
    )
    monkeypatch.setattr(
        room_services, "_close_services_client", fake_close_services_client
    )
    monkeypatch.setattr(room_services, "print_json_table", fake_print_json_table)
    monkeypatch.setattr(room_services, "print", lambda *args, **kwargs: None)

    await room_services.room_services_describe_command(
        project_id="project-1",
        room="room-1",
        service_id="svc-1",
    )

    state_rows, state_columns = captured_tables[0]
    event_rows, event_columns = captured_tables[1]
    assert "last_start_error" in state_columns
    assert "last_start_error_at" in state_columns
    assert (
        state_rows[0]["last_start_error"]
        == "container.environment.token.identity is required"
    )
    assert event_columns == (
        "type",
        "reason",
        "message",
        "count",
        "first_timestamp",
        "last_timestamp",
    )
    assert event_rows[0]["reason"] == "FailedStart"
    assert "token.identity" in str(event_rows[0]["message"])
