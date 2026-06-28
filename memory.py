"""Persistent per-user memory for Molly, backed by MySQL (Railway).

Molly accumulates durable facts about the people she talks to via [remember: ...]
tags in her replies. They're stored here keyed by (guild_id, user_id) — the
Discord user id, NOT a display name, so the same person is never split across the
nickname/handle they happen to show under at a given moment — and injected back
into her prompt when those people speak.

The whole module degrades gracefully: if MySQL isn't configured (no MYSQL_URL /
MYSQL* env), the driver isn't installed, or the database is unreachable, every
call quietly no-ops and the bot runs exactly as before — same spirit as GIFs
silently switching off without KLIPY_API_KEY.

Configuration: set MYSQL_URL to the full connection string Railway gives the
MySQL plugin (mysql://user:pass@host:port/dbname). The individual MYSQL* vars
Railway also exposes (MYSQLHOST/MYSQLUSER/MYSQLPASSWORD/MYSQLDATABASE/MYSQLPORT)
are used as a fallback if MYSQL_URL isn't present.
"""

import os
from urllib.parse import unquote, urlparse

try:  # The driver is optional — without it memory just stays off.
    import aiomysql
except ImportError:  # pragma: no cover - depends on deploy env
    aiomysql = None

# Most facts kept (and injected) per person. Storage is pruned to this on every
# write so a chatty channel can't grow one user's memory without bound, and the
# prompt note stays a manageable size.
MAX_FACTS_PER_USER = 25
# Column width; a longer fact is truncated rather than rejected.
FACT_MAX_LEN = 500

_pool = None
_enabled = False

# Schema is created on demand so a fresh database just works on first boot.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_facts (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    display_name VARCHAR(255),
    fact VARCHAR(500) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_guild_user (guild_id, user_id)
) DEFAULT CHARSET=utf8mb4
"""

# Separate table for the per-user /personality overlay (one row per person per
# guild — the chosen mode, not a list of facts). Keyed the same (guild_id,
# user_id) way as user_facts so the same person is never split across labels.
_PERSONALITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_personalities (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    personality VARCHAR(64) NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id)
) DEFAULT CHARSET=utf8mb4
"""


def enabled() -> bool:
    """True once a pool is live and the schema is ready; False means no-op mode."""
    return _enabled


def _connection_kwargs() -> dict | None:
    """Build aiomysql connect kwargs from the environment, or None if unconfigured.

    Prefers a full MYSQL_URL; falls back to Railway's individual MYSQL* vars.
    """
    url = os.environ.get("MYSQL_URL", "").strip()
    if url:
        parsed = urlparse(url)
        if parsed.scheme.startswith("mysql") and parsed.hostname:
            return {
                "host": parsed.hostname,
                "port": parsed.port or 3306,
                "user": unquote(parsed.username or ""),
                "password": unquote(parsed.password or ""),
                "db": (parsed.path or "").lstrip("/") or None,
            }
    host = os.environ.get("MYSQLHOST", "").strip()
    if host:
        return {
            "host": host,
            "port": int(os.environ.get("MYSQLPORT", "3306") or 3306),
            "user": os.environ.get("MYSQLUSER", "root"),
            "password": os.environ.get("MYSQLPASSWORD", ""),
            "db": os.environ.get("MYSQLDATABASE") or None,
        }
    return None


async def init() -> None:
    """Create the connection pool and ensure the table exists.

    Idempotent and safe to call from on_ready (which can fire more than once).
    Any failure leaves memory disabled and prints why — it never raises, so a
    missing/broken database can't stop the bot from logging in and replying.
    """
    global _pool, _enabled
    if _pool is not None:
        return
    if aiomysql is None:
        print("[memory] aiomysql not installed — persistent memory disabled.")
        return
    cfg = _connection_kwargs()
    if cfg is None:
        print("[memory] no MYSQL_URL/MYSQL* env set — persistent memory disabled.")
        return
    try:
        pool = await aiomysql.create_pool(autocommit=True, minsize=1, maxsize=5, **cfg)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SCHEMA)
                await cur.execute(_PERSONALITY_SCHEMA)
        _pool = pool
        _enabled = True
        print("[memory] persistent memory enabled.")
    except Exception as exc:  # noqa: BLE001 — a DB problem must not crash startup
        print(f"[memory] init failed, persistent memory disabled: {exc}")
        _pool = None
        _enabled = False


async def remember(guild_id: int, user_id: int, display_name: str, fact: str) -> None:
    """Store one durable fact about a user (deduped, then pruned to the cap).

    Identical facts (case-insensitive) are skipped — only the stored display
    name is refreshed — so repeats don't pile up. No-op when memory is disabled.
    """
    if not _enabled or not fact:
        return
    fact = fact.strip()[:FACT_MAX_LEN]
    if not fact:
        return
    try:
        async with _pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM user_facts WHERE guild_id=%s AND user_id=%s "
                    "AND LOWER(fact)=LOWER(%s) LIMIT 1",
                    (guild_id, user_id, fact),
                )
                if await cur.fetchone():
                    # Already known — just keep the name we show current.
                    await cur.execute(
                        "UPDATE user_facts SET display_name=%s "
                        "WHERE guild_id=%s AND user_id=%s",
                        (display_name, guild_id, user_id),
                    )
                    return
                await cur.execute(
                    "INSERT INTO user_facts (guild_id, user_id, display_name, fact) "
                    "VALUES (%s, %s, %s, %s)",
                    (guild_id, user_id, display_name, fact),
                )
                # Drop anything beyond the most recent MAX_FACTS_PER_USER.
                await cur.execute(
                    "DELETE FROM user_facts WHERE guild_id=%s AND user_id=%s "
                    "AND id NOT IN (SELECT id FROM (SELECT id FROM user_facts "
                    "WHERE guild_id=%s AND user_id=%s "
                    "ORDER BY created_at DESC, id DESC LIMIT %s) keep)",
                    (guild_id, user_id, guild_id, user_id, MAX_FACTS_PER_USER),
                )
    except Exception as exc:  # noqa: BLE001 — memory writes must not break a reply
        print(f"[memory] remember failed: {exc}")


async def forget(guild_id: int, user_id: int, needle: str) -> int:
    """Delete a user's facts containing `needle` (case-insensitive substring).

    Returns how many were removed (0 when disabled or nothing matched).
    """
    if not _enabled or not needle:
        return 0
    needle = needle.strip()
    if not needle:
        return 0
    try:
        async with _pool.acquire() as conn:
            async with conn.cursor() as cur:
                removed = await cur.execute(
                    "DELETE FROM user_facts WHERE guild_id=%s AND user_id=%s "
                    "AND LOWER(fact) LIKE LOWER(%s)",
                    (guild_id, user_id, f"%{needle}%"),
                )
                return removed or 0
    except Exception as exc:  # noqa: BLE001
        print(f"[memory] forget failed: {exc}")
        return 0


async def facts_for(guild_id: int, user_ids: list[int]) -> dict[int, tuple[str, list[str]]]:
    """Return {user_id: (display_name, [facts...])} for the given users.

    Oldest-first, capped at MAX_FACTS_PER_USER each. Empty dict when disabled or
    nothing is known. Users with no stored facts are simply absent from the map.
    """
    if not _enabled or not user_ids:
        return {}
    placeholders = ",".join(["%s"] * len(user_ids))
    try:
        async with _pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT user_id, display_name, fact FROM user_facts "
                    f"WHERE guild_id=%s AND user_id IN ({placeholders}) "
                    "ORDER BY created_at ASC, id ASC",
                    (guild_id, *user_ids),
                )
                rows = await cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"[memory] facts_for failed: {exc}")
        return {}

    out: dict[int, tuple[str, list[str]]] = {}
    for user_id, display_name, fact in rows:
        name, facts = out.get(user_id, (display_name, []))
        facts.append(fact)
        out[user_id] = (display_name or name, facts)
    return {
        uid: (name, facts[-MAX_FACTS_PER_USER:]) for uid, (name, facts) in out.items()
    }


async def set_personality(guild_id: int, user_id: int, personality: str) -> None:
    """Persist a user's chosen /personality overlay (upsert, one row per person).

    No-op when memory is disabled — the caller keeps an in-memory cache either
    way, so the feature still works in-session without a database; it just won't
    survive a restart.
    """
    if not _enabled or not personality:
        return
    try:
        async with _pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO user_personalities (guild_id, user_id, personality) "
                    "VALUES (%s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE personality=VALUES(personality)",
                    (guild_id, user_id, personality[:64]),
                )
    except Exception as exc:  # noqa: BLE001 — a settings write must not break anything
        print(f"[memory] set_personality failed: {exc}")


async def clear_personality(guild_id: int, user_id: int) -> None:
    """Drop a user's stored /personality overlay (no-op when disabled/absent)."""
    if not _enabled:
        return
    try:
        async with _pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM user_personalities WHERE guild_id=%s AND user_id=%s",
                    (guild_id, user_id),
                )
    except Exception as exc:  # noqa: BLE001
        print(f"[memory] clear_personality failed: {exc}")


async def all_personalities() -> dict[tuple[int, int], str]:
    """Load every stored personality as {(guild_id, user_id): personality}.

    Called once on startup to warm bot.py's in-memory read cache, so the per-turn
    lookup never touches the database. Empty dict when memory is disabled.
    """
    if not _enabled:
        return {}
    try:
        async with _pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT guild_id, user_id, personality FROM user_personalities"
                )
                rows = await cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"[memory] all_personalities failed: {exc}")
        return {}
    return {(gid, uid): personality for gid, uid, personality in rows}
