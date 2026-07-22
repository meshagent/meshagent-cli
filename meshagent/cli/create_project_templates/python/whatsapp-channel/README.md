# Python WhatsApp Channel

Runs a WhatsApp Cloud API channel inside a MeshAgent process agent. Incoming Meta webhook requests are validated at the MeshAgent edge and again by the sample HTTP endpoint, converted into trusted user turns, and completed agent responses are sent back through the WhatsApp Cloud API.

The channel runs as a language-neutral child process using `--channel='command:["python","server.py"]'` alongside the normal `--channel chat`. Its complete, editable implementation lives in `channel.py`; no separate WhatsApp channel package is installed.

## Meta Credentials

Create or reuse a WhatsApp Business Platform app and phone number, then copy the example environment file:

```sh
cp .env.example .env
${EDITOR:-nano} .env
```

Set:

- `WHATSAPP_ACCESS_TOKEN` to a token with permission to send WhatsApp messages.
- `WHATSAPP_PHONE_NUMBER_ID` to the WhatsApp phone number ID used in the Cloud API path.
- `WHATSAPP_APP_SECRET` to the Meta app secret used for `X-Hub-Signature-256` validation.
- `WHATSAPP_VERIFY_TOKEN` to the webhook verify token you will enter in the Meta app webhook setup.

Local development reads WhatsApp credentials from `.env`. Deployment does not put `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, or `WHATSAPP_VERIFY_TOKEN` in the service template or pass them as `--set` values.

## Text, Interactive Replies, And Media

This template extracts inbound text messages, interactive button/list replies, and inbound media from Meta webhooks. Delivery status updates from `entry[].changes[].value.statuses[]` are parsed and passed to an overridable channel hook for custom follow-up behavior.

Before starting an agent turn, the channel sends a best-effort read receipt with a text typing indicator. Disable these calls with `MESHAGENT_WHATSAPP_SEND_READ_RECEIPTS=0` or `MESHAGENT_WHATSAPP_SEND_TYPING_INDICATOR=0`.

Set `MESHAGENT_WHATSAPP_ALLOWED_FROM_NUMBERS` to a comma-separated phone number allowlist when you only want the channel to process and send messages for specific WhatsApp numbers. Numbers are matched by digits, so `+1 (555) 010-1000` and `15550101000` are equivalent. Leave it empty to allow all senders.

Inbound media messages are downloaded with the WhatsApp media ID, uploaded into room storage as `room:///...` files, and included in the MeshAgent turn content alongside the text/caption. The default storage prefix is `.threads/whatsapp-media`; override it with `MESHAGENT_WHATSAPP_MEDIA_STORAGE_PREFIX` when you want a different room storage path. Inbound media downloads are capped by `MESHAGENT_WHATSAPP_INBOUND_MEDIA_MAX_BYTES`, which defaults to `25000000`; oversized media is skipped and represented as text in the agent turn.

Text responses are sent as WhatsApp text messages. The SDK also exports helpers for interactive reply buttons, interactive list messages, generic templates, image-header templates, limited-time-offer templates, media-card carousel templates, media ID messages, and media upload/download/delete methods when your project needs explicit WhatsApp Cloud API calls. When the agent attaches files with the room `attach_file` tool, the WhatsApp channel sends those attachments as media messages:

- `https://...` and `http://...` attachment URLs are passed through as Cloud API media links.
- `room:///...` attachment URLs are resolved with `room.storage.download_url(path=...)`, so room storage can return a signed download URL when supported.
- Image, audio, and video file extensions are sent as their matching WhatsApp media type. Other file types, including PDFs, are sent as documents.

The media URL must be fetchable by Meta's Cloud API long enough for delivery.

## Platform Secrets

`scripts/deploy.sh` creates or reuses a MeshAgent service account named `python-whatsapp-channel`, unless you override `MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_NAME` or `MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_EMAIL`. It stores the private values as service-account secrets:

```sh
meshagent secret create whatsapp-access-token --subject "$MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_EMAIL" --value-file ...
```

Existing secrets are updated with `meshagent secret add-version`. Values are written to temporary mode-0600 files and passed with `--value-file` instead of appearing in command arguments.

`.meshagent/deploy.yaml` receives `whatsapp_service_account_email`, `whatsapp_access_token_secret_id`, `whatsapp_app_secret_id`, and `whatsapp_verify_token_secret_id`, runs the container as that service account with `secrets:proxy` and `secrets:read`, and uses:

- `whatsapp-access-token` for outbound WhatsApp Cloud API calls from the channel.
- `whatsapp-app-secret` for MeshAgent edge validation of `X-Hub-Signature-256`.
- `whatsapp-verify-token` for Meta's webhook subscription challenge.

`WHATSAPP_PHONE_NUMBER_ID` is not treated as sensitive by this template, so `scripts/deploy.sh` passes it as `whatsapp_phone_number_id`. Future deploys can omit the private values after the service-account secrets already exist; the script searches by the names above, or you can set `MESHAGENT_WHATSAPP_ACCESS_TOKEN_SECRET_ID`, `MESHAGENT_WHATSAPP_APP_SECRET_ID`, and `MESHAGENT_WHATSAPP_VERIFY_TOKEN_SECRET_ID`.

## Local Development

1. Configure credentials:

   ```sh
   cp .env.example .env
   ${EDITOR:-nano} .env
   ```

2. Start the channel in a room:

   ```sh
   ./scripts/dev.sh
   ```

   The script opens the MeshAgent room picker, joins the selected room, and starts:

   ```sh
   meshagent process join --channel chat --channel='command:["python","server.py"]'
   ```

3. Send a local dry-run queue message:

   ```sh
   MESHAGENT_WHATSAPP_DRY_RUN=1 ./scripts/dev.sh
   ```

   In another terminal connected to the same room:

   ```sh
   meshagent room queue send \
     --queue whatsapp-inbound \
     --json '{"body":"{\"object\":\"whatsapp_business_account\",\"entry\":[{\"id\":\"1234567890\",\"changes\":[{\"field\":\"messages\",\"value\":{\"messaging_product\":\"whatsapp\",\"metadata\":{\"display_phone_number\":\"15550102000\",\"phone_number_id\":\"9876543210\"},\"contacts\":[{\"wa_id\":\"15550101000\",\"profile\":{\"name\":\"Local WhatsApp User\"}}],\"messages\":[{\"from\":\"15550101000\",\"id\":\"wamid.local\",\"timestamp\":\"1700000000\",\"type\":\"text\",\"text\":{\"body\":\"hello from WhatsApp\"}}]}}]}]}"}'
   ```

   Dry run mode logs the WhatsApp response instead of sending a real Cloud API request.

## Deploy

Run:

```sh
./scripts/deploy.sh
```

The script opens the MeshAgent room picker when you do not pass `--room`, creates or updates the WhatsApp service-account secrets, builds the code image, and deploys the service template.

After deploy, set the Meta app webhook callback URL to the route URL shown by MeshAgent. Use the same value from `WHATSAPP_VERIFY_TOKEN` as the verify token. MeshAgent validates `X-Hub-Signature-256` with `whatsapp_app_secret_id`; `server.py` validates it again with the injected `WHATSAPP_APP_SECRET`. The endpoint also handles Meta's verification challenge and returns `EVENT_RECEIVED` for accepted events.

## How It Works

`server.py` hosts the provider-facing HTTP endpoint and bridges the local `WhatsAppChannel` from `channel.py` over MeshAgent's MessagePack channel protocol. For each Meta webhook JSON body, the channel extracts text messages, interactive replies, and inbound media from `entry[].changes[].value.messages[]`, parses status events from `entry[].changes[].value.statuses[]`, emits a `TurnStart` with a `Participant` whose name comes from the matching contact profile, then waits for the agent response. Edit `channel.py` to customize this behavior. Set `MESHAGENT_WHATSAPP_GRAPH_API_BASE_URL` to point outbound API calls at a compatible test server.

The agent's text deltas are collected until `TurnEnded`, then the completed response is sent back to the same WhatsApp sender with the Cloud API. File attachments emitted during the turn are resolved into Cloud API media links and sent as media messages. The participant attributes include `whatsapp.from`, `whatsapp.message_id`, `whatsapp.message_type`, `whatsapp.phone_number_id`, `whatsapp.display_phone_number`, and `whatsapp.interactive.reply_id` when present. Long responses are split before sending. The default thread path prefix is `.threads/whatsapp`; override it with `MESHAGENT_WHATSAPP_THREAD_PREFIX` when you want to isolate multiple channel deployments.

`.gitignore` and `.dockerignore` both exclude `.env`, `.env.local`, local virtual environments, and generated cache directories.
