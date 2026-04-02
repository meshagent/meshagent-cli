from meshagent.cli.async_typer import collect_lazy_command_modules_from_entrypoint


def main() -> None:
    for module in collect_lazy_command_modules_from_entrypoint("meshagent.cli.cli"):
        print("--hidden-import", module)


if __name__ == "__main__":
    main()
