import pytest

from meshagent.cli import version_check


def test_cli_is_behind_server_compares_semver_prefixes() -> None:
    assert version_check._cli_is_behind_server(
        cli_version="0.41.5",
        server_version="0.42.0",
    )
    assert not version_check._cli_is_behind_server(
        cli_version="0.42.0",
        server_version="0.41.5",
    )
    assert not version_check._cli_is_behind_server(
        cli_version="0.42.0",
        server_version="0.42.0",
    )


@pytest.mark.asyncio
async def test_maybe_warn_if_cli_out_of_date_prints_yellow_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def _fake_fetch_server_version() -> str:
        return "0.42.0"

    monkeypatch.setattr(
        version_check, "_fetch_server_version", _fake_fetch_server_version
    )
    monkeypatch.setattr(version_check, "MESHAGENT_CLI_VERSION", "0.41.5")

    await version_check._maybe_warn_if_cli_out_of_date()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Warning: this meshagent CLI is older than the server" in captured.err
    assert "CLI 0.41.5, server 0.42.0" in captured.err


@pytest.mark.asyncio
async def test_maybe_warn_if_cli_out_of_date_ignores_fetch_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def _fake_fetch_server_version() -> str:
        raise RuntimeError("offline")

    monkeypatch.setattr(
        version_check, "_fetch_server_version", _fake_fetch_server_version
    )

    await version_check._maybe_warn_if_cli_out_of_date()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
