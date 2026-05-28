# TypeScript Workspace App

Browser workspace app with chat, meetings, and files. The app connects with `RoomClient.withIAP()`, so deployed users authenticate through the room IAP cookie instead of a browser-visible access token.

This template intentionally opens the current room directly. It does not include project or room switching UI.

## Next Steps

1. Install dependencies:

   ```bash
   npm install
   ```

2. Run locally:

   ```bash
   npm run dev
   ```

   The dev script runs through `meshagent room connect` and starts a local raw
   websocket proxy at `/.well-known/meshagent/room/connect`. Browser clients connect to that
   relative endpoint while the dev server forwards the websocket upgrade to the
   connected room using the `MESHAGENT_TOKEN` supplied to the process.

3. Start Codex in the same room from another terminal:

   ```bash
   npm run codex
   ```

   This command joins a `codex` agent with `--thread-storage codex`, so new
   conversations in the Chat tab use Codex's native thread ids instead of room
   dataset thread documents. Install the Python Codex integration first if your
   MeshAgent CLI environment does not already include it:

   ```bash
   pip install meshagent-codex
   ```

4. Deploy:

   ```bash
   npm run deploy
   ```

Open the deployed private route from a room-authenticated browser session. The client connects to `./.well-known/meshagent/room/connect` and exposes chat, meeting, and file views for that room. Run `npm run codex` against the same room when you want the Chat tab to talk to Codex.
