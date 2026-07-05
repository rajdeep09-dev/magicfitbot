"""
llm_orchestrator.py — Natural-language command router for MagicFit bot.

User types "start hunting these 10 accounts: @a @b @c ..." → this module asks
Mistral (with Gemini/Groq/NVIDIA fallback via ai_router providers) to convert
it into a structured intent + parameters, then dispatches to the real
handler functions.

Design goals:
- Zero UX friction: no slash commands required, but slash commands still work.
- Cheap: one LLM call per user message, ~150 output tokens. Cached common
  patterns first via regex (so trivial "yes"/"cancel"/"status" don't hit LLM).
- Robust: if LLM fails, we degrade to keyword matching so the bot never
  becomes unusable.
- Safe: any action that spawns a background scan echoes back what it's about
  to do and lets the user cancel with /cancelscan.
"""

import re
import json
import asyncio
import logging
import time
import requests
from typing import Optional

import database as db

logger = logging.getLogger(__name__)

# Intents the orchestrator can produce.
# Keep the schema flat and boring — Mistral emits it more reliably that way.
INTENT_SCHEMA = """
Emit ONE JSON object. No prose, no markdown fences. Choose exactly one intent.

{
  "intent": "start_hunt" | "add_seeds_and_hunt" | "autoscan" | "scan_status"
          | "cancel_scan" | "push_to_crm" | "list_scans" | "help"
          | "switch_mode" | "set_filters" | "set_scrapemode"
          | "add_cookie" | "add_apify" | "list_backends"
          | "outreach_start" | "outreach_send" | "outreach_status" | "outreach_pipeline"
          | "smalltalk" | "unknown",
  "seeds": ["username1", "username2"],          // for start_hunt / add_seeds_and_hunt / autoscan
  "hops": 1 | 2,                                 // default 1
  "skip_seen": true,                             // default true
  "interval_hours": 24,                          // for autoscan
  "mode": "hunting" | "outreach",                // for switch_mode
  "follower_min": 5000,                          // for set_filters
  "follower_max": 500000,                        // for set_filters
  "scrape_mode": "cookie" | "apify" | "hybrid",  // for set_scrapemode
  "cookie_sid": "",                              // for add_cookie
  "cookie_csrf": "",                             // for add_cookie
  "cookie_label": "",                            // for add_cookie
  "apify_token": "",                             // for add_apify
  "reply": "one-line human reply to send"        // ALWAYS present
}
"""

SYSTEM_PROMPT = """You are the command router for a Telegram-based Instagram creator-discovery bot.
The user chats with you naturally. You translate their message into ONE structured intent.

Rules:
- Extract Instagram usernames as bare handles (no @, no URL, lowercase).
- "start hunting" / "scan these" / "find creators like" → start_hunt (or add_seeds_and_hunt if usernames present).
- "every 24h" / "keep scanning" / "watch for new" → autoscan.
- "what's happening" / "how is the scan" / "any progress" → scan_status.
- "stop" / "cancel" → cancel_scan.
- "switch to outreach" / "email mode" → switch_mode:outreach.
- "back to hunting" → switch_mode:hunting.
- "hi" / greetings / thanks → smalltalk.
- If the user just asks a question or you have no confident mapping → unknown.
- ALWAYS include a "reply" field with a short friendly acknowledgment (max 20 words).
"""


# ────────────────────────────────────────────────────────────────
#  Cheap regex prefilter — save an LLM call on obvious inputs.
# ────────────────────────────────────────────────────────────────

_HANDLE_RE = re.compile(r"(?:^|[\s,@/])([a-zA-Z0-9._]{2,30})(?=$|[\s,])")
_URL_RE = re.compile(r"https?://\S+")

_TRIVIAL_MAP = {
    r"^\s*(hi|hello|hey|yo|sup)\s*[!.]*\s*$": ("smalltalk", "Hey! Tell me what you want — hunt creators, send outreach, or just ask."),
    r"^\s*(status|progress|update|how('?s| is) it going|any update)\??\s*$": ("scan_status", None),
    r"^\s*(cancel|stop|abort|halt)( scan)?\s*[!.]?\s*$": ("cancel_scan", None),
    r"^\s*(help|what can you do|commands?)\s*[?!.]?\s*$": ("help", None),
    r"^\s*(push( to)? ?crm|move to crm|send to crm)\s*[!.]?\s*$": ("push_to_crm", None),
    r"^\s*(scans?|history|past scans?|list scans?)\s*[?!.]?\s*$": ("list_scans", None),
    r"^\s*(hunting|hunt|hunt mode|find creators|scan mode)\s*[!.]?\s*$": ("switch_mode", None, {"mode": "hunting"}),
    r"^\s*(outreach|email mode|outreach mode|send emails)\s*[!.]?\s*$": ("switch_mode", None, {"mode": "outreach"}),
    r"^\s*(backends?|cookies?|apify tokens?)\s*[?!.]?\s*$": ("list_backends", None),
}


def _extract_handles(text: str) -> list[str]:
    """Pull @handles / bare usernames / instagram URLs. Filters out short words."""
    handles = set()
    # instagram.com/username
    for m in re.finditer(r"instagram\.com/([a-zA-Z0-9._]{2,30})", text, re.IGNORECASE):
        handles.add(m.group(1).lower())
    # @handle
    for m in re.finditer(r"@([a-zA-Z0-9._]{2,30})", text):
        handles.add(m.group(1).lower())
    # Filter obvious English words that slipped in
    STOP = {"the", "and", "for", "with", "you", "please", "kindly", "start", "hunt", "hunting",
            "scan", "these", "them", "all", "seed", "seeds", "creators", "creator", "new", "find",
            "add", "here", "are", "look", "watch", "keep", "help", "cancel", "stop", "mode",
            "outreach", "email", "emails", "yes", "no", "ok", "okay", "sure", "thanks", "thank"}
    return [h for h in sorted(handles) if h not in STOP]


def _regex_prefilter(text: str) -> Optional[dict]:
    """Return a full intent dict if the text matches a trivial pattern, else None."""
    t = text.strip().lower()
    for pattern, spec in _TRIVIAL_MAP.items():
        if re.match(pattern, t, re.IGNORECASE):
            intent = spec[0]
            reply = spec[1] or ""
            extra = spec[2] if len(spec) > 2 else {}
            out = {"intent": intent, "reply": reply}
            out.update(extra)
            return out

    # "start hunting these: @a @b" → catch without LLM
    handles = _extract_handles(text)
    if handles and re.search(r"\b(hunt|scan|find|seed|start|these|creators?)\b", t):
        # autoscan if they mention interval / recurring / keep / every
        if re.search(r"\b(every|recurring|keep|watch|auto|autoscan|hourly|daily)\b", t):
            hours = 24
            m = re.search(r"every\s+(\d+)\s*h", t)
            if m: hours = int(m.group(1))
            return {"intent": "autoscan", "seeds": handles, "interval_hours": hours,
                    "reply": f"Setting up autoscan on {len(handles)} seeds every {hours}h."}
        hops = 2 if re.search(r"\b(deep|deeper|2.?hop|thorough)\b", t) else 1
        return {"intent": "add_seeds_and_hunt", "seeds": handles, "hops": hops,
                "skip_seen": "include all" not in t and "include seen" not in t,
                "reply": f"Starting a {hops}-hop scan on {len(handles)} seeds."}

    return None


# ────────────────────────────────────────────────────────────────
#  LLM call — reuse Mistral endpoint from ai_router style.
# ────────────────────────────────────────────────────────────────

def _mistral_call(system: str, user: str, api_key: str, timeout: int = 20) -> Optional[str]:
    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "mistral-large-latest",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": 400,
        "response_format": {"type": "json_object"},
    }
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _groq_call(system: str, user: str, api_key: str, timeout: int = 15) -> Optional[str]:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": 400,
        "response_format": {"type": "json_object"},
    }
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _gemini_call(system: str, user: str, api_key: str, timeout: int = 20) -> Optional[str]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": user + "\n\nRespond with JSON only."}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 400, "responseMimeType": "application/json"},
    }
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def _get_key(setting: str, env_fallback: str) -> str:
    val = db.get_setting(setting, "")
    if val: return val
    from config import GEMINI_API_KEY, MISTRAL_API_KEY, GROQ_API_KEY, NVIDIA_API_KEY
    return {"mistral_api_key": MISTRAL_API_KEY, "gemini_api_key": GEMINI_API_KEY,
            "groq_api_key": GROQ_API_KEY, "nvidia_api_key": NVIDIA_API_KEY}.get(setting, "")


def llm_route(user_msg: str) -> dict:
    """Convert a free-form user message to a structured intent dict.
    Order: regex → Mistral → Groq → Gemini → keyword fallback.
    """
    # 1. Fast path
    pref = _regex_prefilter(user_msg)
    if pref:
        logger.info(f"orchestrator: regex hit intent={pref['intent']}")
        return pref

    # 2. LLM chain
    sys_full = SYSTEM_PROMPT + "\n\n" + INTENT_SCHEMA
    for name, caller, setting in [
        ("mistral", _mistral_call, "mistral_api_key"),
        ("groq",    _groq_call,    "groq_api_key"),
        ("gemini",  _gemini_call,  "gemini_api_key"),
    ]:
        key = _get_key(setting, "")
        if not key:
            continue
        try:
            raw = caller(sys_full, user_msg, key)
            if not raw:
                continue
            data = _safe_json(raw)
            if data and isinstance(data.get("intent"), str):
                # Guarantee seed extraction even if LLM missed it
                if data["intent"] in ("start_hunt", "add_seeds_and_hunt", "autoscan"):
                    if not data.get("seeds"):
                        data["seeds"] = _extract_handles(user_msg)
                    if data.get("seeds") and data["intent"] == "start_hunt":
                        data["intent"] = "add_seeds_and_hunt"
                if "reply" not in data:
                    data["reply"] = "Okay."
                logger.info(f"orchestrator: {name} → {data['intent']}")
                return data
        except Exception as e:
            logger.warning(f"orchestrator: {name} failed: {e}")
            continue

    # 3. Keyword fallback (offline mode)
    handles = _extract_handles(user_msg)
    if handles:
        return {"intent": "add_seeds_and_hunt", "seeds": handles, "hops": 1, "skip_seen": True,
                "reply": f"AI is offline, but I parsed {len(handles)} seeds. Starting scan."}
    return {"intent": "unknown", "reply": "I didn't catch that. Try 'hunt these accounts: @user1 @user2' or /help."}


def _safe_json(raw: str) -> Optional[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            try: return json.loads(raw[start:end+1])
            except json.JSONDecodeError: return None
        return None


# ────────────────────────────────────────────────────────────────
#  Dispatcher — calls the real bot handlers.
#  bot.py hands us: (update, ctx, discovery, db, notify, helpers)
#  We stay decoupled so patch upgrades don't require rewriting bot.py.
# ────────────────────────────────────────────────────────────────

async def dispatch(update, ctx, intent: dict, *, discovery, notify, mode_state, pending_seeds):
    """Route a parsed intent to the appropriate side effect.

    discovery      — the discovery module (may be None if unavailable)
    notify         — async fn(text, **kw) that posts to the user
    mode_state     — dict user_id → 'hunting'|'outreach'
    pending_seeds  — dict user_id → list[str] (multi-turn seed collection)
    """
    uid = update.effective_user.id
    action = intent.get("intent")
    reply = intent.get("reply") or ""

    # Always echo the LLM's friendly line first for perceived speed.
    if reply:
        try: await notify(reply)
        except Exception: pass

    if action == "smalltalk" or action == "unknown":
        return True  # nothing else to do, but we handled it

    if action == "help":
        await notify(
            "💡 <b>Just talk to me:</b>\n"
            "• 'hunt these: @a @b @c' — start a scan\n"
            "• 'deep scan @user1, @user2' — 2-hop scan\n"
            "• 'autoscan @seed1 @seed2 every 12h' — recurring\n"
            "• 'status' — how's the scan\n"
            "• 'cancel' — stop it\n"
            "• 'push to crm' — move results to outreach\n"
            "• 'switch to outreach' — email mode\n\n"
            "Slash commands still work: /scan /crm /help /accounts",
            parse_mode="HTML")
        return True

    if action == "switch_mode":
        mode = intent.get("mode", "hunting")
        mode_state[uid] = mode
        await notify(f"✅ Switched to <b>{mode}</b> mode.", parse_mode="HTML")
        return True

    if action == "list_backends":
        if not discovery:
            await notify("Discovery module not loaded."); return True
        ck = len(discovery.list_cookies())
        ap = [t for t in discovery.list_apify_tokens() if t.get("active")]
        cfg = discovery.get_cfg()
        await notify(f"🔧 Backends: {ck} cookies, {len(ap)} Apify tokens\nMode: {cfg.get('scrape_mode','hybrid')}")
        return True

    if action == "list_scans":
        if not discovery: return True
        rows = discovery.get_scan_history(10)
        if not rows:
            await notify("No past scans. Send seeds to start one."); return True
        lines = [f"#{r['id']} · {str(r['created_at'])[:16]} · {r['profile_count']} profiles" for r in rows]
        await notify("Past scans:\n" + "\n".join(lines))
        return True

    if action == "scan_status":
        if not discovery: return True
        rows = discovery.get_scan_history(1)
        if rows:
            r = rows[0]
            await notify(f"Last scan #{r['id']} at {str(r['created_at'])[:16]}: {r['profile_count']} profiles.")
        else:
            await notify("No scan running. Send seeds to start.")
        return True

    if action == "cancel_scan":
        if discovery:
            try: discovery.cancel_flag = True
            except Exception: pass
            try:
                if hasattr(discovery, "cancel_flags"):
                    discovery.cancel_flags[0] = True
            except Exception: pass
        await notify("🛑 Cancelling any in-flight scan.")
        return True

    if action == "push_to_crm":
        if not discovery: return True
        # Reuse the existing command logic by importing it lazily.
        try:
            import bot as _bot
            await _bot.cmd_pushtocrm(update, ctx)
        except Exception as e:
            logger.exception("push_to_crm dispatch failed")
            await notify(f"⚠️ Push failed: {e}")
        return True

    if action == "set_filters":
        if not discovery: return True
        cfg = discovery.get_cfg()
        fmin = int(intent.get("follower_min", cfg["follower_min"]))
        fmax = int(intent.get("follower_max", cfg["follower_max"]))
        cfg["follower_min"], cfg["follower_max"] = fmin, fmax
        discovery.save_cfg(cfg)
        await notify(f"✅ Filters: {fmin:,}—{fmax:,} followers.")
        return True

    if action == "set_scrapemode":
        if not discovery: return True
        m = intent.get("scrape_mode", "hybrid")
        if m in ("cookie", "apify", "hybrid"):
            discovery.set_mode(m)
            await notify(f"✅ Scrape mode: {m}.")
        return True

    if action == "add_cookie":
        if not discovery: return True
        sid = intent.get("cookie_sid", "").strip()
        csrf = intent.get("cookie_csrf", "").strip()
        label = intent.get("cookie_label") or None
        if not sid or not csrf:
            await notify("Need sessionid + csrftoken. Format: 'add cookie sid=... csrf=...'")
            return True
        discovery.add_cookie(sid, csrf, label)
        await notify(f"✅ Cookie added. Total: {len(discovery.list_cookies())}")
        return True

    if action == "add_apify":
        if not discovery: return True
        tok = intent.get("apify_token", "").strip()
        if not tok:
            await notify("Need the Apify token."); return True
        tid = discovery.add_apify_token(tok, None)
        await notify(f"✅ Apify token #{tid} added.")
        return True

    if action == "start_hunt":
        # No seeds given yet → ask for them and remember the state
        pending_seeds[uid] = {"awaiting": True, "hops": intent.get("hops", 1),
                              "skip_seen": intent.get("skip_seen", True)}
        await notify("Send me the seed usernames (comma or newline separated, up to 10).")
        return True

    if action in ("add_seeds_and_hunt", "autoscan"):
        if not discovery:
            await notify("Discovery module not available."); return True
        seeds = intent.get("seeds") or []
        if not seeds:
            seeds = _extract_handles(update.message.text)
        if not seeds:
            await notify("No seed usernames spotted. Try: 'hunt @user1 @user2 @user3'")
            return True

        if action == "autoscan":
            interval = int(intent.get("interval_hours", 24))
            aid = discovery.add_autoscan(seeds, hops=1, interval=interval) if _autoscan_takes_kw(discovery) else \
                  discovery.add_autoscan(seeds, 1, interval)
            await notify(f"✅ Autoscan #{aid}: {len(seeds)} seeds every {interval}h.\nStopping: /stopautoscan {aid}")
            return True

        hops = int(intent.get("hops", 1))
        skip = bool(intent.get("skip_seen", True))
        # Fire the scan on the background loop; UI stays responsive.
        await notify(f"🚀 Scanning {len(seeds)} seeds ({hops}-hop, skip_seen={skip})…")
        asyncio.create_task(_run_scan(update, ctx, seeds, hops, skip, discovery, notify))
        return True

    if action == "outreach_pipeline":
        try:
            import bot as _bot
            await _bot.cmd_pipeline(update, ctx)
        except Exception as e:
            await notify(f"⚠️ {e}")
        return True

    if action == "outreach_status":
        try:
            import bot as _bot
            await _bot.cmd_report(update, ctx)
        except Exception as e:
            await notify(f"⚠️ {e}")
        return True

    return False  # signal to caller: we did NOT handle it, run the legacy text pipeline


def _autoscan_takes_kw(discovery) -> bool:
    """discovery.add_autoscan changed signature between versions; probe."""
    import inspect
    try:
        sig = inspect.signature(discovery.add_autoscan)
        return "hops" in sig.parameters
    except Exception:
        return False


async def _run_scan(update, ctx, seeds, hops, skip_seen, discovery, notify):
    """Background scan wrapper — mirrors _handle_discovery_callback's disc_go path."""
    try:
        cfg = discovery.get_cfg()
        # Reset any lingering cancel flag
        try: discovery.cancel_flag = False
        except Exception: pass

        progress = {"last": 0.0, "msg": None}
        async def prog(text: str):
            now = time.time()
            if now - progress["last"] < 3.0:  # throttle to once every 3s
                return
            progress["last"] = now
            try:
                if progress["msg"] is None:
                    progress["msg"] = await ctx.bot.send_message(update.effective_user.id, f"⏳ {text[-500:]}")
                else:
                    await progress["msg"].edit_text(f"⏳ {text[-500:]}")
            except Exception:
                pass

        profiles = await discovery.run_pipeline(seeds, cfg, hops, skip_seen, prog)

        if not profiles:
            await notify("No profiles matched. Try loosening /setfilters or add more cookies.")
            return

        exports = discovery.export(profiles)
        discovery.log_scan(seeds, len(profiles), exports.get("csv"), exports.get("xlsx"), exports.get("outreach"))

        cohorts = {}
        for p in profiles: cohorts[p.cohort] = cohorts.get(p.cohort, 0) + 1
        emails = sum(1 for p in profiles if p.email)
        summary = " | ".join(f"{k}:{v}" for k, v in sorted(cohorts.items()))
        await notify(f"✅ {len(profiles)} profiles | {summary} | 📧 {emails} with email")

        from telegram import InputFile
        import os as _os
        for key in ("csv", "xlsx", "outreach"):
            fp = exports.get(key)
            if fp and _os.path.exists(fp):
                with open(fp, "rb") as f:
                    await ctx.bot.send_document(update.effective_user.id, InputFile(f, filename=_os.path.basename(fp)))

        push_fn = getattr(discovery, "push_to_crm", None) or getattr(discovery, "push_profiles_to_crm", None)
        if push_fn:
            res = push_fn(profiles)
            await notify(f"📋 Auto-pushed to CRM: {res['inserted']} new, {res['duplicates']} dupes.")

    except Exception as e:
        logger.exception("background scan failed")
        await notify(f"⚠️ Scan crashed: {e}")


async def handle_pending_seeds(update, ctx, pending_seeds, discovery, notify) -> bool:
    """If the user was asked for seeds after 'start_hunt', consume this message as seeds."""
    uid = update.effective_user.id
    state = pending_seeds.get(uid)
    if not state or not state.get("awaiting"):
        return False
    text = update.message.text or ""
    seeds = _extract_handles(text)
    if not seeds:
        await notify("I didn't spot any usernames. Send them like: @user1 @user2 @user3")
        return True
    pending_seeds.pop(uid, None)
    await notify(f"🚀 Got {len(seeds)} seeds. Scanning…")
    asyncio.create_task(_run_scan(update, ctx, seeds,
                                   state.get("hops", 1), state.get("skip_seen", True),
                                   discovery, notify))
    return True
