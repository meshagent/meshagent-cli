from meshagent.cli import async_typer


app = async_typer.LazyTyper(help="Operate within a room")

app.add_lazy_command(
    name="agent",
    module="meshagent.cli.agent",
    help="Interact with agents and toolkits",
    hidden=True,
)
app.add_lazy_command(
    name="agents",
    module="meshagent.cli.agent",
    help="Interact with agents and toolkits",
)
app.add_lazy_command(
    name="secret",
    module="meshagent.cli.oauth2",
    help="Manage secrets in a room",
)
app.add_lazy_command(
    name="secrets",
    module="meshagent.cli.oauth2",
    help="Manage secrets in a room",
    hidden=True,
)
app.add_lazy_command(
    name="queue",
    module="meshagent.cli.queue",
    help="Use queues in a room",
)
app.add_lazy_command(
    name="messaging",
    module="meshagent.cli.messaging",
    help="Send and receive messages",
)
app.add_lazy_command(
    name="storage",
    module="meshagent.cli.storage",
    help="Manage storage for a room",
)
app.add_lazy_command(
    name="service",
    module="meshagent.cli.room_services",
    help="Manage services in a room",
)
app.add_lazy_command(
    name="services",
    module="meshagent.cli.room_services",
    help="Manage services in a room",
    hidden=True,
)
app.add_lazy_command(
    name="developer",
    module="meshagent.cli.developer",
    help="Developer utilities for a room",
)
app.add_lazy_command(
    name="dataset",
    module="meshagent.cli.dataset",
    help="Manage dataset tables in a room",
)
app.add_lazy_command(
    name="database",
    module="meshagent.cli.dataset",
    help="Manage dataset tables in a room",
    hidden=True,
    deprecated=True,
)
app.add_lazy_command(
    name="memory",
    module="meshagent.cli.memory",
    help="Manage memories in a room",
)
app.add_lazy_command(
    name="container",
    module="meshagent.cli.containers",
    help="Manage containers and images in a room",
)
app.add_lazy_command(
    name="sync",
    module="meshagent.cli.sync",
    help="Inspect and update mesh documents in a room",
)
app.add_lazy_command(
    name="connect",
    module="meshagent.cli.room_connect",
    attribute="connect_command",
    help="Connect to a room and run a local command with room auth env",
)
