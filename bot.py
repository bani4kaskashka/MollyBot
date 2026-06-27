"""MollyBot — a Discord bot that replies in character as Molly Simpson.

In her home channel (CHANNEL_ID) Molly replies to every message; in any other
channel she only responds when she is @-mentioned. History is kept per channel
(each human turn tagged with the speaker's name so she can tell people apart),
and replies are generated with the Anthropic API.
"""

import asyncio
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
MAX_TOKENS = 1000  # a hard ceiling, not a target — the prompt keeps replies short
# Shared history is now spread across everyone in the channel, so it needs to
# hold more turns than a single 1:1 thread did.
HISTORY_LIMIT = 60  # max messages (humans + Molly) retained per channel
# When Molly is pulled into a conversation she hasn't been tracking, prime her
# with this many of the channel's recent messages so she knows what's going on.
CONTEXT_MESSAGES = 10
DISCORD_MAX_LEN = 2000  # Discord's hard cap per message
MAX_REACTIONS = 4  # most emoji Molly may slap on a single message
MAX_STICKERS = 3  # Discord's hard cap of stickers per message
GIF_COOLDOWN_SECONDS = 60  # hard floor between GIFs per channel, so they stay a treat
GIF_RATING = "pg-13"  # Klipy content rating: g < pg < pg-13 < r; this excludes r
# How many server emoji/sticker names to list in the prompt, to bound token use.
MAX_PROMPT_EMOJIS = 60
MAX_PROMPT_STICKERS = 30

# Molly signals reactions/GIFs/stickers with inline tags the users never see;
# the bot strips them out and acts on them, e.g. "[react:😂]", "[gif: happy
# dance]", "[sticker: wave]". Custom server emoji she writes inline as :name:.
REACT_TAG_RE = re.compile(r"\[react:\s*([^\]]+?)\s*\]", re.IGNORECASE)
GIF_TAG_RE = re.compile(r"\[gif:\s*([^\]]+?)\s*\]", re.IGNORECASE)
STICKER_TAG_RE = re.compile(r"\[sticker:\s*([^\]]+?)\s*\]", re.IGNORECASE)
EMOJI_SHORTCODE_RE = re.compile(r":([a-zA-Z0-9_]{2,32}):")
# Custom emoji as they appear in raw message content: <:name:id> / <a:name:id>.
CUSTOM_EMOJI_RE = re.compile(r"<(a)?:(\w+):(\d+)>")

# Vision: incoming images Molly can actually see (Claude-supported formats only).
MAX_IMAGES = 8  # per message, to bound request size/cost
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
SUPPORTED_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

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
# Channels whose history has been primed from Discord at least once this run.
primed_channels: set[int] = set()
# One lock per channel so messages are handled strictly one at a time there —
# concurrent handlers would otherwise interleave on the shared history deque and
# cross-contaminate replies (answering two people as if they were the same one).
channel_locks: dict[int, asyncio.Lock] = {}

intents = discord.Intents.default()
intents.message_content = True
# Privileged "Server Members" intent — needed so the guild member cache is
# populated and we can resolve per-server nicknames for authors of messages
# fetched via channel.history() (REST history carries no inline member data).
# Must also be enabled in the Discord Developer Portal or the bot won't start.
intents.members = True
client = discord.Client(intents=intents)


def get_history(channel_id: int) -> deque:
    """Return (creating if needed) the bounded history deque for a channel."""
    if channel_id not in histories:
        histories[channel_id] = deque(maxlen=HISTORY_LIMIT)
    return histories[channel_id]


def get_channel_lock(channel_id: int) -> asyncio.Lock:
    """Return (creating if needed) the per-channel handling lock."""
    lock = channel_locks.get(channel_id)
    if lock is None:
        lock = channel_locks[channel_id] = asyncio.Lock()
    return lock


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
            # Only role/content reach the API — drop bookkeeping keys like "id".
            request_messages.append(
                {"role": entry["role"], "content": entry["content"]}
            )

    while request_messages and request_messages[0]["role"] != "user":
        request_messages.pop(0)
    return request_messages


def parse_actions(reply_text: str) -> tuple[str, list[str], str | None, list[str]]:
    """Pull Molly's inline action tags out of her reply.

    Returns (clean_text, reactions, gif_query, sticker_names): the message the
    users actually see, up to MAX_REACTIONS emoji to react with, an optional GIF
    search query (the last [gif:...] tag wins if she emits more than one), and up
    to MAX_STICKERS server-sticker names to attach.
    """
    reactions = [m.strip() for m in REACT_TAG_RE.findall(reply_text)][:MAX_REACTIONS]
    gif_matches = GIF_TAG_RE.findall(reply_text)
    gif_query = gif_matches[-1].strip() if gif_matches else None
    sticker_names = [m.strip() for m in STICKER_TAG_RE.findall(reply_text)][:MAX_STICKERS]

    clean_text = STICKER_TAG_RE.sub(
        "", GIF_TAG_RE.sub("", REACT_TAG_RE.sub("", reply_text))
    )
    # Pulling a tag from mid-sentence can leave a double space; collapse runs of
    # spaces/tabs without touching newlines.
    clean_text = re.sub(r"[ \t]{2,}", " ", clean_text).strip()
    return clean_text, reactions, gif_query, sticker_names


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


def resolve_emoji_markup(text: str, guild: "discord.Guild | None") -> str:
    """Replace ``:name:`` with real ``<:name:id>`` markup for this guild's emoji.

    Unknown shortcodes (standard emoji, typos, false hits like times) are left
    untouched, so the substitution is always safe.
    """
    if guild is None or not text:
        return text
    by_name = {e.name.lower(): e for e in guild.emojis}

    def repl(match: "re.Match[str]") -> str:
        emoji = by_name.get(match.group(1).lower())
        return str(emoji) if emoji else match.group(0)

    return EMOJI_SHORTCODE_RE.sub(repl, text)


def resolve_reaction(token: str, guild: "discord.Guild | None"):
    """Turn a react token into something add_reaction accepts.

    A ``:name:`` / ``name`` matching one of the guild's custom emoji becomes
    that emoji object; anything else is passed through as a unicode emoji.
    """
    name = token.strip().strip(":")
    if guild is not None:
        for emoji in guild.emojis:
            if emoji.name.lower() == name.lower():
                return emoji
    return token


def resolve_stickers(names: list[str], guild: "discord.Guild | None") -> list:
    """Map sticker names to this guild's GuildSticker objects (server-only)."""
    if guild is None or not names:
        return []
    by_name = {s.name.lower(): s for s in guild.stickers}
    found = [by_name[n.lower()] for n in names if n.lower() in by_name]
    return found[:MAX_STICKERS]


def build_emoji_sticker_note(guild: "discord.Guild | None") -> str:
    """Tell Molly which custom emoji and stickers this server actually has."""
    if guild is None:
        return ""
    emojis = list(guild.emojis)[:MAX_PROMPT_EMOJIS]
    stickers = list(guild.stickers)[:MAX_PROMPT_STICKERS]
    if not emojis and not stickers:
        return ""

    lines = ["", "SERVER EMOJI & STICKERS AVAILABLE TO YOU RIGHT NOW:"]
    if emojis:
        names = " ".join(f":{e.name}:" for e in emojis)
        more = " (and more)" if len(guild.emojis) > len(emojis) else ""
        lines.append(f"- Custom emoji — drop them inline as :name: — {names}{more}")
    else:
        lines.append("- Custom emoji: none on this server.")
    if stickers:
        names = ", ".join(f'"{s.name}"' for s in stickers)
        more = " (and more)" if len(guild.stickers) > len(stickers) else ""
        lines.append(f"- Stickers — send one with [sticker: name] — {names}{more}")
    else:
        lines.append("- Stickers: none on this server.")
    lines.append(
        "Use ONLY names from these exact lists. If nothing fits, skip it — never "
        "invent emoji or sticker names."
    )
    return "\n".join(lines)


def collect_images(message: discord.Message) -> tuple[list[dict], list[str]]:
    """Gather viewable images from a message for Claude's vision.

    Returns (image_blocks, notes): Anthropic URL image blocks for any image
    attachments, stickers, and custom emoji in the message, plus short text
    notes ("[image]", "[sticker: name]") describing them for the stored history.
    Capped at MAX_IMAGES so a spammy message can't blow up the request.
    """
    blocks: list[dict] = []
    notes: list[str] = []

    def add(url: str) -> None:
        blocks.append({"type": "image", "source": {"type": "url", "url": url}})

    for att in message.attachments:
        ctype = (att.content_type or "").lower()
        if ctype in SUPPORTED_IMAGE_TYPES or att.filename.lower().endswith(
            SUPPORTED_IMAGE_EXTS
        ):
            add(att.url)
            notes.append("[image]")

    for sticker in message.stickers:
        notes.append(f"[sticker: {sticker.name}]")
        # Lottie stickers are JSON, not viewable images — note them but skip.
        if getattr(sticker.format, "name", "").lower() in ("png", "apng", "gif"):
            add(sticker.url)

    for match in CUSTOM_EMOJI_RE.finditer(message.content or ""):
        animated, name, emoji_id = match.groups()
        ext = "gif" if animated else "png"
        add(f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}")
        notes.append(f"[emoji: {name}]")

    return blocks[:MAX_IMAGES], notes


def member_display_name(guild: "discord.Guild | None", author) -> str:
    """The author's per-server nickname when available, else their global name.

    Messages from channel.history() (REST) carry no inline member data, so
    author.display_name there falls back to the @handle. Looking the author up
    in the guild's member cache (populated by the members intent) recovers the
    nickname people actually go by in this server.
    """
    if guild is not None:
        member = guild.get_member(author.id)
        if member is not None:
            return member.display_name
    return author.display_name


def message_to_turn(msg: discord.Message) -> dict | None:
    """Convert a Discord message into a history turn, or None if it's empty/skip.

    Molly's own messages become assistant turns; everyone else's become
    name-tagged user turns. Other bots are skipped. Stickers and image
    attachments are noted in text so the context reads sensibly.
    """
    if msg.author.bot and (client.user is None or msg.author.id != client.user.id):
        return None

    notes = [f"[sticker: {s.name}]" for s in msg.stickers]
    if any(
        (att.content_type or "").lower() in SUPPORTED_IMAGE_TYPES
        or att.filename.lower().endswith(SUPPORTED_IMAGE_EXTS)
        for att in msg.attachments
    ):
        notes.append("[image]")
    text = " ".join(p for p in [msg.clean_content.strip(), *notes] if p).strip()
    if not text:
        return None

    if client.user is not None and msg.author.id == client.user.id:
        return {"role": "assistant", "content": text, "id": msg.id}
    name = member_display_name(msg.guild, msg.author)
    return {"role": "user", "content": f"{name}: {text}", "id": msg.id}


async def prime_channel_context(
    channel: discord.abc.Messageable, history: deque, before: discord.Message
) -> None:
    """Replace `history` with the channel's recent messages so Molly has context.

    Pulls up to CONTEXT_MESSAGES messages just before `before`, oldest first, so
    she knows what's going on when dropped into a conversation. Failures (e.g.
    missing Read Message History) degrade gracefully to no context.
    """
    try:
        recent = [
            msg async for msg in channel.history(limit=CONTEXT_MESSAGES, before=before)
        ]
    except Exception as exc:  # noqa: BLE001 — never let backfill break a reply
        print(f"Context backfill failed: {exc}")
        return

    turns = [turn for msg in reversed(recent) if (turn := message_to_turn(msg))]
    history.clear()
    history.extend(turns)


@client.event
async def on_ready() -> None:
    print(f"Logged in as {client.user} — listening in channel {CHANNEL_ID}")


@client.event
async def on_message(message: discord.Message) -> None:
    # Never respond to ourselves or any other bot.
    if message.author.bot:
        return

    # CHANNEL_ID is Molly's home channel: she replies to everything there.
    # Anywhere else she only speaks up when explicitly @-mentioned (a ping or a
    # reply to her) — and message.reply() below answers exactly that message.
    # @everyone/@here deliberately don't count, so she isn't dragged into mass
    # pings in other channels.
    if message.channel.id != CHANNEL_ID:
        if client.user is None or not any(
            user.id == client.user.id for user in message.mentions
        ):
            return

    # clean_content renders mentions as readable "@name" instead of raw "<@id>".
    content = message.clean_content.strip()
    # Anything she can actually see: image attachments, stickers, custom emoji.
    image_blocks, media_notes = collect_images(message)
    # A sticker/image-only post has no text but should still get a reply; only
    # bail when there's truly nothing (e.g. a non-image attachment).
    if not content and not image_blocks and not media_notes:
        return

    # Serialize handling within a channel: if two people post at the same time,
    # concurrent on_message coroutines would interleave on the shared history
    # deque and Molly could answer both as the same person. The lock makes her
    # work through a channel's messages one at a time, each with correct context.
    async with get_channel_lock(message.channel.id):
        await generate_and_reply(message, content, image_blocks, media_notes)


async def generate_and_reply(
    message: discord.Message,
    content: str,
    image_blocks: list[dict],
    media_notes: list[str],
) -> None:
    """Build context, call the model, and post one reply for `message`.

    The caller holds the channel lock, so this runs without another message in
    the same channel mutating the shared history underneath it.
    """
    # Build the textual record of the turn — the words plus short notes for any
    # media — so the stored (text-only) history stays coherent across turns.
    text_repr = " ".join(part for part in [content, *media_notes] if part).strip()
    if not text_repr:
        text_repr = "(no text)"

    speaker = member_display_name(message.guild, message.author)
    history = get_history(message.channel.id)

    # Prime with the channel's recent messages so she knows what's going on. Her
    # home channel is primed once (she then sees everything live); other channels
    # — where she's only present when @-mentioned — are re-primed every time so
    # she catches up on whatever was said while she was away.
    is_home = message.channel.id == CHANNEL_ID
    if not is_home or message.channel.id not in primed_channels:
        await prime_channel_context(message.channel, history, before=message)
        primed_channels.add(message.channel.id)

    # Tag every human turn with the speaker's display name so Molly can tell
    # the people in the channel apart and address each by name.
    history.append(
        {"role": "user", "content": f"{speaker}: {text_repr}", "id": message.id}
    )

    # Text-only payload (also the fallback if a vision request fails). For this
    # turn only, attach the images to the final user block so Molly sees them
    # without bloating stored history with image data.
    request_messages = build_request_messages(history)
    vision_messages = request_messages
    if image_blocks and request_messages:
        last = dict(request_messages[-1])
        text_value = last["content"] if isinstance(last["content"], str) else ""
        text_part = [{"type": "text", "text": text_value}] if text_value else []
        last["content"] = text_part + image_blocks
        vision_messages = [*request_messages[:-1], last]

    system_prompt = MOLLY_SYSTEM_PROMPT + build_emoji_sticker_note(message.guild)
    try:
        async with message.channel.typing():
            try:
                response = await anthropic_client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    messages=vision_messages,
                )
            except Exception as vision_exc:  # noqa: BLE001
                # If an image couldn't be fetched/parsed, retry without images
                # so she still answers instead of going silent.
                if not image_blocks:
                    raise
                print(f"Vision request failed, retrying text-only: {vision_exc}")
                response = await anthropic_client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    messages=request_messages,
                )
    except Exception as exc:  # noqa: BLE001 — surface any API/network failure
        print(f"Anthropic API error: {exc}")
        await message.reply("ugh my brain just glitched lol, gimme a sec n try again :c")
        return

    reply_text = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()

    clean_text, reactions, gif_query, sticker_names = parse_actions(reply_text)
    guild = message.guild

    # Turn :name: shortcodes into real markup for this server's custom emoji.
    clean_text = resolve_emoji_markup(clean_text, guild)

    # React to the triggering message (unicode or a custom server emoji). A
    # bad/unknown emoji is just skipped.
    for token in reactions:
        emoji = resolve_reaction(token, guild)
        try:
            await message.add_reaction(emoji)
        except Exception as exc:  # noqa: BLE001 — a bad emoji must not kill the reply
            print(f"add_reaction failed for {token!r}: {exc}")

    # Resolve any server stickers she asked for (server-only, capped).
    stickers = resolve_stickers(sticker_names, guild)

    # Resolve a GIF, but only if this channel's cooldown has elapsed, so GIFs
    # stay an occasional treat instead of every-other-message spam.
    gif_url = None
    if gif_query:
        now = time.monotonic()
        if now - last_gif_at.get(message.channel.id, 0.0) >= GIF_COOLDOWN_SECONDS:
            gif_url = await fetch_gif(gif_query)
            if gif_url:
                last_gif_at[message.channel.id] = now

    # If she produced nothing at all, fall back to a line so the user always
    # gets some response.
    if not clean_text and not reactions and not gif_url and not stickers:
        clean_text = "...uh. i blanked. say that again?"

    # Record what she actually did so next turn's history stays coherent.
    history_note = clean_text or (
        "(sends a gif)" if gif_url else "(sends a sticker)" if stickers else "(reacts)"
    )
    history.append({"role": "assistant", "content": history_note})

    # Send the text (chunked to Discord's limit) with any stickers attached to
    # the first message, then the gif. With no text but a sticker, the sticker
    # carries the reply; with only a gif, the gif does; otherwise the reaction
    # alone stands.
    sticker_kwargs = {"stickers": stickers} if stickers else {}
    sent = False
    if clean_text:
        chunks = [
            clean_text[i:i + DISCORD_MAX_LEN]
            for i in range(0, len(clean_text), DISCORD_MAX_LEN)
        ]
        await message.reply(chunks[0], **sticker_kwargs)
        for chunk in chunks[1:]:
            await message.channel.send(chunk)
        sent = True
    elif stickers:
        await message.reply(**sticker_kwargs)
        sent = True

    if gif_url:
        if sent:
            await message.channel.send(gif_url)
        else:
            await message.reply(gif_url)


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
