import asyncio
import tempfile
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from typer._click.testing import CliRunner
import pytest
import typer

from meshagent.api import ApiScope
from meshagent.api.client import (
    ConflictError,
    MeshagentDeploymentConfig,
    MeshagentDomains,
    Mailbox,
    NotFoundError,
    PermissionDeniedError,
    ProjectInfo,
    ProjectRoomGrant,
    ProjectRepository,
    Route,
    Room,
)
from meshagent.api.image_runtime import (
    IMAGE_RUNTIME_BASES,
    IMAGE_RUNTIME_MOUNT_PATH,
    IMAGE_RUNTIME_MOUNT_SUBPATH,
)
from meshagent.api.error_codes import ErrorCode
from meshagent.api.room_ports import ROOM_INTERNAL_API_PORT
from meshagent.cli import async_typer, cli, image
from meshagent.api.room_server_client import (
    PublishedBuildImage,
    RoomException,
    ServiceRuntimeState,
)
from meshagent.api.specs.service import (
    ContainerMountSpec,
    ContainerSpec,
    EnvironmentVariable,
    ImageStorageMountSpec,
    PortSpec,
    RouteBackendSpec,
    RouteMetadata,
    RoutePathSpec,
    RouteRoomBackendSpec,
    RouteSpec,
    SecretValue,
    ServiceMetadata,
    ServiceSpec,
    TokenValue,
)


class _FakeParticipant:
    def __init__(self, *, name: str) -> None:
        self._name = name

    def get_attribute(self, name: str):
        if name == "name":
            return self._name
        return None


def _fake_published_build_image(
    *,
    tag: str = "registry.meshagent.com/repo/web:1",
    resolved_ref: str = "registry.meshagent.com/repo/web@sha256:digest",
) -> PublishedBuildImage:
    return PublishedBuildImage(
        tag=tag,
        digest="sha256:digest",
        resolved_ref=resolved_ref,
        optimized=True,
    )


def _route_for_service(
    *, domain: str, room_name: str, port: str, service_id: str
) -> Route:
    return Route(
        domain=domain,
        spec=RouteSpec(
            metadata=RouteMetadata(
                name=domain,
                annotations={image.ANNOTATION_SERVICE_ID: service_id},
            ),
            domain=domain,
            backend=RouteBackendSpec(room=RouteRoomBackendSpec(name=room_name)),
            paths=[RoutePathSpec(path="/", targetPort=port)],
        ),
    )


class _FakeDeployConfigClient:
    def __init__(
        self,
        *,
        pages: str | None = "pages.meshagent.example",
        mail: str | None = "mail.meshagent.example",
        routes: dict[str, Route] | None = None,
        mailboxes: dict[str, Mailbox] | None = None,
    ) -> None:
        self._config = MeshagentDeploymentConfig(
            domains=MeshagentDomains(pages=pages, mail=mail)
        )
        self._routes = routes or {}
        self._mailboxes = mailboxes or {}

    async def get_config(self) -> MeshagentDeploymentConfig:
        return self._config

    async def get_route(self, *, project_id: str, domain: str) -> Route:
        del project_id
        route = self._routes.get(domain)
        if route is None:
            raise NotFoundError("route not found")
        return route

    async def get_mailbox(self, *, project_id: str, address: str) -> Mailbox:
        del project_id
        mailbox = self._mailboxes.get(address)
        if mailbox is None:
            raise NotFoundError("mailbox not found")
        return mailbox


def test_root_deploy_describe_prints_local_template(tmp_path: Path) -> None:
    deploy_dir = tmp_path / ".meshagent"
    deploy_dir.mkdir()
    (deploy_dir / "deploy.yaml").write_text(
        """
version: v1
kind: ServiceTemplate
metadata:
  name: demo
  description: Demo service.
variables:
  - name: domain
    title: Public domain
    description: Domain to route to the service.
    type: route
    optional: true
container:
  image: "{{ image }}"
  template: none
ports:
  - num: 8080
    type: http
    published: true
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        async_typer.get_command(cli.app),
        ["deploy", "describe", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "Deploy spec" in result.output
    assert "Name: demo" in result.output
    assert "domain (optional, type=route)" in result.output


def test_format_deploy_summary_includes_room_service_domain_and_emails() -> None:
    summary = image._DeploySummary(
        room_name="room-1",
        service_name="web",
        service_id="service-1",
        domain="web.meshagent.app",
        emails=("hello@mail.meshagent.app",),
    )

    rendered = image._format_deploy_summary(summary)

    assert "Deploy complete" in rendered
    assert (
        "Service [bold]web[/bold] was deployed to room [bold]room-1[/bold]." in rendered
    )
    assert "Public domain: web.meshagent.app" in rendered
    assert "Email: hello@mail.meshagent.app" in rendered


@pytest.fixture(autouse=True)
def _stub_project_registry_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeConfigClient:
        async def get_config(self) -> MeshagentDeploymentConfig:
            return MeshagentDeploymentConfig(
                domains=MeshagentDomains(registry="registry.meshagent.com")
            )

        async def close(self) -> None:
            return None

    async def _fake_get_client() -> _FakeConfigClient:
        return _FakeConfigClient()

    monkeypatch.setattr(image, "get_client", _fake_get_client)


@pytest.fixture(autouse=True)
def _stub_deploy_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    calls: list[dict[str, object]] = []
    original = image._wait_for_deployed_service_live

    async def _fake_wait_for_deployed_service_live(**kwargs) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        image,
        "_wait_for_deployed_service_live",
        _fake_wait_for_deployed_service_live,
    )
    return {
        "calls": calls,
        "original": original,
    }


def test_root_help_lists_build_and_deploy_commands() -> None:
    result = CliRunner().invoke(async_typer.get_command(cli.app), ["--help"])

    assert result.exit_code == 0
    assert "│ build" in result.output
    assert "│ deploy" in result.output


def test_root_build_help_uses_positional_path() -> None:
    result = CliRunner().invoke(async_typer.get_command(cli.app), ["build", "--help"])

    assert result.exit_code == 0
    assert "Usage: meshagent build [OPTIONS] PATH" in result.output
    assert "--pack" not in result.output


def test_root_deploy_help_uses_optional_positional_path() -> None:
    result = CliRunner().invoke(async_typer.get_command(cli.app), ["deploy", "--help"])

    assert result.exit_code == 0
    assert "Usage: meshagent deploy [OPTIONS] [PATH]" in result.output
    assert "--pack" not in result.output


def test_root_deploy_help_mentions_existing_room_flow() -> None:
    result = CliRunner().invoke(async_typer.get_command(cli.app), ["deploy", "--help"])
    normalized_output = re.sub(r"[\s│]+", " ", result.output)

    assert result.exit_code == 0
    assert "The target room must already exist." in result.output
    assert "Existing room name." in result.output
    assert "--public --domain <domain>" in result.output
    assert "meshagent config get domains.pages" in normalized_output
    assert "create <room> --if-not-exists" not in result.output
    assert "return a public URL" in normalized_output
    assert ".meshagent.dev" not in result.output
    assert ".meshagent.life" not in result.output
    assert "If PATH does not include a Dockerfile yet" in normalized_output
    assert "--dockerfile-path" in result.output


def test_resolve_deploy_environment_replaces_existing_service_environment() -> None:
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        metadata=ServiceMetadata(name="repo-web"),
        container=ContainerSpec(
            image="repo/web:old",
            environment=[
                EnvironmentVariable(name="KEEP", value="1"),
                EnvironmentVariable(
                    name="MESHAGENT_TOKEN",
                    token=TokenValue(
                        identity="existing-id",
                        api=ApiScope.agent_default(),
                        role="tool",
                    ),
                ),
            ],
        ),
    )

    resolved = image._resolve_deploy_environment(
        existing_service=existing_service,
        default_environment=None,
        parsed_environment=[],
        parsed_secret_environment=[],
        meshagent_token_scope=None,
        token_identity="repo-web",
        identity_override=None,
    )

    assert resolved.identity == "repo-web"
    assert resolved.environment is None


@pytest.mark.asyncio
async def test_deploy_image_missing_room_prints_create_room_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed: list[str] = []

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        raise NotFoundError("Status=404, body=room not found")

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda message: printed.append(message))

    with pytest.raises(typer.Exit) as exc_info:
        await image.deploy_image(
            project_id="project-1",
            room="missing-room",
            tag="repo/web:1",
            domain=None,
            room_mount=[],
            project_mount=[],
            empty_dir_mount=[],
            image_mount=[],
            env=[],
            meshagent_token=None,
            private=True,
        )

    assert exc_info.value.exit_code == 1
    assert printed[-1:] == [
        "[red]Room does not exist: missing-room\n"
        "Create it first with "
        "'meshagent rooms create missing-room --if-not-exists', "
        "then retry deploy.[/red]"
    ]


@pytest.mark.asyncio
async def test_list_owner_deploy_rooms_filters_to_active_user_admin_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def list_room_grants_by_user(
            self,
            *,
            project_id: str,
            user_id: str,
            limit: int,
            offset: int,
            order_by: str,
        ) -> list[ProjectRoomGrant]:
            self.calls.append(
                {
                    "project_id": project_id,
                    "user_id": user_id,
                    "limit": limit,
                    "offset": offset,
                    "order_by": order_by,
                }
            )
            return [
                ProjectRoomGrant(
                    room=Room(
                        id="room-member",
                        name="member",
                        metadata={},
                        annotations={},
                    ),
                    user_id="user-1",
                    permissions=ApiScope.user_default(),
                ),
                ProjectRoomGrant(
                    room=Room(
                        id="room-owner",
                        name="owner",
                        metadata={},
                        annotations={},
                    ),
                    user_id="user-1",
                    permissions=ApiScope.full(),
                ),
            ]

    fake_client = _FakeClient()
    monkeypatch.setattr(image, "get_active_user_id", lambda: "user-1")

    rooms = await image._list_owner_deploy_rooms(
        fake_client,
        project_id="project-1",
    )

    assert [room.name for room in rooms] == ["owner"]
    assert fake_client.calls == [
        {
            "project_id": "project-1",
            "user_id": "user-1",
            "limit": 500,
            "offset": 0,
            "order_by": "room_name",
        }
    ]


@pytest.mark.asyncio
async def test_user_can_create_deploy_room_uses_current_user_role_endpoint() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.project_ids: list[str] = []

        async def can_create_rooms(self, project_id: str) -> bool:
            self.project_ids.append(project_id)
            return True

    fake_client = _FakeClient()

    can_create = await image._user_can_create_deploy_room(
        fake_client,
        project_id="project-1",
    )

    assert can_create is True
    assert fake_client.project_ids == ["project-1"]


@pytest.mark.asyncio
async def test_resolve_deploy_room_prompts_when_room_missing_and_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, object] = {}

    async def _fake_select_deploy_room_interactively(*, project_id: str) -> str:
        called["project_id"] = project_id
        return "picked-room"

    monkeypatch.setattr(image, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        image,
        "_select_deploy_room_interactively",
        _fake_select_deploy_room_interactively,
    )
    monkeypatch.setattr(
        image,
        "resolve_room",
        lambda room: pytest.fail("resolve_room should not be used"),
    )

    room = await image._resolve_deploy_room(project_id="project-1", room=None)

    assert room == "picked-room"
    assert called == {"project_id": "project-1"}


@pytest.mark.asyncio
async def test_resolve_deploy_room_non_tty_uses_existing_room_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image, "_stdio_is_interactive", lambda: False)
    monkeypatch.setattr(image, "resolve_room", lambda room: "env-room")

    room = await image._resolve_deploy_room(project_id="project-1", room=None)

    assert room == "env-room"


@pytest.mark.asyncio
async def test_select_deploy_room_can_create_new_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_rooms: list[dict[str, object]] = []

    class _FakeClient:
        async def list_room_grants_by_user(
            self,
            *,
            project_id: str,
            user_id: str,
            limit: int,
            offset: int,
            order_by: str,
        ) -> list[ProjectRoomGrant]:
            del project_id, user_id, limit, offset, order_by
            return []

        async def can_create_rooms(self, project_id: str) -> bool:
            assert project_id == "project-1"
            return True

        async def create_room(
            self,
            *,
            project_id: str,
            name: str,
            permissions: dict[str, ApiScope],
        ) -> Room:
            created_rooms.append(
                {"project_id": project_id, "name": name, "permissions": permissions}
            )
            return Room(id="room-created", name=name, metadata={}, annotations={})

        async def close(self) -> None:
            return None

    async def _fake_get_client() -> _FakeClient:
        return _FakeClient()

    async def _fake_run_deploy_room_picker_tui(
        *, rooms, can_create_room, create_error=None
    ):
        from meshagent.cli.tui.deploy_room import DeployRoomPickerResult

        assert rooms == []
        assert can_create_room is True
        assert create_error is None
        return DeployRoomPickerResult(
            status="create",
            create_room_name="new-room",
        )

    printed: list[str] = []
    monkeypatch.setattr(image, "get_active_user_id", lambda: "user-1")
    monkeypatch.setattr(image, "get_client", _fake_get_client)
    monkeypatch.setattr(image, "print", lambda message: printed.append(message))
    monkeypatch.setattr(
        "meshagent.cli.tui.deploy_room.run_deploy_room_picker_tui",
        _fake_run_deploy_room_picker_tui,
    )

    room = await image._select_deploy_room_interactively(project_id="project-1")

    assert room == "new-room"
    assert created_rooms == [
        {
            "project_id": "project-1",
            "name": "new-room",
            "permissions": {"user-1": ApiScope.full()},
        }
    ]
    assert printed == ["[bold green]Created room new-room[/bold green]"]


@pytest.mark.asyncio
async def test_select_deploy_room_create_requires_active_user_for_owner_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeClient:
        async def can_create_rooms(self, project_id: str) -> bool:
            assert project_id == "project-1"
            return True

        async def create_room(
            self,
            *,
            project_id: str,
            name: str,
            permissions: dict[str, ApiScope],
        ) -> Room:
            del permissions
            return Room(id="room-created", name=name, metadata={}, annotations={})

        async def close(self) -> None:
            return None

    async def _fake_get_client() -> _FakeClient:
        return _FakeClient()

    async def _fake_run_deploy_room_picker_tui(
        *, rooms, can_create_room, create_error=None
    ):
        from meshagent.cli.tui.deploy_room import DeployRoomPickerResult

        assert rooms == []
        assert can_create_room is True
        assert create_error is None
        return DeployRoomPickerResult(
            status="create",
            create_room_name="new-room",
        )

    monkeypatch.setattr(image, "get_active_user_id", lambda: None)
    monkeypatch.setattr(image, "get_client", _fake_get_client)
    monkeypatch.setattr(image, "print", lambda message: None)
    monkeypatch.setattr(
        "meshagent.cli.tui.deploy_room.run_deploy_room_picker_tui",
        _fake_run_deploy_room_picker_tui,
    )

    with pytest.raises(typer.BadParameter) as exc_info:
        await image._select_deploy_room_interactively(project_id="project-1")

    assert "Unable to determine the active user" in str(exc_info.value)


@pytest.mark.asyncio
async def test_select_deploy_room_prompts_again_when_created_room_name_is_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_rooms: list[dict[str, object]] = []

    class _FakeClient:
        async def list_room_grants_by_user(
            self,
            *,
            project_id: str,
            user_id: str,
            limit: int,
            offset: int,
            order_by: str,
        ) -> list[ProjectRoomGrant]:
            del project_id, user_id, limit, offset, order_by
            return []

        async def can_create_rooms(self, project_id: str) -> bool:
            assert project_id == "project-1"
            return True

        async def create_room(
            self,
            *,
            project_id: str,
            name: str,
            permissions: dict[str, ApiScope],
        ) -> Room:
            created_rooms.append(
                {"project_id": project_id, "name": name, "permissions": permissions}
            )
            if name == "taken-room":
                raise ConflictError("room already exists")
            return Room(id="room-created", name=name, metadata={}, annotations={})

        async def close(self) -> None:
            return None

    async def _fake_get_client() -> _FakeClient:
        return _FakeClient()

    picker_calls: list[dict[str, object]] = []

    async def _fake_run_deploy_room_picker_tui(
        *, rooms, can_create_room, create_error=None
    ):
        from meshagent.cli.tui.deploy_room import DeployRoomPickerResult

        assert rooms == []
        assert can_create_room is True
        picker_calls.append({"create_error": create_error})
        if len(picker_calls) == 1:
            return DeployRoomPickerResult(
                status="create",
                create_room_name="taken-room",
            )
        return DeployRoomPickerResult(
            status="create",
            create_room_name="new-room",
        )

    monkeypatch.setattr(image, "get_active_user_id", lambda: "user-1")
    monkeypatch.setattr(image, "get_client", _fake_get_client)
    monkeypatch.setattr(image, "print", lambda message: None)
    monkeypatch.setattr(
        "meshagent.cli.tui.deploy_room.run_deploy_room_picker_tui",
        _fake_run_deploy_room_picker_tui,
    )

    room = await image._select_deploy_room_interactively(project_id="project-1")

    assert room == "new-room"
    assert picker_calls == [
        {"create_error": None},
        {
            "create_error": (
                "Room name 'taken-room' is already in use. Enter a different room name."
            )
        },
    ]
    assert created_rooms == [
        {
            "project_id": "project-1",
            "name": "taken-room",
            "permissions": {"user-1": ApiScope.full()},
        },
        {
            "project_id": "project-1",
            "name": "new-room",
            "permissions": {"user-1": ApiScope.full()},
        },
    ]


def test_replace_meshagent_image_vars_defaults_to_pkg_dev(monkeypatch) -> None:
    monkeypatch.setattr(
        image,
        "resolve_meshagent_image_prefix",
        lambda: image._DEFAULT_MESHAGENT_IMAGE_PREFIX,
    )

    assert image.replace_meshagent_image_vars("meshagent/python-sdk-slim:default") == (
        "us-central1-docker.pkg.dev/meshagent-public/images/"
        f"python-sdk-slim:{image.__version__}"
    )


def test_replace_meshagent_image_vars_allows_prefix_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MESHAGENT_IMAGE_PREFIX", "registry.example.com/custom/")

    assert image.replace_meshagent_image_vars("meshagent/python-sdk-slim:default") == (
        f"registry.example.com/custom/python-sdk-slim:{image.__version__}"
    )


def test_replace_meshagent_image_vars_uses_dev_prefix(monkeypatch) -> None:
    monkeypatch.setattr(
        image,
        "resolve_meshagent_image_prefix",
        lambda: "us-central1-docker.pkg.dev/meshagent-life/meshagent-public/",
    )

    assert image.replace_meshagent_image_vars("meshagent/node-sdk:default") == (
        "us-central1-docker.pkg.dev/meshagent-life/meshagent-public/"
        f"node-sdk:{image.__version__}"
    )


def test_replace_meshagent_image_vars_keeps_shell_images_on_estargz(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        image,
        "resolve_meshagent_image_prefix",
        lambda: image._DEFAULT_MESHAGENT_IMAGE_PREFIX,
    )

    assert image.replace_meshagent_image_vars("meshagent/shell-codex:default") == (
        "us-central1-docker.pkg.dev/meshagent-public/images/"
        f"shell-codex:{image.__version__}-esgz"
    )


def test_build_generated_pack_dockerfile_defaults_to_scratch() -> None:
    assert image._build_generated_pack_dockerfile(base_image=None) == (
        b"FROM scratch\nCOPY . /\n"
    )


@pytest.mark.asyncio
async def test_get_project_registry_uses_api_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeConfigClient:
        async def get_config(self) -> MeshagentDeploymentConfig:
            captured["get_config_called"] = True
            return MeshagentDeploymentConfig(
                domains=MeshagentDomains(registry="registry.meshagent.life")
            )

        async def close(self) -> None:
            captured["closed"] = True

    async def _fake_get_client() -> _FakeConfigClient:
        return _FakeConfigClient()

    monkeypatch.setattr(image, "get_client", _fake_get_client)

    registry = await image._get_project_registry()

    assert registry == "registry.meshagent.life"
    assert captured == {
        "get_config_called": True,
        "closed": True,
    }


@pytest.mark.asyncio
async def test_get_project_registry_derives_from_api_domain_when_registry_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeConfigClient:
        async def get_config(self) -> MeshagentDeploymentConfig:
            captured["get_config_called"] = True
            return MeshagentDeploymentConfig(
                domains=MeshagentDomains(
                    api="api.meshagent.life",
                    registry=None,
                )
            )

        async def close(self) -> None:
            captured["closed"] = True

    async def _fake_get_client() -> _FakeConfigClient:
        return _FakeConfigClient()

    monkeypatch.setattr(image, "get_client", _fake_get_client)

    registry = await image._get_project_registry()

    assert registry == "registry.meshagent.life"
    assert captured == {
        "get_config_called": True,
        "closed": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config_registry", "tag", "expected_tag", "expects_project_lookup"),
    [
        (
            "registry.meshagent.com",
            "website:1",
            "registry.meshagent.com/powerboards/website:1",
            True,
        ),
        (
            "registry.meshagent.com",
            "powerboards/website:1",
            "registry.meshagent.com/powerboards/website:1",
            False,
        ),
        (
            "room.meshagent.com",
            "room.meshagent.com/website-node:noopt",
            "room.meshagent.com/powerboards/website-node:noopt",
            True,
        ),
    ],
)
async def test_resolve_room_registry_target_normalizes_shorthand_tags(
    monkeypatch: pytest.MonkeyPatch,
    config_registry: str,
    tag: str,
    expected_tag: str,
    expects_project_lookup: bool,
) -> None:
    captured: dict[str, object] = {}

    class _FakeConfigClient:
        async def get_config(self) -> MeshagentDeploymentConfig:
            captured["get_config_called"] = True
            return MeshagentDeploymentConfig(
                domains=MeshagentDomains(registry=config_registry)
            )

        async def get_project_info(self, project_id: str) -> ProjectInfo:
            captured["project_id"] = project_id
            return ProjectInfo(
                id=project_id,
                owner_user_id="user-1",
                name="Powerboards",
                project_key="powerboards",
            )

        async def close(self) -> None:
            captured["closed"] = True

    async def _fake_get_client() -> _FakeConfigClient:
        return _FakeConfigClient()

    monkeypatch.setattr(image, "get_client", _fake_get_client)

    project_registry, parsed_tag = await image._resolve_room_registry_target(
        project_id="project-1",
        parsed_tag=image._parse_build_tag(tag),
    )

    assert project_registry == config_registry
    assert parsed_tag.value == expected_tag
    assert captured["get_config_called"] is True
    assert captured["closed"] is True
    if expects_project_lookup:
        assert captured["project_id"] == "project-1"
    else:
        assert "project_id" not in captured


@pytest.mark.asyncio
async def test_resolve_room_registry_target_derives_registry_from_api_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeConfigClient:
        async def get_config(self) -> MeshagentDeploymentConfig:
            captured["get_config_called"] = True
            return MeshagentDeploymentConfig(
                domains=MeshagentDomains(
                    api="api.meshagent.life",
                    registry=None,
                )
            )

        async def get_project_info(self, project_id: str) -> ProjectInfo:
            captured["project_id"] = project_id
            return ProjectInfo(
                id=project_id,
                owner_user_id="user-1",
                name="Powerboards",
                project_key="powerboards",
            )

        async def close(self) -> None:
            captured["closed"] = True

    async def _fake_get_client() -> _FakeConfigClient:
        return _FakeConfigClient()

    monkeypatch.setattr(image, "get_client", _fake_get_client)

    project_registry, parsed_tag = await image._resolve_room_registry_target(
        project_id="project-1",
        parsed_tag=image._parse_build_tag(
            "registry.meshagent.life/powerboards/website-node:noopt"
        ),
    )

    assert project_registry == "registry.meshagent.life"
    assert parsed_tag.value == "registry.meshagent.life/powerboards/website-node:noopt"
    assert captured["get_config_called"] is True
    assert captured["closed"] is True
    assert "project_id" not in captured


@pytest.mark.asyncio
async def test_pack_image_streams_generated_build_context_and_waits_for_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    source_dir = tmp_path / "sample"
    source_dir.mkdir()
    archive_dir = tempfile.TemporaryDirectory(prefix="meshagent-build-context-test-")
    archive_path = Path(archive_dir.name) / "context.tar"
    archive_bytes = b"context-archive"
    archive_path.write_bytes(archive_bytes)
    registry_credentials = [
        image.DockerSecret(
            registry="registry.meshagent.com",
            username="meshagent",
            password="token",
        )
    ]

    class _FakeContainers:
        async def build(self, **kwargs) -> str:
            captured["build_kwargs"] = {
                key: value for key, value in kwargs.items() if key != "chunks"
            }
            streamed = bytearray()
            async for chunk in kwargs["chunks"]:
                streamed.extend(chunk)
            captured["streamed_context"] = bytes(streamed)
            return "build-1"

        async def list_builds(self):
            return [
                SimpleNamespace(
                    id="build-1",
                    published_images=[_fake_published_build_image()],
                )
            ]

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_resolve_project_id(*, project_id):
        captured["project_id_arg"] = project_id
        return "resolved-project-1"

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_registry_build_credentials(
        *,
        account_client,
        project_id: str,
        parsed_tag,
        project_registry: str,
    ):
        captured["credentials_account_client"] = account_client
        captured["credentials_project_id"] = project_id
        captured["credentials_tag"] = parsed_tag.value
        captured["credentials_registry"] = project_registry
        return registry_credentials

    async def _fake_stream_build_job_logs_and_wait_for_exit(
        *,
        client,
        build_id: str,
    ) -> int:
        captured["wait_client"] = client
        captured["build_id"] = build_id
        return 0

    async def _fake_build_local_context_archive(**kwargs):
        captured["local_context_kwargs"] = kwargs
        return archive_path, len(archive_bytes), archive_dir

    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(
        image,
        "_resolve_project_registry_build_credentials",
        _fake_resolve_project_registry_build_credentials,
    )
    monkeypatch.setattr(
        image,
        "_build_local_context_archive",
        _fake_build_local_context_archive,
    )
    monkeypatch.setattr(
        image,
        "_stream_build_job_logs_and_wait_for_exit",
        _fake_stream_build_job_logs_and_wait_for_exit,
    )

    await image.pack_image(
        project_id="project-1",
        room="room-1",
        path=str(source_dir),
        tag="registry.meshagent.com/sample/app:1",
        base="python:3.13",
    )

    assert captured["project_id_arg"] == "project-1"
    assert captured["project_id"] == "resolved-project-1"
    assert captured["room"] == "room-1"
    assert captured["credentials_project_id"] == "resolved-project-1"
    assert captured["credentials_tag"] == "registry.meshagent.com/sample/app:1"
    assert captured["credentials_registry"] == "registry.meshagent.com"
    assert captured["local_context_kwargs"] == {
        "source_dir": source_dir,
        "preserved_paths": frozenset(),
        "injected_files": {
            image._GENERATED_PACK_DOCKERFILE_NAME: b"FROM python:3.13\nCOPY . /\n"
        },
    }
    assert captured["build_kwargs"] == {
        "tags": ["registry.meshagent.com/sample/app:1"],
        "mount_path": "/context",
        "context_path": "/context",
        "dockerfile_path": "/context/.meshagent-pack.Dockerfile",
        "optimize_image": True,
        "private": False,
        "credentials": registry_credentials,
        "builder_name": "builder",
        "size": len(archive_bytes),
    }
    assert captured["streamed_context"] == archive_bytes
    assert captured["build_id"] == "build-1"
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_build_image_streams_context_and_waits_for_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    credentials = [object()]
    captured: dict[str, object] = {}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    archive_dir = tempfile.TemporaryDirectory(prefix="meshagent-build-context-test-")
    archive_path = Path(archive_dir.name) / "context.tar"
    archive_bytes = b"context-archive"
    archive_path.write_bytes(archive_bytes)

    class _FakeContainers:
        async def build(self, **kwargs) -> str:
            captured["build_kwargs"] = {
                key: value for key, value in kwargs.items() if key != "chunks"
            }
            streamed = bytearray()
            async for chunk in kwargs["chunks"]:
                streamed.extend(chunk)
            captured["streamed_context"] = bytes(streamed)
            return "build-1"

        async def list_builds(self):
            return [
                SimpleNamespace(
                    id="build-1",
                    published_images=[_fake_published_build_image()],
                )
            ]

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()
            self.local_participant = _FakeParticipant(name="jesse@example.com")

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_stream_build_job_logs_and_wait_for_exit(
        *,
        client,
        build_id: str,
    ) -> int:
        captured["wait_client"] = client
        captured["build_id"] = build_id
        return 0

    async def _fake_build_local_context_archive(**kwargs):
        captured["local_context_kwargs"] = kwargs
        return archive_path, len(archive_bytes), archive_dir

    monkeypatch.setattr(image, "_parse_creds", lambda values: credentials)
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(
        image,
        "_resolve_project_registry_build_credentials",
        mock.AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        image,
        "_build_local_context_archive",
        _fake_build_local_context_archive,
    )
    monkeypatch.setattr(
        image,
        "_stream_build_job_logs_and_wait_for_exit",
        _fake_stream_build_job_logs_and_wait_for_exit,
    )

    await image.build_image(
        project_id="project-1",
        room="room-1",
        tag="registry.meshagent.com/project/name:tag",
        pack=f"{source_dir}:/workspace",
        context_path="/workspace",
        dockerfile_path="/workspace/Dockerfile",
        private=True,
        optimize=True,
        cred=["registry,user,password"],
        latest=True,
    )

    assert captured["project_id"] == "project-1"
    assert captured["room"] == "room-1"
    assert captured["local_context_kwargs"] == {
        "source_dir": source_dir,
        "preserved_paths": frozenset(),
    }
    assert captured["build_kwargs"] == {
        "tags": [
            "registry.meshagent.com/project/name:tag",
            "registry.meshagent.com/project/name:latest",
        ],
        "mount_path": "/workspace",
        "context_path": "/workspace",
        "dockerfile_path": "/workspace/Dockerfile",
        "optimize_image": True,
        "private": True,
        "credentials": credentials,
        "builder_name": "builder",
        "size": len(archive_bytes),
    }
    assert captured["streamed_context"] == archive_bytes
    assert captured["build_id"] == "build-1"
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_build_image_normalizes_shorthand_room_registry_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeConfigClient:
        async def get_config(self) -> MeshagentDeploymentConfig:
            return MeshagentDeploymentConfig(
                domains=MeshagentDomains(registry="room.meshagent.com")
            )

        async def get_project_info(self, project_id: str) -> ProjectInfo:
            captured["project_id"] = project_id
            return ProjectInfo(
                id=project_id,
                owner_user_id="user-1",
                name="Powerboards",
                project_key="powerboards",
            )

        async def close(self) -> None:
            captured["closed"] = True

    async def _fake_get_client() -> _FakeConfigClient:
        return _FakeConfigClient()

    async def _fake_resolve_project_id(*, project_id):
        captured["project_id_arg"] = project_id
        return "project-1"

    async def _fake_run_image_build_stage(**kwargs) -> PublishedBuildImage:
        captured["build_stage_kwargs"] = kwargs
        return _fake_published_build_image()

    monkeypatch.setattr(image, "get_client", _fake_get_client)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "_run_image_build_stage", _fake_run_image_build_stage)

    await image.build_image(
        project_id=None,
        room="room-1",
        tag="room.meshagent.com/website-node:noopt",
        pack="/tmp/context",
        context_path="/context",
        dockerfile_path="/context/Dockerfile",
        builder_name=None,
        private=False,
        optimize=True,
        cred=[],
    )

    assert captured["project_id_arg"] is None
    assert captured["project_id"] == "project-1"
    assert captured["closed"] is True
    assert captured["build_stage_kwargs"]["resolved_project_id"] == "project-1"
    assert captured["build_stage_kwargs"]["resolved_room"] == "room-1"
    assert (
        captured["build_stage_kwargs"]["parsed_tag"].value
        == "room.meshagent.com/powerboards/website-node:noopt"
    )
    assert captured["build_stage_kwargs"]["project_registry"] == "room.meshagent.com"


@pytest.mark.asyncio
async def test_build_image_pack_streams_context_and_defaults_context_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    archive_dir = tempfile.TemporaryDirectory(prefix="meshagent-build-context-test-")
    archive_path = Path(archive_dir.name) / "context.tar"
    archive_bytes = b"context-archive"
    archive_path.write_bytes(archive_bytes)

    class _FakeContainers:
        async def build(self, **kwargs) -> str:
            captured["build_kwargs"] = {
                key: value for key, value in kwargs.items() if key != "chunks"
            }
            streamed = bytearray()
            async for chunk in kwargs["chunks"]:
                streamed.extend(chunk)
            captured["streamed_context"] = bytes(streamed)
            return "build-1"

        async def list_builds(self):
            return [
                SimpleNamespace(
                    id="build-1",
                    published_images=[_fake_published_build_image()],
                )
            ]

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()
            self.local_participant = _FakeParticipant(name="jesse@example.com")

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_stream_build_job_logs_and_wait_for_exit(
        *,
        client,
        build_id: str,
    ) -> int:
        captured["wait_client"] = client
        captured["build_id"] = build_id
        return 0

    async def _fake_build_local_context_archive(**kwargs):
        captured["local_context_kwargs"] = kwargs
        return archive_path, len(archive_bytes), archive_dir

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(
        image,
        "_resolve_project_registry_build_credentials",
        mock.AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        image,
        "_build_local_context_archive",
        _fake_build_local_context_archive,
    )
    monkeypatch.setattr(image, "_parse_creds", lambda values: [])
    monkeypatch.setattr(
        image,
        "_stream_build_job_logs_and_wait_for_exit",
        _fake_stream_build_job_logs_and_wait_for_exit,
    )

    await image.build_image(
        project_id="project-1",
        room="room-1",
        tag="registry.meshagent.com/project/example:1",
        pack=str(source_dir),
        context_path=None,
        dockerfile_path=None,
        builder_name="builder-1",
        private=False,
        optimize=True,
        cred=[],
    )

    assert captured["project_id"] == "project-1"
    assert captured["room"] == "room-1"
    assert captured["local_context_kwargs"] == {
        "source_dir": source_dir,
        "preserved_paths": frozenset(),
    }
    assert captured["build_kwargs"] == {
        "tags": ["registry.meshagent.com/project/example:1"],
        "mount_path": "/context",
        "context_path": "/context",
        "dockerfile_path": "/context/Dockerfile",
        "optimize_image": True,
        "private": False,
        "credentials": [],
        "builder_name": "builder-1",
        "size": len(archive_bytes),
    }
    assert captured["streamed_context"] == archive_bytes
    assert captured["build_id"] == "build-1"
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_build_image_pack_preserves_ignored_dockerfile_and_dockerignore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (source_dir / ".dockerignore").write_text("Dockerfile\n", encoding="utf-8")
    archive_dir = tempfile.TemporaryDirectory(prefix="meshagent-build-context-test-")
    archive_path = Path(archive_dir.name) / "context.tar"
    archive_path.write_bytes(b"context-archive")

    class _FakeContainers:
        async def build(self, **kwargs) -> str:
            async for _chunk in kwargs["chunks"]:
                pass
            return "build-1"

        async def list_builds(self):
            return [
                SimpleNamespace(
                    id="build-1",
                    published_images=[_fake_published_build_image()],
                )
            ]

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()
            self.local_participant = _FakeParticipant(name="jesse@example.com")

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeAccountClient:
        async def close(self) -> None:
            return None

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_build_local_context_archive(**kwargs):
        captured["local_context_kwargs"] = kwargs
        return archive_path, archive_path.stat().st_size, archive_dir

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(
        image,
        "_resolve_project_registry_build_credentials",
        mock.AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        image,
        "_build_local_context_archive",
        _fake_build_local_context_archive,
    )
    monkeypatch.setattr(image, "_parse_creds", lambda values: [])
    monkeypatch.setattr(
        image,
        "_stream_build_job_logs_and_wait_for_exit",
        mock.AsyncMock(return_value=0),
    )

    await image.build_image(
        project_id="project-1",
        room="room-1",
        tag="registry.meshagent.com/project/example:1",
        pack=str(source_dir),
        context_path=None,
        dockerfile_path=None,
        private=False,
        optimize=True,
        cred=[],
    )

    assert captured["local_context_kwargs"]["preserved_paths"] == frozenset(
        {".dockerignore", "Dockerfile"}
    )


@pytest.mark.asyncio
async def test_build_image_pack_exits_with_build_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    archive_dir = tempfile.TemporaryDirectory(prefix="meshagent-build-context-test-")
    archive_path = Path(archive_dir.name) / "context.tar"
    archive_path.write_bytes(b"context-archive")

    class _FakeContainers:
        async def build(self, **kwargs) -> str:
            async for _chunk in kwargs["chunks"]:
                pass
            return "build-1"

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()
            self.local_participant = _FakeParticipant(name="jesse@example.com")

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeAccountClient:
        async def close(self) -> None:
            return None

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_build_local_context_archive(**kwargs):
        del kwargs
        return archive_path, archive_path.stat().st_size, archive_dir

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(
        image,
        "_resolve_project_registry_build_credentials",
        mock.AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        image,
        "_build_local_context_archive",
        _fake_build_local_context_archive,
    )
    monkeypatch.setattr(image, "_parse_creds", lambda values: [])
    monkeypatch.setattr(
        image,
        "_stream_build_job_logs_and_wait_for_exit",
        mock.AsyncMock(return_value=17),
    )

    with pytest.raises(typer.Exit) as exc_info:
        await image.build_image(
            project_id="project-1",
            room="room-1",
            tag="registry.meshagent.com/project/example:1",
            pack=str(source_dir),
            context_path=None,
            dockerfile_path=None,
            private=False,
            optimize=True,
            cred=[],
        )

    assert exc_info.value.exit_code == 17


def test_generated_pack_dockerfile_path_uses_mount_path() -> None:
    assert image._generated_pack_dockerfile_path(mount_path="/workspace") == (
        "/workspace/.meshagent-pack.Dockerfile"
    )


def test_infer_deploy_ports_from_packed_dockerfile_reads_expose_lines(
    tmp_path: Path,
) -> None:
    dockerfile_path = tmp_path / "Dockerfile"
    dockerfile_path.write_text(
        "FROM node:22-alpine\nEXPOSE 8080\nEXPOSE 8443/tcp 5353/udp \\\n  9000\n",
        encoding="utf-8",
    )

    ports = image._infer_deploy_ports_from_packed_dockerfile(
        local_packed_dockerfile=dockerfile_path
    )

    assert ports is not None
    assert [port.num for port in ports] == [8080, 8443, 9000]
    assert [port.type for port in ports] == ["http", "http", "http"]
    assert [port.published for port in ports] == [True, True, True]


def test_parse_packed_dockerfile_metadata_uses_final_stage_runtime_config(
    tmp_path: Path,
) -> None:
    dockerfile_path = tmp_path / "Dockerfile"
    dockerfile_path.write_text(
        """
FROM python:3.13 AS build
LABEL meshagent.runtime=python
EXPOSE 9999

FROM scratch
LABEL meshagent.runtime=node other.label="enabled"
WORKDIR /srv
WORKDIR app
ENV NODE_ENV=production PORT=8111
ENTRYPOINT ["/app/dist/index.js"]
CMD ["--port", "8111"]
EXPOSE 8111 8443/tcp 5353/udp
""".strip()
        + "\n",
        encoding="utf-8",
    )

    metadata = image._parse_packed_dockerfile_metadata(
        local_packed_dockerfile=dockerfile_path
    )

    assert metadata is not None
    assert metadata.exposed_ports == (8111, 8443)
    assert metadata.labels == {
        "meshagent.runtime": "node",
        "other.label": "enabled",
    }
    assert metadata.entrypoint == ("/app/dist/index.js",)
    assert metadata.command == ("--port", "8111")
    assert dict(metadata.environment) == {
        "NODE_ENV": "production",
        "PORT": "8111",
    }
    assert metadata.working_dir == "/srv/app"


def test_parse_packed_dockerfile_metadata_tracks_final_stage_volumes(
    tmp_path: Path,
) -> None:
    dockerfile_path = tmp_path / "Dockerfile"
    dockerfile_path.write_text(
        """
FROM python:3.13 AS build
VOLUME /ignored

FROM scratch
VOLUME ["/data", "/cache"]
VOLUME /data /logs/
""".strip()
        + "\n",
        encoding="utf-8",
    )

    metadata = image._parse_packed_dockerfile_metadata(
        local_packed_dockerfile=dockerfile_path
    )

    assert metadata is not None
    assert metadata.volumes == ("/data", "/cache", "/logs")


def test_infer_deploy_ports_from_packed_dockerfile_rejects_reserved_port(
    tmp_path: Path,
) -> None:
    dockerfile_path = tmp_path / "Dockerfile"
    dockerfile_path.write_text(
        f"FROM node:22-alpine\nEXPOSE {ROOM_INTERNAL_API_PORT}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        typer.BadParameter,
        match="reserved MeshAgent room infrastructure port",
    ):
        image._infer_deploy_ports_from_packed_dockerfile(
            local_packed_dockerfile=dockerfile_path
        )


def test_resolve_runtime_container_override_supports_python_runtime() -> None:
    override = image._resolve_runtime_container_override(
        parsed_tag=image._parse_build_tag("registry.meshagent.com/repo/app:1"),
        dockerfile_metadata=image._PackedDockerfileMetadata(
            labels={"meshagent.runtime": "python"},
            command=("main.py",),
            working_dir="/app",
        ),
    )

    assert override is not None
    assert override.image == IMAGE_RUNTIME_BASES["python"].base_image
    assert override.command == "python main.py"
    assert override.working_dir == "/app"
    assert override.image_mount.image == "registry.meshagent.com/repo/app:1"
    assert override.image_mount.path == IMAGE_RUNTIME_MOUNT_PATH
    assert override.image_mount.subpath == IMAGE_RUNTIME_MOUNT_SUBPATH


@pytest.mark.asyncio
async def test_build_image_can_disable_room_image_optimization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    archive_dir = tempfile.TemporaryDirectory(prefix="meshagent-build-context-test-")
    archive_path = Path(archive_dir.name) / "context.tar"
    archive_bytes = b"context-archive"
    archive_path.write_bytes(archive_bytes)

    class _FakeContainers:
        async def build(self, **kwargs) -> str:
            captured["build_kwargs"] = {
                key: value for key, value in kwargs.items() if key != "chunks"
            }
            async for _chunk in kwargs["chunks"]:
                pass
            return "build-1"

        async def list_builds(self):
            return [
                SimpleNamespace(
                    id="build-1",
                    published_images=[_fake_published_build_image()],
                )
            ]

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()
            self.local_participant = _FakeParticipant(name="jesse@example.com")

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeAccountClient:
        async def close(self) -> None:
            return None

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_build_local_context_archive(**kwargs):
        del kwargs
        return archive_path, len(archive_bytes), archive_dir

    async def _fake_stream_build_job_logs_and_wait_for_exit(
        *,
        client,
        build_id: str,
    ) -> int:
        del client, build_id
        return 0

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(
        image,
        "_resolve_project_registry_build_credentials",
        mock.AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        image,
        "_build_local_context_archive",
        _fake_build_local_context_archive,
    )
    monkeypatch.setattr(image, "_parse_creds", lambda values: [])
    monkeypatch.setattr(
        image,
        "_stream_build_job_logs_and_wait_for_exit",
        _fake_stream_build_job_logs_and_wait_for_exit,
    )

    await image.build_image(
        project_id="project-1",
        room="room-1",
        tag="registry.meshagent.com/project/website:1",
        pack=str(source_dir),
        context_path="/context",
        dockerfile_path="/context/Dockerfile",
        private=False,
        optimize=False,
        cred=[],
    )

    assert captured["build_kwargs"] == {
        "tags": ["registry.meshagent.com/project/website:1"],
        "mount_path": "/context",
        "context_path": "/context",
        "dockerfile_path": "/context/Dockerfile",
        "optimize_image": False,
        "private": False,
        "credentials": [],
        "builder_name": "builder",
        "size": len(archive_bytes),
    }


@pytest.mark.asyncio
async def test_build_image_pack_requires_local_dockerfile_when_used_as_context(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "website"
    source_dir.mkdir()

    with pytest.raises(
        typer.BadParameter,
        match="no Dockerfile or Containerfile found in the packed context",
    ):
        await image.build_image(
            project_id="project-1",
            room="room-1",
            tag="registry.meshagent.com/project/website:1",
            pack=str(source_dir),
            context_path=None,
            dockerfile_path=None,
            private=False,
            cred=[],
        )


def test_require_room_pack_tag_rejects_non_project_registry_tag() -> None:
    with pytest.raises(
        typer.BadParameter,
        match="PATH requires --tag to use registry.meshagent.com/<project-key>/<repository>:<tag>",
    ):
        image._require_room_pack_tag(
            parsed_tag=image._parse_build_tag("ghcr.io/example/app:1"),
            project_registry="registry.meshagent.com",
        )


@pytest.mark.asyncio
async def test_resolve_project_registry_build_credentials_rejects_project_key_mismatch():
    class _FakeAccountClient:
        async def get_project_info(self, project_id: str):
            assert project_id == "project-1"
            return ProjectInfo(
                id="project-1",
                owner_user_id="user-1",
                name="Powerboards",
                project_key="other-project",
            )

    with pytest.raises(
        typer.BadParameter,
        match="does not match the selected project",
    ):
        await image._resolve_project_registry_build_credentials(
            account_client=_FakeAccountClient(),
            project_id="project-1",
            parsed_tag=image._parse_build_tag(
                "registry.meshagent.com/powerboards/test:latest"
            ),
            project_registry="registry.meshagent.com",
        )


@pytest.mark.asyncio
async def test_resolve_project_registry_build_credentials_creates_missing_repository():
    captured: dict[str, object] = {}

    class _FakeAccountClient:
        async def get_project_info(self, project_id: str):
            captured["project_info_project_id"] = project_id
            return ProjectInfo(
                id=project_id,
                owner_user_id="user-1",
                name="Powerboards",
                project_key="powerboards",
            )

        async def list_repositories(self, *, project_id: str):
            captured["list_repositories_project_id"] = project_id
            return []

        async def create_repository(self, *, project_id: str, repository):
            captured["create_repository_request"] = {
                "project_id": project_id,
                "name": repository.name,
                "description": repository.description,
                "annotations": repository.annotations,
            }
            return ProjectRepository(
                id="repository-1",
                project_id=project_id,
                name=repository.name,
                description=repository.description,
                annotations=repository.annotations,
                created_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
            )

        async def create_repository_token(
            self,
            *,
            project_id: str,
            repository_id: str,
            request,
        ):
            captured["repository_token_request"] = {
                "project_id": project_id,
                "repository_id": repository_id,
                "actions": request.actions,
                "expires_in_seconds": request.expires_in_seconds,
            }
            return SimpleNamespace(token="repository-jwt")

    credentials = await image._resolve_project_registry_build_credentials(
        account_client=_FakeAccountClient(),
        project_id="project-1",
        parsed_tag=image._parse_build_tag(
            "registry.meshagent.com/powerboards/website-node:latest"
        ),
        project_registry="registry.meshagent.com",
    )

    assert captured["project_info_project_id"] == "project-1"
    assert captured["list_repositories_project_id"] == "project-1"
    assert captured["create_repository_request"] == {
        "project_id": "project-1",
        "name": "website-node",
        "description": "",
        "annotations": {},
    }
    assert captured["repository_token_request"] == {
        "project_id": "project-1",
        "repository_id": "repository-1",
        "actions": ["pull", "push"],
        "expires_in_seconds": 3600,
    }
    assert credentials == [
        image.DockerSecret(
            registry="registry.meshagent.com",
            username=image.DEFAULT_REGISTRY_USERNAME,
            password="repository-jwt",
        )
    ]


@pytest.mark.asyncio
async def test_resolve_project_registry_build_credentials_suggests_cli_create_when_auto_create_is_denied():
    class _FakeAccountClient:
        async def get_project_info(self, project_id: str):
            assert project_id == "project-1"
            return ProjectInfo(
                id=project_id,
                owner_user_id="user-1",
                name="Powerboards",
                project_key="powerboards",
            )

        async def list_repositories(self, *, project_id: str):
            assert project_id == "project-1"
            return []

        async def create_repository(self, *, project_id: str, repository):
            del project_id, repository
            raise PermissionDeniedError("forbidden")

    with pytest.raises(typer.BadParameter) as exc_info:
        await image._resolve_project_registry_build_credentials(
            account_client=_FakeAccountClient(),
            project_id="project-1",
            parsed_tag=image._parse_build_tag(
                "registry.meshagent.com/powerboards/website-node:latest"
            ),
            project_registry="registry.meshagent.com",
        )

    message = str(exc_info.value)
    assert (
        "the target repository does not exist in the selected project: "
        "powerboards/website-node."
    ) in message
    assert "tried to create it automatically" in message
    assert (
        "meshagent registry create --project-id project-1 --name website-node"
        in message
    )
    assert "meshagent registry list --project-id project-1" in message


@pytest.mark.asyncio
async def test_run_image_build_stage_requests_repository_token_and_prepends_registry_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    class _FakeContainersClient:
        async def build(self, **kwargs) -> str:
            captured["build_kwargs"] = kwargs
            return "build-1"

        async def list_builds(self):
            return [
                SimpleNamespace(
                    id="build-1",
                    published_images=[
                        _fake_published_build_image(
                            tag="registry.meshagent.com/powerboards/test:latest",
                            resolved_ref=(
                                "registry.meshagent.com/powerboards/test@sha256:digest"
                            ),
                        )
                    ],
                )
            ]

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = _FakeContainersClient()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def get_project_info(self, project_id: str):
            captured["project_info_project_id"] = project_id
            return ProjectInfo(
                id=project_id,
                owner_user_id="user-1",
                name="Powerboards",
                project_key="powerboards",
            )

        async def list_repositories(self, *, project_id: str):
            captured["list_repositories_project_id"] = project_id
            return [
                ProjectRepository(
                    id="repository-1",
                    project_id=project_id,
                    name="test",
                    description="Repository",
                    annotations={},
                    created_at=datetime(2026, 4, 19, tzinfo=timezone.utc),
                )
            ]

        async def create_repository_token(
            self,
            *,
            project_id: str,
            repository_id: str,
            request,
        ):
            captured["repository_token_request"] = {
                "project_id": project_id,
                "repository_id": repository_id,
                "actions": request.actions,
                "expires_in_seconds": request.expires_in_seconds,
            }
            return SimpleNamespace(token="repository-jwt")

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["with_client"] = {"project_id": project_id, "room": room}
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_build_local_context_archive(
        *,
        source_dir: Path,
        preserved_paths: frozenset[str],
    ):
        captured["archive_source_dir"] = source_dir
        captured["archive_preserved_paths"] = preserved_paths
        temp_dir = tempfile.TemporaryDirectory(prefix="meshagent-build-context-test-")
        return Path(temp_dir.name) / "context.tar", 7, temp_dir

    async def _fake_iter_file_chunks(path: Path):
        captured["iter_file_chunks_path"] = path
        yield b"context"

    async def _fake_stream_build_job_logs_and_wait_for_exit(*, client, build_id: str):
        del client
        captured["build_id"] = build_id
        return 0

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(
        image,
        "_build_local_context_archive",
        _fake_build_local_context_archive,
    )
    monkeypatch.setattr(image, "_iter_file_chunks", _fake_iter_file_chunks)
    monkeypatch.setattr(
        image,
        "_stream_build_job_logs_and_wait_for_exit",
        _fake_stream_build_job_logs_and_wait_for_exit,
    )

    await image._run_image_build_stage(
        resolved_project_id="project-1",
        resolved_room="room-1",
        parsed_tag=image._parse_build_tag(
            "registry.meshagent.com/powerboards/test:latest"
        ),
        project_registry="registry.meshagent.com",
        context_path=None,
        dockerfile_path=None,
        pack=str(source_dir),
        arch="amd64",
        builder_name=None,
        private=False,
        optimize=True,
        cred=["ghcr.io,external-user,external-pass"],
    )

    assert captured["with_client"] == {"project_id": "project-1", "room": "room-1"}
    assert captured["project_info_project_id"] == "project-1"
    assert captured["list_repositories_project_id"] == "project-1"
    assert captured["repository_token_request"] == {
        "project_id": "project-1",
        "repository_id": "repository-1",
        "actions": ["pull", "push"],
        "expires_in_seconds": 3600,
    }
    build_kwargs = captured["build_kwargs"]
    credentials = build_kwargs["credentials"]
    assert credentials[0] == image.DockerSecret(
        registry="registry.meshagent.com",
        username=image.DEFAULT_REGISTRY_USERNAME,
        password="repository-jwt",
    )
    assert credentials[1] == image.DockerSecret(
        registry="ghcr.io",
        username="external-user",
        password="external-pass",
    )
    assert captured["build_id"] == "build-1"
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_pack_image_requires_room(tmp_path: Path) -> None:
    source_dir = tmp_path / "sample"
    source_dir.mkdir()

    with pytest.raises(
        typer.BadParameter, match="--room is required unless MESHAGENT_ROOM is set"
    ):
        await image.pack_image(
            project_id=None,
            room=None,
            path=str(source_dir),
            tag="registry.meshagent.com/sample/app:1",
            base=None,
        )


@pytest.mark.asyncio
async def test_pack_image_normalizes_shorthand_tag_when_publishing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "sample"
    source_dir.mkdir()
    captured: dict[str, object] = {}

    class _FakeConfigClient:
        async def get_config(self) -> MeshagentDeploymentConfig:
            return MeshagentDeploymentConfig(
                domains=MeshagentDomains(registry="registry.meshagent.com")
            )

        async def get_project_info(self, project_id: str) -> ProjectInfo:
            captured["project_id"] = project_id
            return ProjectInfo(
                id=project_id,
                owner_user_id="user-1",
                name="Powerboards",
                project_key="powerboards",
            )

        async def close(self) -> None:
            captured["closed"] = True

    async def _fake_get_client() -> _FakeConfigClient:
        return _FakeConfigClient()

    async def _fake_resolve_project_id(*, project_id):
        captured["project_id_arg"] = project_id
        return "project-1"

    async def _fake_run_image_pack_stage(**kwargs) -> None:
        captured["pack_stage_kwargs"] = kwargs

    monkeypatch.setattr(image, "get_client", _fake_get_client)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "_run_image_pack_stage", _fake_run_image_pack_stage)

    await image.pack_image(
        project_id=None,
        room="room-1",
        path=str(source_dir),
        tag="website:1",
        base=None,
    )

    assert captured["project_id_arg"] is None
    assert captured["project_id"] == "project-1"
    assert captured["closed"] is True
    assert captured["pack_stage_kwargs"]["resolved_project_id"] == "project-1"
    assert captured["pack_stage_kwargs"]["resolved_room"] == "room-1"
    assert captured["pack_stage_kwargs"]["source_dir"] == source_dir
    assert (
        captured["pack_stage_kwargs"]["parsed_tag"].value
        == "registry.meshagent.com/powerboards/website:1"
    )
    assert captured["pack_stage_kwargs"]["project_registry"] == "registry.meshagent.com"


def test_parse_build_tag_rejects_invalid_oci_reference() -> None:
    with pytest.raises(typer.BadParameter, match="invalid OCI image repository"):
        image._parse_build_tag("Bad/Name:latest")


def test_parse_meshagent_token_scope_supports_presets_and_json() -> None:
    user_default = image._parse_meshagent_token_scope(value="userDefault")
    custom = image._parse_meshagent_token_scope(value='{"queues":{"send":["jobs"]}}')

    assert user_default.secrets is not None
    assert user_default.admin is None
    assert custom.queues is not None
    assert custom.queues.send == ["jobs"]


def test_parse_environment_secret_variables_parses_secret_values() -> None:
    environment = image._parse_environment_secret_variables(values=["API_KEY=secret-1"])

    assert environment == [
        image._ParsedEnvironmentSecretVariable(
            name="API_KEY",
            source="secret-1",
        )
    ]


def test_parse_environment_secret_variables_rejects_invalid_format() -> None:
    with pytest.raises(
        typer.BadParameter,
        match="--env-secret must be in the form 'NAME=SECRET_ID'",
    ):
        image._parse_environment_secret_variables(values=["API_KEY:secret-1"])


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [200, 302, 401, 403])
async def test_probe_liveness_url_accepts_reachable_statuses(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    class _FakeResponse:
        status = status_code

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

        def get(self, *args, **kwargs):
            del args, kwargs
            return _FakeResponse()

    monkeypatch.setattr(
        image, "new_client_session", lambda *args, **kwargs: _FakeSession()
    )

    assert await image._probe_liveness_url(url="https://sheets.meshagent.dev/") is True


@pytest.mark.asyncio
async def test_probe_liveness_url_rejects_server_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        status = 502

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

        def get(self, *args, **kwargs):
            del args, kwargs
            return _FakeResponse()

    monkeypatch.setattr(
        image, "new_client_session", lambda *args, **kwargs: _FakeSession()
    )

    assert await image._probe_liveness_url(url="https://sheets.meshagent.dev/") is False


@pytest.mark.asyncio
async def test_wait_for_deployed_service_live_streams_logs_and_checks_liveness(
    monkeypatch: pytest.MonkeyPatch,
    _stub_deploy_wait: dict[str, object],
) -> None:
    captured: dict[str, object] = {
        "prints": [],
        "started_logs": [],
        "stopped_logs": [],
        "probe_urls": [],
    }
    states = [
        ServiceRuntimeState(service_id="service-1", state="starting"),
        ServiceRuntimeState(
            service_id="service-1",
            state="starting",
            container_id="container-1",
        ),
        ServiceRuntimeState(
            service_id="service-1",
            state="running",
            container_id="container-1",
        ),
        ServiceRuntimeState(
            service_id="service-1",
            state="running",
            container_id="container-1",
        ),
    ]
    fake_active_logs = SimpleNamespace(container_id="container-1")

    class _FakeServices:
        def __init__(self, runtime_states: list[ServiceRuntimeState]) -> None:
            self._runtime_states = runtime_states
            self._index = 0

        async def list_with_state(self):
            state = self._runtime_states[
                min(self._index, len(self._runtime_states) - 1)
            ]
            self._index += 1
            return SimpleNamespace(service_states={"service-1": state})

    fake_client = SimpleNamespace(services=_FakeServices(states))

    async def _fake_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    def _fake_start_deploy_log_stream(*, client, container_id: str, log_handler=None):
        del client, log_handler
        captured["started_logs"].append(container_id)
        return fake_active_logs

    async def _fake_stop_deploy_log_stream(*, active_logs) -> None:
        captured["stopped_logs"].append(active_logs)

    probe_results = iter([False, True])

    async def _fake_probe_liveness_url(*, url: str) -> bool:
        captured["probe_urls"].append(url)
        return next(probe_results)

    monkeypatch.setattr(image.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(image, "_DEPLOY_WAIT_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        image, "_start_deploy_log_stream", _fake_start_deploy_log_stream
    )
    monkeypatch.setattr(image, "_stop_deploy_log_stream", _fake_stop_deploy_log_stream)
    monkeypatch.setattr(image, "_probe_liveness_url", _fake_probe_liveness_url)
    monkeypatch.setattr(
        image,
        "print",
        lambda *args, **kwargs: captured["prints"].append(args[0]),
    )

    wait_helper = _stub_deploy_wait["original"]
    assert callable(wait_helper)

    await wait_helper(
        client=fake_client,
        service_id="service-1",
        service_name="repo-web",
        previous_container_id=None,
        domain="app.meshagent.app",
        liveness_path="/ready",
    )

    assert captured["started_logs"] == ["container-1"]
    assert captured["stopped_logs"] == [fake_active_logs]
    assert captured["probe_urls"] == [
        "https://app.meshagent.app/ready",
        "https://app.meshagent.app/ready",
    ]
    assert any("Liveness URL responded" in message for message in captured["prints"])


@pytest.mark.asyncio
async def test_wait_for_deployed_service_live_exits_when_container_restarts(
    monkeypatch: pytest.MonkeyPatch,
    _stub_deploy_wait: dict[str, object],
) -> None:
    captured: dict[str, object] = {
        "prints": [],
        "started_logs": [],
        "stopped_logs": [],
    }
    states = [
        ServiceRuntimeState(
            service_id="service-1",
            state="starting",
            container_id="container-1",
        ),
        ServiceRuntimeState(
            service_id="service-1",
            state="restarting",
            container_id="container-1",
            restart_count=1,
            last_exit_code=137,
        ),
    ]
    fake_active_logs = SimpleNamespace(container_id="container-1")

    class _FakeServices:
        def __init__(self, runtime_states: list[ServiceRuntimeState]) -> None:
            self._runtime_states = runtime_states
            self._index = 0

        async def list_with_state(self):
            state = self._runtime_states[
                min(self._index, len(self._runtime_states) - 1)
            ]
            self._index += 1
            return SimpleNamespace(service_states={"service-1": state})

    fake_client = SimpleNamespace(services=_FakeServices(states))

    def _fake_start_deploy_log_stream(*, client, container_id: str, log_handler=None):
        del client, log_handler
        captured["started_logs"].append(container_id)
        return fake_active_logs

    async def _fake_stop_deploy_log_stream(*, active_logs) -> None:
        captured["stopped_logs"].append(active_logs)

    monkeypatch.setattr(image, "_DEPLOY_WAIT_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        image, "_start_deploy_log_stream", _fake_start_deploy_log_stream
    )
    monkeypatch.setattr(image, "_stop_deploy_log_stream", _fake_stop_deploy_log_stream)
    monkeypatch.setattr(
        image,
        "print",
        lambda *args, **kwargs: captured["prints"].append(args[0]),
    )

    wait_helper = _stub_deploy_wait["original"]
    assert callable(wait_helper)

    with pytest.raises(typer.Exit) as exc_info:
        await wait_helper(
            client=fake_client,
            service_id="service-1",
            service_name="repo-web",
            previous_container_id=None,
            domain="app.meshagent.app",
            liveness_path="/ready",
        )

    assert exc_info.value.exit_code == 1
    assert captured["started_logs"] == ["container-1"]
    assert captured["stopped_logs"] == [fake_active_logs]
    assert any(
        "exit code 137" in message and "before the service was live" in message
        for message in captured["prints"]
    )


@pytest.mark.asyncio
async def test_stop_deploy_log_stream_cancels_local_drain_task() -> None:
    canceled = False

    async def _never_returns() -> None:
        await asyncio.Future()

    class _FakeStream:
        async def cancel(self) -> None:
            nonlocal canceled
            canceled = True

    task = asyncio.create_task(_never_returns())
    active_logs = image._ActiveDeployLogStream(
        container_id="container-1",
        stream=_FakeStream(),
        task=task,
    )

    await image._stop_deploy_log_stream(active_logs=active_logs)

    assert canceled is True
    assert task.cancelled()


@pytest.mark.asyncio
async def test_deploy_image_waits_by_default(
    monkeypatch: pytest.MonkeyPatch,
    _stub_deploy_wait: dict[str, object],
) -> None:
    captured: dict[str, object] = {}

    class _FakeRoomClient:
        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def get_config(self) -> MeshagentDeploymentConfig:
            return MeshagentDeploymentConfig(
                domains=MeshagentDomains(pages="pages.meshagent.example")
            )

        async def list_room_services(self, *, project_id: str, room_name: str):
            del project_id, room_name
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ) -> str:
            del project_id, room_name, service
            return "service-1"

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:1",
        domain=None,
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        meshagent_token=None,
        private=True,
    )

    calls = _stub_deploy_wait["calls"]
    assert isinstance(calls, list)
    assert len(calls) == 1
    assert calls[0]["service_id"] == "service-1"
    assert calls[0]["service_name"] == "repo-web"
    assert calls[0]["domain"] is None


@pytest.mark.asyncio
async def test_deploy_image_no_wait_skips_live_wait(
    monkeypatch: pytest.MonkeyPatch,
    _stub_deploy_wait: dict[str, object],
) -> None:
    captured: dict[str, object] = {}

    class _FakeRoomClient:
        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            del project_id, room_name
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ) -> str:
            del project_id, room_name, service
            return "service-1"

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:1",
        domain=None,
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        meshagent_token=None,
        private=True,
        wait=False,
    )

    calls = _stub_deploy_wait["calls"]
    assert isinstance(calls, list)
    assert calls == []


@pytest.mark.asyncio
async def test_deploy_image_normalizes_shorthand_tag_without_build_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeConfigClient:
        async def get_config(self) -> MeshagentDeploymentConfig:
            captured["get_config_called"] = True
            return MeshagentDeploymentConfig(
                domains=MeshagentDomains(registry="registry.meshagent.com")
            )

        async def get_project_info(self, project_id: str) -> ProjectInfo:
            captured["project_info_project_id"] = project_id
            return ProjectInfo(
                id=project_id,
                owner_user_id="user-1",
                name="Powerboards",
                project_key="powerboards",
            )

        async def close(self) -> None:
            captured["config_client_closed"] = True

    class _FakeRoomClient:
        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def get_config(self) -> MeshagentDeploymentConfig:
            return MeshagentDeploymentConfig(
                domains=MeshagentDomains(pages="pages.meshagent.example")
            )

        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ):
            captured["created_service"] = (project_id, room_name, service)
            return "service-1"

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_get_client() -> _FakeConfigClient:
        return _FakeConfigClient()

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        captured["project_id_arg"] = project_id
        return "project-1"

    monkeypatch.setattr(image, "get_client", _fake_get_client)
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id=None,
        room="room-1",
        tag="test:latest",
        domain=None,
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        meshagent_token=None,
        private=True,
        wait=False,
    )

    assert captured["project_id_arg"] is None
    assert captured["project_info_project_id"] == "project-1"
    assert captured["config_client_closed"] is True
    assert captured["project_id"] == "project-1"
    assert captured["room"] == "room-1"
    created_service = captured["created_service"]
    assert isinstance(created_service, tuple)
    service_spec = created_service[2]
    assert service_spec.container is not None
    assert (
        service_spec.container.image == "registry.meshagent.com/powerboards/test:latest"
    )
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_deploy_image_creates_room_service_with_mounts_env_secret_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeServices:
        async def restart(self, *, service_id: str) -> None:
            captured["restarted_service_id"] = service_id

        async def list_with_state(self):
            return SimpleNamespace(
                service_states={
                    "service-1": ServiceRuntimeState(
                        service_id="service-1",
                        state="running",
                        container_id="container-old",
                    )
                }
            )

    class _FakeSecrets:
        async def exists(
            self,
            *,
            secret_id: str,
            delegated_to: str | None = None,
            for_identity: str | None = None,
        ) -> bool:
            captured.setdefault("secret_checks", []).append(
                {
                    "secret_id": secret_id,
                    "delegated_to": delegated_to,
                    "for_identity": for_identity,
                }
            )
            return True

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = _FakeServices()
            self.secrets = _FakeSecrets()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ):
            captured["created_service"] = (project_id, room_name, service)
            return "service-1"

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:1",
        domain=None,
        room_mount=["/assets:/srv/assets:ro"],
        project_mount=["configs:/etc/config:rw"],
        empty_dir_mount=["/tmp/cache"],
        image_mount=["busybox=/opt/base:rw"],
        env=["FOO=bar"],
        env_secret=["APP_SECRET=secret-1"],
        meshagent_token="agentDefault",
        private=True,
    )

    created_service = captured["created_service"]
    assert isinstance(created_service, tuple)
    assert created_service[0] == "project-1"
    assert created_service[1] == "room-1"
    service_spec = created_service[2]
    assert service_spec.metadata.name == "repo-web"
    assert service_spec.container is not None
    assert service_spec.container.image == "repo/web:1"
    assert service_spec.container.storage is not None
    assert service_spec.container.storage.room is not None
    assert service_spec.container.storage.room[0].subpath == "/assets"
    assert service_spec.container.storage.room[0].path == "/srv/assets"
    assert service_spec.container.storage.room[0].read_only is True
    assert service_spec.container.storage.project is not None
    assert service_spec.container.storage.project[0].subpath == "configs"
    assert service_spec.container.storage.project[0].path == "/etc/config"
    assert service_spec.container.storage.project[0].read_only is False
    assert service_spec.container.storage.images is not None
    assert service_spec.container.storage.images[0].image == "busybox"
    assert service_spec.container.storage.images[0].path == "/opt/base"
    assert service_spec.container.storage.images[0].read_only is False
    assert service_spec.container.storage.empty_dirs is not None
    assert service_spec.container.storage.empty_dirs[0].path == "/tmp/cache"
    env_by_name = {
        env_var.name: env_var for env_var in (service_spec.container.environment or [])
    }
    assert env_by_name["FOO"].value == "bar"
    assert env_by_name["APP_SECRET"].secret == SecretValue(
        identity="repo-web",
        id="secret-1",
    )
    assert env_by_name["MESHAGENT_TOKEN"].token is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.identity == "repo-web"
    assert env_by_name["MESHAGENT_TOKEN"].token.api is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.api.secrets is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.api.services is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.role == "agent"
    for env_name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert env_by_name[env_name].token is not None
        assert env_by_name[env_name].token == env_by_name["MESHAGENT_TOKEN"].token
    assert captured["secret_checks"] == [
        {
            "secret_id": "secret-1",
            "delegated_to": None,
            "for_identity": "repo-web",
        }
    ]
    assert "restarted_service_id" not in captured
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_deploy_image_identity_overrides_env_secret_and_token_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeSecrets:
        async def exists(
            self,
            *,
            secret_id: str,
            delegated_to: str | None = None,
            for_identity: str | None = None,
        ) -> bool:
            captured.setdefault("secret_checks", []).append(
                {
                    "secret_id": secret_id,
                    "delegated_to": delegated_to,
                    "for_identity": for_identity,
                }
            )
            return True

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.secrets = _FakeSecrets()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ) -> str:
            captured["created_service"] = (project_id, room_name, service)
            return "service-1"

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:1",
        domain=None,
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        env_secret=["APP_SECRET=secret-1"],
        identity="custom-agent",
        meshagent_token="agentDefault",
        private=True,
    )

    created_service = captured["created_service"]
    assert isinstance(created_service, tuple)
    service_spec = created_service[2]
    assert service_spec.container is not None
    env_by_name = {
        env_var.name: env_var for env_var in (service_spec.container.environment or [])
    }
    assert env_by_name["APP_SECRET"].secret == SecretValue(
        identity="custom-agent",
        id="secret-1",
    )
    assert env_by_name["MESHAGENT_TOKEN"].token is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.identity == "custom-agent"
    for env_name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert env_by_name[env_name].token is not None
        assert env_by_name[env_name].token == env_by_name["MESHAGENT_TOKEN"].token
    assert captured["secret_checks"] == [
        {
            "secret_id": "secret-1",
            "delegated_to": None,
            "for_identity": "custom-agent",
        }
    ]
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_deploy_image_env_secret_requires_matching_token_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeSecrets:
        async def exists(
            self,
            *,
            secret_id: str,
            delegated_to: str | None = None,
            for_identity: str | None = None,
        ) -> bool:
            del secret_id, delegated_to, for_identity
            captured["exists_called"] = True
            return True

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.secrets = _FakeSecrets()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ) -> str:
            del project_id, room_name, service
            captured["create_room_service_called"] = True
            return "service-1"

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    with pytest.raises(
        typer.BadParameter,
        match="no environment token is defined for identity 'other-agent'",
    ):
        await image.deploy_image(
            project_id="project-1",
            room="room-1",
            tag="repo/web:1",
            domain=None,
            room_mount=[],
            project_mount=[],
            empty_dir_mount=[],
            image_mount=[],
            env=[],
            env_secret=["APP_SECRET=secret-1"],
            identity="other-agent",
            meshagent_token=None,
            private=True,
        )

    assert "exists_called" not in captured
    assert "create_room_service_called" not in captured
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_delete_built_image_from_room_cache_ignores_missing_image() -> None:
    captured: dict[str, object] = {}

    class _FakeContainers:
        async def delete_image(self, *, image: str) -> None:
            captured["deleted_image"] = image
            raise RoomException("image not found", code=ErrorCode.NOT_FOUND)

    client = SimpleNamespace(containers=_FakeContainers())

    await image._delete_built_image_from_room_cache(
        client=client,
        parsed_tag=image._parse_build_tag("registry.meshagent.com/repo/web:1"),
    )

    assert captured["deleted_image"] == "registry.meshagent.com/repo/web:1"


@pytest.mark.asyncio
async def test_delete_built_image_from_room_cache_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image, "_DEPLOY_CACHE_CLEANUP_TIMEOUT_SECONDS", 0.01)

    class _FakeContainers:
        async def delete_image(self, *, image: str) -> None:
            del image
            await asyncio.sleep(1)

    client = SimpleNamespace(containers=_FakeContainers())

    with pytest.raises(RuntimeError, match="timed out cleaning up"):
        await image._delete_built_image_from_room_cache(
            client=client,
            parsed_tag=image._parse_build_tag("registry.meshagent.com/repo/web:1"),
        )


def test_built_service_image_refs_selects_built_refs() -> None:
    old_digest = "sha256:" + "a" * 64
    new_digest = "sha256:" + "b" * 64
    other_digest = "sha256:" + "c" * 64
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        metadata=ServiceMetadata(name="repo-web"),
        container=ContainerSpec(
            image=f"registry.meshagent.com/repo/web@{old_digest}",
            storage=ContainerMountSpec(
                images=[
                    ImageStorageMountSpec(
                        image=f"registry.meshagent.com/repo/web@{old_digest}",
                        path="/app",
                    ),
                    ImageStorageMountSpec(
                        image=f"registry.meshagent.com/repo/web@{new_digest}",
                        path="/current",
                    ),
                    ImageStorageMountSpec(
                        image=f"registry.meshagent.com/repo/other@{other_digest}",
                        path="/other",
                    ),
                    ImageStorageMountSpec(
                        image="meshagent/node:default",
                        path="/runtime",
                    ),
                    ImageStorageMountSpec(
                        image="registry.meshagent.com/repo/web:latest",
                        path="/tag",
                    ),
                    ImageStorageMountSpec(
                        image="registry.meshagent.com/repo/web:old",
                        path="/old-tag",
                    ),
                ]
            ),
        ),
    )
    refs = image._built_service_image_refs(
        service_spec=existing_service,
        parsed_tag=image._parse_build_tag("registry.meshagent.com/repo/web:1"),
        project_registry="registry.meshagent.com",
    )

    assert refs == [
        f"registry.meshagent.com/repo/web@{old_digest}",
        f"registry.meshagent.com/repo/web@{new_digest}",
        "registry.meshagent.com/repo/web:latest",
        "registry.meshagent.com/repo/web:old",
    ]


@pytest.mark.asyncio
async def test_delete_replaced_built_service_images_ignores_missing_image() -> None:
    captured: dict[str, object] = {}

    class _FakeContainers:
        async def delete_image(self, *, image: str) -> None:
            captured["deleted_image"] = image
            raise RoomException("image not found", code=ErrorCode.NOT_FOUND)

    client = SimpleNamespace(containers=_FakeContainers())

    await image._delete_replaced_built_service_images(
        client=client,
        image_refs=["registry.meshagent.com/repo/web@sha256:" + "a" * 64],
    )

    assert captured["deleted_image"] == (
        "registry.meshagent.com/repo/web@sha256:" + "a" * 64
    )


@pytest.mark.asyncio
async def test_apply_deploy_plan_reports_service_apply_steps() -> None:
    statuses: list[str] = []
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        id="service-1",
        metadata=ServiceMetadata(name="repo-web"),
        ports=[PortSpec(num=8080, type="http", published=True)],
        container=ContainerSpec(image="repo/web:old"),
    )
    deploy_plan = image._ServiceDeployPlan(
        spec=ServiceSpec(
            version="v1",
            kind="Service",
            metadata=ServiceMetadata(name="repo-web"),
            ports=[PortSpec(num=8080, type="http", published=True)],
            container=ContainerSpec(image="repo/web:2"),
        ),
        service_id_annotation="repo-web",
    )

    class _FakeServices:
        async def restart(self, *, service_id: str) -> None:
            assert service_id == "service-1"

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = _FakeServices()

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            assert project_id == "project-1"
            assert room_name == "room-1"
            return [existing_service]

        async def update_room_service(
            self,
            *,
            project_id: str,
            room_name: str,
            service_id: str,
            service,
        ) -> None:
            assert project_id == "project-1"
            assert room_name == "room-1"
            assert service_id == "service-1"
            assert service.container is not None
            assert service.container.image == "repo/web:2"

    async def _status(message: str) -> None:
        statuses.append(message)

    result = await image._apply_deploy_plan(
        account_client=_FakeAccountClient(),
        client=_FakeRoomClient(),
        project_id="project-1",
        room_name="room-1",
        deploy_plan=deploy_plan,
        domain=None,
        email=None,
        extra_route_ports=[],
        status_handler=_status,
    )

    assert result.service_id == "service-1"
    assert statuses == [
        "Checking service: repo-web",
        "Updating service: repo-web",
        "Deployed service: repo-web (service-1)",
        "Restarting service: repo-web (service-1)",
        "Restarted service: repo-web (service-1)",
    ]


@pytest.mark.asyncio
async def test_apply_deploy_plan_times_out_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image, "_DEPLOY_SERVICE_RESTART_TIMEOUT_SECONDS", 0.01)
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        id="service-1",
        metadata=ServiceMetadata(name="repo-web"),
        ports=[PortSpec(num=8080, type="http", published=True)],
        container=ContainerSpec(image="repo/web:old"),
    )
    deploy_plan = image._ServiceDeployPlan(
        spec=ServiceSpec(
            version="v1",
            kind="Service",
            metadata=ServiceMetadata(name="repo-web"),
            ports=[PortSpec(num=8080, type="http", published=True)],
            container=ContainerSpec(image="repo/web:2"),
        ),
        service_id_annotation="repo-web",
    )

    class _FakeServices:
        async def restart(self, *, service_id: str) -> None:
            del service_id
            await asyncio.sleep(60)

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = _FakeServices()

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            del project_id, room_name
            return [existing_service]

        async def update_room_service(
            self,
            *,
            project_id: str,
            room_name: str,
            service_id: str,
            service,
        ) -> None:
            del project_id, room_name, service_id, service

    with pytest.raises(RuntimeError, match="timed out restarting deployed service"):
        await image._apply_deploy_plan(
            account_client=_FakeAccountClient(),
            client=_FakeRoomClient(),
            project_id="project-1",
            room_name="room-1",
            deploy_plan=deploy_plan,
            domain=None,
            email=None,
            extra_route_ports=[],
        )


@pytest.mark.asyncio
async def test_deploy_image_pack_builds_before_deploying(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from meshagent.cli.tui.deploy_room import (
        DeployDomainPromptResult,
        DeployProgressResult,
    )

    captured: dict[str, object] = {"events": []}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text(
        "FROM scratch\nEXPOSE 8080\n",
        encoding="utf-8",
    )

    async def _fake_run_image_build_stage(**kwargs) -> PublishedBuildImage:
        captured["build_kwargs"] = kwargs
        captured["events"].append("build")
        return _fake_published_build_image()

    async def _fake_run_deploy_domain_prompt_tui(
        *, service_name: str, port: str, room_name: str, pages_domain: str
    ) -> DeployDomainPromptResult:
        captured["events"].append("domain_tui")
        captured["domain_tui"] = {
            "service_name": service_name,
            "port": port,
            "room_name": room_name,
            "pages_domain": pages_domain,
        }
        return DeployDomainPromptResult(status="skipped")

    async def _fake_run_deploy_progress_tui(*, operation):
        class _FakeProgress:
            async def status(self, message: str) -> None:
                del message

            async def transient_status(self, message: str) -> None:
                del message

            async def log(self, message: str) -> None:
                del message

            async def prompt_domain(
                self,
                *,
                service_name: str,
                port: str,
                room_name: str,
                pages_domain: str,
            ) -> DeployDomainPromptResult:
                return await _fake_run_deploy_domain_prompt_tui(
                    service_name=service_name,
                    port=port,
                    room_name=room_name,
                    pages_domain=pages_domain,
                )

        await operation(_FakeProgress())
        return DeployProgressResult(status="completed")

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = SimpleNamespace(delete_image=self._delete_image)

        async def _delete_image(self, *, image: str) -> None:
            captured["events"].append("delete")
            captured["deleted_image"] = image

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def get_config(self) -> MeshagentDeploymentConfig:
            return MeshagentDeploymentConfig(
                domains=MeshagentDomains(pages="pages.meshagent.example")
            )

        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ):
            captured["events"].append("create")
            captured["created_service"] = (project_id, room_name, service)
            return "service-1"

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_run_image_build_stage", _fake_run_image_build_stage)
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        "meshagent.cli.tui.deploy_room.run_deploy_domain_prompt_tui",
        _fake_run_deploy_domain_prompt_tui,
    )
    monkeypatch.setattr(
        "meshagent.cli.tui.deploy_room.run_deploy_progress_tui",
        _fake_run_deploy_progress_tui,
    )
    monkeypatch.setenv("MESHAGENT_ARCH", "arm64")
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="registry.meshagent.com/repo/web:1",
        pack=str(source_dir),
        context_path="/context",
        dockerfile_path="/context/Dockerfile",
        optimize=False,
        cred=["registry,user,password"],
        domain=None,
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        meshagent_token=None,
    )

    build_kwargs = captured["build_kwargs"]
    assert build_kwargs["resolved_project_id"] == "project-1"
    assert build_kwargs["resolved_room"] == "room-1"
    assert build_kwargs["parsed_tag"].value == "registry.meshagent.com/repo/web:1"
    assert build_kwargs["project_registry"] == "registry.meshagent.com"
    assert build_kwargs["context_path"] == "/context"
    assert build_kwargs["dockerfile_path"] == "/context/Dockerfile"
    assert build_kwargs["pack"] == str(source_dir)
    assert build_kwargs["arch"] == "arm64"
    assert build_kwargs["builder_name"] is None
    assert build_kwargs["private"] is False
    assert build_kwargs["optimize"] is False
    assert build_kwargs["cred"] == ["registry,user,password"]
    assert build_kwargs["add_latest_tag"] is False
    assert captured["domain_tui"] == {
        "service_name": "repo-web",
        "port": "8080",
        "room_name": "room-1",
        "pages_domain": "pages.meshagent.example",
    }
    assert captured["deleted_image"] == "registry.meshagent.com/repo/web:1"
    assert captured["events"] == ["domain_tui", "build", "delete", "create"]
    created_service = captured["created_service"]
    assert isinstance(created_service, tuple)
    service_spec = created_service[2]
    assert service_spec.container is not None
    assert (
        service_spec.container.image == "registry.meshagent.com/repo/web@sha256:digest"
    )
    assert service_spec.metadata.annotations is not None
    assert image.ANNOTATION_REQUEST_VALIDATION_METHOD not in (
        service_spec.metadata.annotations
    )
    assert service_spec.ports is not None
    assert service_spec.ports[0].num == 8080
    assert service_spec.ports[0].published is True
    assert service_spec.ports[0].public is None
    assert service_spec.ports[0].liveness == "/"
    assert service_spec.ports[0].annotations is None
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_deploy_image_pack_fails_before_build_when_env_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text(
        "FROM scratch\nEXPOSE 8080\n",
        encoding="utf-8",
    )

    async def _fake_run_image_build_stage(**kwargs) -> PublishedBuildImage:
        del kwargs
        captured["build_called"] = True
        return _fake_published_build_image()

    class _FakeSecrets:
        async def exists(
            self,
            *,
            secret_id: str,
            delegated_to: str | None = None,
            for_identity: str | None = None,
        ) -> bool:
            captured["secret_check"] = {
                "secret_id": secret_id,
                "delegated_to": delegated_to,
                "for_identity": for_identity,
            }
            return False

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.secrets = _FakeSecrets()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    printed: list[str] = []
    monkeypatch.setattr(image, "_run_image_build_stage", _fake_run_image_build_stage)
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda message: printed.append(message))

    with pytest.raises(
        typer.BadParameter,
        match="references missing secret 'repo-web/secret-1'",
    ):
        await image.deploy_image(
            project_id="project-1",
            room="room-1",
            tag="registry.meshagent.com/repo/web:1",
            pack=str(source_dir),
            context_path="/context",
            dockerfile_path="/context/Dockerfile",
            optimize=False,
            domain=None,
            room_mount=[],
            project_mount=[],
            empty_dir_mount=[],
            image_mount=[],
            env=[],
            env_secret=["APP_SECRET=secret-1"],
            meshagent_token="agentDefault",
        )

    assert captured["secret_check"] == {
        "secret_id": "secret-1",
        "delegated_to": None,
        "for_identity": "repo-web",
    }
    assert "build_called" not in captured
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_deploy_image_pack_requires_matching_volume_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text(
        'FROM scratch\nVOLUME ["/data"]\n',
        encoding="utf-8",
    )

    async def _fake_run_image_build_stage(**kwargs) -> PublishedBuildImage:
        captured["build_kwargs"] = kwargs
        return _fake_published_build_image()

    class _FakeRoomClient:
        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    printed: list[str] = []
    monkeypatch.setattr(image, "_run_image_build_stage", _fake_run_image_build_stage)
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda message: printed.append(message))

    with pytest.raises(
        typer.BadParameter,
        match="declares VOLUME path /data",
    ):
        await image.deploy_image(
            project_id="project-1",
            room="room-1",
            tag="registry.meshagent.com/repo/web:1",
            pack=str(source_dir),
            domain=None,
            room_mount=[],
            project_mount=[],
            empty_dir_mount=[],
            image_mount=[],
            env=[],
            meshagent_token=None,
        )

    assert "build_kwargs" not in captured
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_deploy_image_pack_allows_matching_volume_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {"events": []}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text(
        'FROM scratch\nVOLUME ["/data"]\n',
        encoding="utf-8",
    )

    async def _fake_run_image_build_stage(**kwargs) -> PublishedBuildImage:
        captured["build_kwargs"] = kwargs
        captured["events"].append("build")
        return _fake_published_build_image()

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = SimpleNamespace(delete_image=self._delete_image)

        async def _delete_image(self, *, image: str) -> None:
            captured["events"].append("delete")
            captured["deleted_image"] = image

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ):
            captured["events"].append("create")
            captured["created_service"] = (project_id, room_name, service)
            return "service-1"

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    printed: list[str] = []
    monkeypatch.setattr(image, "_run_image_build_stage", _fake_run_image_build_stage)
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda message: printed.append(message))

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="registry.meshagent.com/repo/web:1",
        pack=str(source_dir),
        domain=None,
        room_mount=[],
        project_mount=[],
        empty_dir_mount=["/data"],
        image_mount=[],
        env=[],
        meshagent_token=None,
    )

    created_service = captured["created_service"]
    assert isinstance(created_service, tuple)
    service_spec = created_service[2]
    assert service_spec.container is not None
    assert service_spec.container.storage is not None
    assert service_spec.container.storage.empty_dirs is not None
    assert service_spec.container.storage.empty_dirs[0].path == "/data"
    assert captured["deleted_image"] == "registry.meshagent.com/repo/web:1"
    assert captured["events"] == ["build", "delete", "create"]
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_deploy_image_pack_runtime_label_uses_cached_runtime_base(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {"events": []}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text(
        """
FROM scratch
LABEL meshagent.runtime=node
WORKDIR /app
ENV NODE_ENV=production PORT=8111
EXPOSE 8111
CMD ["/app/dist/index.js"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    async def _fake_run_image_build_stage(**kwargs) -> PublishedBuildImage:
        captured["build_kwargs"] = kwargs
        captured["events"].append("build")
        return _fake_published_build_image()

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = SimpleNamespace(delete_image=self._delete_image)

        async def _delete_image(self, *, image: str) -> None:
            captured["events"].append("delete")
            captured["deleted_image"] = image

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ):
            captured["events"].append("create")
            captured["created_service"] = (project_id, room_name, service)
            return "service-1"

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_run_image_build_stage", _fake_run_image_build_stage)
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="registry.meshagent.com/repo/web:1",
        pack=str(source_dir),
        context_path="/context",
        dockerfile_path="/context/Dockerfile",
        optimize=True,
        domain=None,
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        meshagent_token=None,
    )

    created_service = captured["created_service"]
    assert isinstance(created_service, tuple)
    service_spec = created_service[2]
    assert service_spec.container is not None
    assert service_spec.container.image == IMAGE_RUNTIME_BASES["node"].base_image
    assert service_spec.container.command == "node /app/dist/index.js"
    assert service_spec.container.working_dir == "/app"
    assert service_spec.container.storage is not None
    assert service_spec.container.storage.images is not None
    assert service_spec.container.storage.images == [
        ImageStorageMountSpec(
            image="registry.meshagent.com/repo/web@sha256:digest",
            path=IMAGE_RUNTIME_MOUNT_PATH,
            subpath=IMAGE_RUNTIME_MOUNT_SUBPATH,
            read_only=True,
        )
    ]
    assert service_spec.container.environment == [
        EnvironmentVariable(name="NODE_ENV", value="production"),
        EnvironmentVariable(name="PORT", value="8111"),
    ]
    assert service_spec.ports is not None
    assert service_spec.ports[0].num == 8111
    assert captured["deleted_image"] == "registry.meshagent.com/repo/web:1"
    assert captured["events"] == ["build", "delete", "create"]
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_resolve_deploy_template_values_uses_saved_values_and_set_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values_file = tmp_path / ".meshagent" / "values.yaml"
    values_file.parent.mkdir()
    values_file.write_text(
        "domain: old.example.com\nimage: repo/old:1\n",
        encoding="utf-8",
    )
    template = image.ServiceTemplateSpec.from_yaml(
        yaml="""
version: v1
kind: ServiceTemplate
metadata:
  name: demo
variables:
  - name: domain
    type: route
  - name: image
    optional: true
container:
  image: "{{ image }}"
""",
        values={},
    )
    monkeypatch.setattr(image, "_stdio_is_interactive", lambda: False)

    values = await image._resolve_deploy_template_values(
        account_client=_FakeDeployConfigClient(
            pages="example.com", mail="mail.example.com"
        ),
        project_id="project-1",
        template=template,
        room_name="room-1",
        service_name="demo",
        values_file=values_file,
        extra_values_files=[],
        set_values=["domain=new.example.com"],
        image="repo/demo:1",
    )

    assert values == {"domain": "new.example.com", "image": "repo/demo:1"}


@pytest.mark.asyncio
async def test_resolve_deploy_template_values_ignores_stale_saved_domain_without_route_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values_file = tmp_path / ".meshagent" / "values.yaml"
    values_file.parent.mkdir()
    values_file.write_text("domain: stale.meshagent.dev\n", encoding="utf-8")
    template = image.ServiceTemplateSpec.from_yaml(
        yaml="""
version: v1
kind: ServiceTemplate
metadata:
  name: demo
variables:
  - name: image
    optional: true
container:
  image: "{{ image }}"
""",
        values={},
    )
    monkeypatch.setattr(image, "_stdio_is_interactive", lambda: False)

    values = await image._resolve_deploy_template_values(
        account_client=_FakeDeployConfigClient(),
        project_id="project-1",
        template=template,
        room_name="room-2",
        service_name="demo",
        values_file=values_file,
        extra_values_files=[],
        set_values=[],
        image="repo/demo:1",
    )

    assert values == {"domain": "stale.meshagent.dev", "image": "repo/demo:1"}
    assert image._service_template_route_values(template=template, values=values) == []


@pytest.mark.asyncio
async def test_resolve_deploy_template_values_discards_saved_route_for_other_room(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values_file = tmp_path / ".meshagent" / "values.yaml"
    values_file.parent.mkdir()
    values_file.write_text(
        "domain: old-room.pages.meshagent.example\n", encoding="utf-8"
    )
    template = image.ServiceTemplateSpec.from_yaml(
        yaml="""
version: v1
kind: ServiceTemplate
metadata:
  name: demo
variables:
  - name: domain
    type: route
    optional: true
  - name: image
    optional: true
container:
  image: "{{ image }}"
""",
        values={},
    )
    prompts: list[tuple[str, str]] = []

    async def _fake_run_deploy_template_variables_tui(*, variables):
        from meshagent.cli.tui.deploy_room import DeployTemplateVariablesResult

        values = {}
        for variable in variables:
            prompts.append((variable.name, variable.default))
            values[variable.name] = variable.default
        return DeployTemplateVariablesResult(status="completed", values=values)

    monkeypatch.setattr(image, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        "meshagent.cli.tui.deploy_room.run_deploy_template_variables_tui",
        _fake_run_deploy_template_variables_tui,
    )

    values = await image._resolve_deploy_template_values(
        account_client=_FakeDeployConfigClient(
            pages="pages.meshagent.example",
            routes={
                "old-room.pages.meshagent.example": _route_for_service(
                    domain="old-room.pages.meshagent.example",
                    room_name="old-room",
                    port="8000",
                    service_id="old-service",
                )
            },
        ),
        project_id="project-1",
        template=template,
        room_name="new-room",
        service_name="demo",
        values_file=values_file,
        extra_values_files=[],
        set_values=[],
        image="repo/demo:1",
    )

    assert values["domain"] == "new-room.pages.meshagent.example"
    assert image._service_template_route_values(template=template, values=values) == [
        "new-room.pages.meshagent.example"
    ]
    assert prompts == [("domain", "new-room.pages.meshagent.example")]


@pytest.mark.asyncio
async def test_resolve_deploy_template_values_discards_saved_email_for_other_room(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values_file = tmp_path / ".meshagent" / "values.yaml"
    values_file.parent.mkdir()
    values_file.write_text(
        "from_email: old-room@mail.meshagent.example\n", encoding="utf-8"
    )
    template = image.ServiceTemplateSpec.from_yaml(
        yaml="""
version: v1
kind: ServiceTemplate
metadata:
  name: demo
variables:
  - name: from_email
    type: email
    optional: true
  - name: image
    optional: true
container:
  image: "{{ image }}"
""",
        values={},
    )
    prompts: list[tuple[str, str]] = []

    async def _fake_run_deploy_template_variables_tui(*, variables):
        from meshagent.cli.tui.deploy_room import DeployTemplateVariablesResult

        values = {}
        for variable in variables:
            prompts.append((variable.name, variable.default))
            values[variable.name] = variable.default
        return DeployTemplateVariablesResult(status="completed", values=values)

    monkeypatch.setattr(image, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        "meshagent.cli.tui.deploy_room.run_deploy_template_variables_tui",
        _fake_run_deploy_template_variables_tui,
    )

    values = await image._resolve_deploy_template_values(
        account_client=_FakeDeployConfigClient(
            mail="mail.meshagent.example",
            mailboxes={
                "old-room@mail.meshagent.example": Mailbox(
                    address="old-room@mail.meshagent.example",
                    room="old-room",
                    queue="old-room@mail.meshagent.example",
                    public=True,
                    annotations={},
                )
            },
        ),
        project_id="project-1",
        template=template,
        room_name="new-room",
        service_name="demo",
        values_file=values_file,
        extra_values_files=[],
        set_values=[],
        image="repo/demo:1",
    )

    assert values["from_email"] == "demo-new-room-2cd45dcc@mail.meshagent.example"
    assert image._service_template_email_values(template=template, values=values) == [
        "demo-new-room-2cd45dcc@mail.meshagent.example"
    ]
    assert prompts == [("from_email", "demo-new-room-2cd45dcc@mail.meshagent.example")]


@pytest.mark.asyncio
async def test_resolve_deploy_template_values_requires_values_when_not_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = image.ServiceTemplateSpec.from_yaml(
        yaml="""
version: v1
kind: ServiceTemplate
metadata:
  name: demo
variables:
  - name: to_email
    type: email
container:
  image: "{{ image }}"
""",
        values={},
    )
    monkeypatch.setattr(image, "_stdio_is_interactive", lambda: False)

    with pytest.raises(
        typer.BadParameter, match="missing required deploy template values"
    ):
        await image._resolve_deploy_template_values(
            account_client=_FakeDeployConfigClient(
                pages="example.com", mail="mail.example.com"
            ),
            project_id="project-1",
            template=template,
            room_name="room-1",
            service_name="demo",
            values_file=tmp_path / ".meshagent" / "values.yaml",
            extra_values_files=[],
            set_values=[],
            image="repo/demo:1",
        )


@pytest.mark.asyncio
async def test_resolve_deploy_template_values_defaults_domains_from_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = image.ServiceTemplateSpec.from_yaml(
        yaml="""
version: v1
kind: ServiceTemplate
metadata:
  name: demo
variables:
  - name: domain
    type: route
  - name: from_email
    type: email
  - name: image
    optional: true
container:
  image: "{{ image }}"
""",
        values={},
    )
    prompts: list[tuple[str, str]] = []

    async def _fake_run_deploy_template_variables_tui(*, variables):
        from meshagent.cli.tui.deploy_room import DeployTemplateVariablesResult

        values = {}
        for variable in variables:
            prompts.append((variable.title, variable.default))
            values[variable.name] = variable.default
        return DeployTemplateVariablesResult(status="completed", values=values)

    monkeypatch.setattr(image, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        "meshagent.cli.tui.deploy_room.run_deploy_template_variables_tui",
        _fake_run_deploy_template_variables_tui,
    )

    values = await image._resolve_deploy_template_values(
        account_client=_FakeDeployConfigClient(
            pages="pages.meshagent.example",
            mail="mail.meshagent.example",
        ),
        project_id="project-1",
        template=template,
        room_name="Contact Room",
        service_name="contact-form",
        values_file=tmp_path / ".meshagent" / "values.yaml",
        extra_values_files=[],
        set_values=[],
        image="repo/demo:1",
    )

    assert values["domain"] == "contact-room.pages.meshagent.example"
    assert (
        values["from_email"]
        == "contact-form-contact-room-21c8b685@mail.meshagent.example"
    )
    assert prompts == [
        ("Domain", "contact-room.pages.meshagent.example"),
        (
            "From_email",
            "contact-form-contact-room-21c8b685@mail.meshagent.example",
        ),
    ]


@pytest.mark.asyncio
async def test_resolve_deploy_template_values_enforces_configured_route_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = image.ServiceTemplateSpec.from_yaml(
        yaml="""
version: v1
kind: ServiceTemplate
metadata:
  name: demo
variables:
  - name: domain
    type: route
container:
  image: image
""",
        values={},
    )
    monkeypatch.setattr(image, "_stdio_is_interactive", lambda: False)

    with pytest.raises(typer.BadParameter, match="configured pages domain suffix"):
        await image._resolve_deploy_template_values(
            account_client=_FakeDeployConfigClient(pages="pages.meshagent.example"),
            project_id="project-1",
            template=template,
            room_name="room-1",
            service_name="demo",
            values_file=tmp_path / ".meshagent" / "values.yaml",
            extra_values_files=[],
            set_values=["domain=demo.example.com"],
            image="repo/demo:1",
        )


@pytest.mark.asyncio
async def test_resolve_deploy_template_values_enforces_configured_mail_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = image.ServiceTemplateSpec.from_yaml(
        yaml="""
version: v1
kind: ServiceTemplate
metadata:
  name: demo
variables:
  - name: from_email
    type: email
container:
  image: image
""",
        values={},
    )
    monkeypatch.setattr(image, "_stdio_is_interactive", lambda: False)

    with pytest.raises(typer.BadParameter, match="configured mail domain"):
        await image._resolve_deploy_template_values(
            account_client=_FakeDeployConfigClient(mail="mail.meshagent.example"),
            project_id="project-1",
            template=template,
            room_name="room-1",
            service_name="demo",
            values_file=tmp_path / ".meshagent" / "values.yaml",
            extra_values_files=[],
            set_values=["from_email=demo@example.com"],
            image="repo/demo:1",
        )


@pytest.mark.asyncio
async def test_deploy_image_pack_domain_uses_inferred_exposed_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {"events": []}
    printed: list[str] = []
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text(
        "FROM node:22-alpine\nEXPOSE 8080\n",
        encoding="utf-8",
    )

    async def _fake_run_image_build_stage(**kwargs) -> PublishedBuildImage:
        captured["build_kwargs"] = kwargs
        captured["events"].append("build")
        return _fake_published_build_image()

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = SimpleNamespace(restart=self._restart)
            self.containers = SimpleNamespace(delete_image=self._delete_image)

        async def _restart(self, *, service_id: str) -> None:
            captured["restarted_service_id"] = service_id

        async def _delete_image(self, *, image: str) -> None:
            captured["events"].append("delete")
            captured["deleted_image"] = image

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ):
            captured["events"].append("create")
            captured["created_service"] = (project_id, room_name, service)
            return "service-1"

        async def create_route(
            self,
            *,
            project_id: str,
            spec,
        ) -> None:
            captured["events"].append("route")
            captured["created_route"] = (project_id, spec)

        async def get_mailbox(self, *, project_id: str, address: str):
            captured["mailbox_lookup"] = (project_id, address)
            raise NotFoundError("Status=404, body=mailbox not found")

        async def create_mailbox(
            self,
            *,
            project_id: str,
            address: str,
            room: str,
            queue: str,
            public: bool,
            annotations,
        ) -> None:
            captured["events"].append("mailbox")
            captured["created_mailbox"] = {
                "project_id": project_id,
                "address": address,
                "room": room,
                "queue": queue,
                "public": public,
                "annotations": annotations,
            }

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_run_image_build_stage", _fake_run_image_build_stage)
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda message: printed.append(message))

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="registry.meshagent.com/repo/web:1",
        pack=str(source_dir),
        context_path="/context",
        dockerfile_path="/context/Dockerfile",
        optimize=True,
        domain="node.meshagent.dev",
        email="hello@mail.meshagent.dev",
        extra_port=["3001:/messages"],
        validation_mode="cookie",
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        meshagent_token=None,
    )

    created_service = captured["created_service"]
    assert isinstance(created_service, tuple)
    service_spec = created_service[2]
    assert service_spec.ports is not None
    assert len(service_spec.ports) == 1
    assert service_spec.ports[0].num == 8080
    assert service_spec.ports[0].published is True
    assert service_spec.ports[0].public is None
    assert service_spec.ports[0].liveness == "/"
    assert service_spec.metadata.annotations is not None
    assert captured["deleted_image"] == "registry.meshagent.com/repo/web:1"
    assert (
        service_spec.metadata.annotations[image.ANNOTATION_REQUEST_VALIDATION_METHOD]
        == "cookie"
    )
    assert service_spec.ports[0].annotations == {
        image.ANNOTATION_REQUEST_VALIDATION_METHOD: "cookie"
    }
    created_route = captured["created_route"]
    assert isinstance(created_route, tuple)
    assert created_route[0] == "project-1"
    route_spec = created_route[1]
    assert route_spec.domain == "node.meshagent.dev"
    assert route_spec.room_name == "room-1"
    assert str(route_spec.paths[0].targetPort) == "8080"
    assert route_spec.paths[1].path == "/messages"
    assert str(route_spec.paths[1].targetPort) == "3001"
    assert route_spec.annotations == {image.ANNOTATION_SERVICE_ID: "repo-web"}
    assert captured["created_mailbox"] == {
        "project_id": "project-1",
        "address": "hello@mail.meshagent.dev",
        "room": "room-1",
        "queue": "hello@mail.meshagent.dev",
        "public": True,
        "annotations": {image.ANNOTATION_SERVICE_ID: "repo-web"},
    }
    assert captured["events"] == ["route", "mailbox", "build", "delete", "create"]
    assert "restarted_service_id" not in captured
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True
    assert printed[-1] == "\n".join(
        [
            "[bold green]Deploy complete[/bold green]",
            "Service [bold]repo-web[/bold] was deployed to room [bold]room-1[/bold].",
            "Public domain: node.meshagent.dev",
            "Email: hello@mail.meshagent.dev",
        ]
    )


def test_parse_extra_route_ports_accepts_numeric_and_named_targets() -> None:
    parsed = image._parse_extra_route_ports(
        values=[
            "3001:/messages",
            "assistant:/chat",
            "assistant:3002:/events",
        ]
    )

    assert parsed == [
        image._ExtraRoutePort(target_port=3001, path="/messages"),
        image._ExtraRoutePort(target_port="assistant", path="/chat"),
        image._ExtraRoutePort(target_port="assistant:3002", path="/events"),
    ]


def test_parse_extra_route_ports_rejects_invalid_service_port() -> None:
    with pytest.raises(
        typer.BadParameter,
        match="--extra-port service port must be an integer",
    ):
        image._parse_extra_route_ports(values=["assistant:messages:/messages"])


@pytest.mark.asyncio
async def test_resolve_deploy_domain_prompts_for_dockerfile_exposed_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meshagent.cli.tui.deploy_room import DeployDomainPromptResult

    captured: dict[str, object] = {}
    parsed_tag = image._parse_build_tag("registry.meshagent.com/repo/web:1")
    default_ports = [PortSpec(num=8080, published=True)]
    deploy_plan = image._build_deploy_service_spec(
        existing_service=None,
        parsed_tag=parsed_tag,
        public=False,
        liveness=None,
        default_ports=default_ports,
    )

    async def _fake_run_deploy_domain_prompt_tui(
        *, service_name: str, port: str, room_name: str, pages_domain: str
    ) -> DeployDomainPromptResult:
        captured["domain_tui"] = {
            "service_name": service_name,
            "port": port,
            "room_name": room_name,
            "pages_domain": pages_domain,
        }
        return DeployDomainPromptResult(
            status="completed",
            domain="chat.example.com",
        )

    monkeypatch.setattr(image, "_stdio_is_interactive", lambda: True)

    domain = await image._resolve_deploy_domain(
        account_client=_FakeDeployConfigClient(pages="apps.meshagent.example"),
        project_id="project-1",
        room_name="room-1",
        explicit_domain=None,
        existing_service=None,
        dockerfile_default_ports=default_ports,
        extra_route_ports=[],
        deploy_plan=deploy_plan,
        domain_prompt_handler=_fake_run_deploy_domain_prompt_tui,
    )

    assert domain == "chat.example.com"
    assert captured["domain_tui"] == {
        "service_name": "repo-web",
        "port": "8080",
        "room_name": "room-1",
        "pages_domain": "apps.meshagent.example",
    }


def test_build_deploy_service_spec_sets_container_template() -> None:
    parsed_tag = image._parse_build_tag("registry.meshagent.com/repo/web:1")

    deploy_plan = image._build_deploy_service_spec(
        existing_service=None,
        parsed_tag=parsed_tag,
        public=False,
        liveness=None,
        template="none",
    )

    assert deploy_plan.spec.container is not None
    assert deploy_plan.spec.container.template == "none"


@pytest.mark.asyncio
async def test_resolve_deploy_domain_reuses_existing_service_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        id="service-1",
        metadata=ServiceMetadata(name="repo-web"),
        container=ContainerSpec(image="registry.meshagent.com/repo/web:old"),
        ports=[PortSpec(num=8080, published=True)],
    )
    parsed_tag = image._parse_build_tag("registry.meshagent.com/repo/web:1")
    default_ports = [PortSpec(num=8080, published=True)]
    deploy_plan = image._build_deploy_service_spec(
        existing_service=existing_service,
        parsed_tag=parsed_tag,
        public=False,
        liveness=None,
        default_ports=default_ports,
    )

    class _FakeAccountClient:
        async def list_room_routes(
            self,
            *,
            project_id: str,
            room_name: str,
            count: int = 100,
            offset: int = 0,
            filter: str | None = None,
        ) -> list[Route]:
            captured["list_room_routes"] = {
                "project_id": project_id,
                "room_name": room_name,
                "count": count,
                "offset": offset,
                "filter": filter,
            }
            return [
                _route_for_service(
                    domain="existing.example.com",
                    room_name=room_name,
                    port="3000",
                    service_id="repo-web",
                )
            ]

    async def _unexpected_run_deploy_domain_prompt_tui(**kwargs) -> None:
        del kwargs
        raise AssertionError("domain prompt should not be shown")

    monkeypatch.setattr(image, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        "meshagent.cli.tui.deploy_room.run_deploy_domain_prompt_tui",
        _unexpected_run_deploy_domain_prompt_tui,
    )

    domain = await image._resolve_deploy_domain(
        account_client=_FakeAccountClient(),
        project_id="project-1",
        room_name="room-1",
        explicit_domain=None,
        existing_service=existing_service,
        dockerfile_default_ports=default_ports,
        extra_route_ports=[],
        deploy_plan=deploy_plan,
    )

    assert domain == "existing.example.com"
    assert captured["list_room_routes"] == {
        "project_id": "project-1",
        "room_name": "room-1",
        "count": 100,
        "offset": 0,
        "filter": None,
    }


@pytest.mark.asyncio
async def test_warn_missing_extra_route_ports_logs_unpublished_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {"messages": []}

    class _FakeAccountClient:
        async def list_room_services(
            self,
            *,
            project_id: str,
            room_name: str,
        ) -> list[ServiceSpec]:
            captured["list_room_services"] = (project_id, room_name)
            return [
                ServiceSpec(
                    version="v1",
                    kind="Service",
                    metadata=ServiceMetadata(name="assistant"),
                    ports=[PortSpec(num=3002, published=True)],
                )
            ]

    def _fake_print(message: str) -> None:
        captured["messages"].append(message)

    monkeypatch.setattr(image, "print", _fake_print)

    await image._warn_missing_extra_route_ports(
        account_client=_FakeAccountClient(),
        project_id="project-1",
        room_name="room-1",
        extra_route_ports=[
            image._ExtraRoutePort(target_port=3001, path="/messages"),
        ],
    )

    assert captured["list_room_services"] == ("project-1", "room-1")
    assert captured["messages"] == [
        "[yellow]Warning:[/] no service currently publishes port 3001 in room room-1; route path /messages may not resolve."
    ]


@pytest.mark.asyncio
async def test_warn_missing_extra_route_ports_skips_published_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {"messages": []}

    class _FakeAccountClient:
        async def list_room_services(
            self,
            *,
            project_id: str,
            room_name: str,
        ) -> list[ServiceSpec]:
            del project_id, room_name
            return [
                ServiceSpec(
                    version="v1",
                    kind="Service",
                    metadata=ServiceMetadata(name="assistant"),
                    ports=[PortSpec(num=3001, published=True)],
                )
            ]

    def _fake_print(message: str) -> None:
        captured["messages"].append(message)

    monkeypatch.setattr(image, "print", _fake_print)

    await image._warn_missing_extra_route_ports(
        account_client=_FakeAccountClient(),
        project_id="project-1",
        room_name="room-1",
        extra_route_ports=[
            image._ExtraRoutePort(target_port=3001, path="/messages"),
        ],
    )

    assert captured["messages"] == []


@pytest.mark.asyncio
async def test_warn_missing_extra_route_ports_skips_named_published_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {"messages": []}

    class _FakeAccountClient:
        async def list_room_services(
            self,
            *,
            project_id: str,
            room_name: str,
        ) -> list[ServiceSpec]:
            del project_id, room_name
            return [
                ServiceSpec(
                    id="service-1",
                    version="v1",
                    kind="Service",
                    metadata=ServiceMetadata(name="assistant"),
                    ports=[PortSpec(num=3001, published=True)],
                )
            ]

    def _fake_print(message: str) -> None:
        captured["messages"].append(message)

    monkeypatch.setattr(image, "print", _fake_print)

    await image._warn_missing_extra_route_ports(
        account_client=_FakeAccountClient(),
        project_id="project-1",
        room_name="room-1",
        extra_route_ports=[
            image._ExtraRoutePort(target_port="assistant:3001", path="/messages"),
        ],
    )

    assert captured["messages"] == []


@pytest.mark.asyncio
async def test_deploy_image_build_options_require_pack() -> None:
    with pytest.raises(
        typer.BadParameter,
        match="--context-path requires PATH",
    ):
        await image.deploy_image(
            project_id="project-1",
            room="room-1",
            tag="repo/web:1",
            pack=None,
            context_path="/context",
            dockerfile_path=None,
            optimize=True,
            domain=None,
            room_mount=[],
            project_mount=[],
            empty_dir_mount=[],
            image_mount=[],
            env=[],
            meshagent_token=None,
            private=True,
        )


@pytest.mark.asyncio
async def test_deploy_image_cred_requires_pack() -> None:
    with pytest.raises(
        typer.BadParameter,
        match="--cred requires PATH",
    ):
        await image.deploy_image(
            project_id="project-1",
            room="room-1",
            tag="repo/web:1",
            pack=None,
            context_path=None,
            dockerfile_path=None,
            optimize=True,
            cred=["registry,user,password"],
            domain=None,
            room_mount=[],
            project_mount=[],
            empty_dir_mount=[],
            image_mount=[],
            env=[],
            meshagent_token=None,
            private=True,
        )


@pytest.mark.asyncio
async def test_deploy_image_latest_requires_pack() -> None:
    with pytest.raises(
        typer.BadParameter,
        match="--latest requires PATH",
    ):
        await image.deploy_image(
            project_id="project-1",
            room="room-1",
            tag="repo/web:1",
            pack=None,
            context_path=None,
            dockerfile_path=None,
            optimize=True,
            latest=True,
            domain=None,
            room_mount=[],
            project_mount=[],
            empty_dir_mount=[],
            image_mount=[],
            env=[],
            meshagent_token=None,
            private=True,
        )


@pytest.mark.asyncio
async def test_deploy_image_sets_cookie_validation_when_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeRoomClient:
        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ):
            captured["created_service"] = (project_id, room_name, service)
            return "service-1"

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:1",
        domain=None,
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        meshagent_token=None,
        private=True,
    )

    created_service = captured["created_service"]
    assert isinstance(created_service, tuple)
    service_spec = created_service[2]
    assert service_spec.metadata.annotations is not None
    assert image.ANNOTATION_REQUEST_VALIDATION_METHOD not in (
        service_spec.metadata.annotations
    )
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_deploy_image_updates_existing_service_route_and_replaces_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        id="service-1",
        metadata=ServiceMetadata(
            name="repo-web",
            annotations={image.ANNOTATION_SERVICE_ID: "repo-web"},
        ),
        ports=[PortSpec(num=8080, type="http", published=True)],
        container=ContainerSpec(
            image="repo/web:old",
            environment=[
                EnvironmentVariable(name="KEEP", value="1"),
                EnvironmentVariable(
                    name="MESHAGENT_TOKEN",
                    token=TokenValue(
                        identity="existing-id",
                        api=ApiScope.agent_default(),
                        role="tool",
                    ),
                ),
            ],
            storage=ContainerMountSpec(
                images=[
                    ImageStorageMountSpec(
                        image="base:1",
                        path="/opt/base",
                        read_only=True,
                    )
                ]
            ),
        ),
    )

    class _FakeServices:
        async def restart(self, *, service_id: str) -> None:
            captured["restarted_service_id"] = service_id

        async def list_with_state(self):
            return SimpleNamespace(
                service_states={
                    "service-1": ServiceRuntimeState(
                        service_id="service-1",
                        state="running",
                        container_id="container-old",
                    )
                }
            )

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = _FakeServices()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return [existing_service]

        async def update_room_service(
            self,
            *,
            project_id: str,
            room_name: str,
            service_id: str,
            service,
        ) -> None:
            captured["updated_service"] = (
                project_id,
                room_name,
                service_id,
                service,
            )

        async def create_route(
            self,
            *,
            project_id: str,
            spec,
        ) -> None:
            captured["created_route"] = (project_id, spec)

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:2",
        domain="app.meshagent.app",
        room_mount=["/workspace:/srv/work:rw"],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=["FOO=bar"],
        meshagent_token="full",
        private=False,
    )

    updated_service = captured["updated_service"]
    assert isinstance(updated_service, tuple)
    assert updated_service[0] == "project-1"
    assert updated_service[1] == "room-1"
    assert updated_service[2] == "service-1"
    updated_spec = updated_service[3]
    assert updated_spec.container is not None
    assert updated_spec.container.image == "repo/web:2"
    assert updated_spec.container.storage is not None
    assert updated_spec.container.storage.room is not None
    assert updated_spec.container.storage.room[0].subpath == "/workspace"
    assert updated_spec.container.storage.room[0].path == "/srv/work"
    assert updated_spec.container.storage.room[0].read_only is False
    assert updated_spec.container.storage.images is not None
    assert updated_spec.container.storage.images[0].image == "base:1"
    env_by_name = {
        env_var.name: env_var for env_var in (updated_spec.container.environment or [])
    }
    assert "KEEP" not in env_by_name
    assert env_by_name["FOO"].value == "bar"
    assert env_by_name["MESHAGENT_TOKEN"].token is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.identity == "repo-web"
    assert env_by_name["MESHAGENT_TOKEN"].token.role == "agent"
    assert env_by_name["MESHAGENT_TOKEN"].token.api is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.api.admin is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.api.secrets is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.api.tunnels is not None
    for env_name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert env_by_name[env_name].token is not None
        assert env_by_name[env_name].token == env_by_name["MESHAGENT_TOKEN"].token
    assert updated_spec.ports is not None
    assert updated_spec.ports[0].liveness == "/"
    assert updated_spec.ports[0].public is True
    created_route = captured["created_route"]
    assert isinstance(created_route, tuple)
    assert created_route[0] == "project-1"
    route_spec = created_route[1]
    assert route_spec.domain == "app.meshagent.app"
    assert route_spec.room_name == "room-1"
    assert str(route_spec.paths[0].targetPort) == "8080"
    assert route_spec.annotations == {image.ANNOTATION_SERVICE_ID: "repo-web"}
    assert captured["restarted_service_id"] == "service-1"
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_deploy_image_sets_cookie_validation_on_private_published_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        id="service-1",
        metadata=ServiceMetadata(
            name="repo-web",
            annotations={image.ANNOTATION_SERVICE_ID: "repo-web"},
        ),
        ports=[
            PortSpec(
                num=8080,
                type="http",
                published=True,
                public=True,
                annotations={"keep": "1"},
            )
        ],
        container=ContainerSpec(image="repo/web:old"),
    )

    class _FakeServices:
        async def restart(self, *, service_id: str) -> None:
            captured["restarted_service_id"] = service_id

        async def list_with_state(self):
            return SimpleNamespace(
                service_states={
                    "service-1": ServiceRuntimeState(
                        service_id="service-1",
                        state="running",
                        container_id="container-old",
                    )
                }
            )

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = _FakeServices()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return [existing_service]

        async def update_room_service(
            self,
            *,
            project_id: str,
            room_name: str,
            service_id: str,
            service,
        ) -> None:
            captured["updated_service"] = (
                project_id,
                room_name,
                service_id,
                service,
            )

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:2",
        domain=None,
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        meshagent_token=None,
        private=True,
    )

    updated_service = captured["updated_service"]
    assert isinstance(updated_service, tuple)
    updated_spec = updated_service[3]
    assert updated_spec.metadata.annotations is not None
    assert image.ANNOTATION_REQUEST_VALIDATION_METHOD not in (
        updated_spec.metadata.annotations
    )
    assert updated_spec.ports is not None
    assert updated_spec.ports[0].liveness == "/"
    assert updated_spec.ports[0].public is None
    assert updated_spec.ports[0].annotations == {"keep": "1"}
    assert captured["restarted_service_id"] == "service-1"
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


def test_update_request_validation_annotations_removes_cookie_when_public() -> None:
    assert image._update_request_validation_annotations(
        annotations={
            image.ANNOTATION_REQUEST_VALIDATION_METHOD: "cookie",
            "keep": "1",
        },
        public=True,
    ) == {"keep": "1"}


@pytest.mark.asyncio
async def test_deploy_image_preserves_existing_liveness_when_default_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        id="service-1",
        metadata=ServiceMetadata(
            name="repo-web",
            annotations={image.ANNOTATION_SERVICE_ID: "repo-web"},
        ),
        ports=[
            PortSpec(
                num=8080,
                type="http",
                published=True,
                liveness="/ready",
            )
        ],
        container=ContainerSpec(image="repo/web:old"),
    )

    class _FakeServices:
        async def restart(self, *, service_id: str) -> None:
            captured["restarted_service_id"] = service_id

        async def list_with_state(self):
            return SimpleNamespace(
                service_states={
                    "service-1": ServiceRuntimeState(
                        service_id="service-1",
                        state="running",
                        container_id="container-old",
                    )
                }
            )

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = _FakeServices()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            del project_id, room_name
            return [existing_service]

        async def update_room_service(
            self,
            *,
            project_id: str,
            room_name: str,
            service_id: str,
            service,
        ) -> None:
            del project_id, room_name, service_id
            captured["updated_service"] = service

        async def close(self) -> None:
            return None

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:2",
        domain=None,
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        meshagent_token=None,
        private=True,
    )

    updated_service = captured["updated_service"]
    assert updated_service.ports is not None
    assert updated_service.ports[0].liveness == "/ready"


@pytest.mark.asyncio
async def test_deploy_image_liveness_flag_overrides_http_ports_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        id="service-1",
        metadata=ServiceMetadata(
            name="repo-web",
            annotations={image.ANNOTATION_SERVICE_ID: "repo-web"},
        ),
        ports=[
            PortSpec(
                num=8080,
                type="http",
                published=True,
                liveness="/ready",
            ),
            PortSpec(
                num=9090,
                type="tcp",
                published=False,
            ),
        ],
        container=ContainerSpec(image="repo/web:old"),
    )

    class _FakeServices:
        async def restart(self, *, service_id: str) -> None:
            captured["restarted_service_id"] = service_id

        async def list_with_state(self):
            return SimpleNamespace(
                service_states={
                    "service-1": ServiceRuntimeState(
                        service_id="service-1",
                        state="running",
                        container_id="container-old",
                    )
                }
            )

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = _FakeServices()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            del project_id, room_name
            return [existing_service]

        async def update_room_service(
            self,
            *,
            project_id: str,
            room_name: str,
            service_id: str,
            service,
        ) -> None:
            del project_id, room_name, service_id
            captured["updated_service"] = service

        async def close(self) -> None:
            return None

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:2",
        domain=None,
        liveness="/healthz",
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        meshagent_token=None,
        private=True,
    )

    updated_service = captured["updated_service"]
    assert updated_service.ports is not None
    assert updated_service.ports[0].liveness == "/healthz"
    assert updated_service.ports[1].liveness is None


@pytest.mark.asyncio
async def test_deploy_image_domain_requires_exactly_one_published_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        id="service-1",
        metadata=ServiceMetadata(
            name="repo-web",
            annotations={image.ANNOTATION_SERVICE_ID: "repo-web"},
        ),
        ports=[
            PortSpec(num=8080, type="http", published=True),
            PortSpec(num=9090, type="http", published=True),
        ],
        container=ContainerSpec(image="repo/web:old"),
    )

    class _FakeServices:
        async def list_with_state(self):
            return SimpleNamespace(
                service_states={
                    "service-1": ServiceRuntimeState(
                        service_id="service-1",
                        state="running",
                        container_id="container-old",
                    )
                }
            )

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = _FakeServices()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return [existing_service]

        async def update_room_service(
            self,
            *,
            project_id: str,
            room_name: str,
            service_id: str,
            service,
        ) -> None:
            del project_id, room_name, service_id, service
            captured["update_room_service_called"] = True

        async def close(self) -> None:
            return None

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    with pytest.raises(
        typer.BadParameter,
        match="--domain requires exactly one published service port",
    ):
        await image.deploy_image(
            project_id="project-1",
            room="room-1",
            tag="repo/web:2",
            domain="app.meshagent.app",
            room_mount=[],
            project_mount=[],
            empty_dir_mount=[],
            image_mount=[],
            env=[],
            meshagent_token=None,
            private=True,
        )

    assert "update_room_service_called" not in captured
