import os

from meshagent.cli import local_settings


def _patch_settings_paths(monkeypatch, tmp_path) -> None:
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

    assert settings.active_user_id == local_settings.LOCAL_STATE_USER_ID
    local_state = settings.users[local_settings.LOCAL_STATE_USER_ID]
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
        api_url="https://legacy.meshagent.test/",
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

    assert settings.active_user_id == "user-123"
    assert local_settings.LOCAL_STATE_USER_ID not in settings.users
    user_settings = settings.users["user-123"]
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
