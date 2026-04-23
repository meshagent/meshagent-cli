from textual.widgets.option_list import Option

from meshagent.cli.local_settings import SavedProfileRecord, StoredUserProfile
from meshagent.cli.tui.auth_switch import AuthSwitchApp


def _saved_profile(
    *,
    user_id: str,
    first_name: str,
    last_name: str,
    email: str,
    api_url: str,
    is_active: bool,
) -> SavedProfileRecord:
    return SavedProfileRecord(
        user_id=user_id,
        profile=StoredUserProfile(
            id=user_id,
            first_name=first_name,
            last_name=last_name,
            email=email,
        ),
        api_url=api_url,
        is_active=is_active,
    )


def test_first_enabled_option_index_returns_first_enabled() -> None:
    options = [
        Option("Disabled", disabled=True),
        Option("Enabled", id="enabled"),
        Option("Exit", id="exit"),
    ]

    assert AuthSwitchApp._first_enabled_option_index(options) == 1


def test_highlighted_profile_user_id_prefers_first_non_active() -> None:
    saved_profiles = [
        _saved_profile(
            user_id="user-123",
            first_name="Jesse",
            last_name="Ezell",
            email="jesse@example.com",
            api_url="https://api.meshagent.test",
            is_active=True,
        ),
        _saved_profile(
            user_id="user-456",
            first_name="Taylor",
            last_name="Swift",
            email="taylor@example.com",
            api_url="https://api.meshagent.test",
            is_active=False,
        ),
    ]

    assert AuthSwitchApp._highlighted_profile_user_id(saved_profiles) == "user-456"


def test_show_profile_selection_renders_saved_profiles(monkeypatch) -> None:
    app = AuthSwitchApp(
        saved_profiles=[
            _saved_profile(
                user_id="user-123",
                first_name="Jesse",
                last_name="Ezell",
                email="jesse@example.com",
                api_url="https://api.meshagent.test",
                is_active=True,
            ),
            _saved_profile(
                user_id="user-456",
                first_name="Taylor",
                last_name="Swift",
                email="taylor@example.com",
                api_url="https://api.meshagent.test",
                is_active=False,
            ),
        ]
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        app,
        "_set_text",
        lambda *, title, message, help_text: captured.update(
            {
                "title": title,
                "message": message,
                "help_text": help_text,
            }
        ),
    )
    monkeypatch.setattr(
        app,
        "_set_options",
        lambda *, options, highlighted_id=None: captured.update(
            {
                "options": [(str(option.prompt), option.id) for option in options],
                "highlighted_id": highlighted_id,
            }
        ),
    )

    app._show_profile_selection()

    assert captured == {
        "title": "Switch MeshAgent Account",
        "message": "Choose which saved local profile should become active.",
        "help_text": "Use Up/Down and Enter. Esc or Ctrl+C cancels.",
        "options": [
            (
                "Jesse Ezell (user-123) @ https://api.meshagent.test (active)",
                "__auth_switch_profile__:user-123",
            ),
            (
                "Taylor Swift (user-456) @ https://api.meshagent.test",
                "__auth_switch_profile__:user-456",
            ),
            ("Exit without switching", "__auth_switch_exit__"),
        ],
        "highlighted_id": "__auth_switch_profile__:user-456",
    }
