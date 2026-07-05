"""
Followup scheduler - background task that sends follow-up emails when due.
Default: followup 1 after 3 days, followup 2 after 2 more days, total 3 followups.
Stops if creator replies.
"""

import time
import threading
import logging
import json
import database as db
import ai_router
import email_sender

logger = logging.getLogger(__name__)

_scheduler_running = False
_scheduler_thread = None


def start_scheduler(notify_callback=None):
    global _scheduler_thread, _scheduler_running
    if _scheduler_running:
        return "Followup scheduler already running"
    _scheduler_running = True
    _scheduler_thread = threading.Thread(target=_run_loop, args=(notify_callback,), daemon=True)
    _scheduler_thread.start()
    return "Followup scheduler started"


def stop_scheduler():
    global _scheduler_running
    _scheduler_running = False
    return "Stopping followup scheduler"


def is_running():
    return _scheduler_running


def _run_loop(notify_callback):
    global _scheduler_running
    while _scheduler_running:
        try:
            _check_due_followups(notify_callback)
        except Exception as e:
            logger.error(f"Followup scheduler error: {e}")

        # Check every 10 minutes
        for _ in range(600):
            if not _scheduler_running:
                break
            time.sleep(1)


def _check_due_followups(notify_callback):
    """Find followups that are due and send them."""
    due = db.get_due_followups()
    if not due:
        return

    for followup in due:
        try:
            _send_followup(followup, notify_callback)
        except Exception as e:
            logger.warning(f"Failed to send followup for creator {followup['creator_id']}: {e}")


def _send_followup(followup_row, notify_callback):
    """Send a single followup email."""
    creator_id = followup_row["creator_id"]
    followup_num = followup_row["followup_number"]

    creator = db.get_creator(creator_id)
    if not creator:
        db.mark_followup_sent(followup_row["id"])
        return

    # Don't follow up if they already replied
    if creator["stage"] in ("replied", "negotiating", "closed_won", "closed_lost"):
        db.mark_followup_sent(followup_row["id"])
        return

    # Get previous emails for context
    previous_emails = db.get_emails_for_creator(creator_id)

    # Generate followup
    creator_info = {
        "name": creator["name"],
        "handle": creator["handle"],
        "followers": creator["followers"],
        "tier": creator["tier"],
        "bio": creator["bio"],
        "niche": creator["niche"],
    }
    email_data = ai_router.generate_followup(creator_info, previous_emails, followup_num)

    # Send
    result = email_sender.send_with_logging(
        creator_id=creator_id,
        to_email=creator["email"],
        subject=email_data["subject"],
        body=email_data["body"],
        message_type=f"followup_{followup_num}",
    )

    if result["success"]:
        db.mark_followup_sent(followup_row["id"])
        db.update_creator_stage(creator_id, f"followup_{followup_num}_sent")

        # Schedule next followup if not at max
        followup_days = json.loads(db.get_setting("followup_days", "[3, 2]"))
        max_followups = int(db.get_setting("max_followups", "3"))

        if followup_num < max_followups:
            # followup_days = [days after opener for FU1, days after FU1 for FU2, ...]
            # followup_num=1 already sent, next is FU2 which uses followup_days[1] if it exists
            next_idx = followup_num  # FU2 uses index 1, FU3 would use index 2
            if next_idx < len(followup_days):
                days = followup_days[next_idx]
            else:
                days = followup_days[-1]  # use last value if we ran out
            db.schedule_followup(creator_id, days, followup_num + 1)

        if notify_callback:
            try:
                notify_callback({
                    "type": "followup_sent",
                    "creator_name": creator["name"],
                    "creator_email": creator["email"],
                    "followup_number": followup_num,
                })
            except Exception:
                pass
    else:
        logger.warning(f"Followup send failed for {creator['email']}: {result.get('error')}")


def schedule_first_followup(creator_id):
    """Schedule the first followup after an opener is sent."""
    followup_days = json.loads(db.get_setting("followup_days", "[3, 2]"))
    days = followup_days[0] if followup_days else 3
    db.schedule_followup(creator_id, days, 1)
