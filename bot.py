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
from collections import OrderedDict, deque

import aiohttp
import discord
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

import memory
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
# Floor between Molly reacting to emoji-reactions on her own messages, per channel,
# so a message getting reaction-spammed doesn't make her spam back.
REACTION_REPLY_COOLDOWN = 45
# How many server emoji/sticker names to list in the prompt, to bound token use.
MAX_PROMPT_EMOJIS = 60
MAX_PROMPT_STICKERS = 30

# Molly signals reactions/GIFs/stickers with inline tags the users never see;
# the bot strips them out and acts on them, e.g. "[react:😂]", "[gif: happy
# dance]", "[sticker: wave]". Custom server emoji she writes inline as :name:.
REACT_TAG_RE = re.compile(r"\[react:\s*([^\]]+?)\s*\]", re.IGNORECASE)
GIF_TAG_RE = re.compile(r"\[gif:\s*([^\]]+?)\s*\]", re.IGNORECASE)
STICKER_TAG_RE = re.compile(r"\[sticker:\s*([^\]]+?)\s*\]", re.IGNORECASE)
# Persistent memory tags (see memory.py). "[remember: fact]" stores a durable
# fact about the current speaker; "[remember: Name | fact]" targets someone else
# in the room. "[forget: ...]" drops matching facts the same way.
REMEMBER_TAG_RE = re.compile(r"\[remember:\s*([^\]]+?)\s*\]", re.IGNORECASE)
FORGET_TAG_RE = re.compile(r"\[forget:\s*([^\]]+?)\s*\]", re.IGNORECASE)
EMOJI_SHORTCODE_RE = re.compile(r":([a-zA-Z0-9_]{2,32}):")
# Custom emoji as they appear in raw message content: <:name:id> / <a:name:id>.
CUSTOM_EMOJI_RE = re.compile(r"<(a)?:(\w+):(\d+)>")

# Zamalko's out-of-character height control. ONLY this exact account may use it,
# and ONLY when the message is just the bare command, e.g. "$ set_molly_height_cm(30)".
# It retunes Molly's size/mood for the channel in the moment — see molly_heights.
# Gate on the Discord *handle* (message.author.name), which is globally unique and
# can't be spoofed — NOT the per-server nickname, which anyone could copy. Override
# via the HEIGHT_CONTROLLER env var if the controlling account ever changes.
HEIGHT_CONTROLLER = os.environ.get("HEIGHT_CONTROLLER", "ca11mebucky").lower()
HEIGHT_CMD_RE = re.compile(r"^\$\s*set_molly_height_cm\(\s*(\d+)\s*\)\s*$")
# Her neutral baseline. Setting her to exactly this CLEARS the override, handing
# height control back to her emotions (pure persona behaviour).
BASELINE_HEIGHT_CM = 140

# Vision: incoming images Molly can actually see (Claude-supported formats only).
MAX_IMAGES = 8  # per message, to bound request size/cost
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
SUPPORTED_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
# Lazily-created shared HTTP session for Klipy GIF lookups.
_http_session: aiohttp.ClientSession | None = None
# Per-channel timestamp (monotonic) of the last GIF, for the cooldown above.
last_gif_at: dict[int, float] = {}
# Per-channel current height (cm) set by Zamalko's control command. Held in
# memory only and NEVER written to history, so it colours how Molly acts in the
# moment without becoming something she "remembers" or repeats. Resets on
# restart, and another command (e.g. back to 140) overrides it.
molly_heights: dict[int, int] = {}
# Per-channel cooldown clock for replying to reactions on her own messages.
last_reaction_reply_at: dict[int, float] = {}

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

# Recent human speakers per channel, mapping Discord user id -> the display name
# we last showed Molly for them. Drives two memory things: which people's stored
# facts to inject this turn, and resolving a "[remember: Name | ...]" target back
# to a real user id. Bounded so it stays a rolling window of the active room.
MAX_TRACKED_SPEAKERS = 8
recent_speakers: dict[int, "OrderedDict[int, str]"] = {}
# Cache of (guild_id, user_id) -> per-server display name, so resolving the
# nickname for a backfilled author costs at most one REST fetch per person.
nick_cache: dict[tuple[int, int], str] = {}

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


def parse_actions(
    reply_text: str,
) -> tuple[str, list[str], str | None, list[str], list[str], list[str]]:
    """Pull Molly's inline action tags out of her reply.

    Returns (clean_text, reactions, gif_query, sticker_names, remembers,
    forgets): the message the users actually see, up to MAX_REACTIONS emoji to
    react with, an optional GIF search query (the last [gif:...] tag wins if she
    emits more than one), up to MAX_STICKERS server-sticker names to attach, and
    the raw bodies of any [remember:...] / [forget:...] memory tags (resolved to
    a target user and persisted later, by process_memory_ops).
    """
    reactions = [m.strip() for m in REACT_TAG_RE.findall(reply_text)][:MAX_REACTIONS]
    gif_matches = GIF_TAG_RE.findall(reply_text)
    gif_query = gif_matches[-1].strip() if gif_matches else None
    sticker_names = [m.strip() for m in STICKER_TAG_RE.findall(reply_text)][:MAX_STICKERS]
    remembers = [m.strip() for m in REMEMBER_TAG_RE.findall(reply_text)]
    forgets = [m.strip() for m in FORGET_TAG_RE.findall(reply_text)]

    clean_text = FORGET_TAG_RE.sub(
        "",
        REMEMBER_TAG_RE.sub(
            "", STICKER_TAG_RE.sub("", GIF_TAG_RE.sub("", REACT_TAG_RE.sub("", reply_text)))
        ),
    )
    # Pulling a tag from mid-sentence can leave a double space; collapse runs of
    # spaces/tabs first.
    clean_text = re.sub(r"[ \t]{2,}", " ", clean_text)
    # Then squash blank lines down to a single newline. The model likes to
    # double-space its lines (markdown paragraph habit), and stripping a tag off
    # its own line leaves an empty line behind — both render as an ugly vertical
    # gap in Discord. A run of newlines (with any blank-line whitespace between)
    # becomes one newline; deliberate single line breaks are left alone.
    clean_text = re.sub(r"\n[ \t]*(?:\n[ \t]*)+", "\n", clean_text).strip()
    return clean_text, reactions, gif_query, sticker_names, remembers, forgets


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


def build_height_note(channel_id: int) -> str:
    """If Zamalko has set a height for this channel, tell Molly her size right now.

    Returns "" when no override is active. The wording leans on the persona's own
    height/mood mapping (WHO YOU ARE) and the "size is background, not a news
    ticker" rule, so she feels the size without reciting the number.
    """
    height = molly_heights.get(channel_id)
    if height is None:
        return ""
    return (
        f"\n\nYOUR HEIGHT RIGHT NOW IS LOCKED AT {height}cm.\n"
        f"- This is a hard fact about you for this whole conversation, not a passing "
        f"mood: you ARE {height}cm tall right now (your neutral baseline is "
        f"{BASELINE_HEIGHT_CM}cm). It stays EXACTLY this until it is changed again — you "
        "do NOT grow or shrink off it on your own, no matter what anyone says or how the "
        "chat makes you feel.\n"
        f"- If anyone asks how tall you are, the answer is exactly {height}cm. Never give "
        "a different number, never claim you're back to baseline, never forget it.\n"
        "- Let it colour your personality the way WHO YOU ARE describes: shorter = more "
        "small, shy, fragile, easily-flustered; taller = more confident, bold, cocky, "
        f"reckless, in-control. The further from {BASELINE_HEIGHT_CM}cm, the harder it "
        "hits — but the number itself does not move.\n"
        "- Don't recite the number out of nowhere like a status readout — just BE this "
        "size and let it shape how you talk; when it's actually relevant, own it."
    )


async def build_memory_note(guild: "discord.Guild | None", channel_id: int) -> str:
    """Inject what Molly durably remembers about the people active in a channel.

    Pulls stored facts for the recent speakers (see recent_speakers) and lays
    them out per person so she can weave them in. Returns "" when memory is off,
    we're not in a guild, or nobody present has any saved facts.
    """
    if guild is None or not memory.enabled():
        return ""
    speakers = recent_speakers.get(channel_id)
    if not speakers:
        return ""
    user_ids = list(speakers.keys())
    data = await memory.facts_for(guild.id, user_ids)
    if not data:
        return ""

    lines = [
        "",
        "WHAT YOU REMEMBER ABOUT THE PEOPLE HERE:",
        "- These are durable facts from past chats — treat them as true and let "
        "them show that you know these people, but weave them in naturally; do "
        "NOT recite them back like a list or announce that you remembered.",
    ]
    for user_id in user_ids:  # oldest-active first, current speaker last
        entry = data.get(user_id)
        if not entry or not entry[1]:
            continue
        name, facts = entry
        lines.append(f"- {name}: {'; '.join(facts)}")
    if len(lines) <= 3:
        return ""
    return "\n".join(lines)


def _split_memory_target(raw: str) -> tuple[str | None, str]:
    """Split a memory tag body into (target_name, text).

    "Name | the fact" targets someone specific; a body with no "|" targets the
    current speaker, so target_name is None.
    """
    if "|" in raw:
        left, right = raw.split("|", 1)
        return (left.strip() or None), right.strip()
    return None, raw.strip()


async def process_memory_ops(
    message: discord.Message,
    memory_subject,
    remembers: list[str],
    forgets: list[str],
) -> None:
    """Persist Molly's [remember:]/[forget:] tags after a reply is sent.

    A bare tag is about `memory_subject` (the current speaker); "Name | text"
    targets another recent speaker, resolved by display name back to their real
    user id. Unresolvable targets are skipped rather than guessed. No-ops unless
    memory is enabled and we're in a guild (per-server scope needs one).
    """
    guild = message.guild
    if guild is None or not memory.enabled() or (not remembers and not forgets):
        return
    channel_id = message.channel.id

    def resolve(target_name: str | None) -> tuple[int, str] | None:
        if target_name:
            wanted = target_name.lstrip("@").strip().lower()
            for uid, label in recent_speakers.get(channel_id, {}).items():
                if label.lower() == wanted:
                    return uid, label
            return None
        if memory_subject is not None:
            return memory_subject.id, member_display_name(guild, memory_subject)
        return None

    for raw in remembers:
        target_name, fact = _split_memory_target(raw)
        resolved = resolve(target_name)
        if resolved and fact:
            uid, label = resolved
            await memory.remember(guild.id, uid, label, fact)
    for raw in forgets:
        target_name, needle = _split_memory_target(raw)
        resolved = resolve(target_name)
        if resolved and needle:
            uid, _label = resolved
            await memory.forget(guild.id, uid, needle)


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


async def resolve_member_name(guild: "discord.Guild | None", author) -> str:
    """Per-server display name for `author`, resolved consistently everywhere.

    member_display_name falls back to the GLOBAL name whenever the author isn't
    in the member cache — which happens for backfilled (REST channel.history)
    authors but not for live gateway messages. That made the same person show as
    their nickname live and their global handle in history (e.g. "zamalko" vs
    "bucky"), confusing Molly and any name-keyed bookkeeping. This resolves the
    nickname the same in both paths by falling back to a one-off REST fetch
    (cached per person), so only people who have actually LEFT the guild ever
    drop to the global name.
    """
    if guild is None:
        return author.display_name
    member = guild.get_member(author.id)
    if member is not None:
        nick_cache[(guild.id, author.id)] = member.display_name
        return member.display_name
    cached = nick_cache.get((guild.id, author.id))
    if cached is not None:
        return cached
    try:
        member = await guild.fetch_member(author.id)
    except Exception:  # noqa: BLE001 — left/uncached member just uses the global name
        member = None
    name = member.display_name if member is not None else author.display_name
    nick_cache[(guild.id, author.id)] = name
    return name


def track_speaker(channel_id: int, user_id: int, name: str) -> None:
    """Record a human speaker as recently-active in a channel (newest last).

    Feeds both the per-user memory injection and "[remember: Name | ...]" target
    resolution. Bounded to MAX_TRACKED_SPEAKERS so it's a rolling window.
    """
    speakers = recent_speakers.get(channel_id)
    if speakers is None:
        speakers = recent_speakers[channel_id] = OrderedDict()
    speakers[user_id] = name
    speakers.move_to_end(user_id)
    while len(speakers) > MAX_TRACKED_SPEAKERS:
        speakers.popitem(last=False)


def message_to_turn(msg: discord.Message, name: str | None = None) -> dict | None:
    """Convert a Discord message into a history turn, or None if it's empty/skip.

    Molly's own messages become assistant turns; everyone else's become
    name-tagged user turns. Other bots are skipped. Stickers and image
    attachments are noted in text so the context reads sensibly. `name`, when
    given, is the pre-resolved speaker label (see resolve_member_name).
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
    speaker = name if name is not None else member_display_name(msg.guild, msg.author)
    return {"role": "user", "content": f"{speaker}: {text}", "id": msg.id}


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

    # Resolve each human author's nickname (with the REST fallback) so backfilled
    # turns are labelled exactly like live ones, and note them as recent speakers
    # so their stored memory is available the moment Molly is dropped back in.
    turns: list[dict] = []
    for msg in reversed(recent):
        name = None
        if not msg.author.bot:
            name = await resolve_member_name(msg.guild, msg.author)
        turn = message_to_turn(msg, name=name)
        if turn is None:
            continue
        turns.append(turn)
        if name is not None:
            track_speaker(channel.id, msg.author.id, name)
    history.clear()
    history.extend(turns)


async def replied_to_author_id(message: discord.Message) -> int | None:
    """The author id of the message this one is a Discord reply to, or None.

    Lets Molly stay out of replies clearly aimed at someone else. Prefers the
    already-resolved reference, falls back to a light fetch, and treats any
    failure (deleted/uncached/no permission) as "unknown" by returning None —
    in which case she just handles the message normally.
    """
    ref = message.reference
    if ref is None:
        return None
    resolved = ref.resolved
    if isinstance(resolved, discord.Message):
        return resolved.author.id
    if isinstance(resolved, discord.DeletedReferencedMessage) or ref.message_id is None:
        return None
    try:
        original = await message.channel.fetch_message(ref.message_id)
    except Exception:  # noqa: BLE001 — never let reply-detection block a response
        return None
    return original.author.id


@client.event
async def on_ready() -> None:
    # Bring up persistent memory (idempotent — on_ready can fire on reconnects).
    # If MySQL isn't configured/reachable it just stays off and the bot runs on.
    await memory.init()
    print(f"Logged in as {client.user} — listening in channel {CHANNEL_ID}")


@client.event
async def on_message(message: discord.Message) -> None:
    # Never respond to ourselves or any other bot.
    if message.author.bot:
        return

    # Zamalko's height control. Checked before the home-channel/mention gate so
    # it works wherever he posts it — but ONLY from his exact account and ONLY
    # when the message is just the bare "$ set_molly_height_cm(N)" command.
    if message.author.name.lower() == HEIGHT_CONTROLLER:
        cmd = HEIGHT_CMD_RE.match(message.content.strip())
        if cmd:
            async with get_channel_lock(message.channel.id):
                await apply_height_shift(message, int(cmd.group(1)))
            return

    # Is Molly explicitly @-mentioned here? A direct ping or a reply-with-ping to
    # her both land in message.mentions; @everyone/@here deliberately do NOT, so
    # she isn't dragged into mass pings.
    is_home = message.channel.id == CHANNEL_ID
    mentioned = client.user is not None and any(
        user.id == client.user.id for user in message.mentions
    )

    # CHANNEL_ID is her home channel (she replies to everything there); anywhere
    # else she only speaks up when explicitly @-mentioned.
    if not is_home and not mentioned:
        return

    # Even at home, stay out of a Discord reply aimed at *another person* — two
    # people talking to each other — unless she was actually pinged. A reply to
    # her own message, or one to Molly, still gets a response.
    if is_home and not mentioned:
        target_id = await replied_to_author_id(message)
        if (
            target_id is not None
            and target_id != message.author.id
            and (client.user is None or target_id != client.user.id)
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


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    """When someone reacts to one of Molly's messages, she reacts back to it.

    Raw (not cached) so it still fires for messages from before this run. Scoped
    to channels she's active in, rate-limited per channel, and ignores her own
    reactions and other bots so she doesn't spam or talk to herself.
    """
    if client.user is None or payload.user_id == client.user.id:
        return
    # Only bother in channels she actually talks in (home, or any she's been
    # pulled into), so we don't fetch messages for reactions all over the server.
    if payload.channel_id != CHANNEL_ID and payload.channel_id not in primed_channels:
        return
    # Other bots reacting shouldn't poke her.
    if payload.member is not None and payload.member.bot:
        return
    # Cheap cooldown check first, so a reaction storm doesn't fetch on every hit.
    now = time.monotonic()
    if now - last_reaction_reply_at.get(payload.channel_id, 0.0) < REACTION_REPLY_COOLDOWN:
        return

    channel = client.get_channel(payload.channel_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception:  # noqa: BLE001 — a missing/uncached message just means skip
        return
    # She only cares about reactions on HER OWN messages.
    if message.author.id != client.user.id:
        return

    last_reaction_reply_at[payload.channel_id] = now
    async with get_channel_lock(payload.channel_id):
        await respond_to_reaction(message, payload)


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

    speaker = await resolve_member_name(message.guild, message.author)
    history = get_history(message.channel.id)

    # Prime with the channel's recent messages so she knows what's going on. Her
    # home channel is primed once (she then sees everything live); other channels
    # — where she's only present when @-mentioned — are re-primed every time so
    # she catches up on whatever was said while she was away.
    is_home = message.channel.id == CHANNEL_ID
    if not is_home or message.channel.id not in primed_channels:
        await prime_channel_context(message.channel, history, before=message)
        primed_channels.add(message.channel.id)

    # Mark the speaker active *after* priming so they're the most-recent entry —
    # drives memory injection and "[remember: Name | ...]" target resolution.
    track_speaker(message.channel.id, message.author.id, speaker)

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

    system_prompt = (
        MOLLY_SYSTEM_PROMPT
        + build_emoji_sticker_note(message.guild)
        + build_height_note(message.channel.id)
        + await build_memory_note(message.guild, message.channel.id)
    )
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

    # Perform her reactions/GIF/stickers and post the message, then record what
    # she actually did so next turn's history stays coherent. Memory tags bind to
    # the person she's replying to (the message author).
    history_note = await deliver_reply(
        message, reply_text, memory_subject=message.author
    )
    history.append({"role": "assistant", "content": history_note})


async def deliver_reply(
    message: discord.Message, reply_text: str, *, memory_subject=None
) -> str:
    """Act on Molly's raw reply and post it; return the text to store in history.

    Strips her inline action tags, performs the reactions/GIF/stickers they ask
    for, persists any [remember:]/[forget:] memory tags (bound to memory_subject
    by default), renders custom-emoji shortcodes, and sends the visible message
    (chunked to Discord's limit). Returns the cleaned text — or a short
    placeholder when she only reacted/giffed/stickered — for the caller to record
    (or ignore).
    """
    clean_text, reactions, gif_query, sticker_names, remembers, forgets = parse_actions(
        reply_text
    )
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

    history_note = clean_text or (
        "(sends a gif)" if gif_url else "(sends a sticker)" if stickers else "(reacts)"
    )

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

    # Persist any memory tags last, so a slow/failed DB write can never hold up
    # or break the visible reply (process_memory_ops also swallows its own errors).
    await process_memory_ops(message, memory_subject, remembers, forgets)

    return history_note


async def apply_height_shift(message: discord.Message, height: int) -> None:
    """Handle Zamalko's "$ set_molly_height_cm(N)" command for this channel.

    Sets the channel's current height (which then colours every later reply via
    build_height_note) and has Molly react to the change in the moment. The
    command and her reaction are deliberately NOT written to history, so this
    tweaks her behaviour without becoming something she "remembers" or repeats.
    The caller holds the channel lock.
    """
    channel_id = message.channel.id
    # Setting her to the neutral baseline clears the override entirely, so she goes
    # back to shifting purely on emotion (no pinned size note). Any other value
    # becomes her current size — which she can still drift from naturally.
    if height == BASELINE_HEIGHT_CM:
        molly_heights.pop(channel_id, None)
    else:
        molly_heights[channel_id] = height
    history = get_history(channel_id)

    # Catch up on the room so her reaction fits the conversation, exactly like a
    # normal turn (home channel primed once, other channels every time).
    is_home = channel_id == CHANNEL_ID
    if not is_home or channel_id not in primed_channels:
        await prime_channel_context(message.channel, history, before=message)
        primed_channels.add(channel_id)

    # A transient cue for THIS request only — never appended to history, so the
    # shift stays out of her memory. build_height_note already states the size;
    # this just tells her it happened right now so she reacts to the jolt.
    cue = (
        f"(You suddenly feel your body change — right this second you're {height}cm "
        "tall. React to the shift in character, quick and natural. Don't mention "
        "commands, numbers, or that anyone 'set' your height.)"
    )
    request_messages = build_request_messages(
        [*history, {"role": "user", "content": cue}]
    )
    system_prompt = (
        MOLLY_SYSTEM_PROMPT
        + build_emoji_sticker_note(message.guild)
        + build_height_note(channel_id)
        + await build_memory_note(message.guild, channel_id)
    )
    try:
        async with message.channel.typing():
            response = await anthropic_client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=request_messages,
            )
    except Exception as exc:  # noqa: BLE001 — surface API/network failure, stay alive
        print(f"Anthropic API error (height shift): {exc}")
        return

    reply_text = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    # Deliver her reaction but DON'T record it — keeps the shift out of memory.
    # No memory_subject: a height jolt isn't the moment to save facts about anyone.
    await deliver_reply(message, reply_text)


async def respond_to_reaction(
    message: discord.Message, payload: discord.RawReactionActionEvent
) -> None:
    """Have Molly react in character to an emoji-reaction on her own message.

    `message` is her own message that got reacted to; the caller holds the
    channel lock and has already enforced the cooldown. The exchange goes into
    history so it stays coherent with the rest of the conversation.
    """
    channel_id = message.channel.id
    history = get_history(channel_id)

    # Who reacted and with what. Custom emoji carry an id; unicode ones don't.
    reactor = (
        await resolve_member_name(message.guild, payload.member)
        if payload.member is not None
        else "someone"
    )
    if payload.member is not None:
        track_speaker(channel_id, payload.member.id, reactor)
    emoji = payload.emoji
    emoji_repr = f":{emoji.name}:" if emoji.id else emoji.name
    reacted_text = (message.clean_content or "").strip()
    snippet = f' "{reacted_text}"' if reacted_text else ""

    # Transient-style cue framed like the others; stored so the moment persists.
    cue = (
        f"({reactor} just reacted with {emoji_repr} to your message{snippet}. React "
        "to getting that little reaction — quick and in character, like someone "
        "clocking an emoji on something they said. Keep it light, sometimes barely "
        "a word, and don't make a big deal of it.)"
    )
    history.append({"role": "user", "content": cue, "id": payload.message_id})

    request_messages = build_request_messages(history)
    system_prompt = (
        MOLLY_SYSTEM_PROMPT
        + build_emoji_sticker_note(message.guild)
        + build_height_note(channel_id)
        + await build_memory_note(message.guild, channel_id)
    )
    try:
        async with message.channel.typing():
            response = await anthropic_client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=request_messages,
            )
    except Exception as exc:  # noqa: BLE001 — surface API/network failure, stay alive
        print(f"Anthropic API error (reaction reply): {exc}")
        return

    reply_text = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    # Memory tags here are about the person who reacted, not Molly (the author).
    history_note = await deliver_reply(
        message, reply_text, memory_subject=payload.member
    )
    history.append({"role": "assistant", "content": history_note})


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
