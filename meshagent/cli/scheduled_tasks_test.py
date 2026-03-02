import json

import pytest
import typer

from meshagent.cli import scheduled_tasks


def test_load_payload_from_inline_json() -> None:
    payload = scheduled_tasks._load_payload(
        payload='{"action":"sync","count":2}', payload_file=None
    )
    assert payload == {"action": "sync", "count": 2}


def test_load_payload_from_file(tmp_path) -> None:
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps({"name": "task"}), encoding="utf-8")

    payload = scheduled_tasks._load_payload(
        payload=None, payload_file=str(payload_file)
    )
    assert payload == {"name": "task"}


def test_load_payload_requires_exactly_one_source() -> None:
    with pytest.raises(typer.BadParameter):
        scheduled_tasks._load_payload(payload=None, payload_file=None)

    with pytest.raises(typer.BadParameter):
        scheduled_tasks._load_payload(payload="{}", payload_file="/tmp/payload.json")


def test_parse_annotations_accepts_mapping() -> None:
    parsed = scheduled_tasks._parse_annotations('{"env":"prod"}')
    assert parsed == {"env": "prod"}


def test_parse_annotations_rejects_non_mapping() -> None:
    with pytest.raises(typer.BadParameter):
        scheduled_tasks._parse_annotations('["a","b"]')


def test_parse_annotations_rejects_non_string_values() -> None:
    with pytest.raises(typer.BadParameter):
        scheduled_tasks._parse_annotations('{"retries": 3}')
