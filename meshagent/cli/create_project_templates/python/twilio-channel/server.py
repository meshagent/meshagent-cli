from meshagent.agents import run_room_channel
from meshagent.twilio.channel import TwilioChannel, create_channel

__all__ = ["TwilioChannel", "create_channel"]


if __name__ == "__main__":
    run_room_channel(lambda room: create_channel(room=room))
