# Python Contact Form

Minimal public aiohttp contact form that sends email through room SMTP.

## Next Steps

1. Install dependencies:

   ```bash
   ./scripts/install.sh
   ```

2. Create a room:

   ```bash
   meshagent rooms create --name <room> --if-not-exists
   ```

3. Run locally:

   ```bash
   ./scripts/dev.sh --room <room>
   ```

4. Deploy:

   ```bash
   CONTACT_FORM_TO=you@example.com ./scripts/deploy.sh --room <room>
   ```

## Mailbox Setup

Before testing a submission, set up the sender mailbox for that room.

New mailbox:

```bash
meshagent mailbox create --address contact-<room-slug>@mail.meshagent.com --room <room> --queue contact-<room-slug>@mail.meshagent.com --public
```

Existing mailbox for that room:

```bash
meshagent mailbox update contact-<room-slug>@mail.meshagent.com --room <room> --queue contact-<room-slug>@mail.meshagent.com --public
```

Use that mailbox as `CONTACT_FORM_FROM`. If create returns 409, choose another room-specific local part; do not reuse a mailbox unless it is listed for this room. Set `CONTACT_FORM_TO` to the address that should receive submissions.

If `CONTACT_FORM_TO` is also a private MeshAgent mailbox, use a public destination mailbox or an external delivery alias.
