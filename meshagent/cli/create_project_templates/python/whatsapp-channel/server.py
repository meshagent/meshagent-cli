from meshagent.agents import run_room_channel
from meshagent.whatsapp.channel import WhatsAppChannel, create_channel

__all__ = ["WhatsAppChannel", "create_channel"]


if __name__ == "__main__":
    run_room_channel(lambda room: create_channel(room=room))
