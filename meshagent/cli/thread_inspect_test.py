from meshagent.agents.messages import (
    AGENT_EVENT_TEXT_CONTENT_DELTA,
    AGENT_EVENT_TEXT_CONTENT_ENDED,
    AGENT_EVENT_THREAD_STATUS,
    AGENT_EVENT_TURN_START_ACCEPTED,
    AgentTextContent,
    AgentTextContentDelta,
    AgentTextContentEnded,
    AgentThreadStatus,
    TurnStartAccepted,
)
from meshagent.cli.thread_inspect import coalesced_thread_rows


def test_coalesced_thread_rows_preserves_message_order() -> None:
    rows = coalesced_thread_rows(
        [
            TurnStartAccepted(
                type=AGENT_EVENT_TURN_START_ACCEPTED,
                sender_name="you",
                thread_id="dataset://agents/codex/threads/thread-1",
                turn_id="turn-1",
                source_message_id="message-1",
                content=[
                    AgentTextContent(
                        type="text",
                        text="Remember this exact seed word: persimmon.",
                    )
                ],
            ),
            AgentThreadStatus(
                type=AGENT_EVENT_THREAD_STATUS,
                sender_name="codex",
                thread_id="dataset://agents/codex/threads/thread-1",
                turn_id="turn-1",
                status="Responding",
            ),
            AgentTextContentDelta(
                type=AGENT_EVENT_TEXT_CONTENT_DELTA,
                thread_id="dataset://agents/codex/threads/thread-1",
                turn_id="turn-1",
                item_id="item-1",
                text="seed",
            ),
            AgentTextContentEnded(
                type=AGENT_EVENT_TEXT_CONTENT_ENDED,
                thread_id="dataset://agents/codex/threads/thread-1",
                turn_id="turn-1",
                item_id="item-1",
            ),
            AgentThreadStatus(
                type=AGENT_EVENT_THREAD_STATUS,
                sender_name="codex",
                thread_id="dataset://agents/codex/threads/thread-1",
                turn_id="turn-1",
                status="Wrapping up",
            ),
        ]
    )

    assert [(row.role, row.text) for row in rows] == [
        ("you", "Remember this exact seed word: persimmon."),
        ("status", "Responding"),
        ("assistant", "seed"),
        ("status", "Wrapping up"),
    ]
