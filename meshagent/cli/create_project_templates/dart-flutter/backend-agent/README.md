# Dart Agent Toolkit

Minimal headless Dart service that exposes custom tools to MeshAgent agents.

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
