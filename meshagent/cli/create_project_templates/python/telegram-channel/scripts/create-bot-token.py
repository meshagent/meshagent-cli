from __future__ import annotations

import argparse
import asyncio
import getpass
import os
from pathlib import Path
import re
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE_PATH = ROOT / ".env.example"
ENV_KEY_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
BOT_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
BOT_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,30}[Bb][Oo][Tt]$")


def decode_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ENV_KEY_RE.match(line.strip())
        if match is None:
            continue
        values[match.group(1)] = decode_env_value(match.group(2))
    return values


def merged_env_values() -> dict[str, str]:
    values = load_env_file(ENV_PATH)
    for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN"):
        env_value = os.getenv(name, "").strip()
        if env_value:
            values[name] = env_value
    return values


def ensure_env_file() -> None:
    if ENV_PATH.exists():
        return
    if ENV_EXAMPLE_PATH.exists():
        shutil.copyfile(ENV_EXAMPLE_PATH, ENV_PATH)
    else:
        ENV_PATH.write_text("", encoding="utf-8")
    print("Created .env")


def set_env_values(updates: dict[str, str]) -> None:
    if not updates:
        return
    ensure_env_file()
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    next_lines: list[str] = []
    for line in lines:
        match = ENV_KEY_RE.match(line.strip())
        if match is None or match.group(1) not in remaining:
            next_lines.append(line)
            continue
        name = match.group(1)
        next_lines.append(f"{name}={remaining.pop(name)}")
    if remaining:
        if next_lines and next_lines[-1] != "":
            next_lines.append("")
        for name, value in remaining.items():
            next_lines.append(f"{name}={value}")
    ENV_PATH.write_text("\n".join(next_lines) + "\n", encoding="utf-8")


def sanitize_botfather_text(text: str) -> str:
    return BOT_TOKEN_RE.sub("<redacted-bot-token>", text.strip())


def parse_bot_token(text: str) -> str | None:
    match = BOT_TOKEN_RE.search(text)
    if match is None:
        return None
    return match.group(0)


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def prompt_required(
    label: str,
    *,
    secret: bool = False,
    help_text: str | None = None,
) -> str:
    while True:
        if secret:
            value = getpass.getpass(f"{label}: ").strip()
        else:
            value = input(f"{label}: ").strip()
        if value:
            return value
        print(f"{label} is required.", file=sys.stderr)
        if help_text is not None:
            print(help_text, file=sys.stderr)


def normalize_bot_username(value: str) -> str:
    return value.strip().lstrip("@")


def validate_bot_username(value: str) -> str | None:
    username = normalize_bot_username(value)
    if BOT_USERNAME_RE.fullmatch(username) is None:
        return (
            "Bot username must be 5-32 letters, numbers, or underscores, "
            "start with a letter, and end with bot."
        )
    return None


def prompt_bot_username(label: str) -> str:
    while True:
        username = normalize_bot_username(prompt_required(label))
        error = validate_bot_username(username)
        if error is None:
            return username
        print(error, file=sys.stderr)


def resolve_api_credentials(
    args: argparse.Namespace,
) -> tuple[int, str, dict[str, str]]:
    values = merged_env_values()
    updates: dict[str, str] = {}

    raw_api_id = str(args.api_id or values.get("TELEGRAM_API_ID", "")).strip()
    api_hash = str(args.api_hash or values.get("TELEGRAM_API_HASH", "")).strip()

    if not raw_api_id:
        if not is_interactive():
            raise RuntimeError("TELEGRAM_API_ID is required.")
        print("Create or reuse a Telegram API app at https://my.telegram.org.")
        raw_api_id = prompt_required(
            "Telegram API ID",
            help_text=(
                "Open https://my.telegram.org, create or reuse an API app, "
                "then paste the numeric API ID here. Press Ctrl+C to cancel."
            ),
        )
    if not raw_api_id.isdigit():
        raise RuntimeError("TELEGRAM_API_ID must be numeric.")
    if not api_hash:
        if not is_interactive():
            raise RuntimeError("TELEGRAM_API_HASH is required.")
        api_hash = prompt_required(
            "Telegram API hash",
            secret=True,
            help_text=(
                "Paste the API hash from https://my.telegram.org. "
                "Press Ctrl+C to cancel."
            ),
        )

    file_values = load_env_file(ENV_PATH)
    if file_values.get("TELEGRAM_API_ID", "").strip() != raw_api_id:
        updates["TELEGRAM_API_ID"] = raw_api_id
    if file_values.get("TELEGRAM_API_HASH", "").strip() != api_hash:
        updates["TELEGRAM_API_HASH"] = api_hash

    return int(raw_api_id), api_hash, updates


def resolve_bot_identity(args: argparse.Namespace) -> tuple[str, str]:
    bot_name = str(args.name or "").strip()
    username = normalize_bot_username(str(args.username or ""))

    if not bot_name:
        if not is_interactive():
            raise RuntimeError("--name is required in non-interactive mode.")
        bot_name = prompt_required("Bot display name")
    if "\n" in bot_name or "\r" in bot_name:
        raise RuntimeError("Bot display name cannot contain newlines.")

    if not username:
        if not is_interactive():
            raise RuntimeError("--username is required in non-interactive mode.")
        username = prompt_bot_username("Bot username")
    else:
        username_error = validate_bot_username(username)
        if username_error is not None:
            raise RuntimeError(username_error)

    return bot_name, username


async def create_bot_token(
    *,
    api_id: int,
    api_hash: str,
    bot_name: str,
    username: str,
) -> str:
    try:
        from telethon import TelegramClient
        from telethon.errors import RPCError
        from telethon.sessions import StringSession
    except ModuleNotFoundError as exc:
        if exc.name == "telethon":
            raise RuntimeError(
                "Telethon is not installed. Run ./scripts/install.sh, then retry."
            ) from exc
        raise

    print("Signing in to Telegram with an in-memory Telethon session.")
    print("No Telegram user session file will be written.")

    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.start()
        async with client.conversation("@BotFather", timeout=180) as conversation:
            await conversation.send_message("/newbot")
            await conversation.get_response()

            await conversation.send_message(bot_name)
            await conversation.get_response()

            while True:
                await conversation.send_message(username)
                response = await conversation.get_response()
                response_text = response.raw_text or ""
                token = parse_bot_token(response_text)
                if token is not None:
                    return token

                print("")
                print("BotFather did not create the bot:")
                print(sanitize_botfather_text(response_text))
                print("")
                if not is_interactive():
                    raise RuntimeError("BotFather did not return a bot token.")
                username = prompt_bot_username("Try another bot username")
    except RPCError as exc:
        raise RuntimeError(f"Telegram API error: {exc}") from exc
    finally:
        await client.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a Telegram bot with BotFather and save its token to .env.",
    )
    parser.add_argument("--name", help="Bot display name to send to BotFather.")
    parser.add_argument(
        "--username",
        help="Bot username to send to BotFather. It must end with bot.",
    )
    parser.add_argument(
        "--api-id", help="Telegram API ID from https://my.telegram.org."
    )
    parser.add_argument(
        "--api-hash",
        help="Telegram API hash from https://my.telegram.org.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Create a new bot and overwrite TELEGRAM_BOT_TOKEN in .env.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    values = merged_env_values()
    existing_token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
    if existing_token and not args.force:
        print("TELEGRAM_BOT_TOKEN is already configured.")
        print("Use --force to create a new bot and overwrite .env.")
        return 0

    try:
        api_id, api_hash, updates = resolve_api_credentials(args)
        bot_name, username = resolve_bot_identity(args)
        token = asyncio.run(
            create_bot_token(
                api_id=api_id,
                api_hash=api_hash,
                bot_name=bot_name,
                username=username,
            )
        )
    except (KeyboardInterrupt, EOFError):
        print("\nBot token creation canceled.", file=sys.stderr)
        return 130
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    updates["TELEGRAM_BOT_TOKEN"] = token
    set_env_values(updates)
    print("Saved TELEGRAM_BOT_TOKEN to .env.")
    print("Run ./scripts/configure-telegram.sh to fill any remaining settings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
