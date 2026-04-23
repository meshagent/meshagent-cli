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
from typing import Callable, Protocol, Sequence

import click

from meshagent.cli.local_settings import (
    get_active_project,
    resolve_api_url,
)

CODEX_DEFAULT_MODEL = "gpt-5.4"
CODEX_DEFAULT_PROFILE_ID = "meshagent"
CODEX_AUTH_TIMEOUT_MS = 10_000
CODEX_AUTH_REFRESH_INTERVAL_MS = 300_000
CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
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


@dataclass(frozen=True, slots=True)
class ClaudeIntegrationResult:
    settings_path: Path
    changed: bool


@dataclass(frozen=True, slots=True)
class CodexProfileDetails:
    profile_id: str
    provider_id: str
    project_id: str | None
    base_url: str | None
    model: str | None
    is_meshagent: bool


@dataclass(frozen=True, slots=True)
class ClaudeIntegrationStatus:
    configured: bool
    project_id: str | None
    api_url: str | None


class CodexProfileConflictError(ValueError):
    def __init__(
        self,
        *,
        profile_id: str,
        project_id: str | None,
        config_path: Path,
    ) -> None:
        project_label = "another MeshAgent project"
        if project_id is not None and project_id.strip() != "":
            project_label = f"MeshAgent project `{project_id.strip()}`"

        super().__init__(
            f"Codex profile `{profile_id}` is already configured for {project_label} "
            f"in {config_path}."
        )
        self.profile_id = profile_id
        self.project_id = project_id
        self.config_path = config_path


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    command: str
    args: tuple[str, ...]

    def shell_command(self) -> str:
        return shlex.join([self.command, *self.args])


type TomlTable = dict[str, object]
type JsonObject = dict[str, object]


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


def _meshagent_project_id_from_provider(provider: TomlTable) -> str | None:
    http_headers = _toml_table(provider.get("http_headers"))
    if http_headers is None:
        return None

    for header_name in ("Meshagent-Project-Id", "MeshAgent-Project-Id"):
        resolved_project_id = _toml_str(http_headers.get(header_name))
        if resolved_project_id is not None:
            return resolved_project_id

    return None


def _provider_looks_like_meshagent(provider: TomlTable) -> bool:
    return (
        _meshagent_project_id_from_provider(provider) is not None
        and _toml_str(provider.get("base_url")) is not None
    )


def _codex_profile_details(
    config: TomlTable,
    *,
    profile_id: str,
) -> CodexProfileDetails | None:
    profiles = _codex_root_table(config, name="profiles")
    profile = _toml_table(profiles.get(profile_id))
    if profile is None:
        return None

    provider_id = _toml_str(profile.get("model_provider"))
    if provider_id is None:
        return None

    model_providers = _codex_root_table(config, name="model_providers")
    provider = _toml_table(model_providers.get(provider_id))
    if provider is None:
        return None

    return CodexProfileDetails(
        profile_id=profile_id,
        provider_id=provider_id,
        project_id=_meshagent_project_id_from_provider(provider),
        base_url=_toml_str(provider.get("base_url")),
        model=_toml_str(profile.get("model")),
        is_meshagent=_provider_looks_like_meshagent(provider),
    )


def _list_meshagent_codex_profiles_from_config(
    config: TomlTable,
) -> list[CodexProfileDetails]:
    profiles = _codex_root_table(config, name="profiles")
    existing_profiles: list[CodexProfileDetails] = []

    for profile_id in profiles:
        details = _codex_profile_details(config, profile_id=profile_id)
        if details is None or not details.is_meshagent:
            continue
        existing_profiles.append(details)

    existing_profiles.sort(
        key=lambda profile: _codex_profile_sort_key(profile.profile_id)
    )
    return existing_profiles


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

    return _meshagent_project_id_from_provider(provider) == project_id


def _codex_profile_sort_key(profile_id: str) -> tuple[int, str]:
    return (0 if profile_id == CODEX_DEFAULT_PROFILE_ID else 1, profile_id)


def list_meshagent_codex_profiles(
    *,
    config_path: Path = CODEX_CONFIG_PATH,
) -> list[CodexProfileDetails]:
    return _list_meshagent_codex_profiles_from_config(
        _parse_codex_config(
            _read_codex_config(config_path),
            config_path=config_path,
        )
    )


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
    provider_base_url = _codex_provider_base_url(api_url=api_url)

    return [
        profile.profile_id
        for profile in list_meshagent_codex_profiles(config_path=config_path)
        if profile.project_id == resolved_project_id
        and profile.base_url is not None
        and profile.base_url.rstrip("/") == provider_base_url.rstrip("/")
    ]


def has_codex_cli(
    which: Callable[[str], str | None] = shutil.which,
) -> bool:
    return which("codex") is not None


def has_claude_code_cli(
    which: Callable[[str], str | None] = shutil.which,
) -> bool:
    return which("claude") is not None


def resolve_current_meshagent_executable(
    *,
    argv: Sequence[str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    resolved_argv = argv if argv is not None else sys.argv
    argv0 = resolved_argv[0].strip() if len(resolved_argv) > 0 else ""
    if argv0 == "":
        return None

    resolved_path: str | None
    if "/" in argv0:
        resolved_path = argv0
    else:
        resolved_path = which(argv0)

    if resolved_path is None:
        return None

    candidate = resolved_path.strip()
    if candidate == "":
        return None

    try:
        resolved_candidate = Path(candidate).expanduser().resolve()
    except OSError:
        return None

    if (
        not resolved_candidate.exists()
        or resolved_candidate.stem != "meshagent"
        or not os.access(resolved_candidate, os.X_OK)
    ):
        return None

    return str(resolved_candidate)


def _resolve_meshagent_auth_invocation(
    *,
    meshagent_executable: str | None = None,
    prefer_bare_meshagent_command: bool = False,
) -> CommandInvocation:
    if meshagent_executable is not None and meshagent_executable.strip() != "":
        return CommandInvocation(
            command=meshagent_executable.strip(),
            args=("auth", "token"),
        )

    if prefer_bare_meshagent_command and shutil.which("meshagent") is not None:
        return CommandInvocation(
            command="meshagent",
            args=("auth", "token"),
        )

    candidates: list[Path] = []
    current_meshagent_executable = resolve_current_meshagent_executable()
    if current_meshagent_executable is not None:
        candidates.append(Path(current_meshagent_executable))

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
            return CommandInvocation(
                command=str(resolved_candidate),
                args=("auth", "token"),
            )

    return CommandInvocation(
        command=sys.executable,
        args=("-m", "meshagent.cli.cli", "auth", "token"),
    )


def _resolve_meshagent_auth_command(
    *,
    meshagent_executable: str | None = None,
    prefer_bare_meshagent_command: bool = False,
) -> str:
    return _resolve_meshagent_auth_invocation(
        meshagent_executable=meshagent_executable,
        prefer_bare_meshagent_command=prefer_bare_meshagent_command,
    ).shell_command()


def _codex_profile_block(
    *,
    project_id: str,
    profile_id: str,
    api_url: str | None = None,
    meshagent_executable: str | None = None,
    model: str = CODEX_DEFAULT_MODEL,
) -> str:
    provider_base_url = _codex_provider_base_url(api_url=api_url)
    auth_invocation = _resolve_meshagent_auth_invocation(
        meshagent_executable=meshagent_executable,
        prefer_bare_meshagent_command=True,
    )

    lines = [
        f"[model_providers.{profile_id}]",
        f"name = {json.dumps('MeshAgent')}",
        f"base_url = {json.dumps(provider_base_url)}",
        f'http_headers = {{"Meshagent-Project-Id"={json.dumps(project_id)}}}',
        "",
        f"[model_providers.{profile_id}.auth]",
        f"command = {json.dumps(auth_invocation.command)}",
        f"args = {json.dumps(list(auth_invocation.args))}",
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


def _write_codex_config(config_path: Path, content: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content)


def _toml_table_name(line: str) -> str | None:
    stripped = line.lstrip()
    if stripped == "" or stripped.startswith("#"):
        return None

    content = stripped.split("#", 1)[0].rstrip()
    if not content.startswith("[") or not content.endswith("]"):
        return None

    table_name = content[1:-1].strip()
    if table_name == "":
        return None
    return table_name


def _remove_toml_table_prefix(existing: str, *, prefix: str) -> str:
    updated_lines: list[str] = []
    skipping_prefix = False

    for line in existing.splitlines(keepends=True):
        table_name = _toml_table_name(line)
        if table_name is not None:
            skipping_prefix = table_name == prefix or table_name.startswith(
                f"{prefix}."
            )
            if skipping_prefix:
                continue

        if skipping_prefix:
            continue
        updated_lines.append(line)

    return "".join(updated_lines)


def _normalize_toml_spacing(content: str) -> str:
    normalized_lines: list[str] = []
    previous_blank = False

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if line == "":
            if len(normalized_lines) == 0 or previous_blank:
                continue
            previous_blank = True
            normalized_lines.append("")
            continue

        previous_blank = False
        normalized_lines.append(line)

    while len(normalized_lines) > 0 and normalized_lines[-1] == "":
        normalized_lines.pop()

    if len(normalized_lines) == 0:
        return ""

    return "\n".join(normalized_lines) + "\n"


def _remove_codex_profile_tables(
    existing: str,
    *,
    config: TomlTable,
    details: CodexProfileDetails,
    clear_default_profile: bool,
) -> str:
    updated = _remove_toml_table_prefix(
        existing,
        prefix=f"profiles.{details.profile_id}",
    )

    remaining_provider_references = [
        profile
        for profile in _list_meshagent_codex_profiles_from_config(config)
        if profile.profile_id != details.profile_id
        and profile.provider_id == details.provider_id
    ]
    if len(remaining_provider_references) == 0:
        updated = _remove_toml_table_prefix(
            updated,
            prefix=f"model_providers.{details.provider_id}",
        )

    if clear_default_profile:
        updated = _upsert_root_toml_string_setting(
            updated,
            key="profile",
            value=None,
        )

    return updated


def configure_codex_integration(
    *,
    profile_id: str,
    project_id: str | None = None,
    project_name: str | None = None,
    api_url: str | None = None,
    meshagent_executable: str | None = None,
    model: str = CODEX_DEFAULT_MODEL,
    config_path: Path = CODEX_CONFIG_PATH,
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
        existing_details = _codex_profile_details(
            _parse_codex_config(existing, config_path=config_path),
            profile_id=resolved_profile_id,
        )
        if existing_details is not None and existing_details.is_meshagent:
            raise CodexProfileConflictError(
                profile_id=resolved_profile_id,
                project_id=existing_details.project_id,
                config_path=config_path,
            )
        raise ValueError(
            f"Codex profile name `{resolved_profile_id}` is already in use in "
            f"{', '.join(conflicts)}."
        )

    profile_block = _codex_profile_block(
        project_id=resolved_project_id,
        profile_id=resolved_profile_id,
        api_url=api_url,
        meshagent_executable=meshagent_executable,
        model=model,
    )
    config_path.write_text(_append_codex_profile(existing, profile_block=profile_block))

    return CodexIntegrationResult(
        config_path=config_path,
        provider_id=resolved_profile_id,
        profile_id=resolved_profile_id,
        changed=True,
    )


def replace_codex_integration(
    *,
    profile_id: str,
    project_id: str | None = None,
    api_url: str | None = None,
    meshagent_executable: str | None = None,
    model: str | None = None,
    config_path: Path = CODEX_CONFIG_PATH,
) -> CodexIntegrationResult:
    resolved_project_id = _resolve_active_project_id(
        project_id=project_id,
        missing_project_message=(
            "An active MeshAgent project is required to configure Codex."
        ),
    )
    resolved_profile_id = _validate_profile_identifier(profile_id)
    existing = _read_codex_config(config_path)
    config = _parse_codex_config(existing, config_path=config_path)
    existing_details = _codex_profile_details(config, profile_id=resolved_profile_id)
    if existing_details is None:
        raise ValueError(
            f"Codex profile `{resolved_profile_id}` is not defined in {config_path}."
        )
    if not existing_details.is_meshagent:
        raise ValueError(
            f"Codex profile `{resolved_profile_id}` is not managed by MeshAgent in "
            f"{config_path}."
        )

    updated = _remove_codex_profile_tables(
        existing,
        config=config,
        details=existing_details,
        clear_default_profile=False,
    )
    updated = _normalize_toml_spacing(updated)
    if not _codex_profile_is_available(
        updated,
        profile_id=resolved_profile_id,
        config_path=config_path,
    ):
        raise ValueError(
            f"Codex profile name `{resolved_profile_id}` is still in use in "
            f"{config_path}."
        )

    rewritten = _append_codex_profile(
        updated,
        profile_block=_codex_profile_block(
            project_id=resolved_project_id,
            profile_id=resolved_profile_id,
            api_url=api_url,
            meshagent_executable=meshagent_executable,
            model=model or existing_details.model or CODEX_DEFAULT_MODEL,
        ),
    )
    _write_codex_config(config_path, rewritten)

    return CodexIntegrationResult(
        config_path=config_path,
        provider_id=resolved_profile_id,
        profile_id=resolved_profile_id,
        changed=rewritten != existing,
    )


def remove_codex_integration(
    *,
    profile_id: str,
    config_path: Path = CODEX_CONFIG_PATH,
) -> bool:
    resolved_profile_id = _validate_profile_identifier(profile_id)
    existing = _read_codex_config(config_path)
    if existing.strip() == "":
        return False

    config = _parse_codex_config(existing, config_path=config_path)
    existing_details = _codex_profile_details(config, profile_id=resolved_profile_id)
    if existing_details is None:
        return False
    if not existing_details.is_meshagent:
        raise ValueError(
            f"Codex profile `{resolved_profile_id}` is not managed by MeshAgent in "
            f"{config_path}."
        )

    updated = _remove_codex_profile_tables(
        existing,
        config=config,
        details=existing_details,
        clear_default_profile=_toml_str(config.get("profile")) == resolved_profile_id,
    )
    updated = _normalize_toml_spacing(updated)
    if updated == existing:
        return False

    _write_codex_config(config_path, updated)
    return True


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
            "Codex detected. Add a MeshAgent proxy profile to ~/.codex/config.toml "
            "so Codex uses your MeshAgent account by default?"
        ),
        default=True,
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


def find_current_codex_default_profile(
    *,
    project_id: str | None = None,
    api_url: str | None = None,
    config_path: Path = CODEX_CONFIG_PATH,
) -> str | None:
    resolved_project_id = _resolve_active_project_id(
        project_id=project_id,
        missing_project_message=(
            "An active MeshAgent project is required to inspect the Codex default profile."
        ),
    )

    config = _parse_codex_config(
        _read_codex_config(config_path),
        config_path=config_path,
    )
    default_profile_id = _toml_str(config.get("profile"))
    if default_profile_id is None:
        return None

    profiles = _codex_root_table(config, name="profiles")
    model_providers = _codex_root_table(config, name="model_providers")
    profile = _toml_table(profiles.get(default_profile_id))
    if profile is None:
        return None

    model_provider_id = _toml_str(profile.get("model_provider"))
    if model_provider_id is None:
        return None

    provider = _toml_table(model_providers.get(model_provider_id))
    if provider is None:
        return None

    if not _provider_matches_meshagent_project(
        provider,
        project_id=resolved_project_id,
        provider_base_url=_codex_provider_base_url(api_url=api_url),
    ):
        return None

    return default_profile_id


def _is_toml_table_header_line(line: str) -> bool:
    stripped = line.lstrip()
    if stripped == "" or stripped.startswith("#"):
        return False

    content = stripped.split("#", 1)[0].rstrip()
    return content.startswith("[") and content.endswith("]")


def _is_root_toml_key_assignment(line: str, *, key: str) -> bool:
    stripped = line.lstrip()
    if stripped == "" or stripped.startswith("#"):
        return False

    content = stripped.split("#", 1)[0].rstrip()
    return re.match(rf"^{re.escape(key)}\s*=", content) is not None


def _upsert_root_toml_string_setting(
    existing: str,
    *,
    key: str,
    value: str | None,
) -> str:
    lines = existing.splitlines(keepends=True)
    updated_lines: list[str] = []
    in_root_section = True
    replaced_existing = False

    for line in lines:
        if in_root_section and _is_toml_table_header_line(line):
            if value is not None and not replaced_existing:
                updated_lines.append(f"{key} = {json.dumps(value)}\n")
                replaced_existing = True
            in_root_section = False

        if in_root_section and _is_root_toml_key_assignment(line, key=key):
            if value is not None and not replaced_existing:
                updated_lines.append(f"{key} = {json.dumps(value)}\n")
                replaced_existing = True
            continue

        updated_lines.append(line)
    if in_root_section and value is not None and not replaced_existing:
        updated_lines.append(f"{key} = {json.dumps(value)}\n")

    return "".join(updated_lines)


def set_codex_default_profile(
    *,
    profile_id: str | None,
    config_path: Path = CODEX_CONFIG_PATH,
) -> bool:
    existing = _read_codex_config(config_path)
    if profile_id is not None:
        resolved_profile_id = _validate_profile_identifier(profile_id)
        config = _parse_codex_config(existing, config_path=config_path)
        profiles = _codex_root_table(config, name="profiles")
        if resolved_profile_id not in profiles:
            raise ValueError(
                f"Codex profile `{resolved_profile_id}` is not defined in {config_path}."
            )
    else:
        resolved_profile_id = None

    updated = _upsert_root_toml_string_setting(
        existing,
        key="profile",
        value=resolved_profile_id,
    )
    if updated == existing:
        return False

    if updated == "":
        if config_path.exists():
            config_path.write_text("")
        return True

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(updated)
    return True


def clear_codex_default_profile_if_meshagent_project(
    *,
    project_id: str | None = None,
    api_url: str | None = None,
    config_path: Path = CODEX_CONFIG_PATH,
) -> bool:
    current_default_profile_id = find_current_codex_default_profile(
        project_id=project_id,
        api_url=api_url,
        config_path=config_path,
    )
    if current_default_profile_id is None:
        return False

    return set_codex_default_profile(profile_id=None, config_path=config_path)


def _toml_inline_string_map(values: dict[str, str]) -> str:
    items = ", ".join(
        f"{json.dumps(key)}={json.dumps(value)}" for key, value in values.items()
    )
    return f"{{{items}}}"


def build_codex_launch_command(
    *,
    project_id: str | None = None,
    api_url: str | None = None,
    extra_args: Sequence[str] = (),
    meshagent_executable: str | None = None,
    codex_executable: str | None = None,
    profile_id: str = CODEX_DEFAULT_PROFILE_ID,
    model: str = CODEX_DEFAULT_MODEL,
) -> list[str]:
    resolved_project_id = _resolve_active_project_id(
        project_id=project_id,
        missing_project_message=(
            "An active MeshAgent project is required to launch Codex."
        ),
    )
    resolved_codex_executable = codex_executable or shutil.which("codex")
    if resolved_codex_executable is None:
        raise RuntimeError("Codex is not installed.")

    normalized_profile_id = _validate_profile_identifier(profile_id)
    normalized_extra_args = list(extra_args)

    for index, arg in enumerate(normalized_extra_args):
        if arg in ("-p", "--profile") or arg.startswith("--profile="):
            raise RuntimeError(
                "`meshagent launch codex` manages the Codex profile automatically; "
                "remove `--profile` from the forwarded Codex arguments."
            )
        if arg in ("-c", "--config") and index + 1 < len(normalized_extra_args):
            next_arg = normalized_extra_args[index + 1].strip()
            if next_arg.startswith("profile="):
                raise RuntimeError(
                    "`meshagent launch codex` manages the Codex profile automatically; "
                    "remove profile overrides from the forwarded Codex arguments."
                )
        if arg.startswith("--config="):
            config_override = arg.split("=", 1)[1].strip()
            if config_override.startswith("profile="):
                raise RuntimeError(
                    "`meshagent launch codex` manages the Codex profile automatically; "
                    "remove profile overrides from the forwarded Codex arguments."
                )

    provider_base_url = _codex_provider_base_url(api_url=api_url)
    auth_invocation = _resolve_meshagent_auth_invocation(
        meshagent_executable=meshagent_executable
    )
    http_headers = _toml_inline_string_map(
        {"Meshagent-Project-Id": resolved_project_id}
    )

    return [
        resolved_codex_executable,
        "-c",
        f"model_providers.{normalized_profile_id}.name={json.dumps('MeshAgent')}",
        "-c",
        f"model_providers.{normalized_profile_id}.base_url={json.dumps(provider_base_url)}",
        "-c",
        f"model_providers.{normalized_profile_id}.http_headers={http_headers}",
        "-c",
        (
            f"model_providers.{normalized_profile_id}.auth.command="
            f"{json.dumps(auth_invocation.command)}"
        ),
        "-c",
        (
            f"model_providers.{normalized_profile_id}.auth.args="
            f"{json.dumps(list(auth_invocation.args))}"
        ),
        "-c",
        (
            f"model_providers.{normalized_profile_id}.auth.timeout_ms="
            f"{CODEX_AUTH_TIMEOUT_MS}"
        ),
        "-c",
        (
            f"model_providers.{normalized_profile_id}.auth.refresh_interval_ms="
            f"{CODEX_AUTH_REFRESH_INTERVAL_MS}"
        ),
        "-c",
        (
            f"profiles.{normalized_profile_id}.model_provider="
            f"{json.dumps(normalized_profile_id)}"
        ),
        "-c",
        f"profiles.{normalized_profile_id}.model={json.dumps(model)}",
        "-p",
        normalized_profile_id,
        *normalized_extra_args,
    ]


def launch_codex(
    *,
    project_id: str | None = None,
    api_url: str | None = None,
    extra_args: Sequence[str] = (),
    meshagent_executable: str | None = None,
    codex_executable: str | None = None,
    profile_id: str = CODEX_DEFAULT_PROFILE_ID,
    model: str = CODEX_DEFAULT_MODEL,
    command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    command = build_codex_launch_command(
        project_id=project_id,
        api_url=api_url,
        extra_args=extra_args,
        meshagent_executable=meshagent_executable,
        codex_executable=codex_executable,
        profile_id=profile_id,
        model=model,
    )
    result = command_runner(command, check=False)
    return result.returncode


def _claude_code_base_url(*, api_url: str | None = None) -> str:
    return f"{resolve_api_url(api_url=api_url)}/anthropic"


def _default_claude_code_settings_path() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir is not None and config_dir.strip() != "":
        return Path(config_dir.strip()).expanduser() / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def _read_claude_code_settings(settings_path: Path) -> str:
    if settings_path.exists():
        return settings_path.read_text()
    return ""


def _parse_claude_code_settings(
    existing: str,
    *,
    settings_path: Path,
) -> JsonObject:
    if existing.strip() == "":
        return {}

    try:
        parsed = json.loads(existing)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Unable to parse Claude settings at {settings_path}: {exc}"
        ) from exc

    resolved_settings = _toml_table(parsed)
    if resolved_settings is None:
        raise RuntimeError(
            f"Unable to parse Claude settings at {settings_path}: top-level object expected."
        )
    return resolved_settings


def _claude_settings_env(
    settings: JsonObject,
    *,
    settings_path: Path,
) -> JsonObject:
    existing_env = settings.get("env")
    if existing_env is None:
        return {}

    env = _toml_table(existing_env)
    if env is None:
        raise RuntimeError(
            f"Unable to parse Claude settings at {settings_path}: `env` must be an object."
        )
    return env


def _is_meshagent_auth_command(command: str | None) -> bool:
    if command is None or command.strip() == "":
        return False

    try:
        parts = shlex.split(command)
    except ValueError:
        return False

    if len(parts) == 3 and Path(parts[0]).name == "meshagent":
        return parts[1:] == ["auth", "token"]

    if len(parts) >= 5 and parts[-4:] == ["-m", "meshagent.cli.cli", "auth", "token"]:
        return Path(parts[0]).name.startswith("python")

    return False


def inspect_claude_code_integration(
    *,
    settings_path: Path | None = None,
) -> ClaudeIntegrationStatus:
    resolved_settings_path = settings_path or _default_claude_code_settings_path()
    settings = _parse_claude_code_settings(
        _read_claude_code_settings(resolved_settings_path),
        settings_path=resolved_settings_path,
    )
    env = _claude_settings_env(settings, settings_path=resolved_settings_path)

    configured = _is_meshagent_auth_command(_toml_str(settings.get("apiKeyHelper")))
    for key in (
        "MESHAGENT_API_URL",
        "MESHAGENT_PROJECT_ID",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
    ):
        if _toml_str(env.get(key)) is not None:
            configured = True
            break

    return ClaudeIntegrationStatus(
        configured=configured,
        project_id=_toml_str(env.get("MESHAGENT_PROJECT_ID")),
        api_url=_toml_str(env.get("MESHAGENT_API_URL")),
    )


def clear_claude_code_integration(
    *,
    settings_path: Path | None = None,
) -> bool:
    resolved_settings_path = settings_path or _default_claude_code_settings_path()
    existing = _read_claude_code_settings(resolved_settings_path)
    settings = _parse_claude_code_settings(
        existing,
        settings_path=resolved_settings_path,
    )
    env = _claude_settings_env(settings, settings_path=resolved_settings_path)
    changed = False

    for key in (
        "MESHAGENT_API_URL",
        "MESHAGENT_PROJECT_ID",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
    ):
        if key in env:
            env.pop(key)
            changed = True

    if changed:
        if len(env) == 0:
            settings.pop("env", None)
        else:
            settings["env"] = env

    api_key_helper = _toml_str(settings.get("apiKeyHelper"))
    if _is_meshagent_auth_command(api_key_helper):
        settings.pop("apiKeyHelper", None)
        changed = True

    if not changed:
        return False

    updated = json.dumps(settings, indent=2) + "\n"
    if updated == existing:
        return False

    resolved_settings_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_settings_path.write_text(updated)
    return True


def configure_claude_code_integration(
    *,
    project_id: str | None = None,
    api_url: str | None = None,
    meshagent_executable: str | None = None,
    settings_path: Path | None = None,
) -> ClaudeIntegrationResult:
    resolved_project_id = _resolve_active_project_id(
        project_id=project_id,
        missing_project_message=(
            "An active MeshAgent project is required to configure Claude."
        ),
    )
    resolved_settings_path = settings_path or _default_claude_code_settings_path()
    existing = _read_claude_code_settings(resolved_settings_path)
    settings = _parse_claude_code_settings(
        existing,
        settings_path=resolved_settings_path,
    )
    env = _claude_settings_env(settings, settings_path=resolved_settings_path)

    env["MESHAGENT_API_URL"] = resolve_api_url(api_url=api_url)
    env["MESHAGENT_PROJECT_ID"] = resolved_project_id
    env["ANTHROPIC_BASE_URL"] = _claude_code_base_url(api_url=api_url)
    env["ANTHROPIC_CUSTOM_HEADERS"] = (
        f"{CLAUDE_CODE_PROJECT_HEADER}: {resolved_project_id}"
    )
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)

    settings["env"] = env
    settings["apiKeyHelper"] = _resolve_meshagent_auth_command(
        meshagent_executable=meshagent_executable,
        prefer_bare_meshagent_command=True,
    )

    updated = json.dumps(settings, indent=2) + "\n"
    if updated == existing:
        return ClaudeIntegrationResult(
            settings_path=resolved_settings_path,
            changed=False,
        )

    resolved_settings_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_settings_path.write_text(updated)
    return ClaudeIntegrationResult(
        settings_path=resolved_settings_path,
        changed=True,
    )


def build_claude_code_env(
    *,
    project_id: str | None = None,
    api_url: str | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    resolved_project_id = _resolve_active_project_id(
        project_id=project_id,
        missing_project_message=(
            "An active MeshAgent project is required to launch Claude."
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
        raise RuntimeError("Claude is not installed.")

    normalized_extra_args = list(extra_args)
    if any(
        arg == "--settings" or arg.startswith("--settings=")
        for arg in normalized_extra_args
    ):
        raise RuntimeError(
            "`meshagent launch claude` manages Claude auth settings automatically; "
            "remove `--settings` from the forwarded Claude arguments."
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


def launch_claude(
    *,
    project_id: str | None = None,
    api_url: str | None = None,
    extra_args: Sequence[str] = (),
    meshagent_executable: str | None = None,
    claude_executable: str | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    base_env: dict[str, str] | None = None,
) -> int:
    return launch_claude_code(
        project_id=project_id,
        api_url=api_url,
        extra_args=extra_args,
        meshagent_executable=meshagent_executable,
        claude_executable=claude_executable,
        command_runner=command_runner,
        base_env=base_env,
    )
