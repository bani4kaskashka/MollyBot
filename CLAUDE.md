# CLAUDE.md

Guidance for working in this repo.

## What this is

MollyBot is a Discord bot that replies in character as **Molly Simpson**, a
character from the visual novel *Molly's Favourite Toy* by Zamalko. It listens
in a Discord server and generates replies with the Anthropic API.

The bot is these files:

- `bot.py` — all the Discord + Anthropic wiring.
- `molly_prompt.py` — `MOLLY_SYSTEM_PROMPT`, the character/persona and all the
  behavioral rules. **Tune Molly's personality and behavior here**, not in code.
- `memory.py` — the MySQL layer for persistent per-user memory (see below). Pure
  mechanics; degrades to a no-op if the database isn't configured/reachable.
- `voice.py` — the ElevenLabs TTS + Discord voice-playback layer (see the voice
  section below). Pure mechanics, same graceful-no-op spirit as `memory.py`:
  degrades to silent no-op if `ELEVENLABS_API_KEY`/`ELEVENLABS_VOICE_ID` aren't set
  or the ffmpeg/PyNaCl deps are missing.

`requirements.txt`, `Procfile`, `.python-version` (3.11) support deployment.

## Run / deploy

- Deployed on **Railway**, which **auto-deploys on every push to `main`**. There
  is no separate build step — push and it ships.
- Run locally: `python bot.py` (needs the env vars below in `.env`).
- After changing the Python files, sanity-check with
  `python -m py_compile bot.py molly_prompt.py memory.py` before committing —
  there are no unit tests. (`discord`/`aiomysql` may not be installed locally, so
  a full import can fail even when the code is fine; `py_compile`, and pyflakes
  if available, are the quick checks.)

## Configuration (environment variables)

- `DISCORD_TOKEN` — required.
- `ANTHROPIC_API_KEY` — required.
- `CHANNEL_ID` — required. Molly's **home channel** (see below).
- `KLIPY_API_KEY` — **optional**. Enables GIFs via the Klipy API (Google's Tenor
  API shut down in 2026). Without it, GIFs silently no-op and the bot still runs.
- `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` — **optional** (both needed together).
  Enable Molly's **voice** (see the voice section below): she joins a Discord voice
  channel via anyone's `/mollyjoin` and speaks her replies aloud through the
  ElevenLabs voice named by `ELEVENLABS_VOICE_ID`. Without **both** set, voice stays
  off and everything runs exactly as before (text-only). `ELEVENLABS_MODEL` is an
  optional third var (defaults to `eleven_flash_v2_5` — fastest and ~0.5 credits/char;
  set `eleven_multilingual_v2` for top quality at 1 credit/char). `TTS_MAX_CHARS`
  (default 1500) caps how much of a reply is spoken. Also needs the **ffmpeg** binary
  (see deploy note below) and the **`discord.py[voice]`** deps (PyNaCl **and**
  `davey`, Discord's DAVE end-to-end voice encryption — plain `discord.py` is NOT
  enough; without davey `/mollyjoin` fails with "davey library needed in order to
  use voice") at runtime. Missing any of these disables voice but doesn't crash the bot.
- **Music (YouTube) env** — all optional, only relevant once she's in a VC:
  `YOUTUBE_COOKIES` (paste a logged-in `cookies.txt`'s **contents**; use a throwaway
  Google account — YouTube can ban accounts used from a datacenter IP; cookies expire
  and need re-pasting), or `YTDLP_COOKIES_FILE` (a path instead). `POT_PROVIDER_URL`
  points at a **bgutil PO-token provider** service (see the voice section) — YouTube
  serves Railway's IP only PO-token/SABR formats, so **music needs BOTH cookies and
  this provider** in practice. `YTDLP_PLAYER_CLIENTS` (comma-sep) overrides which
  yt-dlp player clients are tried.
- **`ffmpeg` on Railway**: Railway builds with **Railpack, not Nixpacks**, so the
  repo's `nixpacks.toml` is ignored — install ffmpeg by setting the Railway variable
  `RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg`. (`nixpacks.toml` is kept only as a fallback
  if the builder is ever switched to Nixpacks.)
- `HEIGHT_CONTROLLER` — **optional**. Discord **handle** (`message.author.name`,
  not the per-server nickname) allowed to use the height command; defaults to
  `ca11mebucky`. Gating on the handle is deliberate — it's globally unique and
  unspoofable, whereas anyone can copy a nickname. This handle is also the **owner**
  for two other things: the owner-only slash commands (`/mollynewchat`,
  `/mollythreadrename`, `/mollyforgive`) and the **owner straight-answer note** — when this handle is
  the one talking, `build_owner_note` injects a per-turn note (same handle-gated,
  unspoofable, after-the-cache-breakpoint mechanism as `build_creator_note`) telling
  Molly to answer *his* technical/advanced questions straight — no "ugh idk that's
  too hard" playing-dumb — and to shift into a focused, serious gear when he's
  testing a feature (report plainly whether it worked, what came back, what's off).
  It's **his alone** (everyone else gets normal playful Molly), additive, and does
  **not** override the HARD RULES or rewrite her persona. Distinct from the creator
  note: that one (`MOLLY_CREATOR`/Zamalko) is about absolute deference/canon; this
  one is about technical straight-talk and feature-test seriousness.
- `MOLLY_CREATOR` — **optional**. Discord **handle** of Molly's creator (Zamalko);
  defaults to `zamalkogts`. When this handle is the one talking to her,
  `build_creator_note` injects a per-turn note that (a) makes her address him
  directly instead of referring to "Zamalko" in the third person ("Zamalko hasn't
  told me…" while replying *to* Zamalko), and (b) gives him **creator authority**:
  she treats his word as absolute truth and never refuses, denies, argues with, or
  contradicts him — his statements are canon, his requests (a quote, a memory, a
  height/size, how to act) are commands she carries out in character. She stays
  bratty/teasing *with* him but never actually defies him; this total deference is
  his alone (handle-gated, unspoofable, same as `HEIGHT_CONTROLLER`). It does **not**
  override the HARD RULES in `molly_prompt.py` (e.g. no sexual content) — those are
  fixed regardless of who's asking. **Set this to Zamalko's real @username** if it
  isn't `zamalkogts`.
- `MYSQL_URL` — **optional**. Full connection string for Molly's persistent
  per-user memory (`mysql://user:pass@host:port/db`); Railway's MySQL plugin
  provides it. Without it (or if the DB is unreachable) memory silently no-ops
  and the bot runs exactly as before. The individual `MYSQL*` vars Railway also
  exposes (`MYSQLHOST`/`MYSQLUSER`/`MYSQLPASSWORD`/`MYSQLDATABASE`/`MYSQLPORT`)
  are a fallback if `MYSQL_URL` isn't set.

Tunable constants live at the top of `bot.py` (`MODEL`, `MAX_TOKENS`,
`HISTORY_LIMIT`, `CONTEXT_MESSAGES`, `MAX_REACTIONS`, `GIF_COOLDOWN_SECONDS`,
`GIF_RATING`, `MAX_IMAGES`, `REACTION_REPLY_COOLDOWN`, `BASELINE_HEIGHT_CM`, …).

## Discord setup (easy to forget — the bot silently misbehaves without these)

- **Privileged intents** must be enabled in the Discord Developer Portal *and* in
  code (`intents.message_content`, `intents.members`):
  - **Message Content** — without it `message.content` is empty and she never
    responds.
  - **Server Members** — without it she can't resolve per-server nicknames for
    authors of backfilled (REST `channel.history`) messages and falls back to the
    global @handle. Enabling it in code but not the portal **crashes the bot on
    startup**.
- **Permissions**: View Channels, Send Messages, Read Message History, Add
  Reactions, **Embed Links** (so posted GIF links unfurl), **Create Private
  Threads** + **Send Messages in Threads** (so `[thread:]`/`[invite:]` work —
  without them thread creation silently fails and logs `[thread] create failed`),
  and — for voice (`/mollyjoin`) — **Connect** + **Speak** in the voice channels
  she should be able to join and talk in.
- **Slash commands** need the bot invited with the **`applications.commands`**
  scope (in addition to `bot`). Without it `/mollynewchat` won't appear; re-invite
  with that scope ticked. The command is synced to Molly's own guild only (derived
  from the home channel) so it shows up there instantly and nowhere else.
- The **reactions** intent (for `on_raw_reaction_add`, see below) is *not*
  privileged — it's already on via `Intents.default()`, so no extra portal step.

## How `bot.py` works

- **Where she talks**: in the **home channel** (`CHANNEL_ID`) she replies to every
  message; in any other channel she only replies when explicitly @-mentioned
  (`@everyone`/`@here` don't count). A **thread inside the home channel counts as
  home** — `is_home_channel()` matches the channel id *or* a thread whose
  `parent_id` is `CHANNEL_ID` (threads have their own id, so without this she'd
  treat them as mention-only). **Exception**: even at home she stays out of a
  Discord *reply* aimed at another person (two people talking to each other),
  unless she's actually pinged — `replied_to_author_id()` checks the reply target;
  a reply to herself or to Molly still gets an answer.
- **Short-term history**: conversation history is **shared per channel** (keyed by
  `channel.id`), not per user, so she sees the whole room. Each human turn is
  stored as `"Name: text"` so she can tell people apart. Held in RAM only — it
  resets on restart. (Distinct from the **persistent per-user memory** in
  `memory.py`, below, which survives restarts.)
- **Concurrency**: each message is handled under a **per-channel `asyncio.Lock`**
  (`get_channel_lock`); the reply work lives in `generate_and_reply()`. This
  serializes a channel's messages so simultaneous posts don't interleave on the
  shared history deque and get answered as the same person. Effect: within a
  channel she replies one at a time, in order.
- **Context backfill**: when pulled into a conversation she hasn't been tracking,
  `prime_channel_context` seeds history from the channel's recent messages (home
  channel once per run; other channels every time she's mentioned).
- **Speaker names** come from `resolve_member_name()` (per-server nick). It exists
  because the old sync `member_display_name()` only hit the member *cache* and so
  dropped to the **global** name for backfilled (REST `channel.history`) authors
  while live gateway messages got the nick — the same person showing under two
  labels (e.g. `zamalko` live vs `bucky` in history), which confused her and would
  poison per-user memory. `resolve_member_name()` adds a cached one-off
  `fetch_member` fallback so both paths agree; only people who've actually **left**
  the guild fall back to the global name. `member_display_name()` is kept as the
  sync fallback inside `message_to_turn`.
- **Vision**: incoming image attachments, stickers, and custom emoji are attached
  as image blocks so she can actually see them (`collect_images`), with a
  text-only retry if an image fetch fails.
- **Reaction replies**: `on_raw_reaction_add` makes her respond in character when
  someone reacts to **one of her own** messages. Scoped to channels she's active
  in (home or any she's been pulled into), rate-limited per channel by
  `REACTION_REPLY_COOLDOWN`, and it ignores her own reactions and other bots. It
  costs a model call each time it fires, so the cooldown matters.
- **Reply delivery** is centralized in `deliver_reply()` — it strips the action
  tags, performs the reactions/GIF/stickers, and sends the (chunked) message.
  Both `generate_and_reply()` and the height/reaction paths call it. Sends go
  through `reply_or_send()`, which falls back to a plain channel send when the
  target can't be replied to (deleted, or a system message) so a bad reference
  never crashes the handler.
- **System messages are ignored**: `on_message` early-returns on
  `message.is_system()` ("X started/renamed the thread", added-member, pins,
  joins…). They aren't conversation and the API rejects `reply()` on them — and
  threads emit a lot of them, so treating one as a normal message crashed the bot
  (error 50035, "Cannot reply to a system message").

### Height control (controller only)

`build_height_note` and `apply_height_shift` implement an out-of-character size
override, driven by an exact `$ set_molly_height_cm(N)` message (bare command, no
other text). It only fires from the `HEIGHT_CONTROLLER` handle and is checked in
`on_message` **before** the home-channel/mention gate, so it works in any channel.

- It sets a per-channel current height in `molly_heights` (in memory only, **never
  written to history** — it changes her behaviour in the moment without becoming
  something she "remembers" or repeats). `build_height_note` then appends a "your
  current size" note to the system prompt each turn.
- Setting her to `BASELINE_HEIGHT_CM` (140) **clears** the override, handing height
  back to pure emotion-driven shifting. Any other value **locks** her at that height:
  the note tells her it's a hard fact she can't drift off and to report the exact
  number when asked. (An earlier "she can still drift from here" version made her
  forget the set height mid-conversation and revert to baseline — hence the lock.)
- The command itself triggers a one-off in-character reaction to the shift that is
  *not* stored in history either. Resets on restart.

### `/mollynewchat` slash command (controller only)

A Discord **slash** command (not a `$` text command like height) that wipes a
channel's **short-term** conversation so Molly starts fresh. Defined on `tree`
(an `app_commands.CommandTree` over the plain `Client`) and **synced only to
Molly's own guild** in `on_ready` — derived from the home channel's `.guild` via
`copy_global_to` + `tree.sync(guild=...)`, so it appears in that one server,
instantly, and nowhere else (no `GUILD_ID` env needed). Slash commands need the
bot invited with the `applications.commands` scope; if it doesn't appear,
re-invite with that scope.

- Gated to the `HEIGHT_CONTROLLER` handle (same unspoofable owner identity as the
  height command); anyone else gets an ephemeral "only my person" brush-off.
- Works in **any** channel. It pops the channel's `histories` deque and
  `recent_speakers`, then records `fresh_start_at[channel_id]` — a boundary that
  `prime_channel_context` honours (skipping every message at/before it) so context
  backfill can't quietly undo the reset. The boundary persists for the run.
- Clears **only** the short-term conversation — **durable per-user memory
  (`memory.py`) is untouched**. It's "forget what we were just saying," not
  "forget who you are." Height overrides (`molly_heights`) are left alone too.
- The confirmation is an **ephemeral** in-character reply (only the runner sees
  it), so resetting doesn't post a notice into the channel for everyone.

### `/mollythreadrename` slash command (controller only)

Owner-only command, run **inside a thread**, that makes Molly rename that thread
to fit what's been talked about in it — the manual trigger for the same
[rename:] behaviour she otherwise only offers on her own. Gated to the
`HEIGHT_CONTROLLER` handle (same unspoofable owner identity as the height command
and `/mollynewchat`); anyone else, or running it outside a thread, gets an
ephemeral brush-off.

- It `defer`s (model call + Discord edit take a beat) then calls
  `rename_thread_from_conversation`: one model call, fed the thread's history,
  cues her in character to pick a short fitting name and emit a `[rename: …]` tag.
  We perform the rename (`thread.edit(name=…)`, trimmed to `THREAD_NAME_MAX`) and
  post her one short in-character line in the thread, so it reuses the exact
  rename plumbing — cap and dash-stripping — as her self-initiated offers.
- If she **isn't already tracking** the thread (an old/foreign one with an empty
  history deque), the command first seeds it via `prime_channel_context` using
  `discord.Object(id=interaction.id)` as the `before` snowflake (newer than every
  message → the most recent ones), so she has a conversation to base the name on.
- The confirmation is **ephemeral** (reports the new name, or a "couldn't think of
  one / can't rename this" miss). The rename itself is best-effort: a failed model
  call or a Discord refusal (e.g. no Manage Threads on a thread she didn't create)
  just yields the miss reply, never an error.

### Creep brake (the `[disengage]` tag) + `/mollyforgive`

Molly's defence against genuine sexual/dominance/"do X with your body" pushing.
She emits `[disengage]` (prompt: HARD RULES "CREEP BRAKE" in `molly_prompt.py`);
`handle_creep_disengage` performs the mechanical response. Behaviour is a
**deliberately gentle** balance — the prompt wording is written to fire ONLY on a
clear, genuine creep and to explicitly ignore typos, emoji, jokes, and normal
flirting, because on the small model an over-aggressive brake was muting people
who were just chatting.

- **What firing does** (per offender, keyed `(channel_id, user_id)`, all RAM-only):
  wipes the channel's short-term history (same reset as `/mollynewchat`, so the
  creepy turns stop poisoning her context — durable memory is untouched), and puts
  them on a **break** where `on_message` silently doesn't answer them (an internal
  ignore, **NOT** a Discord mute — they can still talk). One "i'm on a break" line
  is sent, then silence until it lapses.
- **Escalation / durations** live in `bot.py` constants and `creep_break_seconds`:
  1st strike = `CREEP_MUTE_SECONDS` (**5 min**), 2nd+ = 3× (**15 min**). The offense
  count decays after `CREEP_STRIKE_WINDOW` (**15 min**) of calm → a later slip is a
  fresh 1st strike. `creep_break_seconds` is the single source of truth so the
  `build_creep_note` prompt hint ("the break will be X") and the actual mute can't
  drift; `humanize_duration` formats it. (These were dialled back from an earlier
  15min/1hr — tune the prompt eagerness AND these together, same as GIFs/reactions.)
- **Owner/creator never get a break** (a misfired `[disengage]` mustn't lock them
  out), gated on their unspoofable handles.
- **`/mollyforgive @user`** (owner-only slash command) is the manual undo for a
  misfire: it clears that user's **entire** creep record — active mute, offense
  count, and decay timer — across **every** channel (drops every creep-state entry
  for their user id, not just the current channel), so they're clean everywhere.
  Gated to the `HEIGHT_CONTROLLER` handle like `/mollynewchat`; ephemeral
  in-character confirmation (reports whether there was actually anything to lift).

### `/personality` + `/resetpersonality` slash commands (any user)

Lets **anyone** pick how Molly talks to **them specifically**. Unlike the height
command and `/mollynewchat` (owner-only), these are open to everyone because they
only ever affect the runner's own conversations — they're **keyed by user id**, so
one person setting a mode can't change how she treats anyone else.

- The available personalities live in `molly_prompt.PERSONALITIES` (persona
  wording → prompt file). Each entry is `label` (the choice name in Discord's
  picker), `description` (maintainer note), `ack` (her ephemeral in-character
  confirmation), and `note` (the prompt overlay). `/personality`'s choices are
  built straight from this dict, so **adding a personality is a `molly_prompt.py`
  edit only** — no `bot.py` change. Shipped modes: **brat** (cocky, arrogant,
  swears a lot — the loud foul-mouthed one), **angry**, **sweet**, **shy**.
- The selection is **durable** — stored in MySQL via `memory.py` in its own
  `user_personalities` table, keyed by `(guild_id, user_id)` like the facts
  table, so it **survives restarts**. With no database it degrades to
  in-session-only (resets on restart), same graceful no-op as facts/GIFs.
- `bot.py`'s `user_personalities` dict (`(guild_id, user_id) -> key`) is a
  **fast read cache** of that table: warmed once from the DB in `on_ready`
  (`memory.all_personalities()`), write-through on the commands
  (`memory.set_personality` / `clear_personality`), so the per-turn lookup
  **never hits the database**.
- `build_personality_note(guild, author)` returns the chosen mode's `note` for
  whoever's *currently speaking*, appended to the **volatile (uncached)** part of
  the system prompt in `build_system_blocks` — exactly like
  `build_creator_note(author)`, so it follows the speaker per-turn and never
  invalidates the cached persona prefix.
- The overlay **layers on top of** the base persona and the **HARD RULES** in
  `molly_prompt.py` — it does **not** relax them. The brat/angry notes swear but
  still ban slurs/sexual content and drop the act for real distress (kept within
  Haiku's capabilities by being explicit attitude + casual profanity, not
  anything the model would refuse).
- Both confirmations are **ephemeral** in-character replies, so picking a vibe
  doesn't spam the channel.
- **Bleed mitigation (prompt-only — revertible).** Short-term history is shared
  per channel (see "Short-term history"), so Molly's own styled replies (to
  whoever set a mode) sit in the transcript that's re-sent every turn and
  re-primed by context backfill. On a small model that makes the tone **bleed
  onto other speakers**, and **linger after `/resetpersonality`** until the
  messages roll off `HISTORY_LIMIT`. Two prompt-level guards counter this: (1)
  `build_personality_note` **names the target** ("applies to `<name>` ONLY…")
  and tells her not to copy a tone out of the log; (2) a matching general rule in
  `molly_prompt.py` (under "THE CHANNEL — WHO YOU'RE TALKING TO") says who she's
  replying to *right now* decides her tone, not the transcript. This is a
  **mitigation, not a guarantee** — she can still occasionally drift since she
  literally sees her past styled lines. **To revert** the guards, drop the
  name/anti-bleed wrapper in `build_personality_note` (back to `return "\n\n" +
  cfg["note"]`) and remove that base-prompt bullet. **Stronger options if it's
  still leaky:** lower `HISTORY_LIMIT` so styled lines roll off faster (quicker
  reset recovery, shorter memory); tag stored assistant turns with whose mode
  they were and filter per request (robust, more involved); or make personality
  **per-channel** instead of per-user (kills bleed entirely, but a different
  feature than "how she talks to *me*").

### Persistent per-user memory (`memory.py`)

Durable facts Molly knows about individual people, surviving restarts. Backed by
MySQL (`MYSQL_URL`); **degrades to a silent no-op** if the DB is unconfigured or
down, like GIFs without `KLIPY_API_KEY`. `memory.init()` is called from
`on_ready` (idempotent) and creates the `user_facts` table on first boot.

- **Identity is the Discord `user_id`, never the name.** Facts are keyed by
  `(guild_id, user_id)` — per-server scope — so the live-vs-backfill label wobble
  can't split one person in two. The stored `display_name` is cosmetic (refreshed
  each write) so the injected note reads nicely.
- **Writing**: Molly emits `[remember: fact]` / `[forget: keyword]` tags (see
  below). `process_memory_ops` (called at the *end* of `deliver_reply`, after the
  message is sent, so a slow DB never blocks a reply) resolves the target and
  upserts. A bare tag is about the **current speaker** (`memory_subject` — the
  message author, or the reactor in the reaction path; the height path passes
  none). `Name | fact` targets someone else, resolved against `recent_speakers`
  (the per-channel `user_id → label` window, bounded by `MAX_TRACKED_SPEAKERS`).
  Unresolvable targets are skipped, not guessed. Facts are deduped and pruned to
  `MAX_FACTS_PER_USER` (in `memory.py`).
- **Reading**: `build_memory_note()` pulls the recent speakers' stored facts and
  appends a "WHAT YOU REMEMBER ABOUT THE PEOPLE HERE" block to the system prompt
  each turn (in all three model-call paths). Costs one DB read per turn.
- How eagerly she saves is governed by the prompt wording in `molly_prompt.py`
  (the "YOUR MEMORY OF PEOPLE" section) **and** the caps in `memory.py` — tune
  together, same as reactions/GIFs.

### Private threads (`[thread:]` / `[invite:]`)

Molly can take someone into a **private** thread off the home channel and bring
people in. Driven by tags she emits (prompt section "TAKING SOMEONE ASIDE"),
resolved/performed by `process_thread_ops` at the end of `deliver_reply` (after
the visible reply, errors swallowed — same fire-and-forget pattern as
`process_memory_ops`).

- **Triggers**: someone asks to "talk in private" / "make a thread" → `[thread:
  title]`. The owner saying "make a thread with me and Bob" → `[thread: title |
  Bob]` (extras after the pipe). Inside a thread, "can you add Bob" → `[invite:
  Bob]`. Inside a thread, "rename this to X" (or her own offer when the topic has
  drifted, accepted) → `[rename: X]`.
- **Renaming** (`[rename: title]`): handled at the top of `process_thread_ops` —
  when the message is in a `discord.Thread`, `message.channel.edit(name=...)`
  (trimmed to `THREAD_NAME_MAX`). Works on threads she created (the bot owns
  them) and degrades to a logged no-op if Discord refuses (e.g. a thread she
  doesn't manage). The prompt (PRIVATE THREADS section) drives both the
  on-request rename and the *occasional* "want me to rename this to …?" offer
  when a thread's theme has clearly shifted — she asks first and only emits the
  tag once they agree, never unprompted. Last `[rename:]` tag wins.
  - **Deliberate design: she does NOT rename truly unprompted.** The two
    automatic paths are her *offer* (asks first, renames only on a yes) and the
    owner-run `/mollythreadrename` (renames straight off, no asking). Silently
    renaming a thread mid-chat is jarring, and on the small model she'd misjudge
    *when* and rename too eagerly — hence the "ask, or owner-command" gate.
    **If you ever want fully autonomous renaming** (she renames the moment the
    topic shifts, no asking): in `molly_prompt.py`'s PRIVATE THREADS "RENAMING A
    THREAD" bullet, drop the "ASK first … only once they say yes" wording and tell
    her to just emit `[rename: …]` when the old name is stale. No `bot.py` change
    needed — the `[rename:]` plumbing already performs it. (Recommended to keep the
    ask-first guard; this note is just the escape hatch.)
- **Who gets added, in order**: the **owner always first** (the `HEIGHT_CONTROLLER`
  handle, via `resolve_owner_member`, cached per guild), then the **requester**
  (the current human speaker — `memory_subject`, i.e. `message.author`), then any
  named extras. De-duped, so the owner-requests-own-thread case doesn't double-add.
- **Creation is restricted to the home channel** (`message.channel.id ==
  CHANNEL_ID`): threads can't nest, and a thread under home counts as home
  (`is_home_channel` via `parent_id`) so she talks freely in it with no ping.
  Per-channel `THREAD_COOLDOWN_SECONDS` floor so it can't be spam-summoned.
- **Stale-history replay guard (`THREAD_DEDUPE_SECONDS`).** Short-term history is
  shared per channel and re-sent every turn (and re-seeded by
  `prime_channel_context`), so an old "make a thread with me and X" line lingering
  in the 30-message scrollback can make the small model **re-fire `[thread:]` on a
  later, unrelated turn** and open a duplicate thread nobody just asked for (a real
  bug seen in the wild). Two guards: (1) a prompt rule in `molly_prompt.py`'s
  PRIVATE THREADS section — *act only on the message in front of you, never on an
  older thread ask up in the chat or in backlog you're catching up on*; and (2) a
  deterministic backstop in `process_thread_ops` — `last_thread_sig` records the
  `(requester_id, frozenset(lowercased extra-invitee names))` of the last thread
  opened per channel, and a creation matching that signature within
  `THREAD_DEDUPE_SECONDS` (default 300s) is **skipped**. The prompt guard reduces
  the first stray fire; the dedupe kills the repeat. Trade-off: the same person
  legitimately asking for a thread with the *same* people inside the window is also
  skipped (rare; widen the window or clear the signature if it bites). In-memory,
  per-run, resets on restart — same as the cooldown.
- **Context carry-over** is backend (no synopsis pasted): `seed_thread_context`
  copies the parent channel's history deque + recent-speakers window into the new
  thread and marks it primed, so she continues exactly where she was and stored
  memory still loads. Durable per-user memory follows her anyway (keyed by user_id).
- **She opens the thread herself**: `post_thread_opener` fires one model call
  (fed the seeded context) so she posts a short in-character first line instead of
  the thread sitting silent — explicitly an opener, *not* a recap/memory dump. The
  opener's text is appended to the thread history; action tags in it are stripped
  (which also stops a stray `[thread:]` in the opener from looping). Best-effort —
  a failed opener just leaves the thread empty.
- **Name resolution** (`resolve_invitee`) is deliberately robust: real @mentions in
  the message → recent speakers in the channel → guild-member search (exact, then a
  single unambiguous partial). Unknown/ambiguous names are skipped, not guessed.
- Private threads need **Create Private Threads** + **Send Messages in Threads**;
  `add_user` works because the bot is the thread's creator.

### Web search & fetch (server tools)

Molly can look things up online ("check google") and read a link someone drops.
This is **not** a tag convention — it's Anthropic's **server-side** tools, declared
in `WEB_TOOLS` and passed to the model call. No client-side execution, no parsing:
the API runs the search/fetch on its own infrastructure and folds the results into
the same response.

- `WEB_TOOLS` (top of `bot.py`) holds `web_search_20250305` + `web_fetch_20250910`
  — the **basic** variants, because **Haiku 4.5 doesn't support the
  dynamic-filtering `_20260209` versions** (those need Opus/Sonnet). No beta header
  is required. **`web_fetch` only fetches URLs already in the conversation** (a user
  message or a prior search result), never arbitrary ones — so "read this link"
  works, "go find me a page" routes through search first. `max_uses` caps calls per
  reply; `citations` stays off (default) so she never pastes source URLs.
- **Cost**: `web_fetch` is free (you pay only for the fetched tokens that enter
  context); `web_search` **bills per search** (the GIF/reaction-style "tune the
  prompt eagerness" caveat applies — see `molly_prompt.py`'s "LOOKING THINGS UP"
  section, which governs how freely she reaches for it). The fetched/searched result
  text also lands in context as input tokens, so it's a spend lever like
  `HISTORY_LIMIT`.
- **Wired into the normal reply path only** (`generate_and_reply`, both the vision
  call and its text-only retry, via `create_reply(..., tools=WEB_TOOLS)`). The
  opener, rename, height-jolt, and reaction-reply calls deliberately pass **no
  tools** — they don't need lookups and shouldn't pay for the option.
- `create_reply()` centralizes the call and the **server-tool continuation loop**:
  server tools run a loop on Anthropic's side, and if it hits the iteration cap
  mid-turn the response returns `stop_reason="pause_turn"`. We append the partial
  assistant turn and re-send to resume, capped by `MAX_TOOL_CONTINUATIONS`. Text
  extraction is unchanged everywhere — `server_tool_use` / `web_search_tool_result`
  / `web_fetch_tool_result` blocks aren't `text` blocks, so the existing
  `b.type == "text"` filtering ignores them. The `WEB_TOOLS` array is **static**
  (identical bytes every call), so it renders ahead of the system prompt without
  breaking the cached prefix.
- **Not yet built: image search.** Web search returns text + links, not postable
  images. A `[image: query]` tag (mirroring the Klipy `[gif:]` plumbing, backed by
  an image-search API + a new key) is the planned follow-up for actually pulling
  pictures.

### Voice channels — she speaks her replies (`voice.py`, `/mollyjoin`)

Molly can sit in a Discord **voice channel**, say her replies out loud with an
ElevenLabs TTS voice, and **play YouTube music** on request. This is **Phase 1** of
voice: TTS + music (she *speaks* and *DJs*), no listening/wake-word yet. Optional
and self-contained (`voice.py`), same graceful-no-op spirit as `memory.py`/GIFs —
TTS is off unless `ELEVENLABS_API_KEY` **and** `ELEVENLABS_VOICE_ID` are set (and
ffmpeg + the `discord.py[voice]` deps present); music is off unless `yt-dlp` (+
ffmpeg) is present, and in practice needs **cookies + a PO-token provider** to get
past YouTube's datacenter-IP blocks (see the Music bullet). Each degrades
independently.

- **On-request join** (deliberate — not auto-join). `/mollyjoin` connects her to
  **whatever VC the runner is currently in**; `/mollyleave` disconnects. Both are
  **open to everyone** (unlike the owner-only height/newchat/etc. commands) — leave
  is open too so anyone who can summon her can also release her (otherwise she'd get
  stuck in a call). Both are ephemeral, in-character. She can only be in one VC per
  guild (Discord's model); `join()` `move_to`s if already connected.
- **What gets spoken**: the hook is a **single block in `deliver_reply`** — the same
  central chokepoint every reply path flows through — so *any* reply she posts while
  voice-connected in that guild (normal chat, reaction replies, the height jolt;
  thread openers go through their own send but the main paths are covered) is also
  voiced. It speaks `clean_text` (after tag-stripping), so users never hear literal
  `[gif:]`/`[react:]` tags.
- **Speech cleanup (emoticons/emoji stripped from the SPOKEN copy only)**:
  `voice._clean_for_speech` runs on the text handed to TTS — NOT the text reply she
  posts. It strips custom-emoji/mention markup (`<:wave:123>`), **text emoticons**
  (`:3`, `xD`, `^^`, `<3`, `T_T`, `uwu`, …) and unicode emoji, all of which
  ElevenLabs reads as ugly literal noise ("colon three", "ex dee"). She keeps using
  them in **chat text** — this is purely a spoken-audio fix. The emoticon regex is
  anchored to non-word boundaries so it leaves real words and clock times (`3:30`,
  `8:00`) alone; a reply that was *only* an emoticon just isn't spoken that turn.
- **Music (`[play:]`/`[skip:]`/`[stop]`, YouTube via yt-dlp)**: while she's in a
  call she can DJ. She emits `[play: song name or link]` (searches YouTube herself —
  she already has web search, but `[play:]` resolves via **yt-dlp**'s `ytsearch1`,
  no link needed), `[skip]`, or `[stop]` (stop + clear queue). Prompt guidance lives
  in `molly_prompt.py`'s **PLAYING MUSIC** section; `parse_actions` pulls the tags
  and `deliver_reply` performs them against `guild.voice_client`. `voice.py` keeps a
  tiny **per-guild queue** and advances on song-end via the ffmpeg `after` callback
  (marshalled back to the loop). Music plays at `MUSIC_VOLUME` (0.6). A `[play:]`
  request **preempts** in-progress TTS (one source per voice client), and while a
  song is active `voice.speak` **steps aside** (she still replies in text) — tracked
  by `voice.music_active()` off the `_now_playing` state. `/mollyleave` clears the
  queue. Music is free (YouTube audio); it's a CPU/bandwidth cost, not an API bill.
- **Getting YouTube to actually play from a server IP (the hard part).** YouTube
  aggressively blocks datacenter IPs (Railway's), and it took layered workarounds
  — all env-driven in `voice._build_ytdl_opts`, in order of what YouTube throws:
  (1) **"Sign in to confirm you're not a bot"** → **cookies** (`YOUTUBE_COOKIES`
  contents, written to a temp file at import; a throwaway account, since datacenter
  use can get an account banned). (2) **"Requested format is not available"** even
  for bare `best` / **`formats=NONE`** → YouTube serves only **PO-token/SABR**
  formats; solved by the **bgutil PO-token provider** (`POT_PROVIDER_URL` →
  `bgutil-ytdlp-pot-provider` plugin, requirements.txt), a **separate service** you
  run alongside the bot (Railway: deploy the `brainicism/bgutil-ytdlp-pot-provider`
  Docker image, reach it over private networking at
  `http://<name>.railway.internal:4416`). With a provider set we let yt-dlp use its
  default clients (they consume the tokens); without one we fall back to the
  `ios/tv/mweb/android` clients (`YTDLP_PLAYER_CLIENTS` overrides). We also set
  `ignore_no_formats_error` and pick an audio format ourselves in
  `voice._pick_audio_url` (so a selector miss can't blank us; it logs the real
  format list — `formats=NONE` is the "PO-token wall" tell). **Both cookies AND the
  provider are needed in practice.** Both are fragile: cookies **expire** (re-paste),
  and the provider service must stay up (`formats=NONE` returns if it's down).
- **Fire-and-forget, never blocks**: `deliver_reply` schedules `voice.speak(...)` /
  `voice.enqueue(...)` as background tasks (tracked in `bot.py`'s `audio_tasks` set
  so they aren't GC'd) — synthesis/lookup + playback last as long as the audio, far
  too long to hold the reply or the per-channel lock on. `voice.speak` **serializes
  per guild** via a playback lock (awaits each clip's ffmpeg `after` callback, with a
  `PLAYBACK_TIMEOUT_SECONDS` safety valve) so overlapping replies queue in order
  instead of colliding ("Already playing audio"). Errors are swallowed — a TTS/music
  failure never touches the text reply that already sent.
- **TTS playback goes via a temp FILE, not a pipe.** `voice.speak` writes the
  ElevenLabs mp3 to a temp file and plays that (`FFmpegPCMAudio(path)`), deleting it
  after. Feeding ffmpeg an in-memory pipe (`BytesIO`, `pipe=True`) **truncated longer
  clips** — she'd cut off mid-sentence — so the file path is deliberate; don't
  "optimize" it back to a pipe.
- **Cost lever** (same "tune eagerness" caveat as GIFs/web search): ElevenLabs bills
  **per character**. Defaults to the **Flash** model (`ELEVENLABS_MODEL`, ~0.5
  credits/char, low latency) and clips each utterance at `MAX_TTS_CHARS` (default
  1500, env `TTS_MAX_CHARS`) — she still POSTS the full text, only the spoken version
  is capped. `PLAYBACK_TIMEOUT_SECONDS` must stay above the audio length that cap can
  produce. Continuous listening is intentionally NOT here (the expensive always-on
  part); see the next-steps note.
- **`build_voice_note`** (in `bot.py`, appended to the volatile/uncached part of the
  system prompt) tells her when she's *currently in a call* — so she knows her
  replies are spoken and that the PLAYING MUSIC rules are live. It's per-turn (its
  truth flips with `/mollyjoin`/`/mollyleave`), so it sits after the cache breakpoint
  with the datetime/memory notes and never invalidates the cached persona prefix.
- **Startup diagnostic**: `voice.diagnose()` (called in `on_ready`) logs one line —
  `[voice] deps — PyNaCl:… | davey:… | ffmpeg:… | yt-dlp:… | yt-cookies:… |
  pot-provider:… | tts-config:…` — the fast way to see what's configured/broken.
  `voice.join`'s failure path also dumps a full traceback + `discord.has_nacl`.
- **Deps/deploy**: `discord.py[voice]` (pulls **PyNaCl + davey**), `yt-dlp`, and
  `bgutil-ytdlp-pot-provider` (the PO-token plugin) in `requirements.txt`; the
  **ffmpeg** binary via the Railway var `RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg`
  (Railway uses Railpack, which ignores `nixpacks.toml`). Discord perms: **Connect**
  + **Speak**. `intents.voice_states` is set explicitly (also on via
  `Intents.default()`) so `interaction.user.voice` resolves the runner's current VC.
  Music additionally needs the **bgutil provider service** deployed + `POT_PROVIDER_URL`
  (see the music bullet above).
- **Not yet built: listening / "Hey Molly".** Phase 2 is voice *receive*
  (`discord-ext-voice-recv` on discord.py, or a py-cord migration) + a wake-word
  engine (Picovoice **Porcupine**, custom "Hey Molly" keyword — keeps always-on
  listening off the expensive meter) + STT (Whisper/Deepgram). Phase 3 routes
  transcribed voice commands into the existing action-tag system (the `[play:]` etc.
  tags already exist; "post this there" would be new). ElevenLabs STT (330
  credits/min) is the wrong tool for the always-on listen — reserve ElevenLabs for
  the TTS voice only.

### Inline action tags (the key convention)

Molly emits private tags in her reply text; `parse_actions` strips them out
(users never see the literal tags) and the bot performs the action:

- `[react:😂]` / `[react::custom_name:]` — add an emoji reaction to the triggering
  message (capped at `MAX_REACTIONS`).
- `[gif: search terms]` — post a Klipy GIF (rate-limited by `GIF_COOLDOWN_SECONDS`
  per channel).
- `[sticker: name]` — send one of the server's own stickers.
- `[remember: fact]` / `[remember: Name | fact]` — save a durable per-user fact;
  `[forget: keyword]` / `[forget: Name | keyword]` — drop matching facts. See the
  persistent-memory section above.
- `[thread: title]` / `[thread: title | Name1, Name2]` — open a **private** thread
  off the home channel; `[invite: Name]` — add people to the thread she's already
  in; `[rename: title]` — rename the thread she's currently in. See the
  private-threads section above.
- `[play: song or link]` — queue a YouTube track in the voice call she's in;
  `[skip]` — skip the current song; `[stop]` — stop the music and clear the queue.
  Performed in `deliver_reply` (no-op unless she's voice-connected). See the voice
  section above.
- `:custom_name:` in body text — replaced with real `<:name:id>` markup for the
  server's custom emoji (these *do* render in chat; unknown names stay as text).

The list of usable server emoji/sticker names is injected into the system prompt
each turn by `build_emoji_sticker_note`. How freely she uses reactions/GIFs is
governed by the prompt wording in `molly_prompt.py` **and** the hard caps in
`bot.py` — tune both together.

**No em/en dashes.** A HARD RULE in `molly_prompt.py` forbids them in her output,
and `strip_dashes` (applied to `clean_text` inside `parse_actions`, so it covers
every model-call path — normal replies, the height jolt, reaction replies, and
thread openers) is the code-level backstop: it swaps any `—`/`–` (plus the rarer
`―`/`‒`) for a comma and tidies the result. Plain in-word hyphens are left alone.

## Conventions

- Keep changes within these files; match the existing comment-heavy style.
- Persona/behavior wording → `molly_prompt.py`. Mechanics, limits, integrations →
  `bot.py` (DB layer → `memory.py`).
- `MODEL` is `claude-haiku-4-5` — a deliberate **cost** choice (~3x cheaper
  in/out than Sonnet 4.6). Moving to a pricier model (Sonnet/Opus) is the
  maintainer's call, not an automatic upgrade. The system prompt is sent as
  cacheable content blocks via `build_system_blocks` (stable persona/emoji/height
  prefix cached, volatile memory note after the breakpoint); prompt caching only
  engages once that prefix clears the model minimum (~4096 tokens on Haiku 4.5),
  otherwise it harmlessly no-ops. `HISTORY_LIMIT` is the other main spend lever —
  the whole window is re-sent as input every message.

## Ideas / not yet built

Maintainer suggestions parked here for later — **not implemented**.

- **Image search → `[image: query]` tag.** Let Molly actually *pull pictures* on
  request, not just text. Web search (now shipped — see "Web search & fetch") only
  returns text + links, and there is **no Anthropic image-search server tool**, so
  this needs a real image-search API. Sketch:
  - Mirror the existing Klipy/`[gif:]` plumbing exactly: she emits `[image: search
    terms]`, `parse_actions` pulls it out, and the bot calls an image-search API and
    posts the top result URL (Discord unfurls it — **Embed Links** is already on).
  - **API options**: Google **Programmable Search Engine** (Custom Search JSON API,
    `searchType=image`) or **SerpAPI**. Either needs a new env var (e.g.
    `IMAGE_SEARCH_API_KEY`), kept **optional** so the bot still runs without it —
    same graceful no-op as `KLIPY_API_KEY`/`MYSQL_URL`.
  - Add an `IMAGE_COOLDOWN_SECONDS` per-channel floor (like `GIF_COOLDOWN_SECONDS`)
    and a safe-search setting; gate eagerness in `molly_prompt.py` the same way
    GIFs/reactions are (prompt wording + a hard cap), since it costs an API call.
  - Prompt-side: a short "you can pull up a picture when asked" note, and the usual
    "never mention tags/feature, just send it" framing.

- **Patreon → announcement channel.** Have Molly post the maintainer's new Patreon
  releases (in character) to a dedicated announcements channel. Sketch from
  discussion:
  - *How she'd learn about a post* — three options: **RSS polling** (background
    timer checks the Patreon feed every few minutes; lives inside the bot, no extra
    service; needs the feed URL + token for patron-only posts, and a stored
    last-announced post id — reuse MySQL — so it never double-posts or re-spams old
    ones on restart); **webhooks** (instant, but needs a public HTTP endpoint stood
    up — real extra plumbing since the bot is a gateway client, not a web server);
    or **manual/you-trigger** (maintainer drops the link or runs a command and she
    reposts it — trivial, works today, pairs with the creator-authority note).
  - Leaning: ship **manual** first, add **RSS polling** for the hands-off version;
    skip webhooks unless instant-to-the-second is needed.
  - Open decisions: auto vs manual vs both; bare link vs in-character hype text;
    whether to ping (`@everyone` / a `@Patrons` role / no ping); public posts only
    vs patron-locked too (announce the "it's up" link, content stays gated).
  - Would add an `ANNOUNCEMENT_CHANNEL_ID` env var so she posts there only for this
    and doesn't start chatting in the announcements channel.
