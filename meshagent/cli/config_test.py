import pytest

from meshagent.api.client import MeshagentDeploymentConfig, MeshagentDomains
from meshagent.cli import cli, config


class _FakeConfigClient:
    def __init__(self, deployment_config: MeshagentDeploymentConfig) -> None:
        self._deployment_config = deployment_config
        self.closed = False

    async def get_config(self) -> MeshagentDeploymentConfig:
        return self._deployment_config

    async def close(self) -> None:
        self.closed = True


def test_config_get_domains_pages_prints_pages_domain(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_client = _FakeConfigClient(
        MeshagentDeploymentConfig(
            domains=MeshagentDomains(pages="pages.meshagent.example")
        )
    )

    async def _fake_get_client() -> _FakeConfigClient:
        return fake_client

    monkeypatch.setattr(config, "get_client", _fake_get_client)

    cli.app(["config", "get", "domains.pages"])

    captured = capsys.readouterr()
    assert captured.out == "pages.meshagent.example\n"
    assert captured.err == ""
    assert fake_client.closed is True


def test_config_get_version_prints_server_version(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_client = _FakeConfigClient(
        MeshagentDeploymentConfig(
            domains=MeshagentDomains(),
            version="0.41.5",
        )
    )

    async def _fake_get_client() -> _FakeConfigClient:
        return fake_client

    monkeypatch.setattr(config, "get_client", _fake_get_client)

    cli.app(["config", "get", "version"])

    captured = capsys.readouterr()
    assert captured.out == "0.41.5\n"
    assert captured.err == ""
    assert fake_client.closed is True


def test_config_get_rejects_unknown_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_client = _FakeConfigClient(
        MeshagentDeploymentConfig(domains=MeshagentDomains())
    )

    async def _fake_get_client() -> _FakeConfigClient:
        return fake_client

    monkeypatch.setattr(config, "get_client", _fake_get_client)

    with pytest.raises(SystemExit) as exc_info:
        cli.app(["config", "get", "unknown.path"])

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unsupported config path" in captured.err
    assert fake_client.closed is True
