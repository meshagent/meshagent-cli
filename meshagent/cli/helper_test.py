from pathlib import Path

import pytest
import typer

from meshagent.agents.context import AgentSessionContext
from meshagent.cli.helper import (
    parse_memory_selector,
    parse_shell_tool_mounts,
    resolve_shell_image,
    init_context_from_spec,
)


def test_parse_memory_selector_name_only() -> None:
    memory_name, namespace = parse_memory_selector("graph")

    assert memory_name == "graph"
    assert namespace is None


def test_parse_memory_selector_with_namespace() -> None:
    memory_name, namespace = parse_memory_selector("team/shared/graph")

    assert memory_name == "graph"
    assert namespace == ["team", "shared"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "python:3.13"),
        ("", "python:3.13"),
        ("  ", "python:3.13"),
        ("python:3.12", "python:3.12"),
        (" none ", None),
        ("NONE", None),
    ],
)
def test_resolve_shell_image(value: str | None, expected: str | None) -> None:
    assert resolve_shell_image(value) == expected


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
