import importlib
import types

import click
from click.testing import CliRunner

from meshagent.cli import async_typer


def _module_with_app(
    *, module_name: str, command_name: str, output: str
) -> types.ModuleType:
    module = types.ModuleType(module_name)
    app = async_typer.AsyncTyper(help=f"{command_name} commands")

    @app.callback()
    def _callback() -> None:
        return None

    @app.command(command_name)
    def _command() -> None:
        click.echo(output)

    module.app = app
    return module


def test_lazy_help_does_not_import_subcommands(monkeypatch) -> None:
    app = async_typer.LazyTyper(help="Root commands")
    app.add_lazy_command(
        name="child",
        module="tests.fake_child",
        help="Child commands",
    )

    recorded_imports: list[str] = []
    original_import_module = importlib.import_module

    def _fake_import_module(name: str, package: str | None = None):
        recorded_imports.append(name)
        return original_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", _fake_import_module)

    result = CliRunner().invoke(async_typer.get_command(app), ["--help"])

    assert result.exit_code == 0
    assert "child" in result.output
    assert recorded_imports == []


def test_lazy_subcommand_help_imports_only_selected_branch(monkeypatch) -> None:
    app = async_typer.LazyTyper(help="Root commands")
    app.add_lazy_command(
        name="child",
        module="tests.fake_child",
        help="Child commands",
    )

    fake_child_module = _module_with_app(
        module_name="tests.fake_child",
        command_name="hello",
        output="hello",
    )

    recorded_imports: list[str] = []
    original_import_module = importlib.import_module

    def _fake_import_module(name: str, package: str | None = None):
        recorded_imports.append(name)
        if name == "tests.fake_child":
            return fake_child_module
        return original_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", _fake_import_module)

    result = CliRunner().invoke(async_typer.get_command(app), ["child", "--help"])

    assert result.exit_code == 0
    assert "hello" in result.output
    assert recorded_imports == ["tests.fake_child"]


def test_lazy_command_path_loads_leaf_command(monkeypatch) -> None:
    app = async_typer.LazyTyper(help="Root commands")
    app.add_lazy_command(
        name="hello",
        module="tests.fake_leaf",
        help="Say hello",
        command_path=("hello",),
    )

    fake_leaf_module = _module_with_app(
        module_name="tests.fake_leaf",
        command_name="hello",
        output="hello",
    )

    recorded_imports: list[str] = []
    original_import_module = importlib.import_module

    def _fake_import_module(name: str, package: str | None = None):
        recorded_imports.append(name)
        if name == "tests.fake_leaf":
            return fake_leaf_module
        return original_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", _fake_import_module)

    result = CliRunner().invoke(async_typer.get_command(app), ["hello"])

    assert result.exit_code == 0
    assert result.output == "hello\n"
    assert recorded_imports == ["tests.fake_leaf"]
