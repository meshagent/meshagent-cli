from __future__ import annotations

import asyncio
import inspect
import threading
from functools import partial, wraps
from typing import Any, Callable, TypeVar

import click
import typer
from typer import Typer

T = TypeVar("T")


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

        try:
            return super().__call__(*args, **kwargs)
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
