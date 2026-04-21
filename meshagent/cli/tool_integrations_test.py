from pathlib import Path

import pytest

from meshagent.cli import tool_integrations


def test_configure_codex_integration_writes_project_scoped_profile(
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
        project_id="project-life",
        project_name="Life",
        api_url="https://api.meshagent.life",
        meshagent_executable="/tmp/meshagent-life/bin/meshagent",
        config_path=config_path,
        auth_wrapper_path=wrapper_path,
    )

    assert result.changed is True
    assert result.provider_id == "meshagent-life"
    assert result.profile_id == "meshagent-life"
    assert config_path.read_text() == (
        "# BEGIN MESHAGENT MANAGED BLOCK: CODEX PROJECT project-life\n"
        "# Re-run `meshagent setup` from a different MeshAgent install"
        " if you want Codex to use a different binary.\n"
        "# MeshAgent project: Life (project-life)\n"
        "[model_providers.meshagent-life]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.meshagent.life/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-life"}\n'
        "\n"
        "[model_providers.meshagent-life.auth]\n"
        f'command = "{wrapper_path}"\n'
        "timeout_ms = 10000\n"
        "refresh_interval_ms = 240000\n"
        "\n"
        "[profiles.meshagent-life]\n"
        'model_provider = "meshagent-life"\n'
        'model = "gpt-5.4"\n'
        "# END MESHAGENT MANAGED BLOCK: CODEX PROJECT project-life\n"
    )
    assert wrapper_path.read_text() == (
        "#!/bin/sh\n"
        "exec /tmp/meshagent-life/bin/meshagent auth token\n"
    )
    assert wrapper_path.stat().st_mode & 0o111 == 0o111


def test_configure_codex_integration_reuses_existing_ids_for_same_project(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    wrapper_path = tmp_path / "bin" / "codex-meshagent-auth"
    config_path.write_text(
        'model = "gpt-5.4"\n\n'
        "# BEGIN MESHAGENT MANAGED BLOCK: CODEX PROJECT project-prod\n"
        "# Re-run `meshagent setup` from a different MeshAgent install"
        " if you want Codex to use a different binary.\n"
        "# MeshAgent project: Prod (project-prod)\n"
        "[model_providers.meshagent-prod]\n"
        'name = "MeshAgent"\n'
        "\n"
        "[profiles.meshagent-prod]\n"
        'model_provider = "meshagent-prod"\n'
        'model = "gpt-5.4"\n'
        "# END MESHAGENT MANAGED BLOCK: CODEX PROJECT project-prod\n"
    )

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: "project-prod",
    )
    result = tool_integrations.configure_codex_integration(
        project_id="project-prod",
        project_name="Production",
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
    assert '# MeshAgent project: Production (project-prod)\n' in updated
    assert '[model_providers.meshagent-prod]\n' in updated
    assert '[profiles.meshagent-prod]\n' in updated
    assert updated.count("BEGIN MESHAGENT MANAGED BLOCK: CODEX PROJECT project-prod") == 1
    assert wrapper_path.read_text() == (
        "#!/bin/sh\n"
        "exec /opt/homebrew/bin/meshagent auth token\n"
    )


def test_configure_codex_integration_adds_suffix_for_name_collisions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "# BEGIN MESHAGENT MANAGED BLOCK: CODEX PROJECT project-1\n"
        "# Re-run `meshagent setup` from a different MeshAgent install"
        " if you want Codex to use a different binary.\n"
        "# MeshAgent project: Foo (project-1)\n"
        "[model_providers.meshagent-foo]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.meshagent.life/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-1"}\n'
        "\n"
        "[model_providers.meshagent-foo.auth]\n"
        'command = "/tmp/meshagent"\n'
        "timeout_ms = 10000\n"
        "refresh_interval_ms = 240000\n"
        "\n"
        "[profiles.meshagent-foo]\n"
        'model_provider = "meshagent-foo"\n'
        'model = "gpt-5.4"\n'
        "# END MESHAGENT MANAGED BLOCK: CODEX PROJECT project-1\n"
    )

    monkeypatch.setattr(
        tool_integrations,
        "get_active_project",
        lambda: "project-2",
    )
    result = tool_integrations.configure_codex_integration(
        project_id="project-2",
        project_name="Foo",
        api_url="https://api.meshagent.life",
        meshagent_executable="/tmp/meshagent-life/bin/meshagent",
        config_path=config_path,
        auth_wrapper_path=tmp_path / "bin" / "codex-meshagent-auth",
    )

    updated = config_path.read_text()
    assert result.provider_id == "meshagent-foo-2"
    assert result.profile_id == "meshagent-foo-2"
    assert "[model_providers.meshagent-foo-2]\n" in updated
    assert "[profiles.meshagent-foo-2]\n" in updated


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
            api_url="https://api.meshagent.com",
            meshagent_executable="/opt/homebrew/bin/meshagent",
            config_path=config_path,
        )


def test_maybe_configure_local_tool_integrations_skips_when_codex_missing() -> None:
    prompts: list[str] = []
    messages: list[str] = []

    tool_integrations.maybe_configure_local_tool_integrations(
        project_id="project-1",
        project_name="Foo",
        prompt_fn=lambda message: prompts.append(message) or True,
        echo_fn=messages.append,
        which=lambda command: None,
    )

    assert prompts == []
    assert messages == []


def test_maybe_configure_local_tool_integrations_reports_configured_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    messages: list[str] = []

    monkeypatch.setattr(
        tool_integrations,
        "configure_codex_integration",
        lambda **kwargs: tool_integrations.CodexIntegrationResult(
            config_path=tmp_path / "config.toml",
            provider_id="meshagent-foo",
            profile_id="meshagent-foo",
            changed=True,
        ),
    )

    tool_integrations.maybe_configure_local_tool_integrations(
        project_id="project-1",
        project_name="Foo",
        prompt_fn=lambda message: True,
        echo_fn=messages.append,
        which=lambda command: "/opt/homebrew/bin/codex" if command == "codex" else None,
    )

    assert messages == [
        (
            f"Configured Codex profile `meshagent-foo` in {tmp_path / 'config.toml'}. "
            "Use `codex -p meshagent-foo` to run Codex through MeshAgent."
        )
    ]


def test_maybe_configure_local_tool_integrations_skips_prompt_for_existing_project(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    wrapper_path = tmp_path / "bin" / "codex-meshagent-auth"
    config_path.write_text(
        "# BEGIN MESHAGENT MANAGED BLOCK: CODEX PROJECT project-1\n"
        "# Re-run `meshagent setup` from a different MeshAgent install"
        " if you want Codex to use a different binary.\n"
        "# MeshAgent project: Foo (project-1)\n"
        "[model_providers.meshagent-foo]\n"
        'name = "MeshAgent"\n'
        'base_url = "https://api.meshagent.life/openai/v1"\n'
        'http_headers = {"Meshagent-Project-Id"="project-1"}\n'
        "\n"
        "[model_providers.meshagent-foo.auth]\n"
        f'command = "{wrapper_path}"\n'
        "timeout_ms = 10000\n"
        "refresh_interval_ms = 240000\n"
        "\n"
        "[profiles.meshagent-foo]\n"
        'model_provider = "meshagent-foo"\n'
        'model = "gpt-5.4"\n'
        "# END MESHAGENT MANAGED BLOCK: CODEX PROJECT project-1\n"
    )
    messages: list[str] = []

    tool_integrations.maybe_configure_local_tool_integrations(
        project_id="project-1",
        project_name="Foo",
        prompt_fn=lambda message: (_ for _ in ()).throw(
            AssertionError("prompt should not be shown for existing project profile")
        ),
        echo_fn=messages.append,
        which=lambda command: "/opt/homebrew/bin/codex" if command == "codex" else None,
        config_path=config_path,
        auth_wrapper_path=wrapper_path,
    )

    assert messages == [
        (
            f"Codex profile `meshagent-foo` is already configured in {config_path}. "
            "Use `codex -p meshagent-foo` to run Codex through MeshAgent."
        )
    ]
