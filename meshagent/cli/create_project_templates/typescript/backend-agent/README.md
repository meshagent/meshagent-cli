# TypeScript Agent Toolkit

Minimal headless TypeScript service that exposes custom tools to MeshAgent agents.

## Next Steps

1. Install dependencies:

   ```bash
   npm install
   ```

2. Run locally:

   ```bash
   npm run dev
   ```

3. Deploy:

   ```bash
   npm run deploy
   ```

## Install An Agent

To install an agent in your room that uses this toolkit, run:

```bash
meshagent process deploy --room <room> --agent-name meshagent-create-typescript-agent --require-toolkit meshagent.create.typescript-agent --rule 'Use the meshagent.create.typescript-agent toolkit to answer ping, status, and echo requests.'
```
