from pathlib import Path

import pytest
import typer

from meshagent.agents.context import AgentSessionContext
from meshagent.cli.helper import (
    DEFAULT_DATASET_NAMESPACE,
    DEFAULT_SHELL_IMAGE,
    build_shell_tool,
    init_context_from_spec,
    parse_memory_selector,
    parse_shell_tool_mounts,
    resolve_dataset_namespace,
    resolve_shell_image,
    supports_openai_shell_tool,
)
from meshagent.openai.tools.responses_adapter import ShellTool
from meshagent.tools import ContainerShellTool, ProcessShellTool


def test_parse_memory_selector_name_only() -> None:
    memory_name, namespace = parse_memory_selector("graph")

    assert memory_name == "graph"
    assert namespace is None


def test_parse_memory_selector_with_namespace() -> None:
    memory_name, namespace = parse_memory_selector("team/shared/graph")

    assert memory_name == "graph"
    assert namespace == ["team", "shared"]


def test_resolve_dataset_namespace_without_default_returns_none() -> None:
    assert resolve_dataset_namespace(namespace=None) is None


def test_resolve_dataset_namespace_uses_default_when_omitted() -> None:
    assert resolve_dataset_namespace(
        namespace=None,
        default_namespace=DEFAULT_DATASET_NAMESPACE,
    ) == [".datasets"]


def test_resolve_dataset_namespace_splits_namespace_segments() -> None:
    assert resolve_dataset_namespace(namespace="foo::bar") == ["foo", "bar"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, DEFAULT_SHELL_IMAGE),
        ("", DEFAULT_SHELL_IMAGE),
        ("  ", DEFAULT_SHELL_IMAGE),
        ("python:3.12", "python:3.12"),
        (" none ", None),
        ("NONE", None),
    ],
)
def test_resolve_shell_image(value: str | None, expected: str | None) -> None:
    assert resolve_shell_image(value) == expected


@pytest.mark.parametrize(
    ("model", "llm_participant", "expected"),
    [
        ("gpt-5", None, True),
        ("claude-3-7-sonnet", None, False),
        ("gpt-5", "remote-llm", False),
    ],
)
def test_supports_openai_shell_tool(
    model: str, llm_participant: str | None, expected: bool
) -> None:
    assert (
        supports_openai_shell_tool(
            model=model,
            llm_participant=llm_participant,
        )
        is expected
    )


@pytest.mark.asyncio
async def test_build_shell_tool_uses_container_shell_for_non_gpt_model() -> None:
    tool = build_shell_tool(
        model="claude-3-7-sonnet",
        working_dir="/workspace",
        image=DEFAULT_SHELL_IMAGE,
    )

    assert isinstance(tool, ContainerShellTool)


@pytest.mark.asyncio
async def test_build_shell_tool_uses_shell_tool_for_gpt_model() -> None:
    tool = build_shell_tool(
        model="gpt-5",
        working_dir="/workspace",
        image=DEFAULT_SHELL_IMAGE,
    )

    assert isinstance(tool, ShellTool)


@pytest.mark.asyncio
async def test_build_shell_tool_uses_process_shell_for_non_gpt_model_without_image() -> (
    None
):
    tool = build_shell_tool(
        model="claude-3-7-sonnet",
        working_dir="/workspace",
        image=None,
    )

    assert isinstance(tool, ProcessShellTool)


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "/graph",
        "team/",
        "team//graph",
    ],
)
def test_parse_memory_selector_rejects_empty_segments(value: str) -> None:
    with pytest.raises(typer.BadParameter):
        parse_memory_selector(value)


def test_parse_shell_tool_mounts_parses_empty_dir_mounts() -> None:
    mounts = parse_shell_tool_mounts(
        room_paths=[],
        project_paths=[],
        empty_dir_paths=["/cache", "/tmp/work:ro"],
    )

    assert mounts is not None
    assert mounts.empty_dirs is not None
    assert [mount.model_dump(mode="json") for mount in mounts.empty_dirs] == [
        {"path": "/cache", "read_only": False},
        {"path": "/tmp/work", "read_only": True},
    ]


def test_parse_shell_tool_mounts_parses_config_mounts() -> None:
    mounts = parse_shell_tool_mounts(
        room_paths=[],
        project_paths=[],
        config_paths=["/var/run/meshagent", "/tmp/meshagent-config"],
    )

    assert mounts is not None
    assert mounts.configs is not None
    assert [mount.model_dump(mode="json") for mount in mounts.configs] == [
        {"path": "/var/run/meshagent"},
        {"path": "/tmp/meshagent-config"},
    ]


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "/src:/cache",
        "/src:/cache:ro",
    ],
)
def test_parse_shell_tool_mounts_rejects_empty_dir_bind_syntax(value: str) -> None:
    with pytest.raises(typer.BadParameter, match="--shell-tool-empty-dir"):
        parse_shell_tool_mounts(
            room_paths=[],
            project_paths=[],
            empty_dir_paths=[value],
        )


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "/var/run/meshagent:ro",
    ],
)
def test_parse_shell_tool_mounts_rejects_invalid_config_mounts(value: str) -> None:
    with pytest.raises(typer.BadParameter, match="--shell-tool-config-mount"):
        parse_shell_tool_mounts(
            room_paths=[],
            project_paths=[],
            config_paths=[value],
        )


@pytest.mark.asyncio
async def test_init_context_from_spec_handles_missing_annotations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec_path = tmp_path / "meshagent.yaml"
    spec_path.write_text(
        "\n".join(
            [
                "version: v1",
                "kind: Service",
                "metadata:",
                "  name: chatbot",
                "  description: Helpful chatbot",
            ]
        )
    )
    monkeypatch.setenv("MESHAGENT_SPEC_PATH", str(spec_path))
    context = AgentSessionContext(system_role=None)

    await init_context_from_spec(context)

    assert context.messages == [
        {
            "role": "assistant",
            "content": "This agent's description:\nHelpful chatbot",
        }
    ]


@pytest.mark.asyncio
async def test_init_context_from_spec_adds_readme_when_annotation_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec_path = tmp_path / "meshagent.yaml"
    spec_path.write_text(
        "\n".join(
            [
                "version: v1",
                "kind: Service",
                "metadata:",
                "  name: chatbot",
                "  annotations:",
                '    meshagent.service.readme: "Read me first"',
            ]
        )
    )
    monkeypatch.setenv("MESHAGENT_SPEC_PATH", str(spec_path))
    context = AgentSessionContext(system_role=None)

    await init_context_from_spec(context)

    assert context.messages == [
        {
            "role": "assistant",
            "content": "This agent's README:\nRead me first",
        }
    ]
