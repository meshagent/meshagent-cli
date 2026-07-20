from meshagent.agents import run_room_channel
from meshagent.telegram.channel import TelegramChannel, create_channel

__all__ = ["TelegramChannel", "create_channel"]


if __name__ == "__main__":
    run_room_channel(lambda room: create_channel(room=room))
