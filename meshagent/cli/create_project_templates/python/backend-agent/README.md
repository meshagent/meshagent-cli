# Python Agent Toolkit

Illustrates how to use a Meshagent agent tool: write a normal backend function, register it in a room, and let agents call it by name. The `ping`, `status`, and `echo` tools keep the flow obvious, then you replace them with the actions your agent should actually perform.

## Next Steps

1. Install dependencies:

   ```bash
   ./scripts/install.sh
   ```

2. Run locally:

   ```bash
   ./scripts/dev.sh
   ```

3. Deploy:

   ```bash
   ./scripts/deploy.sh
   ```

## Install An Agent

To install an agent in your room that uses this toolkit, run:

```bash
meshagent process deploy --room <room> --agent-name meshagent-create-python-agent --require-toolkit meshagent.create.python-agent --rule 'Use the meshagent.create.python-agent toolkit to answer ping, status, and echo requests.'
```
