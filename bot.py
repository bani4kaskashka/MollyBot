"""MollyBot — a Discord bot that replies in character as Molly Simpson.

Listens only in the channel named by CHANNEL_ID, keeps a short per-user
conversation history in memory, and generates replies with the Anthropic API.
"""

import os
from collections import deque

import discord
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from molly_prompt import MOLLY_SYSTEM_PROMPT

load_dotenv()

# All secrets/config come from the environment — nothing is hardcoded.
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1000
HISTORY_LIMIT = 12  # max messages (user + assistant) retained per user
DISCORD_MAX_LEN = 2000  # Discord's hard cap per message

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# Per-user conversation history, keyed by Discord user ID. Each value is a
# bounded deque of {"role": ..., "content": ...} dicts; the deque drops the
# oldest message once HISTORY_LIMIT is exceeded.
histories: dict[int, deque] = {}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def get_history(user_id: int) -> deque:
    """Return (creating if needed) the bounded history deque for a user."""
    if user_id not in histories:
        histories[user_id] = deque(maxlen=HISTORY_LIMIT)
    return histories[user_id]


@client.event
async def on_ready() -> None:
    print(f"Logged in as {client.user} — listening in channel {CHANNEL_ID}")


@client.event
async def on_message(message: discord.Message) -> None:
    # Never respond to ourselves or any other bot.
    if message.author.bot:
        return

    # Only listen in the designated channel.
    if message.channel.id != CHANNEL_ID:
        return

    # Ignore empty messages (e.g. attachment-only posts).
    content = message.content.strip()
    if not content:
        return

    history = get_history(message.author.id)
    history.append({"role": "user", "content": content})

    # The Messages API requires the first message to be from the user; the
    # bounded deque can leave a leading assistant turn after it rotates, so
    # drop any leading non-user messages from the request payload.
    request_messages = list(history)
    while request_messages and request_messages[0]["role"] != "user":
        request_messages.pop(0)

    try:
        async with message.channel.typing():
            response = await anthropic_client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=MOLLY_SYSTEM_PROMPT,
                messages=request_messages,
            )
    except Exception as exc:  # noqa: BLE001 — surface any API/network failure
        print(f"Anthropic API error: {exc}")
        await message.reply("ugh my brain just glitched lol, gimme a sec n try again :c")
        return

    reply_text = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()

    if not reply_text:
        reply_text = "...uh. i blanked. say that again?"

    history.append({"role": "assistant", "content": reply_text})

    # Split into Discord-sized chunks; reply to the user with the first one.
    chunks = [
        reply_text[i:i + DISCORD_MAX_LEN]
        for i in range(0, len(reply_text), DISCORD_MAX_LEN)
    ]
    await message.reply(chunks[0])
    for chunk in chunks[1:]:
        await message.channel.send(chunk)


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
