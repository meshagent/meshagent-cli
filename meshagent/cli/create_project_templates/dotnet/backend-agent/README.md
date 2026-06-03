# .NET Agent Toolkit

Illustrates how to use a Meshagent agent tool: register the toolkit in a room, handle the incoming tool-call message, return JSON, and clean up the registration. This is the version to study when you want more control over exactly how the protocol works.

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
meshagent process deploy --room <room> --agent-name meshagent-create-dotnet-agent --require-toolkit meshagent.create.dotnet-agent --rule 'Use the meshagent.create.dotnet-agent toolkit to answer ping, status, and echo requests.'
```
