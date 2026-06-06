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

Deploy uses `.meshagent/deploy.yaml` as a service template. The template injects `CONTACT_FORM_FROM`, `CONTACT_FORM_TO`, and the optional SMTP envelope recipient into the service, and deploy creates or updates the public sender mailbox from `CONTACT_FORM_FROM`.

`./scripts/dev.sh` and `./scripts/deploy.sh` use the MeshAgent room picker when you do not pass a room. Set `CONTACT_FORM_TO` to the address that should receive submissions. Set `CONTACT_FORM_FROM` only when you want a specific sender mailbox on the MeshAgent mail domain.

If deploy reports that the sender mailbox already routes to a different room, choose another room-specific local part.

If `CONTACT_FORM_TO` is also a private MeshAgent mailbox, set `CONTACT_FORM_DELIVERY_TO` to a public destination mailbox or external delivery alias. The form keeps `CONTACT_FORM_TO` in the message header and uses `CONTACT_FORM_DELIVERY_TO` only as the SMTP envelope recipient.
