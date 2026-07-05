"""
DM Automation with Human-Like Scheduling.

Features:
- Random intervals between DMs (configurable min/max, default 7-18 minutes)
- "Break" pauses after every 3-5 DMs (30-60 min break, simulates human behavior)
- Daily active hours window (default 9 AM - 11 PM)
- Typing simulation delay (2-8 seconds before each send)
- Uses full AI chain for DM generation (not just Groq)
"""

import time
import logging
import random
import threading
import json
import requests
import database as db
import ai_router

logger = logging.getLogger("dm_automation")

# ── Defaults ──────────────────────────────────────────────────────
DEFAULT_DM_MIN_INTERVAL = 7    # minutes
DEFAULT_DM_MAX_INTERVAL = 18   # minutes
DEFAULT_DM_BREAK_AFTER = 4     # send N DMs then take a break
DEFAULT_DM_BREAK_MIN = 30      # break duration min (minutes)
DEFAULT_DM_BREAK_MAX = 60      # break duration max (minutes)
DEFAULT_DM_ACTIVE_START = 9    # hour (24h format)
DEFAULT_DM_ACTIVE_END = 23     # hour (24h format)
DEFAULT_TYPING_DELAY_MIN = 2   # seconds
DEFAULT_TYPING_DELAY_MAX = 8   # seconds


def _get_dm_setting(key, default):
    """Get a DM setting from DB or return default."""
    val = db.get_setting(key, None)
    if val is not None:
        try:
            return int(val) if isinstance(default, int) else float(val)
        except (ValueError, TypeError):
            pass
    return default


def generate_personalized_dm(creator_data: dict) -> str:
    """Uses full AI chain to generate a personalized DM opener for a creator."""
    system_prompt = (
        "You are a friendly partnership manager reaching out to Instagram creators. "
        "Keep the message short, casual, and under 250 characters. No formal greetings like 'Dear'. "
        "Just a quick opener asking if they are open to a collab (upfront fee + commission). "
        "Make it highly personalized to their niche or bio. "
        "Sound like a real person typing casually, not a marketer. No emojis overload."
    )
    user_prompt = f"Profile Data: {json.dumps(creator_data, ensure_ascii=False)}"

    # Use full AI chain instead of just Groq
    result, fail_reason = ai_router._try_chain(system_prompt, user_prompt, expect_json=False)
    if result:
        # Clean up quotes if present
        cleaned = result.strip('"').strip("'").strip()
        if len(cleaned) > 10:
            return cleaned

    logger.warning(f"DM generation failed ({fail_reason}), using fallback template")
    return "Hey! Love your content. Are you open to a paid collab right now? Let me know!"


def _calculate_human_schedule(num_dms: int) -> list:
    """
    Calculate a list of scheduled timestamps for N DMs with human-like timing.
    Returns list of Unix timestamps.
    """
    min_interval = _get_dm_setting("dm_min_interval", DEFAULT_DM_MIN_INTERVAL) * 60
    max_interval = _get_dm_setting("dm_max_interval", DEFAULT_DM_MAX_INTERVAL) * 60
    break_after = _get_dm_setting("dm_break_after", DEFAULT_DM_BREAK_AFTER)
    break_min = _get_dm_setting("dm_break_min", DEFAULT_DM_BREAK_MIN) * 60
    break_max = _get_dm_setting("dm_break_max", DEFAULT_DM_BREAK_MAX) * 60

    now = time.time()
    timestamps = []
    current_time = now
    consecutive = 0

    for i in range(num_dms):
        # Add random interval
        delay = random.uniform(min_interval, max_interval)

        # Add micro-jitter (±30 seconds) for extra randomness
        jitter = random.uniform(-30, 30)
        delay += jitter

        # Check if we need a break
        consecutive += 1
        if consecutive >= break_after:
            # Take a break! (randomize the break count slightly)
            actual_break_after = random.randint(max(2, break_after - 1), break_after + 1)
            if consecutive >= actual_break_after:
                break_duration = random.uniform(break_min, break_max)
                delay += break_duration
                consecutive = 0
                logger.info(f"DM schedule: inserting {break_duration/60:.0f}min break after DM #{i+1}")

        current_time += delay
        timestamps.append(current_time)

    return timestamps


def queue_bulk_campaign(creator_ids: list):
    """Queues a list of creators for staggered DM sending with human-like timing."""
    conn = db.get_db()
    c = conn.cursor()

    # Calculate human-like schedule
    schedule = _calculate_human_schedule(len(creator_ids))

    queued = 0
    for idx, cid in enumerate(creator_ids):
        c.execute(
            "SELECT handle, profile_url, tags, location, is_business, post_count, engagement_rate, bio, name "
            "FROM creators WHERE id=?", (cid,)
        )
        row = c.fetchone()
        if not row:
            continue

        username = row[0]
        cdata = {
            "username": username,
            "name": row[8] or username,
            "tags": row[2] or "",
            "location": row[3] or "",
            "is_business": row[4],
            "engagement_rate": row[6],
            "bio": row[7] or "",
        }

        message = generate_personalized_dm(cdata)
        sched_time = schedule[idx] if idx < len(schedule) else time.time() + (idx * 600)

        c.execute("""
            INSERT INTO dm_queue (creator_id, username, message_text, scheduled_time, status)
            VALUES (?, ?, ?, ?, 'pending')
        """, (cid, username, message, sched_time))

        queued += 1

    conn.commit()
    conn.close()

    if queued > 0:
        total_time_min = (schedule[-1] - time.time()) / 60 if schedule else 0
        logger.info(
            f"Queued {queued} DMs with human-like scheduling. "
            f"Estimated completion: {total_time_min:.0f} minutes"
        )
    return queued


def _is_within_active_hours() -> bool:
    """Check if current time is within the active DM sending window."""
    from datetime import datetime
    now = datetime.now()
    start_hour = _get_dm_setting("dm_active_start", DEFAULT_DM_ACTIVE_START)
    end_hour = _get_dm_setting("dm_active_end", DEFAULT_DM_ACTIVE_END)
    return start_hour <= now.hour < end_hour


def _send_dm(sessionid: str, csrftoken: str, username: str, text: str) -> bool:
    """Calls Instagram internal API to send a DM. Returns True if successful."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "X-CSRFToken": csrftoken,
        "Cookie": f"sessionid={sessionid}; csrftoken={csrftoken};"
    }

    try:
        # Simulate typing delay (2-8 seconds)
        typing_delay = random.uniform(DEFAULT_TYPING_DELAY_MIN, DEFAULT_TYPING_DELAY_MAX)
        time.sleep(typing_delay)

        # 1. Resolve username to user ID
        res = requests.get(
            f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}",
            headers=headers, timeout=10
        )
        if res.status_code != 200:
            logger.error(f"DM Resolver failed for {username}: {res.status_code}")
            return False

        data = res.json()
        target_id = data.get("data", {}).get("user", {}).get("id")
        if not target_id:
            return False

        # Small pause between resolve and send (human-like)
        time.sleep(random.uniform(1, 3))

        # 2. Send DM
        payload = {
            "recipient_users": f"[[{target_id}]]",
            "action": "send_item",
            "is_shh_mode": "0",
            "send_attribution": "direct_thread",
            "client_context": str(int(time.time() * 1000)) + str(random.randint(1000, 9999)),
            "text": text,
        }

        post_headers = headers.copy()
        post_headers["Content-Type"] = "application/x-www-form-urlencoded"

        send_res = requests.post(
            "https://i.instagram.com/api/v1/direct_v2/threads/broadcast/text/",
            headers=post_headers, data=payload, timeout=10
        )
        if send_res.status_code == 200:
            return True

        logger.error(f"DM send failed for {username}: {send_res.text}")
        return False

    except Exception as e:
        logger.error(f"DM exception for {username}: {e}")
        return False


def dm_worker_loop():
    """Background thread that checks for and sends DMs with human-like behavior."""
    dms_since_break = 0

    while True:
        try:
            # Check active hours
            if not _is_within_active_hours():
                logger.debug("Outside active DM hours, sleeping 15 minutes...")
                time.sleep(900)  # Check again in 15 minutes
                continue

            conn = db.get_db()
            c = conn.cursor()

            now = time.time()
            c.execute(
                "SELECT id, creator_id, username, message_text FROM dm_queue "
                "WHERE status='pending' AND scheduled_time <= ? "
                "ORDER BY scheduled_time ASC LIMIT 1",
                (now,)
            )
            task = c.fetchone()

            if task:
                task_id, cid, username, message = task

                # Fetch a valid DM cookie
                c.execute(
                    "SELECT id, sessionid, csrftoken FROM dm_cookies "
                    "WHERE active=1 ORDER BY last_used ASC NULLS FIRST LIMIT 1"
                )
                cookie = c.fetchone()

                if cookie:
                    cookie_id, sessionid, csrftoken = cookie
                    logger.info(f"Sending DM to @{username} using cookie #{cookie_id}")

                    success = _send_dm(sessionid, csrftoken, username, message)

                    if success:
                        c.execute(
                            "UPDATE dm_queue SET status='sent', sent_at=?, cookie_id_used=? WHERE id=?",
                            (time.time(), cookie_id, task_id)
                        )
                        c.execute("UPDATE dm_cookies SET last_used=? WHERE id=?", (time.time(), cookie_id))
                        c.execute("UPDATE creators SET last_dm_sent_at=? WHERE id=?", (time.time(), cid))
                        dms_since_break += 1
                        logger.info(f"DM sent to @{username} successfully (#{dms_since_break} since break)")
                    else:
                        c.execute(
                            "UPDATE dm_queue SET status='failed', error_msg='API rejected or resolve failed' WHERE id=?",
                            (task_id,)
                        )
                else:
                    logger.warning("No active DM cookies available in dm_cookies table!")

            conn.commit()
            conn.close()

            # Human-like break pattern
            break_after = _get_dm_setting("dm_break_after", DEFAULT_DM_BREAK_AFTER)
            if dms_since_break >= break_after:
                break_min = _get_dm_setting("dm_break_min", DEFAULT_DM_BREAK_MIN)
                break_max = _get_dm_setting("dm_break_max", DEFAULT_DM_BREAK_MAX)
                break_duration = random.uniform(break_min * 60, break_max * 60)
                logger.info(f"Taking a human-like break for {break_duration/60:.0f} minutes after {dms_since_break} DMs")
                time.sleep(break_duration)
                dms_since_break = 0
            else:
                # Random check interval (30-90 seconds)
                time.sleep(random.uniform(30, 90))

        except Exception as e:
            logger.error(f"DM worker error: {e}")
            time.sleep(60)


def start_dm_worker():
    t = threading.Thread(target=dm_worker_loop, daemon=True)
    t.start()


def get_queue_summary() -> str:
    """Get a formatted summary of the DM queue."""
    stats = db.get_dm_queue_stats()
    lines = [
        f"📨 <b>DM Queue Status</b>\n",
        f"⏳ Pending: {stats['pending']}",
        f"✅ Sent today: {stats['sent_today']}",
        f"❌ Failed: {stats['failed']}",
    ]

    if stats["next_dm"]:
        next_time = stats["next_dm"]["scheduled_time"]
        mins_until = max(0, (next_time - time.time()) / 60)
        lines.append(f"⏱ Next DM: @{stats['next_dm']['username']} in ~{mins_until:.0f} min")

    # Show current interval settings
    min_int = _get_dm_setting("dm_min_interval", DEFAULT_DM_MIN_INTERVAL)
    max_int = _get_dm_setting("dm_max_interval", DEFAULT_DM_MAX_INTERVAL)
    lines.append(f"\n⚙️ Interval: {min_int}-{max_int} min (random)")

    active_start = _get_dm_setting("dm_active_start", DEFAULT_DM_ACTIVE_START)
    active_end = _get_dm_setting("dm_active_end", DEFAULT_DM_ACTIVE_END)
    lines.append(f"🕐 Active hours: {active_start}:00 - {active_end}:00")

    return "\n".join(lines)


def retry_failed_dms() -> int:
    """Re-queue all failed DMs with new human-like scheduling."""
    conn = db.get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM dm_queue WHERE status='failed'")
    failed = c.fetchall()

    if not failed:
        conn.close()
        return 0

    schedule = _calculate_human_schedule(len(failed))
    count = 0
    for idx, row in enumerate(failed):
        sched_time = schedule[idx] if idx < len(schedule) else time.time() + (idx * 600)
        c.execute(
            "UPDATE dm_queue SET status='pending', scheduled_time=?, error_msg=NULL WHERE id=?",
            (sched_time, row["id"])
        )
        count += 1

    conn.commit()
    conn.close()
    logger.info(f"Re-queued {count} failed DMs with human-like scheduling")
    return count
