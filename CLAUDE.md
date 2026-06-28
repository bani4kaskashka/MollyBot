# CLAUDE.md

Guidance for working in this repo.

## What this is

MollyBot is a Discord bot that replies in character as **Molly Simpson**, a
character from the visual novel *Molly's Favourite Toy* by Zamalko. It listens
in a Discord server and generates replies with the Anthropic API.

The bot is three files:

- `bot.py` — all the Discord + Anthropic wiring.
- `molly_prompt.py` — `MOLLY_SYSTEM_PROMPT`, the character/persona and all the
  behavioral rules. **Tune Molly's personality and behavior here**, not in code.
- `memory.py` — the MySQL layer for persistent per-user memory (see below). Pure
  mechanics; degrades to a no-op if the database isn't configured/reachable.

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
- `HEIGHT_CONTROLLER` — **optional**. Discord **handle** (`message.author.name`,
  not the per-server nickname) allowed to use the height command; defaults to
  `ca11mebucky`. Gating on the handle is deliberate — it's globally unique and
  unspoofable, whereas anyone can copy a nickname.
- `MOLLY_CREATOR` — **optional**. Discord **handle** of Molly's creator (Zamalko);
  defaults to `zamalkogts`. When this handle is the one talking to her,
  `build_creator_note` injects a per-turn note so she addresses him directly
  instead of referring to "Zamalko" in the third person ("Zamalko hasn't told
  me…" while replying *to* Zamalko). Handle-gated for the same unspoofable reason
  as `HEIGHT_CONTROLLER`. **Set this to Zamalko's real @username** if it isn't
  `zamalkogts`.
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
  Reactions, **Embed Links** (so posted GIF links unfurl).
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
  Both `generate_and_reply()` and the height/reaction paths call it.

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
- `:custom_name:` in body text — replaced with real `<:name:id>` markup for the
  server's custom emoji (these *do* render in chat; unknown names stay as text).

The list of usable server emoji/sticker names is injected into the system prompt
each turn by `build_emoji_sticker_note`. How freely she uses reactions/GIFs is
governed by the prompt wording in `molly_prompt.py` **and** the hard caps in
`bot.py` — tune both together.

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
