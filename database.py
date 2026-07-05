"""
Database schema for the outreach bot.
SQLite, all in one file. Handles creators, emails, accounts, stages, conversations, settings.
"""

import sqlite3
import os
import json
from datetime import datetime, date, timedelta
from config import (
    DB_PATH, DEFAULT_DAILY_LIMIT_PER_ACCOUNT, DEFAULT_MIN_INTERVAL_SECONDS,
    DEFAULT_MAX_INTERVAL_SECONDS, DEFAULT_FOLLOWUP_DAYS, DEFAULT_MAX_FOLLOWUPS,
    DEFAULT_REPLY_CHECK_MINUTES, DEFAULT_AUTO_REPLY_MODE,
    DEFAULT_SYSTEM_PROMPT, DEFAULT_DEAL,
)


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def init_db():
    """Create all tables if they don't exist."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS gmail_accounts (
            email TEXT PRIMARY KEY,
            app_password TEXT NOT NULL,
            daily_limit INTEGER DEFAULT 50,
            sent_today INTEGER DEFAULT 0,
            last_reset_date TEXT,
            active INTEGER DEFAULT 1,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS creators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            handle TEXT,
            platform TEXT DEFAULT 'instagram',
            email TEXT UNIQUE NOT NULL,
            followers INTEGER,
            tier TEXT,
            bio TEXT,
            niche TEXT,
            stage TEXT DEFAULT 'new',
            source TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_contact TIMESTAMP,
            notes TEXT,
            recent_posts_stats TEXT
        );

        CREATE TABLE IF NOT EXISTS emails_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER,
            from_account TEXT,
            to_email TEXT,
            subject TEXT,
            body TEXT,
            message_type TEXT DEFAULT 'opener',
            status TEXT DEFAULT 'queued',
            queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP,
            error_msg TEXT,
            message_id TEXT,
            FOREIGN KEY (creator_id) REFERENCES creators(id),
            FOREIGN KEY (from_account) REFERENCES gmail_accounts(email)
        );

        CREATE TABLE IF NOT EXISTS replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER,
            from_email TEXT,
            received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            subject TEXT,
            body TEXT,
            handled INTEGER DEFAULT 0,
            action_taken TEXT,
            FOREIGN KEY (creator_id) REFERENCES creators(id)
        );

        CREATE TABLE IF NOT EXISTS conversation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (creator_id) REFERENCES creators(id)
        );

        CREATE TABLE IF NOT EXISTS followups_scheduled (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER,
            scheduled_for TIMESTAMP,
            followup_number INTEGER,
            status TEXT DEFAULT 'pending',
            FOREIGN KEY (creator_id) REFERENCES creators(id)
        );

        CREATE TABLE IF NOT EXISTS dm_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER,
            platform TEXT,
            handle TEXT,
            reminded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            dm_sent INTEGER DEFAULT 0,
            FOREIGN KEY (creator_id) REFERENCES creators(id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS pending_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action_type TEXT,
            context TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()

    # Initialize default settings if first run
    _init_default_settings(conn)

    # ── DISCOVERY_TABLES_V2 ───────────────────────────────────
    try:
        import discovery as _disc
        _disc.init_tables()
    except ImportError:
        pass

    # ── DISCOVERY TABLES PATCH ────────────────────────────────────
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dm_cookies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sessionid TEXT NOT NULL, csrftoken TEXT NOT NULL,
            label TEXT, active INTEGER DEFAULT 1,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used TIMESTAMP);
        CREATE TABLE IF NOT EXISTS dm_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id TEXT NOT NULL,
            username TEXT NOT NULL,
            message_text TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            scheduled_time TIMESTAMP NOT NULL,
            cookie_id_used INTEGER,
            sent_at TIMESTAMP,
            error_msg TEXT);
        CREATE TABLE IF NOT EXISTS ig_cookies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sessionid TEXT NOT NULL, csrftoken TEXT NOT NULL,
            label TEXT, active INTEGER DEFAULT 1,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS seen_profiles (
            username TEXT PRIMARY KEY,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS discovery_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            seed_list TEXT, hops INTEGER DEFAULT 1,
            profile_count INTEGER DEFAULT 0,
            csv_path TEXT, xlsx_path TEXT, outreach_path TEXT,
            scrape_mode TEXT DEFAULT 'hybrid');
        CREATE TABLE IF NOT EXISTS autoscans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seed_list TEXT, hops INTEGER DEFAULT 1,
            interval_hours INTEGER DEFAULT 24,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS apify_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL, label TEXT,
            credits_used REAL DEFAULT 0, last_used TIMESTAMP,
            last_error TEXT, active INTEGER DEFAULT 1,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS stage_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER, from_stage TEXT, to_stage TEXT,
            changed_by TEXT DEFAULT 'user',
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, reason TEXT);
        CREATE TABLE IF NOT EXISTS seeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            source TEXT DEFAULT 'manual',
            active INTEGER DEFAULT 1,
            last_scanned TIMESTAMP,
            profiles_found INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS campaign_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER,
            event_type TEXT NOT NULL,
            from_stage TEXT, to_stage TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT);
        CREATE TABLE IF NOT EXISTS smtp_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT UNIQUE,
            smtp_user TEXT,
            smtp_pass TEXT,
            daily_sent INTEGER DEFAULT 0,
            daily_limit INTEGER DEFAULT 10,
            last_reset TEXT,
            is_active INTEGER DEFAULT 1,
            warmup_day INTEGER DEFAULT 1);
    """)
    conn.commit()
    for _cn, _ct in [("profile_url","TEXT"),("location","TEXT"),("is_verified","INTEGER DEFAULT 0"),
        ("is_business","INTEGER DEFAULT 0"),("post_count","INTEGER DEFAULT 0"),
        ("engagement_rate","REAL"),("reach_ratio","REAL"),("score_total","INTEGER DEFAULT 0"),
        ("budget_tier","TEXT"),("source_seed","TEXT"),("hop","INTEGER DEFAULT 0"),
        ("discovered_at","TIMESTAMP"),("won_at","TIMESTAMP"),("lost_at","TIMESTAMP"),("tags","TEXT"),
        ("last_dm_sent_at","TIMESTAMP"),("thread_id","TEXT")]:
        try: conn.execute(f"ALTER TABLE creators ADD COLUMN {_cn} {_ct}")
        except Exception: pass
    for _cn, _ct in [("sentiment","TEXT"),("intent","TEXT"),("urgency","INTEGER DEFAULT 0")]:
        try: conn.execute(f"ALTER TABLE replies ADD COLUMN {_cn} {_ct}")
        except Exception: pass
        
    # Performance indices
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_creators_stage ON creators(stage);
        CREATE INDEX IF NOT EXISTS idx_creators_username ON creators(handle);
        CREATE INDEX IF NOT EXISTS idx_dm_queue_status ON dm_queue(status, scheduled_time);
        CREATE INDEX IF NOT EXISTS idx_replies_creator_id ON replies(creator_id);
    """)
    conn.commit()
    conn.close()


def _init_default_settings(conn):
    defaults = {
        "min_interval_seconds": str(DEFAULT_MIN_INTERVAL_SECONDS),
        "max_interval_seconds": str(DEFAULT_MAX_INTERVAL_SECONDS),
        "followup_days": json.dumps(DEFAULT_FOLLOWUP_DAYS),
        "max_followups": str(DEFAULT_MAX_FOLLOWUPS),
        "reply_check_minutes": str(DEFAULT_REPLY_CHECK_MINUTES),
        "auto_reply_mode": DEFAULT_AUTO_REPLY_MODE,
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "deal_structure": json.dumps(DEFAULT_DEAL),
        "onboarded": "0",
        "openrouter_api_key": "",
        "openrouter_model": "meta-llama/llama-3.1-8b-instruct:free",
        "cf_token": "",
        "cf_zone": "",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
    conn.commit()


# ═══════════════════════════════════════════════════════════════
#                       SETTINGS
# ═══════════════════════════════════════════════════════════════

def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, str(value))
    )
    conn.commit()
    conn.close()


def is_onboarded():
    return get_setting("onboarded", "0") == "1"


def mark_onboarded():
    set_setting("onboarded", "1")


# ═══════════════════════════════════════════════════════════════
#                    GMAIL ACCOUNTS
# ═══════════════════════════════════════════════════════════════

def add_account(email, app_password, daily_limit=DEFAULT_DAILY_LIMIT_PER_ACCOUNT):
    conn = get_db()
    today = date.today().isoformat()
    try:
        conn.execute(
            "INSERT INTO gmail_accounts (email, app_password, daily_limit, last_reset_date) VALUES (?,?,?,?)",
            (email, app_password, daily_limit, today)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_account(email):
    """Remove account, nullifying from_account in emails_sent to avoid FK constraint."""
    conn = get_db()
    conn.execute("UPDATE emails_sent SET from_account=NULL WHERE from_account=?", (email,))
    conn.execute("DELETE FROM gmail_accounts WHERE email=?", (email,))
    conn.commit()
    conn.close()


def remove_account_safe(email):
    """Smart remove: checks for sent emails first, warns if there's history."""
    conn = get_db()
    sent_count = conn.execute(
        "SELECT COUNT(*) as c FROM emails_sent WHERE from_account=?", (email,)
    ).fetchone()["c"]
    exists = conn.execute(
        "SELECT COUNT(*) as c FROM gmail_accounts WHERE email=?", (email,)
    ).fetchone()["c"]

    if not exists:
        conn.close()
        return {"success": False, "message": f"Account {email} not found."}

    # Nullify FK reference, then delete
    conn.execute("UPDATE emails_sent SET from_account=NULL WHERE from_account=?", (email,))
    conn.execute("DELETE FROM gmail_accounts WHERE email=?", (email,))
    conn.commit()
    conn.close()
    return {
        "success": True,
        "message": f"Removed {email}. {sent_count} sent emails preserved in history.",
    }


def set_account_limit(email, new_limit):
    conn = get_db()
    conn.execute("UPDATE gmail_accounts SET daily_limit=? WHERE email=?", (new_limit, email))
    conn.commit()
    conn.close()


def set_account_active(email, active):
    conn = get_db()
    conn.execute("UPDATE gmail_accounts SET active=? WHERE email=?", (1 if active else 0, email))
    conn.commit()
    conn.close()


def get_all_accounts():
    """Get all gmail accounts."""
    return get_db().execute("SELECT * FROM gmail_accounts").fetchall()

# --- SMTP ALIAS FUNCTIONS ---
def add_smtp_alias(alias, smtp_user, smtp_pass):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO smtp_aliases (alias, smtp_user, smtp_pass) VALUES (?, ?, ?)",
            (alias, smtp_user, smtp_pass)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def get_all_aliases():
    return get_db().execute("SELECT * FROM smtp_aliases").fetchall()

def toggle_alias(alias):
    conn = get_db()
    current = conn.execute("SELECT is_active FROM smtp_aliases WHERE alias=?", (alias,)).fetchone()
    if not current:
        conn.close()
        return False
    new_status = 1 if current["is_active"] == 0 else 0
    conn.execute("UPDATE smtp_aliases SET is_active=? WHERE alias=?", (new_status, alias))
    conn.commit()
    conn.close()
    return new_status == 1

def remove_alias_safe(alias):
    """Safely removes an alias without breaking foreign key constraints on emails_sent (if any)."""
    conn = get_db()
    # Check if exists
    exists = conn.execute("SELECT 1 FROM smtp_aliases WHERE alias=?", (alias,)).fetchone()
    if not exists:
        conn.close()
        return {"success": False, "message": f"Alias {alias} not found."}
        
    # Technically emails_sent doesn't use alias as a strict FK yet, but if it does:
    conn.execute("UPDATE emails_sent SET from_account=NULL WHERE from_account=?", (alias,))
    conn.execute("DELETE FROM smtp_aliases WHERE alias=?", (alias,))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Alias {alias} completely removed."}

def set_master_aliases_status(master_email, status):
    """Sets is_active=status (1 or 0) for all aliases belonging to a master email."""
    conn = get_db()
    cursor = conn.execute("UPDATE smtp_aliases SET is_active=? WHERE smtp_user=?", (status, master_email))
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count

def update_alias_limit(alias, new_limit):
    conn = get_db()
    conn.execute("UPDATE smtp_aliases SET daily_limit=? WHERE alias=?", (new_limit, alias))
    conn.commit()

def increment_alias_sent(alias):
    today = date.today().isoformat()
    conn = get_db()
    row = conn.execute("SELECT last_reset FROM smtp_aliases WHERE alias=?", (alias,)).fetchone()
    if row and row["last_reset"] != today:
        conn.execute("UPDATE smtp_aliases SET daily_sent=1, last_reset=? WHERE alias=?", (today, alias))
    else:
        conn.execute("UPDATE smtp_aliases SET daily_sent=daily_sent+1 WHERE alias=?", (alias,))
    conn.commit()

def progress_alias_warmup(alias):
    """Increase daily limit by 10 up to 50 for warmup."""
    conn = get_db()
    row = conn.execute("SELECT warmup_day, daily_limit FROM smtp_aliases WHERE alias=?", (alias,)).fetchone()
    if not row: return
    
    new_limit = min(50, row["daily_limit"] + 10)
    new_day = row["warmup_day"] + 1
    conn.execute("UPDATE smtp_aliases SET daily_limit=?, warmup_day=? WHERE alias=?", (new_limit, new_day, alias))
    conn.commit()


def get_next_available_account():
    """Get the next account or alias that has room for sending today."""
    conn = get_db()
    today = date.today().isoformat()

    # Reset daily counters for new day
    conn.execute(
        "UPDATE gmail_accounts SET sent_today=0, last_reset_date=? WHERE last_reset_date != ? OR last_reset_date IS NULL",
        (today, today)
    )
    conn.execute(
        "UPDATE smtp_aliases SET daily_sent=0, last_reset=? WHERE last_reset != ? OR last_reset IS NULL",
        (today, today)
    )
    conn.commit()

    candidates = []
    
    # Get gmail accounts
    acc_rows = conn.execute("""
        SELECT email as id, 'gmail' as type, email as from_email, email as smtp_user, app_password as smtp_pass, last_used, sent_today
        FROM gmail_accounts
        WHERE active=1 AND sent_today < daily_limit
    """).fetchall()
    candidates.extend([dict(r) for r in acc_rows])
    
    # Get aliases
    alias_rows = conn.execute("""
        SELECT alias as id, 'alias' as type, alias as from_email, smtp_user, smtp_pass, NULL as last_used, daily_sent as sent_today
        FROM smtp_aliases
        WHERE is_active=1 AND daily_sent < daily_limit
    """).fetchall()
    candidates.extend([dict(r) for r in alias_rows])
    
    conn.close()
    
    if not candidates:
        return None
        
    # Sort by least recently used, or least sent today
    candidates.sort(key=lambda x: (x["sent_today"]))
    return candidates[0]


def increment_account_sent(email, is_alias=False):
    if is_alias:
        increment_alias_sent(email)
        return
    conn = get_db()
    conn.execute(
        "UPDATE gmail_accounts SET sent_today=sent_today+1, last_used=? WHERE email=?",
        (datetime.now().isoformat(), email)
    )
    conn.commit()
    conn.close()


def total_remaining_today():
    conn = get_db()
    today = date.today().isoformat()
    conn.execute(
        "UPDATE gmail_accounts SET sent_today=0, last_reset_date=? WHERE last_reset_date != ? OR last_reset_date IS NULL",
        (today, today)
    )
    conn.execute(
        "UPDATE smtp_aliases SET daily_sent=0, last_reset=? WHERE last_reset != ? OR last_reset IS NULL",
        (today, today)
    )
    conn.commit()
    row1 = conn.execute("""
        SELECT COALESCE(SUM(daily_limit - sent_today), 0) as remaining
        FROM gmail_accounts WHERE active=1 AND sent_today < daily_limit
    """).fetchone()
    
    row2 = conn.execute("""
        SELECT COALESCE(SUM(daily_limit - daily_sent), 0) as remaining
        FROM smtp_aliases WHERE is_active=1 AND daily_sent < daily_limit
    """).fetchone()
    
    conn.close()
    
    rem1 = row1["remaining"] if row1 else 0
    rem2 = row2["remaining"] if row2 else 0
    return rem1 + rem2


# ═══════════════════════════════════════════════════════════════
#                       CREATORS
# ═══════════════════════════════════════════════════════════════

def add_or_get_creator(email, **kwargs):
    """Add a creator, or return existing one with same email."""
    conn = get_db()
    existing = conn.execute("SELECT * FROM creators WHERE email=?", (email,)).fetchone()
    if existing:
        conn.close()
        return existing["id"], False

    fields = ["email"] + list(kwargs.keys())
    values = [email] + list(kwargs.values())
    placeholders = ",".join("?" * len(fields))
    cur = conn.execute(
        f"INSERT INTO creators ({','.join(fields)}) VALUES ({placeholders})",
        values
    )
    conn.commit()
    creator_id = cur.lastrowid
    conn.close()
    return creator_id, True


def get_creator(creator_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM creators WHERE id=?", (creator_id,)).fetchone()
    conn.close()
    return row


def get_creator_by_email(email):
    conn = get_db()
    row = conn.execute("SELECT * FROM creators WHERE email=?", (email,)).fetchone()
    conn.close()
    return row


def update_creator_stage(creator_id, stage):
    conn = get_db()
    conn.execute("UPDATE creators SET stage=?, last_contact=? WHERE id=?",
                 (stage, datetime.now().isoformat(), creator_id))
    conn.commit()
    conn.close()


def get_pipeline_breakdown():
    conn = get_db()
    rows = conn.execute(
        "SELECT stage, COUNT(*) as cnt FROM creators GROUP BY stage"
    ).fetchall()
    conn.close()
    return {r["stage"]: r["cnt"] for r in rows}


# ═══════════════════════════════════════════════════════════════
#                       EMAILS
# ═══════════════════════════════════════════════════════════════

def log_email(creator_id, from_account, to_email, subject, body, message_type="opener", status="queued"):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO emails_sent
           (creator_id, from_account, to_email, subject, body, message_type, status)
           VALUES (?,?,?,?,?,?,?)""",
        (creator_id, from_account, to_email, subject, body, message_type, status)
    )
    conn.commit()
    email_id = cur.lastrowid
    conn.close()
    return email_id


def mark_email_sent(email_id, message_id=None):
    conn = get_db()
    conn.execute(
        "UPDATE emails_sent SET status='sent', sent_at=?, message_id=? WHERE id=?",
        (datetime.now().isoformat(), message_id, email_id)
    )
    conn.commit()
    conn.close()


def mark_email_failed(email_id, error_msg):
    conn = get_db()
    conn.execute(
        "UPDATE emails_sent SET status='failed', error_msg=? WHERE id=?",
        (error_msg, email_id)
    )
    conn.commit()
    conn.close()


def email_already_sent(email_address):
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM emails_sent WHERE to_email=? AND status IN ('sent','queued')",
        (email_address,)
    ).fetchone()
    conn.close()
    return row["cnt"] > 0


def get_today_sent_count():
    conn = get_db()
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM emails_sent WHERE status='sent' AND DATE(sent_at)=?",
        (today,)
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def get_emails_for_creator(creator_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM emails_sent WHERE creator_id=? ORDER BY queued_at",
        (creator_id,)
    ).fetchall()
    conn.close()
    return rows


# ═══════════════════════════════════════════════════════════════
#                       REPLIES
# ═══════════════════════════════════════════════════════════════

def log_reply(creator_id, from_email, subject, body):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO replies (creator_id, from_email, subject, body)
           VALUES (?,?,?,?)""",
        (creator_id, from_email, subject, body)
    )
    conn.commit()
    reply_id = cur.lastrowid
    conn.close()
    return reply_id


def mark_reply_handled(reply_id, action):
    conn = get_db()
    conn.execute(
        "UPDATE replies SET handled=1, action_taken=? WHERE id=?",
        (action, reply_id)
    )
    conn.commit()
    conn.close()


def get_unhandled_replies():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM replies WHERE handled=0 ORDER BY received_at"
    ).fetchall()
    conn.close()
    return rows


def get_reply(reply_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM replies WHERE id=?", (reply_id,)).fetchone()
    conn.close()
    return row


# ═══════════════════════════════════════════════════════════════
#                   CONVERSATION HISTORY
# ═══════════════════════════════════════════════════════════════

def add_conversation_message(creator_id, role, content):
    """Role is 'us' or 'them'."""
    conn = get_db()
    conn.execute(
        "INSERT INTO conversation_history (creator_id, role, content) VALUES (?,?,?)",
        (creator_id, role, content)
    )
    conn.commit()
    conn.close()


def get_conversation(creator_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM conversation_history WHERE creator_id=? ORDER BY timestamp",
        (creator_id,)
    ).fetchall()
    conn.close()
    return rows


# ═══════════════════════════════════════════════════════════════
#                       FOLLOWUPS
# ═══════════════════════════════════════════════════════════════

def schedule_followup(creator_id, days_from_now, followup_number):
    conn = get_db()
    scheduled = (datetime.now() + timedelta(days=days_from_now)).isoformat()
    conn.execute(
        "INSERT INTO followups_scheduled (creator_id, scheduled_for, followup_number) VALUES (?,?,?)",
        (creator_id, scheduled, followup_number)
    )
    conn.commit()
    conn.close()


def get_due_followups():
    conn = get_db()
    now = datetime.now().isoformat()
    rows = conn.execute(
        """SELECT f.*, c.email, c.name, c.handle, c.tier
           FROM followups_scheduled f
           JOIN creators c ON f.creator_id = c.id
           WHERE f.status='pending' AND f.scheduled_for <= ?
           AND c.stage NOT IN ('replied', 'negotiating', 'closed_won', 'closed_lost')""",
        (now,)
    ).fetchall()
    conn.close()
    return rows


def cancel_followups_for_creator(creator_id):
    conn = get_db()
    conn.execute(
        "UPDATE followups_scheduled SET status='cancelled' WHERE creator_id=? AND status='pending'",
        (creator_id,)
    )
    conn.commit()
    conn.close()


def mark_followup_sent(followup_id):
    conn = get_db()
    conn.execute("UPDATE followups_scheduled SET status='sent' WHERE id=?", (followup_id,))
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════
#                   DM REMINDERS
# ═══════════════════════════════════════════════════════════════

def add_dm_reminder(creator_id, platform, handle):
    conn = get_db()
    conn.execute(
        "INSERT INTO dm_reminders (creator_id, platform, handle) VALUES (?,?,?)",
        (creator_id, platform, handle)
    )
    conn.commit()
    conn.close()


def get_pending_dm_reminders():
    conn = get_db()
    rows = conn.execute(
        """SELECT dr.*, c.name FROM dm_reminders dr
           JOIN creators c ON dr.creator_id = c.id
           WHERE dr.dm_sent = 0"""
    ).fetchall()
    conn.close()
    return rows


def mark_dm_done(reminder_id):
    conn = get_db()
    conn.execute("UPDATE dm_reminders SET dm_sent=1 WHERE id=?", (reminder_id,))
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════
#                   REPORTING
# ═══════════════════════════════════════════════════════════════

def get_full_report():
    conn = get_db()
    today = date.today().isoformat()

    total_sent = conn.execute("SELECT COUNT(*) as c FROM emails_sent WHERE status='sent'").fetchone()["c"]
    total_failed = conn.execute("SELECT COUNT(*) as c FROM emails_sent WHERE status='failed'").fetchone()["c"]
    total_queued = conn.execute("SELECT COUNT(*) as c FROM emails_sent WHERE status='queued'").fetchone()["c"]
    total_creators = conn.execute("SELECT COUNT(*) as c FROM creators").fetchone()["c"]
    today_sent = conn.execute(
        "SELECT COUNT(*) as c FROM emails_sent WHERE status='sent' AND DATE(sent_at)=?", (today,)
    ).fetchone()["c"]
    today_replies = conn.execute(
        "SELECT COUNT(*) as c FROM replies WHERE DATE(received_at)=?", (today,)
    ).fetchone()["c"]
    total_replies = conn.execute("SELECT COUNT(*) as c FROM replies").fetchone()["c"]

    daily = conn.execute("""
        SELECT DATE(sent_at) as day, COUNT(*) as cnt
        FROM emails_sent WHERE status='sent' AND sent_at IS NOT NULL
        GROUP BY DATE(sent_at) ORDER BY day DESC LIMIT 7
    """).fetchall()

    conn.close()
    return {
        "total_sent": total_sent,
        "total_failed": total_failed,
        "total_queued": total_queued,
        "total_creators": total_creators,
        "today_sent": today_sent,
        "today_replies": today_replies,
        "total_replies": total_replies,
        "daily_breakdown": [{"date": r["day"], "count": r["cnt"]} for r in daily],
    }


# ═══════════════════════════════════════════════════════════════
#                   PENDING ACTIONS (UI state)
# ═══════════════════════════════════════════════════════════════

def save_pending_action(user_id, action_type, context):
    """Used for multi-step flows like onboarding, reply handling."""
    conn = get_db()
    conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user_id,))
    conn.execute(
        "INSERT INTO pending_actions (user_id, action_type, context) VALUES (?,?,?)",
        (user_id, action_type, json.dumps(context))
    )
    conn.commit()
    conn.close()


def get_pending_action(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM pending_actions WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    if row:
        return {
            "action_type": row["action_type"],
            "context": json.loads(row["context"]) if row["context"] else {},
        }
    return None


def clear_pending_action(user_id):
    conn = get_db()
    conn.execute("DELETE FROM pending_actions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════
#                   CRM / DASHBOARD
# ═══════════════════════════════════════════════════════════════

def get_all_creators(limit=50, offset=0, stage_filter=None):
    """Get paginated creator list for CRM dashboard."""
    conn = get_db()
    query = "SELECT * FROM creators"
    params = []
    if stage_filter:
        query += " WHERE stage=?"
        params.append(stage_filter)
    query += " ORDER BY added_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def get_creator_full_detail(creator_id):
    """Get creator with all related emails, replies, followups for detail view."""
    conn = get_db()
    creator = conn.execute("SELECT * FROM creators WHERE id=?", (creator_id,)).fetchone()
    if not creator:
        conn.close()
        return None

    emails = conn.execute(
        "SELECT id, from_account, subject, message_type, status, sent_at, queued_at FROM emails_sent WHERE creator_id=? ORDER BY queued_at",
        (creator_id,)
    ).fetchall()

    replies = conn.execute(
        "SELECT id, subject, body, received_at, handled, action_taken FROM replies WHERE creator_id=? ORDER BY received_at",
        (creator_id,)
    ).fetchall()

    followups = conn.execute(
        "SELECT followup_number, scheduled_for, status FROM followups_scheduled WHERE creator_id=? ORDER BY followup_number",
        (creator_id,)
    ).fetchall()

    conn.close()
    return {
        "creator": creator,
        "emails": emails,
        "replies": replies,
        "followups": followups,
    }


def get_total_creator_count(stage_filter=None):
    conn = get_db()
    if stage_filter:
        row = conn.execute("SELECT COUNT(*) as c FROM creators WHERE stage=?", (stage_filter,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) as c FROM creators").fetchone()
    conn.close()
    return row["c"]


# ═══════════════════════════════════════════════════════════════
#                   REPLY CHECK TRACKING
# ═══════════════════════════════════════════════════════════════

def record_reply_check():
    """Record that we just checked for replies."""
    set_setting("last_reply_check", datetime.now().isoformat())


def get_last_reply_check():
    """Get timestamp of last reply check."""
    val = get_setting("last_reply_check")
    if val:
        try:
            return datetime.fromisoformat(val)
        except Exception:
            pass
    return None


def get_next_reply_check():
    """Get estimated time of next reply check."""
    last = get_last_reply_check()
    check_mins = int(get_setting("reply_check_minutes", "5"))
    if last:
        return last + timedelta(minutes=check_mins)
    return None


# ═══════════════════════════════════════════════════════════════
#                   QUICK ACTIONS / HOT LEADS
# ═══════════════════════════════════════════════════════════════

def get_hot_leads():
    """Creators who replied but we haven't responded to yet."""
    conn = get_db()
    rows = conn.execute("""
        SELECT c.*,
               (SELECT body FROM replies r WHERE r.creator_id=c.id ORDER BY received_at DESC LIMIT 1) as last_reply,
               (SELECT received_at FROM replies r WHERE r.creator_id=c.id ORDER BY received_at DESC LIMIT 1) as last_reply_at,
               (SELECT COUNT(*) FROM replies r WHERE r.creator_id=c.id AND r.handled=0) as unhandled_count
        FROM creators c
        WHERE c.stage IN ('replied', 'manual_handling', 'needs_info', 'interested')
              AND EXISTS (SELECT 1 FROM replies r WHERE r.creator_id=c.id AND r.handled=0)
        ORDER BY last_reply_at DESC
        LIMIT 30
    """).fetchall()
    conn.close()
    return rows


def update_creator_notes(creator_id, notes):
    conn = get_db()
    conn.execute("UPDATE creators SET notes=? WHERE id=?", (notes, creator_id))
    conn.commit()
    conn.close()


def get_today_breakdown():
    """Detailed today breakdown by stage transitions."""
    conn = get_db()
    today = date.today().isoformat()
    rows = conn.execute("""
        SELECT message_type, status, COUNT(*) as c
        FROM emails_sent
        WHERE DATE(COALESCE(sent_at, queued_at))=?
        GROUP BY message_type, status
    """, (today,)).fetchall()
    conn.close()
    return rows


def get_unhandled_reply_for_creator(creator_id):
    """Get latest unhandled reply for a creator."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM replies WHERE creator_id=? AND handled=0 ORDER BY received_at DESC LIMIT 1",
        (creator_id,)
    ).fetchone()
    conn.close()
    return row


# ── STAGE HISTORY (merged patch) ─────────────────────────────────────
def log_stage_transition(creator_id, from_stage, to_stage, changed_by="user", reason=""):
    conn = get_db()
    conn.execute("INSERT INTO stage_history (creator_id,from_stage,to_stage,changed_by,reason) VALUES (?,?,?,?,?)",
                 (creator_id, from_stage, to_stage, changed_by, reason))
    conn.commit(); conn.close()

def get_stage_history(creator_id, limit=20):
    conn = get_db()
    rows = conn.execute("SELECT * FROM stage_history WHERE creator_id=? ORDER BY changed_at DESC LIMIT ?",
                        (creator_id, limit)).fetchall()
    conn.close(); return rows


# ── SEEDS MANAGEMENT ──────────────────────────────────────────────────
def add_seed(username: str, source: str = "manual") -> bool:
    """Add a seed account. Returns True if newly added, False if already exists."""
    username = username.lstrip("@").strip().lower()
    if not username:
        return False
    conn = get_db()
    try:
        conn.execute("INSERT OR IGNORE INTO seeds (username, source) VALUES (?, ?)", (username, source))
        # Reactivate if it was deactivated
        conn.execute("UPDATE seeds SET active=1 WHERE username=?", (username,))
        conn.commit()
        changed = conn.total_changes > 0
    except Exception:
        changed = False
    conn.close()
    return changed

def remove_seed(username: str) -> bool:
    """Deactivate a seed. Returns True if found and deactivated."""
    username = username.lstrip("@").strip().lower()
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE seeds SET active=0 WHERE username=? AND active=1", (username,))
    changed = c.rowcount > 0
    conn.commit()
    conn.close()
    return changed

def get_active_seeds() -> list:
    """Return list of active seed dicts."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM seeds WHERE active=1 ORDER BY added_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_seed_scan_stats(username: str, profiles_found: int):
    """Update last_scanned and profiles_found for a seed."""
    conn = get_db()
    conn.execute(
        "UPDATE seeds SET last_scanned=CURRENT_TIMESTAMP, profiles_found=profiles_found+? WHERE username=?",
        (profiles_found, username.lstrip("@").strip().lower())
    )
    conn.commit()
    conn.close()


# ── DASHBOARD QUERIES ─────────────────────────────────────────────────
def get_stage_counts() -> dict:
    """Return dict of {stage: count} for all creators."""
    conn = get_db()
    rows = conn.execute("SELECT stage, COUNT(*) as c FROM creators GROUP BY stage").fetchall()
    conn.close()
    return {r["stage"]: r["c"] for r in rows}

def get_dm_queue_stats() -> dict:
    """Return DM queue stats: pending, sent_today, failed, next_scheduled."""
    conn = get_db()
    import time
    now = time.time()
    today_start = now - (now % 86400)  # approx midnight UTC
    
    pending = conn.execute("SELECT COUNT(*) as c FROM dm_queue WHERE status='pending'").fetchone()["c"]
    sent_today = conn.execute(
        "SELECT COUNT(*) as c FROM dm_queue WHERE status='sent' AND sent_at>=?", (today_start,)
    ).fetchone()["c"]
    failed = conn.execute("SELECT COUNT(*) as c FROM dm_queue WHERE status='failed'").fetchone()["c"]
    
    next_dm = conn.execute(
        "SELECT username, scheduled_time FROM dm_queue WHERE status='pending' ORDER BY scheduled_time ASC LIMIT 1"
    ).fetchone()
    
    conn.close()
    return {
        "pending": pending,
        "sent_today": sent_today,
        "failed": failed,
        "next_dm": dict(next_dm) if next_dm else None
    }

def get_creators_with_email_count() -> tuple:
    """Return (total_creators, creators_with_email)."""
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) as c FROM creators").fetchone()["c"]
    with_email = conn.execute(
        "SELECT COUNT(*) as c FROM creators WHERE email IS NOT NULL AND email NOT LIKE 'no_email_%'"
    ).fetchone()["c"]
    conn.close()
    return total, with_email

def get_hot_leads_count() -> int:
    """Count creators in 'replied' or 'negotiating' stage that need attention."""
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) as c FROM creators WHERE stage IN ('replied','negotiating')"
    ).fetchone()["c"]
    conn.close()
    return count

def get_active_seeds_count() -> int:
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) as c FROM seeds WHERE active=1").fetchone()["c"]
    conn.close()
    return count

def get_ai_keys_status() -> dict:
    """Check which AI API keys are configured."""
    keys = {}
    for k in ["gemini_api_key", "mistral_api_key", "groq_api_key", "nvidia_api_key"]:
        val = get_setting(k, "")
        keys[k.replace("_api_key", "")] = bool(val)
    # Also check env/config fallbacks
    from config import GEMINI_API_KEY, MISTRAL_API_KEY, GROQ_API_KEY, NVIDIA_API_KEY
    if GEMINI_API_KEY: keys["gemini"] = True
    if MISTRAL_API_KEY: keys["mistral"] = True
    if GROQ_API_KEY: keys["groq"] = True
    if NVIDIA_API_KEY: keys["nvidia"] = True
    return keys

def get_active_cookies_count() -> int:
    conn = get_db()
    ig = 0
    dm = 0
    try:
        ig = conn.execute("SELECT COUNT(*) as c FROM ig_cookies WHERE active=1").fetchone()["c"]
    except Exception:
        pass
    try:
        dm = conn.execute("SELECT COUNT(*) as c FROM dm_cookies WHERE active=1").fetchone()["c"]
    except Exception:
        pass
    conn.close()
    return ig + dm

def get_active_apify_count() -> int:
    conn = get_db()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM apify_tokens WHERE active=1").fetchone()["c"]
    except Exception:
        count = 0
    conn.close()
    return count

