import pytest
from types import SimpleNamespace

from meshagent.api import ApiScope, ParticipantToken
from meshagent.api.keys import ApiKey, encode_api_key
from meshagent.cli import helper


def test_print_json_table_uses_custom_empty_message() -> None:
    with pytest.raises(SystemExit, match="No LLM loggers found"):
        helper.print_json_table([], empty="No LLM loggers found")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "relation"),
    [
        (
            lambda client, project_id: client.can_create_rooms(project_id),
            "room_creator",
        ),
        (
            lambda client, project_id: client.can_use_llm_proxy(project_id),
            "llm_proxy_user",
        ),
    ],
)
async def test_custom_client_project_permission_helpers_check_current_user_project_role(
    method,
    relation: str,
) -> None:
    calls: list[dict[str, object]] = []
    client = helper.CustomMeshagentClient(base_url="http://example.test", token="token")

    async def fake_test_access(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(allowed=True)

    client.test_access = fake_test_access

    try:
        assert await method(client, "project-1") is True
    finally:
        await client.close()

    assert len(calls) == 1
    call = calls[0]
    assert call["project_id"] == "project-1"
    assert call["relation"] == relation
    assert call["subject"].type == "user"
    assert call["subject"].id == "me"
    assert call["resource"].type == "project"
    assert call["resource"].id == "project-1"


@pytest.mark.asyncio
async def test_mint_participant_token_for_cli_signs_with_active_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-input"
        return "project-1"

    async def fake_get_active_api_key(project_id: str) -> str | None:
        assert project_id == "project-1"
        return encode_api_key(
            ApiKey(
                id="11111111-1111-1111-1111-111111111111",
                project_id="22222222-2222-2222-2222-222222222222",
                secret="test-secret",
            )
        )

    async def fail_get_client():
        raise AssertionError("active API key should sign locally")

    monkeypatch.delenv("MESHAGENT_API_KEY", raising=False)
    monkeypatch.delenv("MESHAGENT_SECRET", raising=False)
    monkeypatch.setattr(helper, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(helper, "get_active_api_key", fake_get_active_api_key)
    monkeypatch.setattr(helper, "get_client", fail_get_client)

    jwt = await helper.mint_participant_token_for_cli(
        project_id="project-input",
        name="agent-name",
        room_name="room-name",
        role="agent",
        api_scope=ApiScope.agent_default(),
    )

    token = ParticipantToken.from_jwt(jwt, validate=False)
    assert token.project_id == "22222222-2222-2222-2222-222222222222"
    assert token.name == "agent-name"
    assert token.role == "agent"
    assert token.grant_scope("room") == "room-name"
    assert token.get_api_grant() == ApiScope.agent_default()


@pytest.mark.asyncio
async def test_mint_participant_token_for_cli_uses_router_without_signing_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeClient:
        async def mint_participant_token(self, project_id: str, **kwargs) -> str:
            calls.append({"project_id": project_id, **kwargs})
            return "router-token"

        async def close(self) -> None:
            calls.append({"closed": True})

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-input"
        return "project-1"

    async def fake_get_active_api_key(project_id: str) -> str | None:
        assert project_id == "project-1"
        return None

    async def fake_get_client() -> FakeClient:
        return FakeClient()

    monkeypatch.delenv("MESHAGENT_API_KEY", raising=False)
    monkeypatch.delenv("MESHAGENT_SECRET", raising=False)
    monkeypatch.setattr(helper, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(helper, "get_active_api_key", fake_get_active_api_key)
    monkeypatch.setattr(helper, "get_client", fake_get_client)

    jwt = await helper.mint_participant_token_for_cli(
        project_id="project-input",
        name="agent-name",
        room_name="room-name",
        role="agent",
        api_scope=ApiScope.agent_default(),
    )

    assert jwt == "router-token"
    assert calls == [
        {
            "project_id": "project-1",
            "name": "agent-name",
            "room_name": "room-name",
            "role": "agent",
            "api": ApiScope.agent_default().model_dump(mode="json"),
        },
        {"closed": True},
    ]


@pytest.mark.asyncio
async def test_mint_participant_token_for_cli_passes_serialized_grants_to_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    grants = [
        {"name": "role", "scope": "agent"},
        {"name": "room", "scope": "room-name"},
        {"name": "tunnel_ports", "scope": "9000"},
    ]

    class FakeClient:
        async def mint_participant_token(self, project_id: str, **kwargs) -> str:
            calls.append({"project_id": project_id, **kwargs})
            return "router-token"

        async def close(self) -> None:
            calls.append({"closed": True})

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-input"
        return "project-1"

    async def fake_get_active_api_key(project_id: str) -> str | None:
        assert project_id == "project-1"
        return None

    async def fake_get_client() -> FakeClient:
        return FakeClient()

    monkeypatch.delenv("MESHAGENT_API_KEY", raising=False)
    monkeypatch.delenv("MESHAGENT_SECRET", raising=False)
    monkeypatch.setattr(helper, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(helper, "get_active_api_key", fake_get_active_api_key)
    monkeypatch.setattr(helper, "get_client", fake_get_client)

    jwt = await helper.mint_participant_token_for_cli(
        project_id="project-input",
        name="agent-name",
        grants=grants,
    )

    assert jwt == "router-token"
    assert calls == [
        {
            "project_id": "project-1",
            "name": "agent-name",
            "grants": grants,
        },
        {"closed": True},
    ]
