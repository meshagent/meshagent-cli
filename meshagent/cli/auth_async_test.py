from meshagent.cli import auth_async


def test_scopes_defaults_to_full_official_scope_set(
    monkeypatch,
) -> None:
    monkeypatch.delenv("MESHAGENT_OAUTH_SCOPES", raising=False)

    assert auth_async._scopes() == auth_async.DEFAULT_OAUTH_SCOPE_STRING


def test_scopes_prefers_environment_override(monkeypatch) -> None:
    monkeypatch.setenv("MESHAGENT_OAUTH_SCOPES", "admin")

    assert auth_async._scopes() == "admin"
