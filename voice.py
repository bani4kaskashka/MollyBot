"""voice.py — Molly's voice-channel presence: she joins a VC, speaks her replies
aloud with an ElevenLabs voice, and plays YouTube music on request.

Self-contained and OPTIONAL, exactly like memory.py. If ELEVENLABS_API_KEY /
ELEVENLABS_VOICE_ID aren't set — or the voice deps aren't installed (PyNaCl for
Discord voice, the ffmpeg binary for playback) — the TTS side degrades to a silent
no-op and the rest of the bot runs unchanged. Music needs yt-dlp + ffmpeg; without
yt-dlp it also no-ops. Her text replies always post regardless; only the spoken /
musical parts depend on the extras.

Two things flow through here:
  - `speak(voice_client, text)` — synthesize `text` through ElevenLabs and play it,
    serialized per guild. Called fire-and-forget from bot.py's deliver_reply. Before
    synthesis the text is cleaned for SPEECH (emoticons like ":3"/"xD" and unicode
    emoji stripped — they sound awful read literally; the TEXT reply keeps them).
  - `enqueue/skip/stop_music` — a tiny per-guild YouTube music player sharing the
    same single voice connection. While a song is actively playing, `speak` steps
    aside (she still replies in text) since one voice client can only play one
    source at a time.

Cost note (mirrors the GIF/web-search caveats in bot.py): ElevenLabs bills per
character. We default to the Flash model (~0.5 credits/char, low latency) and cap
each utterance at MAX_TTS_CHARS. Music is free (YouTube audio), just CPU/bandwidth.
"""

import asyncio
import io
import os
import re
from collections import deque

import aiohttp

# discord is always present in the deployed bot, but guard the import so voice.py
# can be imported (and no-op) in a bare environment, same spirit as the rest.
try:
    import discord
except ImportError:  # pragma: no cover — bot can't really run without it anyway
    discord = None  # type: ignore

# yt-dlp is only needed for music; without it, music no-ops but TTS still works.
try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None  # type: ignore

# --- Configuration (all optional; absence just disables the relevant feature) --
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
# The specific cloned/chosen voice for Molly. Without it we can't speak as her, so
# voice stays off even if the API key is present.
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID")
# Flash v2.5: lowest latency and ~half the credit cost per character — the right
# default for short chat lines. Override with eleven_multilingual_v2 for top
# quality at 1 credit/char if the voice matters more than the spend.
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")
# mp3 is fine as the wire format — ffmpeg decodes it to the PCM Discord wants.
TTS_OUTPUT_FORMAT = "mp3_44100_128"
# Hard ceiling on how much of one reply we voice. Keeps cost bounded and stops her
# reading a giant paragraph over everyone. She still POSTS the full text; only the
# spoken version is clipped.
MAX_TTS_CHARS = 600
# Safety valve so a stuck/never-finishing playback can't hold the per-guild lock
# forever (e.g. if the after-callback never fires). Generous — real lines are short.
PLAYBACK_TIMEOUT_SECONDS = 120
# Music playback level (0..1). A touch under full so it isn't ear-splitting and,
# once ducking lands, so her voice can sit over it.
MUSIC_VOLUME = 0.6

# --- Speech cleanup patterns --------------------------------------------------
# Custom-emoji markup (<:name:id>) and mention markup shouldn't be read aloud.
_CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
_MENTION_RE = re.compile(r"<@[!&]?\d+>|<#\d+>")
# Text emoticons / kaomoji that TTS mangles into gibberish when read literally
# (":3" -> "colon three", "xD" -> "ex dee"). Anchored to non-word boundaries so we
# don't chew into real words or clock times ("3:30"). The TEXT reply is untouched —
# this only ever runs on the copy we send to the voice.
_EMOTICON_RE = re.compile(
    r"(?<![\w])(?:"
    r"[:;=8][\-'^]?[)(\][dDpPoO3><|/\\]+"   # :) :3 :D :P :/ :| :> ;) 8) =D etc
    r"|[)(\]\[dDpP][\-']?[:;=8]"             # reversed  (:  D:
    r"|</?3+"                                 # <3  </3
    r"|[oO0>][._~][oO0<]"                     # o_o  O.O  >_<-ish
    r"|[tT;][_.][tT;]"                        # T_T  ;_;
    r"|\^[_.]?\^|>[_.]<|-_-|\._\."            # ^^  ^_^  >_<  -_-  ._.
    r"|[uUoO][wW][uUoO]"                       # uwu  owo
    r"|xD+|XD+|:'\)|:'\("                      # xD  :')  :'(
    r")(?![\w])",
)
# Broad unicode-emoji sweep (emoji, symbols, dingbats, flags, variation selectors).
_UNICODE_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"
    "\U00002300-\U000023FF"
    "]+",
    flags=re.UNICODE,
)

# yt-dlp: grab the best audio-only stream, no playlists, quiet. ytsearch1 turns a
# plain phrase into the top YouTube hit; a bare URL is used as-is.
_YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "cachedir": False,
}
# Reconnect flags matter for long YouTube streams; -vn drops any video.
_FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
_FFMPEG_OPTS = "-vn"

# Lazily-created shared HTTP session for ElevenLabs calls (separate from bot.py's
# Klipy session).
_http_session: "aiohttp.ClientSession | None" = None
# One playback lock per guild so TTS utterances play strictly one after another
# there, instead of raising "Already playing audio" or clipping each other.
_play_locks: dict[int, asyncio.Lock] = {}
# Per-guild music state: the upcoming queue and the track currently playing.
# _now_playing being set is the single "music is on" signal speak() checks.
_music_queues: dict[int, "deque[dict]"] = {}
_now_playing: dict[int, dict] = {}


# --- Enablement gates ---------------------------------------------------------
def enabled() -> bool:
    """True only if TTS can work: a key, a voice id, and discord present.

    ffmpeg/PyNaCl are checked implicitly at play/connect time (they raise there and
    are swallowed), so this gate is about configuration, matching memory.enabled().
    """
    return bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID and discord is not None)


def music_enabled() -> bool:
    """True if music can work (yt-dlp + discord present). Independent of TTS."""
    return bool(yt_dlp is not None and discord is not None)


def music_active(guild_id: int) -> bool:
    """True if a song is currently playing in this guild (so speak steps aside)."""
    return guild_id in _now_playing


def diagnose() -> str:
    """One-line startup health check of the voice runtime deps, for the deploy logs.

    discord.py refuses voice with a blanket "PyNaCl library needed in order to use
    voice" whenever `import nacl` fails for ANY reason — a missing package OR an
    installed-but-unimportable one (e.g. a shared-lib the slim deploy image lacks).
    This does the exact imports discord.py does and surfaces the REAL exception, plus
    whether ffmpeg is actually on PATH and yt-dlp/config are present. Purely
    diagnostic and always safe to call.
    """
    import shutil
    parts = []
    try:
        import nacl.secret  # noqa: F401
        import nacl.utils  # noqa: F401
        parts.append("PyNaCl:OK")
    except Exception as exc:  # noqa: BLE001 — we WANT the real error text
        parts.append(f"PyNaCl:FAIL({exc!r})")
    parts.append("ffmpeg:" + ("found" if shutil.which("ffmpeg") else "MISSING"))
    parts.append("yt-dlp:" + ("OK" if yt_dlp is not None else "MISSING"))
    parts.append("tts-config:" + ("set" if enabled() else "unset"))
    return "[voice] deps — " + " | ".join(parts)


# --- Shared helpers -----------------------------------------------------------
async def _get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


def _play_lock(guild_id: int) -> asyncio.Lock:
    lock = _play_locks.get(guild_id)
    if lock is None:
        lock = _play_locks[guild_id] = asyncio.Lock()
    return lock


def _clean_for_speech(text: str) -> str:
    """Prep Molly's visible reply text for TTS (only the spoken copy is affected).

    Strips custom-emoji/mention markup, text emoticons (":3", "xD", "^^"), and
    unicode emoji — all of which ElevenLabs reads as ugly literal noise — then
    collapses the whitespace/stray punctuation the removals leave behind. Returns
    "" if nothing speakable is left (e.g. a reply that was ONLY an emoticon), in
    which case we just don't speak that turn.
    """
    text = _CUSTOM_EMOJI_RE.sub(" ", text)
    text = _MENTION_RE.sub(" ", text)
    text = _EMOTICON_RE.sub(" ", text)
    text = _UNICODE_EMOJI_RE.sub(" ", text)
    # Tidy up: drop orphaned spaces before punctuation and any doubled punctuation
    # the stripping created, then collapse whitespace.
    text = re.sub(r"\s+([,.!?…])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    # A line that's now just leftover punctuation isn't worth speaking.
    if not re.search(r"[A-Za-z0-9]", text):
        return ""
    if len(text) > MAX_TTS_CHARS:
        # Trim to the cap, then back off to the last sentence/space so we don't cut
        # mid-word — better to end a beat early than choke on a fragment.
        clipped = text[:MAX_TTS_CHARS]
        cut = max(clipped.rfind(". "), clipped.rfind("! "), clipped.rfind("? "),
                  clipped.rfind(" "))
        text = clipped[: cut + 1].strip() if cut > 0 else clipped
    return text


# --- Connection ---------------------------------------------------------------
async def join(channel) -> bool:
    """Connect Molly to `channel` (a discord.VoiceChannel), or move if already in
    a VC in that guild. Returns True on success, False on failure.
    """
    if not enabled() or channel is None:
        return False
    guild = channel.guild
    vc = guild.voice_client
    try:
        if vc is not None and vc.is_connected():
            if vc.channel and vc.channel.id == channel.id:
                return True
            await vc.move_to(channel)
        else:
            await channel.connect()
        return True
    except Exception as exc:  # noqa: BLE001 — perms/deps issues shouldn't crash the command
        print(f"[voice] join failed: {exc}")
        return False


async def leave(guild) -> bool:
    """Disconnect Molly from `guild`'s voice channel. Returns True if she was in one.

    Also clears any music state for the guild so a later rejoin starts clean.
    """
    vc = getattr(guild, "voice_client", None)
    if vc is None:
        return False
    _music_queues.pop(guild.id, None)
    _now_playing.pop(guild.id, None)
    try:
        await vc.disconnect(force=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[voice] leave failed: {exc}")
    return True


# --- Text-to-speech -----------------------------------------------------------
async def synthesize(text: str) -> "bytes | None":
    """Return ElevenLabs mp3 bytes for `text`, or None on any failure.

    Failures (bad key, quota exhausted, network) are logged and swallowed so a TTS
    hiccup never breaks the text reply that already went out.
    """
    if not enabled():
        return None
    session = await _get_session()
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    params = {"output_format": TTS_OUTPUT_FORMAT}
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    body = {"text": text, "model_id": ELEVENLABS_MODEL}
    try:
        async with session.post(
            url, params=params, headers=headers, json=body,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                detail = (await resp.text())[:300]
                print(f"[voice] ElevenLabs TTS {resp.status}: {detail}")
                return None
            return await resp.read()
    except Exception as exc:  # noqa: BLE001 — a TTS failure must not break a reply
        print(f"[voice] TTS request failed: {exc}")
        return None


async def speak(voice_client, text: str) -> None:
    """Synthesize `text` and play it into `voice_client`, serialized per guild.

    No-ops silently if voice is disabled, we're not connected, music is currently
    playing (one source at a time — she stays in text then), or there's nothing
    speakable once emoticons/emoji are stripped. Playback is gated by a per-guild
    lock and awaited to completion so consecutive replies queue in order. Designed
    to be fire-and-forgotten by the caller (see bot.py) — it never raises.
    """
    if not enabled() or voice_client is None:
        return
    if discord is None or not voice_client.is_connected():
        return
    guild = getattr(voice_client, "guild", None)
    guild_id = getattr(guild, "id", 0)
    # If music's on, she doesn't talk over it (single audio source). Checked up
    # front to save the ElevenLabs call, and again after synth in case it just started.
    if music_active(guild_id):
        return
    speech = _clean_for_speech(text)
    if not speech:
        return
    audio = await synthesize(speech)
    if not audio:
        return

    async with _play_lock(guild_id):
        # Re-check: the owner may have run /mollyleave, or a song may have started,
        # while we were synthesizing.
        if not voice_client.is_connected() or music_active(guild_id):
            return
        loop = asyncio.get_running_loop()
        done = asyncio.Event()

        def _after(err) -> None:
            if err:
                print(f"[voice] playback error: {err}")
            loop.call_soon_threadsafe(done.set)

        try:
            source = discord.FFmpegPCMAudio(io.BytesIO(audio), pipe=True)
            voice_client.play(source, after=_after)
        except Exception as exc:  # noqa: BLE001 — missing ffmpeg / already playing / etc.
            print(f"[voice] play failed: {exc}")
            return
        try:
            await asyncio.wait_for(done.wait(), timeout=PLAYBACK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            print("[voice] playback timed out; moving on")
            try:
                voice_client.stop()
            except Exception:  # noqa: BLE001
                pass


# --- YouTube music ------------------------------------------------------------
def _resolve_blocking(query: str) -> "dict | None":
    """yt-dlp lookup (BLOCKING — always call via run_in_executor).

    Turns a phrase into the top YouTube hit or reads a given link, and returns the
    direct audio stream url + title, or None if nothing usable came back.
    """
    is_url = query.strip().lower().startswith(("http://", "https://"))
    target = query if is_url else f"ytsearch1:{query}"
    try:
        with yt_dlp.YoutubeDL(_YTDL_OPTS) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[voice] yt-dlp lookup failed for {query!r}: {exc}")
        return None
    if info is None:
        return None
    if "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return None
        info = entries[0]
    stream_url = info.get("url")
    if not stream_url:
        return None
    return {
        "title": info.get("title") or "something",
        "stream_url": stream_url,
        "webpage": info.get("webpage_url"),
    }


async def enqueue(voice_client, query: str) -> "dict | None":
    """Resolve `query` and add it to the guild's music queue, starting it if idle.

    Returns the resolved track dict (title etc.) or None on failure. Safe to
    fire-and-forget from bot.py. No-ops if music isn't available or she isn't
    actually connected to a voice channel.
    """
    if not music_enabled() or voice_client is None or not voice_client.is_connected():
        return None
    loop = asyncio.get_running_loop()
    track = await loop.run_in_executor(None, _resolve_blocking, query)
    if not track:
        return None
    guild_id = voice_client.guild.id
    queue = _music_queues.setdefault(guild_id, deque())
    queue.append(track)
    # Start playback if nothing musical is going yet. (If she's mid-TTS, _start_next
    # will preempt it — a music request is a clear "play now".)
    if guild_id not in _now_playing:
        await _start_next(voice_client)
    return track


async def _start_next(voice_client) -> None:
    """Play the next queued track, or clear music state if the queue's empty.

    Advances on song end via the ffmpeg after-callback (marshalled back onto the
    loop). Runs entirely on the loop thread, so the shared queue/now-playing dicts
    are mutated without extra locking.
    """
    guild_id = voice_client.guild.id
    queue = _music_queues.get(guild_id)
    if not queue or not voice_client.is_connected():
        _now_playing.pop(guild_id, None)
        return
    track = queue.popleft()
    _now_playing[guild_id] = track
    loop = asyncio.get_running_loop()

    def _after(err) -> None:
        if err:
            print(f"[voice] music playback error: {err}")
        # Advance to the next track back on the event loop.
        asyncio.run_coroutine_threadsafe(_start_next(voice_client), loop)

    try:
        source = discord.FFmpegPCMAudio(
            track["stream_url"], before_options=_FFMPEG_BEFORE, options=_FFMPEG_OPTS
        )
        source = discord.PCMVolumeTransformer(source, volume=MUSIC_VOLUME)
        # A music request preempts any in-progress TTS (one source at a time).
        if voice_client.is_playing():
            voice_client.stop()
        voice_client.play(source, after=_after)
    except Exception as exc:  # noqa: BLE001 — a bad stream must not wedge the player
        print(f"[voice] music play failed: {exc}")
        _now_playing.pop(guild_id, None)


def skip(voice_client) -> bool:
    """Skip the current song (the after-callback advances to the next). Returns
    True if something was playing to skip.
    """
    if voice_client is None or voice_client.guild.id not in _now_playing:
        return False
    try:
        voice_client.stop()  # fires _after → _start_next → next track (or clear)
    except Exception as exc:  # noqa: BLE001
        print(f"[voice] skip failed: {exc}")
    return True


def stop_music(voice_client) -> bool:
    """Stop the music and clear the whole queue. Returns True if music was on."""
    if voice_client is None:
        return False
    guild_id = voice_client.guild.id
    was_on = guild_id in _now_playing
    _music_queues.pop(guild_id, None)
    # Clear now_playing BEFORE stop() so the after-callback's _start_next sees an
    # empty queue and doesn't restart anything.
    _now_playing.pop(guild_id, None)
    try:
        if voice_client.is_playing():
            voice_client.stop()
    except Exception as exc:  # noqa: BLE001
        print(f"[voice] stop failed: {exc}")
    return was_on
