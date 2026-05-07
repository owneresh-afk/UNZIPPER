import sqlite3
import os
import time
import secrets
import string
from datetime import datetime, timedelta
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "bot_data.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            last_name   TEXT,
            is_active   INTEGER DEFAULT 0,
            license_key TEXT,
            license_expires REAL,
            joined_at   REAL DEFAULT (unixepoch()),
            last_seen   REAL DEFAULT (unixepoch()),
            files_sent  INTEGER DEFAULT 0,
            archives_processed INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            key          TEXT PRIMARY KEY,
            duration_sec INTEGER NOT NULL,
            label        TEXT,
            created_at   REAL DEFAULT (unixepoch()),
            used_by      INTEGER,
            used_at      REAL,
            is_used      INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_stats (
            id                      INTEGER PRIMARY KEY DEFAULT 1,
            total_files_sent        INTEGER DEFAULT 0,
            total_archives_done     INTEGER DEFAULT 0,
            total_keys_generated    INTEGER DEFAULT 0
        )
    """)

    c.execute("INSERT OR IGNORE INTO bot_stats (id) VALUES (1)")
    conn.commit()
    conn.close()


# ── User helpers ────────────────────────────────────────────────────────────

def upsert_user(user_id: int, username: str, first_name: str, last_name: str):
    conn = get_conn()
    conn.execute("""
        INSERT INTO users (user_id, username, first_name, last_name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username   = excluded.username,
            first_name = excluded.first_name,
            last_name  = excluded.last_name,
            last_seen  = unixepoch()
    """, (user_id, username or "", first_name or "", last_name or ""))
    conn.commit()
    conn.close()


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def is_user_active(user_id: int) -> bool:
    user = get_user(user_id)
    if not user or not user["is_active"]:
        return False
    if user["license_expires"] and user["license_expires"] < time.time():
        conn = get_conn()
        conn.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return False
    return True


def activate_user(user_id: int, license_key: str, duration_sec: int):
    conn = get_conn()
    now = time.time()
    expires = now + duration_sec
    conn.execute("""
        UPDATE users
        SET is_active = 1, license_key = ?, license_expires = ?
        WHERE user_id = ?
    """, (license_key, expires, user_id))
    conn.commit()
    conn.close()


def increment_user_stats(user_id: int, files: int = 0, archives: int = 0):
    conn = get_conn()
    conn.execute("""
        UPDATE users
        SET files_sent = files_sent + ?,
            archives_processed = archives_processed + ?
        WHERE user_id = ?
    """, (files, archives, user_id))
    conn.commit()
    conn.close()


def get_all_users():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY joined_at DESC").fetchall()
    conn.close()
    return rows


def get_active_users():
    conn = get_conn()
    now = time.time()
    rows = conn.execute("""
        SELECT * FROM users
        WHERE is_active = 1 AND (license_expires IS NULL OR license_expires > ?)
    """, (now,)).fetchall()
    conn.close()
    return rows


# ── License helpers ──────────────────────────────────────────────────────────

def _rand_key(length: int = 16) -> str:
    chars = string.ascii_uppercase + string.digits
    return "-".join(
        "".join(secrets.choice(chars) for _ in range(4))
        for _ in range(length // 4)
    )


def generate_keys(count: int, duration_sec: int, label: str) -> list[str]:
    conn = get_conn()
    keys = []
    for _ in range(count):
        k = _rand_key()
        conn.execute(
            "INSERT INTO licenses (key, duration_sec, label) VALUES (?, ?, ?)",
            (k, duration_sec, label)
        )
        keys.append(k)
    conn.execute("UPDATE bot_stats SET total_keys_generated = total_keys_generated + ? WHERE id = 1", (count,))
    conn.commit()
    conn.close()
    return keys


def redeem_license(key: str, user_id: int):
    """Returns (ok: bool, message: str, duration_sec: int)"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM licenses WHERE key = ?", (key.upper(),)).fetchone()
    if not row:
        conn.close()
        return False, "❌ Invalid key. Please check and try again.", 0
    if row["is_used"] and row["used_by"] != user_id:
        conn.close()
        return False, "❌ This key has already been used by someone else.", 0
    dur = row["duration_sec"]
    conn.execute("""
        UPDATE licenses SET is_used = 1, used_by = ?, used_at = unixepoch()
        WHERE key = ?
    """, (user_id, key.upper()))
    conn.commit()
    conn.close()
    activate_user(user_id, key.upper(), dur)
    return True, "✅ License activated!", dur


def get_license_info(key: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM licenses WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row


# ── Global stats ─────────────────────────────────────────────────────────────

def increment_global_stats(files: int = 0, archives: int = 0):
    conn = get_conn()
    conn.execute("""
        UPDATE bot_stats
        SET total_files_sent    = total_files_sent + ?,
            total_archives_done = total_archives_done + ?
        WHERE id = 1
    """, (files, archives))
    conn.commit()
    conn.close()


def get_global_stats() -> sqlite3.Row:
    conn = get_conn()
    row = conn.execute("SELECT * FROM bot_stats WHERE id = 1").fetchone()
    conn.close()
    return row


def parse_duration(text: str):
    """
    Parse duration strings like 1D, 7D, 30D, 1H, 30M, 1W, 1MO.
    Returns (seconds, label) or (None, None) on failure.
    """
    text = text.strip().upper()
    units = {
        "MO": 30 * 24 * 3600,
        "W":  7 * 24 * 3600,
        "D":  24 * 3600,
        "H":  3600,
        "M":  60,
    }
    for suffix, mul in units.items():
        if text.endswith(suffix) and text[:-len(suffix)].isdigit():
            n = int(text[:-len(suffix)])
            labels = {"MO": "month(s)", "W": "week(s)", "D": "day(s)", "H": "hour(s)", "M": "minute(s)"}
            return n * mul, f"{n} {labels[suffix]}"
    return None, None
