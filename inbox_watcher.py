import time
import logging
import threading
import requests
import database as db

logger = logging.getLogger("inbox_watcher")

def fetch_recent_inbox(sessionid: str, csrftoken: str):
    """Fetches the recent Instagram inbox threads using internal API."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "X-CSRFToken": csrftoken,
        "Cookie": f"sessionid={sessionid}; csrftoken={csrftoken};",
        "Accept": "application/json"
    }
    
    try:
        # Fetch Inbox
        res = requests.get("https://i.instagram.com/api/v1/direct_v2/inbox/?persistentBadging=true&folder=&thread_message_limit=10", headers=headers, timeout=15)
        if res.status_code != 200:
            logger.error(f"Inbox fetch failed: {res.status_code}")
            return None
            
        data = res.json()
        threads = data.get("inbox", {}).get("threads", [])
        return threads
    except Exception as e:
        logger.error(f"Inbox exception: {e}")
        return None

def sync_replies(_app):
    """Checks the inbox, matches with CRM, and updates stages to Replied."""
    conn = db.get_db()
    c = conn.cursor()
    
    # 1. Get an active DM cookie
    c.execute("SELECT sessionid, csrftoken FROM dm_cookies WHERE active=1 ORDER BY last_used ASC LIMIT 1")
    cookie = c.fetchone()
    
    if not cookie:
        conn.close()
        return
        
    sessionid, csrftoken = cookie
    threads = fetch_recent_inbox(sessionid, csrftoken)
    
    if threads is None:
        conn.close()
        return
        
    for thread in threads:
        # Check if the last message in the thread is from them
        last_msg = thread.get("last_permanent_item", {})
        sender_id = last_msg.get("user_id")
        
        users = thread.get("users", [])
        if not users: continue
        
        # Determine the other person's username
        other_user = users[0]
        other_username = other_user.get("username")
        other_id = other_user.get("pk")
        
        # If the last message was sent by the other person (not us)
        if str(sender_id) == str(other_id):
            msg_text = last_msg.get("text", "")
            
            # Match with CRM
            c.execute("SELECT id, stage FROM creators WHERE username=?", (other_username,))
            row = c.fetchone()
            
            if row:
                cid, stage = row
                if stage != "Replied":
                    logger.info(f"Detected reply from {other_username}! Updating CRM.")
                    c.execute("UPDATE creators SET stage='Replied' WHERE id=?", (cid,))
                    db.log_stage_change(cid, stage, "Replied", "auto", "Inbox sync detected reply")
                    
                    # Notify via Telegram if we have the _app global reference
                    if _app:
                        from bot import _notify_sync
                        _notify_sync(_app, other_username, msg_text)
                        
    conn.commit()
    conn.close()

def inbox_watcher_loop(_app):
    """Background thread that wakes up and syncs inbox."""
    while True:
        try:
            sync_replies(_app)
        except Exception as e:
            logger.error(f"Inbox watcher error: {e}")
            
        time.sleep(300)  # Check every 5 minutes

def start_inbox_watcher(app):
    t = threading.Thread(target=inbox_watcher_loop, args=(app,), daemon=True)
    t.start()
