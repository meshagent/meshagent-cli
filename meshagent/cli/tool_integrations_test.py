import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshagent.cli import tool_integrations


def test_configure_codex_integration_writes_named_profile(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"

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
        'command = "/tmp/meshagent-life/bin/meshagent"\n'
        'args = ["auth", "token"]\n'
        "timeout_ms = 10000\n"
        "refresh_interval_ms = 300000\n"
        "\n"
        "[profiles.meshagent]\n"
        'model_provider = "meshagent"\n'
        'model = "gpt-5.4"\n'
    )


def test_configure_codex_integration_prefers_meshagent_command_from_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: "project-life",
    )
    monkeypatch.setattr(
        tool_integrations.shutil,
        "which",
        lambda command: (
            "/opt/homebrew/bin/meshagent" if command == "meshagent" else None
        ),
    )
    monkeypatch.setattr(
        tool_integrations,
        "resolve_current_meshagent_executable",
        lambda *args, **kwargs: "/tmp/current/bin/meshagent",
    )

    tool_integrations.configure_codex_integration(
        profile_id="meshagent",
        project_id="project-life",
        api_url="https://api.meshagent.life",
        config_path=config_path,
    )

    assert 'command = "meshagent"\n' in config_path.read_text()
    assert 'args = ["auth", "token"]\n' in config_path.read_text()


def test_configure_codex_integration_appends_after_existing_content(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
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
    )

    updated = config_path.read_text()
    assert result.changed is True
    assert result.provider_id == "meshagent-prod"
    assert result.profile_id == "meshagent-prod"
    assert updated.startswith('model = "gpt-5.4"\n\n')
    assert "[model_providers.meshagent-prod]\n" in updated
    assert "[profiles.meshagent-prod]\n" in updated
    assert 'command = "/opt/homebrew/bin/meshagent"\n' in updated
    assert 'args = ["auth", "token"]\n' in updated


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


def test_configure_codex_integration_does_not_create_auth_wrapper_files(
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

    assert wrapper_dir.exists() is False
    assert (
        config_path.read_text().count('command = "/opt/homebrew/bin/meshagent"\n') == 2
    )
    assert config_path.read_text().count('args = ["auth", "token"]\n') == 2


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


def test_find_current_codex_default_profile_returns_matching_profile(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'profile = "meshagent-work"\n'
        "\n"
        "[model_providers.meshagent-work]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.meshagent.life/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-life"}\n'
        "\n"
        "[profiles.meshagent-work]\n"
        'model_provider = "meshagent-work"\n'
        'model = "gpt-5.4"\n'
    )

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: "project-life",
    )

    assert (
        tool_integrations.find_current_codex_default_profile(
            api_url="https://api.meshagent.life",
            config_path=config_path,
        )
        == "meshagent-work"
    )


def test_set_codex_default_profile_writes_root_profile_setting(
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

    assert (
        tool_integrations.set_codex_default_profile(
            profile_id="meshagent",
            config_path=config_path,
        )
        is True
    )
    assert config_path.read_text().startswith('profile = "meshagent"\n')


def test_clear_codex_default_profile_if_meshagent_project_removes_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'profile = "meshagent"\n'
        "\n"
        "[model_providers.meshagent]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.meshagent.life/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-life"}\n'
        "\n"
        "[profiles.meshagent]\n"
        'model_provider = "meshagent"\n'
        'model = "gpt-5.4"\n'
    )

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: "project-life",
    )

    assert (
        tool_integrations.clear_codex_default_profile_if_meshagent_project(
            api_url="https://api.meshagent.life",
            config_path=config_path,
        )
        is True
    )
    assert 'profile = "meshagent"\n' not in config_path.read_text()


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
            "Codex detected. Add a MeshAgent proxy profile to ~/.codex/config.toml "
            "so Codex uses your MeshAgent account by default?",
            True,
        )
    ]
    assert messages == []


def test_maybe_configure_local_tool_integrations_retries_until_profile_name_is_usable(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
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
    updated = config_path.read_text()
    assert "[profiles.meshagent-work]\n" in updated
    assert 'command = "/opt/homebrew/bin/meshagent"\n' in updated
    assert 'args = ["auth", "token"]\n' in updated


def test_build_codex_launch_command_sets_profile_overrides() -> None:
    command = tool_integrations.build_codex_launch_command(
        project_id="project-123",
        api_url="https://api.meshagent.test",
        extra_args=("--search", "fix auth flow"),
        meshagent_executable="/tmp/meshagent-life/bin/meshagent",
        codex_executable="/tmp/codex",
    )

    assert command == [
        "/tmp/codex",
        "-c",
        'model_providers.meshagent.name="MeshAgent"',
        "-c",
        'model_providers.meshagent.base_url="https://api.meshagent.test/openai/v1"',
        "-c",
        'model_providers.meshagent.http_headers={"Meshagent-Project-Id"="project-123"}',
        "-c",
        'model_providers.meshagent.auth.command="/tmp/meshagent-life/bin/meshagent"',
        "-c",
        'model_providers.meshagent.auth.args=["auth", "token"]',
        "-c",
        "model_providers.meshagent.auth.timeout_ms=10000",
        "-c",
        "model_providers.meshagent.auth.refresh_interval_ms=300000",
        "-c",
        'profiles.meshagent.model_provider="meshagent"',
        "-c",
        'profiles.meshagent.model="gpt-5.4"',
        "-p",
        "meshagent",
        "--search",
        "fix auth flow",
    ]


def test_build_codex_launch_command_rejects_profile_override() -> None:
    with pytest.raises(RuntimeError, match="`meshagent launch codex`"):
        tool_integrations.build_codex_launch_command(
            project_id="project-123",
            extra_args=("--profile", "custom"),
            codex_executable="/tmp/codex",
        )


def test_launch_codex_runs_subprocess() -> None:
    captured: dict[str, object] = {}

    def _fake_run(command, *, check: bool):
        captured["command"] = command
        captured["check"] = check
        return SimpleNamespace(returncode=9)

    exit_code = tool_integrations.launch_codex(
        project_id="project-123",
        api_url="https://api.meshagent.test",
        extra_args=("write tests",),
        meshagent_executable="/tmp/meshagent-life/bin/meshagent",
        codex_executable="/tmp/codex",
        command_runner=_fake_run,
    )

    assert exit_code == 9
    assert captured["check"] is False
    assert captured["command"] == [
        "/tmp/codex",
        "-c",
        'model_providers.meshagent.name="MeshAgent"',
        "-c",
        'model_providers.meshagent.base_url="https://api.meshagent.test/openai/v1"',
        "-c",
        'model_providers.meshagent.http_headers={"Meshagent-Project-Id"="project-123"}',
        "-c",
        'model_providers.meshagent.auth.command="/tmp/meshagent-life/bin/meshagent"',
        "-c",
        'model_providers.meshagent.auth.args=["auth", "token"]',
        "-c",
        "model_providers.meshagent.auth.timeout_ms=10000",
        "-c",
        "model_providers.meshagent.auth.refresh_interval_ms=300000",
        "-c",
        'profiles.meshagent.model_provider="meshagent"',
        "-c",
        'profiles.meshagent.model="gpt-5.4"',
        "-p",
        "meshagent",
        "write tests",
    ]


def test_build_claude_code_env_sets_project_header_and_base_url() -> None:
    env = tool_integrations.build_claude_code_env(
        project_id="project-123",
        api_url="https://api.meshagent.test",
        base_env={
            "KEEP_ME": "yes",
            "ANTHROPIC_API_KEY": "old-key",
            "ANTHROPIC_AUTH_TOKEN": "old-token",
        },
    )

    assert env["KEEP_ME"] == "yes"
    assert env["MESHAGENT_API_URL"] == "https://api.meshagent.test"
    assert env["MESHAGENT_PROJECT_ID"] == "project-123"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.meshagent.test/anthropic"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "Meshagent-Project-Id: project-123"
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_configure_claude_code_integration_writes_settings_json(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "claude" / "settings.json"

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: "project-123",
    )

    result = tool_integrations.configure_claude_code_integration(
        project_id="project-123",
        api_url="https://api.meshagent.test",
        meshagent_executable="/tmp/meshagent-life/bin/meshagent",
        settings_path=settings_path,
    )

    assert result.changed is True
    assert result.settings_path == settings_path
    assert json.loads(settings_path.read_text()) == {
        "env": {
            "MESHAGENT_API_URL": "https://api.meshagent.test",
            "MESHAGENT_PROJECT_ID": "project-123",
            "ANTHROPIC_BASE_URL": "https://api.meshagent.test/anthropic",
            "ANTHROPIC_CUSTOM_HEADERS": "Meshagent-Project-Id: project-123",
        },
        "apiKeyHelper": "/tmp/meshagent-life/bin/meshagent auth token",
    }


def test_configure_claude_code_integration_prefers_meshagent_command_from_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "claude" / "settings.json"

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: "project-123",
    )
    monkeypatch.setattr(
        tool_integrations.shutil,
        "which",
        lambda command: (
            "/opt/homebrew/bin/meshagent" if command == "meshagent" else None
        ),
    )
    monkeypatch.setattr(
        tool_integrations,
        "resolve_current_meshagent_executable",
        lambda *args, **kwargs: "/tmp/current/bin/meshagent",
    )

    tool_integrations.configure_claude_code_integration(
        project_id="project-123",
        api_url="https://api.meshagent.test",
        settings_path=settings_path,
    )

    assert (
        json.loads(settings_path.read_text())["apiKeyHelper"] == "meshagent auth token"
    )


def test_build_claude_code_command_uses_api_key_helper() -> None:
    command = tool_integrations.build_claude_code_command(
        extra_args=("-p", "say hi"),
        meshagent_executable="/tmp/meshagent-life/bin/meshagent",
        claude_executable="/tmp/claude",
    )

    assert command == [
        "/tmp/claude",
        "--settings",
        '{"apiKeyHelper": "/tmp/meshagent-life/bin/meshagent auth token"}',
        "-p",
        "say hi",
    ]


def test_resolve_meshagent_auth_command_falls_back_for_non_executable_argv0(
    monkeypatch,
    tmp_path: Path,
) -> None:
    non_executable = tmp_path / "cli.py"
    non_executable.write_text("print('hi')\n")
    non_executable.chmod(0o644)

    monkeypatch.setattr(tool_integrations.sys, "argv", [str(non_executable)])
    monkeypatch.setattr(tool_integrations.shutil, "which", lambda command: None)
    monkeypatch.setattr(tool_integrations.sys, "executable", "/tmp/python")

    assert tool_integrations._resolve_meshagent_auth_command() == (
        "/tmp/python -m meshagent.cli.cli auth token"
    )


def test_build_claude_code_command_rejects_settings_override() -> None:
    with pytest.raises(RuntimeError, match="`meshagent launch claude`"):
        tool_integrations.build_claude_code_command(
            extra_args=("--settings", "{}"),
            claude_executable="/tmp/claude",
        )


def test_launch_claude_code_runs_subprocess_with_meshagent_env() -> None:
    captured: dict[str, object] = {}

    def _fake_run(command, *, env: dict[str, str], check: bool):
        captured["command"] = command
        captured["env"] = env
        captured["check"] = check
        return SimpleNamespace(returncode=17)

    exit_code = tool_integrations.launch_claude_code(
        project_id="project-123",
        api_url="https://api.meshagent.test",
        extra_args=("-p", "say hi"),
        meshagent_executable="/tmp/meshagent-life/bin/meshagent",
        claude_executable="/tmp/claude",
        command_runner=_fake_run,
        base_env={"KEEP_ME": "yes"},
    )

    assert exit_code == 17
    assert captured["command"] == [
        "/tmp/claude",
        "--settings",
        '{"apiKeyHelper": "/tmp/meshagent-life/bin/meshagent auth token"}',
        "-p",
        "say hi",
    ]
    assert captured["check"] is False
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["KEEP_ME"] == "yes"
    assert env["MESHAGENT_PROJECT_ID"] == "project-123"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.meshagent.test/anthropic"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "Meshagent-Project-Id: project-123"
