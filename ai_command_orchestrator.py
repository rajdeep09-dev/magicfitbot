import json
import logging
from llm_orchestrator import _gemini_call, _mistral_call, _groq_call, _get_key

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an autonomous AI Agent that manages a Telegram-based Instagram discovery and DM outreach bot.
Your job is to read the user's natural language request and figure out exactly which slash commands the bot needs to run to achieve the user's goal.
You must output a JSON array of exact slash commands (strings) in the correct order.

Available Bot Commands (The AI can execute ANY of these):
- /accounts, /addaccount, /removeaccount: Manage Instagram accounts
- /addapify, /removeapify, /apifytokens: Manage Apify API tokens
- /addcookie, /removecookie, /cookies, /howtocookies, /adddmcookie: Manage Instagram cookies
- /ai: You are the AI. You can trigger other commands.
- /analytics, /report, /fullreport: View bot analytics and stats
- /autoscan, /autoscans, /stopautoscan: Manage recurring background scans
- /bulk, /passall, /skipall: Bulk actions for creators
- /cancel, /exit: Cancel current operation
- /cancelscan: Stop an active scan
- /clearseen, /seen: Manage the database of seen profiles
- /creator, /crm, /pipeline, /hot, /won, /lost, /note: Manage the CRM and creator states
- /dashboard, /d, /today: View main dashboard and daily stats
- /dmdone, /dmlist, /dmstatus, /previewdm: Manage the DM queue
- /export, /export_tinder, /exportdms: Export data to CSV/HTML
- /files: Access exported files
- /help: View help menu
- /hunt, /outreach, /outreachall: Switch modes
- /keywords: Manage bio keywords
- /launch_campaign: Start a DM campaign
- /lookalike: Find similar creators
- /pause, /resume: Pause or resume background tasks
- /preview: Preview the next creator in tinder mode
- /pushtocrm: Move a discovered creator to the CRM
- /queue, /startqueue, /stopqueue: Manage the action queue
- /reply, /checkreplies, /replystatus, /startwatcher, /stopwatcher: Manage Inbox monitoring
- /resend, /retrydms: Retry failed DMs
- /scan, /scanhistory: Run a 1-hop or 2-hop network scan
- /scrapemode: Switch between cookie, apify, or hybrid scraping
- /seeds, /addseed, /removeseed: Manage the seed list
- /setautoreply, /setdminterval, /setfollowup, /setinterval, /setlimit, /setprompt, /setreplycheck: Configure bot parameters
- /setfilters, /setmaxfollow, /setmaxresults, /setreach: Configure scan filters
- /setgemini, /setgroq, /setmistral, /setnvidia, /setscrapingdog, /apikeys: Manage API keys
- /start: Start the bot
- /tinder: Enter tinder mode for manual swiping

Rules:
1. ONLY output a valid JSON object with a single key "commands" that contains an array of strings.
2. Ensure you format the commands EXACTLY as they appear above, substituting arguments appropriately.
3. If the user wants to do multiple things (e.g. "set scrape mode to apify and export my dms"), you must output multiple commands: ["/scrapemode apify", "/exportdms"].
4. Do not output any conversational text, ONLY the JSON object.

Example output:
{
  "commands": [
    "/scrapemode apify",
    "/exportdms"
  ]
}
"""

def generate_ai_commands(user_prompt: str) -> list[str]:
    """
    Sends the prompt through Gemini (3x) -> Mistral (3x) -> Groq (3x).
    Returns a list of commands, e.g. ["/scrapemode apify", "/exportdms"].
    """
    providers = [
        ("gemini", "gemini_api_key", _gemini_call),
        ("mistral", "mistral_api_key", _mistral_call),
        ("groq", "groq_api_key", _groq_call),
    ]

    for provider_name, setting_key, caller_fn in providers:
        api_key = _get_key(setting_key, "")
        if not api_key:
            continue
            
        for attempt in range(3):
            try:
                raw_response = caller_fn(SYSTEM_PROMPT, user_prompt, api_key)
                if raw_response:
                    # Clean markdown if present
                    raw_response = raw_response.strip().strip("```json").strip("```").strip()
                    parsed = json.loads(raw_response)
                    if "commands" in parsed and isinstance(parsed["commands"], list):
                        return parsed["commands"]
            except Exception as e:
                logger.warning(f"{provider_name} attempt {attempt+1} failed: {e}")
                
    return []
