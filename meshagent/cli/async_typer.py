from __future__ import annotations

import asyncio
import inspect
import threading
from functools import partial, wraps
from typing import Any, Callable, TypeVar

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


class AsyncTyper(Typer):
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
