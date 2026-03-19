import pytest
import typer

from meshagent.cli.helper import (
    parse_memory_selector,
    parse_shell_tool_mounts,
    resolve_shell_image,
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
