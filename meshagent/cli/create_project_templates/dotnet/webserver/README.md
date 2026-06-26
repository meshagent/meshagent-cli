# .NET Web App

Shows how your existing web framework fits into MeshAgent's deploy model. The service exposes a few easy-to-test endpoints, MeshAgent checks `/health` before sending traffic, and local development writes to room storage so you can see how a web app shares data with agents and other room tools.

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
