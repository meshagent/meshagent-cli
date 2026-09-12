import hashlib
import errno
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import signal
import stat
import sys
import time

import pytest
from typer.testing import CliRunner

from meshagent.cli.sqlite import app
from meshagent.cli import sqlite_recovery


def recovery_report(status: str = "restored") -> dict:
    return {
        "version": 1,
        "status": status,
        "requested_txid": "00000000000000b4",
        "recovered_txid": "00000000000000b4",
        "sources": [
            {
                "level": 1,
                "min_txid": "0000000000000001",
                "max_txid": "00000000000000b4",
                "size": 1000,
                "sha256": "a" * 64,
            }
        ],
        "output_sha256": hashlib.sha256(b"candidate").hexdigest(),
        "rejected": [],
        "zero_filled_pages": [991],
        "integrity": "ok",
        "foreign_keys": "ok",
    }


def mock_helper(monkeypatch, payload: dict, returncode: int = 0) -> list[list[str]]:
    calls = []
    monkeypatch.setattr(sqlite_recovery.shutil, "which", lambda _: "/test/recovery")

    def run(args, **kwargs):
        calls.append(args)
        if args[-1] == "--version":
            protocol = 1
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"recovery_protocol": protocol}), ""
            )
        if payload["status"] == "restored" and "--output" in args:
            output = Path(args[args.index("--output") + 1])
            output.write_bytes(b"candidate")
            payload.setdefault("output_path", str(output))
        return subprocess.CompletedProcess(args, returncode, json.dumps(payload), "")

    monkeypatch.setattr(sqlite_recovery.subprocess, "run", run)
    monkeypatch.setattr(
        sqlite_recovery,
        "_run_recovery_helper",
        lambda command: (
            subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", check=False
            ),
            None,
        ),
    )
    return calls


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("published", [False, True])
@pytest.mark.skipif(sys.platform == "win32", reason="Unix process signal contract")
def test_restore_process_cancellation_preserves_candidate(
    tmp_path: Path, signum, published: bool
):
    helper = tmp_path / "helper"
    ready = tmp_path / "ready"
    stopped = tmp_path / "stopped"
    output = tmp_path / "candidate.sqlite"
    report_path = tmp_path / "report.json"
    payload = recovery_report()
    payload["status"] = "restored"
    protocol = 1
    helper.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, sys, time\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        f"    print('{{\"recovery_protocol\":{protocol}}}')\n"
        "    sys.exit(0)\n"
        f"report = {payload!r}\n"
        "output = Path(sys.argv[sys.argv.index('--output') + 1])\n"
        f"if {published!r}: output.write_bytes(b'candidate')\n"
        "report['output_path'] = str(output)\n"
        "def stop(signum, frame):\n"
        f"    Path({str(stopped)!r}).write_text(str(signum))\n"
        f"    if not {published!r}:\n"
        "        report['status'] = 'failed'\n"
        "        report['error'] = 'context canceled'\n"
        # Simulate a publication completed just before cancellation. Even a
        # successful helper report must not turn an interrupted CLI into success.
        "    print(json.dumps(report), flush=True)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGINT, stop)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "while True: time.sleep(0.01)\n"
    )
    helper.chmod(0o700)
    packaged_cli = sqlite_recovery.os.environ.get("MESHAGENT_SQLITE_PACKAGED_CLI")
    command = (
        [packaged_cli] if packaged_cli else [sys.executable, "-m", "meshagent.cli.cli"]
    )
    process_env = sqlite_recovery.os.environ.copy()
    if packaged_cli:
        process_env["PATH"] = ""
    process = subprocess.Popen(
        command
        + [
            "room",
            "sqlite",
            "recover",
            "--source",
            "copy",
            "--output",
            str(output),
            "--report",
            str(report_path),
            "--recovery-tool",
            str(helper),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=process_env,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "helper did not become ready"
            time.sleep(0.01)
        process.send_signal(signum)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 1, (stdout, stderr)
        report = json.loads(stdout)
        assert report["version"] == protocol
        assert report == json.loads(report_path.read_text())
        assert report["status"] == "failed"
        assert "interrupted" in report["error"]
        assert stopped.read_text() == str(signum)
        if published:
            assert output.read_bytes() == b"candidate"
        else:
            assert not output.exists()
    finally:
        if process.poll() is None:
            process.kill()
        # Before the fix, signaling only the CLI can leave its helper running.
        if ready.exists() and not stopped.exists():
            try:
                sqlite_recovery.os.kill(int(ready.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
        process.communicate(timeout=10)


@pytest.mark.parametrize("launch_error", [False, True])
def test_recovery_helper_restores_caller_signal_handlers(tmp_path: Path, launch_error):
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    command = (
        [str(tmp_path / "missing-helper")]
        if launch_error
        else [sys.executable, "-c", "print('finished')"]
    )
    if launch_error:
        with pytest.raises(FileNotFoundError):
            sqlite_recovery._run_recovery_helper(command)
    else:
        completed, interrupted = sqlite_recovery._run_recovery_helper(command)
        assert completed.returncode == 0
        assert completed.stdout == "finished\n"
        assert interrupted is None
    assert {sig: signal.getsignal(sig) for sig in previous} == previous


@pytest.mark.parametrize("replace", [False, True])
def test_restore_candidate_path_changed_during_verification(
    monkeypatch, tmp_path: Path, replace: bool
) -> None:
    mock_helper(monkeypatch, recovery_report())
    output = tmp_path / "candidate.sqlite"
    report = tmp_path / "result.json"
    original_digest = hashlib.file_digest

    def digest(stream, algorithm):
        result = original_digest(stream, algorithm)
        output.unlink()
        if replace:
            output.write_bytes(b"another candidate")
        return result

    monkeypatch.setattr(sqlite_recovery.hashlib, "file_digest", digest)
    result = CliRunner().invoke(
        app,
        [
            "recover",
            "--source",
            "source",
            "--output",
            str(output),
            "--report",
            str(report),
        ],
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload == json.loads(report.read_text())
    assert payload["status"] == "failed"
    assert "Could not verify recovery candidate" in payload["error"]
    if replace:
        assert output.read_bytes() == b"another candidate"
    else:
        assert not output.exists()


def test_restore_dry_run_without_room_connection(monkeypatch) -> None:
    calls = mock_helper(monkeypatch, recovery_report("validated"))
    result = CliRunner().invoke(
        app, ["recover", "--source", "copied replica", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert calls[-1] == [
        "/test/recovery",
        "--source",
        "copied replica",
        "--dry-run",
    ]
    assert json.loads(result.output)["zero_filled_pages"] == [991]


def test_restore_writes_structured_report(monkeypatch, tmp_path: Path) -> None:
    calls = mock_helper(monkeypatch, recovery_report())
    report = tmp_path / "report.json"
    output = tmp_path / "restored.sqlite"
    result = CliRunner().invoke(
        app,
        [
            "recover",
            "--source",
            "source",
            "--output",
            str(output),
            "--report",
            str(report),
            "--txid",
            "00000000000000B4",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(report.read_text())["status"] == "restored"
    assert calls[-1][-2:] == ["--txid", "00000000000000b4"]


@pytest.mark.parametrize("replace_report", [False, True])
def test_restore_helper_launch_failure_preserves_report_path(
    monkeypatch, tmp_path: Path, replace_report: bool
):
    mock_helper(monkeypatch, recovery_report())
    original_run = sqlite_recovery.subprocess.run
    report = tmp_path / "result.json"

    def run(args, **kwargs):
        if "--version" in args:
            return original_run(args, **kwargs)
        assert report.exists()
        if replace_report:
            report.unlink()
            report.write_text("another operation's report")
        raise OSError(errno.ENOENT, "helper disappeared after probe")

    monkeypatch.setattr(sqlite_recovery.subprocess, "run", run)
    output = tmp_path / "candidate.sqlite"
    result = CliRunner().invoke(
        app,
        [
            "recover",
            "--source",
            "copy",
            "--output",
            str(output),
            "--report",
            str(report),
        ],
    )
    assert result.exit_code != 0
    assert "helper disappeared after probe" in result.output
    assert not output.exists()
    assert report.read_text() == (
        "another operation's report" if replace_report else ""
    )


@pytest.mark.parametrize("replace", [False, True])
def test_restore_report_path_changed_during_recovery(
    monkeypatch, tmp_path: Path, replace: bool
):
    mock_helper(monkeypatch, recovery_report())
    original_run = sqlite_recovery.subprocess.run
    report = tmp_path / "result.json"

    def run(args, **kwargs):
        result = original_run(args, **kwargs)
        if "--version" not in args:
            report.unlink()
            if replace:
                report.write_text("another result")
        return result

    monkeypatch.setattr(sqlite_recovery.subprocess, "run", run)
    output = tmp_path / "candidate.sqlite"
    result = CliRunner().invoke(
        app,
        [
            "recover",
            "--source",
            "copy",
            "--output",
            str(output),
            "--report",
            str(report),
        ],
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert "Could not save recovery report" in payload["error"]
    assert payload["output_path"] == str(output)
    assert output.read_bytes() == b"candidate"
    if replace:
        assert report.read_text() == "another result"
    else:
        assert not report.exists()


@pytest.mark.parametrize("failure_stage", ["file", "directory"])
def test_restore_report_sync_failure_preserves_candidate_metadata(
    monkeypatch, tmp_path: Path, failure_stage: str
) -> None:
    mock_helper(monkeypatch, recovery_report())
    output = tmp_path / "candidate.sqlite"
    report = tmp_path / "result.json"

    original_sync = sqlite_recovery.os.fsync
    reached = []

    def fail_sync(fd: int) -> None:
        stage = (
            "directory"
            if stat.S_ISDIR(sqlite_recovery.os.fstat(fd).st_mode)
            else "file"
        )
        reached.append(stage)
        if stage == failure_stage:
            raise OSError(errno.ENOSPC, "report storage full")
        original_sync(fd)

    monkeypatch.setattr(sqlite_recovery.os, "fsync", fail_sync)
    result = CliRunner().invoke(
        app,
        [
            "recover",
            "--source",
            "source",
            "--output",
            str(output),
            "--report",
            str(report),
        ],
    )
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert "report storage full" in payload["error"]
    assert payload["output_path"] == str(output)
    assert payload["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert output.read_bytes() == b"candidate"
    assert reached == (["file"] if failure_stage == "file" else ["file", "directory"])


@pytest.mark.parametrize("failure_stage", ["open", "read"])
def test_restore_candidate_io_failure_preserves_failed_report(
    monkeypatch, tmp_path: Path, failure_stage: str
) -> None:
    mock_helper(monkeypatch, recovery_report())
    output = tmp_path / "candidate.sqlite"
    report = tmp_path / "result.json"
    original_open = Path.open

    def fail_open(path, mode="r", *args, **kwargs):
        if path == output and mode == "rb":
            raise OSError(errno.EIO, "candidate read failed")
        return original_open(path, mode, *args, **kwargs)

    def fail_digest(*args, **kwargs):
        raise OSError(errno.EIO, "candidate read failed")

    with monkeypatch.context() as patch:
        if failure_stage == "open":
            patch.setattr(Path, "open", fail_open)
        else:
            patch.setattr(sqlite_recovery.hashlib, "file_digest", fail_digest)
        result = CliRunner().invoke(
            app,
            [
                "recover",
                "--source",
                "source",
                "--output",
                str(output),
                "--report",
                str(report),
            ],
        )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert "candidate read failed" in payload["error"]
    assert payload["output_path"] == str(output)
    assert payload["output_sha256"] == hashlib.sha256(b"candidate").hexdigest()
    assert json.loads(report.read_text()) == payload
    assert output.read_bytes() == b"candidate"


@pytest.mark.parametrize("destination", ["--output", "--report"])
def test_restore_never_overwrites(
    monkeypatch, tmp_path: Path, destination: str
) -> None:
    calls = mock_helper(monkeypatch, recovery_report())
    existing = tmp_path / "existing"
    existing.write_text("preserve")
    result = CliRunner().invoke(
        app,
        [
            "recover",
            "--source",
            "source",
            "--dry-run",
            destination,
            str(existing),
        ],
    )
    assert result.exit_code != 0
    assert existing.read_text() == "preserve"
    assert calls == []


def test_restore_failure_is_machine_readable(monkeypatch, tmp_path: Path) -> None:
    payload = recovery_report("failed")
    payload["error"] = "missing required SQLite page 2"
    mock_helper(monkeypatch, payload, returncode=1)
    report = tmp_path / "report.json"
    result = CliRunner().invoke(
        app,
        [
            "recover",
            "--source",
            "source",
            "--dry-run",
            "--report",
            str(report),
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["error"] == payload["error"]
    assert json.loads(report.read_text())["status"] == "failed"


def test_restore_rejects_silent_rollback(monkeypatch) -> None:
    payload = recovery_report("validated")
    payload["recovered_txid"] = "00000000000000b3"
    mock_helper(monkeypatch, payload)
    result = CliRunner().invoke(app, ["recover", "--source", "source", "--dry-run"])
    assert result.exit_code == 1


def test_restore_requires_compatible_helper(monkeypatch) -> None:
    monkeypatch.setattr(sqlite_recovery.shutil, "which", lambda _: None)
    result = CliRunner().invoke(app, ["recover", "--source", "source", "--dry-run"])
    assert result.exit_code != 0
    assert "not installed" in result.output


@pytest.mark.parametrize(
    "field,value",
    [
        ("output_sha256", "0" * 64),
        ("output_path", "/wrong/destination.sqlite"),
        ("error", "helper reported an internal failure"),
        ("sources", []),
        ("requested_txid", ""),
    ],
)
def test_restore_rejects_inconsistent_success(
    monkeypatch, tmp_path: Path, field, value
) -> None:
    payload = recovery_report()
    payload[field] = value
    mock_helper(monkeypatch, payload)
    result = CliRunner().invoke(
        app,
        [
            "recover",
            "--source",
            "source",
            "--output",
            str(tmp_path / "output.sqlite"),
        ],
    )
    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report["status"] == "failed"
    assert report["error"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", None),
        ("sha256", "not-a-hash"),
        ("min_txid", "0000000000000002"),
        ("min_txid", "invalid"),
        ("max_txid", "00000000000000b3"),
        ("max_txid", "00000000000000b5"),
        ("level", 10),
        ("size", 0),
        ("error", "source validation failed"),
    ],
)
def test_restore_rejects_incomplete_source_provenance(monkeypatch, field, value):
    payload = recovery_report("validated")
    payload["sources"][0][field] = value
    mock_helper(monkeypatch, payload)
    result = CliRunner().invoke(app, ["recover", "--source", "source", "--dry-run"])
    assert result.exit_code == 1
    assert json.loads(result.output)["status"] == "failed"


@pytest.mark.parametrize(
    "second_minimum,second_level,successful",
    [(100, 1, True), (102, 1, False), (100, 9, False)],
)
def test_restore_source_chain_allows_overlap_but_rejects_gaps(
    monkeypatch, second_minimum: int, second_level: int, successful: bool
) -> None:
    payload = recovery_report("validated")
    second = payload["sources"][0].copy()
    payload["sources"][0]["max_txid"] = f"{100:016x}"
    second["min_txid"] = f"{second_minimum:016x}"
    second["level"] = second_level
    payload["sources"].append(second)
    mock_helper(monkeypatch, payload)
    result = CliRunner().invoke(app, ["recover", "--source", "source", "--dry-run"])
    assert result.exit_code == (0 if successful else 1)
    assert json.loads(result.output)["status"] == (
        "validated" if successful else "failed"
    )


def test_bundled_helper_precedes_path(monkeypatch, tmp_path: Path) -> None:
    module = tmp_path / "meshagent" / "cli" / "sqlite_recovery.py"
    executable = (
        module.parent
        / "bin"
        / (
            "meshagent-sqlite-recover.exe"
            if sqlite_recovery.os.name == "nt"
            else "meshagent-sqlite-recover"
        )
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("test helper")
    executable.chmod(0o700)
    monkeypatch.setattr(sqlite_recovery, "__file__", str(module))
    monkeypatch.setattr(sqlite_recovery.shutil, "which", lambda _: "/unrelated/helper")
    assert sqlite_recovery._recovery_executable(None) == str(executable)
    assert sqlite_recovery._recovery_executable(tmp_path / "explicit") == str(
        tmp_path / "explicit"
    )


@pytest.mark.parametrize("helper_stdout", ["truncated {", '{"status":"restored"}'])
@pytest.mark.parametrize("returncode", [0, 1])
def test_restore_invalid_helper_report_preserves_unverified_candidate(
    monkeypatch, tmp_path: Path, helper_stdout: str, returncode: int
):
    mock_helper(monkeypatch, recovery_report())
    original_run = sqlite_recovery.subprocess.run

    def run(args, **kwargs):
        result = original_run(args, **kwargs)
        if "--version" not in args:
            return subprocess.CompletedProcess(
                args, returncode, helper_stdout, "helper stopped"
            )
        return result

    monkeypatch.setattr(sqlite_recovery.subprocess, "run", run)
    output = tmp_path / "candidate.sqlite"
    report_path = tmp_path / "result.json"
    result = CliRunner().invoke(
        app,
        [
            "recover",
            "--source",
            "copy",
            "--output",
            str(output),
            "--report",
            str(report_path),
        ],
    )
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert json.loads(report_path.read_text()) == report
    assert report["status"] == "failed"
    assert "unverified" in report["error"]
    assert str(output) in report["error"]
    assert "output_path" not in report
    assert "output_sha256" not in report
    assert "recovered_txid" not in report
    assert report["sources"] == []
    assert output.read_bytes() == b"candidate"


@pytest.mark.parametrize("stream", [1, 2])
def test_restore_invalid_utf8_helper_output_is_structured_failure(
    tmp_path: Path, stream: int
):
    import sys

    helper = tmp_path / "synthetic-helper"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('{\"recovery_protocol\":1}')\n"
        "else:\n"
        "    output = Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "    output.write_bytes(b'candidate')\n"
        f"    report = {recovery_report()!r}\n"
        "    report['output_path'] = str(output)\n"
        + ("    print(json.dumps(report), flush=True)\n" if stream == 2 else "")
        + f"    os.write({stream}, b'\\xe2\\x82')\n"
    )
    helper.chmod(0o700)
    output = tmp_path / "candidate.sqlite"
    report_path = tmp_path / "result.json"
    result = CliRunner().invoke(
        app,
        [
            "recover",
            "--source",
            "copy",
            "--output",
            str(output),
            "--report",
            str(report_path),
            "--recovery-tool",
            str(helper),
        ],
    )
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert json.loads(report_path.read_text()) == report
    assert report["status"] == "failed"
    assert "UTF-8" in report["error"]
    assert "unverified" in report["error"]
    assert "output_sha256" not in report
    assert "recovered_txid" not in report
    assert output.read_bytes() == b"candidate"


def test_native_recovery_cli(tmp_path: Path) -> None:
    """Real helper and optional frozen CLI; CI must supply the fixture and tool."""
    import os
    import sqlite3
    import sys

    fixture = os.environ.get("MESHAGENT_SQLITE_CLI_FIXTURE")
    packaged_cli = os.environ.get("MESHAGENT_SQLITE_PACKAGED_CLI")
    helper = os.environ.get("MESHAGENT_SQLITE_RECOVERY_HELPER")
    if fixture is None or (packaged_cli is None and helper is None):
        pytest.skip("run scripts/test-sqlite-recovery-cli.sh for native coverage")
    source = Path(fixture) / "replica"
    command = (
        [packaged_cli] if packaged_cli else [sys.executable, "-m", "meshagent.cli.cli"]
    )
    command += ["room", "sqlite", "recover", "--source", str(source)]
    if packaged_cli is None:
        command += ["--recovery-tool", helper]
    execution_env = os.environ.copy()
    if packaged_cli:
        # The frozen distribution must find its bundled helper without PATH.
        execution_env["PATH"] = str(tmp_path / "empty-path")
    before = {
        str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source.rglob("*.ltx")
    }
    dry_run = subprocess.run(
        [*command, "--dry-run"],
        capture_output=True,
        text=True,
        timeout=120,
        env=execution_env,
    )
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    plan = json.loads(dry_run.stdout)
    assert plan["status"] == "validated"
    assert len(plan["rejected"]) == 1
    assert plan["recovered_txid"] == "0000000000000001"
    output = tmp_path / "restored database.sqlite"
    report_path = tmp_path / "recovery report.json"
    arguments = [
        *command,
        "--output",
        str(output),
        "--report",
        str(report_path),
        "--txid",
        plan["requested_txid"],
        "--expected-sha256",
        plan["output_sha256"],
    ]
    restored = subprocess.run(
        arguments, capture_output=True, text=True, timeout=120, env=execution_env
    )
    assert restored.returncode == 0, restored.stdout + restored.stderr
    report = json.loads(restored.stdout)
    assert json.loads(report_path.read_text()) == report
    assert report["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    with sqlite3.connect(f"{output.as_uri()}?mode=ro", uri=True) as db:
        assert db.execute(
            "SELECT body,revision FROM documents WHERE id='p1'"
        ).fetchone() == (f"{1:05000d}", 28)
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    repeated = subprocess.run(
        arguments, capture_output=True, text=True, timeout=120, env=execution_env
    )
    assert repeated.returncode != 0
    assert hashlib.sha256(output.read_bytes()).hexdigest() == report["output_sha256"]
    mismatched_output = tmp_path / "mismatched candidate.sqlite"
    mismatch = subprocess.run(
        [*command, "--output", str(mismatched_output), "--expected-sha256", "0" * 64],
        capture_output=True,
        text=True,
        timeout=120,
        env=execution_env,
    )
    assert mismatch.returncode == 1, mismatch.stdout + mismatch.stderr
    mismatch_report = json.loads(mismatch.stdout)
    assert mismatch_report["status"] == "failed"
    assert "differs from the expected value" in mismatch_report["error"]
    assert mismatch_report["output_path"] == str(mismatched_output)
    assert mismatch_report["output_sha256"] == plan["output_sha256"]
    assert mismatched_output.read_bytes() == output.read_bytes()
    # Replace a private replica copy with valid history for different contents
    # at the same TXID. The original plan must not authorize these new bytes.
    changed_source = tmp_path / "changed replica"
    shutil.copytree(source, changed_source)
    shutil.copytree(
        Path(fixture) / "changed" / "replica", changed_source, dirs_exist_ok=True
    )
    changed_before = {path: path.read_bytes() for path in changed_source.rglob("*.ltx")}
    changed_output = tmp_path / "changed candidate.sqlite"
    changed_report_path = tmp_path / "changed result.json"
    changed_command = command.copy()
    changed_command[changed_command.index("--source") + 1] = str(changed_source)
    changed = subprocess.run(
        [
            *changed_command,
            "--txid",
            plan["requested_txid"],
            "--expected-sha256",
            plan["output_sha256"],
            "--output",
            str(changed_output),
            "--report",
            str(changed_report_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=execution_env,
    )
    assert changed.returncode == 1, changed.stdout + changed.stderr
    changed_report = json.loads(changed.stdout)
    assert json.loads(changed_report_path.read_text()) == changed_report
    assert changed_report["status"] == "failed"
    assert "differs from the expected value" in changed_report["error"]
    assert changed_report["recovered_txid"] == plan["requested_txid"]
    assert changed_report["output_sha256"] != plan["output_sha256"]
    assert (
        changed_report["output_sha256"]
        == hashlib.sha256(changed_output.read_bytes()).hexdigest()
    )
    with sqlite3.connect(f"{changed_output.as_uri()}?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute(
            "SELECT body,revision FROM documents WHERE id='p1'"
        ).fetchone() == (f"{2:05000d}", 29)
    assert changed_before == {
        path: path.read_bytes() for path in changed_source.rglob("*.ltx")
    }
    unreachable_source = tmp_path / "unreachable latest replica"
    shutil.copytree(source, unreachable_source)
    bad_latest = unreachable_source / "ltx/9/0000000000000001-0000000000000002.ltx"
    bad_latest.parent.mkdir(parents=True, exist_ok=True)
    bad_latest.write_bytes(b"truncated synthetic latest snapshot")
    unreachable_before = {
        path: path.read_bytes() for path in unreachable_source.rglob("*.ltx")
    }
    latest_command = command.copy()
    latest_command[latest_command.index("--source") + 1] = str(unreachable_source)
    latest_output = tmp_path / "latest candidate.sqlite"
    latest_result = subprocess.run(
        [*latest_command, "--output", str(latest_output)],
        capture_output=True,
        text=True,
        timeout=120,
        env=execution_env,
    )
    assert latest_result.returncode == 1, latest_result.stdout + latest_result.stderr
    latest_report = json.loads(latest_result.stdout)
    assert latest_report["status"] == "failed"
    assert latest_report["requested_txid"] == "0000000000000002"
    assert "recovered_txid" not in latest_report
    assert not latest_output.exists()
    earlier_output = tmp_path / "explicit earlier candidate.sqlite"
    earlier = subprocess.run(
        [
            *latest_command,
            "--txid",
            plan["requested_txid"],
            "--output",
            str(earlier_output),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=execution_env,
    )
    assert earlier.returncode == 0, earlier.stdout + earlier.stderr
    assert json.loads(earlier.stdout)["recovered_txid"] == plan["requested_txid"]
    assert earlier_output.read_bytes() == output.read_bytes()
    assert unreachable_before == {
        path: path.read_bytes() for path in unreachable_source.rglob("*.ltx")
    }
    assert before == {
        str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source.rglob("*.ltx")
    }


@pytest.mark.parametrize("valid_source", [True, False])
def test_native_recovery_cli_documented_automation(tmp_path: Path, valid_source: bool):
    """Run the guide's actual shell block against copied synthetic history."""
    import os
    import sqlite3
    import sys

    fixture = os.environ.get("MESHAGENT_SQLITE_CLI_FIXTURE")
    packaged_cli = os.environ.get("MESHAGENT_SQLITE_PACKAGED_CLI")
    helper = os.environ.get("MESHAGENT_SQLITE_RECOVERY_HELPER")
    if fixture is None or (packaged_cli is None and helper is None):
        pytest.skip("run scripts/test-sqlite-recovery-cli.sh for native coverage")
    guide = (
        Path(__file__).resolve().parents[3]
        / "meshagent-docs/room_api/sqlite-recovery.mdx"
    ).read_text()
    section = guide.split("## Automate candidate creation on Linux\n", 1)[1]
    script = section.split("```sh\n", 1)[1].split("```", 1)[0]
    source = tmp_path / "copied-replica"
    shutil.copytree(Path(fixture) / "replica", source)
    if not valid_source:
        # Keep the inventory but remove all usable page data, including the
        # valid baseline. Planning must stop the shell before candidate creation.
        for path in source.rglob("*.ltx"):
            path.write_bytes(b"truncated synthetic object")
    before = {path: path.read_bytes() for path in source.rglob("*.ltx")}
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("jq", "mktemp"):
        executable = shutil.which(name)
        assert executable, f"documented automation requires {name}"
        (binaries / name).symlink_to(Path(executable).resolve())
    command = (
        [packaged_cli] if packaged_cli else [sys.executable, "-m", "meshagent.cli.cli"]
    )
    launcher = binaries / "meshagent"
    launcher.write_text("#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n')
    launcher.chmod(0o700)
    if not packaged_cli:
        (binaries / "meshagent-sqlite-recover").symlink_to(Path(helper).resolve())
    env = os.environ.copy()
    # Keep shell utilities available without allowing an unrelated host helper
    # to conceal a missing helper in the frozen distribution.
    env["PATH"] = str(binaries)
    result = subprocess.run(
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    attempts = list(tmp_path.glob("sqlite-recovery.*"))
    assert len(attempts) == 1, result.stdout + result.stderr
    attempt = attempts[0]
    plan = json.loads((attempt / "plan.json").read_text())
    candidate = attempt / "candidate.sqlite"
    if valid_source:
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads((attempt / "result.json").read_text())
        assert plan["status"] == "validated"
        assert report["status"] == "restored"
        assert report["recovered_txid"] == plan["requested_txid"]
        assert report["output_sha256"] == plan["output_sha256"]
        assert (
            hashlib.sha256(candidate.read_bytes()).hexdigest() == plan["output_sha256"]
        )
        with sqlite3.connect(f"{candidate.as_uri()}?mode=ro", uri=True) as db:
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert db.execute(
                "SELECT body,revision FROM documents WHERE id='p1'"
            ).fetchone() == (f"{1:05000d}", 28)
    else:
        assert result.returncode != 0
        assert plan["status"] == "failed"
        assert not candidate.exists()
        assert not (attempt / "result.json").exists()
    assert before == {path: path.read_bytes() for path in source.rglob("*.ltx")}


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("matches", [False, True])
def test_restore_enforces_planned_database_hash(
    monkeypatch, tmp_path, dry_run, matches
):
    payload = recovery_report("validated" if dry_run else "restored")
    calls = mock_helper(monkeypatch, payload)
    output = tmp_path / "candidate.sqlite"
    report = tmp_path / "result.json"
    expected = payload["output_sha256"].upper() if matches else "0" * 64
    arguments = [
        "recover",
        "--source",
        "copy",
        "--expected-sha256",
        expected,
        "--report",
        str(report),
    ]
    arguments.extend(["--dry-run"] if dry_run else ["--output", str(output)])
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == (0 if matches else 1), result.output
    saved = json.loads(report.read_text())
    assert saved == json.loads(result.stdout)
    assert saved["output_sha256"] == payload["output_sha256"]
    assert "--expected-sha256" not in calls[-1]
    if matches:
        assert saved["status"] == payload["status"]
    else:
        assert saved["status"] == "failed"
        assert "differs from the expected value" in saved["error"]
        assert expected in saved["error"]
    if dry_run:
        assert not output.exists()
    else:
        assert output.read_bytes() == b"candidate"
        assert saved["output_path"] == str(output)


@pytest.mark.parametrize("value", ["", "a" * 63, "a" * 65, "g" * 64])
def test_restore_rejects_invalid_expected_hash(monkeypatch, value):
    calls = mock_helper(monkeypatch, recovery_report("validated"))
    result = CliRunner().invoke(
        app,
        [
            "recover",
            "--source",
            "copy",
            "--dry-run",
            "--expected-sha256",
            value,
        ],
    )
    assert result.exit_code == 2
    assert not calls
