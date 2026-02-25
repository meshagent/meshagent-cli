import pytest
import typer

from meshagent.cli.port import _parse_port_mapping


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3000:3000", (3000, 3000)),
        ("0:3000", (0, 3000)),
        (" 8080 : 80 ", (8080, 80)),
    ],
)
def test_parse_port_mapping_accepts_valid_values(
    value: str, expected: tuple[int, int]
) -> None:
    assert _parse_port_mapping(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "3000",
        ":3000",
        "3000:",
        "abc:3000",
        "3000:abc",
        "-1:3000",
        "70000:3000",
        "3000:0",
        "3000:70000",
    ],
)
def test_parse_port_mapping_rejects_invalid_values(value: str) -> None:
    with pytest.raises(typer.BadParameter):
        _parse_port_mapping(value)
