# TypeScript Workspace App

Shows why room apps matter once you need more than one feature. A private page connects to the room, chats with agents, shows meeting and file views, reads room storage, exposes a developer console, and lets Codex join the same room, so beginners can see the whole MeshAgent workspace shape.

This template intentionally opens the current room directly. It does not include project or room switching UI.

## Next Steps

1. Run locally:

   ```bash
   npm run dev
   ```

   The dev script runs through `meshagent room connect` and starts a local raw
   websocket proxy at `/.well-known/meshagent/room/connect`. Browser clients connect to that
   relative endpoint while the dev server forwards the websocket upgrade to the
   connected room using the `MESHAGENT_TOKEN` supplied to the process.

2. Start Codex in the same room from another terminal:

   ```bash
   npm run codex
   ```

   This command joins a `codex` agent in the same room with the Codex process
   backend. New conversations in the Chat tab use the default MeshAgent dataset
   thread storage, matching the standard process-agent path.

3. Deploy:

   ```bash
   npm run deploy
   ```

Open the deployed private route from a room-authenticated browser session. The client connects to `./.well-known/meshagent/room/connect` and exposes chat, meeting, and file views for that room. Run `npm run codex` against the same room when you want the Chat tab to talk to Codex.
