"""MollyBot — a Discord bot that replies in character as Molly Simpson.

Listens only in the channel named by CHANNEL_ID, keeps a short *shared*
conversation history per channel (with each human message tagged by the
speaker's name so Molly can tell people apart), and generates replies with
the Anthropic API.
"""

import os
import random
import re
import time
from collections import deque

import aiohttp
import discord
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from molly_prompt import MOLLY_SYSTEM_PROMPT

load_dotenv()

# All secrets/config come from the environment — nothing is hardcoded.
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
# Optional: without it, the GIF feature simply stays off and the bot still runs.
# Klipy is the post-Tenor GIF provider (free key at partner.klipy.com); Google's
# Tenor API stopped issuing keys in Jan 2026 and shuts down entirely 2026-06-30.
KLIPY_API_KEY = os.environ.get("KLIPY_API_KEY")

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1000
# Shared history is now spread across everyone in the channel, so it needs to
# hold more turns than a single 1:1 thread did.
HISTORY_LIMIT = 40  # max messages (humans + Molly) retained per channel
DISCORD_MAX_LEN = 2000  # Discord's hard cap per message
MAX_REACTIONS = 3  # most emoji Molly may slap on a single message
GIF_COOLDOWN_SECONDS = 90  # hard floor between GIFs per channel, so they stay rare
GIF_RATING = "pg-13"  # Klipy content rating: g < pg < pg-13 < r; this excludes r

# Molly signals reactions/GIFs with inline tags the users never see; the bot
# strips them out and acts on them. e.g. "[react:😂]" or "[gif: happy dance]".
REACT_TAG_RE = re.compile(r"\[react:\s*([^\]]+?)\s*\]", re.IGNORECASE)
GIF_TAG_RE = re.compile(r"\[gif:\s*([^\]]+?)\s*\]", re.IGNORECASE)

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
# Lazily-created shared HTTP session for Klipy GIF lookups.
_http_session: aiohttp.ClientSession | None = None
# Per-channel timestamp (monotonic) of the last GIF, for the cooldown above.
last_gif_at: dict[int, float] = {}

# Shared conversation history, keyed by Discord *channel* ID so Molly sees the
# whole room and can keep individuals straight. Each value is a bounded deque
# of {"role": ..., "content": ...} dicts; human turns carry a "Name: text"
# prefix. The deque drops the oldest message once HISTORY_LIMIT is exceeded.
histories: dict[int, deque] = {}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def get_history(channel_id: int) -> deque:
    """Return (creating if needed) the bounded history deque for a channel."""
    if channel_id not in histories:
        histories[channel_id] = deque(maxlen=HISTORY_LIMIT)
    return histories[channel_id]


def build_request_messages(history: deque) -> list[dict]:
    """Collapse the channel history into a valid Messages API payload.

    Consecutive same-role turns (e.g. several people talking before Molly
    replies) are merged into one block, and any leading non-user turns left
    behind by deque rotation are dropped so the payload starts with a user.
    """
    request_messages: list[dict] = []
    for entry in history:
        if request_messages and request_messages[-1]["role"] == entry["role"]:
            request_messages[-1]["content"] += "\n" + entry["content"]
        else:
            request_messages.append(dict(entry))

    while request_messages and request_messages[0]["role"] != "user":
        request_messages.pop(0)
    return request_messages


def parse_actions(reply_text: str) -> tuple[str, list[str], str | None]:
    """Pull Molly's inline action tags out of her reply.

    Returns (clean_text, reactions, gif_query): the message the users actually
    see, up to MAX_REACTIONS emoji to react with, and an optional GIF search
    query (the last [gif:...] tag wins if she somehow emits more than one).
    """
    reactions = [m.strip() for m in REACT_TAG_RE.findall(reply_text)][:MAX_REACTIONS]
    gif_matches = GIF_TAG_RE.findall(reply_text)
    gif_query = gif_matches[-1].strip() if gif_matches else None

    clean_text = GIF_TAG_RE.sub("", REACT_TAG_RE.sub("", reply_text))
    # Pulling a tag from mid-sentence can leave a double space; collapse runs of
    # spaces/tabs without touching newlines.
    clean_text = re.sub(r"[ \t]{2,}", " ", clean_text).strip()
    return clean_text, reactions, gif_query


def _first_gif_url(media: object) -> str | None:
    """Walk a Klipy item's media tree and return the best GIF (or any) URL.

    The per-item ``file`` object nests by size (md/hd/sm/...) then format
    (gif/webp/mp4), each with a ``url``. The exact keys aren't fully pinned in
    the public docs, so prefer gif-at-medium but fall back to any url found.
    """
    if not isinstance(media, dict):
        return None
    for size in ("md", "hd", "sm", "xs", "lg"):
        slot = media.get(size)
        if isinstance(slot, dict):
            for fmt in ("gif", "webp", "mp4"):
                url = (slot.get(fmt) or {}).get("url") if isinstance(slot.get(fmt), dict) else None
                if url:
                    return url
    # Fallback: first "url" string anywhere in the tree (prefer one ending .gif).
    found: list[str] = []
    stack = [media]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "url" and isinstance(val, str):
                    found.append(val)
                else:
                    stack.append(val)
        elif isinstance(node, list):
            stack.extend(node)
    return next((u for u in found if u.lower().endswith(".gif")), found[0] if found else None)


async def fetch_gif(query: str) -> str | None:
    """Return a GIF URL for the query via Klipy, or None on any failure."""
    if not KLIPY_API_KEY:
        return None

    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()

    # Klipy puts the app key in the path, not a header/query param.
    url = f"https://api.klipy.com/api/v1/{KLIPY_API_KEY}/gifs/search"
    params = {"q": query, "per_page": "20", "rating": GIF_RATING}
    try:
        async with _http_session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as exc:  # noqa: BLE001 — network/JSON issues shouldn't crash a reply
        print(f"Klipy API error: {exc}")
        return None

    if not data.get("result"):
        return None
    items = (data.get("data") or {}).get("data") or []
    if not items:
        return None
    # Pick a random hit so the same query doesn't always yield the same GIF.
    item = random.choice(items)
    return _first_gif_url(item.get("file") or item.get("files"))


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

    # Tag every human turn with the speaker's display name so Molly can tell
    # the people in the channel apart and address each by name. The prefix is
    # part of the stored content; the system prompt explains the convention.
    speaker = message.author.display_name
    history = get_history(message.channel.id)
    history.append({"role": "user", "content": f"{speaker}: {content}"})

    request_messages = build_request_messages(history)

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

    clean_text, reactions, gif_query = parse_actions(reply_text)

    # React to the triggering message. A bad/unknown emoji is just skipped.
    for emoji in reactions:
        try:
            await message.add_reaction(emoji)
        except Exception as exc:  # noqa: BLE001 — a bad emoji must not kill the reply
            print(f"add_reaction failed for {emoji!r}: {exc}")

    # Resolve a GIF, but only if this channel's cooldown has elapsed, so GIFs
    # stay an occasional treat instead of every-other-message spam.
    gif_url = None
    if gif_query:
        now = time.monotonic()
        if now - last_gif_at.get(message.channel.id, 0.0) >= GIF_COOLDOWN_SECONDS:
            gif_url = await fetch_gif(gif_query)
            if gif_url:
                last_gif_at[message.channel.id] = now

    # If she said nothing, reacted with nothing, and has no gif, fall back to a
    # line so the user always gets some response.
    if not clean_text and not reactions and not gif_url:
        clean_text = "...uh. i blanked. say that again?"

    # Record what she actually did so next turn's history stays coherent.
    history_note = clean_text or ("(sends a gif)" if gif_url else "(reacts)")
    history.append({"role": "assistant", "content": history_note})

    # Send the text (chunked to Discord's limit), then the gif if any. With no
    # text, the gif carries the reply; with neither, the reaction alone stands.
    if clean_text:
        chunks = [
            clean_text[i:i + DISCORD_MAX_LEN]
            for i in range(0, len(clean_text), DISCORD_MAX_LEN)
        ]
        await message.reply(chunks[0])
        for chunk in chunks[1:]:
            await message.channel.send(chunk)
        if gif_url:
            await message.channel.send(gif_url)
    elif gif_url:
        await message.reply(gif_url)


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
