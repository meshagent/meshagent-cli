from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from stat import S_IXGRP, S_IXOTH, S_IXUSR
from typing import Callable, Protocol, Sequence

import click

from meshagent.cli.local_settings import (
    SETTINGS_DIR,
    get_active_project,
    resolve_api_url,
)

CODEX_DEFAULT_MODEL = "gpt-5.4"
CODEX_DEFAULT_PROFILE_ID = "meshagent"
CODEX_AUTH_TIMEOUT_MS = 10_000
CODEX_AUTH_REFRESH_INTERVAL_MS = 240_000
CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
CODEX_AUTH_WRAPPER_DIR = SETTINGS_DIR / "bin"
CODEX_PROFILE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
CLAUDE_CODE_PROJECT_HEADER = "Meshagent-Project-Id"


class ConfirmFn(Protocol):
    def __call__(self, text: str, default: bool = False) -> bool: ...


class PromptFn(Protocol):
    def __call__(self, text: str, default: str = CODEX_DEFAULT_PROFILE_ID) -> str: ...


@dataclass(frozen=True, slots=True)
class CodexIntegrationResult:
    config_path: Path
    provider_id: str
    profile_id: str
    changed: bool


type TomlTable = dict[str, object]


def _codex_provider_base_url(*, api_url: str | None = None) -> str:
    return f"{resolve_api_url(api_url=api_url)}/openai/v1"


def _validate_profile_identifier(value: str) -> str:
    identifier = value.strip()
    if identifier == "":
        raise ValueError("Codex profile name cannot be empty.")
    if CODEX_PROFILE_IDENTIFIER_PATTERN.fullmatch(identifier) is None:
        raise ValueError(
            "Codex profile names may only include letters, numbers, hyphens, and underscores."
        )
    return identifier


def _parse_codex_config(existing: str, *, config_path: Path) -> TomlTable:
    if existing.strip() == "":
        return {}

    try:
        parsed = tomllib.loads(existing)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(
            f"Unable to parse Codex config at {config_path}: {exc}"
        ) from exc

    return parsed


def _toml_table(value: object) -> TomlTable | None:
    if not isinstance(value, dict):
        return None

    resolved_table: TomlTable = {}
    for key, child in value.items():
        if isinstance(key, str):
            resolved_table[key] = child
    return resolved_table


def _toml_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    resolved_value = value.strip()
    if resolved_value == "":
        return None
    return resolved_value


def _codex_root_table(config: TomlTable, *, name: str) -> TomlTable:
    root_table = _toml_table(config.get(name))
    if root_table is None:
        return {}
    return root_table


def _codex_profile_conflicts(
    existing: str, *, profile_id: str, config_path: Path
) -> list[str]:
    config = _parse_codex_config(existing, config_path=config_path)
    model_providers = _codex_root_table(config, name="model_providers")
    profiles = _codex_root_table(config, name="profiles")

    conflicts: list[str] = []
    if profile_id in model_providers:
        conflicts.append(f"[model_providers.{profile_id}]")
    if profile_id in profiles:
        conflicts.append(f"[profiles.{profile_id}]")
    return conflicts


def _codex_profile_is_available(
    existing: str, *, profile_id: str, config_path: Path
) -> bool:
    return (
        len(
            _codex_profile_conflicts(
                existing,
                profile_id=profile_id,
                config_path=config_path,
            )
        )
        == 0
    )


def _read_codex_config(config_path: Path) -> str:
    if config_path.exists():
        return config_path.read_text()
    return ""


def _provider_matches_meshagent_project(
    provider: TomlTable,
    *,
    project_id: str,
    provider_base_url: str,
) -> bool:
    provider_url = _toml_str(provider.get("base_url"))
    if provider_url is None or provider_url.rstrip("/") != provider_base_url.rstrip(
        "/"
    ):
        return False

    http_headers = _toml_table(provider.get("http_headers"))
    if http_headers is None:
        return False

    for header_name in ("Meshagent-Project-Id", "MeshAgent-Project-Id"):
        resolved_project_id = _toml_str(http_headers.get(header_name))
        if resolved_project_id == project_id:
            return True

    return False


def _codex_profile_sort_key(profile_id: str) -> tuple[int, str]:
    return (0 if profile_id == CODEX_DEFAULT_PROFILE_ID else 1, profile_id)


def find_existing_codex_profiles(
    *,
    project_id: str | None = None,
    api_url: str | None = None,
    config_path: Path = CODEX_CONFIG_PATH,
) -> list[str]:
    resolved_project_id = (
        project_id.strip()
        if project_id is not None and project_id.strip() != ""
        else get_active_project()
    )
    if resolved_project_id is None or resolved_project_id.strip() == "":
        raise RuntimeError(
            "An active MeshAgent project is required to inspect Codex profiles."
        )
    resolved_project_id = resolved_project_id.strip()

    config = _parse_codex_config(
        _read_codex_config(config_path),
        config_path=config_path,
    )
    model_providers = _codex_root_table(config, name="model_providers")
    profiles = _codex_root_table(config, name="profiles")
    provider_base_url = _codex_provider_base_url(api_url=api_url)

    matching_provider_ids: set[str] = set()
    for provider_id, provider_value in model_providers.items():
        provider = _toml_table(provider_value)
        if provider is None:
            continue
        if _provider_matches_meshagent_project(
            provider,
            project_id=resolved_project_id,
            provider_base_url=provider_base_url,
        ):
            matching_provider_ids.add(provider_id)

    if len(matching_provider_ids) == 0:
        return []

    matching_profile_ids: list[str] = []
    for profile_id, profile_value in profiles.items():
        profile = _toml_table(profile_value)
        if profile is None:
            continue
        model_provider = _toml_str(profile.get("model_provider"))
        if model_provider in matching_provider_ids:
            matching_profile_ids.append(profile_id)

    return sorted(matching_profile_ids, key=_codex_profile_sort_key)


def has_codex_cli(
    which: Callable[[str], str | None] = shutil.which,
) -> bool:
    return which("codex") is not None


def has_claude_code_cli(
    which: Callable[[str], str | None] = shutil.which,
) -> bool:
    return which("claude") is not None


def _resolve_meshagent_auth_command(
    *,
    meshagent_executable: str | None = None,
) -> str:
    if meshagent_executable is not None and meshagent_executable.strip() != "":
        return shlex.join([meshagent_executable.strip(), "auth", "token"])

    candidates: list[Path] = []
    argv0 = sys.argv[0].strip() if sys.argv[0] else ""
    if argv0 != "":
        if "/" in argv0:
            candidates.append(Path(argv0).expanduser())
        else:
            resolved_argv0 = shutil.which(argv0)
            if resolved_argv0 is not None:
                candidates.append(Path(resolved_argv0))

    resolved_meshagent = shutil.which("meshagent")
    if resolved_meshagent is not None:
        candidates.append(Path(resolved_meshagent))

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved_candidate = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved_candidate in seen:
            continue
        seen.add(resolved_candidate)
        if resolved_candidate.exists() and os.access(resolved_candidate, os.X_OK):
            return shlex.join([str(resolved_candidate), "auth", "token"])

    return shlex.join([sys.executable, "-m", "meshagent.cli.cli", "auth", "token"])


def _default_codex_auth_wrapper_path(*, profile_id: str) -> Path:
    return CODEX_AUTH_WRAPPER_DIR / f"codex-meshagent-auth-{profile_id}"


def _write_codex_auth_wrapper(
    *,
    profile_id: str,
    meshagent_executable: str | None = None,
    auth_wrapper_path: Path | None = None,
) -> Path:
    resolved_wrapper_path = auth_wrapper_path or _default_codex_auth_wrapper_path(
        profile_id=profile_id
    )
    resolved_wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    command = _resolve_meshagent_auth_command(meshagent_executable=meshagent_executable)
    resolved_wrapper_path.write_text(f"#!/bin/sh\nexec {command}\n")
    resolved_wrapper_path.chmod(
        resolved_wrapper_path.stat().st_mode | S_IXUSR | S_IXGRP | S_IXOTH
    )
    return resolved_wrapper_path


def _codex_profile_block(
    *,
    project_id: str,
    profile_id: str,
    api_url: str | None = None,
    meshagent_executable: str | None = None,
    auth_wrapper_path: Path | None = None,
    model: str = CODEX_DEFAULT_MODEL,
) -> str:
    provider_base_url = _codex_provider_base_url(api_url=api_url)
    wrapper_path = _write_codex_auth_wrapper(
        profile_id=profile_id,
        meshagent_executable=meshagent_executable,
        auth_wrapper_path=auth_wrapper_path,
    )

    lines = [
        f"[model_providers.{profile_id}]",
        f"name = {json.dumps('MeshAgent')}",
        f"base_url = {json.dumps(provider_base_url)}",
        f'http_headers = {{"Meshagent-Project-Id"={json.dumps(project_id)}}}',
        "",
        f"[model_providers.{profile_id}.auth]",
        f"command = {json.dumps(str(wrapper_path))}",
        f"timeout_ms = {CODEX_AUTH_TIMEOUT_MS}",
        f"refresh_interval_ms = {CODEX_AUTH_REFRESH_INTERVAL_MS}",
        "",
        f"[profiles.{profile_id}]",
        f"model_provider = {json.dumps(profile_id)}",
        f"model = {json.dumps(model)}",
        "",
    ]
    return "\n".join(lines)


def _append_codex_profile(existing: str, *, profile_block: str) -> str:
    existing_content = existing.rstrip()
    if existing_content == "":
        return profile_block
    return f"{existing_content}\n\n{profile_block}"


def configure_codex_integration(
    *,
    profile_id: str,
    project_id: str | None = None,
    project_name: str | None = None,
    api_url: str | None = None,
    meshagent_executable: str | None = None,
    model: str = CODEX_DEFAULT_MODEL,
    config_path: Path = CODEX_CONFIG_PATH,
    auth_wrapper_path: Path | None = None,
) -> CodexIntegrationResult:
    del project_name

    resolved_project_id = (
        project_id.strip()
        if project_id is not None and project_id.strip() != ""
        else get_active_project()
    )
    if resolved_project_id is None or resolved_project_id.strip() == "":
        raise RuntimeError(
            "An active MeshAgent project is required to configure Codex."
        )
    resolved_project_id = resolved_project_id.strip()

    resolved_profile_id = _validate_profile_identifier(profile_id)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_codex_config(config_path)
    conflicts = _codex_profile_conflicts(
        existing,
        profile_id=resolved_profile_id,
        config_path=config_path,
    )
    if len(conflicts) > 0:
        raise ValueError(
            f"Codex profile name `{resolved_profile_id}` is already in use in "
            f"{', '.join(conflicts)}."
        )

    profile_block = _codex_profile_block(
        project_id=resolved_project_id,
        profile_id=resolved_profile_id,
        api_url=api_url,
        meshagent_executable=meshagent_executable,
        auth_wrapper_path=auth_wrapper_path,
        model=model,
    )
    config_path.write_text(_append_codex_profile(existing, profile_block=profile_block))

    return CodexIntegrationResult(
        config_path=config_path,
        provider_id=resolved_profile_id,
        profile_id=resolved_profile_id,
        changed=True,
    )


def maybe_configure_local_tool_integrations(
    *,
    project_id: str | None = None,
    project_name: str | None = None,
    api_url: str | None = None,
    meshagent_executable: str | None = None,
    model: str = CODEX_DEFAULT_MODEL,
    confirm_fn: ConfirmFn = click.confirm,
    prompt_fn: PromptFn = click.prompt,
    echo_fn: Callable[[str], None] = click.echo,
    which: Callable[[str], str | None] = shutil.which,
    config_path: Path = CODEX_CONFIG_PATH,
    auth_wrapper_path: Path | None = None,
) -> None:
    del project_name

    if which("codex") is None:
        return

    resolved_project_id = (
        project_id.strip()
        if project_id is not None and project_id.strip() != ""
        else get_active_project()
    )
    if resolved_project_id is None or resolved_project_id.strip() == "":
        raise RuntimeError(
            "An active MeshAgent project is required to configure Codex."
        )
    resolved_project_id = resolved_project_id.strip()

    if not confirm_fn(
        (
            "Codex detected. Add a profile to ~/.codex/config.toml so Codex can use "
            "your MeshAgent account for access?"
        ),
        default=False,
    ):
        return

    while True:
        entered_profile_id = prompt_fn(
            "Codex profile name",
            default=CODEX_DEFAULT_PROFILE_ID,
        )
        try:
            resolved_profile_id = _validate_profile_identifier(entered_profile_id)
        except ValueError as exc:
            echo_fn(str(exc))
            continue

        existing = _read_codex_config(config_path)
        if _codex_profile_is_available(
            existing,
            profile_id=resolved_profile_id,
            config_path=config_path,
        ):
            break

        echo_fn(
            f"Codex profile `{resolved_profile_id}` is already in use in "
            f"{config_path}. Choose a different name."
        )

    result = configure_codex_integration(
        profile_id=resolved_profile_id,
        project_id=resolved_project_id,
        api_url=api_url,
        meshagent_executable=meshagent_executable,
        model=model,
        config_path=config_path,
        auth_wrapper_path=auth_wrapper_path,
    )
    echo_fn(
        f"Configured Codex profile `{result.profile_id}` in {result.config_path}. "
        f"Use `codex -p {result.profile_id}` to run Codex through MeshAgent."
    )


def _resolve_active_project_id(
    *,
    project_id: str | None,
    missing_project_message: str,
) -> str:
    resolved_project_id = (
        project_id.strip()
        if project_id is not None and project_id.strip() != ""
        else get_active_project()
    )
    if resolved_project_id is None or resolved_project_id.strip() == "":
        raise RuntimeError(missing_project_message)
    return resolved_project_id.strip()


def _claude_code_base_url(*, api_url: str | None = None) -> str:
    return f"{resolve_api_url(api_url=api_url)}/anthropic"


def build_claude_code_env(
    *,
    project_id: str | None = None,
    api_url: str | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    resolved_project_id = _resolve_active_project_id(
        project_id=project_id,
        missing_project_message=(
            "An active MeshAgent project is required to launch Claude Code."
        ),
    )
    env = dict(base_env) if base_env is not None else os.environ.copy()
    env["MESHAGENT_API_URL"] = resolve_api_url(api_url=api_url)
    env["MESHAGENT_PROJECT_ID"] = resolved_project_id
    env["ANTHROPIC_BASE_URL"] = _claude_code_base_url(api_url=api_url)
    env["ANTHROPIC_CUSTOM_HEADERS"] = (
        f"{CLAUDE_CODE_PROJECT_HEADER}: {resolved_project_id}"
    )
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def build_claude_code_command(
    *,
    extra_args: Sequence[str] = (),
    meshagent_executable: str | None = None,
    claude_executable: str | None = None,
) -> list[str]:
    resolved_claude_executable = claude_executable or shutil.which("claude")
    if resolved_claude_executable is None:
        raise RuntimeError("Claude Code is not installed.")

    normalized_extra_args = list(extra_args)
    if any(
        arg == "--settings" or arg.startswith("--settings=")
        for arg in normalized_extra_args
    ):
        raise RuntimeError(
            "`meshagent claude-code` manages Claude Code auth settings automatically; "
            "remove `--settings` from the forwarded Claude Code arguments."
        )

    settings_payload = json.dumps(
        {
            "apiKeyHelper": _resolve_meshagent_auth_command(
                meshagent_executable=meshagent_executable
            )
        }
    )
    return [
        resolved_claude_executable,
        "--settings",
        settings_payload,
        *normalized_extra_args,
    ]


def launch_claude_code(
    *,
    project_id: str | None = None,
    api_url: str | None = None,
    extra_args: Sequence[str] = (),
    meshagent_executable: str | None = None,
    claude_executable: str | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    base_env: dict[str, str] | None = None,
) -> int:
    env = build_claude_code_env(
        project_id=project_id,
        api_url=api_url,
        base_env=base_env,
    )
    command = build_claude_code_command(
        extra_args=extra_args,
        meshagent_executable=meshagent_executable,
        claude_executable=claude_executable,
    )
    result = command_runner(command, env=env, check=False)
    return result.returncode
