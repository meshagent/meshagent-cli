# Dart Agent Toolkit

Illustrates how to use a Meshagent agent tool: join the room, publish `ping`, `status`, and `echo`, call them as a proof, and keep the toolkit available for agents. It is a compact way to understand that agent tools are your process answering room requests.

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
meshagent process deploy --room <room> --agent-name meshagent-create-dart-agent --require-toolkit meshagent.create.dart-agent --rule 'Use the meshagent.create.dart-agent toolkit to answer ping, status, and echo requests.'
```
