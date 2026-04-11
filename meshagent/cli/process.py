from meshagent.cli import async_typer
from meshagent.cli import chatbot
from meshagent.cli.helper import (
    DEPRECATED_REQUIRE_OPTION_ALIASES,
    strip_command_options,
)

app = async_typer.AsyncTyper(help="Join a process-backed agent to a room")
app.add_deprecated_option_aliases(DEPRECATED_REQUIRE_OPTION_ALIASES)

app.async_command("join", help="Join a room and run a process-backed agent.")(
    chatbot.join
)
app.async_command("service", help="Add a process-backed agent service to the host.")(
    chatbot.service
)
app.async_command(
    "spec",
    help="Generate a service spec for deploying a process-backed agent.",
)(chatbot.spec)
app.async_command("deploy", help="Deploy a process-backed agent service.")(
    chatbot.deploy
)
app.async_command(
    "run",
    help="Join a room, run a process-backed agent, and wait for messages.",
)(chatbot.run)
app.async_command(
    "use",
    help="Send a one-shot or interactive message to a running process-backed agent.",
)(chatbot.use)

strip_command_options(app, option_names=chatbot._HIDDEN_REQUIRE_OPTION_NAMES)
