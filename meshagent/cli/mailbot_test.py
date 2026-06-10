import asyncio

from typer import _click as click

from meshagent.cli import mailbot


def test_mailbot_join_passes_room_jwt_as_api_key(monkeypatch) -> None:
    build_calls: list[dict[str, object]] = []

    class _DummyAccountClient:
        async def close(self) -> None:
            return None

    async def fake_get_client():
        return _DummyAccountClient()

    async def fake_resolve_project_id(*, project_id=None):
        del project_id
        return "project-123"

    async def fake_resolve_key(*, project_id=None, key=None):
        del project_id
        del key
        return None

    def fake_build_mailbot(**kwargs):
        build_calls.append(kwargs)
        return type("DummyMailbot", (), {})

    monkeypatch.setenv("MESHAGENT_TOKEN", "test-token")
    monkeypatch.setattr(mailbot, "get_client", fake_get_client)
    monkeypatch.setattr(mailbot, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(mailbot, "resolve_key", fake_resolve_key)
    monkeypatch.setattr(mailbot, "resolve_room", lambda room_name=None: room_name)
    monkeypatch.setattr(mailbot, "build_mailbot", fake_build_mailbot)
    monkeypatch.setattr(mailbot, "get_deferred", lambda: True)
    monkeypatch.setattr(mailbot, "print", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mailbot.sys,
        "argv",
        [
            "meshagent",
            "mailbot",
            "join",
            "--agent-name",
            "helper",
            "--room",
            "quickstart",
            "--email-address",
            "helper@example.test",
        ],
    )

    async def invoke_join() -> None:
        await mailbot.join(
            project_id=None,
            room="quickstart",
            agent_name="helper",
            email_address="helper@example.test",
        )

    root_command = click.Command("meshagent")
    mailbot_command = click.Command("mailbot")
    join_command = click.Command("join")
    with click.Context(root_command, info_name="meshagent") as root_context:
        with click.Context(
            mailbot_command,
            info_name="mailbot",
            parent=root_context,
        ) as mailbot_context:
            with click.Context(
                join_command,
                info_name="join",
                parent=mailbot_context,
            ):
                asyncio.run(invoke_join())

    assert len(build_calls) == 1
    assert build_calls[0]["api_key"] == "test-token"
