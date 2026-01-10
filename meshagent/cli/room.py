from meshagent.cli import async_typer
from meshagent.cli import database
from meshagent.cli import queue
from meshagent.cli import agent
from meshagent.cli import messaging
from meshagent.cli import storage
from meshagent.cli import developer
from meshagent.cli import cli_secrets
from meshagent.cli import containers

app = async_typer.AsyncTyper()

app.add_typer(agent.app, name="agents")
app.add_typer(cli_secrets.app, name="secret")
app.add_typer(queue.app, name="queue")
app.add_typer(messaging.app, name="messaging")
app.add_typer(storage.app, name="storage")
app.add_typer(developer.app, name="developer")
app.add_typer(database.app, name="database")
app.add_typer(containers.app, name="container")
