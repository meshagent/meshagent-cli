import pytest
import typer

from meshagent.cli.sync import _thread_path_for_conversation


def test_thread_path_for_conversation_sanitizes_id() -> None:
    assert (
        _thread_path_for_conversation(
            thread_dir=".threads/anthropic",
            conversation_name="Obsidian MCP Configuration",
            conversation_id="abc/def:ghi",
        )
        == ".threads/anthropic/Obsidian MCP Configuration.thread"
    )


def test_thread_path_for_conversation_falls_back_to_id() -> None:
    assert (
        _thread_path_for_conversation(
            thread_dir=".threads/anthropic",
            conversation_name=" ",
            conversation_id="abc/def:ghi",
        )
        == ".threads/anthropic/abc def ghi.thread"
    )


def test_thread_path_for_conversation_suffixes_duplicates() -> None:
    assert (
        _thread_path_for_conversation(
            thread_dir=".threads/anthropic",
            conversation_name="Obsidian MCP Configuration",
            conversation_id="abc/def:ghi",
            occurrence=2,
        )
        == ".threads/anthropic/Obsidian MCP Configuration 2.thread"
    )


def test_thread_path_for_conversation_rejects_empty_dir() -> None:
    with pytest.raises(typer.BadParameter):
        _thread_path_for_conversation(
            thread_dir="   ",
            conversation_name="conversation",
            conversation_id="conversation-id",
        )


def test_thread_path_for_conversation_rejects_invalid_occurrence() -> None:
    with pytest.raises(typer.BadParameter):
        _thread_path_for_conversation(
            thread_dir=".threads/anthropic",
            conversation_name="conversation",
            conversation_id="conversation-id",
            occurrence=0,
        )
