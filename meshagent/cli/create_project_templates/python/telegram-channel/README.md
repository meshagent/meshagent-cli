# Python Telegram Channel

Runs a Telegram-backed channel inside a MeshAgent process agent. Incoming Telegram messages become trusted user turns, and completed agent responses are sent back to the same Telegram chat.

The channel runs as a language-neutral child process using `--channel='command:["python","server.py"]'` alongside the normal `--channel chat`. Its complete, editable implementation lives in `channel.py`; no separate Telegram channel package is installed.

## Telegram Credentials

Create or reuse a Telegram API app at `https://my.telegram.org`, then run the
guided TUI setup:

```bash
./scripts/configure-telegram.sh
```

The setup flow uses the same full-screen terminal UI style as the MeshAgent room picker. On first run, it shows dependency installation progress in the TUI, then collects `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_BOT_TOKEN`. If setup cannot prepare dependencies, it shows a TUI error screen with the correction step instead of a Python traceback. It writes detailed setup logs to `.meshagent/telegram-setup-install.log`.

If you do not already have a bot token, create one through BotFather with:

```bash
./scripts/create-bot-token.sh --name "My MeshAgent Bot" --username my_meshagent_bot
```

The helper also needs `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`. It signs in to Telegram with Telethon using an in-memory user session, asks BotFather to create the bot, saves `TELEGRAM_BOT_TOKEN` to `.env`, and does not write a Telegram user session file. Telegram may ask for your phone number, login code, and two-step verification password during that sign-in. Keep `.env` private; the bot token controls the Telegram bot until you revoke or rotate it with BotFather.

`.env` is listed in `.gitignore` and `.dockerignore`, so local credentials are not committed or copied into the image build context. Local development uses Telethon and needs `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_BOT_TOKEN`. Deploys store service-account secrets, use Telegram's Bot API webhook path, and only need the bot token at runtime.

Telegram bots only receive messages from users who have started the bot or from chats where the bot has been added. For group chats, use BotFather privacy settings and chat permissions that match the messages you expect the channel to process.

## Platform Secrets

Local development reads Telegram credentials from `.env`. Deployment does not put sensitive values in the service template or pass them as `--set` values.

Set `MESHAGENT_TELEGRAM_ALLOWED_CHAT_IDS` to a comma-separated chat ID allowlist when you only want the channel to process and respond to specific Telegram chats. Use Telethon's marked chat IDs, such as positive private chat IDs, negative group IDs, and `-100...` channel or supergroup IDs. Leave it empty to allow all chats.

## Text And Media

The channel handles Telegram text messages, media-only messages, and media messages with captions. Inbound media is downloaded with Telethon, uploaded to room storage, and passed to the room agent as `AgentFileContent` with a `room:///...` URL. Set `MESHAGENT_TELEGRAM_MEDIA_STORAGE_PREFIX` to change the storage prefix, and set `MESHAGENT_TELEGRAM_INBOUND_MEDIA_MAX_BYTES` to control the largest inbound media file the channel will download.

When the agent attaches files with the room `attach_file` tool, the Telegram channel sends those attachments back with Telethon `send_file`. `room:///...` attachment URLs are downloaded with `room.storage.download(path=...)` before upload to Telegram, and allowed `https://...` or `http://...` media URLs are downloaded by the channel and uploaded as Telegram files.

The local and deployed process commands enable MeshAgent image generation with `--image-generation gpt-image-2`, so image requests produce generated-image events that the Telegram channel sends as image uploads. Override the local image model with `MESHAGENT_IMAGE_GENERATION_MODEL` before running `./scripts/dev.sh`.

When you run `./scripts/deploy.sh`, the script uses the MeshAgent room picker when `--room` is omitted, then creates or reuses the `python-telegram-channel` service account and stores:

- `TELEGRAM_BOT_TOKEN` as service-account secret `telegram-bot-token`
- a generated Telegram webhook validation secret as service-account secret `telegram-webhook-secret`

`.meshagent/deploy.yaml` runs the container as that service account with `container.run_as`, injects the bot token through a service-account secret-backed environment variable, and configures `MESHAGENT_TELEGRAM_MODE=webhook`.

`scripts/deploy.sh` derives a stable webhook validation secret from `TELEGRAM_BOT_TOKEN` and stores it automatically. After the service-account secrets exist, future deploys can omit `TELEGRAM_BOT_TOKEN` from `.env` by setting `MESHAGENT_TELEGRAM_SKIP_CONFIGURE=1`. For non-interactive deploys, pass `--room <room>` or set `MESHAGENT_ROOM`.

The deploy template publishes liveness at `/health` and an HTTP webhook endpoint at `/telegram/webhook`. MeshAgent validates Telegram's `X-Telegram-Bot-Api-Secret-Token` header before proxying the request to `server.py`, which validates the header again with the injected `TELEGRAM_WEBHOOK_SECRET` and returns Telegram's JSON acknowledgement. `scripts/deploy.sh` uses `MESHAGENT_TELEGRAM_WEBHOOK_URL` when set; otherwise it derives `https://<domain>/telegram/webhook` from `MESHAGENT_TELEGRAM_WEBHOOK_DOMAIN` or the room's default MeshAgent pages domain, then calls Telegram `setWebhook` after deploy using the generated secret token.

## Next Steps

1. Configure Telegram:

   ```bash
   ./scripts/configure-telegram.sh
   ```

2. Run locally:

   ```bash
   ./scripts/dev.sh
   ```

3. Deploy:

   ```bash
   ./scripts/deploy.sh
   ```

## How It Works

`server.py` hosts the deployed webhook endpoint and bridges the local `TelegramChannel` from `channel.py` over MeshAgent's MessagePack channel protocol. `scripts/dev.sh` builds the JSON command value using the virtual environment's exact Python path, then starts the equivalent of:

```bash
meshagent process join --channel chat --channel='command:["python","server.py"]'
```

For local development, each incoming Telegram text or media message arrives through Telethon. For deployed services, Telegram POSTs to `/telegram/webhook` and the local HTTP endpoint passes the validated update directly to the channel, which sends replies through the Bot API. In both modes, the channel emits a `TurnStart` with a `Participant` whose name comes from the Telegram sender. The agent's text and file deltas are collected until `TurnEnded`, then the completed response is sent back to Telegram. Edit `channel.py` to customize this behavior. Use `MESHAGENT_TELEGRAM_BOT_API_BASE_URL` to point outbound Bot API calls at a compatible test server.

The default thread path prefix is `.threads/telegram`. Override it with `MESHAGENT_TELEGRAM_THREAD_PREFIX` when you want to isolate multiple channel deployments.
