from pathlib import Path

import meshagent as _meshagent

_meshagent.__path__.append(str(Path(__file__).resolve().parent / "meshagent"))

from meshagent.slack_channel import SlackChannel, create_channel  # noqa: E402
from meshagent.agents import run_room_channel  # noqa: E402

__all__ = ["SlackChannel", "create_channel"]


if __name__ == "__main__":
    run_room_channel(lambda room: create_channel(room=room))
