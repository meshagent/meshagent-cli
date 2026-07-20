# Python Twilio Channel

Runs a Twilio-backed SMS/MMS channel inside a MeshAgent process agent. Incoming Twilio webhook requests are validated by MeshAgent, placed onto a room queue, converted into trusted user turns, and completed agent responses are sent back through the Twilio Messages API.

The channel runs as a language-neutral child process using `--channel='command:["python","server.py"]'` alongside the normal `--channel chat`. The CLI gives the child a private capability-protected connection, and the channel remains the authority that maps each SMS sender to a MeshAgent participant.

## Twilio Credentials

Create or reuse a Twilio SMS sender, then configure credentials. You can either copy the example into the project-local `.env`:

```sh
cp .env.example .env
${EDITOR:-nano} .env
```

or keep shared credentials in a `.env-twilio` file in this project or any parent directory. The generated `scripts/dev.sh` and `scripts/deploy.sh` load `.env-twilio` first, then project-local `.env`.

Set:

- `TWILIO_ACCOUNT_SID` from the Twilio Console.
- `TWILIO_AUTH_TOKEN` from the Twilio Console. Keep this private.

Local development reads Twilio credentials from `.env-twilio`, `.env`, or the shell. Deployment does not put `TWILIO_AUTH_TOKEN` in the service template or pass it as a `--set` value.

## SMS And MMS

This template uses Twilio Programmable Messaging for SMS and MMS. Twilio sends ordinary E.164 addresses like `+15550101000` in the webhook `From` and `To` fields, and the channel sends replies with those same address values.

Set `MESHAGENT_TWILIO_ALLOWED_FROM_NUMBERS` to a comma-separated phone number allowlist when you only want the channel to process and send messages for specific SMS/MMS senders. Numbers are matched by digits, so `+1 (555) 010-1000` and `15550101000` are equivalent. Leave it empty to allow all senders.

Configure the sender's Messaging webhook for "A message comes in". MeshAgent validates Twilio's request signature before the form body reaches the `twilio-inbound` queue. Use the `whatsapp-channel` template for Meta WhatsApp Cloud API integrations.

Text responses are sent as SMS. When the agent attaches files with the room `attach_file` tool, the Twilio channel sends those attachments as MMS media:

- `https://...` and `http://...` attachment URLs are passed through as Twilio `MediaUrl` values.
- `room:///...` attachment URLs are resolved with `room.storage.download_url(path=...)`, so room storage can return a signed download URL when supported.
- Twilio allows up to 10 media URLs per message, so the channel sends larger attachment sets as multiple MMS messages.

Inbound MMS media works in the other direction. Twilio webhook fields like `NumMedia`, `MediaUrl0`, and `MediaContentType0` are parsed, each media URL is downloaded with the channel's Twilio credentials, and the file is uploaded to room storage before the agent receives it as `AgentFileContent`. Media-only MMS messages and MMS messages with text captions are both supported.

Inbound MMS files are stored under `MESHAGENT_TWILIO_MEDIA_STORAGE_PREFIX`, which defaults to `.threads/twilio-media`. Set `MESHAGENT_TWILIO_INBOUND_MEDIA_MAX_BYTES` to cap each downloaded media file; the default is `25000000`.

Your Twilio sender, recipient, and carrier path must support MMS for media delivery. Plain SMS-only routes will still receive text replies, but may reject media.

## Platform Secrets

`scripts/deploy.sh` creates or reuses a MeshAgent service account named `python-twilio-channel`, unless you override `MESHAGENT_TWILIO_SERVICE_ACCOUNT_NAME` or `MESHAGENT_TWILIO_SERVICE_ACCOUNT_EMAIL`. It stores `TWILIO_AUTH_TOKEN` as the service-account secret `twilio-auth-token` with:

```sh
meshagent secret create twilio-auth-token --subject "$MESHAGENT_TWILIO_SERVICE_ACCOUNT_EMAIL" --value-file ...
```

Existing secrets are updated with `meshagent secret add-version`. The value is written to a temporary mode-0600 file and passed with `--value-file` instead of appearing in command arguments.

`.meshagent/deploy.yaml` receives `twilio_service_account_email` and `twilio_auth_token_secret_id`, runs the container as that service account with `secrets:proxy` and `secrets:read`, and uses the same secret ID for outbound Twilio API calls and MeshAgent edge validation of incoming Twilio requests.

`TWILIO_ACCOUNT_SID` is not treated as sensitive by this template, so `scripts/deploy.sh` passes it as `twilio_account_sid`. Future deploys can omit `TWILIO_AUTH_TOKEN` after the service-account secret already exists; the script searches by the `twilio-auth-token` name, or you can set `MESHAGENT_TWILIO_AUTH_TOKEN_SECRET_ID`.

## Local Development

1. Configure credentials in `.env`, `.env-twilio`, or the shell:

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
   MESHAGENT_TWILIO_DRY_RUN=1 ./scripts/dev.sh
   ```

   In another terminal connected to the same room:

   ```sh
   meshagent room queue send \
     --queue twilio-inbound \
     --json '{"body":"From=%2B15550101000&To=%2B15550102000&Body=hello&MessageSid=SMlocal"}'
   ```

   Dry run mode logs the Twilio response instead of sending a real Messages API request.

## Deploy

Run:

```sh
./scripts/deploy.sh
```

The script opens the MeshAgent room picker when you do not pass `--room`, creates or updates the Twilio service-account secret, builds the code image, and deploys the service template.

After deploy, set the Twilio sender's Messaging webhook for "A message comes in" to the route URL shown by MeshAgent, using HTTP `POST`. The route is backed by the room queue `twilio-inbound`; MeshAgent validates `X-Twilio-Signature` with the service-account secret ID passed as `twilio_auth_token_secret_id` before any request reaches the queue.

## How It Works

`server.py` is a small executable adapter that imports `create_channel(...)` from `meshagent.twilio.channel` and bridges the resulting `TwilioChannel` over MeshAgent's existing MessagePack channel protocol. For each queued Twilio form body, the channel parses `From`, `To`, `Body`, `MessageSid`, `NumMedia`, `MediaUrl{N}`, and `MediaContentType{N}`. It emits a `TurnStart` with a `Participant` whose name comes from `ProfileName` or the Twilio sender address, then waits for the agent response.

The agent's text deltas are collected until `TurnEnded`, then the completed response is sent back to the same sender with Twilio's Messages API. Inbound file attachments are stored in room storage before the turn starts, and file attachments emitted during the turn are resolved into Twilio-fetchable media URLs and sent as MMS. The participant attributes include `twilio.channel=sms`. Long responses are split before sending. The default thread path prefix is `.threads/twilio`; override it with `MESHAGENT_TWILIO_THREAD_PREFIX` when you want to isolate multiple channel deployments.

`.gitignore` and `.dockerignore` both exclude `.env`, `.env.local`, local virtual environments, and generated cache directories.
