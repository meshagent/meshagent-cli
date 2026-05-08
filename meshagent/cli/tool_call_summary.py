from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import PurePath
from typing import Literal, Sequence

ParsedCommandKind = Literal["read", "list_files", "search", "unknown"]

_CONNECTORS = {"&&", "||", "|", ";"}
_SHELL_NAMES = {"bash", "zsh", "sh"}
_POWERSHELL_NAMES = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
_SHELL_TOOLS = {"shell", "local_shell", "code_interpreter"}
_CONTAINER_COMMAND_TOOLS = {"container_shell", "process_shell", "run_in_container"}
_COMMAND_TOOLS = _SHELL_TOOLS | _CONTAINER_COMMAND_TOOLS


@dataclass(frozen=True)
class ParsedCommand:
    kind: ParsedCommandKind
    cmd: str
    name: str | None = None
    path: str | None = None
    query: str | None = None


def parse_command(command: Sequence[str] | str) -> list[ParsedCommand]:
    tokens = _coerce_command_tokens(command)
    parsed = _parse_command_impl(tokens)
    deduped: list[ParsedCommand] = []
    for item in parsed:
        if len(deduped) > 0 and deduped[-1] == item:
            continue
        deduped.append(item)
    if any(item.kind == "unknown" for item in deduped):
        return [_single_unknown_for_command(tokens)]
    return deduped


def format_tool_call_summary(
    *,
    toolkit: str,
    tool: str,
    arguments: dict[str, object] | None,
    failed: bool = False,
    completed: bool = True,
) -> str:
    label = tool_call_label(toolkit=toolkit, tool=tool, arguments=arguments)
    friendly = _friendly_builtin_summary(
        toolkit=toolkit,
        tool=tool,
        arguments=arguments,
        failed=failed,
        completed=completed,
    )
    if friendly is not None:
        return friendly

    normalized_tool = tool.strip().casefold()
    normalized_toolkit = toolkit.strip().casefold()
    if (
        not completed
        and not failed
        and normalized_toolkit == "openai"
        and normalized_tool in _SHELL_TOOLS
        and arguments is None
    ):
        return "Running commands"
    if failed or normalized_tool not in _COMMAND_TOOLS or arguments is None:
        return f"{'Failed' if failed else ('Ran' if completed else 'Running')} {label}"

    commands = _command_arguments(tool=normalized_tool, arguments=arguments)
    if len(commands) == 0:
        return f"{'Failed' if failed else ('Ran' if completed else 'Running')} {label}"

    parsed: list[ParsedCommand] = []
    for command in commands:
        parsed.extend(parse_command(command))
    if len(parsed) == 0 or any(item.kind == "unknown" for item in parsed):
        return f"{'Ran' if completed else 'Running'} {label}"

    lines = ["Explored"]
    for line in _exploring_detail_lines(parsed):
        lines.append(f"  {line}")
    return "\n".join(lines)


def tool_call_label(
    *,
    toolkit: str,
    tool: str,
    arguments: dict[str, object] | None,
) -> str:
    normalized_tool = tool.strip()
    normalized_toolkit = toolkit.strip()
    if normalized_tool.casefold() in _COMMAND_TOOLS and arguments is not None:
        commands = _command_arguments(
            tool=normalized_tool.casefold(),
            arguments=arguments,
        )
        if len(commands) > 0:
            return " && ".join(_command_label(command) for command in commands)
    if normalized_toolkit != "" and normalized_toolkit != normalized_tool:
        return f"{normalized_toolkit}: {normalized_tool or 'tool'}"
    return normalized_tool or "tool"


def _friendly_builtin_summary(
    *,
    toolkit: str,
    tool: str,
    arguments: dict[str, object] | None,
    failed: bool,
    completed: bool,
) -> str | None:
    normalized_toolkit = toolkit.strip().casefold()
    normalized_tool = tool.strip().casefold()
    if arguments is None:
        arguments = {}

    summary: str | None = None
    if normalized_toolkit == "storage":
        summary = _storage_summary(
            tool=normalized_tool,
            arguments=arguments,
            completed=completed,
        )
    elif normalized_toolkit == "dataset":
        summary = _dataset_summary(
            tool=normalized_tool,
            arguments=arguments,
            completed=completed,
        )
    elif normalized_toolkit in {"datetime", "time"}:
        summary = _datetime_summary(
            tool=normalized_tool,
            arguments=arguments,
            completed=completed,
        )
    elif normalized_toolkit == "web_fetch":
        summary = _web_fetch_summary(
            tool=normalized_tool,
            arguments=arguments,
            completed=completed,
        )
    elif normalized_toolkit == "container":
        summary = _container_summary(tool=normalized_tool, completed=completed)
    elif normalized_toolkit == "chat":
        summary = _chat_summary(
            tool=normalized_tool,
            arguments=arguments,
            completed=completed,
        )
    elif normalized_toolkit in {"mail", "email", "emails"}:
        summary = _mail_summary(
            tool=normalized_tool,
            arguments=arguments,
            completed=completed,
        )

    if summary is None:
        return None
    if failed:
        return f"Failed: {summary}"
    return summary


def _storage_summary(
    *,
    tool: str,
    arguments: dict[str, object],
    completed: bool,
) -> str | None:
    path = _string_argument(arguments, "path")
    if tool == "read_file":
        return _with_optional_suffix(
            "Read file" if completed else "Reading file",
            path,
        )
    if tool == "grep_file":
        pattern = _string_argument(arguments, "pattern")
        if pattern is not None and path is not None:
            return f"{'Searched' if completed else 'Searching'} {path} for {pattern}"
        return _with_optional_suffix(
            "Searched file" if completed else "Searching file",
            path,
        )
    if tool == "write_file":
        return _with_optional_suffix(
            "Wrote file" if completed else "Writing file",
            path,
        )
    if tool == "get_file_download_url":
        return _with_optional_suffix(
            "Prepared download" if completed else "Preparing download",
            path,
        )
    if tool == "list_files_in_room":
        return _with_optional_suffix(
            "Listed files" if completed else "Listing files",
            path,
        )
    if tool == "save_file_from_url":
        url = _string_argument(arguments, "url")
        if path is not None:
            return f"{'Saved' if completed else 'Saving'} file to {path}"
        return _with_optional_suffix(
            "Saved file from URL" if completed else "Saving file from URL",
            url,
        )
    return None


def _dataset_summary(
    *,
    tool: str,
    arguments: dict[str, object],
    completed: bool,
) -> str | None:
    if tool == "list_tables":
        return "Listed dataset tables" if completed else "Listing dataset tables"
    if tool == "execute_sql":
        query = _string_argument(arguments, "query")
        prefix = "Ran SQL" if completed else "Running SQL"
        if query is not None:
            return f"{prefix}: {_single_line(query)}"
        return prefix

    table = _dataset_table_from_tool(tool=tool)
    if tool.startswith("insert_") and tool.endswith("_rows"):
        return _with_optional_suffix(
            "Inserted dataset rows" if completed else "Inserting dataset rows",
            table,
        )
    if tool.startswith("update_") and tool.endswith("_rows"):
        return _with_optional_suffix(
            "Updated dataset rows" if completed else "Updating dataset rows",
            table,
        )
    if tool.startswith("delete_") and tool.endswith("_rows"):
        return _with_optional_suffix(
            "Deleted dataset rows" if completed else "Deleting dataset rows",
            table,
        )
    if tool.startswith("advanced_delete_"):
        return _with_optional_suffix(
            "Deleted dataset rows" if completed else "Deleting dataset rows",
            table,
        )
    if tool.startswith("search_") or tool.startswith("advanced_search_"):
        return _with_optional_suffix(
            "Searched dataset" if completed else "Searching dataset",
            table,
        )
    if tool.startswith("count_"):
        return _with_optional_suffix(
            "Counted dataset rows" if completed else "Counting dataset rows",
            table,
        )
    if tool.startswith("spawn_task_for_each_") and tool.endswith("_row"):
        return _with_optional_suffix(
            "Queued tasks for dataset rows"
            if completed
            else "Queueing tasks for dataset rows",
            table,
        )
    return None


def _datetime_summary(
    *,
    tool: str,
    arguments: dict[str, object],
    completed: bool,
) -> str | None:
    tz = _string_argument(arguments, "tz", "assume_tz")
    if tool == "now":
        return _with_optional_suffix(
            "Checked current time" if completed else "Checking current time",
            tz,
        )
    if tool == "today_range":
        return _with_optional_suffix(
            "Checked today" if completed else "Checking today", tz
        )
    if tool == "week_range":
        return _with_optional_suffix(
            "Checked week range" if completed else "Checking week range",
            tz,
        )
    if tool == "month_range":
        return _with_optional_suffix(
            "Checked month range" if completed else "Checking month range",
            tz,
        )
    if tool == "add_duration":
        return "Added duration" if completed else "Adding duration"
    if tool == "diff":
        return "Compared datetimes" if completed else "Comparing datetimes"
    if tool == "parse_iso":
        return "Parsed datetime" if completed else "Parsing datetime"
    if tool == "format_dt":
        return "Formatted datetime" if completed else "Formatting datetime"
    if tool == "to_utc_z":
        return (
            "Converted datetime to UTC" if completed else "Converting datetime to UTC"
        )
    return None


def _web_fetch_summary(
    *,
    tool: str,
    arguments: dict[str, object],
    completed: bool,
) -> str | None:
    url = _string_argument(arguments, "url")
    if tool == "web_fetch":
        return _with_optional_suffix(
            "Fetched URL" if completed else "Fetching URL", url
        )
    if tool == "web_grep":
        pattern = _string_argument(arguments, "pattern")
        if pattern is not None and url is not None:
            return f"{'Searched' if completed else 'Searching'} {url} for {pattern}"
        return _with_optional_suffix(
            "Searched URL" if completed else "Searching URL",
            url,
        )
    return None


def _container_summary(*, tool: str, completed: bool) -> str | None:
    if tool == "list_managed_containers":
        return "Listed containers" if completed else "Listing containers"
    if tool == "start_container":
        return "Started container" if completed else "Starting container"
    if tool == "stop_managed_container":
        return "Stopped container" if completed else "Stopping container"
    return None


def _chat_summary(
    *,
    tool: str,
    arguments: dict[str, object],
    completed: bool,
) -> str | None:
    if tool == "new_thread":
        return "Started chat thread" if completed else "Starting chat thread"
    if tool == "attach_file":
        return _with_optional_suffix(
            "Attached file" if completed else "Attaching file",
            _string_argument(arguments, "path"),
        )
    if tool == "list_threads":
        return "Listed chat threads" if completed else "Listing chat threads"
    if tool == "grep_thread_list":
        return _with_optional_suffix(
            "Searched chat threads" if completed else "Searching chat threads",
            _string_argument(arguments, "pattern"),
        )
    if tool.startswith("run_") and tool.endswith("_task"):
        return _with_optional_suffix(
            "Sent task" if completed else "Sending task",
            _string_argument(arguments, "prompt"),
        )
    return None


def _mail_summary(
    *,
    tool: str,
    arguments: dict[str, object],
    completed: bool,
) -> str | None:
    if tool == "new_email_thread":
        subject = _string_argument(arguments, "subject")
        prefix = "Started email thread" if completed else "Starting email thread"
        if subject is not None:
            return f"{prefix}: {subject}"
        return prefix
    if tool in {"attach_file", "attach file"}:
        return _with_optional_suffix(
            "Attached file" if completed else "Attaching file",
            _string_argument(arguments, "path"),
        )
    return None


def _exploring_detail_lines(commands: list[ParsedCommand]) -> list[str]:
    lines: list[str] = []
    read_names: list[str] = []
    for command in commands:
        if command.kind == "read":
            if command.name is not None and command.name not in read_names:
                read_names.append(command.name)
            continue
        if len(read_names) > 0:
            lines.append(f"Read {', '.join(read_names)}")
            read_names = []
        if command.kind == "list_files":
            if command.path is not None:
                lines.append(f"List {command.path}")
            else:
                lines.append("List files")
        elif command.kind == "search":
            if command.query is not None and command.path is not None:
                lines.append(f"Search {command.query} in {command.path}")
            elif command.query is not None:
                lines.append(f"Search {command.query}")
            elif command.path is not None:
                lines.append(f"Search in {command.path}")
            else:
                lines.append("Search")
    if len(read_names) > 0:
        lines.append(f"Read {', '.join(read_names)}")
    return [
        f"└ {line}" if index == 0 else f"  {line}" for index, line in enumerate(lines)
    ]


def _shell_command_argument(arguments: dict[str, object]) -> str | list[str] | None:
    for key in ("command", "cmd", "code"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip() != "":
            return value.strip()
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return [*value]
    action = arguments.get("action")
    if isinstance(action, dict):
        command = action.get("command")
        if isinstance(command, str) and command.strip() != "":
            return command.strip()
        if isinstance(command, list) and all(isinstance(item, str) for item in command):
            return [*command]
    return None


def _command_arguments(
    *, tool: str, arguments: dict[str, object]
) -> list[str | list[str]]:
    if tool in _SHELL_TOOLS:
        command = _shell_command_argument(arguments)
        if command is None:
            return []
        return [command]

    if tool in _CONTAINER_COMMAND_TOOLS:
        commands = arguments.get("commands")
        if isinstance(commands, list) and all(
            isinstance(item, str) for item in commands
        ):
            return [item.strip() for item in commands if item.strip() != ""]
        command = _shell_command_argument(arguments)
        if command is None:
            return []
        return [command]

    return []


def _command_label(command: str | list[str]) -> str:
    if isinstance(command, str):
        return command
    return _shlex_join(command)


def _string_argument(arguments: dict[str, object], *names: str) -> str | None:
    for name in names:
        value = arguments.get(name)
        if isinstance(value, str) and value.strip() != "":
            return _single_line(value.strip())
    return None


def _with_optional_suffix(prefix: str, suffix: str | None) -> str:
    if suffix is None:
        return prefix
    return f"{prefix}: {suffix}"


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _dataset_table_from_tool(*, tool: str) -> str | None:
    patterns = (
        (r"^insert_(?P<table>.+)_rows$", "table"),
        (r"^update_(?P<table>.+)_rows$", "table"),
        (r"^delete_(?P<table>.+)_rows$", "table"),
        (r"^advanced_delete_(?P<table>.+)$", "table"),
        (r"^advanced_search_(?P<table>.+)$", "table"),
        (r"^search_(?P<table>.+)$", "table"),
        (r"^count_(?P<table>.+)$", "table"),
        (r"^spawn_task_for_each_(?P<table>.+)_row$", "table"),
    )
    for pattern, group_name in patterns:
        match = re.match(pattern, tool)
        if match is not None:
            return match.group(group_name)
    return None


def _coerce_command_tokens(command: Sequence[str] | str) -> list[str]:
    if isinstance(command, str):
        return _shlex_split(command)
    return [str(item) for item in command]


def _shlex_split(script: str) -> list[str]:
    try:
        lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        try:
            return shlex.split(script)
        except ValueError:
            return script.split()


def _shlex_join(tokens: Sequence[str]) -> str:
    try:
        return shlex.join(list(tokens))
    except ValueError:
        return "<command included NUL byte>"


def _parse_command_impl(command: list[str]) -> list[ParsedCommand]:
    shell_command = _extract_shell_command(command)
    if shell_command is not None:
        return _parse_shell_script(shell_command)

    powershell_command = _extract_powershell_command(command)
    if powershell_command is not None:
        return [ParsedCommand(kind="unknown", cmd=powershell_command)]

    normalized = _normalize_tokens(command)
    parts = (
        _split_on_connectors(normalized)
        if _contains_connectors(normalized)
        else [normalized]
    )
    commands: list[ParsedCommand] = []
    cwd: str | None = None
    for tokens in parts:
        if len(tokens) == 0:
            continue
        if tokens[0] == "cd":
            target = _cd_target(tokens[1:])
            if target is not None:
                cwd = _join_paths(cwd, target)
            continue
        parsed = _summarize_main_tokens(tokens)
        commands.append(_with_cwd(parsed, cwd))

    return _simplify(commands)


def _parse_shell_script(script: str) -> list[ParsedCommand]:
    tokens = _shlex_split(script)
    if len(tokens) == 0:
        return [ParsedCommand(kind="unknown", cmd=script)]
    parts = _split_on_connectors(tokens) if _contains_connectors(tokens) else [tokens]
    had_connectors = len(parts) > 1
    filtered = _drop_small_formatting_commands(parts)
    if len(filtered) == 0:
        return [ParsedCommand(kind="unknown", cmd=script)]

    commands: list[ParsedCommand] = []
    cwd: str | None = None
    for part in filtered:
        if len(part) == 0:
            continue
        if part[0] == "cd":
            target = _cd_target(part[1:])
            if target is not None:
                cwd = _join_paths(cwd, target)
            continue
        commands.append(_with_cwd(_summarize_main_tokens(part), cwd))

    commands = _simplify(commands)
    if len(commands) == 1:
        command = commands[0]
        if command.kind == "read":
            if had_connectors and "|" in tokens and _script_contains_sed_n(tokens):
                return [
                    ParsedCommand(
                        kind="read",
                        cmd=script,
                        name=command.name,
                        path=command.path,
                    )
                ]
            if not had_connectors:
                return [
                    ParsedCommand(
                        kind="read",
                        cmd=_shlex_join(tokens),
                        name=command.name,
                        path=command.path,
                    )
                ]
        if command.kind in {"list_files", "search"} and not had_connectors:
            return [
                ParsedCommand(
                    kind=command.kind,
                    cmd=_shlex_join(tokens),
                    name=command.name,
                    path=command.path,
                    query=command.query,
                )
            ]
    return commands


def _single_unknown_for_command(command: list[str]) -> ParsedCommand:
    shell_command = _extract_shell_command(command)
    if shell_command is not None:
        return ParsedCommand(kind="unknown", cmd=shell_command)
    powershell_command = _extract_powershell_command(command)
    if powershell_command is not None:
        return ParsedCommand(kind="unknown", cmd=powershell_command)
    return ParsedCommand(kind="unknown", cmd=_shlex_join(command))


def _extract_shell_command(command: list[str]) -> str | None:
    if len(command) < 3:
        return None
    shell_name = os.path.basename(command[0]).casefold()
    if shell_name not in _SHELL_NAMES:
        return None
    index = 1
    while index < len(command) - 1:
        flag = command[index]
        if flag in {"-c", "-lc"}:
            return command[index + 1]
        if flag.startswith("-") and "c" in flag:
            return command[index + 1]
        index += 1
    return None


def _extract_powershell_command(command: list[str]) -> str | None:
    if len(command) < 2:
        return None
    shell_name = os.path.basename(command[0]).casefold()
    if shell_name not in _POWERSHELL_NAMES:
        return None
    index = 1
    while index < len(command) - 1:
        flag = command[index].casefold()
        if flag in {"-command", "-c"}:
            return command[index + 1]
        index += 1
    return None


def _normalize_tokens(command: list[str]) -> list[str]:
    if (
        len(command) >= 3
        and command[0] in {"yes", "y", "no", "n"}
        and command[1] == "|"
    ):
        return command[2:]
    shell_command = _extract_shell_command(command)
    if shell_command is not None:
        return _shlex_split(shell_command)
    return [*command]


def _contains_connectors(tokens: Sequence[str]) -> bool:
    return any(token in _CONNECTORS for token in tokens)


def _split_on_connectors(tokens: Sequence[str]) -> list[list[str]]:
    parts: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _CONNECTORS:
            if len(current) > 0:
                parts.append(current)
                current = []
        else:
            current.append(token)
    if len(current) > 0:
        parts.append(current)
    return parts


def _trim_at_connector(tokens: Sequence[str]) -> list[str]:
    for index, token in enumerate(tokens):
        if token in _CONNECTORS:
            return list(tokens[:index])
    return list(tokens)


def _short_display_path(path: str) -> str:
    normalized = path.replace("\\", "/").rstrip("/")
    parts = [
        part
        for part in reversed(normalized.split("/"))
        if part not in {"", "build", "dist", "node_modules", "src"}
    ]
    if len(parts) > 0:
        return parts[0]
    return normalized


def _skip_flag_values(args: Sequence[str], flags_with_vals: set[str]) -> list[str]:
    output: list[str] = []
    skip_next = False
    index = 0
    while index < len(args):
        arg = args[index]
        if skip_next:
            skip_next = False
            index += 1
            continue
        if arg == "--":
            output.extend(args[index + 1 :])
            break
        if arg.startswith("--") and "=" in arg:
            index += 1
            continue
        if arg in flags_with_vals:
            skip_next = index + 1 < len(args)
            index += 1
            continue
        output.append(arg)
        index += 1
    return output


def _positional_operands(args: Sequence[str], flags_with_vals: set[str]) -> list[str]:
    output: list[str] = []
    after_double_dash = False
    skip_next = False
    for index, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if after_double_dash:
            output.append(arg)
            continue
        if arg == "--":
            after_double_dash = True
            continue
        if arg.startswith("--") and "=" in arg:
            continue
        if arg in flags_with_vals:
            skip_next = index + 1 < len(args)
            continue
        if arg.startswith("-"):
            continue
        output.append(arg)
    return output


def _first_non_flag_operand(
    args: Sequence[str], flags_with_vals: set[str]
) -> str | None:
    operands = _positional_operands(args, flags_with_vals)
    if len(operands) == 0:
        return None
    return operands[0]


def _single_non_flag_operand(
    args: Sequence[str], flags_with_vals: set[str]
) -> str | None:
    operands = _positional_operands(args, flags_with_vals)
    if len(operands) != 1:
        return None
    return operands[0]


def _parse_grep_like(main_cmd: Sequence[str], args: Sequence[str]) -> ParsedCommand:
    args_no_connector = _trim_at_connector(args)
    operands: list[str] = []
    pattern: str | None = None
    after_double_dash = False
    index = 0
    while index < len(args_no_connector):
        arg = args_no_connector[index]
        if after_double_dash:
            operands.append(arg)
            index += 1
            continue
        if arg == "--":
            after_double_dash = True
            index += 1
            continue
        if arg in {"-e", "--regexp", "-f", "--file"}:
            if index + 1 < len(args_no_connector) and pattern is None:
                pattern = args_no_connector[index + 1]
            index += 2
            continue
        if arg in {
            "-m",
            "--max-count",
            "-C",
            "--context",
            "-A",
            "--after-context",
            "-B",
            "--before-context",
        }:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        operands.append(arg)
        index += 1
    has_pattern = pattern is not None
    query = (
        pattern if pattern is not None else operands[0] if len(operands) > 0 else None
    )
    path_index = 0 if has_pattern else 1
    path = (
        _short_display_path(operands[path_index])
        if len(operands) > path_index
        else None
    )
    return ParsedCommand(
        kind="search", cmd=_shlex_join(main_cmd), query=query, path=path
    )


def _awk_data_file_operand(args: Sequence[str]) -> str | None:
    if len(args) == 0:
        return None
    args_no_connector = _trim_at_connector(args)
    has_script_file = any(arg in {"-f", "--file"} for arg in args_no_connector)
    candidates = _skip_flag_values(
        args_no_connector,
        {"-F", "-v", "-f", "--field-separator", "--assign", "--file"},
    )
    non_flags = [arg for arg in candidates if not arg.startswith("-")]
    if has_script_file:
        return non_flags[0] if len(non_flags) > 0 else None
    if len(non_flags) >= 2:
        return non_flags[1]
    return None


def _python_walks_files(args: Sequence[str]) -> bool:
    args_no_connector = _trim_at_connector(args)
    index = 0
    while index < len(args_no_connector) - 1:
        if args_no_connector[index] == "-c":
            script = args_no_connector[index + 1]
            return any(
                marker in script
                for marker in (
                    "os.walk",
                    "os.listdir",
                    "os.scandir",
                    "glob.glob",
                    "glob.iglob",
                    "pathlib.Path",
                    ".rglob(",
                )
            )
        index += 1
    return False


def _is_python_command(command: str) -> bool:
    return (
        command in {"python", "python2", "python3"}
        or command.startswith("python2.")
        or command.startswith("python3.")
    )


def _cd_target(args: Sequence[str]) -> str | None:
    target: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return args[index + 1] if index + 1 < len(args) else None
        if arg in {"-L", "-P"} or arg.startswith("-"):
            index += 1
            continue
        target = arg
        index += 1
    return target


def _is_pathish(value: str) -> bool:
    return (
        value in {".", ".."}
        or value.startswith("./")
        or value.startswith("../")
        or "/" in value
        or "\\" in value
    )


def _parse_fd_query_and_path(tail: Sequence[str]) -> tuple[str | None, str | None]:
    args_no_connector = _trim_at_connector(tail)
    candidates = _skip_flag_values(
        args_no_connector,
        {"-t", "--type", "-e", "--extension", "-E", "--exclude", "--search-path"},
    )
    non_flags = [arg for arg in candidates if not arg.startswith("-")]
    if len(non_flags) == 1:
        one = non_flags[0]
        if _is_pathish(one):
            return None, _short_display_path(one)
        return one, None
    if len(non_flags) >= 2:
        return non_flags[0], _short_display_path(non_flags[1])
    return None, None


def _parse_find_query_and_path(tail: Sequence[str]) -> tuple[str | None, str | None]:
    args_no_connector = _trim_at_connector(tail)
    path: str | None = None
    for arg in args_no_connector:
        if not arg.startswith("-") and arg not in {"!", "(", ")"}:
            path = _short_display_path(arg)
            break
    query: str | None = None
    index = 0
    while index < len(args_no_connector) - 1:
        if args_no_connector[index] in {"-name", "-iname", "-path", "-regex"}:
            query = args_no_connector[index + 1]
            break
        index += 1
    return query, path


def _is_valid_sed_n_arg(value: str | None) -> bool:
    if value is None or not value.endswith("p"):
        return False
    core = value[:-1]
    parts = core.split(",")
    if len(parts) == 1:
        return parts[0].isdigit()
    if len(parts) == 2:
        return parts[0].isdigit() and parts[1].isdigit()
    return False


def _sed_read_path(args: Sequence[str]) -> str | None:
    args_no_connector = _trim_at_connector(args)
    if "-n" not in args_no_connector:
        return None
    has_range_script = False
    index = 0
    while index < len(args_no_connector):
        arg = args_no_connector[index]
        if arg in {"-e", "--expression"}:
            if index + 1 < len(args_no_connector) and _is_valid_sed_n_arg(
                args_no_connector[index + 1]
            ):
                has_range_script = True
            index += 2
            continue
        if arg in {"-f", "--file"}:
            index += 2
            continue
        index += 1
    if not has_range_script:
        has_range_script = any(
            not arg.startswith("-") and _is_valid_sed_n_arg(arg)
            for arg in args_no_connector
        )
    if not has_range_script:
        return None
    candidates = _skip_flag_values(
        args_no_connector,
        {"-e", "-f", "--expression", "--file"},
    )
    non_flags = [arg for arg in candidates if not arg.startswith("-")]
    if len(non_flags) == 0:
        return None
    if _is_valid_sed_n_arg(non_flags[0]):
        return non_flags[1] if len(non_flags) > 1 else None
    return non_flags[0]


def _is_small_formatting_command(tokens: Sequence[str]) -> bool:
    if len(tokens) == 0:
        return False
    command = tokens[0]
    if command in {"wc", "tr", "cut", "sort", "uniq", "tee", "column", "yes", "printf"}:
        return True
    if command == "xargs":
        return not _is_mutating_xargs_command(tokens)
    if command == "awk":
        return _awk_data_file_operand(tokens[1:]) is None
    if command == "head":
        if len(tokens) == 1:
            return True
        if len(tokens) == 2:
            return tokens[1].startswith("-")
        if len(tokens) == 3 and tokens[1] in {"-n", "-c"} and tokens[2].isdigit():
            return True
        return False
    if command == "tail":
        if len(tokens) == 1:
            return True
        if len(tokens) == 2:
            return tokens[1].startswith("-")
        if len(tokens) == 3 and tokens[1] in {"-n", "-c"}:
            value = tokens[2][1:] if tokens[2].startswith("+") else tokens[2]
            return value.isdigit()
        return False
    if command == "sed":
        return _sed_read_path(tokens[1:]) is None
    return False


def _is_mutating_xargs_command(tokens: Sequence[str]) -> bool:
    subcommand = _xargs_subcommand(tokens)
    return subcommand is not None and _xargs_is_mutating_subcommand(subcommand)


def _xargs_subcommand(tokens: Sequence[str]) -> list[str] | None:
    if len(tokens) == 0 or tokens[0] != "xargs":
        return None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return list(tokens[index + 1 :]) if index + 1 < len(tokens) else None
        if not token.startswith("-"):
            return list(tokens[index:])
        takes_value = token in {"-E", "-e", "-I", "-L", "-n", "-P", "-s"}
        index += 2 if takes_value and len(token) == 2 else 1
    return None


def _xargs_is_mutating_subcommand(tokens: Sequence[str]) -> bool:
    if len(tokens) == 0:
        return False
    command = tokens[0]
    tail = tokens[1:]
    if command in {"perl", "ruby"}:
        return _xargs_has_in_place_flag(tail)
    if command == "sed":
        return _xargs_has_in_place_flag(tail) or "--in-place" in tail
    if command == "rg":
        return "--replace" in tail
    return False


def _xargs_has_in_place_flag(tokens: Sequence[str]) -> bool:
    return any(
        token == "-i"
        or token.startswith("-i")
        or token == "-pi"
        or token.startswith("-pi")
        for token in tokens
    )


def _drop_small_formatting_commands(commands: list[list[str]]) -> list[list[str]]:
    return [tokens for tokens in commands if not _is_small_formatting_command(tokens)]


def _summarize_main_tokens(main_cmd: Sequence[str]) -> ParsedCommand:
    if len(main_cmd) == 0:
        return ParsedCommand(kind="unknown", cmd="")
    command = main_cmd[0]
    tail = list(main_cmd[1:])
    if command in {"ls", "eza", "exa"}:
        flags = {
            "ls": {
                "-I",
                "-w",
                "--block-size",
                "--format",
                "--time-style",
                "--color",
                "--quoting-style",
            },
            "eza": {
                "-I",
                "--ignore-glob",
                "--color",
                "--sort",
                "--time-style",
                "--time",
            },
            "exa": {
                "-I",
                "--ignore-glob",
                "--color",
                "--sort",
                "--time-style",
                "--time",
            },
        }[command]
        path = _first_non_flag_operand(tail, flags)
        return ParsedCommand(
            kind="list_files",
            cmd=_shlex_join(main_cmd),
            path=_short_display_path(path) if path is not None else None,
        )
    if command == "tree":
        path = _first_non_flag_operand(
            tail, {"-L", "-P", "-I", "--charset", "--filelimit", "--sort"}
        )
        return ParsedCommand(
            kind="list_files",
            cmd=_shlex_join(main_cmd),
            path=_short_display_path(path) if path is not None else None,
        )
    if command == "du":
        path = _first_non_flag_operand(
            tail,
            {"-d", "--max-depth", "-B", "--block-size", "--exclude", "--time-style"},
        )
        return ParsedCommand(
            kind="list_files",
            cmd=_shlex_join(main_cmd),
            path=_short_display_path(path) if path is not None else None,
        )
    if command in {"rg", "rga", "ripgrep-all"}:
        args_no_connector = _trim_at_connector(tail)
        has_files_flag = "--files" in args_no_connector
        candidates = _skip_flag_values(
            args_no_connector,
            {
                "-g",
                "--glob",
                "--iglob",
                "-t",
                "--type",
                "--type-add",
                "--type-not",
                "-m",
                "--max-count",
                "-A",
                "-B",
                "-C",
                "--context",
                "--max-depth",
            },
        )
        non_flags = [arg for arg in candidates if not arg.startswith("-")]
        if has_files_flag:
            path = non_flags[0] if len(non_flags) > 0 else None
            return ParsedCommand(
                kind="list_files",
                cmd=_shlex_join(main_cmd),
                path=_short_display_path(path) if path is not None else None,
            )
        query = non_flags[0] if len(non_flags) > 0 else None
        path = non_flags[1] if len(non_flags) > 1 else None
        return ParsedCommand(
            kind="search",
            cmd=_shlex_join(main_cmd),
            query=query,
            path=_short_display_path(path) if path is not None else None,
        )
    if command == "git":
        if len(tail) > 0 and tail[0] == "grep":
            return _parse_grep_like(main_cmd, tail[1:])
        if len(tail) > 0 and tail[0] == "ls-files":
            path = _first_non_flag_operand(
                tail[1:], {"--exclude", "--exclude-from", "--pathspec-from-file"}
            )
            return ParsedCommand(
                kind="list_files",
                cmd=_shlex_join(main_cmd),
                path=_short_display_path(path) if path is not None else None,
            )
        return ParsedCommand(kind="unknown", cmd=_shlex_join(main_cmd))
    if command == "fd":
        query, path = _parse_fd_query_and_path(tail)
        if query is not None:
            return ParsedCommand(
                kind="search", cmd=_shlex_join(main_cmd), query=query, path=path
            )
        return ParsedCommand(kind="list_files", cmd=_shlex_join(main_cmd), path=path)
    if command == "find":
        query, path = _parse_find_query_and_path(tail)
        if query is not None:
            return ParsedCommand(
                kind="search", cmd=_shlex_join(main_cmd), query=query, path=path
            )
        return ParsedCommand(kind="list_files", cmd=_shlex_join(main_cmd), path=path)
    if command in {"grep", "egrep", "fgrep"}:
        return _parse_grep_like(main_cmd, tail)
    if command in {"ag", "ack", "pt"}:
        candidates = _skip_flag_values(
            _trim_at_connector(tail),
            {
                "-G",
                "-g",
                "--file-search-regex",
                "--ignore-dir",
                "--ignore-file",
                "--path-to-ignore",
            },
        )
        non_flags = [arg for arg in candidates if not arg.startswith("-")]
        query = non_flags[0] if len(non_flags) > 0 else None
        path = non_flags[1] if len(non_flags) > 1 else None
        return ParsedCommand(
            kind="search",
            cmd=_shlex_join(main_cmd),
            query=query,
            path=_short_display_path(path) if path is not None else None,
        )
    if command == "cat":
        return _read_from_single_operand(main_cmd, tail, set())
    if command in {"bat", "batcat"}:
        return _read_from_single_operand(
            main_cmd,
            tail,
            {
                "--theme",
                "--language",
                "--style",
                "--terminal-width",
                "--tabs",
                "--line-range",
                "--map-syntax",
            },
        )
    if command == "less":
        return _read_from_single_operand(
            main_cmd,
            tail,
            {
                "-p",
                "-P",
                "-x",
                "-y",
                "-z",
                "-j",
                "--pattern",
                "--prompt",
                "--tabs",
                "--shift",
                "--jump-target",
            },
        )
    if command == "more":
        return _read_from_single_operand(main_cmd, tail, set())
    if command == "head":
        path = _head_tail_path(tail, allow_plus=False)
        if path is not None:
            return _read_command(main_cmd, path)
        return ParsedCommand(kind="unknown", cmd=_shlex_join(main_cmd))
    if command == "tail":
        path = _head_tail_path(tail, allow_plus=True)
        if path is not None:
            return _read_command(main_cmd, path)
        return ParsedCommand(kind="unknown", cmd=_shlex_join(main_cmd))
    if command == "awk":
        path = _awk_data_file_operand(tail)
        if path is not None:
            return _read_command(main_cmd, path)
        return ParsedCommand(kind="unknown", cmd=_shlex_join(main_cmd))
    if command == "nl":
        candidates = _skip_flag_values(tail, {"-s", "-w", "-v", "-i", "-b"})
        path = next((item for item in candidates if not item.startswith("-")), None)
        if path is not None:
            return _read_command(main_cmd, path)
        return ParsedCommand(kind="unknown", cmd=_shlex_join(main_cmd))
    if command == "sed":
        path = _sed_read_path(tail)
        if path is not None:
            return _read_command(main_cmd, path)
        return ParsedCommand(kind="unknown", cmd=_shlex_join(main_cmd))
    if _is_python_command(command):
        if _python_walks_files(tail):
            return ParsedCommand(
                kind="list_files", cmd=_shlex_join(main_cmd), path=None
            )
        return ParsedCommand(kind="unknown", cmd=_shlex_join(main_cmd))
    return ParsedCommand(kind="unknown", cmd=_shlex_join(main_cmd))


def _read_from_single_operand(
    main_cmd: Sequence[str],
    tail: Sequence[str],
    flags_with_vals: set[str],
) -> ParsedCommand:
    path = _single_non_flag_operand(tail, flags_with_vals)
    if path is None:
        return ParsedCommand(kind="unknown", cmd=_shlex_join(main_cmd))
    return _read_command(main_cmd, path)


def _read_command(main_cmd: Sequence[str], path: str) -> ParsedCommand:
    return ParsedCommand(
        kind="read",
        cmd=_shlex_join(main_cmd),
        name=_short_display_path(path),
        path=path,
    )


def _head_tail_path(tail: Sequence[str], *, allow_plus: bool) -> str | None:
    if len(tail) == 1 and not tail[0].startswith("-"):
        return tail[0]
    if len(tail) >= 2:
        first = tail[0]
        if first == "-n":
            value = tail[1]
            numeric = value[1:] if allow_plus and value.startswith("+") else value
            if numeric.isdigit():
                return next(
                    (item for item in tail[2:] if not item.startswith("-")), None
                )
        if first.startswith("-n"):
            value = first[2:]
            numeric = value[1:] if allow_plus and value.startswith("+") else value
            if numeric.isdigit():
                return next(
                    (item for item in tail[1:] if not item.startswith("-")), None
                )
    return None


def _simplify(commands: list[ParsedCommand]) -> list[ParsedCommand]:
    current = [*commands]
    while True:
        next_commands = _simplify_once(current)
        if next_commands is None:
            return current
        current = next_commands


def _simplify_once(commands: list[ParsedCommand]) -> list[ParsedCommand] | None:
    if len(commands) <= 1:
        return None
    first = commands[0]
    if first.kind == "unknown":
        tokens = _shlex_split(first.cmd)
        if len(tokens) > 0 and tokens[0] == "echo":
            return commands[1:]
    for index, command in enumerate(commands):
        if command.kind != "unknown":
            continue
        tokens = _shlex_split(command.cmd)
        if len(tokens) > 0 and tokens[0] == "cd" and len(commands) > index + 1:
            return [*commands[:index], *commands[index + 1 :]]
    for index, command in enumerate(commands):
        if command.kind == "unknown" and command.cmd == "true":
            return [*commands[:index], *commands[index + 1 :]]
    for index, command in enumerate(commands):
        if command.kind != "unknown":
            continue
        tokens = _shlex_split(command.cmd)
        if (
            len(tokens) > 0
            and tokens[0] == "nl"
            and all(token.startswith("-") for token in tokens[1:])
        ):
            return [*commands[:index], *commands[index + 1 :]]
    return None


def _with_cwd(command: ParsedCommand, cwd: str | None) -> ParsedCommand:
    if cwd is None or command.kind != "read" or command.path is None:
        return command
    return ParsedCommand(
        kind=command.kind,
        cmd=command.cmd,
        name=command.name,
        path=_join_paths(cwd, command.path),
        query=command.query,
    )


def _join_paths(base: str | None, rel: str) -> str:
    if _is_abs_like(rel):
        return rel
    if base is None or base == "":
        return rel
    return str(PurePath(base) / rel)


def _is_abs_like(path: str) -> bool:
    if os.path.isabs(path):
        return True
    if re.match(r"^[A-Za-z]:\\", path):
        return True
    return path.startswith("\\\\")


def _script_contains_sed_n(tokens: Sequence[str]) -> bool:
    return any(
        tokens[index] == "sed" and tokens[index + 1] == "-n"
        for index in range(len(tokens) - 1)
    )
