from datetime import datetime, timezone

import pytest

from meshagent.api.client import CustomDomain, CustomDomainsPage
from meshagent.cli import custom_domains
from meshagent.cli.testing import CliRunner


def _domain(*, domain: str = "docs.example.com", available: bool = False):
    return CustomDomain.model_validate(
        {
            "domain": domain,
            "project_id": "resolved-project",
            "phase": "available" if available else "pending_dns",
            "available": available,
            "dns_authorization_record": {
                "name": "_acme-challenge.example.com.",
                "type": "CNAME",
                "data": "authorization.example.net.",
            },
            "routing_records": [{"name": "@", "type": "A", "data": "203.0.113.10"}],
            "certificate_state": "ACTIVE" if available else "PROVISIONING",
            "created_at": datetime(2026, 8, 31, tzinfo=timezone.utc),
        }
    )


def test_custom_domain_help_exposes_immutable_resource_commands() -> None:
    result = CliRunner().invoke(custom_domains.app, ["--help"])

    assert result.exit_code == 0
    assert "create" in result.output
    assert "get" in result.output
    assert "list" in result.output
    assert "delete" in result.output
    assert "update" not in result.output


@pytest.mark.asyncio
async def test_create_prints_dns_and_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []
    printed_tables: list[tuple[list[dict[str, object]], tuple[str, ...]]] = []

    class _FakeClient:
        async def create_custom_domain(self, *, project_id: str, domain: str):
            calls.append({"project_id": project_id, "domain": domain})
            return _domain(domain=domain)

        async def close(self) -> None:
            pass

    async def fake_get_client():
        return _FakeClient()

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(custom_domains, "get_client", fake_get_client)
    monkeypatch.setattr(custom_domains, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(
        custom_domains,
        "print_json_table",
        lambda rows, *columns: printed_tables.append((rows, columns)),
    )

    await custom_domains.create_custom_domain(
        project_id="project-1",
        domain="docs.example.com",
        o="table",
    )

    assert calls == [{"project_id": "resolved-project", "domain": "docs.example.com"}]
    assert printed_tables[0][0][0]["available"] is False
    assert "_acme-challenge.example.com." in str(
        printed_tables[0][0][0]["dns_authorization"]
    )
    assert "203.0.113.10" in str(printed_tables[0][0][0]["route_dns"])


@pytest.mark.asyncio
async def test_list_follows_continuation_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str | None] = []
    printed: list[object] = []

    class _FakeClient:
        async def list_custom_domains(
            self,
            *,
            project_id: str,
            count: int,
            continuation_token: str | None,
            filter: str | None,
            view: str,
        ) -> CustomDomainsPage:
            assert project_id == "resolved-project"
            assert filter == "example"
            assert view == "my"
            calls.append(continuation_token)
            if continuation_token is None:
                return CustomDomainsPage(
                    custom_domains=[_domain(domain="a.example.com")],
                    continuation_token="next",
                )
            return CustomDomainsPage(
                custom_domains=[_domain(domain="b.example.com", available=True)]
            )

        async def close(self) -> None:
            pass

    async def fake_get_client():
        return _FakeClient()

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        return "resolved-project"

    monkeypatch.setattr(custom_domains, "get_client", fake_get_client)
    monkeypatch.setattr(custom_domains, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(custom_domains, "print", printed.append)

    await custom_domains.list_custom_domains(
        project_id="project-1",
        filter="example",
        count=2,
        view="my",
        o="json",
    )

    assert calls == [None, "next"]
    assert '"a.example.com"' in str(printed[0])
    assert '"b.example.com"' in str(printed[0])
