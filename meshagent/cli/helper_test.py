import pytest
import typer

from meshagent.cli.helper import parse_memory_selector


def test_parse_memory_selector_name_only() -> None:
    memory_name, namespace = parse_memory_selector("graph")

    assert memory_name == "graph"
    assert namespace is None


def test_parse_memory_selector_with_namespace() -> None:
    memory_name, namespace = parse_memory_selector("team/shared/graph")

    assert memory_name == "graph"
    assert namespace == ["team", "shared"]


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
