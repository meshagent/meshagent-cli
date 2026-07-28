from __future__ import annotations

from meshagent.api.messaging import JsonContent
from meshagent.cli.tool_call_summary import (
    ParsedCommand,
    format_tool_call_summary,
    parse_command,
)


def test_parse_command_collapses_unknown_pipeline() -> None:
    command = (
        "rg -l QkBindingController presentation/src/main/java | "
        "xargs perl -pi -e 's/QkBindingController/QkController/g'"
    )

    assert parse_command(command) == [
        ParsedCommand(
            kind="unknown",
            cmd=(
                "rg -l QkBindingController presentation/src/main/java '|' "
                "xargs perl -pi -e s/QkBindingController/QkController/g"
            ),
        )
    ]


def test_parse_command_supports_cd_then_cat() -> None:
    assert parse_command("cd foo && cat foo.txt") == [
        ParsedCommand(
            kind="read",
            cmd="cat foo.txt",
            name="foo.txt",
            path="foo/foo.txt",
        )
    ]


def test_parse_command_supports_rg_files_with_path_and_pipe() -> None:
    assert parse_command(["bash", "-lc", "rg --files webview/src | sed -n"]) == [
        ParsedCommand(
            kind="list_files",
            cmd="rg --files webview/src",
            path="webview",
        )
    ]


def test_parse_command_supports_nl_then_sed_reading() -> None:
    command = "nl -ba core/src/parse_command.rs | sed -n '1200,1720p'"

    assert parse_command(["bash", "-lc", command]) == [
        ParsedCommand(
            kind="read",
            cmd=command,
            name="parse_command.rs",
            path="core/src/parse_command.rs",
        )
    ]


def test_parse_command_supports_searching() -> None:
    assert parse_command("rg -n 'foo bar' -S") == [
        ParsedCommand(
            kind="search",
            cmd="rg -n 'foo bar' -S",
            query="foo bar",
        )
    ]


def test_format_tool_call_summary_renders_explored_lines() -> None:
    summary = format_tool_call_summary(
        toolkit="",
        tool="shell",
        arguments={"command": "cat a.py && cat b.py && rg TODO src"},
    )

    assert summary == "Explored\n  └ Read a.py, b.py\n    Search TODO in src"


def test_format_tool_call_summary_supports_openai_shell_action_command() -> None:
    summary = format_tool_call_summary(
        toolkit="openai",
        tool="shell",
        arguments={"action": {"command": ["cat", "a.py"]}},
    )

    assert summary == "Explored\n  └ Read a.py"


def test_format_tool_call_summary_supports_openai_local_shell_action_command() -> None:
    summary = format_tool_call_summary(
        toolkit="openai",
        tool="local_shell",
        arguments={"action": {"command": "rg TODO src"}},
    )

    assert summary == "Explored\n  └ Search TODO in src"


def test_format_tool_call_summary_keeps_unknown_as_ran() -> None:
    summary = format_tool_call_summary(
        toolkit="",
        tool="shell",
        arguments={"command": "pytest tests"},
    )

    assert summary == "Ran pytest tests"


def test_format_tool_call_summary_uses_storage_friendly_items() -> None:
    assert (
        format_tool_call_summary(
            toolkit="storage",
            tool="read_file",
            arguments={"path": "/src/report.html"},
        )
        == "Read file: /src/report.html"
    )
    assert (
        format_tool_call_summary(
            toolkit="storage",
            tool="write_file",
            arguments={"path": "/src/report.html"},
        )
        == "Wrote file: /src/report.html"
    )


def test_format_tool_call_summary_uses_image_generation_paths() -> None:
    assert (
        format_tool_call_summary(
            toolkit="image-generation",
            tool="imagegen",
            arguments={"prompt": "draw a fox"},
        )
        == "Generated image"
    )
    assert (
        format_tool_call_summary(
            toolkit="image-generation",
            tool="import_image",
            arguments={"source_path": "/in/photo.webp"},
        )
        == "Imported image from: /in/photo.webp"
    )
    assert (
        format_tool_call_summary(
            toolkit="image-generation",
            tool="export_image",
            arguments={"id": "image-1", "destination_path": "/out/photo.png"},
            result=JsonContent(
                json={
                    "exported_image_id": "image-1",
                    "destination_path": "/out/photo-converted.png",
                }
            ),
        )
        == "Exported image to: /out/photo-converted.png"
    )


def test_format_tool_call_summary_uses_in_progress_wording_before_completion() -> None:
    assert (
        format_tool_call_summary(
            toolkit="storage",
            tool="write_file",
            arguments=None,
            completed=False,
        )
        == "Writing file"
    )
    assert (
        format_tool_call_summary(
            toolkit="storage",
            tool="write_file",
            arguments={"path": "/src/report.html"},
            completed=False,
        )
        == "Writing file: /src/report.html"
    )
    assert (
        format_tool_call_summary(
            toolkit="",
            tool="shell",
            arguments=None,
            completed=False,
        )
        == "Running shell"
    )
    assert (
        format_tool_call_summary(
            toolkit="openai",
            tool="shell",
            arguments=None,
            completed=False,
        )
        == "Running commands"
    )


def test_format_tool_call_summary_uses_dataset_friendly_items() -> None:
    assert (
        format_tool_call_summary(
            toolkit="dataset",
            tool="execute_sql",
            arguments={"query": "SELECT *\nFROM food"},
        )
        == "Ran SQL: SELECT * FROM food"
    )
    assert (
        format_tool_call_summary(
            toolkit="dataset",
            tool="advanced_search_food",
            arguments={"where": "name = 'apple'"},
        )
        == "Searched dataset: food"
    )


def test_format_tool_call_summary_uses_datetime_and_web_friendly_items() -> None:
    assert (
        format_tool_call_summary(
            toolkit="datetime",
            tool="now",
            arguments={"tz": "America/Los_Angeles"},
        )
        == "Checked current time: America/Los_Angeles"
    )
    assert (
        format_tool_call_summary(
            toolkit="web_fetch",
            tool="web_fetch",
            arguments={"url": "https://example.com"},
        )
        == "Fetched URL: https://example.com"
    )


def test_format_tool_call_summary_uses_container_friendly_items_and_commands() -> None:
    assert (
        format_tool_call_summary(
            toolkit="container",
            tool="start_container",
            arguments={"image": "python:3.13"},
        )
        == "Started container"
    )
    assert (
        format_tool_call_summary(
            toolkit="container",
            tool="run_in_container",
            arguments={"commands": ["cat /src/report.html"]},
        )
        == "Explored\n  └ Read report.html"
    )


def test_format_tool_call_summary_uses_chat_and_mail_friendly_items() -> None:
    assert (
        format_tool_call_summary(
            toolkit="chat",
            tool="attach_file",
            arguments={"path": "/src/report.html"},
        )
        == "Attached file: /src/report.html"
    )
    assert (
        format_tool_call_summary(
            toolkit="mail",
            tool="new_email_thread",
            arguments={"subject": "Food report"},
        )
        == "Started email thread: Food report"
    )
