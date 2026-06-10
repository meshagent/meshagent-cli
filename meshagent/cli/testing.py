from collections.abc import Mapping, Sequence
import shlex
import sys
from typing import Any, cast

import typer
from typer import _click
from typer.testing import CliRunner as TyperCliRunner
from typer.testing import Result

from meshagent.cli.async_typer import get_command


class CliRunner(TyperCliRunner):
    def invoke(
        self,
        app: typer.Typer | _click.Command,
        args: str | Sequence[str] | None = None,
        input: bytes | str | None = None,
        env: Mapping[str, str | None] | None = None,
        catch_exceptions: bool = True,
        color: bool = False,
        **extra: Any,
    ) -> Result:
        if isinstance(app, _click.Command):
            return self._invoke_command(
                app,
                args=args,
                input=input,
                env=env,
                catch_exceptions=catch_exceptions,
                color=color,
                **extra,
            )
        return self._invoke_command(
            get_command(app),
            args=args,
            input=input,
            env=env,
            catch_exceptions=catch_exceptions,
            color=color,
            **extra,
        )

    def _invoke_command(
        self,
        command: _click.Command,
        args: str | Sequence[str] | None = None,
        input: bytes | str | None = None,
        env: Mapping[str, str | None] | None = None,
        catch_exceptions: bool = True,
        color: bool = False,
        **extra: Any,
    ) -> Result:
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
                prog_name = self.get_default_prog_name(command)

            try:
                return_value = command.main(
                    args=args or (), prog_name=prog_name, **extra
                )
            except SystemExit as exc:
                exc_info = sys.exc_info()
                code = cast("int | Any | None", exc.code)
                if code is None:
                    code = 0
                if code != 0:
                    exception = exc
                if not isinstance(code, int):
                    sys.stdout.write(str(code))
                    sys.stdout.write("\n")
                    code = 1
                exit_code = code
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
            exc_info=exc_info,
        )
