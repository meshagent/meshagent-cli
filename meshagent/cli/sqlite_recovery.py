"""Offline SQLite replica recovery; never opens or replaces a room database."""

import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
from types import FrameType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
import typer


class RecoveryObject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: int
    min_txid: str
    max_txid: str
    size: int
    sha256: str | None = None
    error: str | None = None


class RecoveryReportFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["failed", "validated", "restored"]
    requested_txid: str
    recovered_txid: str | None = None
    sources: list[RecoveryObject]
    rejected: list[RecoveryObject]
    zero_filled_pages: list[int]
    integrity: str | None = None
    foreign_keys: str | None = None
    output_path: str | None = None
    output_sha256: str | None = None
    error: str | None = None


class RecoveryReport(RecoveryReportFields):
    version: Literal[1]


class RecoveryProtocol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recovery_protocol: Literal[1]


def _is_lower_hex(value: str | None, length: int) -> bool:
    return (
        value is not None
        and len(value) == length
        and all(char in "0123456789abcdef" for char in value)
    )


def _valid_recovery_sources(sources: list[RecoveryObject], target: int) -> bool:
    current = 0
    for source in sources:
        if (
            not 0 <= source.level <= 9
            or source.size <= 0
            or source.error is not None
            or not _is_lower_hex(source.sha256, 64)
            or not _is_lower_hex(source.min_txid, 16)
            or not _is_lower_hex(source.max_txid, 16)
        ):
            return False
        minimum, maximum = int(source.min_txid, 16), int(source.max_txid, 16)
        if source.level == 9 and minimum != 1:
            return False
        # Overlapping compactions are valid, but every selected object must
        # advance a contiguous chain without passing the requested target.
        if not 0 < minimum <= current + 1 <= maximum <= target:
            return False
        current = maximum
    return current == target


def _run_recovery_helper(
    command: list[str],
) -> tuple[subprocess.CompletedProcess[str], int | None]:
    process: subprocess.Popen[str] | None = None
    interrupted = None

    def forward_signal(signum: int, frame: FrameType | None) -> None:
        nonlocal interrupted
        interrupted = signum
        if process is not None:
            process.send_signal(signum)

    # The CLI owns these handlers only while waiting for its helper. Forwarding
    # allows the helper's context cancellation to close streams and temporary
    # files and return a report, including a candidate already published.
    previous = {}
    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, forward_signal)
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        ) as process:
            if interrupted is not None:
                process.send_signal(interrupted)
            stdout, stderr = process.communicate()
            completed = subprocess.CompletedProcess(
                command, process.returncode, stdout, stderr
            )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return completed, interrupted


def restore_database(
    source: Annotated[
        str,
        typer.Option(
            "--source",
            help="Copied replica directory, or gs:// replica URL",
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="New local SQLite file; never overwritten"),
    ] = None,
    txid: Annotated[
        str | None,
        typer.Option(
            "--txid", help="Exact 16-digit hexadecimal TXID; defaults to latest"
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Reconstruct and validate without publishing a file"
        ),
    ] = False,
    report: Annotated[
        Path | None,
        typer.Option("--report", help="Also write JSON results to a new report file"),
    ] = None,
    expected_sha256: Annotated[
        str | None,
        typer.Option(
            "--expected-sha256",
            help="Expected database SHA-256 from planning; fail if the result differs",
        ),
    ] = None,
    recovery_tool: Annotated[
        Path | None,
        typer.Option(
            "--recovery-tool", help="Path to meshagent-sqlite-recover executable"
        ),
    ] = None,
) -> None:
    if not dry_run and output is None:
        raise typer.BadParameter("--output is required unless --dry-run is set")
    if txid is not None and (
        len(txid) != 16
        or any(char not in "0123456789abcdefABCDEF" for char in txid)
        or int(txid, 16) == 0
    ):
        raise typer.BadParameter("--txid must be a nonzero 16-digit hexadecimal value")
    if expected_sha256 is not None:
        expected_sha256 = expected_sha256.lower()
        if not _is_lower_hex(expected_sha256, 64):
            raise typer.BadParameter(
                "--expected-sha256 must be a 64-digit hexadecimal value"
            )
    for path in (output, report):
        if path is not None and os.path.lexists(path):
            raise typer.BadParameter(f"Destination already exists: {path}")
    if (
        output is not None
        and report is not None
        and output.resolve() == report.resolve()
    ):
        raise typer.BadParameter("--output and --report must be different files")

    executable = _recovery_executable(recovery_tool)
    if executable is None:
        raise typer.BadParameter(
            "meshagent-sqlite-recover is not installed. Install the MeshAgent recovery "
            "helper or supply --recovery-tool; see the SQLite recovery guide."
        )
    try:
        probe = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=10,
        )
        RecoveryProtocol.model_validate_json(probe.stdout)
    except (
        OSError,
        subprocess.SubprocessError,
        ValidationError,
        UnicodeDecodeError,
    ) as error:
        raise typer.BadParameter(f"Incompatible recovery helper: {error}") from error

    command = [executable, "--source", source]
    if output is not None:
        command.extend(["--output", str(output.resolve())])
    if txid is not None:
        command.extend(["--txid", txid.lower()])
    if dry_run:
        command.append("--dry-run")

    # Reserve report output before recovery starts. Both destinations are
    # exclusive: neither a retry nor a racing caller may replace an old result.
    report_fd = None
    try:
        if report is not None:
            report_fd = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        completed = None
        interrupted = None
        try:
            completed, interrupted = _run_recovery_helper(command)
            payload = RecoveryReport.model_validate_json(completed.stdout)
        except (ValidationError, UnicodeDecodeError):
            detail = (
                "helper output is not valid UTF-8"
                if completed is None
                else completed.stderr.strip() or "invalid JSON recovery report"
            )
            exit_detail = (
                f" (exit {completed.returncode})" if completed is not None else ""
            )
            message = (
                f"Recovery helper returned no valid report{exit_detail}: "
                f"{detail}. Recovery outcome is unverified."
            )
            if output is not None:
                message += f" Inspect requested output path {output.resolve()} before retrying."
            # A helper may have published a candidate before its report was
            # interrupted. Preserve it without claiming ownership, contents,
            # recovered position, or integrity from an invalid response.
            payload = RecoveryReport(
                version=1,
                status="failed",
                requested_txid=txid.lower() if txid is not None else "",
                sources=[],
                rejected=[],
                zero_filled_pages=[],
                error=message,
            )
        expected_status = "validated" if dry_run else "restored"
        successful = (
            completed is not None
            and completed.returncode == 0
            and payload.status == expected_status
            and payload.integrity == "ok"
            and payload.foreign_keys == "ok"
            and payload.recovered_txid == payload.requested_txid
            and (txid is None or payload.recovered_txid == txid.lower())
            and len(payload.requested_txid) == 16
            and all(char in "0123456789abcdef" for char in payload.requested_txid)
            and int(payload.requested_txid, 16) != 0
            and _valid_recovery_sources(
                payload.sources, int(payload.requested_txid, 16)
            )
            and payload.output_sha256 is not None
            and len(payload.output_sha256) == 64
            and all(char in "0123456789abcdef" for char in payload.output_sha256)
            and payload.error is None
        )
        if successful:
            if dry_run:
                successful = payload.output_path is None and (
                    output is None or not os.path.lexists(output)
                )
            else:
                assert output is not None
                successful = (
                    payload.output_path == str(output.resolve())
                    and output.is_file()
                    and not output.is_symlink()
                )
                if successful:
                    try:
                        with output.open("rb") as candidate:
                            successful = (
                                hashlib.file_digest(candidate, "sha256").hexdigest()
                                == payload.output_sha256
                            )
                            if not os.path.samestat(
                                os.fstat(candidate.fileno()), output.lstat()
                            ):
                                raise OSError(
                                    f"Recovery candidate destination changed: {output}"
                                )
                    except OSError as error:
                        successful = False
                        payload.error = f"Could not verify recovery candidate: {error}"
        if (
            successful
            and expected_sha256 is not None
            and payload.output_sha256 != expected_sha256
        ):
            successful = False
            payload.error = (
                f"Recovered database SHA-256 differs from the expected value: "
                f"expected {expected_sha256}, recovered {payload.output_sha256}"
            )
        if interrupted is not None:
            successful = False
            detail = f"Recovery interrupted by {signal.Signals(interrupted).name}"
            payload.error = f"{payload.error}; {detail}" if payload.error else detail
        if not successful:
            payload.status = "failed"
            if payload.error is None:
                payload.error = "Recovery helper result did not satisfy the requested restore contract"
        serialized = payload.model_dump_json(indent=2, exclude_none=True) + "\n"
        if report_fd is not None:
            try:
                with os.fdopen(report_fd, "w") as stream:
                    report_fd = None
                    stream.write(serialized)
                    stream.flush()
                    os.fsync(stream.fileno())
                    assert report is not None
                    # Sync the new directory entry as well as the file bytes
                    # before acknowledging a durable recovery report.
                    directory_fd = os.open(report.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                    if not os.path.samestat(os.fstat(stream.fileno()), report.lstat()):
                        raise OSError(f"Recovery report destination changed: {report}")
            except OSError as error:
                successful = False
                payload.status = "failed"
                detail = f"Could not save recovery report: {error}"
                payload.error = (
                    f"{payload.error}; {detail}" if payload.error else detail
                )
                serialized = payload.model_dump_json(indent=2, exclude_none=True) + "\n"
        typer.echo(serialized, nl=False)
        if not successful:
            raise typer.Exit(1)
    except OSError as error:
        raise typer.BadParameter(f"Recovery failed: {error}") from error
    finally:
        if report_fd is not None:
            os.close(report_fd)
            # Preserve the reservation on failure. The pathname may now refer
            # to another file, so unlinking it could delete another operation's
            # report rather than the file represented by this descriptor.


def _recovery_executable(explicit: Path | None) -> str | None:
    if explicit is not None:
        return str(explicit.resolve())
    name = (
        "meshagent-sqlite-recover.exe"
        if os.name == "nt"
        else "meshagent-sqlite-recover"
    )
    bundled = Path(__file__).parent / "bin" / name
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    return shutil.which(name)
