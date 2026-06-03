from __future__ import annotations

import shlex
import sys
from collections.abc import Mapping, Sequence
from typing import Any, cast

from typer import Typer
from typer import _click as typer_click
from typer.testing import CliRunner as TyperCliRunner
from typer.testing import Result


class CliRunner(TyperCliRunner):
    def invoke(
        self,
        app: Typer | typer_click.Command,
        args: str | Sequence[str] | None = None,
        input: bytes | str | None = None,
        env: Mapping[str, str | None] | None = None,
        catch_exceptions: bool = True,
        color: bool = False,
        **extra: Any,
    ) -> Result:
        if not isinstance(app, typer_click.Command):
            return super().invoke(
                app,
                args=args,
                input=input,
                env=env,
                catch_exceptions=catch_exceptions,
                color=color,
                **extra,
            )

        cli = app
        exc_info = None
        with self.isolation(input=input, env=env, color=color) as outstreams:
            return_value = None
            exception: BaseException | None = None
            exit_code = 0

            if isinstance(args, str):
                args = shlex.split(args)

            try:
                prog_name = extra.pop("prog_name")
            except KeyError:
                prog_name = self.get_default_prog_name(cli)

            try:
                return_value = cli.main(args=args or (), prog_name=prog_name, **extra)
            except SystemExit as exc:
                exc_info = sys.exc_info()
                exit_code_value = cast("int | Any | None", exc.code)

                if exit_code_value is None:
                    exit_code_value = 0

                if exit_code_value != 0:
                    exception = exc

                if not isinstance(exit_code_value, int):
                    sys.stdout.write(str(exit_code_value))
                    sys.stdout.write("\n")
                    exit_code_value = 1

                exit_code = exit_code_value
            except Exception as exc:
                if not catch_exceptions:
                    raise
                exception = exc
                exit_code = 1
                exc_info = sys.exc_info()
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
                stdout = outstreams[0].getvalue()
                stderr = outstreams[1].getvalue()
                output = outstreams[2].getvalue()

        return Result(
            runner=self,
            stdout_bytes=stdout,
            stderr_bytes=stderr,
            output_bytes=output,
            return_value=return_value,
            exit_code=exit_code,
            exception=exception,
            exc_info=exc_info,  # type: ignore[arg-type]
        )
