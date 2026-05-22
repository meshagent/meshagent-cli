from meshagent.cli.tui.deploy_room import (
    DeployDomainPromptApp,
    DeployTemplateVariablePrompt,
    DeployTemplateVariablesApp,
)


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


def test_deploy_template_variables_records_required_value() -> None:
    app = DeployTemplateVariablesApp(
        variables=[
            DeployTemplateVariablePrompt(
                name="domain",
                title="Domain",
                description="Public route",
                default="demo.meshagent.app",
                optional=False,
            )
        ]
    )

    app._submit_current_value("custom.meshagent.app")

    assert app.result.status == "completed"
    assert app.result.values == {"domain": "custom.meshagent.app"}


def test_deploy_template_variables_rejects_empty_required_value() -> None:
    app = DeployTemplateVariablesApp(
        variables=[
            DeployTemplateVariablePrompt(
                name="domain",
                title="Domain",
                description="Public route",
                default="",
                optional=False,
            )
        ]
    )

    app._submit_current_value("")

    assert app.result.status == "canceled"
    assert app._index == 0
