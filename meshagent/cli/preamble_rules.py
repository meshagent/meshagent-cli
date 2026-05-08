from __future__ import annotations

DEFAULT_PREAMBLE_RULE = (
    "For progress commentary, send at most one brief assistant message before "
    "a related group of tool calls, summarizing the grouped action and why it "
    "is needed. Do not send a separate commentary message for each individual "
    "tool call. Keep updates to 1-2 concise sentences, connect them to prior "
    "work when useful, and skip commentary for obvious or trivial single-step "
    "tool calls."
)
