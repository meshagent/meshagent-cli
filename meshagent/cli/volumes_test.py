import pytest
import typer

from meshagent.cli.volumes import _volume_options


@pytest.mark.parametrize("storage_class", ["juice", "zerofs"])
def test_volume_options_support_max_size_for_managed_filesystems(
    storage_class: str,
) -> None:
    assert _volume_options(
        annotations={"example.test/purpose": "models"},
        volume_type=storage_class,
        max_size_mb=512,
    ) == ({"example.test/purpose": "models"}, storage_class, 512)


@pytest.mark.parametrize(
    ("storage_class", "max_size_mb"),
    [("standard", 512), ("zerofs", 0), ("juice", -1)],
)
def test_volume_options_reject_unsupported_or_invalid_max_size(
    storage_class: str,
    max_size_mb: int,
) -> None:
    with pytest.raises(typer.BadParameter):
        _volume_options(
            annotations=None,
            volume_type=storage_class,
            max_size_mb=max_size_mb,
        )
