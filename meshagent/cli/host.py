from meshagent.api.services import ServiceHost
from meshagent.api.specs.service import ServiceSpec
import asyncio


options = {"deferred": False}
services = {}


def set_deferred(deferred: bool):
    options["deferred"] = deferred


def get_deferred() -> bool:
    return options["deferred"]


def get_service(port: int, host: str) -> ServiceHost:
    if port not in services:
        services[port] = ServiceHost(host=host, port=port)

    return services[port]


def service_specs() -> list[ServiceSpec]:
    specs = []
    for port, s in services.items():
        specs.append(s.get_service_spec(image=""))
    return specs


async def run_services():
    tasks = []
    for port, s in services.items():
        tasks.append(s.run())

    await asyncio.gather(*tasks)
