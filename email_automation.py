"""
Email Automation

Features:
- Full AI chain for generating personalized email subjects and bodies.
- Queues emails into the `emails_sent` table for background processing by email_sender.
"""

import time
import logging
import json
import database as db
import ai_router

logger = logging.getLogger("email_automation")

def generate_personalized_email(creator_data: dict) -> tuple:
    """Uses full AI chain to generate a personalized email subject and body for a creator."""
    default_prompt = (
        "You are a friendly partnership manager reaching out to Instagram creators on behalf of MagicFit. "
        "Keep the message professional but highly personalized to their niche or bio. "
        "The goal is asking if they are open to a paid collab (upfront fee + commission). "
        "Return the response in JSON format with two keys: 'subject' and 'body'. "
        "Do not include placeholders like [Your Name]. Just write the message ready to send."
    )
    user_system = db.get_setting("email_generator_prompt", default_prompt)
    if not user_system:
        user_system = default_prompt
        
    system_prompt = user_system
    user_prompt = f"Profile Data: {json.dumps(creator_data, ensure_ascii=False)}"

    result, fail_reason = ai_router._try_chain(system_prompt, user_prompt, expect_json=True)
    
    if result:
        try:
            # Result should be JSON if expect_json=True
            data = json.loads(result) if isinstance(result, str) else result
            subject = data.get("subject", "Collab Opportunity - MagicFit")
            body = data.get("body", "Hey! Love your content. Are you open to a paid collab right now? Let me know!")
            return subject.strip(), body.strip()
        except Exception as e:
            logger.warning(f"Failed to parse email JSON: {e}")
            
    logger.warning(f"Email generation failed ({fail_reason}), using fallback template")
    fallback_subject = f"Collaboration Inquiry"
    fallback_body = f"Hey {creator_data.get('name', 'there')}!\n\nLove your content. We're looking for creators in your space for a paid collaboration. Let me know if you're open to discussing rates!\n\nBest,\nPartnerships Team"
    return fallback_subject, fallback_body


def queue_bulk_campaign(creator_ids: list):
    """Queues a list of creators for email sending."""
    conn = db.get_db()
    c = conn.cursor()

    queued = 0
    for cid in creator_ids:
        c.execute(
            "SELECT handle, profile_url, tags, location, is_business, post_count, engagement_rate, bio, name, email "
            "FROM creators WHERE id=?", (cid,)
        )
        row = c.fetchone()
        if not row or not row["email"] or "no_email" in row["email"]:
            continue

        username = row["handle"]
        email = row["email"]
        cdata = {
            "username": username,
            "name": row["name"] or username,
            "tags": row["tags"] or "",
            "location": row["location"] or "",
            "is_business": row["is_business"],
            "engagement_rate": row["engagement_rate"],
            "bio": row["bio"] or "",
        }

        subject, body = generate_personalized_email(cdata)
        
        # Log to emails_sent with status 'queued'
        db.log_email(cid, None, email, subject, body, 'outreach', 'queued')
        queued += 1

    conn.close()

    logger.info(f"Queued {queued} personalized emails.")
    return queued
