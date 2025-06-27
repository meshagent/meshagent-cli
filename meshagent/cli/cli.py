import typer
import asyncio

from meshagent.cli import auth
from meshagent.cli import api_keys
from meshagent.cli import projects
from meshagent.cli import sessions
from meshagent.cli import participant_token
from meshagent.cli import agent
from meshagent.cli import messaging
from meshagent.cli import storage
from meshagent.cli import developer
from meshagent.cli import webhook
from meshagent.cli import services
from meshagent.cli import cli_secrets
from meshagent.cli import call
from meshagent.cli import cli_mcp
from meshagent.cli import chatbot
from meshagent.cli import voicebot
from meshagent.cli import tty

app = typer.Typer()
app.add_typer(call.app, name="call")
app.add_typer(auth.app, name="auth")
app.add_typer(projects.app, name="project")
app.add_typer(api_keys.app, name="api-key")
app.add_typer(sessions.app, name="session")
app.add_typer(participant_token.app, name="participant-token")
app.add_typer(agent.app, name="agents")
app.add_typer(messaging.app, name="messaging")
app.add_typer(storage.app, name="storage")
app.add_typer(developer.app, name="developer")
app.add_typer(webhook.app, name="webhook")
app.add_typer(services.app, name="service")
app.add_typer(cli_secrets.app, name="secret")
app.add_typer(cli_mcp.app, name="mcp")
app.add_typer(chatbot.app, name="chatbot")
app.add_typer(voicebot.app, name="voicebot")
app.add_typer(tty.app, name="tty")

def _run_async(coro):
    asyncio.run(coro)

@app.command("setup")
def setup_command():
    """Perform initial login and project/api key activation."""

    async def runner():
        await auth.login()
        print("Activate a project...")
        project_id = await projects.activate(None, interactive=True)
        if project_id is None:
            print("You have choosen to not activate a project. Exiting.")
        if project_id is not None:
            print("Activate an api-key...")
            api_key_id = await api_keys.activate(None, interactive=True)
            if api_key_id is None:
                print("You have choosen to not activate an api-key. Exiting.")

    _run_async(runner())

if __name__ == "__main__":
    app()
