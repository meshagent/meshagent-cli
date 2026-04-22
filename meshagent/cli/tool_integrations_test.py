from pathlib import Path

import pytest

from meshagent.cli import tool_integrations


def test_configure_codex_integration_writes_named_profile(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    wrapper_path = tmp_path / "bin" / "codex-meshagent-auth"

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: "project-life",
    )
    result = tool_integrations.configure_codex_integration(
        profile_id="meshagent",
        project_id="project-life",
        api_url="https://api.meshagent.life",
        meshagent_executable="/tmp/meshagent-life/bin/meshagent",
        config_path=config_path,
        auth_wrapper_path=wrapper_path,
    )

    assert result.changed is True
    assert result.provider_id == "meshagent"
    assert result.profile_id == "meshagent"
    assert config_path.read_text() == (
        "[model_providers.meshagent]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.meshagent.life/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-life"}\n'
        "\n"
        "[model_providers.meshagent.auth]\n"
        f'command = "{wrapper_path}"\n'
        "timeout_ms = 10000\n"
        "refresh_interval_ms = 240000\n"
        "\n"
        "[profiles.meshagent]\n"
        'model_provider = "meshagent"\n'
        'model = "gpt-5.4"\n'
    )
    assert wrapper_path.read_text() == (
        "#!/bin/sh\nexec /tmp/meshagent-life/bin/meshagent auth token\n"
    )
    assert wrapper_path.stat().st_mode & 0o111 == 0o111


def test_configure_codex_integration_appends_after_existing_content(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    wrapper_path = tmp_path / "bin" / "codex-meshagent-auth"
    config_path.write_text('model = "gpt-5.4"\n')

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: "project-prod",
    )
    result = tool_integrations.configure_codex_integration(
        profile_id="meshagent-prod",
        project_id="project-prod",
        api_url="https://api.meshagent.com",
        meshagent_executable="/opt/homebrew/bin/meshagent",
        config_path=config_path,
        auth_wrapper_path=wrapper_path,
    )

    updated = config_path.read_text()
    assert result.changed is True
    assert result.provider_id == "meshagent-prod"
    assert result.profile_id == "meshagent-prod"
    assert updated.startswith('model = "gpt-5.4"\n\n')
    assert "[model_providers.meshagent-prod]\n" in updated
    assert "[profiles.meshagent-prod]\n" in updated
    assert wrapper_path.read_text() == (
        "#!/bin/sh\nexec /opt/homebrew/bin/meshagent auth token\n"
    )


def test_configure_codex_integration_rejects_profile_name_in_use(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[model_providers.meshagent]\n"
        'name = "MeshAgent"\n'
        "\n"
        "[profiles.meshagent]\n"
        'model_provider = "meshagent"\n'
        'model = "gpt-5.4"\n'
    )

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: "project-1",
    )

    with pytest.raises(ValueError, match="already in use"):
        tool_integrations.configure_codex_integration(
            profile_id="meshagent",
            project_id="project-1",
            api_url="https://api.meshagent.com",
            meshagent_executable="/opt/homebrew/bin/meshagent",
            config_path=config_path,
        )


def test_configure_codex_integration_uses_profile_specific_default_wrapper_paths(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    wrapper_dir = tmp_path / "bin"

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: "project-1",
    )
    monkeypatch.setattr(
        tool_integrations,
        "CODEX_AUTH_WRAPPER_DIR",
        wrapper_dir,
    )

    tool_integrations.configure_codex_integration(
        profile_id="meshagent",
        project_id="project-1",
        api_url="https://api.meshagent.com",
        meshagent_executable="/opt/homebrew/bin/meshagent",
        config_path=config_path,
    )
    tool_integrations.configure_codex_integration(
        profile_id="meshagent-work",
        project_id="project-1",
        api_url="https://api.meshagent.com",
        meshagent_executable="/opt/homebrew/bin/meshagent",
        config_path=config_path,
    )

    assert (
        wrapper_dir / "codex-meshagent-auth-meshagent"
    ).read_text() == "#!/bin/sh\nexec /opt/homebrew/bin/meshagent auth token\n"
    assert (
        wrapper_dir / "codex-meshagent-auth-meshagent-work"
    ).read_text() == "#!/bin/sh\nexec /opt/homebrew/bin/meshagent auth token\n"


def test_configure_codex_integration_requires_active_project(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: None,
    )

    with pytest.raises(RuntimeError, match="active MeshAgent project"):
        tool_integrations.configure_codex_integration(
            profile_id="meshagent",
            api_url="https://api.meshagent.com",
            meshagent_executable="/opt/homebrew/bin/meshagent",
            config_path=config_path,
        )


def test_find_existing_codex_profiles_returns_matching_profiles(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[model_providers.openai]\n"
        'name = "OpenAI"\n'
        'base_url = "https://api.openai.com/v1"\n'
        "\n"
        "[model_providers.meshagent-old]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.meshagent.life/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-old"}\n'
        "\n"
        "[model_providers.meshagent-work]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.meshagent.life/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-life"}\n'
        "\n"
        "[profiles.default]\n"
        'model_provider = "openai"\n'
        'model = "gpt-5.4"\n'
        "\n"
        "[profiles.meshagent-work]\n"
        'model_provider = "meshagent-work"\n'
        'model = "gpt-5.4"\n'
        "\n"
        "[profiles.meshagent]\n"
        'model_provider = "meshagent-work"\n'
        'model = "gpt-5.4"\n'
    )

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: "project-life",
    )

    assert tool_integrations.find_existing_codex_profiles(
        api_url="https://api.meshagent.life",
        config_path=config_path,
    ) == ["meshagent", "meshagent-work"]


def test_maybe_configure_local_tool_integrations_skips_when_codex_missing() -> None:
    confirmations: list[str] = []
    prompts: list[str] = []
    messages: list[str] = []

    tool_integrations.maybe_configure_local_tool_integrations(
        project_id="project-1",
        confirm_fn=lambda text, default=False: confirmations.append(text) or True,
        prompt_fn=lambda text, default="meshagent": prompts.append(text) or default,
        echo_fn=messages.append,
        which=lambda command: None,
    )

    assert confirmations == []
    assert prompts == []
    assert messages == []


def test_maybe_configure_local_tool_integrations_skips_when_user_declines(
    tmp_path: Path,
) -> None:
    confirmations: list[tuple[str, bool]] = []
    messages: list[str] = []

    tool_integrations.maybe_configure_local_tool_integrations(
        project_id="project-1",
        confirm_fn=lambda text, default=False: (
            confirmations.append((text, default)) or False
        ),
        prompt_fn=lambda text, default="meshagent": (_ for _ in ()).throw(
            AssertionError("profile prompt should not be shown")
        ),
        echo_fn=messages.append,
        which=lambda command: "/opt/homebrew/bin/codex" if command == "codex" else None,
        config_path=tmp_path / "config.toml",
    )

    assert confirmations == [
        (
            "Codex detected. Add a profile to ~/.codex/config.toml so Codex can use "
            "your MeshAgent account for access?",
            False,
        )
    ]
    assert messages == []


def test_maybe_configure_local_tool_integrations_retries_until_profile_name_is_usable(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    wrapper_path = tmp_path / "bin" / "codex-meshagent-auth"
    config_path.write_text(
        '[profiles.meshagent]\nmodel_provider = "meshagent"\nmodel = "gpt-5.4"\n'
    )
    messages: list[str] = []
    prompt_values = iter(["bad name", "meshagent", "meshagent-work"])

    tool_integrations.maybe_configure_local_tool_integrations(
        project_id="project-1",
        api_url="https://api.meshagent.life",
        meshagent_executable="/opt/homebrew/bin/meshagent",
        confirm_fn=lambda text, default=False: True,
        prompt_fn=lambda text, default="meshagent": next(prompt_values),
        echo_fn=messages.append,
        which=lambda command: "/opt/homebrew/bin/codex" if command == "codex" else None,
        config_path=config_path,
        auth_wrapper_path=wrapper_path,
    )

    assert messages == [
        (
            "Codex profile names may only include letters, numbers, hyphens, "
            "and underscores."
        ),
        (
            f"Codex profile `meshagent` is already in use in {config_path}. "
            "Choose a different name."
        ),
        (
            f"Configured Codex profile `meshagent-work` in {config_path}. "
            "Use `codex -p meshagent-work` to run Codex through MeshAgent."
        ),
    ]
    assert "[profiles.meshagent-work]\n" in config_path.read_text()
