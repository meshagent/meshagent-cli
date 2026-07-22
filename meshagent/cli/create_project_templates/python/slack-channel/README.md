# Python Slack Channel

Runs a Slack-backed channel inside a MeshAgent process agent. Incoming Slack Events API requests are validated at the MeshAgent edge and again by the sample HTTP endpoint, converted into trusted user turns, and completed agent responses are sent back through Slack `chat.postMessage`.

The channel runs as a language-neutral child process using `--channel='command:["python","server.py"]'` alongside the normal `--channel chat`. The editable channel implementation lives in this project as `channel.py`; no separate Slack channel package is installed.

## Slack App Credentials

Create or reuse a Slack app, then run the setup UI:

```sh
./scripts/configure-slack.sh
```

Set:

- `SLACK_BOT_TOKEN` to the Bot User OAuth Token, usually an `xoxb-...` token.
- `SLACK_SIGNING_SECRET` to the app Signing Secret. Keep this private.

The bot token needs permission to send messages and upload files. Add `chat:write` and `files:write`. For Events API subscriptions, add the scopes and events that match how you want users to reach the channel, for example `app_mentions:read` with the `app_mention` event, and direct-message or channel history scopes/events if you want plain `message` events.

Local development reads Slack credentials from `.env`. Deployment does not put `SLACK_BOT_TOKEN` or `SLACK_SIGNING_SECRET` in the service template or pass either value as a `--set` value.

If the setup UI needs to install local dependencies, it writes progress to `.meshagent/slack-setup-install.log`.

## Events API

This template expects Slack Events API HTTP `POST` callbacks. MeshAgent validates `X-Slack-Signature` and `X-Slack-Request-Timestamp` before proxying the request to `server.py`. The sample endpoint repeats that validation before handing the body to `channel.py`, and answers Slack `url_verification` requests with the challenge value.

Supported inbound events:

- `app_mention`
- ordinary `message` events without edit/delete subtypes

Bot messages are ignored by default. Set `MESHAGENT_SLACK_IGNORE_BOTS=0` when you intentionally want bot-originated events to become room turns. Set `MESHAGENT_SLACK_ALLOWED_CHANNELS` to a comma-separated list of Slack channel IDs when you only want the channel to process specific channels.

Responses are sent back to the event channel. By default replies are placed in the source Slack thread, or a new thread rooted at the source message. Set `MESHAGENT_SLACK_REPLY_IN_THREAD=0` to post top-level replies for top-level messages.

## Platform Secrets

`scripts/deploy.sh` creates or reuses a MeshAgent service account named `python-slack-channel`, unless you override `MESHAGENT_SLACK_SERVICE_ACCOUNT_NAME` or `MESHAGENT_SLACK_SERVICE_ACCOUNT_EMAIL`. It stores Slack credentials as service-account secrets:

```sh
meshagent secret create slack-bot-token --subject "$MESHAGENT_SLACK_SERVICE_ACCOUNT_EMAIL" --value-file ...
meshagent secret create slack-signing-secret --subject "$MESHAGENT_SLACK_SERVICE_ACCOUNT_EMAIL" --value-file ...
```

Existing secrets are updated with `meshagent secret add-version`. Values are written to temporary mode-0600 files and passed with `--value-file` instead of appearing in command arguments.

`.meshagent/deploy.yaml` receives `slack_service_account_email`, `slack_bot_token_secret_id`, and `slack_signing_secret_id`, runs the container as that service account with `secrets:proxy` and `secrets:read`, uses the bot-token secret for outbound Slack API calls, and uses the signing-secret ID for MeshAgent edge validation of incoming Slack requests.

Future deploys can omit `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` after the service-account secrets already exist. The script searches by `slack-bot-token` and `slack-signing-secret`, or you can set `MESHAGENT_SLACK_BOT_TOKEN_SECRET_ID` and `MESHAGENT_SLACK_SIGNING_SECRET_ID`.

## Local Development

1. Configure credentials:

   ```sh
   ./scripts/configure-slack.sh
   ```

2. Start the channel in a room:

   ```sh
   ./scripts/dev.sh
   ```

   The script opens the MeshAgent room picker, joins the selected room, and starts:

   ```sh
   meshagent process join --channel chat --channel='command:["python","server.py"]' --image-generation gpt-image-2
   ```

   It also creates or updates a room service named `python-slack-channel-events`, uses your local `SLACK_SIGNING_SECRET` for the dev callback verifier, creates or updates the public route for the selected room, and prints:

   ```text
   Slack Events API Request URL:
     https://<room-name>.meshagent.dev/
   ```

   Paste that URL into your Slack app's Event Subscriptions Request URL. Slack will verify the URL with a signed `url_verification` request. The local callback service validates the signing secret, answers the challenge, and puts valid Slack events onto the same `slack-events` queue consumed by `dev.sh`.

3. Send a local dry-run queue message:

   ```sh
   MESHAGENT_SLACK_DRY_RUN=1 ./scripts/dev.sh
   ```

   In another terminal connected to the same room:

   ```sh
   meshagent room queue send \
     --queue slack-events \
     --json '{"body":"{\"type\":\"event_callback\",\"team_id\":\"T123\",\"event_id\":\"Evlocal\",\"event\":{\"type\":\"app_mention\",\"channel\":\"C123\",\"user\":\"U123\",\"text\":\"hello\",\"ts\":\"1710000000.000100\",\"channel_type\":\"channel\"}}"}'
   ```

   Dry run mode logs Slack response and file-upload payloads instead of sending real Slack Web API requests.

## Deploy

Run:

```sh
./scripts/deploy.sh
```

The script opens the MeshAgent room picker when you do not pass `--room`, creates or updates the Slack service-account secrets, allocates a public route, builds the code image, and deploys the service template.

`scripts/dev.sh` and `scripts/deploy.sh` print the exact Slack Events API Request URL to paste into the Slack app configuration. By default they derive `https://<room-name>.meshagent.dev/` from the selected room, or use the configured MeshAgent pages domain when one is available. Override the base domain with `MESHAGENT_SLACK_EVENTS_BASE_DOMAIN`, override the full route domain with `MESHAGENT_SLACK_EVENTS_DOMAIN`, or set `MESHAGENT_SLACK_EVENTS_URL` when you want the printed Slack Request URL to include a specific path.

The deployed route proxies to the HTTP endpoint in `server.py`. MeshAgent validates Slack's signature with `slack_signing_secret_id`, and the endpoint validates it again with the injected `SLACK_SIGNING_SECRET`.

## Interact From Slack

Use the callback URL printed by `./scripts/dev.sh` for local development, or the callback URL printed by `./scripts/deploy.sh` for the deployed service. Copy that URL, open your Slack app configuration, enable Event Subscriptions, paste the URL as the Request URL, and let Slack verify it with the signed `url_verification` challenge. Under Bot Events, subscribe to `app_mention` at minimum, then reinstall the Slack app to your workspace if Slack prompts you to apply permission changes. Invite the bot user to a channel with `/invite @your-bot-name`, then mention it in Slack with `@your-bot-name hello`. If Slack shows "Sending messages to this app has been turned off" in the app DM/App Home view, use a channel mention first; direct messages require enabling the app's App Home Messages tab, adding the matching DM event subscription such as `message.im`, granting the needed bot scopes such as `im:history` and `chat:write`, and reinstalling the app. The running `python-slack-channel` agent validates the event and answers with Slack `chat.postMessage`.

## How It Works

`server.py` hosts the provider-facing HTTP endpoint and bridges the local `SlackChannel` from `channel.py` over MeshAgent's MessagePack channel protocol. For each Slack Events API payload, the channel parses `event.channel`, `event.user`, `event.text`, `event.ts`, and `event.thread_ts`. It emits a `TurnStart` with a Slack `Participant`, then waits for the agent response. Edit `channel.py` to customize parsing, participant mapping, or replies. Set `MESHAGENT_SLACK_API_BASE_URL` to point outbound API calls at a compatible test server.

The agent's text deltas are collected until `TurnEnded`, then the completed response is sent back to Slack with `chat.postMessage`. Long responses are split before sending.

The local and deployed process commands enable MeshAgent image generation with `--image-generation gpt-image-2`, so image requests produce generated-image events that the Slack channel uploads to Slack with `files.getUploadURLExternal` and `files.completeUploadExternal`. Override the local image model with `MESHAGENT_IMAGE_GENERATION_MODEL` before running `./scripts/dev.sh`.

When the agent attaches files with the room `attach_file` tool, the Slack channel resolves `room:///...` URLs from room storage, downloads allowed `https://...` or `http://...` file URLs, decodes `data:` URLs, and uploads the resulting bytes to Slack. If a file cannot be resolved, the original URL is included as a text fallback. Set `MESHAGENT_SLACK_OUTBOUND_FILE_MAX_BYTES` to control the largest outbound file the channel will download or upload; the default is `50000000` bytes.

The default thread path prefix is `threads/slack`; override it with `MESHAGENT_SLACK_THREAD_PREFIX` when you want to isolate multiple channel deployments.

`.gitignore` and `.dockerignore` both exclude `.env`, `.env.local`, local virtual environments, and generated cache directories.
