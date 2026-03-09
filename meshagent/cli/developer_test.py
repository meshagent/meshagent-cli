from dataclasses import dataclass

from meshagent.cli.developer import _plain_event_lines


@dataclass
class _Event:
    type: str
    data: dict


def test_plain_event_lines_formats_otel_logs() -> None:
    event = _Event(
        type="otel.log",
        data={
            "resourceLogs": [
                {
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "timeUnixNano": "1710000000000000000",
                                    "severityText": "ERROR",
                                    "body": {"stringValue": "container failed"},
                                }
                            ]
                        }
                    ]
                }
            ]
        },
    )

    lines = _plain_event_lines(event)

    assert len(lines) == 1
    assert "ERROR" in lines[0]
    assert "container failed" in lines[0]


def test_plain_event_lines_skips_otel_metrics_and_traces() -> None:
    assert _plain_event_lines(_Event(type="otel.metric", data={})) == []
    assert _plain_event_lines(_Event(type="otel.trace", data={})) == []


def test_plain_event_lines_skips_non_otel_events() -> None:
    assert _plain_event_lines(_Event(type="custom.log", data={"hello": "world"})) == []
