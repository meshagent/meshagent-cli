import pytest

from meshagent.api.oauth_scopes import FULL_OAUTH_SCOPE
from meshagent.cli import auth_async


class _FakeTokenResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, object]:
        assert mode == "json"
        assert exclude_none is True
        return self._payload


class _FakeMeshagent:
    instances: list["_FakeMeshagent"] = []

    def __init__(self, *, base_url: str, token: str) -> None:
        self.base_url = base_url
        self.token = token
        self.closed = False
        self.exchange_calls: list[dict[str, str]] = []
        _FakeMeshagent.instances.append(self)

    async def exchange_oauth_token(self, *, form: dict[str, str]) -> _FakeTokenResponse:
        self.exchange_calls.append(form)
        return _FakeTokenResponse(
            {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        )

    async def close(self) -> None:
        self.closed = True


def test_scopes_defaults_to_full_official_scope_set(
    monkeypatch,
) -> None:
    monkeypatch.delenv("MESHAGENT_OAUTH_SCOPES", raising=False)

    assert auth_async._scopes() == FULL_OAUTH_SCOPE


def test_scopes_prefers_environment_override(monkeypatch) -> None:
    monkeypatch.setenv("MESHAGENT_OAUTH_SCOPES", "admin")

    assert auth_async._scopes() == "admin"


@pytest.mark.asyncio
async def test_exchange_code_for_tokens_uses_meshagent_client(monkeypatch) -> None:
    _FakeMeshagent.instances = []
    monkeypatch.setattr(auth_async, "Meshagent", _FakeMeshagent)
    monkeypatch.setenv("MESHAGENT_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.delenv("MESHAGENT_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(auth_async, "_now", lambda: 1000)

    tokens = await auth_async._exchange_code_for_tokens(
        "auth-code",
        "code-verifier",
        api_url="https://api.meshagent.test",
    )

    assert tokens == {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "expires_in": 3600,
        "token_type": "Bearer",
        "expires_at": 4570,
    }
    assert len(_FakeMeshagent.instances) == 1
    client = _FakeMeshagent.instances[0]
    assert client.base_url == "https://api.meshagent.test"
    assert client.token == ""
    assert client.exchange_calls == [
        {
            "grant_type": "authorization_code",
            "code": "auth-code",
            "redirect_uri": "http://localhost:8765/callback",
            "client_id": "client-id",
            "code_verifier": "code-verifier",
        }
    ]
    assert client.closed is True
