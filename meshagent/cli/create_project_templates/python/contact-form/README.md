# Python Contact Form

Shows a realistic app problem: a public form needs configuration, validation, and email delivery. MeshAgent provides the room mailbox path, deploy-time settings, and automatic sender mailbox creation, so beginners can see how real app settings get wired into a deployed service.

## Next Steps

1. Run locally:

   ```bash
   ./scripts/dev.sh
   ```

2. Deploy:

   ```bash
   CONTACT_FORM_TO=you@example.com ./scripts/deploy.sh
   ```

## Email Setup

Deploy uses `.meshagent/deploy.yaml` as a service template. The template injects `CONTACT_FORM_FROM` and `CONTACT_FORM_TO` into the service, and deploy creates or updates the public sender mailbox from `CONTACT_FORM_FROM`.

`./scripts/dev.sh` and `./scripts/deploy.sh` use the MeshAgent room picker when you do not pass a room. Set `CONTACT_FORM_TO` to the address that should receive submissions. Set `CONTACT_FORM_FROM` only when you want a specific sender mailbox on the MeshAgent mail domain.

If deploy reports that the sender mailbox already routes to a different room, choose another room-specific local part.
