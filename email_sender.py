"""
Email sender with multi-account rotation.
- Sends via Gmail SMTP using app passwords
- Rotates between active accounts
- Respects per-account daily limits
- Random intervals between sends
"""

import smtplib
import time
import random
import threading
import logging
import uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, make_msgid

import database as db

logger = logging.getLogger(__name__)

_queue_running = False
_queue_thread = None


def send_email_now(to_email: str, subject: str, body: str, account_dict: dict = None) -> dict:
    """
    Send an email immediately. If account not specified, picks next available.
    Returns: {success, account_used, error, message_id}
    """
    if not account_dict:
        account_dict = db.get_next_available_account()
        if not account_dict:
            return {"success": False, "error": "No active accounts or aliases with capacity remaining today"}

    from_email = account_dict["from_email"]
    smtp_user = account_dict["smtp_user"]
    smtp_pass = account_dict["smtp_pass"]

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = formataddr(("Rajdeep", from_email))
        msg["To"] = to_email
        msg["Subject"] = subject
        # If it's a gmail master account, create message_id for it, if alias, maybe the same
        domain = from_email.split("@")[1] if "@" in from_email else "gmail.com"
        message_id = make_msgid(domain=domain)
        msg["Message-ID"] = message_id
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(from_email, [to_email], msg.as_string())

        db.increment_account_sent(account_dict["id"], account_dict["type"] == "alias")
        return {
            "success": True,
            "account_used": from_email,
            "message_id": message_id,
        }

    except smtplib.SMTPAuthenticationError:
        return {"success": False, "error": f"Auth failed for {smtp_user}. Check app password.", "account_used": from_email}
    except smtplib.SMTPRecipientsRefused:
        return {"success": False, "error": f"Recipient refused: {to_email}", "account_used": from_email}
    except Exception as e:
        return {"success": False, "error": f"Send error: {str(e)}", "account_used": from_email}


def send_with_logging(creator_id: int, to_email: str, subject: str, body: str, message_type: str = "opener") -> dict:
    """Send an email and log it in the database."""
    # Pick account
    account = db.get_next_available_account()
    if not account:
        # No capacity, queue it
        email_id = db.log_email(creator_id, None, to_email, subject, body, message_type, status="queued")
        return {
            "success": False,
            "queued": True,
            "email_id": email_id,
            "message": "All accounts at daily limit. Queued for tomorrow.",
        }

    # Log as queued first
    email_id = db.log_email(creator_id, account["from_email"], to_email, subject, body, message_type, status="queued")

    # Send
    result = send_email_now(to_email, subject, body, account)

    if result["success"]:
        db.mark_email_sent(email_id, result.get("message_id"))
        # Log to conversation history
        db.add_conversation_message(creator_id, "us", f"[{message_type.upper()}] Subject: {subject}\n\n{body}")
        return {
            "success": True,
            "email_id": email_id,
            "account_used": result["account_used"],
            "message_id": result.get("message_id"),
        }
    else:
        db.mark_email_failed(email_id, result.get("error", "Unknown error"))
        return {
            "success": False,
            "email_id": email_id,
            "error": result.get("error"),
        }


def process_queue(status_callback=None):
    """Process queued emails with random intervals."""
    global _queue_running
    _queue_running = True

    def notify(msg):
        if status_callback:
            try:
                status_callback(msg)
            except Exception:
                logger.info(f"Queue: {msg}")

    min_interval = int(db.get_setting("min_interval_seconds", "120"))
    max_interval = int(db.get_setting("max_interval_seconds", "420"))

    while _queue_running:
        # Get all queued emails
        from database import get_db
        conn = get_db()
        queued = conn.execute(
            "SELECT * FROM emails_sent WHERE status='queued' ORDER BY queued_at LIMIT 100"
        ).fetchall()
        conn.close()

        if not queued:
            notify("Queue empty. All caught up!")
            break

        remaining = db.total_remaining_today()
        if remaining <= 0:
            notify("All Gmail accounts hit their daily limit. Will resume tomorrow.")
            break

        for email_row in queued:
            if not _queue_running:
                break

            remaining = db.total_remaining_today()
            if remaining <= 0:
                notify(f"Daily limit reached across all accounts. {len(queued)} remaining in queue.")
                break

            email_id = email_row["id"]
            to_email = email_row["to_email"]
            subject = email_row["subject"]
            body = email_row["body"]

            notify(f"Sending to {to_email}...")
            account = db.get_next_available_account()
            if not account:
                notify("No accounts available, pausing.")
                break

            result = send_email_now(to_email, subject, body, account)

            if result["success"]:
                db.mark_email_sent(email_id, result.get("message_id"))
                # Update from_account in case it changed
                from database import get_db
                conn = get_db()
                conn.execute("UPDATE emails_sent SET from_account=? WHERE id=?", (account["from_email"], email_id))
                conn.commit()
                conn.close()
                notify(f"Sent to {to_email} from {account['from_email']}")
            else:
                db.mark_email_failed(email_id, result.get("error"))
                notify(f"Failed: {to_email} - {result.get('error')}")

            # Random delay
            delay = random.randint(min_interval, max_interval)
            notify(f"Waiting {delay}s before next send...")
            for _ in range(delay):
                if not _queue_running:
                    break
                time.sleep(1)

    _queue_running = False
    notify("Queue processor stopped.")


def start_queue_processor(status_callback=None):
    """Start background queue processor."""
    global _queue_thread, _queue_running
    if _queue_running:
        return "Queue already running"
    _queue_thread = threading.Thread(target=process_queue, args=(status_callback,), daemon=True)
    _queue_thread.start()
    return "Queue processor started"


def stop_queue_processor():
    global _queue_running
    _queue_running = False
    return "Stopping queue..."


def is_queue_running():
    return _queue_running
