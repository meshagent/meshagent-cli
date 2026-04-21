from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from stat import S_IXGRP, S_IXOTH, S_IXUSR
from typing import Callable

import click

from meshagent.cli.local_settings import (
    SETTINGS_DIR,
    get_active_project,
    resolve_api_url,
)

CODEX_PROVIDER_ID_PREFIX = "meshagent"
CODEX_PROFILE_ID_PREFIX = "meshagent"
CODEX_DEFAULT_MODEL = "gpt-5.4"
CODEX_AUTH_TIMEOUT_MS = 10_000
CODEX_AUTH_REFRESH_INTERVAL_MS = 240_000
CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
CODEX_AUTH_WRAPPER_PATH = SETTINGS_DIR / "bin" / "codex-meshagent-auth"
CODEX_MANAGED_BLOCK_START_PREFIX = "# BEGIN MESHAGENT MANAGED BLOCK: CODEX PROJECT "
CODEX_MANAGED_BLOCK_END_PREFIX = "# END MESHAGENT MANAGED BLOCK: CODEX PROJECT "


@dataclass(frozen=True, slots=True)
class _CodexManagedProjectBlock:
    project_id: str
    provider_id: str
    profile_id: str


@dataclass(frozen=True, slots=True)
class CodexIntegrationResult:
    config_path: Path
    provider_id: str
    profile_id: str
    changed: bool


def _codex_provider_base_url(*, api_url: str | None = None) -> str:
    return f"{resolve_api_url(api_url=api_url)}/openai/v1"


def _codex_project_block_markers(*, project_id: str) -> tuple[str, str]:
    normalized_project_id = project_id.strip()
    return (
        f"{CODEX_MANAGED_BLOCK_START_PREFIX}{normalized_project_id}",
        f"{CODEX_MANAGED_BLOCK_END_PREFIX}{normalized_project_id}",
    )


def _normalize_profile_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if slug == "":
        return "project"
    return slug[:32].rstrip("-") or "project"


def _default_profile_slug(*, project_id: str, project_name: str | None) -> str:
    if project_name is not None and project_name.strip() != "":
        return _normalize_profile_slug(project_name)

    short_project_id = project_id.strip().split("-", maxsplit=1)[0]
    return _normalize_profile_slug(f"project-{short_project_id}")


def _iter_managed_project_blocks(existing: str) -> list[_CodexManagedProjectBlock]:
    block_pattern = re.compile(
        rf"(?ms)^"
        rf"{re.escape(CODEX_MANAGED_BLOCK_START_PREFIX)}(?P<project_id>[^\n]+)\n"
        rf"(?P<body>.*?)^"
        rf"{re.escape(CODEX_MANAGED_BLOCK_END_PREFIX)}(?P=project_id)\n?"
    )
    provider_pattern = re.compile(r"(?m)^\[model_providers\.([A-Za-z0-9_-]+)\]$")
    profile_pattern = re.compile(r"(?m)^\[profiles\.([A-Za-z0-9_-]+)\]$")

    managed_blocks: list[_CodexManagedProjectBlock] = []
    for match in block_pattern.finditer(existing):
        body = match.group("body")
        provider_match = provider_pattern.search(body)
        profile_match = profile_pattern.search(body)
        if provider_match is None or profile_match is None:
            continue
        managed_blocks.append(
            _CodexManagedProjectBlock(
                project_id=match.group("project_id").strip(),
                provider_id=provider_match.group(1),
                profile_id=profile_match.group(1),
            )
        )

    return managed_blocks


def _find_managed_project_block(
    *,
    existing: str,
    project_id: str,
) -> _CodexManagedProjectBlock | None:
    normalized_project_id = project_id.strip()
    for block in _iter_managed_project_blocks(existing):
        if block.project_id == normalized_project_id:
            return block
    return None


def _has_toml_table(existing: str, *, table_prefix: str, identifier: str) -> bool:
    return (
        f"[{table_prefix}.{identifier}]" in existing
        or f'[{table_prefix}."{identifier}"]' in existing
    )


def _resolve_codex_identifiers(
    *,
    existing: str,
    project_id: str,
    project_name: str | None,
) -> tuple[str, str]:
    existing_block = _find_managed_project_block(existing=existing, project_id=project_id)
    if existing_block is not None:
        return existing_block.provider_id, existing_block.profile_id

    base_identifier = (
        f"{CODEX_PROFILE_ID_PREFIX}-"
        f"{_default_profile_slug(project_id=project_id, project_name=project_name)}"
    )
    candidate = base_identifier
    suffix = 2
    while _has_toml_table(
        existing,
        table_prefix="model_providers",
        identifier=candidate,
    ) or _has_toml_table(
        existing,
        table_prefix="profiles",
        identifier=candidate,
    ):
        candidate = f"{base_identifier}-{suffix}"
        suffix += 1

    return candidate, candidate


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
        if resolved_candidate.exists():
            return shlex.join([str(resolved_candidate), "auth", "token"])

    return shlex.join(
        [sys.executable, "-m", "meshagent.cli.cli", "auth", "token"]
    )


def _write_codex_auth_wrapper(
    *,
    meshagent_executable: str | None = None,
    auth_wrapper_path: Path = CODEX_AUTH_WRAPPER_PATH,
) -> Path:
    auth_wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    command = _resolve_meshagent_auth_command(
        meshagent_executable=meshagent_executable
    )
    auth_wrapper_path.write_text(
        "#!/bin/sh\n"
        f"exec {command}\n"
    )
    auth_wrapper_path.chmod(
        auth_wrapper_path.stat().st_mode | S_IXUSR | S_IXGRP | S_IXOTH
    )
    return auth_wrapper_path


def _codex_managed_block(
    *,
    project_id: str,
    project_name: str | None,
    provider_id: str,
    profile_id: str,
    api_url: str | None = None,
    meshagent_executable: str | None = None,
    auth_wrapper_path: Path = CODEX_AUTH_WRAPPER_PATH,
    model: str = CODEX_DEFAULT_MODEL,
) -> str:
    provider_base_url = _codex_provider_base_url(api_url=api_url)
    wrapper_path = _write_codex_auth_wrapper(
        meshagent_executable=meshagent_executable,
        auth_wrapper_path=auth_wrapper_path,
    )
    start_marker, end_marker = _codex_project_block_markers(project_id=project_id)
    display_project_name = (
        project_name.strip()
        if project_name is not None and project_name.strip() != ""
        else project_id
    )
    lines = [
        start_marker,
        (
            "# Re-run `meshagent setup` from a different MeshAgent install"
            " if you want Codex to use a different binary."
        ),
        f"# MeshAgent project: {display_project_name} ({project_id})",
        f"[model_providers.{provider_id}]",
        f'name = {json.dumps("MeshAgent")}',
        f"base_url = {json.dumps(provider_base_url)}",
        (
            "http_headers = "
            f'{{"Meshagent-Project-Id"={json.dumps(project_id)}}}'
        ),
        "",
        f"[model_providers.{provider_id}.auth]",
        f"command = {json.dumps(str(wrapper_path))}",
        f"timeout_ms = {CODEX_AUTH_TIMEOUT_MS}",
        f"refresh_interval_ms = {CODEX_AUTH_REFRESH_INTERVAL_MS}",
        "",
        f"[profiles.{profile_id}]",
        f'model_provider = {json.dumps(provider_id)}',
        f"model = {json.dumps(model)}",
        end_marker,
        "",
    ]
    return "\n".join(lines)


def _upsert_managed_block(
    *,
    existing: str,
    project_id: str,
    managed_block: str,
) -> str:
    start_marker, end_marker = _codex_project_block_markers(project_id=project_id)
    pattern = re.compile(
        rf"(?ms)^{re.escape(start_marker)}\n"
        rf".*?^{re.escape(end_marker)}\n?"
    )
    if pattern.search(existing):
        return pattern.sub(managed_block, existing, count=1)

    existing_content = existing.rstrip()
    if existing_content == "":
        return managed_block
    return f"{existing_content}\n\n{managed_block}"


def configure_codex_integration(
    *,
    project_id: str | None = None,
    project_name: str | None = None,
    api_url: str | None = None,
    meshagent_executable: str | None = None,
    model: str = CODEX_DEFAULT_MODEL,
    config_path: Path = CODEX_CONFIG_PATH,
    auth_wrapper_path: Path = CODEX_AUTH_WRAPPER_PATH,
) -> CodexIntegrationResult:
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

    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = config_path.read_text() if config_path.exists() else ""
    provider_id, profile_id = _resolve_codex_identifiers(
        existing=existing,
        project_id=resolved_project_id,
        project_name=project_name,
    )
    managed_block = _codex_managed_block(
        project_id=resolved_project_id,
        project_name=project_name,
        provider_id=provider_id,
        profile_id=profile_id,
        api_url=api_url,
        meshagent_executable=meshagent_executable,
        auth_wrapper_path=auth_wrapper_path,
        model=model,
    )
    updated = _upsert_managed_block(
        existing=existing,
        project_id=resolved_project_id,
        managed_block=managed_block,
    )
    changed = updated != existing
    if changed:
        config_path.write_text(updated)

    return CodexIntegrationResult(
        config_path=config_path,
        provider_id=provider_id,
        profile_id=profile_id,
        changed=changed,
    )


def maybe_configure_local_tool_integrations(
    *,
    project_id: str | None = None,
    project_name: str | None = None,
    api_url: str | None = None,
    meshagent_executable: str | None = None,
    model: str = CODEX_DEFAULT_MODEL,
    prompt_fn: Callable[[str], bool] = lambda message: click.confirm(
        message, default=True
    ),
    echo_fn: Callable[[str], None] = click.echo,
    which: Callable[[str], str | None] = shutil.which,
    config_path: Path = CODEX_CONFIG_PATH,
    auth_wrapper_path: Path = CODEX_AUTH_WRAPPER_PATH,
) -> None:
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

    existing = config_path.read_text() if config_path.exists() else ""
    existing_result = _find_managed_project_block(
        existing=existing,
        project_id=resolved_project_id,
    )
    if existing_result is None:
        _, candidate_profile_id = _resolve_codex_identifiers(
            existing=existing,
            project_id=resolved_project_id,
            project_name=project_name,
        )
        display_project_name = (
            project_name.strip()
            if project_name is not None and project_name.strip() != ""
            else resolved_project_id
        )
        if not prompt_fn(
            (
                "Codex detected. Configure MeshAgent Codex profile "
                f"`{candidate_profile_id}` for project `{display_project_name}` "
                "in ~/.codex/config.toml?"
            )
        ):
            return

    result = configure_codex_integration(
        project_id=resolved_project_id,
        project_name=project_name,
        api_url=api_url,
        meshagent_executable=meshagent_executable,
        model=model,
        config_path=config_path,
        auth_wrapper_path=auth_wrapper_path,
    )
    if result.changed:
        message = (
            f"Configured Codex profile `{result.profile_id}` in {result.config_path}. "
            f"Use `codex -p {result.profile_id}` to run Codex through MeshAgent."
        )
    else:
        message = (
            f"Codex profile `{result.profile_id}` is already configured in {result.config_path}. "
            f"Use `codex -p {result.profile_id}` to run Codex through MeshAgent."
        )
    echo_fn(
        message
    )
