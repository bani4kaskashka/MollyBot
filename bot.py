"""MollyBot — a Discord bot that replies in character as Molly Simpson.

In her home channel (CHANNEL_ID) — and any thread inside it — Molly replies to
every message; in any other channel she only responds when she is @-mentioned.
History is kept per channel
(each human turn tagged with the speaker's name so she can tell people apart),
and replies are generated with the Anthropic API.
"""

import asyncio
import os
import random
import re
import time
from collections import OrderedDict, deque
from datetime import datetime

import aiohttp
import discord
from discord import app_commands
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

MODEL = "claude-haiku-4-5"  # cost choice: ~3x cheaper in/out than Sonnet 4.6
MAX_TOKENS = 1000  # a hard ceiling, not a target — the prompt keeps replies short
# Shared history is spread across everyone in the channel. Kept moderate because
# every message re-sends the whole window as input — the dominant cost driver —
# so this is a direct lever on spend (was 60; halved to cut input tokens).
HISTORY_LIMIT = 30  # max messages (humans + Molly) retained per channel
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
# Floor between Molly opening private threads in a channel, so "make a thread" can't
# be spam-summoned (every thread also pings the owner, so this matters).
THREAD_COOLDOWN_SECONDS = 30
THREAD_NAME_MAX = 90  # Discord caps thread names at 100; leave margin
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
# Private-thread tags. "[thread: title]" opens a private thread off her channel
# (owner + requester are pulled in automatically); "[thread: title | Bob, Carol]"
# also invites the named people. "[invite: Bob]" adds people to the thread she's
# already in. Names are resolved leniently (see resolve_invitee).
THREAD_TAG_RE = re.compile(r"\[thread:\s*([^\]]+?)\s*\]", re.IGNORECASE)
INVITE_TAG_RE = re.compile(r"\[invite:\s*([^\]]+?)\s*\]", re.IGNORECASE)
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
# Molly's creator. When the person CURRENTLY talking to her uses this handle, she
# knows she's speaking to Zamalko himself and shouldn't refer to him in the third
# person ("Zamalko hasn't told me") as if he weren't right there. Gated on the
# handle (message.author.name — globally unique, unspoofable), like HEIGHT_CONTROLLER;
# override via MOLLY_CREATOR if his account differs.
MOLLY_CREATOR = os.environ.get("MOLLY_CREATOR", "zamalkogts").lower()
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
# Per-channel timestamp (monotonic) of the last private thread Molly opened, for
# the THREAD_COOLDOWN_SECONDS floor.
last_thread_at: dict[int, float] = {}
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
# Per-channel "fresh start" line set by /mollynewchat: Molly ignores everything in
# that channel from before this timestamp. Clearing her live history isn't enough
# on its own — context priming would just backfill the old messages again — so
# prime_channel_context also drops anything at or before this boundary.
fresh_start_at: dict[int, datetime] = {}
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
# Cache of guild_id -> the owner's user id (the HEIGHT_CONTROLLER handle), so we
# don't rescan the member list every time a private thread needs the owner added.
owner_member_cache: dict[int, int] = {}

intents = discord.Intents.default()
intents.message_content = True
# Privileged "Server Members" intent — needed so the guild member cache is
# populated and we can resolve per-server nicknames for authors of messages
# fetched via channel.history() (REST history carries no inline member data).
# Must also be enabled in the Discord Developer Portal or the bot won't start.
intents.members = True
client = discord.Client(intents=intents)
# Slash commands hang off a command tree. We only ever sync it to Molly's own
# guild (in on_ready), so the commands appear in that server and nowhere else.
tree = app_commands.CommandTree(client)


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
) -> tuple[
    str, list[str], str | None, list[str], list[str], list[str],
    tuple[str, list[str]] | None, list[str],
]:
    """Pull Molly's inline action tags out of her reply.

    Returns (clean_text, reactions, gif_query, sticker_names, remembers, forgets,
    thread_request, invites): the message the users actually see, up to
    MAX_REACTIONS emoji to react with, an optional GIF search query (the last
    [gif:...] tag wins if she emits more than one), up to MAX_STICKERS
    server-sticker names to attach, the raw bodies of any [remember:...] /
    [forget:...] memory tags, an optional (title, [extra invitee names]) private
    thread to open (the first [thread:...] tag wins), and any [invite:...] names
    to add to the current thread. Threads/invites are resolved and performed
    later by process_thread_ops.
    """
    reactions = [m.strip() for m in REACT_TAG_RE.findall(reply_text)][:MAX_REACTIONS]
    gif_matches = GIF_TAG_RE.findall(reply_text)
    gif_query = gif_matches[-1].strip() if gif_matches else None
    sticker_names = [m.strip() for m in STICKER_TAG_RE.findall(reply_text)][:MAX_STICKERS]
    remembers = [m.strip() for m in REMEMBER_TAG_RE.findall(reply_text)]
    forgets = [m.strip() for m in FORGET_TAG_RE.findall(reply_text)]

    # First [thread:...] wins. Body is "title" or "title | name1, name2" — the
    # part after the pipe is extra people to invite beyond owner + requester.
    thread_request: tuple[str, list[str]] | None = None
    thread_matches = THREAD_TAG_RE.findall(reply_text)
    if thread_matches:
        body = thread_matches[0].strip()
        title, _, names_part = body.partition("|")
        extra = [n.strip() for n in re.split(r"[;,]", names_part) if n.strip()]
        thread_request = (title.strip(), extra)
    # Every [invite:...] adds its (comma/semicolon-separated) names to the thread.
    invites: list[str] = []
    for body in INVITE_TAG_RE.findall(reply_text):
        invites.extend(n.strip() for n in re.split(r"[;,]", body) if n.strip())

    clean_text = INVITE_TAG_RE.sub(
        "",
        THREAD_TAG_RE.sub(
            "",
            FORGET_TAG_RE.sub(
                "",
                REMEMBER_TAG_RE.sub(
                    "",
                    STICKER_TAG_RE.sub(
                        "", GIF_TAG_RE.sub("", REACT_TAG_RE.sub("", reply_text))
                    ),
                ),
            ),
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
    return (
        clean_text, reactions, gif_query, sticker_names, remembers, forgets,
        thread_request, invites,
    )


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


def build_creator_note(author) -> str:
    """Tell Molly when her own creator is the one talking, so she addresses him
    directly instead of name-dropping "Zamalko" like an absent third party.

    Returns "" for everyone else. Gated on the handle so a copied nickname can't
    impersonate the creator (same reasoning as HEIGHT_CONTROLLER).
    """
    if getattr(author, "name", "").lower() != MOLLY_CREATOR:
        return ""
    return (
        "\n\nHEADS UP: the person talking to you RIGHT NOW is Zamalko himself — "
        "your actual creator, the one who makes the game. Do NOT talk about "
        '"Zamalko" in the third person, or say he hasn\'t told you things, as if '
        "he weren't here — he is literally who you're replying to. Talk straight "
        "to him: rib him that HE's the one keeping secrets, bug HIM about the "
        "release date, creation-teasing-its-creator energy. Stay casual and in "
        "character — don't get weird, formal, or worshippy about it."
    )


def build_system_blocks(
    guild: "discord.Guild | None", channel_id: int, memory_note: str, author=None
) -> list[dict]:
    """Assemble the system prompt as cacheable content blocks.

    The big, stable prefix — persona + this server's emoji/sticker list + any
    height override — goes in one block marked for prompt caching, so it's served
    from cache (~10% of input price) on repeat calls instead of being re-billed in
    full on every single message (the dominant cost). The volatile, per-speaker
    notes — whether the creator is talking, plus the per-turn memory note — sit
    AFTER the cache breakpoint, uncached, where they can't invalidate the prefix.

    Caching only actually kicks in once the cached prefix clears the model's
    minimum (~4096 tokens on Haiku 4.5, ~2048 on Sonnet 4.6); under that it
    harmlessly no-ops (no error, just nothing cached).
    """
    stable = (
        MOLLY_SYSTEM_PROMPT
        + build_emoji_sticker_note(guild)
        + build_height_note(channel_id)
    )
    blocks: list[dict] = [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}
    ]
    volatile = "".join(
        part for part in (build_creator_note(author), memory_note) if part
    )
    if volatile:
        blocks.append({"type": "text", "text": volatile})
    return blocks


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


def resolve_owner_member(guild: discord.Guild) -> "discord.Member | None":
    """The guild member whose handle is HEIGHT_CONTROLLER (the owner), or None.

    Every private thread must have the owner in it first, so we look them up by
    their globally-unique handle and cache the resulting user id per guild. Needs
    the members intent (on) so guild.members is populated; returns None if the
    owner isn't in this guild / isn't cached yet (we then just skip adding them).
    """
    uid = owner_member_cache.get(guild.id)
    if uid is not None:
        member = guild.get_member(uid)
        if member is not None:
            return member
    for member in guild.members:
        if member.name.lower() == HEIGHT_CONTROLLER:
            owner_member_cache[guild.id] = member.id
            return member
    return None


async def resolve_invitee(
    guild: discord.Guild, message: discord.Message, name: str
) -> "discord.Member | None":
    """Best-effort resolve a name Molly wants to invite to a real guild member.

    Robust by design — tries, in order: (1) an actual @mention in the triggering
    message that matches the name (most reliable, since the human pointed at them),
    (2) someone who's recently spoken in this channel, (3) a guild-member search by
    nick / display name / global name / handle, preferring an exact match and only
    accepting a partial one if it's unambiguous. Anything unknown or ambiguous
    returns None so we skip rather than invite the wrong person.
    """
    wanted = name.lstrip("@").strip().lower()
    if not wanted:
        return None

    def labels_of(m) -> list[str]:
        return [
            (getattr(m, "nick", None) or "").lower(),
            (getattr(m, "display_name", "") or "").lower(),
            (getattr(m, "global_name", None) or "").lower(),
            (getattr(m, "name", "") or "").lower(),
        ]

    # 1) The human actually @mentioned someone — match the name against them.
    for user in message.mentions:
        labels = labels_of(user)
        if any(lbl == wanted for lbl in labels) or any(
            lbl and wanted in lbl for lbl in labels
        ):
            member = guild.get_member(user.id)
            if member is not None:
                return member

    # 2) Someone who's recently spoken here (label we showed Molly == the name).
    for uid, label in recent_speakers.get(message.channel.id, {}).items():
        if label.lower() == wanted:
            member = guild.get_member(uid)
            if member is not None:
                return member

    # 3) Search the guild: exact match first, then a single unambiguous partial.
    exact: list[discord.Member] = []
    partial: list[discord.Member] = []
    for member in guild.members:
        if member.bot:
            continue
        labels = labels_of(member)
        if any(lbl == wanted for lbl in labels):
            exact.append(member)
        elif any(lbl and wanted in lbl for lbl in labels):
            partial.append(member)
    if len(exact) == 1:
        return exact[0]
    if not exact and len(partial) == 1:
        return partial[0]
    return None


def seed_thread_context(parent_id: int, thread_id: int) -> None:
    """Copy a channel's recent conversation into a freshly-made thread.

    So Molly carries on in the thread exactly where she left off — backend only,
    nothing is posted. Copies the history deque and the recent-speakers window
    (so stored memory still loads), and marks the thread primed so context
    backfill doesn't run against the empty thread.
    """
    parent_hist = histories.get(parent_id)
    if parent_hist:
        get_history(thread_id).extend(list(parent_hist))
    parent_speakers = recent_speakers.get(parent_id)
    if parent_speakers:
        recent_speakers[thread_id] = OrderedDict(parent_speakers)
    primed_channels.add(thread_id)


async def post_thread_opener(thread: discord.Thread, requester, invited_names: list[str]) -> None:
    """Have Molly post the first line in a freshly-opened private thread.

    The thread starts empty and she only speaks when spoken to, so without this it
    just sits silent after creation. One model call — fed the context we already
    seeded into the thread — produces a warm, in-character opener that picks up
    where the conversation was, like leading someone into a quieter room. It is
    NOT a memory dump or a recap list. Best-effort: a failure just leaves the
    thread empty, no worse than before, and never disturbs the main reply.
    """
    history = get_history(thread.id)
    who = ", ".join(n for n in invited_names if n) or "them"
    cue = (
        f"(You just brought {who} into a private thread, away from the main channel, "
        "because that was asked for. You're the first one here — open it with a warm, "
        "in-character line or two that picks up naturally right where you all just "
        "were, like walking into a quieter room together and carrying on. Keep it "
        "short and real — NOT a recap, a summary, or a list of what you remember.)"
    )
    history.append({"role": "user", "content": cue})
    try:
        request_messages = build_request_messages(history)
        system_prompt = build_system_blocks(
            thread.guild,
            thread.id,
            await build_memory_note(thread.guild, thread.id),
            author=requester,
        )
        async with thread.typing():
            response = await anthropic_client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=request_messages,
            )
    except Exception as exc:  # noqa: BLE001 — a failed opener must not break anything
        print(f"[thread] opener generation failed: {exc}")
        history.pop()  # drop the cue we added; nothing came of it
        return

    reply_text = "".join(b.text for b in response.content if b.type == "text").strip()
    # Strip any action tags she slipped in — in the thread we only want her words
    # (and this also stops a stray [thread:]/[invite:] in the opener from looping).
    clean_text = parse_actions(reply_text)[0]
    clean_text = resolve_emoji_markup(clean_text, thread.guild)
    if not clean_text:
        history.pop()
        return
    for i in range(0, len(clean_text), DISCORD_MAX_LEN):
        await thread.send(clean_text[i:i + DISCORD_MAX_LEN])
    history.append({"role": "assistant", "content": clean_text})


async def process_thread_ops(
    message: discord.Message,
    requester,
    thread_request: "tuple[str, list[str]] | None",
    invites: list[str],
) -> None:
    """Open private threads / add people, from Molly's [thread:]/[invite:] tags.

    Runs after the visible reply is sent and swallows its own errors, so a Discord
    hiccup can never break the message — same spirit as process_memory_ops.

    - [invite: name] adds people to the CURRENT thread (only meaningful when this
      message is already in a thread).
    - [thread: title | names] opens a PRIVATE thread off the home channel and
      pulls people in, ALWAYS in this order: the owner first, then the requester
      (the person Molly's replying to), then any explicitly-named extras. That
      covers both "can we talk in private" (owner + requester) and the owner's
      "make a thread with me and Bob" (owner + Bob). Capped by a per-channel
      cooldown. The new thread is seeded with the channel's recent context.
    """
    guild = message.guild
    if guild is None:
        return

    # Add people to the thread Molly is already in.
    if invites and isinstance(message.channel, discord.Thread):
        for name in invites:
            member = await resolve_invitee(guild, message, name)
            if member is None:
                continue
            try:
                await message.channel.add_user(member)
            except Exception as exc:  # noqa: BLE001 — one bad invite mustn't break others
                print(f"[thread] invite {name!r} failed: {exc}")

    if thread_request is None:
        return
    # Only open threads off the home channel (can't nest threads, and that's where
    # she lives) — a thread there counts as home so she'll talk freely in it.
    if message.channel.id != CHANNEL_ID or not isinstance(
        message.channel, discord.TextChannel
    ):
        return
    now = time.monotonic()
    if now - last_thread_at.get(message.channel.id, 0.0) < THREAD_COOLDOWN_SECONDS:
        return

    title, extra_names = thread_request
    title = (title or "molly chat")[:THREAD_NAME_MAX]
    try:
        thread = await message.channel.create_thread(
            name=title,
            type=discord.ChannelType.private_thread,
            invitable=False,
        )
    except Exception as exc:  # noqa: BLE001 — never let a failed create break the reply
        print(f"[thread] create failed: {exc}")
        return
    last_thread_at[message.channel.id] = now

    # Build the invite list in the required order, de-duped: owner, requester, extras.
    ordered: list = []
    seen: set[int] = set()

    def add(member) -> None:
        if member is not None and getattr(member, "id", None) not in seen:
            seen.add(member.id)
            ordered.append(member)

    owner = resolve_owner_member(guild)
    if owner is None:
        print("[thread] owner not found in guild — creating thread without them")
    add(owner)
    add(requester)
    for name in extra_names:
        add(await resolve_invitee(guild, message, name))

    # Carry her recent context into the thread so she continues seamlessly.
    seed_thread_context(message.channel.id, thread.id)

    # Add everyone (owner first). add_user on a private thread works because the
    # bot is the thread's creator; per-user failures are non-fatal.
    for member in ordered:
        try:
            await thread.add_user(member)
        except Exception as exc:  # noqa: BLE001
            print(f"[thread] add {member} failed: {exc}")

    # Now that the room exists and people are in it, Molly opens the conversation
    # so the thread isn't dead silent — picking up where they just were.
    await post_thread_opener(
        thread, requester, [getattr(m, "display_name", "") for m in ordered]
    )


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
    # Anything at or before a /mollynewchat boundary is skipped so a reset can't be
    # undone by backfill — she genuinely never sees what was said before the line.
    boundary = fresh_start_at.get(channel.id)
    turns: list[dict] = []
    for msg in reversed(recent):
        if boundary is not None and msg.created_at <= boundary:
            continue
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


@tree.command(
    name="mollynewchat",
    description="Wipe Molly's short-term memory of this channel so she starts fresh.",
)
async def mollynewchat(interaction: discord.Interaction) -> None:
    """Owner-only reset of a channel's short-term conversation.

    Clears the live history deque AND drops a fresh-start boundary so context
    priming won't backfill the old messages — from here she only sees what's said
    after this point. Her *durable* per-user memory (memory.py / MySQL) is left
    untouched: this forgets the conversation, not who people are.

    Gated to the same controller handle as the height command (globally unique,
    unspoofable). The reply is ephemeral, so only the runner sees it and the
    channel isn't spammed with a reset notice.
    """
    if interaction.user.name.lower() != HEIGHT_CONTROLLER:
        await interaction.response.send_message(
            "nah, only my person gets to do that one :p", ephemeral=True
        )
        return
    channel_id = interaction.channel_id
    if channel_id is not None:
        histories.pop(channel_id, None)
        recent_speakers.pop(channel_id, None)
        fresh_start_at[channel_id] = interaction.created_at
        # Mark primed so the home channel doesn't re-seed from before the line;
        # non-home channels re-prime but the boundary filters the old messages.
        primed_channels.add(channel_id)
    await interaction.response.send_message(
        "aight, clean slate :3 i don't remember anything we were just talking "
        "about — what's up?",
        ephemeral=True,
    )


@client.event
async def on_ready() -> None:
    # Bring up persistent memory (idempotent — on_ready can fire on reconnects).
    # If MySQL isn't configured/reachable it just stays off and the bot runs on.
    await memory.init()
    # Register the slash command on Molly's own guild only (instant, and it won't
    # show up in any other server). Derived from the home channel so there's no
    # extra GUILD_ID to configure. Best-effort: a sync failure must not stop her.
    try:
        home = client.get_channel(CHANNEL_ID)
        guild = getattr(home, "guild", None)
        if guild is not None:
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
            print(f"[slash] /mollynewchat synced to {guild.name}")
        else:
            print("[slash] home channel's guild not found — slash command not synced")
    except Exception as exc:  # noqa: BLE001 — slash sync must not crash startup
        print(f"[slash] command sync failed: {exc}")
    print(f"Logged in as {client.user} — listening in channel {CHANNEL_ID}")


def is_home_channel(channel) -> bool:
    """True for Molly's home channel AND any thread that lives inside it.

    A Discord thread has its own channel id (with parent_id pointing back to the
    channel it was created in), so a thread under the home channel would
    otherwise look like a foreign channel and she'd only answer when @-mentioned.
    Matching on parent_id lets her talk freely in threads off her channel, same
    as in the channel itself. getattr keeps it safe for channel types that have
    no parent_id (regular text channels return None and just fall through).
    """
    if channel.id == CHANNEL_ID:
        return True
    return getattr(channel, "parent_id", None) == CHANNEL_ID


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
    is_home = is_home_channel(message.channel)
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
    is_home = is_home_channel(message.channel)
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

    system_prompt = build_system_blocks(
        message.guild,
        message.channel.id,
        await build_memory_note(message.guild, message.channel.id),
        author=message.author,
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
    (
        clean_text, reactions, gif_query, sticker_names, remembers, forgets,
        thread_request, invites,
    ) = parse_actions(reply_text)
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

    # Private-thread actions, after the visible reply. The requester (who gets
    # pulled in after the owner) is the current human speaker — memory_subject —
    # which is message.author in the normal path. Swallows its own errors.
    await process_thread_ops(message, memory_subject, thread_request, invites)

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
    is_home = is_home_channel(message.channel)
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
    system_prompt = build_system_blocks(
        message.guild,
        channel_id,
        await build_memory_note(message.guild, channel_id),
        author=message.author,
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
    system_prompt = build_system_blocks(
        message.guild,
        channel_id,
        await build_memory_note(message.guild, channel_id),
        author=payload.member,
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
