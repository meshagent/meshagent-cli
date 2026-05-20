from meshagent.cli.tui.deploy_room import DeployDomainPromptApp


def test_deploy_domain_prompt_prefills_subdomain_from_room_name() -> None:
    app = DeployDomainPromptApp(
        service_name="web",
        port="8080",
        room_name="My Room! 2026",
        pages_domain=".meshagent.dev",
    )

    assert app._default_subdomain == "my-room-2026"
    assert app._pages_domain == "meshagent.dev"


def test_deploy_domain_prompt_accepts_subdomain_only() -> None:
    assert DeployDomainPromptApp._is_valid_subdomain("my-room") is True
    assert DeployDomainPromptApp._is_valid_subdomain("my.room") is False
    assert DeployDomainPromptApp._is_valid_subdomain("https://my-room") is False
