from meshagent.api.services import ServiceHost
from meshagent.api.specs.service import (
    ServiceSpec,
    EnvironmentVariable,
    TokenValue,
)
from meshagent.api import ApiScope
import asyncio
from meshagent.agents import SingleRoomAgent


options = {"deferred": False}
services = {}


agents: list[tuple[SingleRoomAgent, str]] = []


def _default_token_identity(*, spec: ServiceSpec) -> str:
    if spec.agents is not None:
        for agent in spec.agents:
            name = getattr(agent, "name", None)
            if isinstance(name, str) and name.strip() != "":
                return name.strip()

    for port in spec.ports or []:
        for endpoint in port.endpoints:
            meshagent_endpoint = getattr(endpoint, "meshagent", None)
            identity = getattr(meshagent_endpoint, "identity", None)
            if isinstance(identity, str) and identity.strip() != "":
                return identity.strip()

    return "agent"


def _ensure_meshagent_token_environment(
    *, spec: ServiceSpec, token_identity: str | None = None
) -> None:
    if spec.container is None:
        return

    environment = list(spec.container.environment or [])
    default_scope = ApiScope.agent_default()
    identity = (
        token_identity.strip()
        if isinstance(token_identity, str) and token_identity.strip() != ""
        else _default_token_identity(spec=spec)
    )

    for env_var in environment:
        if env_var.name != "MESHAGENT_TOKEN":
            continue

        if env_var.token is not None:
            if env_var.token.identity is None:
                env_var.token.identity = identity
            if env_var.token.api is None:
                env_var.token.api = default_scope
            if env_var.token.role is None:
                env_var.token.role = "agent"
        elif env_var.value is None and env_var.secret is None:
            env_var.token = TokenValue(
                identity=identity,
                api=default_scope,
                role="agent",
            )

        spec.container.environment = environment
        return

    environment.append(
        EnvironmentVariable(
            name="MESHAGENT_TOKEN",
            token=TokenValue(
                identity=identity,
                api=default_scope,
                role="agent",
            ),
        )
    )
    spec.container.environment = environment


def set_deferred(deferred: bool):
    options["deferred"] = deferred


def get_deferred() -> bool:
    return options["deferred"]


def get_service(port: int, host: str) -> ServiceHost:
    if port not in services:
        services[port] = ServiceHost(host=host, port=port)

    return services[port]


def service_specs(*, token_identity: str | None = None) -> list[ServiceSpec]:
    specs = []
    for port, s in services.items():
        spec = s.get_service_spec(image="")
        _ensure_meshagent_token_environment(spec=spec, token_identity=token_identity)
        specs.append(spec)
    return specs


async def run_services():
    tasks = []
    for port, s in services.items():
        tasks.append(s.run())

    await asyncio.gather(*tasks)
