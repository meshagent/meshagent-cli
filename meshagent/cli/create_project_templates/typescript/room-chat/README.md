# TypeScript Room Chat

Shows collaboration features that do not involve a model at all. The private page signs into the room, discovers participants, receives room messages, and sends direct messages, giving beginners the pieces needed for presence, chat, and human coordination inside MeshAgent.

## Next Steps

1. Run locally:

   ```bash
   npm run dev
   ```

   The dev script runs through `meshagent room connect` and starts a local raw
   websocket proxy at `/.well-known/meshagent/room/connect`. Browser clients connect to that
   relative endpoint while the dev server forwards the websocket upgrade to the
   connected room using the `MESHAGENT_TOKEN` supplied to the process.

2. Deploy:

   ```bash
   npm run deploy
   ```

Open the deployed private route from a room-authenticated browser session. The client connects to `./.well-known/meshagent/room/connect`, lists remote room messaging participants, and sends direct chat messages to the selected participant.
