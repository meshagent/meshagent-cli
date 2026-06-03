# JavaScript Agent Toolkit

Illustrates how to use a Meshagent agent tool: connect with an agent token, publish `ping`, `status`, and `echo`, prove the calls locally, then deploy the toolkit. The point is to make a tool feel like an extension of the agent, not a separate app.

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
meshagent process deploy --room <room> --agent-name meshagent-create-javascript-agent --require-toolkit meshagent.create.javascript-agent --rule 'Use the meshagent.create.javascript-agent toolkit to answer ping, status, and echo requests.'
```
