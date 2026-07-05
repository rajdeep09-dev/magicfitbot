"""
Reply watcher - polls Gmail via IMAP to detect replies from creators.
Runs in background, checks every N minutes.
When a reply is detected from a creator in DB, notifies the user via Telegram.
"""

import imaplib
import email
import logging
import time
import threading
import re
from email.header import decode_header
from datetime import datetime, timedelta

import database as db

logger = logging.getLogger(__name__)

_watcher_running = False
_watcher_thread = None
_last_check_times = {}


def start_reply_watcher(notify_callback):
    """Start watcher in background. notify_callback(reply_dict) called on new reply."""
    global _watcher_thread, _watcher_running
    if _watcher_running:
        return "Reply watcher already running"
    _watcher_running = True
    _watcher_thread = threading.Thread(target=_watch_loop, args=(notify_callback,), daemon=True)
    _watcher_thread.start()
    return "Reply watcher started"


def stop_reply_watcher():
    global _watcher_running
    _watcher_running = False
    return "Reply watcher stopping"


def is_running():
    return _watcher_running


def check_now(notify_callback=None):
    """Manual one-time reply check. Returns count of new replies found."""
    new_replies = []
    def capture_callback(reply_data):
        new_replies.append(reply_data)
        if notify_callback:
            notify_callback(reply_data)
    try:
        _check_all_accounts(capture_callback)
        db.record_reply_check()
    except Exception as e:
        logger.error(f"Manual reply check error: {e}")
    return new_replies


def _watch_loop(notify_callback):
    global _watcher_running
    while _watcher_running:
        try:
            check_minutes = int(db.get_setting("reply_check_minutes", "5"))
            _check_all_accounts(notify_callback)
            db.record_reply_check()
        except Exception as e:
            logger.error(f"Reply watcher error: {e}")

        # Sleep in 5-second chunks so we can stop cleanly
        sleep_total = check_minutes * 60
        for _ in range(sleep_total):
            if not _watcher_running:
                break
            time.sleep(1)


def _check_all_accounts(notify_callback):
    """Check all active Gmail accounts for new replies."""
    accounts = db.get_all_accounts()
    for account in accounts:
        if not account["active"]:
            continue
        try:
            _check_account(account, notify_callback)
        except Exception as e:
            logger.warning(f"Failed to check {account['email']}: {e}")


def _check_account(account, notify_callback):
    """Check a single Gmail account for new replies from creators we've emailed."""
    email_addr = account["email"]
    password = account["app_password"]

    # Determine search window
    last_check = _last_check_times.get(email_addr)
    if not last_check:
        # First check, look at last hour only
        last_check = datetime.now() - timedelta(hours=1)

    try:
        imaplib.IMAP4_SSL.timeout = 30
        with imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30) as mail:
            mail.login(email_addr, password)
            mail.select("inbox")

            # Search both seen and unseen since we last checked
            since_date = last_check.strftime("%d-%b-%Y")
            status, data = mail.search(None, f'(SINCE "{since_date}")')

            if status != "OK":
                return

            msg_ids = data[0].split()
            # Only process last 50 to avoid huge backlog
            for msg_id in msg_ids[-50:]:
                try:
                    _process_message(mail, msg_id, email_addr, notify_callback)
                except Exception as e:
                    logger.warning(f"Failed to process message {msg_id}: {e}")

            _last_check_times[email_addr] = datetime.now()

    except imaplib.IMAP4.error as e:
        logger.warning(f"IMAP error for {email_addr}: {e}")
    except Exception as e:
        logger.warning(f"Error checking {email_addr}: {e}")


def _process_message(mail, msg_id, our_email, notify_callback):
    """Process a single incoming message - check if it's a reply from a creator."""
    status, msg_data = mail.fetch(msg_id, "(RFC822)")
    if status != "OK":
        return

    raw_email = msg_data[0][1]
    msg = email.message_from_bytes(raw_email)

    # Extract sender email and To header (for alias mapping)
    from_header = msg.get("From", "")
    sender_email = _extract_email_from_header(from_header)
    to_header = msg.get("To", "")
    to_email = _extract_email_from_header(to_header)
    if not sender_email:
        return

    # Check if this sender is a creator we've emailed
    creator = db.get_creator_by_email(sender_email)
    if not creator:
        normalized = _normalize_gmail(sender_email)
        creator = db.get_creator_by_email(normalized)
    if not creator:
        return  # Not a creator in our pipeline

    # Extract subject
    subject = _decode_header_value(msg.get("Subject", ""))

    # Extract body
    body = _extract_body(msg)

    # Skip auto-replies / out-of-office
    if _is_auto_reply(subject, body):
        logger.info(f"Skipping auto-reply from {sender_email}")
        return

    # Check if we already logged this exact message
    if _reply_already_logged(creator["id"], subject, body):
        return

    # Log the reply
    reply_id = db.log_reply(creator["id"], sender_email, subject, body)
    db.add_conversation_message(creator["id"], "them", f"Subject: {subject}\n\n{body}")
    db.update_creator_stage(creator["id"], "replied")

    # Cancel any pending follow-ups since they replied
    db.cancel_followups_for_creator(creator["id"])

    # Generate draft using OpenRouter / AI chain
    try:
        import ai_router
        conversation = db.get_conversation(creator["id"])
        draft_res = ai_router.generate_reply(
            creator_info=creator,
            conversation=conversation,
            their_latest_reply=body,
            instruction="Close the creator partnership deal"
        )
        ai_draft = draft_res.get("body", "Failed to generate draft.")
    except Exception as e:
        logger.error(f"Failed to generate AI draft: {e}")
        ai_draft = "Draft generation failed."

    # Notify user via Telegram
    if notify_callback:
        try:
            notify_callback({
                "reply_id": reply_id,
                "creator_id": creator["id"],
                "creator_name": creator["name"],
                "creator_handle": creator["handle"],
                "from_email": sender_email,
                "to_email": to_email,
                "subject": subject,
                "body": body[:1000],  # truncate for Telegram
                "ai_draft": ai_draft,
            })
        except Exception as e:
            logger.warning(f"Failed to notify on reply: {e}")


def _extract_email_from_header(header_value: str) -> str | None:
    """Extract just the email address from a 'Name <email@example.com>' header."""
    match = re.search(r'[\w._%+\-]+@[\w.\-]+\.\w+', header_value)
    return match.group(0).lower() if match else None


def _decode_header_value(value: str) -> str:
    """Decode email header (handles UTF-8, etc.)."""
    if not value:
        return ""
    try:
        parts = decode_header(value)
        result = ""
        for part, encoding in parts:
            if isinstance(part, bytes):
                result += part.decode(encoding or "utf-8", errors="replace")
            else:
                result += part
        return result
    except Exception:
        return value


def _extract_body(msg) -> str:
    """Extract plain text body from email message."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            if content_type == "text/plain" and "attachment" not in content_disposition:
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body = payload.decode(charset, errors="replace")
                        break
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace")
        except Exception:
            body = str(msg.get_payload())

    # Strip quoted reply text
    body = _strip_quoted(body)
    return body.strip()


def _strip_quoted(body: str) -> str:
    """Remove quoted parts of the email (the > stuff and 'On X wrote:' headers)."""
    lines = body.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        # Stop at common reply indicators
        if re.match(r'^On .+ wrote:\s*$', stripped):
            break
        if stripped.startswith("From:") and "@" in stripped:
            break
        if stripped == "--" or stripped.startswith("-- "):
            break
        cleaned.append(line)
    return "\n".join(cleaned)


def _is_auto_reply(subject: str, body: str) -> bool:
    """Detect common auto-reply patterns."""
    subj_lower = (subject or "").lower()
    body_lower = (body or "").lower()
    auto_patterns = [
        "out of office", "automatic reply", "auto reply", "auto-reply",
        "vacation reply", "currently away", "on vacation", "i am out",
        "delivery status", "undeliverable", "mail delivery failed",
    ]
    for pattern in auto_patterns:
        if pattern in subj_lower or pattern in body_lower[:300]:
            return True
    return False


def _reply_already_logged(creator_id: int, subject: str, body: str) -> bool:
    """Dedup based on subject + body hash, not just creator id."""
    import hashlib
    from database import get_db
    msg_hash = hashlib.md5(f"{subject}|{body[:500]}".encode("utf-8", errors="replace")).hexdigest()
    conn = get_db()
    row = conn.execute("""
        SELECT body, subject FROM replies WHERE creator_id=?
    """, (creator_id,)).fetchall()
    conn.close()
    for r in row:
        existing_hash = hashlib.md5(f"{r['subject'] or ''}|{(r['body'] or '')[:500]}".encode("utf-8", errors="replace")).hexdigest()
        if existing_hash == msg_hash:
            return True
    return False


def _normalize_gmail(email):
    local, _, domain = email.partition("@")
    if domain.lower() in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "").lower().split("+")[0]
    return f"{local}@{domain.lower()}"
