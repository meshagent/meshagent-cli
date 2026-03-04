from textual.widgets.option_list import Option

from meshagent.cli.tui.setup import SetupWizardApp


def test_first_enabled_option_index_returns_first_enabled() -> None:
    options = [
        Option("No projects available yet.", disabled=True),
        Option("Launch browser to sign in", id="launch"),
        Option("Exit setup", id="exit"),
    ]

    assert SetupWizardApp._first_enabled_option_index(options) == 1


def test_first_enabled_option_index_returns_none_when_all_disabled() -> None:
    options = [
        Option("Unavailable option 1", disabled=True),
        Option("Unavailable option 2", disabled=True),
    ]

    assert SetupWizardApp._first_enabled_option_index(options) is None
