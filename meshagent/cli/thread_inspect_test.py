from meshagent.agents.messages import (
    AGENT_EVENT_IMAGE_GENERATION_COMPLETED,
    AGENT_MESSAGE_TURN_START,
    AGENT_EVENT_TEXT_CONTENT_DELTA,
    AGENT_EVENT_TEXT_CONTENT_ENDED,
    AGENT_EVENT_THREAD_STATUS,
    AGENT_EVENT_TURN_START_ACCEPTED,
    AgentGeneratedImage,
    AgentImageGenerationCompleted,
    AgentTextContent,
    AgentTextContentDelta,
    AgentTextContentEnded,
    AgentThreadStatus,
    TurnStart,
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


def test_coalesced_thread_rows_uses_raw_turn_start_when_accepted_content_is_empty() -> (
    None
):
    rows = coalesced_thread_rows(
        [
            TurnStart(
                type=AGENT_MESSAGE_TURN_START,
                message_id="source-message",
                sender_name="telegram-chat-5875963210",
                thread_id="dataset://agents/python-telegram-channel/threads/5875963210",
                turn_id="turn-1",
                content=[
                    AgentTextContent(
                        type="text",
                        text="Yo",
                    )
                ],
            ),
            TurnStartAccepted(
                type=AGENT_EVENT_TURN_START_ACCEPTED,
                sender_name="david.mcqueen@timu.com",
                thread_id="dataset://agents/python-telegram-channel/threads/5875963210",
                turn_id="turn-1",
                source_message_id="source-message",
                content=[],
            ),
            AgentTextContentDelta(
                type=AGENT_EVENT_TEXT_CONTENT_DELTA,
                sender_name="david.mcqueen@timu.com",
                thread_id="dataset://agents/python-telegram-channel/threads/5875963210",
                turn_id="turn-1",
                item_id="item-1",
                text="Yo! What's up?",
            ),
            AgentTextContentEnded(
                type=AGENT_EVENT_TEXT_CONTENT_ENDED,
                sender_name="david.mcqueen@timu.com",
                thread_id="dataset://agents/python-telegram-channel/threads/5875963210",
                turn_id="turn-1",
                item_id="item-1",
            ),
        ]
    )

    assert [(row.role, row.text) for row in rows] == [
        ("telegram-chat-5875963210", "Yo"),
        ("david.mcqueen@timu.com", "Yo! What's up?"),
    ]


def test_coalesced_thread_rows_deduplicates_raw_and_accepted_turn_input() -> None:
    rows = coalesced_thread_rows(
        [
            TurnStart(
                type=AGENT_MESSAGE_TURN_START,
                message_id="source-message",
                sender_name="telegram-chat-5875963210",
                thread_id="dataset://agents/python-telegram-channel/threads/5875963210",
                turn_id="turn-1",
                content=[
                    AgentTextContent(
                        type="text",
                        text="Yo",
                    )
                ],
            ),
            TurnStartAccepted(
                type=AGENT_EVENT_TURN_START_ACCEPTED,
                sender_name="telegram-chat-5875963210",
                thread_id="dataset://agents/python-telegram-channel/threads/5875963210",
                turn_id="turn-1",
                source_message_id="source-message",
                content=[
                    AgentTextContent(
                        type="text",
                        text="Yo",
                    )
                ],
            ),
        ]
    )

    assert [(row.role, row.text) for row in rows] == [
        ("telegram-chat-5875963210", "Yo"),
    ]


def test_coalesced_thread_rows_includes_image_only_response() -> None:
    rows = coalesced_thread_rows(
        [
            TurnStart(
                type=AGENT_MESSAGE_TURN_START,
                message_id="source-message",
                sender_name="telegram-chat-5875963210",
                thread_id="dataset://agents/python-telegram-channel/threads/5875963210",
                turn_id="turn-1",
                content=[
                    AgentTextContent(
                        type="text",
                        text="Make a pink elephant",
                    )
                ],
            ),
            AgentImageGenerationCompleted(
                type=AGENT_EVENT_IMAGE_GENERATION_COMPLETED,
                sender_name="david.mcqueen@timu.com",
                thread_id="dataset://agents/python-telegram-channel/threads/5875963210",
                turn_id="turn-1",
                item_id="image-1",
                images=[
                    AgentGeneratedImage(
                        uri="dataset://images?id=image-id",
                        mime_type="image/png",
                        width=1122,
                        height=1402,
                        status="completed",
                    )
                ],
            ),
        ]
    )

    assert [(row.role, row.text) for row in rows] == [
        ("telegram-chat-5875963210", "Make a pink elephant"),
        (
            "david.mcqueen@timu.com",
            "image: dataset://images?id=image-id (image/png, 1122x1402, completed)",
        ),
    ]


def test_coalesced_thread_rows_deduplicates_completed_image_generation_items() -> None:
    rows = coalesced_thread_rows(
        [
            AgentImageGenerationCompleted(
                type=AGENT_EVENT_IMAGE_GENERATION_COMPLETED,
                sender_name="assistant",
                thread_id="dataset://agents/python-telegram-channel/threads/5875963210",
                turn_id="turn-1",
                item_id="image-1",
                images=[
                    AgentGeneratedImage(
                        uri="dataset://images?id=image-id",
                        mime_type="image/png",
                        width=1122,
                        height=1402,
                        status="completed",
                    )
                ],
            ),
            AgentImageGenerationCompleted(
                type=AGENT_EVENT_IMAGE_GENERATION_COMPLETED,
                sender_name="assistant",
                thread_id="dataset://agents/python-telegram-channel/threads/5875963210",
                turn_id="turn-1",
                item_id="image-1",
                images=[
                    AgentGeneratedImage(
                        uri="data:image/png;base64,abc123",
                        mime_type="image/png",
                        width=1122,
                        height=1402,
                        status="generating",
                    )
                ],
            ),
        ]
    )

    assert [(row.role, row.text) for row in rows] == [
        (
            "assistant",
            "image: dataset://images?id=image-id (image/png, 1122x1402, completed)",
        ),
    ]
