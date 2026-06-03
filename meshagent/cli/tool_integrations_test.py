import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshagent.cli import tool_integrations


def test_configure_codex_default_integration_writes_default_provider(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: "project-life",
    )
    result = tool_integrations.configure_codex_default_integration(
        project_id="project-life",
        api_url="https://api.meshagent.life",
        meshagent_executable="/tmp/meshagent-life/bin/meshagent",
        config_path=config_path,
    )

    assert result.changed is True
    assert result.provider_id == "meshagent"
    assert result.profile_id == "meshagent"
    assert result.config_path == config_path
    assert config_path.read_text() == (
        'model_provider = "meshagent"\n'
        'model = "gpt-5.5"\n'
        "\n"
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
    )


def test_find_existing_codex_profiles_returns_matching_profiles(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[model_providers.openai]\n"
        'name = "OpenAI"\n'
        'base_url = "https://api.openai.com/v1"\n'
        "\n"
        "[model_providers.openrouter]\n"
        'name = "OpenRouter"\n'
        'base_url = "https://openrouter.ai/api/v1"\n'
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
        'model = "gpt-5.5"\n'
        "\n"
        "[profiles.router]\n"
        'model_provider = "openrouter"\n'
        'model = "openai/gpt-5.5"\n'
        "\n"
        "[profiles.meshagent-work]\n"
        'model_provider = "meshagent-work"\n'
        'model = "gpt-5.5"\n'
        "\n"
        "[profiles.meshagent]\n"
        'model_provider = "meshagent-work"\n'
        'model = "gpt-5.5"\n'
    )

    # Only MeshAgent-managed profiles are surfaced (openai/openrouter excluded).
    # `meshagent-old` is a dangling provider block referenced by no profile,
    # default, or *.config.toml file, so it is not listed.
    assert tool_integrations.find_existing_codex_profiles(
        config_path=config_path,
    ) == ["meshagent", "meshagent-work"]


def test_find_existing_codex_profiles_includes_stale_default_with_different_api_url(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'model_provider = "meshagent"\n'
        'model = "gpt-5.5"\n'
        "\n"
        "[model_providers.meshagent]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.wayvia.cloud/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-life"}\n'
    )

    # A default provider left over from a different API host must still be found.
    assert tool_integrations.find_existing_codex_profiles(
        config_path=config_path,
    ) == ["meshagent"]


def test_find_existing_codex_profiles_includes_stale_default_with_different_project(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'model_provider = "meshagent"\n'
        'model = "gpt-5.5"\n'
        "\n"
        "[model_providers.meshagent]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.meshagent.life/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-old"}\n'
    )

    # A default provider configured for a different project must still be found.
    assert tool_integrations.find_existing_codex_profiles(
        config_path=config_path,
    ) == ["meshagent"]


def test_find_existing_codex_profiles_excludes_non_meshagent_providers(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'model_provider = "openai"\n'
        "\n"
        "[model_providers.openai]\n"
        'name = "OpenAI"\n'
        'base_url = "https://api.openai.com/v1"\n'
        "\n"
        "[model_providers.openrouter]\n"
        'name = "OpenRouter"\n'
        'base_url = "https://openrouter.ai/api/v1"\n'
    )

    assert (
        tool_integrations.find_existing_codex_profiles(config_path=config_path) == []
    )


def test_find_existing_codex_profiles_does_not_require_active_project(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'model_provider = "meshagent"\n'
        'model = "gpt-5.5"\n'
        "\n"
        "[model_providers.meshagent]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.meshagent.life/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-life"}\n'
    )

    monkeypatch.setattr(tool_integrations, "get_active_project", lambda: None)

    # Detection no longer needs an active project (this is the stale-config case).
    assert (
        tool_integrations.find_existing_codex_profiles(config_path=config_path)
        == ["meshagent"]
    )


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
        'model = "gpt-5.5"\n'
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
        'model = "gpt-5.5"\n'
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
    updated = config_path.read_text()
    assert 'profile = "meshagent"\n' not in updated
    assert 'model_provider = "openai"\n' in updated


def test_remove_codex_integration_keeps_shared_provider_for_other_profiles(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'profile = "meshagent"\n'
        "\n"
        "[model_providers.meshagent]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.meshagent.com/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-life"}\n'
        "\n"
        "[profiles.meshagent]\n"
        'model_provider = "meshagent"\n'
        'model = "gpt-5.5"\n'
        "\n"
        "[profiles.meshagent-work]\n"
        'model_provider = "meshagent"\n'
        'model = "gpt-5.5"\n'
    )

    assert (
        tool_integrations.remove_codex_integration(
            profile_id="meshagent",
            config_path=config_path,
        )
        is True
    )

    assert config_path.read_text() == (
        "[model_providers.meshagent]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.meshagent.com/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-life"}\n'
        "\n"
        "[profiles.meshagent-work]\n"
        'model_provider = "meshagent"\n'
        'model = "gpt-5.5"\n'
    )


def test_remove_codex_integration_resets_default_and_removes_matching_profile_file(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    profile_path = tmp_path / "meshagent.config.toml"
    provider_block = (
        "[model_providers.meshagent]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.meshagent.com/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-life"}\n'
    )
    config_path.write_text(
        f'model_provider = "meshagent"\nmodel = "gpt-5.5"\n\n{provider_block}'
    )
    profile_path.write_text(
        f'model_provider = "meshagent"\nmodel = "gpt-5.5"\n\n{provider_block}'
    )

    assert (
        tool_integrations.remove_codex_integration(
            profile_id="meshagent",
            config_path=config_path,
        )
        is True
    )

    updated = config_path.read_text()
    assert 'model_provider = "openai"\n' in updated
    assert "[model_providers.meshagent]\n" not in updated
    assert not profile_path.exists()


def test_remove_codex_integration_removes_stale_default_with_different_api_url(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'model_provider = "meshagent"\n'
        'model = "gpt-5.5"\n'
        "\n"
        "[model_providers.meshagent]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.wayvia.cloud/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-old"}\n'
        "\n"
        "[model_providers.meshagent.auth]\n"
        'command = "meshagent"\n'
        'args = ["auth", "token"]\n'
        "timeout_ms = 10000\n"
        "refresh_interval_ms = 300000\n"
    )

    # Removal works on a stale default even with no active project / a different host.
    monkeypatch.setattr(tool_integrations, "get_active_project", lambda: None)

    assert (
        tool_integrations.remove_codex_integration(
            profile_id="meshagent",
            config_path=config_path,
        )
        is True
    )

    updated = config_path.read_text()
    assert 'model_provider = "openai"\n' in updated
    assert "[model_providers.meshagent]\n" not in updated


def test_remove_codex_integration_removes_stale_default_with_nondefault_provider_id(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'model_provider = "meshagent-old"\n'
        'model = "gpt-5.5"\n'
        "\n"
        "[model_providers.meshagent-old]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.wayvia.cloud/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-old"}\n'
        "\n"
        "[model_providers.meshagent-old.auth]\n"
        'command = "meshagent"\n'
        'args = ["auth", "token"]\n'
        "timeout_ms = 10000\n"
        "refresh_interval_ms = 300000\n"
    )

    monkeypatch.setattr(tool_integrations, "get_active_project", lambda: None)

    # A stale default under a non-default provider id is also cleaned up.
    assert (
        tool_integrations.remove_codex_integration(
            profile_id="meshagent-old",
            config_path=config_path,
        )
        is True
    )

    updated = config_path.read_text()
    assert 'model_provider = "openai"\n' in updated
    assert "[model_providers.meshagent-old]\n" not in updated


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
        'model_provider="meshagent"',
        "-c",
        'model="gpt-5.5"',
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
        'model_provider="meshagent"',
        "-c",
        'model="gpt-5.5"',
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
    installed_meshagent = tmp_path / "bin" / "meshagent"
    installed_meshagent.parent.mkdir(parents=True, exist_ok=True)
    installed_meshagent.write_text("#!/bin/sh\n")
    installed_meshagent.chmod(0o755)
    current_meshagent = tmp_path / "current" / "meshagent"
    current_meshagent.parent.mkdir(parents=True, exist_ok=True)
    current_meshagent.write_text("#!/bin/sh\n")
    current_meshagent.chmod(0o755)

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: "project-123",
    )
    monkeypatch.setattr(
        tool_integrations.shutil,
        "which",
        lambda command: str(installed_meshagent) if command == "meshagent" else None,
    )
    monkeypatch.setattr(
        tool_integrations,
        "resolve_current_meshagent_executable",
        lambda *args, **kwargs: str(current_meshagent),
    )

    tool_integrations.configure_claude_code_integration(
        project_id="project-123",
        api_url="https://api.meshagent.test",
        settings_path=settings_path,
    )

    assert (
        json.loads(settings_path.read_text())["apiKeyHelper"]
        == f"{installed_meshagent} auth token"
    )


def test_inspect_claude_code_integration_returns_meshagent_status(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {
                "env": {
                    "MESHAGENT_API_URL": "https://api.meshagent.test",
                    "MESHAGENT_PROJECT_ID": "project-123",
                    "ANTHROPIC_BASE_URL": "https://api.meshagent.test/anthropic",
                    "ANTHROPIC_CUSTOM_HEADERS": "Meshagent-Project-Id: project-123",
                },
                "apiKeyHelper": "/opt/homebrew/bin/meshagent auth token",
            }
        )
    )

    status = tool_integrations.inspect_claude_code_integration(
        settings_path=settings_path,
    )

    assert status == tool_integrations.ClaudeIntegrationStatus(
        configured=True,
        project_id="project-123",
        api_url="https://api.meshagent.test",
    )


def test_clear_claude_code_integration_removes_only_meshagent_settings(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {
                "env": {
                    "KEEP_ME": "yes",
                    "MESHAGENT_API_URL": "https://api.meshagent.test",
                    "MESHAGENT_PROJECT_ID": "project-123",
                    "ANTHROPIC_BASE_URL": "https://api.meshagent.test/anthropic",
                    "ANTHROPIC_CUSTOM_HEADERS": "Meshagent-Project-Id: project-123",
                },
                "apiKeyHelper": "meshagent auth token",
                "theme": "dark",
            }
        )
    )

    assert (
        tool_integrations.clear_claude_code_integration(settings_path=settings_path)
        is True
    )
    assert json.loads(settings_path.read_text()) == {
        "env": {"KEEP_ME": "yes"},
        "theme": "dark",
    }


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
