import json
import logging
from dataclasses import dataclass, field

import pytest
from meshagent.api import RoomException
from meshagent.api.client import (
    ProjectMemberAccess,
    ProjectMembersPage,
    User,
    UserProfilesPage,
)
from meshagent.cli import cli, users
from meshagent.cli.testing import CliRunner
from pydantic import JsonValue
from rich.console import Console

PROFILE = User(
    id="user-1",
    email="alice@example.com",
    first_name="Alice",
    metadata={"nested": [True, None, {"label": "[red]literal[/red]"}]},
    annotations={"team": "support"},
)


@dataclass
class FakeClient:
    pages: list[ProjectMembersPage] = field(default_factory=list)
    list_calls: list[tuple[str, int, str | None, str | None]] = field(
        default_factory=list
    )
    get_calls: list[str] = field(default_factory=list)
    update_calls: list[dict[str, object]] = field(default_factory=list)
    error: RoomException | None = None
    closed: bool = False

    async def close(self) -> None:
        self.closed = True

    async def get_users_in_project_page(
        self,
        project_id: str,
        *,
        page_size: int,
        continuation_token: str | None,
        filter: str | None,
        view: str = "merged",
    ) -> ProjectMembersPage:
        self.list_calls.append((project_id, page_size, continuation_token, filter))
        if self.error:
            raise self.error
        return self.pages.pop(0)

    async def get_user_profile(
        self, user_id: str, *, project_id: str | None = None, view: str = "merged"
    ) -> dict[str, object]:
        self.get_calls.append(user_id)
        if self.error:
            raise self.error
        return PROFILE.model_dump(mode="json")

    async def update_user_profile(
        self,
        user_id: str,
        *,
        first_name: str | None,
        last_name: str | None,
        metadata: dict[str, JsonValue] | None,
        annotations: dict[str, str] | None,
        project_id: str | None,
        inherit: list[str] | None = None,
    ) -> dict[str, bool]:
        self.update_calls.append(
            {
                "user_id": user_id,
                "first_name": first_name,
                "last_name": last_name,
                "metadata": metadata,
                "annotations": annotations,
                "project_id": project_id,
            }
        )
        if self.error:
            raise self.error
        return {"ok": True}

    async def search_sysadmin_users(
        self, *, filter=None, page_size=100, continuation_token=None
    ):
        self.list_calls.append(("sysadmin", page_size, continuation_token, filter))
        if self.error:
            raise self.error
        return UserProfilesPage(users=[PROFILE])

    async def get_sysadmin_user_profile(self, user_id):
        self.get_calls.append(user_id)
        if self.error:
            raise self.error
        return PROFILE

    async def update_sysadmin_user_profile(self, user_id, *, update):
        self.update_calls.append(
            {"user_id": user_id, **update.model_dump(exclude_unset=True)}
        )
        if self.error:
            raise self.error
        return {"ok": True}


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> FakeClient:
    fake = FakeClient()
    caplog.set_level(logging.WARNING, logger="asyncio")

    async def get_client() -> FakeClient:
        return fake

    monkeypatch.setattr(users, "get_client", get_client)
    monkeypatch.setenv("MESHAGENT_CLI_BUILD", "1")
    monkeypatch.setenv("MESHAGENT_DISABLE_VERSION_CHECK", "1")
    monkeypatch.delenv("MESHAGENT_PROJECT_ID", raising=False)
    return fake


@pytest.mark.parametrize("command", [[], ["list"], ["get"], ["update"]])
def test_help_is_available_from_root(command: list[str], client: FakeClient) -> None:
    result = CliRunner().invoke(cli.app, ["user", *command, "--help"])

    assert result.exit_code == 0, result.output
    if not command:
        assert all(name in result.output for name in ("list", "get", "update"))
    assert not client.list_calls and not client.get_calls and not client.update_calls


def test_list_follows_empty_filtered_pages_and_preserves_cursor(
    client: FakeClient,
) -> None:
    member = ProjectMemberAccess(user=PROFILE, direct_roles=["user_profile_editor"])
    client.pages = [
        ProjectMembersPage(users=[], continuation_token="next"),
        ProjectMembersPage(users=[member], continuation_token="resume"),
    ]
    result = CliRunner().invoke(
        users.app,
        [
            "list",
            "--project-id",
            "project-1",
            "--count",
            "1",
            "--filter",
            "alice",
            "--continuation-token",
            "start",
            "-o",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "users": [member.model_dump(mode="json")],
        "continuation_token": "resume",
    }
    assert client.list_calls == [
        ("project-1", 1, "start", "alice"),
        ("project-1", 1, "next", "alice"),
    ]
    assert client.closed


def test_list_caps_api_pages_and_uses_default_project(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MESHAGENT_PROJECT_ID", "project-env")
    member = ProjectMemberAccess(user=PROFILE, direct_roles=[])
    client.pages = [
        ProjectMembersPage(users=[member] * 100, continuation_token="next"),
        ProjectMembersPage(users=[member], continuation_token=None),
    ]
    result = CliRunner().invoke(users.app, ["list", "--count", "101", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)["users"]) == 101
    assert client.list_calls == [
        ("project-env", 100, None, None),
        ("project-env", 1, "next", None),
    ]


def test_empty_listing_succeeds(client: FakeClient) -> None:
    client.pages = [ProjectMembersPage(users=[], continuation_token=None)]
    result = CliRunner().invoke(users.app, ["list", "--project-id", "project-1"])

    assert result.exit_code == 0, result.output
    assert "No users found" in result.output
    assert client.closed


def test_nonadvancing_cursor_fails_and_closes_client(
    client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    client.pages = [ProjectMembersPage(users=[], continuation_token="same")]
    with pytest.raises(SystemExit) as error:
        users.app(["list", "--project-id", "project-1", "--continuation-token", "same"])

    assert error.value.code == 1
    assert "continuation token did not advance" in capsys.readouterr().err
    assert client.closed


@pytest.mark.parametrize("user_id", [None, "user-1"])
def test_get_preserves_profile_json(user_id: str | None, client: FakeClient) -> None:
    result = CliRunner().invoke(
        users.app, ["get", *([user_id] if user_id else []), "-o", "json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == PROFILE.model_dump(mode="json")
    assert client.get_calls == [user_id or "me"]
    assert client.closed


def test_get_table_preserves_literal_metadata(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(users, "Console", lambda: Console(width=300))
    result = CliRunner().invoke(users.app, ["get"])

    assert result.exit_code == 0, result.output
    assert "[red]literal[/red]" in result.output
    assert "support" in result.output
    assert client.closed


@pytest.mark.parametrize(
    "value", ['{"nested":[true,null,{"label":"[red]literal[/red]"}]}', "{}"]
)
def test_update_own_metadata_preserves_omitted_fields(
    value: str, client: FakeClient
) -> None:
    result = CliRunner().invoke(
        users.app, ["update", "--metadata", value, "-o", "json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"ok": True}
    assert client.update_calls == [
        {
            "user_id": "me",
            "first_name": None,
            "last_name": None,
            "metadata": json.loads(value),
            "annotations": None,
            "project_id": None,
        }
    ]
    assert client.closed


@pytest.mark.parametrize("value", ['{"team":"support"}', "{}"])
def test_update_other_user_annotations_with_project(
    value: str, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MESHAGENT_PROJECT_ID", "project-env")
    result = CliRunner().invoke(
        users.app,
        [
            "update",
            "user-1",
            "--project-id",
            "project-1",
            "--first-name",
            "",
            "--annotations",
            value,
        ],
    )

    assert result.exit_code == 0, result.output
    assert client.update_calls == [
        {
            "user_id": "user-1",
            "first_name": "",
            "last_name": None,
            "metadata": None,
            "annotations": json.loads(value),
            "project_id": "project-1",
        }
    ]
    assert client.closed


@pytest.mark.parametrize(
    "args",
    [
        ["update"],
        ["update", "--metadata", "[]"],
        ["update", "--metadata", "null"],
        ["update", "--metadata", "invalid"],
        ["update", "--metadata", '{"number":NaN}'],
        ["update", "--metadata", '{"number":Infinity}'],
        ["update", "--metadata", '{"number":1e999}'],
        ["update", "--annotations", '{"team":1}'],
        ["update", "--annotations", '{"team":null}'],
        ["get", "--output", "csv"],
        ["list", "--count", "0"],
    ],
)
def test_invalid_input_fails_before_client_creation(
    args: list[str], client: FakeClient
) -> None:
    result = CliRunner().invoke(users.app, args)

    assert result.exit_code == 2, result.output
    assert not client.list_calls and not client.get_calls and not client.update_calls
    assert not client.closed


def test_annotations_require_project_before_client_creation(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_project() -> None:
        return None

    monkeypatch.setattr("meshagent.cli.helper.get_active_project", no_project)
    result = CliRunner().invoke(users.app, ["update", "--annotations", "{}"])

    assert result.exit_code == 2, result.output
    assert "sysadmin user update" in result.output
    assert not client.update_calls and not client.closed


@pytest.mark.parametrize(
    "args",
    [["get"], ["list", "--project-id", "project-1"], ["update", "--metadata", "{}"]],
)
def test_api_errors_are_reported_and_client_closed(
    args: list[str], client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    client.error = RoomException("Status=403, body=user_profile_editor is required")
    with pytest.raises(SystemExit) as error:
        users.app(args)

    assert error.value.code == 1
    assert "user_profile_editor is required" in capsys.readouterr().err
    assert client.closed


@pytest.mark.parametrize(
    "args",
    [
        ["list", "--filter", "alice", "-o", "json"],
        ["get", "user-1", "-o", "json"],
        ["update", "user-1", "--annotations", '{"verified":"yes"}', "-o", "json"],
    ],
)
def test_sysadmin_user_commands_use_global_admin_apis(args, client):
    result = CliRunner().invoke(cli.app, ["sysadmin", "user", *args])
    assert result.exit_code == 0, result.output
    assert client.closed
    if args[0] == "list":
        assert json.loads(result.stdout)["users"][0]["id"] == "user-1"
        assert client.list_calls == [("sysadmin", 100, None, "alice")]
    elif args[0] == "update":
        assert client.update_calls == [
            {"user_id": "user-1", "annotations": {"verified": "yes"}}
        ]


@pytest.mark.parametrize(
    "args", [["get", "user-1"], ["list"], ["update", "user-1", "--annotations", "{}"]]
)
def test_sysadmin_cli_reports_denials_and_closes_client(args, client, capsys):
    client.error = RoomException("Status=403, body=sysadmin is required")
    with pytest.raises(SystemExit):
        users.sysadmin_app(args)
    assert "sysadmin is required" in capsys.readouterr().err
    assert client.closed


def test_global_flag_ignores_active_project(client, monkeypatch):
    monkeypatch.setenv("MESHAGENT_PROJECT_ID", "project-1")
    result = CliRunner().invoke(
        users.app, ["update", "--global", "--first-name", "Global"]
    )
    assert result.exit_code == 0, result.output
    assert client.update_calls[0]["project_id"] is None


@pytest.mark.parametrize(
    "args",
    [
        ["get", "--view", "invalid"],
        ["get", "--view", "project"],
        ["update", "--inherit", "metadata"],
        ["update", "--project-id", "p", "--inherit", "email"],
        ["update", "--project-id", "p", "--inherit", "metadata", "--metadata", "{}"],
    ],
)
def test_invalid_views_and_inheritance_are_rejected_before_api_calls(args, client):
    result = CliRunner().invoke(users.app, args)
    assert result.exit_code == 2, result.output
    assert not client.get_calls and not client.update_calls
