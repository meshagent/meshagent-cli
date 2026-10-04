from meshagent.cli import async_typer
from meshagent.cli.users import sysadmin_app

app = async_typer.AsyncTyper(help="Platform administration; requires sysadmin access")
app.add_typer(sysadmin_app, name="user")
