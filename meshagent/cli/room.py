from meshagent.cli import async_typer
from meshagent.cli import database
from meshagent.cli import queue
from meshagent.cli import agent
from meshagent.cli import messaging
from meshagent.cli import storage
from meshagent.cli import developer
from meshagent.cli import cli_secrets
from meshagent.cli import containers

app = async_typer.AsyncTyper(help="Operate within a room")

app.add_typer(agent.app, name="agents", help="Interact with agents and toolkits")
app.add_typer(cli_secrets.app, name="secret", help="Manage secrets for your project")
app.add_typer(queue.app, name="queue", help="Use queues in a room")
app.add_typer(messaging.app, name="messaging", help="Send and receive messages")
app.add_typer(storage.app, name="storage", help="Manage storage for a room")
app.add_typer(developer.app, name="developer", help="Developer utilities for a room")
app.add_typer(database.app, name="database", help="Manage database tables in a room")
app.add_typer(
    containers.app, name="container", help="Manage containers and images in a room"
)
