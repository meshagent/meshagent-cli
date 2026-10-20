import os
import json
import pytest
from pathlib import Path

from meshagent.cli import local_settings


def _patch_settings_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MESHAGENT_API_URL", raising=False)
    monkeypatch.setattr(local_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(local_settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(
        local_settings,
        "LEGACY_SESSION_FILE",
        tmp_path / "session.json",
    )
    monkeypatch.setattr(
        local_settings,
        "LEGACY_PROJECT_FILE",
        tmp_path / "project.json",
    )


def test_load_settings_migrates_legacy_session_and_project(
    tmp_path,
    monkeypatch,
) -> None:
    _patch_settings_paths(monkeypatch, tmp_path)
    local_settings.LEGACY_SESSION_FILE.write_text(
        '{"access_token":"access-token","refresh_token":"refresh-token","expires_at":123}'
    )
    local_settings.LEGACY_PROJECT_FILE.write_text(
        '{"active_project":"project-123","active_api_keys":{"project-123":"ma-key"}}'
    )

    settings = local_settings.load_settings()

    key = local_settings.profile_key(local_settings.LOCAL_STATE_USER_ID, None)
    assert settings.active_user_id == key
    local_state = settings.users[key]
    assert local_state.session is not None
    assert local_state.session.access_token == "access-token"
    assert local_state.project.active_project == "project-123"
    assert local_state.project.active_api_keys == {"project-123": "ma-key"}
    assert local_settings.SETTINGS_FILE.exists() is True
    assert local_settings.LEGACY_SESSION_FILE.exists() is False
    assert local_settings.LEGACY_PROJECT_FILE.exists() is False


def test_save_authenticated_profile_promotes_local_state_to_user(
    tmp_path,
    monkeypatch,
) -> None:
    _patch_settings_paths(monkeypatch, tmp_path)

    local_settings.set_local_session(
        session=local_settings.StoredSession(access_token="legacy-token"),
        api_url="https://profile.meshagent.test/",
    )
    local_settings.set_active_project("project-legacy")
    local_settings.set_active_api_key("project-legacy", "ma-legacy")

    local_settings.save_authenticated_profile(
        profile=local_settings.StoredUserProfile(
            id="user-123",
            first_name="Jesse",
            last_name="Ezell",
            email="jesse@example.com",
        ),
        session=local_settings.StoredSession(access_token="new-token"),
        api_url="https://profile.meshagent.test/",
    )

    settings = local_settings.load_settings()

    key = local_settings.profile_key("user-123", "https://profile.meshagent.test")
    assert settings.active_user_id == key
    assert len(settings.users) == 1
    user_settings = settings.users[key]
    assert user_settings.session is not None
    assert user_settings.session.access_token == "new-token"
    assert user_settings.project.active_project == "project-legacy"
    assert user_settings.project.active_api_keys == {"project-legacy": "ma-legacy"}
    assert user_settings.api_url == "https://profile.meshagent.test"


def test_switch_active_profile_matches_email_and_updates_process_api_url(
    tmp_path,
    monkeypatch,
) -> None:
    _patch_settings_paths(monkeypatch, tmp_path)
    monkeypatch.delenv(local_settings.PROFILE_API_URL_ENV, raising=False)

    local_settings.save_authenticated_profile(
        profile=local_settings.StoredUserProfile(
            id="user-1",
            first_name="One",
            last_name="User",
            email="one@example.com",
        ),
        session=local_settings.StoredSession(access_token="token-1"),
        api_url="https://one.meshagent.test",
    )
    local_settings.save_authenticated_profile(
        profile=local_settings.StoredUserProfile(
            id="user-2",
            first_name="Two",
            last_name="User",
            email="two@example.com",
        ),
        session=local_settings.StoredSession(access_token="token-2"),
        api_url="https://two.meshagent.test",
    )

    selected_profile = local_settings.switch_active_profile("two@example.com")

    assert selected_profile.user_id == "user-2"
    assert local_settings.get_active_user_id() == "user-2"
    assert (
        os.environ[local_settings.PROFILE_API_URL_ENV] == "https://two.meshagent.test"
    )


def test_resolve_api_url_prefers_explicit_then_environment_then_profile(
    tmp_path,
    monkeypatch,
) -> None:
    _patch_settings_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("MESHAGENT_API_URL", "https://env.meshagent.test")

    local_settings.save_authenticated_profile(
        profile=local_settings.StoredUserProfile(
            id="user-123",
            first_name="Jesse",
            last_name="Ezell",
            email="jesse@example.com",
        ),
        session=local_settings.StoredSession(access_token="token-1"),
        api_url="https://profile.meshagent.test",
    )

    assert local_settings.resolve_api_url() == "https://env.meshagent.test"
    assert (
        local_settings.resolve_api_url(api_url="https://explicit.meshagent.test/")
        == "https://explicit.meshagent.test"
    )


def test_resolve_pages_domain_maps_life_api_to_app_domain() -> None:
    assert (
        local_settings.resolve_pages_domain(api_url="https://api.meshagent.life")
        == "meshagent.app"
    )


def test_resolve_pages_domain_maps_prod_api_to_app_domain() -> None:
    assert (
        local_settings.resolve_pages_domain(api_url="https://api.meshagent.com")
        == "meshagent.app"
    )


def _save_profile(api_url, token):
    local_settings.save_authenticated_profile(
        profile=local_settings.StoredUserProfile(
            id="same-id", email="same@example.com"
        ),
        session=local_settings.StoredSession(
            access_token=token, refresh_token=f"refresh-{token}", expires_at=4102444800
        ),
        api_url=api_url,
    )


def test_same_id_and_email_can_switch_environments_without_sharing_project_keys(
    tmp_path, monkeypatch
):
    _patch_settings_paths(monkeypatch, tmp_path)
    for environment in ["https://api.meshagent.com", "https://api.meshagent.life"]:
        _save_profile(environment, environment)
        local_settings.set_active_project("same-project-id")
        local_settings.set_active_api_key("same-project-id", f"key-{environment}")
        local_settings.set_llm_proxy_bearer_token(f"llm-{environment}")
    assert len(local_settings.list_saved_profiles()) == 2
    with pytest.raises(LookupError, match="--api-url"):
        local_settings.switch_active_profile("same-id")
    for selector in ["same-id", "same@example.com"]:
        for environment in ["https://api.meshagent.com", "https://api.meshagent.life"]:
            selected = local_settings.switch_active_profile(
                selector, api_url=f"{environment}/"
            )
            assert selected.user_id == "same-id"
            assert local_settings.resolve_api_url() == environment
            assert local_settings.get_active_user_id() == "same-id"
            assert local_settings.get_active_session().access_token == environment
            assert (
                local_settings.get_active_api_key("same-project-id")
                == f"key-{environment}"
            )
            assert local_settings.get_llm_proxy_bearer_token() == f"llm-{environment}"
    local_settings.clear_active_session()
    local_settings.switch_active_profile("same-id", api_url="https://api.meshagent.com")
    assert (
        local_settings.get_active_session().access_token == "https://api.meshagent.com"
    )


def test_environment_override_never_reuses_credentials_from_another_environment(
    tmp_path, monkeypatch
):
    _patch_settings_paths(monkeypatch, tmp_path)
    _save_profile("https://api.meshagent.com", "com-token")
    local_settings.set_active_api_key("project", "com-key")
    monkeypatch.setenv("MESHAGENT_API_URL", "https://api.meshagent.life")
    assert local_settings.get_active_session() is None
    assert local_settings.get_active_api_key("project") is None
    local_settings.set_active_session(
        session=local_settings.StoredSession(access_token="life-token"),
        api_url="https://api.meshagent.life",
    )
    assert local_settings.get_active_session().access_token == "life-token"
    monkeypatch.setenv("MESHAGENT_API_URL", "https://api.meshagent.com")
    assert local_settings.get_active_session().access_token == "com-token"
    assert local_settings.get_active_api_key("project") == "com-key"


def test_legacy_profile_upgrade_preserves_state_is_atomic_and_idempotent(
    tmp_path, monkeypatch
):
    _patch_settings_paths(monkeypatch, tmp_path)
    legacy = {
        "active_user_id": "same-id",
        "users": {
            "same-id": {
                "profile": {"id": "same-id", "email": "same@example.com"},
                "api_url": "https://api.meshagent.life/",
                "session": {
                    "access_token": "preserve",
                    "refresh_token": "preserve-refresh",
                    "expires_at": 4102444800,
                },
                "project": {
                    "active_project": "p",
                    "active_api_keys": {"p": "key"},
                    "llm_proxy_bearer_token": "llm",
                },
            }
        },
    }
    local_settings.SETTINGS_FILE.write_text(json.dumps(legacy))
    first = local_settings.load_settings()
    key = local_settings.profile_key("same-id", "https://api.meshagent.life")
    assert first.active_user_id == key
    assert first.active_profiles == {"https://api.meshagent.life": key}
    assert first.users[key].project.active_api_keys == {"p": "key"}
    assert first.users[key].session.refresh_token == "preserve-refresh"
    assert first.users[key].project.llm_proxy_bearer_token == "llm"
    written = local_settings.SETTINGS_FILE.read_bytes()
    modified = local_settings.SETTINGS_FILE.stat().st_mtime_ns
    assert local_settings.load_settings() == first
    assert local_settings.SETTINGS_FILE.read_bytes() == written
    assert local_settings.SETTINGS_FILE.stat().st_mtime_ns == modified
    if os.name == "posix":
        assert local_settings.SETTINGS_FILE.stat().st_mode & 0o777 == 0o600


def test_upgrade_matches_the_rust_cli_storage_fixture(tmp_path, monkeypatch):
    _patch_settings_paths(monkeypatch, tmp_path)
    fixture = json.loads(
        (
            Path(__file__).parents[2]
            / "tests/fixtures/profile_environment_upgrade.json"
        ).read_text()
    )
    local_settings.SETTINGS_FILE.write_text(json.dumps(fixture["legacy"]))
    assert local_settings.load_settings().model_dump(mode="json") == fixture["upgraded"]


def test_upgrade_without_environment_metadata_uses_default_not_current_override(
    tmp_path, monkeypatch
):
    _patch_settings_paths(monkeypatch, tmp_path)
    local_settings.SETTINGS_FILE.write_text(
        json.dumps(
            {
                "active_user_id": "u",
                "users": {
                    "u": {
                        "profile": {"id": "u"},
                        "session": {"access_token": "com-token"},
                    }
                },
            }
        )
    )
    monkeypatch.setenv("MESHAGENT_API_URL", "https://api.meshagent.life")
    assert local_settings.get_active_session() is None
    monkeypatch.setenv("MESHAGENT_API_URL", local_settings.DEFAULT_API_URL)
    assert local_settings.get_active_session().access_token == "com-token"


def test_failed_atomic_upgrade_preserves_the_original_file(tmp_path, monkeypatch):
    _patch_settings_paths(monkeypatch, tmp_path)
    original = b'{"users":{"u":{"profile":{"id":"u"}}}}'
    local_settings.SETTINGS_FILE.write_bytes(original)

    def fail_replace(source, destination):
        raise OSError(5, "injected storage failure")

    monkeypatch.setattr(local_settings.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected storage failure"):
        local_settings.load_settings()
    assert local_settings.SETTINGS_FILE.read_bytes() == original
    assert list(tmp_path.iterdir()) == [local_settings.SETTINGS_FILE]


def test_local_project_state_does_not_move_to_a_login_in_another_environment(
    tmp_path, monkeypatch
):
    _patch_settings_paths(monkeypatch, tmp_path)
    local_settings.set_local_session(
        session=local_settings.StoredSession(access_token="com-local"),
        api_url="https://api.meshagent.com",
    )
    local_settings.set_active_api_key("p", "com-key")
    _save_profile("https://api.meshagent.life", "life")
    assert local_settings.get_active_api_key("p") is None
    monkeypatch.setenv("MESHAGENT_API_URL", "https://api.meshagent.com")
    assert local_settings.get_active_api_key("p") == "com-key"


@pytest.mark.asyncio
async def test_refresh_targets_the_selected_environment_and_preserves_other_session(
    tmp_path, monkeypatch
):
    from meshagent.cli import auth_async

    _patch_settings_paths(monkeypatch, tmp_path)
    for environment in ["https://api.meshagent.com", "https://api.meshagent.life"]:
        _save_profile(environment, environment)
        local_settings.set_active_session(
            session=local_settings.StoredSession(
                access_token=environment,
                refresh_token=f"refresh-{environment}",
                expires_at=0,
            ),
            api_url=environment,
        )
    calls = []

    async def refresh(tokens, *, api_url):
        calls.append((api_url, tokens["refresh_token"]))
        return {**tokens, "access_token": f"new-{api_url}", "expires_at": 4102444800}

    monkeypatch.setattr(auth_async, "_refresh_tokens", refresh)
    for environment in ["https://api.meshagent.com", "https://api.meshagent.life"]:
        monkeypatch.setenv("MESHAGENT_API_URL", environment)
        assert await auth_async.get_access_token() == f"new-{environment}"
    assert calls == [
        (environment, f"refresh-{environment}")
        for environment in ["https://api.meshagent.com", "https://api.meshagent.life"]
    ]
