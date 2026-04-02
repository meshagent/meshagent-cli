from meshagent.cli.async_typer import collect_lazy_command_modules_from_entrypoint


hiddenimports = collect_lazy_command_modules_from_entrypoint("meshagent.cli.cli")
