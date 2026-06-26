# TypeScript Agent Toolkit

Illustrates how to use a Meshagent agent tool: give custom functionality a tool name and description, let MeshAgent route tool call results through the room, and confirm the loop before you replace `echo` with useful work.

## Next Steps

1. Run locally:

   ```bash
   npm run dev
   ```

2. Deploy:

   ```bash
   npm run deploy
   ```

## Install An Agent

To install an agent in your room that uses this toolkit, run:

```bash
meshagent process deploy --room <room> --agent-name meshagent-create-typescript-agent --require-toolkit meshagent.create.typescript-agent --rule 'Use the meshagent.create.typescript-agent toolkit to answer ping, status, and echo requests.'
```
