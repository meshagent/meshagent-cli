from __future__ import annotations

from meshagent.cli import codex


def test_channel_thread_dir_strips_dataset_url_scheme() -> None:
    assert (
        codex._channel_thread_dir_for_storage(
            thread_storage="dataset",
            thread_dir="dataset://agents/codex/threads",
        )
        == "agents/codex/threads"
    )


def test_channel_thread_dir_strips_tmp_url_scheme() -> None:
    assert (
        codex._channel_thread_dir_for_storage(
            thread_storage="none",
            thread_dir="tmp://agents/codex/threads",
        )
        == "agents/codex/threads"
    )


def test_channel_thread_dir_keeps_codex_thread_dir() -> None:
    assert (
        codex._channel_thread_dir_for_storage(
            thread_storage="codex",
            thread_dir="/agents/codex/threads",
        )
        == "/agents/codex/threads"
    )
