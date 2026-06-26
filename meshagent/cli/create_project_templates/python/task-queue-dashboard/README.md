# Python Task Queue Dashboard

Shows a room-connected queue workflow with an operational dashboard: six scheduled demo entries enqueue text payloads 20 seconds apart, a listener drains the room queue, and the page reports pending tasks, queue size, and enqueue/dequeue totals.

MeshAgent scheduled tasks use `ScheduledTaskSpec` and enforce a 15-minute minimum recurring interval. This template uses a short in-process scheduler so the flow is visible during local development while still using the MeshAgent room queue API for enqueue, dequeue, and queue size.

## Next Steps

1. Run locally:

   ```bash
   ./scripts/dev.sh
   ```

   The dashboard runs at `http://127.0.0.1:8000/`.

2. Deploy:

   ```bash
   ./scripts/deploy.sh
   ```

## Configuration

- `TASK_QUEUE_NAME` sets a stable room queue name. By default each run uses a unique demo queue.
- `TASK_QUEUE_TASK_COUNT` defaults to `6`.
- `TASK_QUEUE_INTERVAL_SECONDS` defaults to `20`.
- `TASK_QUEUE_DASHBOARD_OPEN_BROWSER=0` disables the local browser launch.
