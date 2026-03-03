import pytest
import typer

from meshagent.cli.sync import (
    _conversation_guid_for_thread_name,
    _thread_list_path_for_thread_dir,
    _thread_path_for_conversation,
)


def test_thread_path_for_conversation_uses_conversation_uuid() -> None:
    assert (
        _thread_path_for_conversation(
            thread_dir=".threads/anthropic",
            conversation_id="6f1f3dc1-f7a5-4db1-84aa-c0e1c8cf93c3",
        )
        == ".threads/anthropic/6f1f3dc1-f7a5-4db1-84aa-c0e1c8cf93c3.thread"
    )


def test_thread_path_for_conversation_stabilizes_non_uuid_id() -> None:
    assert (
        _thread_path_for_conversation(
            thread_dir=".threads/anthropic",
            conversation_id="abc/def:ghi",
        )
        == ".threads/anthropic/eda68fe1-4356-5156-9479-cc60cbf229d5.thread"
    )


def test_thread_path_for_conversation_suffixes_duplicates() -> None:
    assert (
        _thread_path_for_conversation(
            thread_dir=".threads/anthropic",
            conversation_id="6f1f3dc1-f7a5-4db1-84aa-c0e1c8cf93c3",
            occurrence=2,
        )
        == ".threads/anthropic/6f1f3dc1-f7a5-4db1-84aa-c0e1c8cf93c3 2.thread"
    )


def test_thread_path_for_conversation_rejects_empty_dir() -> None:
    with pytest.raises(typer.BadParameter):
        _thread_path_for_conversation(
            thread_dir="   ",
            conversation_id="conversation-id",
        )


def test_thread_path_for_conversation_rejects_invalid_occurrence() -> None:
    with pytest.raises(typer.BadParameter):
        _thread_path_for_conversation(
            thread_dir=".threads/anthropic",
            conversation_id="conversation-id",
            occurrence=0,
        )


def test_conversation_guid_for_thread_name_rejects_empty_id() -> None:
    with pytest.raises(typer.BadParameter):
        _conversation_guid_for_thread_name(conversation_id="   ")


def test_thread_list_path_for_thread_dir() -> None:
    assert (
        _thread_list_path_for_thread_dir(thread_dir="threads/anthropic5/")
        == "threads/anthropic5/index.threadl"
    )
