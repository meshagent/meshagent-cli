from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
import threading
from dataclasses import dataclass
from functools import partial, wraps
from typing import Any, Callable, Sequence, TypeVar

import click
import typer
from typer import Typer
from typer.main import DeveloperExceptionConfig
from typer.main import _typer_developer_exception_attr_name
from typer.main import except_hook
from typer.main import get_command as get_typer_command
from typer.main import get_group as get_typer_group
from typer.main import get_install_completion_arguments

T = TypeVar("T")


@dataclass(frozen=True)
class LazyCommandRegistration:
    name: str
    module: str
    attribute: str = "app"
    help: str | None = None
    short_help: str | None = None
    hidden: bool = False
    deprecated: bool = False
    command_path: tuple[str, ...] = ()


class LazyLoadedCommand(click.Command):
    def __init__(
        self,
        *,
        registration: LazyCommandRegistration,
    ) -> None:
        super().__init__(
            name=registration.name,
            help=registration.help,
            short_help=registration.short_help,
            hidden=registration.hidden,
            deprecated=registration.deprecated,
        )
        self._registration = registration
        self._loaded_command: click.Command | None = None

    def _load_command(self) -> click.Command:
        if self._loaded_command is not None:
            return self._loaded_command

        module = importlib.import_module(self._registration.module)
        try:
            target = module.__dict__[self._registration.attribute]
        except KeyError as exc:
            raise RuntimeError(
                f"{self._registration.module} has no attribute {self._registration.attribute}"
            ) from exc

        command = _coerce_to_click_command(target)
        for segment in self._registration.command_path:
            if not isinstance(command, click.Group):
                raise RuntimeError(
                    f"{self._registration.module}.{self._registration.attribute} does not expose subcommand path "
                    f"{' '.join(self._registration.command_path)}"
                )
            resolved = command.get_command(click.Context(command), segment)
            if resolved is None:
                raise RuntimeError(
                    f"{self._registration.module}.{self._registration.attribute} has no subcommand {segment}"
                )
            command = resolved

        self._loaded_command = command
        return command

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        return self._load_command().make_context(
            info_name,
            args,
            parent=parent,
            **extra,
        )

    def shell_complete(
        self, ctx: click.Context, incomplete: str
    ) -> list[click.shell_completion.CompletionItem]:
        return self._load_command().shell_complete(ctx, incomplete)

    def get_help(self, ctx: click.Context) -> str:
        return self._load_command().get_help(ctx)


def _coerce_to_click_command(target: Any) -> click.Command:
    if isinstance(target, click.Command):
        return target
    if isinstance(target, Typer):
        return get_command(target)
    raise TypeError(f"Unsupported lazy command target: {target!r}")


def _materialize_command(command: click.Command) -> click.Command:
    if isinstance(command, LazyLoadedCommand):
        return _materialize_command(command._load_command())

    if isinstance(command, click.Group):
        command.commands = {
            name: _materialize_command(subcommand)
            for name, subcommand in command.commands.items()
        }
    return command


def get_command(
    typer_instance: Typer | click.Command,
    *,
    materialize_lazy: bool = False,
) -> click.Command:
    if isinstance(typer_instance, click.Command):
        click_command = typer_instance
    elif isinstance(typer_instance, LazyTyper):
        if len(typer_instance.registered_lazy_commands) > 0:
            click_command = get_typer_group(typer_instance)
            for registration in typer_instance.registered_lazy_commands:
                click_command.commands[registration.name] = LazyLoadedCommand(
                    registration=registration,
                )
            if typer_instance._add_completion:
                click_install_param, click_show_param = (
                    get_install_completion_arguments()
                )
                click_command.params.append(click_install_param)
                click_command.params.append(click_show_param)
        else:
            click_command = get_typer_command(typer_instance)
    else:
        click_command = get_typer_command(typer_instance)

    if materialize_lazy:
        return _materialize_command(click_command)
    return click_command


def collect_lazy_command_modules(root: "LazyTyper") -> list[str]:
    modules: set[str] = set()
    pending: list[LazyTyper] = [root]
    seen_apps: set[int] = set()
    seen_targets: set[tuple[str, str]] = set()

    while pending:
        app = pending.pop()
        app_id = id(app)
        if app_id in seen_apps:
            continue
        seen_apps.add(app_id)

        for registration in app.registered_lazy_commands:
            modules.add(registration.module)

            target_key = (registration.module, registration.attribute)
            if target_key in seen_targets:
                continue
            seen_targets.add(target_key)

            module = importlib.import_module(registration.module)
            try:
                target = module.__dict__[registration.attribute]
            except KeyError as exc:
                raise RuntimeError(
                    f"{registration.module} has no attribute {registration.attribute}"
                ) from exc

            if isinstance(target, LazyTyper):
                pending.append(target)

    return sorted(modules)


def collect_lazy_command_modules_from_entrypoint(
    module_name: str,
    *,
    attribute: str = "app",
) -> list[str]:
    module = importlib.import_module(module_name)
    try:
        target = module.__dict__[attribute]
    except KeyError as exc:
        raise RuntimeError(f"{module_name} has no attribute {attribute}") from exc

    if not isinstance(target, LazyTyper):
        raise TypeError(
            f"{module_name}.{attribute} must be a LazyTyper, got {type(target)!r}"
        )

    return collect_lazy_command_modules(target)


def _run_coroutine_sync(
    coro: "asyncio.Future[T] | asyncio.coroutines.Coroutine[Any, Any, T]",
) -> T:
    """
    Run an awaitable from sync code.

    - If we're not currently in an event loop, use asyncio.run().
    - If we ARE in a running loop (e.g. inside an agent / notebook / ASGI app),
      run asyncio.run() in a separate thread and block for the result.

    This avoids: RuntimeError: asyncio.run() cannot be called from a running event loop
    """
    try:
        asyncio.get_running_loop()
        in_running_loop = True
    except RuntimeError:
        in_running_loop = False

    if not in_running_loop:
        return asyncio.run(coro)  # type: ignore[arg-type]

    result: dict[str, Any] = {}
    done = threading.Event()

    def _worker() -> None:
        try:
            result["value"] = asyncio.run(coro)  # type: ignore[arg-type]
        except BaseException as e:
            result["error"] = e
        finally:
            done.set()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    done.wait()

    if "error" in result:
        raise result["error"]
    return result["value"]  # type: ignore[return-value]


def _missing_parameter_name(error: click.MissingParameter) -> str:
    param = error.param
    if isinstance(param, click.Option):
        for option_name in param.opts:
            if option_name.startswith("--"):
                return option_name
        if param.opts:
            return param.opts[0]
        if param.secondary_opts:
            return param.secondary_opts[0]
    if isinstance(param, click.Argument) and param.name is not None:
        return param.name
    if param is not None and param.name is not None:
        return param.name

    param_hint = error.param_hint
    if isinstance(param_hint, str):
        return param_hint
    if param_hint:
        return param_hint[0]
    return "unknown"


class AsyncTyper(Typer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if "no_args_is_help" not in kwargs:
            kwargs["no_args_is_help"] = True
        super().__init__(*args, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        explicit_standalone_mode = "standalone_mode" in kwargs
        if not explicit_standalone_mode:
            kwargs["standalone_mode"] = False

        if sys.excepthook != except_hook:
            sys.excepthook = except_hook

        try:
            return get_command(self)(*args, **kwargs)
        except click.MissingParameter as e:
            if explicit_standalone_mode:
                raise
            missing_name = _missing_parameter_name(e)
            click.secho(
                f" Required parameter is missing: {missing_name}",
                fg="red",
                err=True,
            )
            if e.ctx is not None:
                typer.echo(e.ctx.get_help(), err=True)
            raise SystemExit(e.exit_code)
        except click.ClickException as e:
            if explicit_standalone_mode:
                raise
            e.show()
            raise SystemExit(e.exit_code)
        except click.Abort:
            if explicit_standalone_mode:
                raise
            raise SystemExit(1)
        except click.exceptions.Exit as e:
            if explicit_standalone_mode:
                raise
            raise SystemExit(e.exit_code)
        except Exception as e:
            setattr(
                e,
                _typer_developer_exception_attr_name,
                DeveloperExceptionConfig(
                    pretty_exceptions_enable=self.pretty_exceptions_enable,
                    pretty_exceptions_show_locals=self.pretty_exceptions_show_locals,
                    pretty_exceptions_short=self.pretty_exceptions_short,
                ),
            )
            raise

    @staticmethod
    def maybe_run_async(decorator: Callable[..., Any], func: Callable[..., Any]) -> Any:
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            def runner(*args: Any, **kwargs: Any) -> Any:
                return _run_coroutine_sync(func(*args, **kwargs))

            decorator(runner)
        else:
            decorator(func)
        return func

    def callback(self, *args: Any, **kwargs: Any) -> Any:
        decorator = super().callback(*args, **kwargs)
        return partial(self.maybe_run_async, decorator)

    def command(self, *args: Any, **kwargs: Any) -> Any:
        decorator = super().command(*args, **kwargs)
        return partial(self.maybe_run_async, decorator)

    # keep your existing name if you prefer
    def async_command(self, *args: Any, **kwargs: Any) -> Any:
        return self.command(*args, **kwargs)


class LazyTyper(AsyncTyper):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.registered_lazy_commands: list[LazyCommandRegistration] = []

    def add_lazy_command(
        self,
        *,
        name: str,
        module: str,
        attribute: str = "app",
        help: str | None = None,
        short_help: str | None = None,
        hidden: bool = False,
        deprecated: bool = False,
        command_path: Sequence[str] = (),
    ) -> None:
        self.registered_lazy_commands.append(
            LazyCommandRegistration(
                name=name,
                module=module,
                attribute=attribute,
                help=help,
                short_help=short_help,
                hidden=hidden,
                deprecated=deprecated,
                command_path=tuple(command_path),
            )
        )
