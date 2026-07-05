"""
AI Router: Gemini → Mistral → Groq → NVIDIA NIM fallback for text generation.
Generates personalized emails and replies based on creator context + conversation history.
"""

import json
import time
import logging
import requests
from config import (
    GEMINI_API_KEY as _CFG_GEMINI, MISTRAL_API_KEY as _CFG_MISTRAL,
    GROQ_API_KEY as _CFG_GROQ, NVIDIA_API_KEY as _CFG_NVIDIA,
)
import database as db

logger = logging.getLogger(__name__)


def _get_key(setting_name, fallback):
    """Get API key from DB setting first, then fall back to config/env."""
    val = db.get_setting(setting_name, "")
    return val if val else fallback


# Module-level references that can be hot-swapped by bot.py
GEMINI_API_KEY = _CFG_GEMINI
MISTRAL_API_KEY = _CFG_MISTRAL
GROQ_API_KEY = _CFG_GROQ
NVIDIA_API_KEY = _CFG_NVIDIA
OPENROUTER_API_KEY = None


def generate_opener_email(creator_info: dict) -> dict:
    """
    Generate a personalized opener email for a creator.
    creator_info: {name, handle, platform, followers, tier, bio, niche}
    Returns: {subject, body, used_fallback}
    """
    if hasattr(creator_info, "keys"):
        creator_info = dict(creator_info)
    system_prompt = db.get_setting("system_prompt")

    user_prompt = f"""Write a cold outreach email to this creator. This is the FIRST email so it should be just an opener asking if they're open to collabs, with a one-line hint at the deal (upfront fee + 50% commission for 12 months). Do not include full deal details.

CRITICAL INSTRUCTIONS FOR HUMANIZATION:
- Write entirely in lowercase.
- Use casual slang like "yo", "ur", "rn", "lmk".
- Include 1 or 2 natural typos (like a missing comma or a slight misspelling).
- End the email with "\\n\\nSent from my iPhone" roughly 40% of the time, otherwise just end it normally.

Creator info:
- Name: {creator_info.get('name', 'Unknown')}
- Handle: @{creator_info.get('handle', 'unknown')}
- Platform: {creator_info.get('platform', 'instagram')}
- Followers: {creator_info.get('followers', 'unknown')}
- Tier: {creator_info.get('tier', 'unknown')}
- Bio: {creator_info.get('bio', 'No bio available')}
- Niche/Content: {creator_info.get('niche', 'unknown')}

Output format (JSON only, no markdown, no extra text):
{{"subject": "subject line here", "body": "email body here with line breaks as \\n"}}"""

    result, fail_reason = _try_chain(system_prompt, user_prompt, expect_json=True)
    if result and isinstance(result, dict) and "subject" in result and "body" in result:
        result["used_fallback"] = False
        return result

    logger.error(f"generate_opener_email: all AI providers failed ({fail_reason}), using static fallback template")
    from templates_fallback import generate_fallback_opener
    fallback = generate_fallback_opener(creator_info)
    fallback["used_fallback"] = True
    fallback["fallback_reason"] = fail_reason
    return fallback


def generate_reply(creator_info: dict, conversation: list, their_latest_reply: str, instruction: str = "") -> dict:
    """
    Generate a contextual reply based on conversation history.
    instruction: optional guidance from user like "offer them $150 flat fee" or "explain the commission better"
    Returns: {subject, body, suggested_stage, used_fallback}
    """
    if hasattr(creator_info, "keys"):
        creator_info = dict(creator_info)
    system_prompt = db.get_setting("system_prompt")
    deal_structure = db.get_setting("deal_structure")

    convo_text = ""
    for msg in conversation:
        role_label = "Us" if msg["role"] == "us" else "Them"
        convo_text += f"\n{role_label}: {msg['content']}\n"

    user_prompt = f"""Write a reply email continuing this conversation with a creator.

CRITICAL INSTRUCTIONS FOR HUMANIZATION:
- Write entirely in lowercase.
- Use casual slang like "yo", "ur", "rn", "lmk".
- Include 1 or 2 natural typos.
- End the email with "\\n\\nSent from my iPhone".
- YOU MUST STRICTLY FOLLOW THIS USER INSTRUCTION: {instruction or 'Generate the most appropriate next response. If they showed interest, share the full deal details.'}

Creator info:
- Name: {creator_info.get('name', 'Unknown')}
- Tier: {creator_info.get('tier', 'unknown')}
- Followers: {creator_info.get('followers', 'unknown')}

Full conversation so far:{convo_text}

Their latest message:
{their_latest_reply}

Default deal structure available for this tier:
{deal_structure}

Keep the body reasonably concise so the full JSON response fits well within the token budget.

Output format (JSON only, no markdown, no extra text):
{{"subject": "Re: [original subject or new]", "body": "reply body with \\n for line breaks", "suggested_stage": "negotiating|interested|closed_won|closed_lost|needs_info"}}"""

    result, fail_reason = _try_chain(system_prompt, user_prompt, expect_json=True)
    if result and isinstance(result, dict) and result.get("subject") and result.get("body"):
        result["used_fallback"] = False
        return result

    logger.error(f"generate_reply: all AI providers failed ({fail_reason}). Instruction was: {(instruction or '(none)')[:200]}")

    # Build a more useful fallback than a generic non-answer: pull real numbers
    # from the configured deal structure for their tier so it's not empty content.
    from templates_fallback import generate_fallback_reply
    fallback = generate_fallback_reply(creator_info, instruction)
    fallback["used_fallback"] = True
    fallback["fallback_reason"] = fail_reason
    return fallback


def generate_followup(creator_info: dict, previous_emails: list, followup_number: int) -> dict:
    """Generate a follow-up email (no reply received)."""
    if hasattr(creator_info, "keys"):
        creator_info = dict(creator_info)
    system_prompt = db.get_setting("system_prompt")

    previous_text = ""
    for em in previous_emails:
        previous_text += f"\n--- Previous email ({em['message_type']}) ---\nSubject: {em['subject']}\n{em['body']}\n"

    user_prompt = f"""Write follow-up #{followup_number} to this creator. They haven't replied to your previous emails. Keep it short, friendly, no pressure.

Creator info:
- Name: {creator_info.get('name', 'Unknown')}
- Tier: {creator_info.get('tier', 'unknown')}

Previous emails sent:{previous_text}

Make this follow-up:
- Very short (2-3 sentences max)
- Different angle from previous emails
- Casual, no pushy tone
- If this is follow-up 3 (final), mention you'll close the loop here

Output format (JSON only):
{{"subject": "Re: [previous subject]", "body": "follow-up body"}}"""

    result, fail_reason = _try_chain(system_prompt, user_prompt, expect_json=True)
    if result and isinstance(result, dict) and result.get("subject") and result.get("body"):
        result["used_fallback"] = False
        return result

    logger.error(f"generate_followup: all AI providers failed ({fail_reason}) for followup #{followup_number}")
    from templates_fallback import generate_fallback_followup
    fallback = generate_fallback_followup(creator_info, followup_number)
    fallback["used_fallback"] = True
    fallback["fallback_reason"] = fail_reason
    return fallback


def generate_dm_hook(creator_info: dict) -> str:
    """Generate a single-sentence personalized hook based on bio/niche."""
    if hasattr(creator_info, "keys"):
        creator_info = dict(creator_info)
    system_prompt = db.get_setting("system_prompt")
    user_prompt = f"""Write ONE short, highly personalized sentence to be used as a DM opener hook.
    
CRITICAL INSTRUCTIONS:
- Write entirely in lowercase.
- Mention something specific from their bio or niche.
- Keep it under 15 words.
- Use casual language. Do not sound corporate.
- Output ONLY the raw sentence text. Do not output JSON. Do not output quotes.

Creator info:
- Name: {creator_info.get('name', 'Unknown')}
- Bio: {creator_info.get('bio', 'No bio available')}
- Niche: {creator_info.get('niche', 'unknown')}"""

    result, _ = _try_chain(system_prompt, user_prompt, expect_json=False)
    if result:
        return result.strip().strip('"').strip("'")
    return f"saw ur profile and loved ur content"

def generate_batched_dm_hooks(creators_list: list[dict]) -> dict[str, str]:
    """
    Takes a list of creator info dicts (up to 50) and asks the AI to generate a hook for each.
    Returns a dictionary mapping handle -> generated hook string.
    """
    if not creators_list:
        return {}
        
    system_prompt = db.get_setting("system_prompt")
    
    creators_json_str = json.dumps([
        {"handle": c.get("handle"), "bio": c.get("bio", ""), "niche": c.get("niche", "")}
        for c in creators_list
    ])
    
    user_prompt = f"""Generate a single-sentence personalized DM opener hook for EACH creator in this list.

CRITICAL INSTRUCTIONS:
- Write entirely in lowercase.
- Mention something specific from their bio or niche.
- Keep it under 15 words per hook.
- Use casual language. Do not sound corporate.
- Output MUST be a JSON object mapping the exact "handle" to the generated string.

Creators List:
{creators_json_str}

Output Format:
{{
  "handle1": "hook for handle1",
  "handle2": "hook for handle2"
}}"""

    result, _ = _try_chain(system_prompt, user_prompt, expect_json=True)
    out_map = {}
    if result and isinstance(result, dict):
        for handle, hook in result.items():
            if isinstance(hook, str):
                out_map[handle] = hook.strip().strip('"').strip("'")
                
    # Fill in fallbacks for any missing ones
    for c in creators_list:
        h = c.get("handle")
        if h and h not in out_map:
            out_map[h] = "saw ur profile and loved ur content"
            
    return out_map

def _try_chain(system_prompt: str, user_prompt: str, expect_json: bool = False):
    """
    Try Gemini → Mistral → Groq → NVIDIA in order. Retries once per provider on
    transient network errors (connection drops, common on mobile data) before
    moving to the next provider. Returns (result_or_None, failure_summary_or_None)
    so callers can surface exactly why generation failed instead of guessing.
    """
    any_key_configured = False
    failure_reasons = []

    providers = [
        ("openrouter", "openrouter_api_key", OPENROUTER_API_KEY),
        ("gemini", "gemini_api_key", GEMINI_API_KEY),
        ("mistral", "mistral_api_key", MISTRAL_API_KEY),
        ("groq", "groq_api_key", GROQ_API_KEY),
        ("nvidia", "nvidia_api_key", NVIDIA_API_KEY),
    ]

    for provider, setting_key, fallback_key in providers:
        key = _get_key(setting_key, fallback_key)
        if not key:
            logger.info(f"{provider}: no API key configured, skipping")
            continue
        any_key_configured = True

        max_attempts = 2  # original try + 1 retry, only for transient network errors
        for attempt in range(1, max_attempts + 1):
            try:
                result = _call_provider(provider, system_prompt, user_prompt)
                if not result:
                    logger.warning(f"{provider}: returned empty response")
                    failure_reasons.append(f"{provider}: empty response")
                    break

                if expect_json:
                    parsed = _extract_json(result)
                    if parsed and _has_required_fields(parsed):
                        logger.info(f"{provider}: success (attempt {attempt})")
                        return parsed, None
                    logger.warning(
                        f"{provider}: response was not valid/complete JSON. "
                        f"Raw response (first 300 chars): {result[:300]}"
                    )
                    failure_reasons.append(f"{provider}: malformed/incomplete response")
                    break
                else:
                    return result, None

            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else "?"
                body = ""
                try:
                    body = e.response.text[:200]
                except Exception:
                    pass
                logger.warning(f"{provider}: HTTP {status} - {body}")
                reason = f"{provider}: HTTP {status}"
                if status == 429:
                    reason += " rate limited"
                elif status in (401, 403):
                    reason += " invalid/unauthorized key"
                failure_reasons.append(reason)
                break  # HTTP errors are not transient, retrying won't help, move to next provider

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                logger.warning(f"{provider}: network error on attempt {attempt}/{max_attempts} - {e}")
                if attempt < max_attempts:
                    time.sleep(2)
                    continue  # retry once, likely a mobile data blip
                failure_reasons.append(f"{provider}: connection dropped (network blip)")
                break

            except Exception as e:
                logger.warning(f"{provider}: {e}")
                failure_reasons.append(f"{provider}: {str(e)[:100]}")
                break

    if not any_key_configured:
        msg = "No AI API keys configured. Set one with /setgemini, /setmistral, /setgroq, or /setnvidia."
        logger.error(msg)
        return None, msg

    summary = " | ".join(failure_reasons) if failure_reasons else "all providers failed for an unknown reason"
    logger.error(f"All configured AI providers failed: {summary}")
    return None, summary


def _has_required_fields(parsed: dict) -> bool:
    """Check the parsed JSON actually has usable subject+body, not a truncated/garbage parse."""
    if not isinstance(parsed, dict):
        return False
    subject = parsed.get("subject", "")
    body = parsed.get("body", "")
    return bool(subject and body and len(body) > 10)


def _call_provider(provider: str, system_prompt: str, user_prompt: str) -> str | None:
    if provider == "openrouter":
        key = _get_key("openrouter_api_key", OPENROUTER_API_KEY)
        if key:
            return _call_openrouter(system_prompt, user_prompt, key)
    elif provider == "gemini":
        key = _get_key("gemini_api_key", GEMINI_API_KEY)
        if key:
            return _call_gemini(system_prompt, user_prompt, key)
    elif provider == "mistral":
        key = _get_key("mistral_api_key", MISTRAL_API_KEY)
        if key:
            return _call_mistral(system_prompt, user_prompt, key)
    elif provider == "groq":
        key = _get_key("groq_api_key", GROQ_API_KEY)
        if key:
            return _call_groq(system_prompt, user_prompt, key)
    elif provider == "nvidia":
        key = _get_key("nvidia_api_key", NVIDIA_API_KEY)
        if key:
            return _call_nvidia(system_prompt, user_prompt, key)
    return None

def _call_openrouter(system_prompt: str, user_prompt: str, api_key: str) -> str | None:
    """OpenRouter via OpenAI-compatible endpoint."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://magicfit.ai",
        "X-Title": "MagicFit Bot"
    }
    model = db.get_setting("openrouter_model", "meta-llama/llama-3.1-8b-instruct:free")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.85,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]

def _call_gemini(system_prompt: str, user_prompt: str, api_key: str) -> str | None:
    """Gemini 2.5 Flash via Google AI Studio."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.85,
            "maxOutputTokens": 2048,
        }
    }
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _call_mistral(system_prompt: str, user_prompt: str, api_key: str) -> str | None:
    """Mistral Large via La Plateforme. Free Experiment tier, no card."""
    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "mistral-large-latest",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.85,
        "max_tokens": 2048,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def _call_groq(system_prompt: str, user_prompt: str, api_key: str) -> str | None:
    """Groq with Llama 3.3 70B."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.85,
        "max_tokens": 2048,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def _call_nvidia(system_prompt: str, user_prompt: str, api_key: str) -> str | None:
    """NVIDIA NIM with Kimi K2.6 (Moonshot AI)."""
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "moonshotai/kimi-k2.6",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.85,
        "top_p": 1.00,
        "max_tokens": 4096,
        "stream": False,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=45)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def _extract_json(text: str):
    """Extract JSON from a possibly markdown-wrapped or truncated response."""
    if not text:
        return None
    text = text.strip()
    # Strip code fences
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    start = text.find("{")
    if start == -1:
        return None
    end = text.rfind("}")

    # Try strict parse first
    if end != -1:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    # Fallback: response was likely truncated mid-string (hit token limit).
    # Pull subject/body out with regex even if the JSON never closed properly.
    import re
    candidate = text[start:]

    subject_match = re.search(r'"subject"\s*:\s*"((?:[^"\\]|\\.)*)"', candidate, re.DOTALL)
    body_match = re.search(r'"body"\s*:\s*"((?:[^"\\]|\\.)*)"', candidate, re.DOTALL)
    stage_match = re.search(r'"suggested_stage"\s*:\s*"((?:[^"\\]|\\.)*)"', candidate, re.DOTALL)

    if subject_match and body_match:
        def _unescape(s):
            return s.replace('\\n', '\n').replace('\\"', '"').replace('\\t', '\t')
        result = {
            "subject": _unescape(subject_match.group(1)),
            "body": _unescape(body_match.group(1)),
        }
        if stage_match:
            result["suggested_stage"] = stage_match.group(1)
        return result

    # True truncation: body field started but never closed (hit token limit mid-sentence)
    if subject_match:
        body_start = re.search(r'"body"\s*:\s*"', candidate)
        if body_start:
            def _unescape(s):
                return s.replace('\\n', '\n').replace('\\"', '"').replace('\\t', '\t')
            raw_tail = candidate[body_start.end():]
            # Drop a trailing dangling escape character if the cut happened mid-escape
            if raw_tail.endswith("\\"):
                raw_tail = raw_tail[:-1]
            body_text = _unescape(raw_tail).strip()
            if len(body_text) > 20:
                logger.warning("Recovered a truncated AI response (hit token limit mid-body)")
                return {
                    "subject": _unescape(subject_match.group(1)),
                    "body": body_text + "\n\n[Note: response was cut short, may be incomplete]",
                }

    return None
