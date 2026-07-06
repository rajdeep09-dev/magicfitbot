"""
MagicFit AI Outreach Bot - Main Telegram Interface

Run with: python bot.py
Requires .env file (see .env.example)
"""

import os
import re
import json
import logging
import tempfile
import asyncio
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from telegram.request import HTTPXRequest

import config
import database as db
import ai_router
import ocr_router
import scrapers
import email_sender
import reply_watcher
import followup_scheduler

# PATCH9_KB_HELPERS
def _mfai_main_kb():
    """Main mode-selector keyboard. Tapping a button sends the slash command."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(" /hunt"), KeyboardButton(" /outreach")],
         [KeyboardButton(" /help"), KeyboardButton(" /report")]],
        resize_keyboard=True, one_time_keyboard=False,
        input_field_placeholder="Tap a button or type a command",
    )

def _mfai_hunt_kb():
    """Hunt-mode keyboard."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(" /scan"), KeyboardButton(" /autoscans")],
         [KeyboardButton(" /cookies"), KeyboardButton(" /apifytokens")],
         [KeyboardButton(" /scanhistory"), KeyboardButton(" /pushtocrm")],
         [KeyboardButton(" /exit")]],
        resize_keyboard=True, one_time_keyboard=False,
    )

def _mfai_outreach_kb():
    """Outreach-mode keyboard."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(" /hot"), KeyboardButton(" /crm")],
         [KeyboardButton(" /report"), KeyboardButton(" /queue")],
         [KeyboardButton(" /accounts"), KeyboardButton(" /apikeys")],
         [KeyboardButton(" /exit")]],
        resize_keyboard=True, one_time_keyboard=False,
    )

# In-memory mode tracking (single-user bot).
_user_mode = {}


#  DISCOVERY ENGINE (merged from scraper bot) 
try:
    import discovery
except ImportError:
    discovery = None

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("bot")
logging.getLogger().addHandler(logging.FileHandler("bot_runtime.log", encoding="utf-8"))

# Global app reference for notifications from background threads
_app = None
_main_loop = None


def authorized(user_id):
    if not config.ALLOWED_USER_ID:
        return True
    return user_id == config.ALLOWED_USER_ID


# 
#                       ONBOARDING
# 


#  DUAL_MODE_START_V2 
_user_mode = {}  # user_id -> "hunting" | "outreach"
_disc_awaiting_seeds = {}  # user_id -> "scan" | "autoscan"
_disc_pending_seeds = {}   # user_id -> [seeds]


async def _handle_mode_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    data = query.data

    if data == "mode_hunting":
        _user_mode[uid] = "hunting"
        await query.answer("Hunting mode")
        await query.edit_message_text(
            " Hunting Mode\n\n"
            "/scan — start scanning\n"
            "/addcookie /addapify — add backends\n"
            "/setfilters /keywords /scrapemode — config\n"
            "/scanhistory /pushtocrm — results\n"
            "/exit — switch mode")

    elif data == "mode_outreach":
        _user_mode[uid] = "outreach"
        await query.answer("Outreach mode")
        if not db.is_onboarded():
            await query.edit_message_text("Setting up outreach...")
            await _start_onboarding_from_callback(query, ctx)
        else:
            await query.edit_message_text(
                " Outreach Mode\n\n"
                "Send screenshot / URL / email to begin.\n"
                "/crm — pipeline | /hot — priority replies\n"
                "/help — all commands | /exit — switch mode")

async def _start_onboarding_from_callback(query, ctx):
    """Trigger onboarding from a callback (no update.message available)."""
    uid = query.from_user.id
    db.save_pending_action(uid, "onboarding", {"step": "welcome"})
    await ctx.bot.send_message(uid,
        "Welcome to MagicFit AI Outreach setup!\n\n"
        "First, add your Gmail accounts.\n"
        "Each needs 2FA + app password (https://myaccount.google.com/apppasswords)\n\n"
        "Send: <code>email@gmail.com app_password</code>\n"
        "Or /skip to skip.",
        parse_mode=ParseMode.HTML)

async def cmd_exit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    _user_mode.pop(update.effective_user.id, None)
    _disc_awaiting_seeds.pop(update.effective_user.id, None)
    if discovery: discovery.cancel_flag = True
    await update.message.reply_text("Switched back to Command Center.", reply_markup=_mfai_main_kb())


async def _start_onboarding(update, ctx):
    db.save_pending_action(
        update.effective_user.id,
        "onboarding",
        {"step": "welcome"}
    )
    await update.message.reply_text(
        "Welcome to MagicFit AI Outreach Bot setup!\n\n"
        "I'll walk you through getting set up. This takes about 5 minutes.\n\n"
        "First, we'll add your Gmail accounts (you can add multiple for higher capacity).\n\n"
        "Each Gmail account needs:\n"
        "1. 2FA enabled on the Google account\n"
        "2. An app password generated (https://myaccount.google.com/apppasswords)\n\n"
        "Send your first account in this format:\n"
        "<code>email@gmail.com app_password_here</code>\n\n"
        "Or type /skip to skip account setup (you can add later with /addaccount).",
        parse_mode=ParseMode.HTML,
    )


async def _continue_onboarding(update, ctx, step_data):
    step = step_data.get("step")
    user_id = update.effective_user.id

    if step == "welcome":
        # Expecting first gmail account
        text = update.message.text.strip()
        if text == "/skip":
            await _ask_followup_settings(update, ctx)
            return

        result = await _try_add_account(text)
        if result["success"]:
            await update.message.reply_text(
                f" Added {result['email']} (limit: 50/day)\n\n"
                f"Add another account in the same format, or send /done to continue setup."
            )
            db.save_pending_action(user_id, "onboarding", {"step": "more_accounts"})
        else:
            await update.message.reply_text(f" {result['error']}\n\nTry again or send /skip.")

    elif step == "more_accounts":
        text = update.message.text.strip()
        if text == "/done":
            await _ask_followup_settings(update, ctx)
            return
        if text == "/skip":
            await _ask_followup_settings(update, ctx)
            return

        result = await _try_add_account(text)
        if result["success"]:
            accounts = db.get_all_accounts()
            await update.message.reply_text(
                f" Added {result['email']}. Total accounts: {len(accounts)}\n\n"
                f"Add another or send /done."
            )
        else:
            await update.message.reply_text(f" {result['error']}\n\nTry again or send /done.")

    elif step == "api_keys":
        text = update.message.text.strip()
        if text == "/skip":
            await _ask_followup_real(update, ctx)
            return
        # Save Gemini key
        db.set_setting("gemini_api_key", text)
        config.GEMINI_API_KEY = text
        os.environ["GEMINI_API_KEY"] = text
        _persist_to_env("GEMINI_API_KEY", text)
        await update.message.reply_text(f" Gemini key saved: {_mask_key(text)}")
        db.save_pending_action(user_id, "onboarding", {"step": "api_mistral"})
        await update.message.reply_text(
            "Now Mistral key (backup for both screenshots and email writing if Gemini is rate limited).\n"
            "Get free at https://console.mistral.ai (no card needed)\n\n"
            "Send key or /skip."
        )

    elif step == "api_mistral":
        text = update.message.text.strip()
        if text != "/skip":
            db.set_setting("mistral_api_key", text)
            config.MISTRAL_API_KEY = text
            os.environ["MISTRAL_API_KEY"] = text
            _persist_to_env("MISTRAL_API_KEY", text)
            await update.message.reply_text(f" Mistral key saved: {_mask_key(text)}")
        db.save_pending_action(user_id, "onboarding", {"step": "api_groq"})
        await update.message.reply_text(
            "Now Groq key (text backup AI).\n"
            "Get free at https://console.groq.com\n\n"
            "Send key or /skip."
        )

    elif step == "api_groq":
        text = update.message.text.strip()
        if text != "/skip":
            db.set_setting("groq_api_key", text)
            config.GROQ_API_KEY = text
            os.environ["GROQ_API_KEY"] = text
            _persist_to_env("GROQ_API_KEY", text)
            await update.message.reply_text(f" Groq key saved: {_mask_key(text)}")
        db.save_pending_action(user_id, "onboarding", {"step": "api_nvidia"})
        await update.message.reply_text(
            "NVIDIA NIM key (second backup).\n"
            "Get free at https://build.nvidia.com\n\n"
            "Send key or /skip."
        )

    elif step == "api_nvidia":
        text = update.message.text.strip()
        if text != "/skip":
            db.set_setting("nvidia_api_key", text)
            config.NVIDIA_API_KEY = text
            os.environ["NVIDIA_API_KEY"] = text
            _persist_to_env("NVIDIA_API_KEY", text)
            await update.message.reply_text(f" NVIDIA key saved: {_mask_key(text)}")
        await _ask_followup_real(update, ctx)

    elif step == "followup":
        text = update.message.text.strip()
        if text == "/default":
            db.set_setting("followup_days", json.dumps([3, 2]))
            db.set_setting("max_followups", "3")
        else:
            # Parse like "3 2 3" or "5,7,2"
            try:
                parts = re.split(r'[,\s]+', text)
                nums = [int(p) for p in parts if p.strip()]
                if len(nums) >= 1:
                    days = nums[:-1] if len(nums) > 1 else [3, 2]
                    max_fu = nums[-1] if len(nums) > 1 else 3
                    db.set_setting("followup_days", json.dumps(days))
                    db.set_setting("max_followups", str(max_fu))
            except Exception:
                await update.message.reply_text("Couldn't parse that. Using defaults.")
                db.set_setting("followup_days", json.dumps([3, 2]))
                db.set_setting("max_followups", "3")

        await _ask_auto_reply_mode(update, ctx)

    elif step == "auto_reply":
        text = update.message.text.strip().lower()
        if "trust" in text:
            db.set_setting("auto_reply_mode", "trust")
        else:
            db.set_setting("auto_reply_mode", "preview")
        await _ask_scrapingdog(update, ctx)

    elif step == "scrapingdog":
        text = update.message.text.strip()
        if text == "/skip":
            pass
        else:
            # Save to DB settings, env, and .env file
            db.set_setting("scrapingdog_api_key", text)
            os.environ["SCRAPINGDOG_API_KEY"] = text
            config.SCRAPINGDOG_API_KEY = text
            _persist_to_env("SCRAPINGDOG_API_KEY", text)
            await update.message.reply_text(f" Scrapingdog key saved: {_mask_key(text)}")
        await _finish_onboarding(update, ctx)


async def _try_add_account(text):
    """Parse 'email password' and add to DB."""
    parts = text.strip().split(maxsplit=1)
    if len(parts) != 2:
        return {"success": False, "error": "Format must be: email@gmail.com app_password"}

    email_addr, password = parts
    if "@" not in email_addr:
        return {"success": False, "error": "Invalid email address"}

    # Test connection
    test = email_sender.send_email_now.__self__ if False else None
    # We can't really test without sending, so just save it
    success = db.add_account(email_addr, password.replace(" ", ""))
    if not success:
        return {"success": False, "error": "Account already exists"}

    return {"success": True, "email": email_addr}


async def _ask_followup_settings(update, ctx):
    user_id = update.effective_user.id
    accounts = db.get_all_accounts()
    if not accounts:
        await update.message.reply_text(
            " No accounts added. You can add some later with /addaccount.\n\n"
            "Continuing setup..."
        )

    db.save_pending_action(user_id, "onboarding", {"step": "api_keys"})
    await update.message.reply_text(
        "<b> API Keys Setup</b>\n\n"
        "You need at least one AI key. All are free, no credit card.\n\n"
        "<b>Gemini</b> (recommended, does both OCR + email writing):\n"
        "Get key at https://aistudio.google.com\n\n"
        "<b>Mistral</b> (backup for both screenshots and email writing if Gemini is rate limited):\n"
        "Get key at https://console.mistral.ai\n\n"
        "<b>Groq</b> (fast text backup):\n"
        "Get key at https://console.groq.com\n\n"
        "<b>NVIDIA NIM</b> (another backup, both text and vision):\n"
        "Get key at https://build.nvidia.com\n\n"
        "Send your Gemini key first (most important), or /skip to continue.",
        parse_mode=ParseMode.HTML,
    )


async def _ask_followup_real(update, ctx):
    """Actual followup settings question (after API keys)."""
    user_id = update.effective_user.id
    db.save_pending_action(user_id, "onboarding", {"step": "followup"})
    await update.message.reply_text(
        "Now let's configure follow-ups.\n\n"
        "Default: First followup after <b>3 days</b>, second after <b>2 more days</b>, max <b>3 followups</b>.\n\n"
        "Send /default to use defaults, or send custom values like:\n"
        "<code>3 2 3</code>  (days between followups, last number is max count)\n\n"
        "Send /default to continue.",
        parse_mode=ParseMode.HTML,
    )


async def _ask_auto_reply_mode(update, ctx):
    user_id = update.effective_user.id
    db.save_pending_action(user_id, "onboarding", {"step": "auto_reply"})

    keyboard = [
        [InlineKeyboardButton("Preview before send", callback_data="onb_reply_preview")],
        [InlineKeyboardButton("Trust mode (auto-send)", callback_data="onb_reply_trust")],
    ]
    await update.message.reply_text(
        "How should I handle auto-replies?\n\n"
        "<b>Preview mode</b>: When a creator replies, I show you the draft and you approve before sending.\n\n"
        "<b>Trust mode</b>: I auto-send replies without preview.\n\n"
        "You can change this anytime with /settings.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


async def _ask_scrapingdog(update, ctx):
    user_id = update.effective_user.id
    db.save_pending_action(user_id, "onboarding", {"step": "scrapingdog"})
    await update.message.reply_text(
        "Last step: Scrapingdog API key (optional, for Instagram link scraping).\n\n"
        "Get a free key at https://scrapingdog.com (1000 free credits/month, no credit card).\n\n"
        "Send your key, or /skip to skip (you can still use screenshots for Instagram).",
    )


async def _finish_onboarding(update, ctx):
    db.mark_onboarded()
    db.clear_pending_action(update.effective_user.id)

    accounts = db.get_all_accounts()

    # Start background services
    reply_watcher.start_reply_watcher(_reply_notification_callback)
    followup_scheduler.start_scheduler(_followup_notification_callback)

    msg = (
        " Setup complete!\n\n"
        f" Accounts: {len(accounts)}\n"
        f" Daily total capacity: {db.total_remaining_today()}\n\n"
        "Background services started:\n"
        " Reply watcher (checks every 5 min)\n"
        " Followup scheduler\n\n"
        "Now just send me:\n"
        "• A screenshot of a creator's profile\n"
        "• A profile link\n"
        "• An email address\n\n"
        "Type /help for all commands."
    )
    await update.message.reply_text(msg)


def _persist_to_env(key, value):
    """Append/update a key in the .env file."""
    from pathlib import Path
    env_path = Path(__file__).parent / ".env"
    lines = []
    found = False
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith(f"{key}="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"{key}={value}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)


# 
#                       HELP & SETTINGS
# 

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    uid = update.effective_user.id
    mode = _user_mode.get(uid)
    if mode == "hunting":
        text = (
            "<b>🔍 Hunt Mode Commands</b>\n\n"
            "/scan - Start lookalike scan (prompts for seed usernames)\n"
            "/autoscan H - Setup recurring auto-scan (e.g. /autoscan 24, then send seeds)\n"
            "/autoscans - List active auto-scan schedules\n"
            "/stopautoscan ID - Stop an auto-scan by ID\n"
            "/cancelscan - Cancel the active scan run\n"
            "/scanhistory - List past scan runs\n"
            "/pushtocrm - Manually push matched profiles from last scan to CRM\n\n"
            "<b>🔧 Config & Filters</b>\n"
            "/scrapemode [mode] - View or set scrape mode (cookie | apify | hybrid)\n"
            "/setfilters min max - Set follower range filter (e.g. /setfilters 10000 500000)\n"
            "/setreach X.X - Set reach ratio filter (e.g. /setreach 0.8)\n"
            "/keywords <add|remove|clear> <reward|penalize> [word1, word2] - Configure keyword priority list\n\n"
            "<b>🍪 Cookies & APIs</b>\n"
            "/addcookie sessionid csrftoken [label] - Add Instagram session cookies\n"
            "/cookies - List Instagram session cookies\n"
            "/removecookie ID - Remove session cookies by ID\n"
            "/howtocookies - Instructions to extract Instagram cookies\n"
            "/addapify token [label] - Add Apify API token\n"
            "/apifytokens - List active Apify tokens\n"
            "/removeapify ID - Remove Apify token by ID\n\n"
            "<b>🎯 Seen profiles</b>\n"
            "/seen - Show count of already scanned profiles\n"
            "/clearseen - Clear already scanned profiles list\n\n"
            "<b>🚪 Navigation</b>\n"
            "/exit - Return to the main menu"
        )
    else:
        text = (
            "<b>📧 Outreach Mode Commands</b>\n\n"
            "<b>Send</b>\n"
            "Send screenshot / link / email / URL+email\n"
            "/bulk - paste many at once\n\n"
            "<b>Hot leads (priority)</b>\n"
            "/hot - creators awaiting your reply\n"
            "/reply ID - respond to their latest msg\n\n"
            "<b>CRM</b>\n"
            "/crm [page] [stage] - dashboard\n"
            "/creator ID - full detail view\n"
            "/won ID - mark deal won\n"
            "/lost ID - mark deal lost\n"
            "/note ID text - add note\n"
            "/resend ID - manual followup\n\n"
            "<b>Reports</b>\n"
            "/report - today's stats\n"
            "/today - detailed today breakdown\n"
            "/fullreport - 7-day breakdown\n"
            "/pipeline - by stage\n\n"
            "<b>Reply Watcher</b>\n"
            "/checkreplies - check now\n"
            "/replystatus - last/next check timer\n"
            "/startwatcher | /stopwatcher\n"
            "/setreplycheck N\n"
            "/setautoreply preview|trust\n\n"
            "<b>Gmail accounts</b>\n"
            "/accounts | /addaccount email pass\n"
            "/removeaccount email | /setlimit email N\n"
            "/pause email | /resume email\n\n"
            "<b>API Keys</b>\n"
            "/apikeys | /setgemini | /setmistral\n"
            "/setgroq | /setnvidia | /setscrapingdog\n\n"
            "<b>Settings</b>\n"
            "/settings | /setprompt\n"
            "/setinterval min max | /setfollowup\n\n"
            "<b>Queue + DMs</b>\n"
            "/queue | /startqueue | /stopqueue\n"
            "/dmlist | /dmdone N\n\n"
            "<b>Other</b>\n"
            "/preview name | /cancel | /exit"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.clear_pending_action(update.effective_user.id)
    await update.message.reply_text("Cancelled.")


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return

    min_int = db.get_setting("min_interval_seconds")
    max_int = db.get_setting("max_interval_seconds")
    followup_days = db.get_setting("followup_days")
    max_followups = db.get_setting("max_followups")
    reply_check = db.get_setting("reply_check_minutes")
    auto_reply = db.get_setting("auto_reply_mode")

    text = (
        "<b> Current Settings</b>\n\n"
        f"Send interval: {min_int}-{max_int} sec\n"
        f"Followup days: {followup_days}\n"
        f"Max followups: {max_followups}\n"
        f"Reply check: every {reply_check} min\n"
        f"Auto-reply mode: {auto_reply}\n\n"
        f"Use /setprompt to edit the AI system prompt.\n"
        f"Use /accounts to manage Gmail accounts."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_setprompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return

    text_parts = update.message.text.split(maxsplit=1)
    if len(text_parts) < 2:
        current = db.get_setting("system_prompt")
        await update.message.reply_text(
            f"<b>Current system prompt:</b>\n\n<pre>{_escape_html(str(current)[:3000])}</pre>\n\n"
            f"To change it, reply with /setprompt followed by your new prompt.",
            parse_mode=ParseMode.HTML,
        )
        return
        
    new_prompt = text_parts[1].strip()
    db.set_setting("system_prompt", new_prompt)
    await update.message.reply_text("✅ System prompt updated successfully.")


async def cmd_setinterval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    if len(ctx.args) != 2:
        await update.message.reply_text("Usage: /setinterval <min_seconds> <max_seconds>")
        return
    try:
        min_s = int(ctx.args[0])
        max_s = int(ctx.args[1])
        db.set_setting("min_interval_seconds", str(min_s))
        db.set_setting("max_interval_seconds", str(max_s))
        await update.message.reply_text(f" Send interval set to {min_s}-{max_s}s")
    except ValueError:
        await update.message.reply_text("Both values must be integers.")


async def cmd_setfollowup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    if not ctx.args:
        days = db.get_setting("followup_days")
        max_fu = db.get_setting("max_followups")
        await update.message.reply_text(
            f"Current: days={days}, max={max_fu}\n"
            f"Usage: /setfollowup 3 2 3 (3 days after opener, 2 days between, max 3)"
        )
        return
    try:
        nums = [int(x) for x in ctx.args]
        days = nums[:-1]
        max_fu = nums[-1]
        db.set_setting("followup_days", json.dumps(days))
        db.set_setting("max_followups", str(max_fu))
        await update.message.reply_text(f" Followup: days={days}, max={max_fu}")
    except Exception:
        await update.message.reply_text("Couldn't parse. Use: /setfollowup 3 2 3")


async def cmd_setreplycheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text(f"Current: {db.get_setting('reply_check_minutes')} min")
        return
    try:
        mins = int(ctx.args[0])
        db.set_setting("reply_check_minutes", str(mins))
        await update.message.reply_text(f" Reply check interval: {mins} min")
    except ValueError:
        await update.message.reply_text("Must be an integer (minutes).")


async def cmd_setautoreply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    if not ctx.args or ctx.args[0] not in ("preview", "trust"):
        await update.message.reply_text("Usage: /setautoreply preview|trust")
        return
    db.set_setting("auto_reply_mode", ctx.args[0])
    await update.message.reply_text(f" Auto-reply mode: {ctx.args[0]}")


# 
#                       ACCOUNTS
# 

async def cmd_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    accounts = db.get_all_accounts()
    aliases = db.get_all_aliases()
    
    if not accounts and not aliases:
        await update.message.reply_text("No accounts or aliases found. Add one with /addaccount")
        return
        
    messages = []
    text = "<b> Gmail Accounts & Aliases</b>\n\n"
    
    for a in accounts:
        status = "✅" if a["active"] else "❌"
        block = f"{status} <code>{a['email']}</code>\n   Sent today: {a['sent_today']}/{a['daily_limit']}\n"
        
        master_aliases = [al for al in aliases if al["smtp_user"] == a["email"]]
        for al in master_aliases:
            al_status = "✅" if al["is_active"] else "❌"
            block += f"   ↳ {al_status} <code>{al['alias']}</code> ({al['daily_sent']}/{al['daily_limit']})\n"
        block += "\n"
        
        if len(text) + len(block) > 3800:
            messages.append(text)
            text = block
        else:
            text += block
            
    # Handle orphaned aliases
    master_emails = {a['email'] for a in accounts}
    orphaned = [al for al in aliases if al["smtp_user"] not in master_emails]
    if orphaned:
        block = "<b>Orphaned Aliases (Master Not Found)</b>\n"
        for al in orphaned:
            al_status = "✅" if al["is_active"] else "❌"
            block += f"   ↳ {al_status} <code>{al['alias']}</code> via {al['smtp_user']} ({al['daily_sent']}/{al['daily_limit']})\n"
        block += "\n"
        if len(text) + len(block) > 3800:
            messages.append(text)
            text = block
        else:
            text += block
            
    text += f"Total remaining today: <b>{db.total_remaining_today()}</b>"
    messages.append(text)
    
    for msg in messages:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_addaccount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    # Parse manually since app passwords can have spaces
    text = update.message.text.split(maxsplit=1)
    if len(text) < 2:
        await update.message.reply_text("Usage: /addaccount email@gmail.com app_password")
        return
    parts = text[1].strip().split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /addaccount email@gmail.com app_password\nApp password is required.")
        return
    email_addr = parts[0].strip()
    # Strip spaces from app password (Gmail shows them with spaces but uses without)
    password = parts[1].strip().replace(" ", "")
    if "@" not in email_addr:
        await update.message.reply_text("Invalid email address.")
        return
    if len(password) < 8:
        await update.message.reply_text("App password looks too short. Gmail app passwords are 16 characters.")
        return
    success = db.add_account(email_addr, password)
    if success:
        await update.message.reply_text(
            f" Added {email_addr}\n"
            f"Daily limit: 50 (change with /setlimit)\n"
            f"Status: active"
        )
    else:
        await update.message.reply_text("Account already exists. Use /removeaccount first to update.")


async def cmd_removeaccount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /removeaccount email@gmail.com")
        return
    email_addr = ctx.args[0]
    result = db.remove_account_safe(email_addr)
    if result["success"]:
        await update.message.reply_text(f" Removed {email_addr}")
    else:
        await update.message.reply_text(f" {result['message']}\n\nUse /pause {email_addr} instead to stop sending without losing history.")


async def cmd_addalias(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /addalias <alias@domain.com> <smtp_user@gmail.com> <app_password>")
        return
    parts = args[1].strip().split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("Provide alias, smtp user, and app password.")
        return
        
    alias, smtp_user, smtp_pass = parts[0].strip(), parts[1].strip(), parts[2].strip().replace(" ", "")
    
    success = db.add_smtp_alias(alias, smtp_user, smtp_pass)
    if success:
        await update.message.reply_text(f" Added alias {alias} (via {smtp_user}).\nStarts at 10 emails/day (Warmup mode).")
    else:
        await update.message.reply_text("Alias already exists.")

async def cmd_bulkaddalias(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    args = update.message.text.replace("/bulkaddalias", "").strip().split()
    if len(args) < 4:
        await update.message.reply_text("Usage: /bulkaddalias <domain.com> <smtp_user@gmail.com> <app_password_without_spaces> <alias1,alias2...>")
        return
        
    domain = args[0].strip()
    smtp_user = args[1].strip()
    smtp_pass = args[2].strip()
    aliases_str = args[3].strip()
    
    aliases = [a.strip() for a in aliases_str.split(",") if a.strip()]
    
    added = 0
    for prefix in aliases:
        full_alias = f"{prefix}@{domain}"
        if db.add_smtp_alias(full_alias, smtp_user, smtp_pass):
            added += 1
            
    await update.message.reply_text(f" Successfully added {added} aliases to the database!")

async def cmd_listaliases(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    await update.message.reply_text("The /listaliases command is deprecated! Please use /accounts to see your complete sending infrastructure.")

async def cmd_togglealias(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    args = update.message.text.split()
    if len(args) < 2:
        await update.message.reply_text("Usage: /togglealias <alias@domain.com>")
        return
    status = db.toggle_alias(args[1].strip())
    if status is None:
        await update.message.reply_text("Alias not found.")
    else:
        await update.message.reply_text(f"Alias {'enabled' if status else 'disabled'}.")

async def cmd_removealias(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    args = update.message.text.split()
    if len(args) < 2:
        await update.message.reply_text("Usage: /removealias <alias@domain.com>")
        return
        
    result = db.remove_alias_safe(args[1].strip())
    await update.message.reply_text(result["message"])

async def cmd_pausemasteraliases(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    args = update.message.text.split()
    if len(args) < 2:
        await update.message.reply_text("Usage: /pausemasteraliases <master_email@gmail.com>")
        return
    count = db.set_master_aliases_status(args[1].strip(), 0)
    await update.message.reply_text(f"Paused {count} aliases under {args[1].strip()}.")

async def cmd_resumemasteraliases(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    args = update.message.text.split()
    if len(args) < 2:
        await update.message.reply_text("Usage: /resumemasteraliases <master_email@gmail.com>")
        return
    count = db.set_master_aliases_status(args[1].strip(), 1)
    await update.message.reply_text(f"Resumed {count} aliases under {args[1].strip()}.")

async def cmd_removeseed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Remove a seed: /removeseed @username"""
    if not authorized(update.effective_user.id): return
    
    text = update.message.text or ""
    handles = re.findall(r'@?([a-zA-Z0-9_.]+)', text.replace("/removeseed", "").strip())
    
    if not handles:
        await update.message.reply_text(
            "Usage: <code>/removeseed @username</code>",
            parse_mode="HTML"
        )
        return
    
    removed = 0
    for h in handles:
        if db.remove_seed(h):
            removed += 1
    
    if removed:
        await update.message.reply_text(f"🗑 Removed {removed} seed(s).")
    else:
        await update.message.reply_text("❌ Seed(s) not found or already inactive.")

async def cmd_updateemail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Update a creator's email: /updateemail @username new@email.com"""
    if not authorized(update.effective_user.id): return
    args = update.message.text.split()
    if len(args) != 3:
        await update.message.reply_text("Usage: `/updateemail @username new@email.com`", parse_mode="HTML")
        return
        
    username = args[1].lstrip("@").strip().lower()
    new_email = args[2].strip().lower()
    
    conn = db.get_db()
    creator = conn.execute("SELECT id FROM creators WHERE LOWER(handle)=?", (username,)).fetchone()
    if not creator:
        conn.close()
        await update.message.reply_text(f"❌ Could not find creator @{username} in the CRM.")
        return
        
    try:
        conn.execute("UPDATE creators SET email=? WHERE id=?", (new_email, creator["id"]))
        conn.commit()
        await update.message.reply_text(f"✅ Successfully updated email for @{username} to {new_email}!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error updating email (might be a duplicate): {e}")
    finally:
        conn.close()

async def cmd_stopseed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    import discovery
    discovery.stop_scrape()
    await update.message.reply_text("🛑 Sent stop signal to the active hunting pipeline! The scrape will abort momentarily.")

async def cmd_setlimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    if len(ctx.args) != 2:
        await update.message.reply_text("Usage: /setlimit <email_or_alias> <limit>")
        return
    target, limit_str = ctx.args[0], ctx.args[1]
    try:
        limit = int(limit_str)
        # Try as alias first
        db.update_alias_limit(target, limit)
        # Also try as account
        db.set_account_limit(target, limit)
        await update.message.reply_text(f" Limit for {target} set to {limit}")
    except ValueError:
        await update.message.reply_text("Limit must be an integer.")

async def cmd_aliasesstats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Alias for listaliases
    await cmd_listaliases(update, ctx)

async def cmd_warmupstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Alias for listaliases for now, as it shows warmup day and limit
    await cmd_listaliases(update, ctx)


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /pause email@gmail.com")
        return
    db.set_account_active(ctx.args[0], False)
    await update.message.reply_text(f" Paused {ctx.args[0]}")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /resume email@gmail.com")
        return
    db.set_account_active(ctx.args[0], True)
    await update.message.reply_text(f" Resumed {ctx.args[0]}")


# 
#                       REPORTS
# 

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    full = db.get_full_report()
    reply_rate = f"\nReply rate: {(full['total_replies']/full['total_sent']*100):.1f}%" if full['total_sent'] else ""
    text = (
        f"<b> Today</b>\n"
        f"Sent: {full['today_sent']} | Replies: {full['today_replies']}\n"
        f"Remaining capacity: {db.total_remaining_today()}\n\n"
        f"<b> All time</b>\n"
        f"Sent: {full['total_sent']}\n"
        f"Failed: {full['total_failed']}\n"
        f"Queued: {full['total_queued']}\n"
        f"Replies: {full['total_replies']}\n"
        f"Creators: {full['total_creators']}{reply_rate}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_fullreport(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    full = db.get_full_report()
    text = "<b> 7-Day Breakdown</b>\n\n"
    for day in full["daily_breakdown"]:
        text += f"  {day['date']}: {day['count']}\n"
    if not full["daily_breakdown"]:
        text += "No data yet.\n"
    text += f"\n<b>Total sent:</b> {full['total_sent']}\n<b>Total replies:</b> {full['total_replies']}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_pipeline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    breakdown = db.get_pipeline_breakdown()
    if not breakdown:
        await update.message.reply_text("No creators in pipeline yet.")
        return
    text = "<b> Pipeline</b>\n\n"
    for stage, count in sorted(breakdown.items(), key=lambda x: -x[1]):
        text += f"  {stage}: {count}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    from database import get_db
    conn = get_db()
    q_count = conn.execute("SELECT COUNT(*) as c FROM emails_sent WHERE status='queued'").fetchone()["c"]
    conn.close()
    text = (
        f"<b> Queue</b>\n\n"
        f"Queued: {q_count}\n"
        f"Status: {' Running' if email_sender.is_queue_running() else ' Idle'}\n"
        f"Remaining capacity today: {db.total_remaining_today()}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_startqueue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return

    chat_id = update.effective_chat.id

    def callback(msg):
        _schedule_notification(chat_id, f" {msg}")

    result = email_sender.start_queue_processor(callback)
    await update.message.reply_text(f" {result}")


async def cmd_stopqueue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    result = email_sender.stop_queue_processor()
    await update.message.reply_text(f" {result}")


async def cmd_startwatcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    result = reply_watcher.start_reply_watcher(_reply_notification_callback)
    await update.message.reply_text(f" {result}")


async def cmd_stopwatcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    result = reply_watcher.stop_reply_watcher()
    await update.message.reply_text(f" {result}")


# 
#                       DM REMINDERS
# 

async def cmd_dmlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    rems = db.get_pending_dm_reminders()
    if not rems:
        await update.message.reply_text("No pending DM reminders.")
        return
    text = "<b> Pending DMs</b>\n\n"
    for r in rems[:30]:
        text += f"#{r['id']} — {r['name']} (@{r['handle']}) on {r['platform']}\n"
    text += "\nMark done: /dmdone &lt;id&gt;"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_dmdone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /dmdone <id>")
        return
    try:
        db.mark_dm_done(int(ctx.args[0]))
        await update.message.reply_text(f" Marked done.")
    except ValueError:
        await update.message.reply_text("Invalid ID.")


async def cmd_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    name = " ".join(ctx.args) if ctx.args else "Sample Creator"
    email_data = ai_router.generate_opener_email({
        "name": name, "handle": name.lower().replace(" ", ""),
        "platform": "instagram", "followers": 100000, "tier": "100k_plus",
        "bio": "AI and tech creator", "niche": "AI and tech",
    })
    await update.message.reply_text(
        f"<b>Subject:</b> {email_data['subject']}\n\n{email_data['body']}",
        parse_mode=ParseMode.HTML,
    )


# 
#              REPLY CHECKER + TIMER
# 

async def cmd_checkreplies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manual one-time reply check. Runs in background thread so it doesn't freeze the bot."""
    if not authorized(update.effective_user.id):
        return

    accounts = db.get_all_accounts()
    active_accounts = [a for a in accounts if a["active"]]
    if not active_accounts:
        await update.message.reply_text("No active Gmail accounts to check.")
        return

    await update.message.reply_text(f" Checking {len(active_accounts)} account(s) for replies...")

    loop = asyncio.get_event_loop()
    try:
        new_replies = await loop.run_in_executor(
            None, reply_watcher.check_now, _reply_notification_callback
        )
    except Exception as e:
        await update.message.reply_text(f" Check failed: {e}")
        return

    if new_replies:
        await update.message.reply_text(
            f" Found {len(new_replies)} new reply(s)!\n"
            f"Notifications sent above."
        )
    else:
        await update.message.reply_text("No new replies. All quiet.")


async def cmd_replystatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show reply watcher status: last check, next check, countdown."""
    if not authorized(update.effective_user.id):
        return

    from datetime import datetime as dt

    watcher_running = reply_watcher.is_running()
    last_check = db.get_last_reply_check()
    next_check = db.get_next_reply_check()
    check_interval = db.get_setting("reply_check_minutes", "5")

    now = dt.now()

    last_str = "Never"
    ago_str = ""
    if last_check:
        last_str = last_check.strftime("%H:%M:%S")
        ago = now - last_check
        ago_mins = int(ago.total_seconds() / 60)
        ago_str = f" ({ago_mins}m ago)"

    next_str = "N/A"
    countdown_str = ""
    if next_check and watcher_running:
        next_str = next_check.strftime("%H:%M:%S")
        remaining = next_check - now
        remaining_mins = max(0, int(remaining.total_seconds() / 60))
        remaining_secs = max(0, int(remaining.total_seconds() % 60))
        countdown_str = f" ({remaining_mins}m {remaining_secs}s)"

    text = (
        f"<b> Reply Watcher Status</b>\n\n"
        f"Status: {' Running' if watcher_running else ' Stopped'}\n"
        f"Interval: every {check_interval} min\n"
        f"Last check: {last_str}{ago_str}\n"
        f"Next check: {next_str}{countdown_str}\n\n"
        f"Manual check: /checkreplies"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# 
#              API KEY MANAGEMENT
# 

def _mask_key(key: str) -> str:
    if not key:
        return " Not set"
    if len(key) <= 8:
        return " Set (***)"
    return f" {key[:4]}...{key[-4:]}"


async def cmd_apikeys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return

    gemini = db.get_setting("gemini_api_key", "") or config.GEMINI_API_KEY
    mistral = db.get_setting("mistral_api_key", "") or config.MISTRAL_API_KEY
    groq = db.get_setting("groq_api_key", "") or config.GROQ_API_KEY
    nvidia = db.get_setting("nvidia_api_key", "") or config.NVIDIA_API_KEY
    scrapingdog = db.get_setting("scrapingdog_api_key", "") or config.SCRAPINGDOG_API_KEY
    openrouter = db.get_setting("openrouter_api_key", "")
    openrouter_model = db.get_setting("openrouter_model", "meta-llama/llama-3.1-8b-instruct:free")

    text = (
        f"<b> API Keys</b>\n\n"
        f"<i>Text generation, in fallback order:</i>\n"
        f"OpenRouter ({openrouter_model}): {_mask_key(openrouter)}\n"
        f"Gemini: {_mask_key(gemini)}\n"
        f"Mistral (Large): {_mask_key(mistral)}\n"
        f"Groq: {_mask_key(groq)}\n"
        f"NVIDIA NIM: {_mask_key(nvidia)}\n\n"
        f"<i>Vision (screenshot OCR), in fallback order:</i>\n"
        f"Gemini: {_mask_key(gemini)}\n"
        f"Mistral (Pixtral): {_mask_key(mistral)}\n"
        f"NVIDIA NIM: {_mask_key(nvidia)}\n\n"
        f"<i>Scraping:</i>\n"
        f"Scrapingdog: {_mask_key(scrapingdog)}\n\n"
        f"Update: /setopenrouter /setgemini /setmistral /setgroq /setnvidia /setscrapingdog\n"
        f"Model: /setopenroutermodel &lt;model&gt;\n"
        f"All free, no credit card needed."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def _set_api_key(update, key_name, setting_key, env_var):
    """Generic API key setter."""
    msg = update.message or update.edited_message
    if not msg or not msg.text:
        return
        
    text = msg.text.split(maxsplit=1)
    if len(text) < 2 or not text[1].strip():
        current = db.get_setting(setting_key, "")
        await msg.reply_text(
            f"Current: {_mask_key(current)}\n"
            f"Usage: /{key_name.lower().replace(' ','')} YOUR_KEY_HERE"
        )
        return

    key = text[1].strip()
    if key.lower() in ("none", "null", "clear", "empty"):
        db.set_setting(setting_key, "")
        setattr(config, env_var, "")
        if env_var in os.environ:
            del os.environ[env_var]
        _persist_to_env(env_var, "")
        await update.message.reply_text(f" {key_name} key cleared.")
        return

    db.set_setting(setting_key, key)
    # Also update runtime config
    setattr(config, env_var, key)
    os.environ[env_var] = key
    _persist_to_env(env_var, key)
    await update.message.reply_text(f" {key_name} key saved: {_mask_key(key)}")


async def cmd_setopenrouter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    await _set_api_key(update, "OpenRouter", "openrouter_api_key", "OPENROUTER_API_KEY")
    import ai_router
    ai_router.OPENROUTER_API_KEY = db.get_setting("openrouter_api_key", "")

async def cmd_setopenroutermodel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    text = update.message.text.split(maxsplit=1)
    if len(text) < 2 or not text[1].strip():
        current = db.get_setting("openrouter_model", "meta-llama/llama-3.1-8b-instruct:free")
        await update.message.reply_text(f"Current OpenRouter model: {current}\nUsage: /setopenroutermodel <model_id>")
        return
    new_model = text[1].strip()
    db.set_setting("openrouter_model", new_model)
    await update.message.reply_text(f"OpenRouter model saved: {new_model}")

async def cmd_setgemini(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    await _set_api_key(update, "Gemini", "gemini_api_key", "GEMINI_API_KEY")
    # Reload into ai_router
    import ai_router
    ai_router.GEMINI_API_KEY = db.get_setting("gemini_api_key", "")


async def cmd_setmistral(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    await _set_api_key(update, "Mistral", "mistral_api_key", "MISTRAL_API_KEY")
    # ocr_router reads this live from the DB, no reload needed there.
    # ai_router caches it at module level for the text chain, reload it.
    import ai_router
    ai_router.MISTRAL_API_KEY = db.get_setting("mistral_api_key", "")


async def cmd_setgroq(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    await _set_api_key(update, "Groq", "groq_api_key", "GROQ_API_KEY")
    import ai_router
    ai_router.GROQ_API_KEY = db.get_setting("groq_api_key", "")


async def cmd_setnvidia(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    await _set_api_key(update, "NVIDIA NIM", "nvidia_api_key", "NVIDIA_API_KEY")
    import ai_router
    ai_router.NVIDIA_API_KEY = db.get_setting("nvidia_api_key", "")


async def cmd_setscrapingdog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    await _set_api_key(update, "Scrapingdog", "scrapingdog_api_key", "SCRAPINGDOG_API_KEY")
    config.SCRAPINGDOG_API_KEY = db.get_setting("scrapingdog_api_key", "")


async def cmd_setcftoken(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    text = update.message.text.split(maxsplit=1)
    if len(text) < 2 or not text[1].strip():
        current = db.get_setting("cf_token", "")
        await update.message.reply_text(f"Current CF Token: {_mask_key(current)}\nUsage: /setcftoken <token>")
        return
    db.set_setting("cf_token", text[1].strip())
    await update.message.reply_text("Cloudflare API Token saved.")

async def cmd_setcfzone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    text = update.message.text.split(maxsplit=1)
    if len(text) < 2 or not text[1].strip():
        current = db.get_setting("cf_zone", "")
        await update.message.reply_text(f"Current CF Zone ID: {current}\nUsage: /setcfzone <zone_id>")
        return
    db.set_setting("cf_zone", text[1].strip())
    await update.message.reply_text("Cloudflare Zone ID saved.")

async def cmd_createaliases(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /createaliases <master_email@gmail.com> <alias1,alias2,alias3>")
        return
        
    master_email = args[0]
    aliases = args[1].split(",")
    
    cf_token = db.get_setting("cf_token", "")
    cf_zone = db.get_setting("cf_zone", "")
    if not cf_token or not cf_zone:
        await update.message.reply_text("Please set /setcftoken and /setcfzone first.")
        return
        
    headers = {
        "Authorization": f"Bearer {cf_token}",
        "Content-Type": "application/json"
    }
    
    # 1. Get domain name from zone ID
    import requests
    zone_resp = requests.get(f"https://api.cloudflare.com/client/v4/zones/{cf_zone}", headers=headers)
    if not zone_resp.ok:
        await update.message.reply_text(f"Failed to fetch zone details. Check your token and zone ID. Response: {zone_resp.text}")
        return
    domain = zone_resp.json()["result"]["name"]
    
    results = []
    for alias_prefix in aliases:
        alias_prefix = alias_prefix.strip()
        if not alias_prefix: continue
        
        full_alias = f"{alias_prefix}@{domain}"
        
        payload = {
            "name": f"Route {full_alias} to {master_email}",
            "enabled": True,
            "matchers": [{"type": "literal", "field": "to", "value": full_alias}],
            "actions": [{"type": "forward", "value": [master_email]}]
        }
        
        url = f"https://api.cloudflare.com/client/v4/zones/{cf_zone}/email/routing/rules"
        resp = requests.post(url, headers=headers, json=payload)
        
        if resp.ok:
            results.append(f"✅ Created: {full_alias} -> {master_email}")
        else:
            results.append(f"❌ Failed {full_alias}: {resp.json().get('errors', resp.text)}")
            
    await update.message.reply_text("\n".join(results) or "No aliases processed.")

# 
#              CRM DASHBOARD
# 

async def cmd_crm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """CRM dashboard - paginated list of all creators with status."""
    if not authorized(update.effective_user.id):
        return

    # Parse page number and optional stage filter
    page = 1
    stage_filter = None
    for arg in (ctx.args or []):
        try:
            page = int(arg)
        except ValueError:
            stage_filter = arg

    per_page = 10
    offset = (page - 1) * per_page
    total = db.get_total_creator_count(stage_filter)
    total_pages = max(1, (total + per_page - 1) // per_page)
    creators = db.get_all_creators(limit=per_page, offset=offset, stage_filter=stage_filter)

    if not creators:
        await update.message.reply_text("No creators in the pipeline yet.")
        return

    stage_icons = {
        "new": "", "opener_sent": "", "followup_1_sent": "",
        "followup_2_sent": "", "followup_3_sent": "",
        "replied": "", "negotiating": "", "interested": "",
        "needs_info": "", "manual_handling": "",
        "closed_won": "", "closed_lost": "",
    }

    text = f"<b> CRM Dashboard</b> (page {page}/{total_pages}, {total} total)\n"
    if stage_filter:
        text += f"Filter: {stage_filter}\n"
    text += "\n"

    for c in creators:
        icon = stage_icons.get(c["stage"], "")
        name = c["name"] or "Unknown"
        fol = f" ({c['followers']:,})" if c["followers"] else ""
        handle_str = f" @{c['handle']}" if c["handle"] else ""
        date_str = c["added_at"][:10] if c["added_at"] else ""
        text += f"{icon} <b>#{c['id']}</b> {name}{handle_str}{fol}\n"
        text += f"   {c['stage']} | {c['email']}\n"
        text += f"   Added: {date_str} | {c['tier'] or '?'}\n\n"

    text += f"Detail: /creator <code>ID</code>\n"
    if page < total_pages:
        text += f"Next: /crm {page + 1}"
    if stage_filter:
        text += f"\nClear filter: /crm"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_creator(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Full detail view of a single creator."""
    if not authorized(update.effective_user.id):
        return

    if not ctx.args:
        await update.message.reply_text("Usage: /creator <id>")
        return

    try:
        creator_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID must be a number.")
        return

    data = db.get_creator_full_detail(creator_id)
    if not data:
        await update.message.reply_text("Creator not found.")
        return

    c = data["creator"]
    fol = f"{c['followers']:,}" if c["followers"] else "Unknown"

    text = (
        f"<b> Creator #{c['id']}</b>\n\n"
        f"<b>Name:</b> {c['name'] or 'Unknown'}\n"
        f"<b>Handle:</b> @{c['handle'] or '?'}\n"
        f"<b>Platform:</b> {c['platform']}\n"
        f"<b>Email:</b> <code>{c['email']}</code>\n"
        f"<b>Followers:</b> {fol}\n"
        f"<b>Tier:</b> {c['tier'] or '?'}\n"
        f"<b>Stage:</b> {c['stage']}\n"
        f"<b>Niche:</b> {c['niche'] or '?'}\n"
        f"<b>Source:</b> {c['source']}\n"
        f"<b>Added:</b> {c['added_at'][:16] if c['added_at'] else '?'}\n"
        f"<b>Last contact:</b> {c['last_contact'][:16] if c['last_contact'] else 'Never'}\n"
    )

    if c['bio']:
        text += f"\n<b>Bio:</b> {_escape_html(c['bio'][:200])}\n"

    if c['notes']:
        text += f"\n<b> Notes:</b> {_escape_html(c['notes'])}\n"

    # Emails sent
    if data["emails"]:
        text += f"\n<b> Emails ({len(data['emails'])})</b>\n"
        for e in data["emails"]:
            status_icon = {"sent": "", "failed": "", "queued": ""}.get(e["status"], "")
            date = e["sent_at"][:10] if e["sent_at"] else e["queued_at"][:10] if e["queued_at"] else "?"
            text += f"  {status_icon} {e['message_type']} | {date} | from {e['from_account'] or '?'}\n"

    # Replies
    if data["replies"]:
        text += f"\n<b> Replies ({len(data['replies'])})</b>\n"
        for r in data["replies"]:
            handled = "" if r["handled"] else ""
            date = r["received_at"][:10] if r["received_at"] else "?"
            text += f"  {handled} {date} | {_escape_html((r['body'] or '')[:80])}\n"

    # Followups
    if data["followups"]:
        text += f"\n<b> Followups</b>\n"
        for f in data["followups"]:
            status = {"pending": "", "sent": "", "cancelled": ""}.get(f["status"], "?")
            text += f"  {status} FU#{f['followup_number']} | {f['scheduled_for'][:10] if f['scheduled_for'] else '?'} | {f['status']}\n"

    # Truncate if too long for Telegram
    if len(text) > 3500:
        text = text[:3400] + "\n\n<i>(truncated)</i>"

    text += (
        f"\n\n<b>Quick actions:</b>\n"
        f"/won {c['id']} | /lost {c['id']} | /note {c['id']} text\n"
        f"/resend {c['id']} — Send manual followup\n"
        f"/reply {c['id']} — Reply to their latest message"
    )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# 
#              QUICK STAGE CHANGES + NOTES
# 

async def cmd_won(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Mark deal as won."""
    if not authorized(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /won <creator_id>")
        return
    try:
        cid = int(ctx.args[0])
        creator = db.get_creator(cid)
        if not creator:
            await update.message.reply_text("Creator not found.")
            return
        db.update_creator_stage(cid, "closed_won")
        db.cancel_followups_for_creator(cid)
        await update.message.reply_text(f" {creator['name']} marked as WON. Followups cancelled.")
    except ValueError:
        await update.message.reply_text("ID must be a number.")


async def cmd_lost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Mark deal as lost."""
    if not authorized(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /lost <creator_id>")
        return
    try:
        cid = int(ctx.args[0])
        creator = db.get_creator(cid)
        if not creator:
            await update.message.reply_text("Creator not found.")
            return
        db.update_creator_stage(cid, "closed_lost")
        db.cancel_followups_for_creator(cid)
        await update.message.reply_text(f" {creator['name']} marked as lost. Followups cancelled.")
    except ValueError:
        await update.message.reply_text("ID must be a number.")


async def cmd_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Add a note to a creator."""
    if not authorized(update.effective_user.id):
        return
    text = update.message.text.split(maxsplit=2)
    if len(text) < 3:
        await update.message.reply_text("Usage: /note <creator_id> <note text>")
        return
    try:
        cid = int(text[1])
        note = text[2]
        creator = db.get_creator(cid)
        if not creator:
            await update.message.reply_text("Creator not found.")
            return
        # Append to existing notes
        existing = creator['notes'] or ""
        from datetime import datetime as dt
        timestamp = dt.now().strftime("%m/%d %H:%M")
        new_notes = f"{existing}\n[{timestamp}] {note}" if existing else f"[{timestamp}] {note}"
        db.update_creator_notes(cid, new_notes.strip())
        await update.message.reply_text(f" Note added to {creator['name']}")
    except ValueError:
        await update.message.reply_text("Creator ID must be a number.")


# 
#              HOT LEADS + RESEND + REPLY
# 

async def cmd_hot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show creators who replied but haven't been responded to."""
    if not authorized(update.effective_user.id):
        return

    leads = db.get_hot_leads()
    if not leads:
        await update.message.reply_text(" No hot leads right now. Inbox zero!")
        return

    text = f"<b> Hot Leads ({len(leads)})</b>\n"
    text += "<i>Creators who replied, awaiting your response</i>\n\n"

    for l in leads[:15]:
        date = (l['last_reply_at'] or "?")[:16]
        preview = _escape_html((l['last_reply'] or "")[:100])
        text += (
            f"<b>#{l['id']}</b> {l['name'] or '?'} ({l['stage']})\n"
            f"  {date}\n"
            f"  <i>{preview}</i>\n"
            f"  /reply {l['id']} | /creator {l['id']}\n\n"
        )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Reply to a creator's latest unhandled message."""
    if not authorized(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /reply <creator_id>\n\nUse /hot to see leads awaiting reply.")
        return
    try:
        cid = int(ctx.args[0])
        creator = db.get_creator(cid)
        if not creator:
            await update.message.reply_text("Creator not found.")
            return

        latest_reply = db.get_unhandled_reply_for_creator(cid)
        if not latest_reply:
            await update.message.reply_text(
                f"No unhandled reply from {creator['name']}.\n"
                f"Use /creator {cid} to see history."
            )
            return

        # Trigger the auto-reply flow on this reply
        keyboard = [
            [InlineKeyboardButton(" Use default deal", callback_data=f"reply_default_{latest_reply['id']}")],
            [InlineKeyboardButton(" Custom instruction", callback_data=f"reply_custom_{latest_reply['id']}")],
            [InlineKeyboardButton(" Manual (skip)", callback_data=f"reply_manual_{latest_reply['id']}")],
        ]
        await update.message.reply_text(
            f"<b>Reply to {creator['name']}</b>\n\n"
            f"<i>Their message:</i>\n{_escape_html((latest_reply['body'] or '')[:500])}\n\n"
            f"How should I respond?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except ValueError:
        await update.message.reply_text("ID must be a number.")


async def cmd_resend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manually trigger a followup for a creator."""
    if not authorized(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /resend <creator_id>")
        return
    try:
        cid = int(ctx.args[0])
        creator = db.get_creator(cid)
        if not creator:
            await update.message.reply_text("Creator not found.")
            return

        # Count previous followups
        from database import get_db
        conn = get_db()
        fu_count = conn.execute(
            "SELECT COUNT(*) as c FROM emails_sent WHERE creator_id=? AND message_type LIKE 'followup%' AND status='sent'",
            (cid,)
        ).fetchone()["c"]
        conn.close()

        followup_num = fu_count + 1
        await update.message.reply_text(f" Generating followup #{followup_num} for {creator['name']}...")

        previous_emails = db.get_emails_for_creator(cid)
        creator_info = {
            "name": creator['name'], "handle": creator['handle'],
            "followers": creator['followers'], "tier": creator['tier'],
            "bio": creator['bio'], "niche": creator['niche'],
        }
        email_data = ai_router.generate_followup(creator_info, previous_emails, followup_num)

        result = email_sender.send_with_logging(
            creator_id=cid,
            to_email=creator['email'],
            subject=email_data['subject'],
            body=email_data['body'],
            message_type=f"followup_{followup_num}_manual",
        )

        if result.get("success"):
            db.update_creator_stage(cid, f"followup_{followup_num}_sent")
            await update.message.reply_text(
                f" Followup #{followup_num} sent to {creator['email']}\n\n"
                f"<b>Subject:</b> {email_data['subject']}\n\n"
                f"{email_data['body'][:400]}...",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(f" Failed: {result.get('error')}")
    except ValueError:
        await update.message.reply_text("ID must be a number.")


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Detailed today breakdown."""
    if not authorized(update.effective_user.id):
        return

    breakdown = db.get_today_breakdown()
    if not breakdown:
        await update.message.reply_text("Nothing happened today yet.")
        return

    text = "<b> Today's Activity</b>\n\n"
    by_type = {}
    for row in breakdown:
        mtype = row["message_type"]
        if mtype not in by_type:
            by_type[mtype] = {"sent": 0, "failed": 0, "queued": 0}
        by_type[mtype][row["status"]] = row["c"]

    for mtype, stats in by_type.items():
        text += f"<b>{mtype}</b>:  {stats['sent']} |  {stats['failed']} |  {stats['queued']}\n"

    today_replies = db.get_full_report()['today_replies']
    text += f"\n<b>Replies received:</b> {today_replies}"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# 
#              BULK MODE
# 

async def cmd_bulk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Enter bulk mode - paste many URLs/emails at once."""
    if not authorized(update.effective_user.id):
        return
    db.save_pending_action(
        update.effective_user.id,
        "bulk_mode",
        {"step": "awaiting_list"}
    )
    await update.message.reply_text(
        "<b> Bulk Mode</b>\n\n"
        "Paste a list of profile URLs, emails, or URL+email combos.\n"
        "One per line. Examples:\n\n"
        "<code>creator1@email.com</code>\n"
        "<code>https://instagram.com/creator2 creator2@email.com</code>\n"
        "<code>creator3@email.com</code>\n\n"
        "I'll queue all of them with random intervals between sends.\n"
        "Send /cancel to abort.",
        parse_mode=ParseMode.HTML,
    )


# 
#                MESSAGE HANDLERS (photos, links, text)
# 

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return

    # Check if we're in an onboarding flow
    pending = db.get_pending_action(update.effective_user.id)
    if pending and pending["action_type"] == "onboarding":
        await update.message.reply_text("Currently in setup. Send /cancel to exit setup first.")
        return

    # Auto-recover if accounts exist but onboarded flag not set
    if not db.is_onboarded() and db.get_all_accounts():
        db.mark_onboarded()
        reply_watcher.start_reply_watcher(_reply_notification_callback)
        followup_scheduler.start_scheduler(_followup_notification_callback)

    if not db.is_onboarded():
        await update.message.reply_text("No accounts set up yet. Run /start first.")
        return

    await update.message.reply_text(" Processing screenshot with AI vision...")

    photo = update.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        result = ocr_router.extract_from_screenshot(tmp_path)

        if not result.get("emails"):
            await update.message.reply_text(
                f" No email found.\n\n"
                f"OCR source: {result.get('source', 'unknown')}\n"
                f"Name: {result.get('name') or 'not detected'}\n"
                f"Handle: @{result.get('handle') or 'not detected'}\n"
                f"Followers: {result.get('followers') or 'not detected'}\n\n"
                f"Try a clearer screenshot or paste the email directly."
            )
            return

        for email_addr in result["emails"]:
            await _process_found_creator(
                update, ctx, email_addr,
                name=result.get("name"),
                handle=result.get("handle"),
                followers=result.get("followers"),
                bio=result.get("bio"),
                niche=result.get("niche"),
                platform=result.get("platform", "instagram"),
                source="screenshot",
            )

    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return

    # If we are awaiting seeds for scanning, route to the seed handler
    if discovery:
        if await _handle_disc_seeds(update, ctx):
            return

    # PATCH9_NL_ROUTER
    _mfai_txt = (update.message.text or "").strip() if update.message and update.message.text else ""
    _mfai_txt_lower = _mfai_txt.lower()
    _mfai_pending = db.get_pending_action(update.effective_user.id)
    _mfai_in_onboarding = _mfai_pending and _mfai_pending.get("action_type") == "onboarding"
    _mfai_in_reply_flow = _mfai_pending and _mfai_pending.get("action_type") in (
        "awaiting_reply_instruction", "editing_reply", "bulk_mode",
    )

    #  3A. If we're mid-seed-collection (from "start hunting" without seeds),
    #        consume this message as the seed list.
    if _mfai_txt and _user_mode.get(update.effective_user.id) == "_awaiting_seeds":
        try:
            import discovery as _disc
        except ImportError:
            _disc = None
        if _disc:
            _seeds = _mfai_extract_handles(_mfai_txt)
            if _seeds:
                _user_mode[update.effective_user.id] = "hunting"
                cfg = _disc.get_cfg()
                await update.message.reply_text(f" Scanning {len(_seeds)} seeds…")
                import asyncio as _asyncio
                _asyncio.create_task(_mfai_run_scan(update, ctx, _seeds, 1, True, _disc))
                return
            else:
                await update.message.reply_text(
                    "I didn't spot any usernames. Send them like: @user1 @user2 @user3"
                )
                return

    #  3B. Natural-language router. Skip during onboarding / reply flows.
    if _mfai_txt and not _mfai_in_onboarding and not _mfai_in_reply_flow             and not _mfai_txt.startswith("/"):
        _mfai_intent = _mfai_route(_mfai_txt_lower, _mfai_txt)
        if _mfai_intent:
            _mfai_action = _mfai_intent["intent"]

            # unknown fallback
            if _mfai_action == "unknown":
                reply_txt = _mfai_intent.get("reply", "No command found.")
                await update.message.reply_text(reply_txt)
                return

            # extract followings
            if _mfai_action == "extract_followings":
                try:
                    import discovery as _disc
                    _seeds = _mfai_intent["seeds"]
                    await update.message.reply_text(
                        f" Extracting following list for {len(_seeds)} seeds..."
                    )
                    import asyncio as _asyncio
                    _asyncio.create_task(_mfai_run_extraction(update, ctx, _seeds))
                    return
                except ImportError:
                    await update.message.reply_text("Discovery module not loaded.")
                    return

            # start_hunt with seeds  dispatch scan
            if _mfai_action == "add_seeds_and_hunt":
                try:
                    import discovery as _disc
                    _seeds = _mfai_intent["seeds"]
                    _hops = _mfai_intent.get("hops", 1)
                    _skip = _mfai_intent.get("skip_seen", True)
                    hops_lbl = f"{_hops}-hop" if _hops > 0 else "direct"
                    await update.message.reply_text(
                        f" Scanning {len(_seeds)} seeds ({hops_lbl})…"
                    )
                    import asyncio as _asyncio
                    _asyncio.create_task(_mfai_run_scan(update, ctx, _seeds, _hops, _skip, _disc))
                    return
                except ImportError:
                    await update.message.reply_text("Discovery module not loaded.")
                    return

            # start_hunt without seeds  prompt for them
            if _mfai_action == "start_hunt":
                _user_mode[update.effective_user.id] = "_awaiting_seeds"
                await update.message.reply_text(
                    " Send me the seed usernames (comma or newline separated, up to 10):\n\n"
                    "<i>@futuretools, @toolify, @shopify</i>",
                    parse_mode="HTML",
                )
                return

            # autoscan
            if _mfai_action == "autoscan":
                try:
                    import discovery as _disc
                    _seeds = _mfai_intent["seeds"]
                    _hrs = _mfai_intent.get("interval_hours", 24)
                    try:
                        _aid = _disc.add_autoscan(_seeds, hops=1, interval_hours=_hrs)
                    except TypeError:
                        _aid = _disc.add_autoscan(_seeds, 1, _hrs)
                    await update.message.reply_text(
                        f" Autoscan #{_aid}  {len(_seeds)} seeds every {_hrs}h"
                    )
                    return
                except ImportError:
                    pass

            # switch_mode
            if _mfai_action == "switch_mode":
                _mode = _mfai_intent.get("mode", "hunting")
                if _mode == "hunting":
                    await cmd_hunt(update, ctx)
                else:
                    await cmd_outreach(update, ctx)
                return

            # cancel
            if _mfai_action == "cancel_scan":
                try:
                    import discovery as _disc
                    _disc.cancel_flag = True
                except ImportError:
                    pass
                await update.message.reply_text(" Cancelling scan.")
                return

            # push_to_crm
            if _mfai_action == "push_to_crm":
                try:
                    import discovery as _disc
                    _rows = _disc.get_scan_history(1)
                    if not _rows:
                        await update.message.reply_text("No scan to push. Run a scan first.")
                        return
                    # Reuse existing pushtocrm command if present
                    _fn = globals().get("cmd_pushtocrm")
                    if _fn:
                        await _fn(update, ctx)
                        return
                except ImportError:
                    pass

            # smalltalk
            if _mfai_action == "smalltalk":
                await update.message.reply_text(
                    _mfai_intent.get("reply", "Hey! Tell me what you want to do.")
                )
                return

            # help
            if _mfai_action == "help":
                _fn = globals().get("cmd_help")
                if _fn:
                    await _fn(update, ctx)
                    return

        # ── AI Command Orchestrator fallback ──────────────────────────
        # If the regex router didn't match, try the AI-powered orchestrator
        if not _mfai_intent:
            try:
                import ai_command_orchestrator as _orch
                _ai_cmd = _orch.parse_command(_mfai_txt)
                if _ai_cmd and _ai_cmd.get("action") != "unknown":
                    _action = _ai_cmd["action"]
                    
                    # Dispatch to the appropriate handler
                    if _action == "dashboard":
                        await cmd_dashboard(update, ctx)
                        return
                    elif _action == "list_cookies":
                        _fn = globals().get("cmd_cookies")
                        if _fn: await _fn(update, ctx)
                        return
                    elif _action == "list_seeds":
                        await cmd_seeds(update, ctx)
                        return
                    elif _action == "add_seed":
                        usernames = _ai_cmd.get("usernames", [])
                        if usernames:
                            added = 0
                            for u in usernames:
                                if db.add_seed(u, source="ai_command"):
                                    added += 1
                            await update.message.reply_text(f"🌱 Added {added} seed(s): {', '.join('@' + u for u in usernames)}")
                        else:
                            await update.message.reply_text("🌱 Which accounts do you want to add as seeds? Send: /addseed @user1 @user2")
                        return
                    elif _action == "remove_seed":
                        username = _ai_cmd.get("username", "")
                        if username and db.remove_seed(username):
                            await update.message.reply_text(f"🗑 Removed @{username} from seeds.")
                        else:
                            await update.message.reply_text("❌ Seed not found or already inactive.")
                        return
                    elif _action == "scan":
                        seeds = _ai_cmd.get("seeds", [])
                        if seeds:
                            try:
                                import discovery as _disc
                                hops = _ai_cmd.get("hops", 1)
                                await update.message.reply_text(f"🔍 Scanning {len(seeds)} seeds ({hops}-hop)…")
                                import asyncio as _asyncio
                                _asyncio.create_task(_mfai_run_scan(update, ctx, seeds, hops, True, _disc))
                            except ImportError:
                                await update.message.reply_text("Discovery module not loaded.")
                        else:
                            _user_mode[update.effective_user.id] = "_awaiting_seeds"
                            await update.message.reply_text("🔍 Send me the seed usernames:\n\n<i>@user1, @user2, @user3</i>", parse_mode="HTML")
                        return
                    elif _action == "crm":
                        await cmd_crm(update, ctx)
                        return
                    elif _action == "export_tinder":
                        await cmd_export_tinder(update, ctx)
                        return
                    elif _action == "tinder":
                        await cmd_tinder(update, ctx)
                        return
                    elif _action == "set_dm_interval":
                        min_m = _ai_cmd.get("min_minutes", 7)
                        max_m = _ai_cmd.get("max_minutes", 18)
                        db.set_setting("dm_min_interval", str(min_m))
                        db.set_setting("dm_max_interval", str(max_m))
                        await update.message.reply_text(f"✅ DM interval set to <b>{min_m}-{max_m} minutes</b> (randomized).", parse_mode="HTML")
                        return
                    elif _action == "hot_leads":
                        await cmd_hot(update, ctx)
                        return
                    elif _action == "today_stats":
                        await cmd_today(update, ctx)
                        return
                    elif _action == "analytics":
                        await cmd_analytics(update, ctx)
                        return
                    elif _action == "dm_status":
                        await cmd_dmstatus(update, ctx)
                        return
                    elif _action == "pass_all":
                        await cmd_passall(update, ctx)
                        return
                    elif _action == "skip_all":
                        await cmd_skipall(update, ctx)
                        return
                    elif _action == "outreach_all":
                        await cmd_outreachall(update, ctx)
                        return
                    elif _action == "retry_failed_dms":
                        await cmd_retrydms(update, ctx)
                        return
                    elif _action == "preview_dm":
                        handle = _ai_cmd.get("handle", "")
                        if handle:
                            # Simulate the command
                            update.message.text = f"/previewdm @{handle}"
                            await cmd_previewdm(update, ctx)
                        return
                    elif _action == "help":
                        await cmd_help(update, ctx)
                        return
                    elif _action == "settings":
                        await cmd_settings(update, ctx)
                        return
                    elif _action == "queue_outreach":
                        handles = _ai_cmd.get("handles", [])
                        if handles:
                            conn = db.get_db()
                            c = conn.cursor()
                            cids = []
                            for h in handles:
                                c.execute("SELECT id FROM creators WHERE handle=?", (h,))
                                r = c.fetchone()
                                if r: cids.append(r["id"])
                            conn.close()
                            if cids:
                                pass  # dm_automation disabled
                            else:
                                await update.message.reply_text("❌ None of those handles were found in CRM.")
                        else:
                            await cmd_outreachall(update, ctx)
                        return
            except ImportError:
                pass

    # Check onboarding state
    pending = db.get_pending_action(update.effective_user.id)
    if pending and pending["action_type"] == "onboarding":
        await _continue_onboarding(update, ctx, pending["context"])
        return

    if pending and pending["action_type"] == "awaiting_reply_instruction":
        await _handle_reply_instruction(update, ctx, pending["context"])
        return

    if pending and pending["action_type"] == "editing_reply":
        await _handle_edited_reply(update, ctx, pending["context"])
        return

    if pending and pending["action_type"] == "bulk_mode":
        await _handle_bulk_input(update, ctx)
        return

    # Auto-recover if accounts exist but onboarded flag not set
    if not db.is_onboarded() and db.get_all_accounts():
        db.mark_onboarded()
        reply_watcher.start_reply_watcher(_reply_notification_callback)
        followup_scheduler.start_scheduler(_followup_notification_callback)

    if not db.is_onboarded():
        await update.message.reply_text("No accounts set up yet. Run /start first.")
        return

    text = update.message.text.strip()

    # URL + email combo detection (e.g. "https://instagram.com/user creator@email.com")
    url_match = re.search(r'https?://[^\s]+', text)
    emails_in_text = [e for e in ocr_router.EMAIL_PATTERN.findall(text) if e.lower() not in ocr_router.JUNK_EMAILS]

    if url_match and emails_in_text:
        # COMBO MODE: screenshot the URL, analyze, send to the provided email
        url = url_match.group(0)
        target_email = emails_in_text[0]

        await update.message.reply_text(
            f" Got URL + email combo\n"
            f" Screenshotting {url}...\n"
            f" Will send to: {target_email}"
        )

        # Screenshot via Scrapingdog and analyze with Gemini Vision
        result = scrapers.screenshot_and_analyze(url)

        if result.get("error"):
            await update.message.reply_text(f" {result['error']}")
            # Still try to send with whatever info we got
            if not result.get("name"):
                result["name"] = _name_from_email(target_email)

        # Clean up screenshot file
        if result.get("screenshot_path"):
            try:
                os.unlink(result["screenshot_path"])
            except Exception:
                pass

        # Use provided email, not whatever was found in screenshot
        await _process_found_creator(
            update, ctx, target_email,
            name=result.get("name"),
            handle=result.get("handle"),
            followers=result.get("followers"),
            bio=result.get("bio"),
            niche=result.get("niche"),
            platform=result.get("platform", scrapers._detect_platform(url)),
            source="url_email_combo",
        )
        return

    # URL only (no email in message)
    if url_match:
        url = url_match.group(0)
        platform = scrapers._detect_platform(url)

        # For Instagram, use screenshot approach since IG blocks scraping
        if platform == "instagram" and scrapers.get_scrapingdog_key():
            await update.message.reply_text(f" Screenshotting Instagram profile...")
            result = scrapers.screenshot_and_analyze(url)

            # Also try the API for email/data
            api_result = scrapers.scrape_profile(url)
            # Merge: prefer screenshot data for bio/niche, API for email
            for key in ["name", "handle", "followers", "bio", "niche"]:
                if not result.get(key) and api_result.get(key):
                    result[key] = api_result[key]
            if api_result.get("emails"):
                for e in api_result["emails"]:
                    if e not in result.get("emails", []):
                        result.setdefault("emails", []).append(e)

            if result.get("screenshot_path"):
                try:
                    os.unlink(result["screenshot_path"])
                except Exception:
                    pass
        else:
            await update.message.reply_text(f" Scraping {url}...")
            result = scrapers.scrape_profile(url)

        if result.get("error"):
            await update.message.reply_text(f" {result['error']}")

        if not result.get("emails"):
            await update.message.reply_text(
                f" No email found.\n\n"
                f"Try sending: URL + email together\n"
                f"Example: <code>{url} creator@email.com</code>\n\n"
                f"Or send a screenshot of their profile.",
                parse_mode=ParseMode.HTML,
            )
            return

        for email_addr in result["emails"]:
            await _process_found_creator(
                update, ctx, email_addr,
                name=result.get("name"),
                handle=result.get("handle"),
                followers=result.get("followers"),
                bio=result.get("bio"),
                niche=result.get("niche"),
                platform=result.get("platform", "other"),
                source="link",
            )
        return

    # Direct email only
    if emails_in_text:
        for email_addr in emails_in_text:
            name = _name_from_text_or_email(text, email_addr)
            await _process_found_creator(
                update, ctx, email_addr, name=name, source="manual",
            )
        return

    await update.message.reply_text(
        "Send me a screenshot, profile URL, or email address.\n"
        "URL + email combo works too: paste an IG link and the email together.\n"
        "Type /help for all commands."
    )


async def _handle_bulk_input(update, ctx):
    """Process bulk paste of URLs/emails/combos."""
    text = update.message.text.strip()
    db.clear_pending_action(update.effective_user.id)

    if text == "/cancel":
        await update.message.reply_text("Bulk mode cancelled.")
        return

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        await update.message.reply_text("No items to process.")
        return

    await update.message.reply_text(
        f" Processing {len(lines)} item(s)...\n"
        f"I'll send updates as each is processed."
    )

    success_count = 0
    skip_count = 0
    error_count = 0

    for i, line in enumerate(lines, 1):
        try:
            url_match = re.search(r'https?://[^\s]+', line)
            emails_in_line = [e for e in ocr_router.EMAIL_PATTERN.findall(line) if e.lower() not in ocr_router.JUNK_EMAILS]

            if url_match and emails_in_line:
                # URL + email combo
                url = url_match.group(0)
                target_email = emails_in_line[0]
                if db.email_already_sent(target_email):
                    skip_count += 1
                    continue
                # Use screenshot pipeline
                ss_result = scrapers.screenshot_and_analyze(url)
                if ss_result.get("screenshot_path"):
                    try:
                        os.unlink(ss_result["screenshot_path"])
                    except Exception:
                        pass
                await _process_found_creator(
                    update, ctx, target_email,
                    name=ss_result.get("name"),
                    handle=ss_result.get("handle"),
                    followers=ss_result.get("followers"),
                    bio=ss_result.get("bio"),
                    niche=ss_result.get("niche"),
                    platform=ss_result.get("platform", scrapers._detect_platform(url)),
                    source="bulk_combo",
                    silent=True,
                )
                success_count += 1

            elif emails_in_line:
                # Email only
                target_email = emails_in_line[0]
                if db.email_already_sent(target_email):
                    skip_count += 1
                    continue
                name = _name_from_text_or_email(line, target_email)
                await _process_found_creator(
                    update, ctx, target_email,
                    name=name, source="bulk_manual", silent=True,
                )
                success_count += 1

            elif url_match:
                # URL only
                url = url_match.group(0)
                result = scrapers.scrape_profile(url)
                if not result.get("emails"):
                    error_count += 1
                    continue
                for email_addr in result["emails"]:
                    if db.email_already_sent(email_addr):
                        skip_count += 1
                        continue
                    await _process_found_creator(
                        update, ctx, email_addr,
                        name=result.get("name"),
                        handle=result.get("handle"),
                        followers=result.get("followers"),
                        bio=result.get("bio"),
                        niche=result.get("niche"),
                        platform=result.get("platform"),
                        source="bulk_link",
                        silent=True,
                    )
                    success_count += 1
            else:
                error_count += 1

            # Progress update every 5 items
            if i % 5 == 0:
                await update.message.reply_text(f" Processed {i}/{len(lines)}...")

        except Exception as e:
            logger.warning(f"Bulk item {i} failed: {e}")
            error_count += 1

    await update.message.reply_text(
        f"<b> Bulk complete</b>\n\n"
        f" Sent: {success_count}\n"
        f" Skipped (duplicates): {skip_count}\n"
        f" Errors: {error_count}\n\n"
        f"Today: {db.get_today_sent_count()} sent | {db.total_remaining_today()} remaining",
        parse_mode=ParseMode.HTML,
    )


async def _process_found_creator(update, ctx, email_addr, silent=False, **kwargs):
    """Save creator, generate email, send."""
    # Dedupe check
    if db.email_already_sent(email_addr):
        if not silent:
            await update.message.reply_text(f" Already contacted {email_addr}. Skipping.")
        return

    # Determine tier
    tier = ocr_router.determine_tier(kwargs.get("followers"))
    kwargs["tier"] = tier

    # Save creator
    if not kwargs.get("name"):
        kwargs["name"] = _name_from_email(email_addr)

    creator_id, is_new = db.add_or_get_creator(
        email_addr,
        name=kwargs.get("name"),
        handle=kwargs.get("handle"),
        platform=kwargs.get("platform", "instagram"),
        followers=kwargs.get("followers"),
        tier=tier,
        bio=kwargs.get("bio"),
        niche=kwargs.get("niche"),
        source=kwargs.get("source", "manual"),
        stage="new",
    )

    # Generate email
    creator_info = {
        "name": kwargs.get("name"),
        "handle": kwargs.get("handle"),
        "platform": kwargs.get("platform"),
        "followers": kwargs.get("followers"),
        "tier": tier,
        "bio": kwargs.get("bio"),
        "niche": kwargs.get("niche"),
    }
    if not silent:
        await update.message.reply_text(" Generating personalized email with AI...")
    email_data = ai_router.generate_opener_email(creator_info)

    # Send via rotation
    if not silent:
        await update.message.reply_text(" Sending...")
    result = email_sender.send_with_logging(
        creator_id=creator_id,
        to_email=email_addr,
        subject=email_data["subject"],
        body=email_data["body"],
        message_type="opener",
    )

    if result.get("success"):
        db.update_creator_stage(creator_id, "opener_sent")
        followup_scheduler.schedule_first_followup(creator_id)

        # DM reminder if we have a handle
        if kwargs.get("handle"):
            db.add_dm_reminder(creator_id, kwargs.get("platform", "instagram"), kwargs.get("handle"))

        if silent:
            return

        fol_str = f" ({kwargs.get('followers'):,} followers)" if kwargs.get("followers") else ""
        # Quick action buttons after each send
        keyboard = [
            [InlineKeyboardButton(f" View #{creator_id}", callback_data=f"view_creator_{creator_id}"),
             InlineKeyboardButton(f" Mark lost", callback_data=f"mark_lost_{creator_id}")],
        ]
        fallback_note = ""
        if email_data.get("used_fallback"):
            reason = email_data.get("fallback_reason") or "unknown error"
            fallback_note = (
                f" <b>AI failed ({_escape_html(reason)})</b> — sent a generic template, "
                f"not personalized to their bio/niche.\n\n"
            )
        await update.message.reply_text(
            f"{fallback_note}"
            f" Sent to <code>{email_addr}</code>{fol_str}\n"
            f"From: <code>{result['account_used']}</code>\n"
            f"Tier: {tier} | Creator #{creator_id}\n"
            f"Today: {db.get_today_sent_count()} sent, {db.total_remaining_today()} remaining\n\n"
            f"<b>What was sent:</b>\n"
            f"<i>Subject:</i> {email_data['subject']}\n\n"
            f"{email_data['body'][:500]}{'...' if len(email_data['body']) > 500 else ''}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif result.get("queued"):
        if not silent:
            await update.message.reply_text(f" {result.get('message')}\nUse /startqueue tomorrow.")
    else:
        if not silent:
            await update.message.reply_text(f" {result.get('error')}")


def _name_from_email(email_addr):
    local = email_addr.split("@")[0]
    if local.lower() in ("contact", "hello", "hi", "info", "partnerships", "collab", "business", "team"):
        return "there"
    parts = re.split(r'[._\-]', local)
    return " ".join(p.capitalize() for p in parts if len(p) > 1) or "there"


def _name_from_text_or_email(text, email_addr):
    # Try to find a name before the email in the text
    parts = text.split(email_addr)
    if parts and parts[0].strip():
        possible_name = parts[0].strip().split("\n")[-1].strip()
        if possible_name and len(possible_name) < 50 and not possible_name.startswith("/"):
            return possible_name
    return _name_from_email(email_addr)


# 
#                       REPLY HANDLING
# 

def _notify_sync(app, username, text):
    """Sends a Telegram notification when a new DM is synced via inbox watcher."""
    import asyncio
    users = db.get_all_users()
    if not users: return
    
    msg = f"📩 **New Instagram DM** from @{username}:\n\n_{text}_\n\n_(Stage updated to Replied)_"
    for u in users:
        try:
            asyncio.run_coroutine_threadsafe(
                app.bot.send_message(u, msg, parse_mode="Markdown"),
                app.loop
            )
        except Exception:
            pass

def _reply_notification_callback(reply_data):
    """Called by reply_watcher when a new reply is detected."""
    if not _app or not _main_loop:
        return

    chat_id = config.ALLOWED_USER_ID
    if not chat_id:
        return

    to_display = f" (To alias: {reply_data.get('to_email')})" if reply_data.get('to_email') else ""

    text = (
        f" <b>NEW REPLY!</b>\n\n"
        f"From: <b>{reply_data['creator_name']}</b> (@{reply_data.get('creator_handle', '?')})\n"
        f"Email: <code>{reply_data['from_email']}</code>{to_display}\n"
        f"Subject: {reply_data['subject']}\n\n"
        f"<i>Message:</i>\n{_escape_html(reply_data['body'][:800])}\n\n"
        f"<i>AI Draft Reply:</i>\n{_escape_html(reply_data.get('ai_draft', 'No draft generated.')[:800])}"
    )

    keyboard = [
        [InlineKeyboardButton(" Auto Reply", callback_data=f"reply_auto_{reply_data['reply_id']}")],
        [InlineKeyboardButton(" Manual (I'll handle)", callback_data=f"reply_manual_{reply_data['reply_id']}")],
        [InlineKeyboardButton(" Mark as closed", callback_data=f"reply_close_{reply_data['reply_id']}")],
    ]

    async def send():
        try:
            await _app.bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception as e:
            logger.error(f"Failed to send reply notification: {e}")

    asyncio.run_coroutine_threadsafe(send(), _main_loop)


def _followup_notification_callback(data):
    if not _app or not _main_loop:
        return
    chat_id = config.ALLOWED_USER_ID
    if not chat_id:
        return

    text = (
        f" Followup #{data['followup_number']} sent\n"
        f"To: {data['creator_name']} ({data['creator_email']})"
    )

    async def send():
        try:
            await _app.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            pass

    asyncio.run_coroutine_threadsafe(send(), _main_loop)


def _schedule_notification(chat_id, text):
    """Helper to send a notification from a background thread."""
    if not _app or not _main_loop:
        return

    async def send():
        try:
            await _app.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            pass

    asyncio.run_coroutine_threadsafe(send(), _main_loop)


# 
#                       CALLBACK QUERY HANDLERS
# 

async def _safe_answer(query, text=None):
    """Answer a callback query, ignoring errors from expired/stale queries
    (happens after network drops or bot restarts on flaky mobile data)."""
    try:
        if text:
            await query.answer(text=text)
        else:
            await query.answer()
        return True
    except Exception as e:
        logger.warning(f"Callback query answer failed (likely expired): {e}")
        return False


async def _safe_edit_text(query, text, **kwargs):
    """Edit a message's text, ignoring 'message not modified' (happens on
    double-taps or retried callbacks where the content is already identical)
    and other stale-message errors."""
    try:
        await query.edit_message_text(text, **kwargs)
        return True
    except Exception as e:
        if "not modified" in str(e).lower():
            pass  # harmless, content was already correct
        else:
            logger.warning(f"edit_message_text failed (likely stale message): {e}")
        return False


async def _safe_edit_markup(query, **kwargs):
    """Edit a message's reply markup, ignoring 'not modified' and stale-message errors."""
    try:
        await query.edit_message_reply_markup(**kwargs)
        return True
    except Exception as e:
        if "not modified" in str(e).lower():
            pass
        else:
            logger.warning(f"edit_message_reply_markup failed (likely stale message): {e}")
        return False


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    data = query.data

    # Onboarding callbacks
    if data == "onb_reply_preview":
        db.set_setting("auto_reply_mode", "preview")
        await _safe_edit_text(query, " Preview mode set. I'll show you drafts before sending.")
        # Continue onboarding
        db.save_pending_action(query.from_user.id, "onboarding", {"step": "auto_reply"})
        # Forward to next step manually
        msg = await ctx.bot.send_message(chat_id=query.message.chat_id, text="Moving to next step...")
        # Trigger the next onboarding step
        class FakeUpdate:
            def __init__(self, msg, user):
                self.message = msg
                self.effective_user = user
                self.effective_chat = msg.chat
        await _ask_scrapingdog(FakeUpdate(msg, query.from_user), ctx)
        return

    elif data == "onb_reply_trust":
        db.set_setting("auto_reply_mode", "trust")
        await _safe_edit_text(query, " Trust mode set. I'll auto-send replies.")
        db.save_pending_action(query.from_user.id, "onboarding", {"step": "auto_reply"})
        msg = await ctx.bot.send_message(chat_id=query.message.chat_id, text="Moving to next step...")
        class FakeUpdate:
            def __init__(self, msg, user):
                self.message = msg
                self.effective_user = user
                self.effective_chat = msg.chat
        await _ask_scrapingdog(FakeUpdate(msg, query.from_user), ctx)
        return

    # Reply handling callbacks
    if data.startswith("reply_auto_"):
        reply_id = int(data.split("_")[2])
        await _start_auto_reply(query, ctx, reply_id)

    elif data.startswith("reply_manual_"):
        reply_id = int(data.split("_")[2])
        reply = db.get_reply(reply_id)
        if reply:
            db.mark_reply_handled(reply_id, "manual")
            db.update_creator_stage(reply["creator_id"], "manual_handling")
        await _safe_edit_text(query, " Marked for manual handling. I won't auto-respond.")

    elif data.startswith("reply_close_"):
        reply_id = int(data.split("_")[2])
        reply = db.get_reply(reply_id)
        if reply:
            db.mark_reply_handled(reply_id, "closed")
            db.update_creator_stage(reply["creator_id"], "closed_lost")
            db.cancel_followups_for_creator(reply["creator_id"])
        await _safe_edit_text(query, " Marked as closed. No more emails will be sent.")

    elif data.startswith("send_reply_"):
        # User approved a generated reply - send it
        parts = data.split("_", 3)
        reply_id = int(parts[2])
        await _send_approved_reply(query, ctx, reply_id)

    elif data.startswith("edit_reply_"):
        reply_id = int(data.split("_")[2])
        db.save_pending_action(query.from_user.id, "editing_reply", {"reply_id": reply_id})
        await _safe_edit_text(query, 
            "Send me the edited reply text (or /cancel to abort).\n\n"
            "Include subject on first line if changing it (e.g. 'SUBJECT: Re: ...')"
        )

    elif data.startswith("regen_reply_"):
        reply_id = int(data.split("_")[2])
        db.save_pending_action(query.from_user.id, "awaiting_reply_instruction", {"reply_id": reply_id})
        await _safe_edit_text(query, 
            "What should I change about the reply? Send your instruction (e.g. 'be more direct about the price', 'offer them $200 instead', 'ask what their rate would be')."
        )

    elif data.startswith("cancel_reply_"):
        await _safe_edit_text(query, "Cancelled.")
        db.clear_pending_action(query.from_user.id)

    elif data.startswith("view_creator_"):
        cid = int(data.split("_")[2])
        data_info = db.get_creator_full_detail(cid)
        if not data_info:
            await query.message.reply_text("Creator not found.")
            return
        c = data_info["creator"]
        await query.message.reply_text(
            f"<b> #{c['id']} {c['name']}</b>\n"
            f"@{c['handle'] or '?'} | {c['stage']}\n"
            f"{c['email']}\n"
            f"Use /creator {cid} for full details.",
            parse_mode=ParseMode.HTML,
        )

    elif data.startswith("mark_lost_"):
        cid = int(data.split("_")[2])
        db.update_creator_stage(cid, "closed_lost")
        db.cancel_followups_for_creator(cid)
        creator = db.get_creator(cid)
        await _safe_edit_markup(query, reply_markup=None)
        await query.message.reply_text(f" {creator['name'] if creator else f'#{cid}'} marked as lost. Followups cancelled.")


async def _start_auto_reply(query, ctx, reply_id):
    """Begin auto-reply flow."""
    reply = db.get_reply(reply_id)
    if not reply:
        await _safe_edit_text(query, "Reply not found.")
        return

    keyboard = [
        [InlineKeyboardButton(" Use default deal", callback_data=f"reply_default_{reply_id}")],
        [InlineKeyboardButton(" Custom instruction", callback_data=f"reply_custom_{reply_id}")],
        [InlineKeyboardButton(" Cancel", callback_data=f"cancel_reply_{reply_id}")],
    ]
    await _safe_edit_text(query, 
        f"How should I respond?\n\n"
        f"<b>Default deal</b>: I'll respond with the standard offer for their tier.\n"
        f"<b>Custom</b>: You tell me what to say (e.g. 'offer $200', 'ask their rate').",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_callback_extra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Secondary callback handler for reply choices."""
    query = update.callback_query
    data = query.data

    if data.startswith("reply_default_"):
        await _safe_answer(query)
        reply_id = int(data.split("_")[2])
        await _generate_and_show_reply(query, ctx, reply_id, instruction="")

    elif data.startswith("reply_custom_"):
        await _safe_answer(query)
        reply_id = int(data.split("_")[2])
        db.save_pending_action(query.from_user.id, "awaiting_reply_instruction", {"reply_id": reply_id})
        await _safe_edit_text(query, 
            "Send me your instruction. Examples:\n"
            "• 'offer $200 flat fee'\n"
            "• 'ask what their rate would be'\n"
            "• 'be more direct'\n"
            "• 'they want CPM, explain our view bonus structure'"
        )


async def _handle_reply_instruction(update, ctx, context_data):
    """User sent instruction for the AI reply."""
    instruction = update.message.text.strip()
    reply_id = context_data["reply_id"]
    db.clear_pending_action(update.effective_user.id)

    # Create a fake query object for the helper
    class FakeQuery:
        def __init__(self, message):
            self.message = message
        async def edit_message_text(self, text, **kwargs):
            await message.reply_text(text, **kwargs)

    message = update.message
    await message.reply_text(" Generating reply with your instruction...")
    await _generate_and_show_reply_msg(message, ctx, reply_id, instruction)


async def _generate_and_show_reply(query, ctx, reply_id, instruction=""):
    """Generate a reply and show it for approval."""
    await _safe_edit_text(query, " Generating reply...")
    reply = db.get_reply(reply_id)
    if not reply:
        await _safe_edit_text(query, "Reply not found.")
        return
    await _generate_and_show_reply_msg(query.message, ctx, reply_id, instruction)


async def _generate_and_show_reply_msg(msg, ctx, reply_id, instruction):
    """Generate reply and show buttons (works with message object)."""
    reply = db.get_reply(reply_id)
    if not reply:
        await msg.reply_text("Reply not found.")
        return

    creator = db.get_creator(reply["creator_id"])
    conversation = db.get_conversation(reply["creator_id"])

    conv_list = [{"role": c["role"], "content": c["content"]} for c in conversation]

    creator_info = {
        "name": creator["name"],
        "tier": creator["tier"],
        "followers": creator["followers"],
    }

    generated = ai_router.generate_reply(
        creator_info=creator_info,
        conversation=conv_list,
        their_latest_reply=reply["body"],
        instruction=instruction,
    )

    # Save the generated reply temporarily in pending_actions
    used_fallback = generated.get("used_fallback", False)

    db.save_pending_action(
        config.ALLOWED_USER_ID,
        "generated_reply",
        {
            "reply_id": reply_id,
            "subject": generated["subject"],
            "body": generated["body"],
            "suggested_stage": generated.get("suggested_stage", "negotiating"),
        }
    )

    # If trust mode, send immediately — UNLESS AI failed and this is a fallback,
    # in which case force a preview so a bad/incomplete draft never auto-sends.
    auto_reply_mode = db.get_setting("auto_reply_mode", "preview")

    if auto_reply_mode == "trust" and not used_fallback:
        await msg.reply_text(" Trust mode: sending immediately...")
        await _send_approved_reply_msg(msg, ctx, reply_id)
        return

    # Preview mode
    keyboard = [
        [InlineKeyboardButton(" Send", callback_data=f"send_reply_{reply_id}")],
        [
            InlineKeyboardButton(" Regenerate", callback_data=f"regen_reply_{reply_id}"),
            InlineKeyboardButton(" Edit", callback_data=f"edit_reply_{reply_id}"),
        ],
        [InlineKeyboardButton(" Cancel", callback_data=f"cancel_reply_{reply_id}")],
    ]

    warning = ""
    if used_fallback:
        reason = generated.get("fallback_reason") or "unknown error, check terminal logs"
        warning = (
            f" <b>AI generation failed:</b> {_escape_html(reason)}\n"
            f"This is a static fallback using your default deal numbers, NOT your custom instruction.\n\n"
        )

    await msg.reply_text(
        f"{warning}"
        f"<b> Draft Reply</b>\n\n"
        f"<b>Subject:</b> {generated['subject']}\n\n"
        f"{generated['body']}\n\n"
        f"<i>Suggested stage: {generated.get('suggested_stage', 'negotiating')}</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _send_approved_reply(query, ctx, reply_id):
    await _safe_answer(query)
    await _send_approved_reply_msg(query.message, ctx, reply_id)


async def _send_approved_reply_msg(msg, ctx, reply_id):
    pending = db.get_pending_action(config.ALLOWED_USER_ID)
    if not pending or pending["action_type"] != "generated_reply":
        await msg.reply_text("No draft found. The action expired.")
        return

    draft = pending["context"]
    reply = db.get_reply(reply_id)
    if not reply:
        await msg.reply_text("Reply not found.")
        return

    creator = db.get_creator(reply["creator_id"])

    result = email_sender.send_with_logging(
        creator_id=creator["id"],
        to_email=creator["email"],
        subject=draft["subject"],
        body=draft["body"],
        message_type="reply",
    )

    if result.get("success"):
        db.mark_reply_handled(reply_id, "auto_replied")
        db.update_creator_stage(creator["id"], draft.get("suggested_stage", "negotiating"))
        db.clear_pending_action(config.ALLOWED_USER_ID)
        await msg.reply_text(
            f" Reply sent to {creator['email']}\n"
            f"Stage: {draft.get('suggested_stage', 'negotiating')}"
        )
    else:
        await msg.reply_text(f" Send failed: {result.get('error')}")


async def _handle_edited_reply(update, ctx, context_data):
    """User sent an edited reply."""
    text = update.message.text.strip()
    reply_id = context_data["reply_id"]
    db.clear_pending_action(update.effective_user.id)

    # Parse subject if present
    subject = "Re: Collab opportunity, MagicFit AI"
    body = text
    if text.upper().startswith("SUBJECT:"):
        lines = text.split("\n", 1)
        subject = lines[0][8:].strip()
        body = lines[1] if len(lines) > 1 else ""

    reply = db.get_reply(reply_id)
    if not reply:
        await update.message.reply_text("Reply not found.")
        return

    creator = db.get_creator(reply["creator_id"])

    result = email_sender.send_with_logging(
        creator_id=creator["id"],
        to_email=creator["email"],
        subject=subject,
        body=body,
        message_type="reply",
    )

    if result.get("success"):
        db.mark_reply_handled(reply_id, "manual_edit")
        db.update_creator_stage(creator["id"], "negotiating")
        await update.message.reply_text(f" Sent to {creator['email']}")
    else:
        await update.message.reply_text(f" Send failed: {result.get('error')}")


# 
#                       UTILITIES
# 

def _escape_html(text):
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def error_handler(update, ctx):
    logger.error(f"Error: {ctx.error}", exc_info=ctx.error)
    if update and hasattr(update, "message") and update.message:
        try:
            await update.message.reply_text(f"Something went wrong. Check logs.\nError: {ctx.error}")
        except Exception:
            pass


# 
#                       MAIN
# 



async def cmd_setemailprompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    text_parts = update.message.text.split(maxsplit=1)
    if len(text_parts) < 2:
        current = db.get_setting("email_generator_prompt", "DEFAULT PROMPT")
        await update.message.reply_text(
            f"<b>Current Email AI Prompt:</b>\n\n<pre>{_escape_html(str(current)[:3000])}</pre>\n\n"
            f"To change it, use: /setemailprompt <new prompt>", parse_mode=ParseMode.HTML
        )
        return
    db.set_setting("email_generator_prompt", text_parts[1].strip())
    await update.message.reply_text("✅ Email AI Prompt updated.")

async def cmd_setemailtemplate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    text_parts = update.message.text.split(maxsplit=1)
    if len(text_parts) < 2:
        current = db.get_setting("email_template", "DEFAULT TEMPLATE")
        await update.message.reply_text(
            f"<b>Current Base Email Template:</b>\n\n<pre>{_escape_html(str(current)[:3000])}</pre>\n\n"
            f"To change it, use: /setemailtemplate <new template>", parse_mode=ParseMode.HTML
        )
        return
    db.set_setting("email_template", text_parts[1].strip())
    await update.message.reply_text("✅ Base Email Template updated.")

async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    conn = db.get_db()
    c = conn.cursor()
    c.execute("SELECT id, creator_id, subject FROM emails_sent WHERE status='queued' ORDER BY id ASC LIMIT 20")
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("📭 Email queue is empty.")
        return
        
    lines = ["<b>Queued Emails:</b>\n"]
    for r in rows:
        lines.append(f"#{r['id']} - {r['subject']}")
    lines.append("\nUse /editemail <id> <new_body> to edit.")
    lines.append("Use /deleteemail <id> to drop an email.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_editemail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    text_parts = update.message.text.split(maxsplit=2)
    if len(text_parts) < 3:
        await update.message.reply_text("Usage: /editemail <id> <new body>")
        return
        
    email_id = text_parts[1]
    new_body = text_parts[2]
    conn = db.get_db()
    conn.execute("UPDATE emails_sent SET body=? WHERE id=?", (new_body, email_id))
    if conn.total_changes > 0:
        await update.message.reply_text(f"✅ Email #{email_id} updated.")
    else:
        await update.message.reply_text(f"❌ Email #{email_id} not found.")
    conn.commit()
    conn.close()

async def cmd_deleteemail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text("Usage: /deleteemail <id>")
        return
    email_id = ctx.args[0]
    conn = db.get_db()
    conn.execute("DELETE FROM emails_sent WHERE id=?", (email_id,))
    if conn.total_changes > 0:
        await update.message.reply_text(f"✅ Email #{email_id} deleted.")
    else:
        await update.message.reply_text(f"❌ Email #{email_id} not found.")
    conn.commit()
    conn.close()


async def post_init(app):
    """After bot starts, capture references for background notifications."""
    global _app, _main_loop
    _app = app
    _main_loop = asyncio.get_event_loop()

    # Restart background services if onboarded
    if db.is_onboarded():
        reply_watcher.start_reply_watcher(_reply_notification_callback)
        followup_scheduler.start_scheduler(_followup_notification_callback)
        logger.info("Restarted background services")
        
    # Register slash commands list dynamically
    try:
        from telegram import BotCommand
        commands = [
            # Navigation & General
            BotCommand("dashboard", "📊 Full status dashboard"),
            BotCommand("d", "📊 Dashboard (shortcut)"),
            BotCommand("today", "📅 Today's outreach stats"),
            BotCommand("help", "❓ All commands"),
            BotCommand("ai", "🤖 AI natural language commands"),
            BotCommand("exit", "🚪 Return to main menu"),
            BotCommand("cancel", "❌ Cancel current operation"),
            # Discovery
            BotCommand("hunt", "🔍 Switch to Hunt mode"),
            BotCommand("scan", "🔍 Run a lookalike scan"),
            BotCommand("cancelscan", "🛑 Cancel active scan"),
            BotCommand("scanhistory", "📜 Past scan results"),
            BotCommand("pushtocrm", "📤 Push scan results to CRM"),
            BotCommand("autoscan", "🔄 Setup recurring scan"),
            BotCommand("autoscans", "📋 List auto-scans"),
            BotCommand("stopautoscan", "🛑 Stop an auto-scan"),
            BotCommand("lookalike", "👥 Quick lookalike lookup"),
            BotCommand("seen", "👁 Seen profiles count"),
            BotCommand("clearseen", "🧹 Clear seen profiles"),
            BotCommand("setmaxfollow", "🔢 Max followings/seed"),
            BotCommand("setmaxresults", "🔢 Max results/scan"),
            # Filters
            BotCommand("scrapemode", "⚙️ View/set scrape backend"),
            BotCommand("setfilters", "📏 Follower range filter"),
            BotCommand("setreach", "📐 Reach ratio filter"),
            BotCommand("keywords", "🏷 Bio keyword scoring"),
            # Seeds
            BotCommand("seeds", "🌱 View saved seeds"),
            BotCommand("addseed", "🌱 Add a seed account"),
            BotCommand("removeseed", "🗑 Remove a seed"),
            # Cookies & Apify
            BotCommand("addcookie", "🍪 Add IG cookies"),
            BotCommand("cookies", "🍪 List cookies"),
            BotCommand("removecookie", "🗑 Remove a cookie"),
            BotCommand("howtocookies", "📖 How to get cookies"),
            BotCommand("addapify", "🔑 Add Apify token"),
            BotCommand("apifytokens", "🔑 List Apify tokens"),
            BotCommand("removeapify", "🗑 Remove Apify token"),
            # CRM
            BotCommand("crm", "📋 CRM pipeline dashboard"),
            BotCommand("creator", "👤 Creator detail view"),
            BotCommand("hot", "🔥 Hot leads awaiting reply"),
            BotCommand("won", "🏆 Mark deal Won"),
            BotCommand("lost", "💀 Mark deal Lost"),
            BotCommand("note", "📝 Add a note"),
            BotCommand("reply", "💬 Reply to creator"),
            BotCommand("resend", "🔄 Manual follow-up"),
            BotCommand("updateemail", "📧 Update creator email"),
            # Tinder / Review
            BotCommand("tinder", "🎯 Swipe through creators"),
            BotCommand("export_tinder", "📁 Bulk review HTML"),
            BotCommand("export", "📁 Export (alias)"),
            BotCommand("preview", "👀 Preview creator card"),
            # Bulk Actions
            BotCommand("bulk", "📦 Paste many emails/URLs"),
            BotCommand("passall", "✅ Pass all discovered"),
            BotCommand("skipall", "❌ Skip all discovered"),
            BotCommand("outreachall", "🚀 Queue all for outreach"),
            BotCommand("launch_campaign", "🚀 Launch campaign"),
            # Outreach & Email
            BotCommand("outreach", "📧 Switch to Outreach mode"),
            BotCommand("accounts", "📬 List Gmail accounts"),
            BotCommand("addaccount", "📬 Add Gmail account"),
            BotCommand("removeaccount", "🗑 Remove Gmail account"),
            BotCommand("setlimit", "🔢 Set daily send limit"),
            BotCommand("pause", "⏸ Pause an account"),
            BotCommand("resume", "▶️ Resume an account"),
            BotCommand("warmupstatus", "🌡 Warmup status"),
            # Aliases
            BotCommand("addalias", "📧 Add SMTP alias"),
            BotCommand("bulkaddalias", "📧 Bulk add aliases"),
            BotCommand("listaliases", "📋 List all aliases"),
            BotCommand("removealias", "🗑 Remove an alias"),
            BotCommand("togglealias", "🔀 Toggle alias on/off"),
            BotCommand("pausemasteraliases", "⏸ Pause master aliases"),
            BotCommand("resumemasteraliases", "▶️ Resume master aliases"),
            BotCommand("aliasesstats", "📊 Alias statistics"),
            # Queue
            BotCommand("queue", "📬 View send queue"),
            BotCommand("startqueue", "▶️ Start queue processor"),
            BotCommand("stopqueue", "⏹ Stop queue processor"),
            BotCommand("dmlist", "📨 Recent sent messages"),
            BotCommand("exportdms", "📤 Export sent to CSV"),
            # Reply Watcher
            BotCommand("checkreplies", "📥 Check for replies now"),
            BotCommand("replystatus", "⏱ Reply check timer"),
            BotCommand("startwatcher", "▶️ Start reply watcher"),
            BotCommand("stopwatcher", "⏹ Stop reply watcher"),
            # API Keys
            BotCommand("apikeys", "🔑 API key status"),
            BotCommand("setgemini", "🔑 Set Gemini key"),
            BotCommand("setopenrouter", "🔑 Set OpenRouter key"),
            # Settings & Reports
            BotCommand("settings", "⚙️ View all settings"),
            BotCommand("setprompt", "✏️ Edit AI prompt"),
            BotCommand("report", "📊 Campaign report"),
            BotCommand("fullreport", "📊 7-day breakdown"),
            BotCommand("pipeline", "📊 Stage breakdown"),
            BotCommand("analytics", "📈 Funnel analytics"),
            BotCommand("files", "📂 Access exported files"),
                    BotCommand("createaliases", "⚙️ Execute /createaliases"),
            BotCommand("deleteemail", "⚙️ Execute /deleteemail"),
            BotCommand("dmdone", "⚙️ Execute /dmdone"),
            BotCommand("dmstatus", "⚙️ Execute /dmstatus"),
            BotCommand("editemail", "⚙️ Execute /editemail"),
            BotCommand("previewemail", "⚙️ Execute /previewemail"),
            BotCommand("removefile", "⚙️ Execute /removefile"),
            BotCommand("retryemails", "⚙️ Execute /retryemails"),
            BotCommand("setautoreply", "⚙️ Execute /setautoreply"),
            BotCommand("setcftoken", "⚙️ Execute /setcftoken"),
            BotCommand("setcfzone", "⚙️ Execute /setcfzone"),
            BotCommand("setdminterval", "⚙️ Execute /setdminterval"),
            BotCommand("setemailprompt", "⚙️ Execute /setemailprompt"),
            BotCommand("setemailtemplate", "⚙️ Execute /setemailtemplate"),
            BotCommand("setfollowup", "⚙️ Execute /setfollowup"),
            BotCommand("setgroq", "⚙️ Execute /setgroq"),
            BotCommand("setinterval", "⚙️ Execute /setinterval"),
            BotCommand("setmistral", "⚙️ Execute /setmistral"),
            BotCommand("setnvidia", "⚙️ Execute /setnvidia"),
            BotCommand("setopenroutermodel", "⚙️ Execute /setopenroutermodel"),
            BotCommand("setreplycheck", "⚙️ Execute /setreplycheck"),
            BotCommand("setscrapingdog", "⚙️ Execute /setscrapingdog"),
        ]
        await app.bot.set_my_commands(commands)
        logger.info("Slash commands list registered successfully.")
    except Exception as e:
        logger.error(f"Failed to set slash commands: {e}")


# 
#             DISCOVERY COMMANDS PATCH (merged scraper bot)
# 

_disc_awaiting_seeds = {}
_disc_pending_seeds = {}

async def cmd_files(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid): return
    
    import os, glob
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    
    files = glob.glob("discovery_exports/*.*")
    if not files:
        await update.message.reply_text("No exported files found.")
        return
        
    # Sort by modified time descending, take top 15
    files.sort(key=os.path.getmtime, reverse=True)
    files = files[:15]
    
    keyboard = []
    for f in files:
        fname = os.path.basename(f)
        size_kb = os.path.getsize(f) // 1024
        keyboard.append([InlineKeyboardButton(f"📁 {fname} ({size_kb} KB)", callback_data=f"getf_{fname}")])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📂 <b>Exported Files</b>\nClick a file to download it. Use /removefile &lt;filename&gt; to delete.",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def cmd_removefile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Remove a file from discovery_exports."""
    if not authorized(update.effective_user.id): return
    
    if len(ctx.args) < 1:
        await update.message.reply_text("Usage: /removefile <filename>")
        return
        
    import os
    fname = os.path.basename(ctx.args[0]) 
    filepath = os.path.join("discovery_exports", fname)
    
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
            await update.message.reply_text(f"✅ Removed file: {fname}")
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to remove file: {e}")
    else:
        await update.message.reply_text(f"❌ File not found: {fname}")

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    _disc_awaiting_seeds[update.effective_user.id] = "scan"
    await update.message.reply_text("Send seed usernames (one per line or comma-separated, max 10):")

async def cmd_lookalike(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /lookalike @username")
        return
    seed = args[0].strip().lstrip('@')
    await update.message.reply_text(f"🔎 Hunting lookalike clones for @{seed}...")
    import asyncio
    asyncio.create_task(_mfai_run_scan(update, ctx, [seed], hops=1, skip_seen=True, discovery=discovery))

async def cmd_adddmcookie(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    args = ctx.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /adddmcookie <cookie_string> [label]")
        return
    
    raw = args[0]
    label = args[1] if len(args) > 1 else ""
    sessionid, csrftoken = "", ""
    for chunk in raw.replace(";", " ").split():
        if chunk.startswith("sessionid="): sessionid = chunk.split("=", 1)[1]
        elif chunk.startswith("csrftoken="): csrftoken = chunk.split("=", 1)[1]
        elif chunk == "sessionid": sessionid = "missing"
    
    # Fallback parsing
    if not sessionid and "%" in raw: sessionid = raw
    if not csrftoken and len(raw) == 32: csrftoken = raw
    
    if not sessionid or not csrftoken:
        await update.message.reply_text("Could not parse sessionid and csrftoken.")
        return
        
    conn = db.get_db()
    conn.execute("INSERT INTO dm_cookies (sessionid, csrftoken, label) VALUES (?, ?, ?)", (sessionid, csrftoken, label))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ DM Cookie added: {label or sessionid[:6]}")

async def cmd_launch_campaign(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    
    # Get all passed creators not already sent a DM
    conn = db.get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM creators WHERE stage='Passed' AND (last_dm_sent_at IS NULL OR last_dm_sent_at='')")
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("No eligible 'Passed' creators found for outreach.")
        return
        
    cids = [r[0] for r in rows]
    
    # dm_automation disabled
    await update.message.reply_text(f"🚀 Campaign launched for {len(cids)} creators.")


async def cmd_addcookie(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /addcookie <sessionid> <csrftoken> [label]"); return
    label = ctx.args[2] if len(ctx.args) > 2 else None
    discovery.add_cookie(ctx.args[0], ctx.args[1], label)
    await update.message.reply_text(f" Cookie added. {len(discovery.list_cookies())} session(s) in rotation.")

async def cmd_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    rows = discovery.list_cookies()
    if not rows: await update.message.reply_text("No cookies. /howtocookies for setup."); return
    lines = [f"#{r['id']} {r['label']} (added {str(r['added_at'])[:10]})" for r in rows]
    await update.message.reply_text("Sessions:\n" + "\n".join(lines) + "\n\n/removecookie <id>")

async def cmd_removecookie(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery or not ctx.args: return
    discovery.remove_cookie(int(ctx.args[0]))
    await update.message.reply_text(" Removed.")

async def cmd_howtocookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    await update.message.reply_text(
        " Getting Instagram cookies:\n\n"
        "1. Install Kiwi Browser (Android)\n"
        "2. Install Cookie-Editor extension\n"
        "3. Log into instagram.com (use a BURNER account)\n"
        "4. Open Cookie-Editor, copy sessionid + csrftoken values\n"
        "5. /addcookie <sessionid> <csrftoken> <label>\n\n"
        "Add 2-3 burner sessions for rotation.")

async def cmd_setfilters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    if len(ctx.args) < 2:
        c = discovery.get_cfg()
        await update.message.reply_text(f"Current: {c['follower_min']:,}—{c['follower_max']:,}\n/setfilters <min> <max>"); return
    c = discovery.get_cfg()
    c["follower_min"], c["follower_max"] = int(ctx.args[0]), int(ctx.args[1])
    discovery.save_cfg(c)
    await update.message.reply_text(f" {c['follower_min']:,}—{c['follower_max']:,}")

async def cmd_setreach(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    if not ctx.args:
        c = discovery.get_cfg()
        await update.message.reply_text(f"Current: {c['min_reach_ratio']}x\n/setreach 0.8"); return
    c = discovery.get_cfg(); c["min_reach_ratio"] = float(ctx.args[0]); discovery.save_cfg(c)
    await update.message.reply_text(f" Reach: {c['min_reach_ratio']}x")

async def cmd_keywords(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    c = discovery.get_cfg()
    if not ctx.args:
        await update.message.reply_text(
            f" Reward: {', '.join(c['reward_keywords'])}\n"
            f" Penalize: {', '.join(c['penalize_keywords'])}\n\n"
            "Usage:\n/keywords <add|remove> <reward|penalize> <word1, word2...>\n"
            "/keywords clear <reward|penalize>"
        )
        return
        
    action = ctx.args[0].lower()
    if len(ctx.args) >= 2 and action in ("add", "remove", "clear", "reset"):
        target = ctx.args[1].lower()
        if target not in ("reward", "penalize"):
            await update.message.reply_text("Target must be 'reward' or 'penalize'.")
            return
            
        key = target + "_keywords"
        
        if action in ("clear", "reset"):
            c[key] = []
        elif len(ctx.args) >= 3:
            words = [w.strip() for w in " ".join(ctx.args[2:]).lower().split(",")]
            if action == "add":
                for w in words:
                    if w and w not in c[key]: c[key].append(w)
            elif action == "remove":
                for w in words:
                    if w in c[key]: c[key].remove(w)
                    
        discovery.save_cfg(c)
        await update.message.reply_text(f" {key}: {', '.join(c[key])}")

async def cmd_scrapemode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    c = discovery.get_cfg()
    if not ctx.args:
        ck = len(discovery.list_cookies()); ap = len([t for t in discovery.list_apify_tokens() if t["active"]])
        await update.message.reply_text(
            f"Mode: {c['scrape_mode']}\nBackends: {ck} cookies, {ap} Apify\n\n/scrapemode cookie|apify|hybrid"); return
    m = ctx.args[0].lower()
    if m not in ("cookie","apify","hybrid"):
        await update.message.reply_text("Must be: cookie, apify, or hybrid"); return
    discovery.set_scrape_mode(m)
    await update.message.reply_text(f" Mode: {m}")

async def cmd_addapify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: /addapify <token> [label]\n"
            "Get free: https://console.apify.com/account/integrations\n"
            "Each account = $5/month. Add multiple for pooled capacity."); return
    label = ctx.args[1] if len(ctx.args) > 1 else None
    tid = discovery.add_apify_token(ctx.args[0], label)
    await update.message.reply_text(f" Token #{tid} added. Total: {len(discovery.list_apify_tokens())}")

    c = discovery.get_cfg()
    await update.message.reply_text(
        f"Mode: {c['scrape_mode']} | Cookies: {len(discovery.list_cookies())}\n\n"+"\n".join(lines)+"\n\n/removeapify <id>")

async def cmd_removeapify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery or not ctx.args: return
    discovery.remove_apify_token(int(ctx.args[0]))
    await update.message.reply_text(" Removed.")

async def cmd_autoscan_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    if not ctx.args:
        await update.message.reply_text("Usage: /autoscan <hours> then send seeds"); return
    ctx.user_data["autoscan_interval"] = int(ctx.args[0])
    _disc_awaiting_seeds[update.effective_user.id] = "autoscan"
    await update.message.reply_text(f"Send seeds to auto-scan every {ctx.args[0]}h.")

async def cmd_autoscans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    rows = discovery.list_autoscans()
    if not rows: await update.message.reply_text("None active. /autoscan <hours>"); return
    lines = [f"#{r['id']} every {r['interval_hours']}h: {r['seed_list'][:50]}" for r in rows]
    await update.message.reply_text("\n".join(lines)+"\n\n/stopautoscan <id>")

async def cmd_stopautoscan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery or not ctx.args: return
    discovery.stop_autoscan(int(ctx.args[0]))
    await update.message.reply_text(" Stopped.")

async def cmd_scanhistory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    rows = discovery.get_scan_history()
    if not rows: await update.message.reply_text("No scans. /scan"); return
    kb = [[InlineKeyboardButton(f"{str(r['created_at'])[:16]} — {r['profile_count']} found",
            callback_data=f"disc_dl:{r['id']}")] for r in rows[:10]]
    await update.message.reply_text("Past scans (tap to download):",
        reply_markup=InlineKeyboardMarkup(kb))

async def cmd_pushtocrm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    scans = discovery.get_scan_history(1)
    if not scans: await update.message.reply_text("Run /scan first."); return
    cp = scans[0]["csv_path"]
    if not cp or not os.path.exists(cp):
        await update.message.reply_text("CSV file missing."); return
    import csv as cm
    from discovery import CreatorProfile
    profiles = []
    with open(cp,"r",encoding="utf-8") as f:
        reader = cm.DictReader(f)
        # normalize field names to lower case without spaces
        reader.fieldnames = [str(name).strip().lower() for name in reader.fieldnames]
        for row in reader:
            def _get(keys, default=""):
                for k in keys:
                    if k in row and row[k]: return row[k].strip()
                return default
            email_val = _get(["email", "public_email", "businessemail", "business_email"])
            profiles.append(CreatorProfile(
                username=_get(["username", "instagram_username", "handle"]),
                full_name=_get(["full_name", "name", "first_name"]),
                followers=int(row.get("followers",0)) if row.get("followers") else 0,
                following=int(row.get("following",0)) if row.get("following") else 0,
                bio=row.get("bio",""), email=email_val,
                category=row.get("category",""), is_verified=str(row.get("is_verified","")).lower()=="true",
                is_business=str(row.get("is_business","")).lower()=="true",
                post_count=int(row.get("post_count",0)) if row.get("post_count") else 0,
                engagement_rate=float(row.get("engagement_rate",0)) if row.get("engagement_rate") else 0.0,
                reach_ratio=float(row.get("reach_ratio",-1)) if row.get("reach_ratio") else -1.0,
                external_url=row.get("external_url",""), profile_url=row.get("profile_url",""),
                bio_score=int(row.get("bio_score",0)) if row.get("bio_score") else 0,
                cohort=row.get("cohort","C"),
                hop=int(row.get("hop",1)) if row.get("hop") else 1,
                source_seed=row.get("source_seed","")))
    res = discovery.push_to_crm(profiles)
    await update.message.reply_text(
        f" Pushed to CRM:\n  Inserted: {res.get('inserted',0)}\n"
        f"  Duplicates: {res.get('duplicates',0)}\n  Skipped: {res.get('skipped',0)}\n\n"
        "/exit then Outreach to start emailing them.")

async def cmd_cancelscan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if discovery: discovery.cancel_flag = True
    await update.message.reply_text(" Cancelling...")

async def _handle_disc_seeds(update, ctx):
    """Process seed usernames when awaiting after /scan."""
    if not discovery: return False
    uid = update.effective_user.id
    mode = _disc_awaiting_seeds.get(uid)
    if not mode: return False
    _disc_awaiting_seeds[uid] = None

    text = update.message.text
    seeds = [s.strip().lstrip("@") for s in text.replace(",","\n").splitlines() if s.strip()]
    if not seeds:
        await update.message.reply_text("No usernames found."); return True

    if mode == "autoscan":
        interval = ctx.user_data.get("autoscan_interval", 24)
        aid = discovery.add_autoscan(seeds, 1, interval)
        await update.message.reply_text(f" Autoscan #{aid} ({len(seeds)} seeds, every {interval}h)")
        return True

    _disc_pending_seeds[uid] = seeds
    kb = [
        [InlineKeyboardButton("Direct scan (no expansion)", callback_data="disc_hop:0")],
        [InlineKeyboardButton("1-hop (expand following)", callback_data="disc_hop:1"),
         InlineKeyboardButton("2-hop (deeper)", callback_data="disc_hop:2")],
        [InlineKeyboardButton("Extract followings only", callback_data="disc_hop:extract")]
    ]
    await update.message.reply_text(f"Got {len(seeds)} seeds. Expansion depth:",
        reply_markup=InlineKeyboardMarkup(kb))
    return True

async def _handle_discovery_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle all disc_ prefixed callbacks."""
    if not discovery: return
    q = update.callback_query; data = q.data; uid = q.from_user.id
    await q.answer()

    if data.startswith("disc_hop:"):
        val = data.split(":")[1]
        if val == "extract":
            seeds = _disc_pending_seeds.get(uid)
            if not seeds:
                await q.edit_message_text("Expired. /scan"); return
            await q.edit_message_text(f" Extracting following lists for {len(seeds)} seeds...")
            import asyncio
            asyncio.create_task(_mfai_run_extraction(update, ctx, seeds))
        else:
            hops = int(val)
            hops_lbl = f"{hops}-hop" if hops > 0 else "Direct scan"
            kb = [[InlineKeyboardButton("Skip seen", callback_data=f"disc_go:1:{hops}"),
                   InlineKeyboardButton("Include all", callback_data=f"disc_go:0:{hops}")]]
            await q.edit_message_text(f"{hops_lbl}. Skip seen?", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("disc_go:"):
        _, sf, hops = data.split(":"); skip = sf=="1"; hops = int(hops)
        seeds = _disc_pending_seeds.get(uid)
        if not seeds:
            await q.edit_message_text("Expired. /scan"); return
        hops_lbl = f"{hops}-hop" if hops > 0 else "direct scan"
        await q.edit_message_text(f" Scanning {len(seeds)} seeds ({hops_lbl})...")

        log_lines = []
        async def prog(text):
            if text.startswith("EXPORT_TRIGGER:TOKEN_SWAP|"):
                filepath = text.split("|", 1)[1]
                from telegram import InputFile
                try:
                    with open(filepath, "rb") as f:
                        await ctx.bot.send_document(uid, document=InputFile(f, filename="partial_export_apify_swap.csv"),
                                                    caption="⚠️ Sent mid-flight CSV export because Apify token changed.")
                except Exception as e:
                    pass
                return

            is_bar = text.startswith("[") and ("█" in text or "░" in text)
            if is_bar and log_lines and log_lines[-1].startswith("[") and ("█" in log_lines[-1] or "░" in log_lines[-1]):
                log_lines[-1] = text
            else:
                log_lines.append(text)
            display_text = "\n".join(log_lines[-12:])
            try: await q.edit_message_text(display_text)
            except: pass

        cfg = discovery.get_cfg()
        profiles = await discovery.run_pipeline(seeds, cfg, hops, skip, prog)

        if not profiles:
            await ctx.bot.send_message(uid, "No profiles matched. Check /howtocookies or loosen /setfilters."); return

        exports = discovery.export(profiles, seeds)
        
        # log_scan try-except signature fallback
        try:
            email_ct = sum(1 for p in profiles if p.email)
            discovery.log_scan(seeds, hops, len(profiles), email_ct, exports.get("csv"), exports.get("xlsx"), exports.get("outreach"))
        except Exception as e:
            import logging
            logging.getLogger("bot").warning(f"log_scan failed: {e}")

        cohorts = {}
        for p in profiles: cohorts[p.cohort] = cohorts.get(p.cohort,0)+1
        emails = sum(1 for p in profiles if p.email)
        summary = " | ".join(f"{k}:{v}" for k,v in sorted(cohorts.items()))
        await ctx.bot.send_message(uid, f" {len(profiles)} profiles | {summary} |  {emails} with email")

        from telegram import InputFile
        for key in ["csv","xlsx","outreach"]:
            fp = exports.get(key)
            if fp and os.path.exists(fp):
                with open(fp,"rb") as f:
                    await ctx.bot.send_document(uid, InputFile(f, filename=os.path.basename(fp)))

        res = discovery.push_to_crm(profiles)
        kb = [[InlineKeyboardButton(" Switch to Outreach", callback_data="mode_outreach")]]
        await ctx.bot.send_message(uid,
            f" Auto-pushed to CRM: {res.get('inserted',0)} new, {res.get('duplicates',0)} dupes\n\n"
            "Ready to start emailing? Switch to Outreach mode:",
            reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("disc_dl:"):
        sid = int(data.split(":")[1])
        scans = discovery.get_scan_history(50)
        row = next((s for s in scans if s["id"]==sid), None)
        if not row: await q.edit_message_text("Not found."); return
        from telegram import InputFile
        for key in ["csv_path","xlsx_path","outreach_path"]:
            fp = row.get(key)
            if fp and os.path.exists(fp):
                with open(fp,"rb") as f:
                    await ctx.bot.send_document(uid, InputFile(f, filename=os.path.basename(fp)))
        await q.edit_message_text(f"Scan #{sid}: {row['profile_count']} profiles. Files sent above.")



async def cmd_hunt(update, ctx):
    """Enter hunting mode + show hunt keyboard."""
    if not authorized(update.effective_user.id):
        return
    uid = update.effective_user.id
    _user_mode[uid] = "hunting"
    try:
        import discovery as _disc
        ck = len(_disc.list_cookies())
        ap = len([t for t in _disc.list_apify_tokens() if t.get("active")])
        cfg = _disc.get_cfg()
        backend = f"{ck} cookies  {ap} Apify tokens  {cfg.get('scrape_mode','hybrid')} mode"
    except Exception:
        backend = "discovery module not loaded"
    await update.message.reply_text(
        f" <b>Hunting mode</b>\n"
        f"{backend}\n\n"
        "Just tell me who to hunt:\n"
        "  <i>hunt @user1 @user2 @user3</i>\n"
        "  <i>deep scan @user1, @user2</i>  (2-hop)\n"
        "  <i>autoscan @a @b every 12h</i>\n\n"
        "Or tap /scan to be prompted for seeds.",
        parse_mode="HTML",
        reply_markup=_mfai_hunt_kb(),
    )


async def cmd_outreach(update, ctx):
    """Enter outreach mode + show outreach keyboard."""
    if not authorized(update.effective_user.id):
        return
    uid = update.effective_user.id
    _user_mode[uid] = "outreach"
    await update.message.reply_text(
        " <b>Outreach mode</b>\n\n"
        "Send me a screenshot, profile URL, or email to add a creator.\n"
        "Tap /crm for pipeline, /hot for priority replies, /help for all commands.",
        parse_mode="HTML",
        reply_markup=_mfai_outreach_kb(),
    )


async def cmd_exit(update, ctx):
    """Return to main mode-selector."""
    if not authorized(update.effective_user.id):
        return
    uid = update.effective_user.id
    _user_mode.pop(uid, None)
    try:
        import discovery as _disc
        _disc.cancel_flag = True
    except Exception:
        pass
    db.clear_pending_action(uid)
    await update.message.reply_text(
        "Back to main menu.",
        reply_markup=_mfai_main_kb(),
    )


# PATCH9_NL_HELPERS
import re as _mfai_re

_MFAI_HANDLE_RE = _mfai_re.compile(r"@([a-zA-Z0-9_.]{2,30})")
_MFAI_URL_RE = _mfai_re.compile(r"instagram\.com/([a-zA-Z0-9_.]{2,30})", _mfai_re.IGNORECASE)
_MFAI_STOP = {"the","and","for","with","you","please","kindly","start","hunt","hunting",
    "scan","these","them","all","seed","seeds","creators","creator","new","find",
    "add","here","are","look","watch","keep","help","cancel","stop","mode",
    "outreach","email","emails","yes","no","ok","okay","sure","thanks","thank",
    "an","a","is","this","that","account","accounts","from","on","to","of",
    "then","again","now","started","begin","autoscan","every","hourly","daily",
    "like","deep","deeper","thorough","hop","hops","fast","two","one","include",
    "skip","seen","us","let","lets","kick","off","run","do","initiate","launch",
    "search","discover","lookalike","lookalikes","similar","related","get","give",
    "me","my","some","few","more","up","also","just","ass","com"}


def _mfai_extract_handles(text: str) -> list:
    """Extract Instagram usernames from arbitrary text.
    Accepts @user, instagram.com/user, and bare comma/space separated tokens.
    Usernames may contain dots and underscores (e.g. rajdeep.0.21).
    """
    handles = []
    seen = set()

    # @handle
    for m in _MFAI_HANDLE_RE.finditer(text):
        h = m.group(1).lower().strip(".")
        if h and h not in seen:
            handles.append(h); seen.add(h)

    # instagram.com/user
    for m in _MFAI_URL_RE.finditer(text):
        h = m.group(1).lower().strip(".")
        if h and h not in seen:
            handles.append(h); seen.add(h)

    return handles


def _mfai_route(text_lower: str, text_raw: str) -> dict:
    """Regex-first NL router. Returns intent dict or None.
    Order matters: check most-specific patterns first.
    """
    t = text_lower.strip()

    # Trivial exact/near-exact matches
    if _mfai_re.match(r"^(hi|hello|hey|yo|sup|hii+)\s*[!.]*$", t):
        return {"intent": "smalltalk", "reply": "Hey! Tell me what you want — hunt creators or send outreach."}

    if _mfai_re.match(r"^(help|what can you do|commands?)\s*[?!.]*$", t):
        return {"intent": "help"}

    if _mfai_re.match(r"^(cancel|stop|abort|halt)( scan)?\s*[!.]?$", t):
        return {"intent": "cancel_scan"}

    if _mfai_re.match(r"^(push( to)? ?crm|move to crm|send to crm)\s*[!.]?$", t):
        return {"intent": "push_to_crm"}

    if _mfai_re.match(r"^(outreach|email mode|outreach mode|switch to outreach|go to outreach)\s*[!.]?$", t):
        return {"intent": "switch_mode", "mode": "outreach"}

    if _mfai_re.match(r"^(hunt|hunting|hunt mode|hunting mode|scan mode|switch to hunt(ing)?|go to hunt(ing)?)\s*[!.]?$", t):
        return {"intent": "switch_mode", "mode": "hunting"}

    # Extract handles first — they inform many intents
    handles = _mfai_extract_handles(text_raw)

    if _mfai_re.search(r"\b(extract|get followings|following list)\b", t):
        if handles:
            return {"intent": "extract_followings", "seeds": handles}

    # "start hunting" / "begin scanning" / "deep scan" / "let's hunt"
    hunt_verb = _mfai_re.search(
        r"\b(start|begin|let'?s|kick.?off|run|do|initiate|launch|deep|deeper|thorough)\b.*"
        r"\b(hunt|hunting|scan|scanning|discover|find|search)\b", t
    ) or _mfai_re.search(
        r"\b(hunt|scan|find|search|discover)\b.*\b(creator|creators|accounts?|lookalikes?|like)\b", t
    ) or _mfai_re.search(
        r"^(hunt|scan|find|search|discover|autoscan)\b", t
    )

    if hunt_verb:
        # With seeds  run it
        if handles:
            # Autoscan variant
            if _mfai_re.search(r"\b(every|recurring|keep|auto ?scan|hourly|daily)\b", t):
                hrs = 24
                m2 = _mfai_re.search(r"every\s+(\d+)\s*h", t)
                if m2: hrs = int(m2.group(1))
                elif "hourly" in t: hrs = 1
                elif "daily" in t: hrs = 24
                return {"intent": "autoscan", "seeds": handles, "interval_hours": hrs}
            # Depth
            if _mfai_re.search(r"\b(direct|no expansion|0.?hop|filter directly)\b", t):
                hops = 0
            else:
                hops = 2 if _mfai_re.search(r"\b(deep|deeper|2.?hop|thorough|two.?hop)\b", t) else 1
            skip = "include all" not in t and "include seen" not in t
            return {"intent": "add_seeds_and_hunt", "seeds": handles, "hops": hops, "skip_seen": skip}
        # No seeds — prompt for them
        return {"intent": "start_hunt"}

    # Just handles alone  treat as seed list for hunt
    if handles and len(handles) <= 20 and not _mfai_re.search(r"[?]", t):
        return {"intent": "add_seeds_and_hunt", "seeds": handles, "hops": 1, "skip_seen": True}

    # Fallback to LLM if regex fails
    try:
        import llm_orchestrator
        llm_intent = llm_orchestrator.llm_route(text_raw)
        if llm_intent:
            if llm_intent.get("intent") == "unknown":
                return {"intent": "unknown", "reply": "No command found. Please send an Instagram handle (like @username), a screenshot, or use /help to see commands."}
            return llm_intent
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"LLM route fallback failed: {e}")

    # If completely unrecognized and LLM failed
    return {"intent": "unknown", "reply": "No command found. Please send an Instagram handle (like @username), a screenshot, or use /help to see commands."}

async def _mfai_run_scan(update, ctx, seeds, hops, skip_seen, discovery):
    """Background scan task — mirrors the existing scan flow."""
    import asyncio, time as _t
    from telegram import InputFile
    uid = update.effective_user.id
    try:
        discovery.cancel_flag = False
        cfg = discovery.get_cfg()
        # Throttled progress messages
        state = {"last": 0.0, "msg": None}
        log_lines = []
        async def prog(txt):
            if txt.startswith("EXPORT_TRIGGER:TOKEN_SWAP|"):
                filepath = txt.split("|", 1)[1]
                from telegram import InputFile
                try:
                    with open(filepath, "rb") as f:
                        await ctx.bot.send_document(uid, document=InputFile(f, filename="partial_export_apify_swap.csv"),
                                                    caption="⚠️ Sent mid-flight CSV export because Apify token changed.")
                except Exception as e:
                    import logging
                    logging.getLogger("bot").warning(f"Failed to send partial export: {e}")
                return

            is_bar = txt.startswith("[") and ("█" in txt or "░" in txt)
            if is_bar and log_lines and log_lines[-1].startswith("[") and ("█" in log_lines[-1] or "░" in log_lines[-1]):
                log_lines[-1] = txt
            else:
                log_lines.append(txt)
            now = _t.time()
            if now - state["last"] < 1.5 and not txt.startswith("Scan done"):
                return
            state["last"] = now
            display_text = "\n".join(log_lines[-12:])
            try:
                if state["msg"] is None:
                    state["msg"] = await ctx.bot.send_message(uid, display_text)
                else:
                    await state["msg"].edit_text(display_text)
            except Exception:
                pass

        profiles = await discovery.run_pipeline(seeds, cfg, hops, skip_seen, prog)
        if not profiles:
            await ctx.bot.send_message(uid,
                "No profiles matched. Try loosening /setfilters or add more backends.")
            return

        # Export
        exports = discovery.export(profiles, seeds) if hasattr(discovery, "export") \
                  else discovery.export_all(profiles, seeds)

        # Log scan (older/newer signatures)
        try:
            email_ct = sum(1 for p in profiles if p.email)
            discovery.log_scan(seeds, hops, len(profiles), email_ct,
                               exports.get("csv"), exports.get("xlsx"), exports.get("outreach"))
        except Exception as e:
            import logging
            logging.getLogger("bot").warning(f"log_scan failed: {e}")

        # Summary
        cohorts = {}
        for p in profiles:
            cohorts[p.cohort] = cohorts.get(p.cohort, 0) + 1
        emails = sum(1 for p in profiles if p.email)
        summary = "  ".join(f"{k}:{v}" for k, v in sorted(cohorts.items()))
        await ctx.bot.send_message(uid,
            f" {len(profiles)} profiles  {summary}   {emails} with email")

        # Deliver files
        import os as _os
        for key in ("csv", "xlsx", "outreach"):
            fp = exports.get(key)
            if fp and _os.path.exists(fp):
                with open(fp, "rb") as f:
                    await ctx.bot.send_document(uid,
                        InputFile(f, filename=_os.path.basename(fp)))

        # Auto-push to CRM
        push_fn = getattr(discovery, "push_to_crm", None) \
                  or getattr(discovery, "push_profiles_to_crm", None)
        if push_fn:
            res = push_fn(profiles)
            await ctx.bot.send_message(uid,
                f" CRM: {res.get('inserted',0)} new  "
                f"{res.get('duplicates',0)} dupes  "
                f"{res.get('skipped',0)} skipped")
    except Exception as e:
        import logging
        logging.getLogger("mfai_scan").exception("scan failed")
        try:
            await ctx.bot.send_message(uid, f" Scan crashed: {e}")
        except Exception:
            pass

async def _mfai_run_extraction(update, ctx, seeds):
    import time
    uid = update.effective_user.id
    try:
        log_lines = []
        async def prog(text):
            is_bar = text.startswith("[") and ("█" in text or "░" in text)
            if is_bar and log_lines and log_lines[-1].startswith("[") and ("█" in log_lines[-1] or "░" in log_lines[-1]):
                log_lines[-1] = text
            else:
                log_lines.append(text)
            display_text = "\n".join(log_lines[-12:])
            try: await ctx.bot.send_message(uid, display_text)
            except: pass

        usernames = await discovery.extract_only(seeds, prog)
        if not usernames:
            await ctx.bot.send_message(uid, "No usernames were extracted.")
            return

        # Write to TXT file
        import os
        filename = f"extracted_followings_{seeds[0]}_{int(time.time())}.txt"
        filepath = os.path.join(discovery.OUT_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(usernames))

        from telegram import InputFile
        with open(filepath, "rb") as f:
            await ctx.bot.send_document(uid, InputFile(f, filename=filename))
        
        await ctx.bot.send_message(uid, 
            f"Successfully extracted {len(usernames)} usernames. "
            f"You can copy these, and scan them in batches (e.g. 50 at a time) using direct /scan (no expansion) to apply filters.")
    except Exception as e:
        import logging
        logging.getLogger("bot").exception("extraction failed")
        await ctx.bot.send_message(uid, f"Extraction failed: {e}")

async def cmd_exportdms(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    
    limit = None
    if ctx.args:
        try:
            limit = int(ctx.args[0])
        except ValueError:
            await update.message.reply_text("Limit must be a number. Example: /exportdms 100")
            return
            
    await update.message.reply_text("⏳ Fetching undiscovered creators and generating AI personalized DM hooks. This might take a minute...")
    
    conn = db.get_db()
    # Get creators that haven't been exported or DMed yet
    # Assuming 'discovered' or 'Passed' stage.
    query = "SELECT * FROM creators WHERE stage='discovered' OR stage='Passed'"
    if limit:
        query += f" LIMIT {limit}"
        
    rows = conn.execute(query).fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("No new creators found to export.")
        return
        
    import ai_router
    import html_exporter
    import os
    from telegram import InputFile
    
    info_list = []
    for r in rows:
        info_list.append({
            "name": r["name"],
            "handle": r["handle"],
            "platform": r["platform"],
            "bio": r["bio"],
            "niche": r["niche"],
            "followers": r["followers"] or 0
        })
        
    status_msg = await update.message.reply_text(f"⏳ Generating AI personalized DM hooks for {len(info_list)} creators in batches of 50. This might take a few minutes...")
    
    all_hooks = {}
    batch_size = 50
    total_batches = (len(info_list) + batch_size - 1) // batch_size
    
    for i in range(0, len(info_list), batch_size):
        chunk = info_list[i:i+batch_size]
        try:
            batch_num = (i // batch_size) + 1
            pct = batch_num / total_batches if total_batches else 1
            bar_len = 10
            filled = int(bar_len * pct)
            bar = "█" * filled + "░" * (bar_len - filled)
            await status_msg.edit_text(f"⏳ Generating AI hooks...\n[{bar}] {batch_num}/{total_batches} batches")
        except Exception:
            pass
            
        hooks_map = ai_router.generate_batched_dm_hooks(chunk)
        all_hooks.update(hooks_map)
        
    creators_list = []
    for info in info_list:
        h = info["handle"]
        hook = all_hooks.get(h, "saw ur profile and loved ur content")
        
        f = info["followers"]
        if f > 100000: tier = "t1"
        elif f > 50000: tier = "t2"
        else: tier = "t3"
        
        creators_list.append({
            "h": f"@{h}",
            "n": info["name"],
            "t": tier,
            "s": "new",
            "hook": hook
        })
        
    out_path = os.path.join("discovery_exports", "magicfit_dm_tool.html")
    os.makedirs("discovery_exports", exist_ok=True)
    html_exporter.generate_dm_tool(creators_list, out_path)
    
    with open(out_path, "rb") as f:
        await update.message.reply_document(InputFile(f, filename="magicfit_dm_tool.html"), caption="✅ Here is your personalized MagicFit DM Tool. Open this in your browser, tap 'Download Progress' when done, and upload the JSON back here.")


async def cmd_uploaddms(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle when user uploads the dm_progress.json or selections.json file."""
    if not authorized(update.effective_user.id): return
    if not update.message.document or not update.message.document.file_name.endswith('.json'):
        await update.message.reply_text("Please upload a .json file.")
        return
        
    await update.message.reply_text("Processing uploaded JSON file...")
    import json
    import os
    
    file = await update.message.document.get_file()
    path = os.path.join("discovery_exports", "uploaded_file.json")
    await file.download_to_drive(path)
    
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except:
            await update.message.reply_text("Invalid JSON file.")
            return
            
    if not data:
        await update.message.reply_text("The file is empty.")
        return
        
    conn = db.get_db()
    c = conn.cursor()
    
    if isinstance(data, list):
        # Legacy DM progress file
        count = 0
        for handle in data:
            clean_handle = handle.replace("@", "")
            c.execute("UPDATE creators SET stage='contacted' WHERE handle=?", (clean_handle,))
            if c.rowcount > 0:
                count += 1
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ Successfully marked {count} creators as DMed (legacy).")
        return
        
    elif isinstance(data, dict):
        # Tinder Export selections.json
        # Format: { "123": {"good": true, "seed": false, "outreach": true} }
        good_count = 0
        seed_count = 0
        outreach_count = 0
        
        for cid_str, flags in data.items():
            if not isinstance(flags, dict):
                continue
            
            try:
                cid = int(cid_str)
            except:
                continue
                
            # Get the handle for this creator
            c.execute("SELECT handle FROM creators WHERE id=?", (cid,))
            row = c.fetchone()
            if not row:
                continue
            handle = row["handle"]
            
            if flags.get("good", False):
                c.execute("UPDATE creators SET stage='Passed' WHERE id=?", (cid,))
                if c.rowcount > 0:
                    good_count += 1
                    
            if flags.get("seed", False):
                added = db.add_seed(handle, source="tinder_export")
                if added:
                    seed_count += 1
                    
            if flags.get("outreach", False):
                # We want to queue this creator for email outreach
                c.execute("UPDATE creators SET stage='Outreach Queued' WHERE id=?", (cid,))
                if c.rowcount > 0:
                    outreach_count += 1
                    
        conn.commit()
        conn.close()
        
        await update.message.reply_text(
            f"✅ <b>Tinder Selections Applied!</b>\n\n"
            f"👍 Marked Passed: {good_count}\n"
            f"🌱 Added to Seeds: {seed_count}\n"
            f"📨 Queued for Email: {outreach_count}",
            parse_mode="HTML"
        )
    else:
        conn.close()
        await update.message.reply_text("Unrecognized JSON format.")


async def cmd_seen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    seen_set = discovery.get_seen()
    await update.message.reply_text(f" Already scanned profiles: {len(seen_set)} account(s).")

async def cmd_clearseen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    discovery.clear_seen()
    await update.message.reply_text(" Cleared seen profiles database.")

async def cmd_setmaxfollow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    if not ctx.args:
        cfg = discovery.get_cfg()
        await update.message.reply_text(
            f"Max following per seed: {cfg.get('max_following', 500)}\n"
            f"Usage: /setmaxfollow <number> (50-50000)")
        return
    try:
        n = int(ctx.args[0])
        discovery.set_max_following(n)
        await update.message.reply_text(f" Max following per seed set to {max(50, min(50000, n))}")
    except ValueError:
        await update.message.reply_text("Invalid number.")

async def cmd_setmaxresults(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id) or not discovery: return
    if not ctx.args:
        cfg = discovery.get_cfg()
        await update.message.reply_text(
            f"Max results per scan: {cfg.get('max_total', 1000)}\n"
            f"Usage: /setmaxresults <number> (50-50000)")
        return
    try:
        n = int(ctx.args[0])
        discovery.set_max_total(n)
        await update.message.reply_text(f" Max results per scan set to {max(50, min(50000, n))}")
    except ValueError:
        await update.message.reply_text("Invalid number.")

async def _send_tinder_profile(update, ctx, edit_message=None):
    conn = db.get_db()
    c = conn.cursor()
    c.execute("SELECT id, handle, name, followers, engagement_rate, bio, niche, tags, location, profile_url, recent_posts_stats FROM creators WHERE stage='discovered' ORDER BY id ASC LIMIT 1")
    row = c.fetchone()
    conn.close()
    
    if not row:
        msg = "🎉 You have reviewed all discovered profiles! Run /scan or /lookalike to find more."
        if edit_message:
            await edit_message.edit_text(msg)
        else:
            await update.message.reply_text(msg)
        return
        
    cid, handle, name, followers, eng, bio, niche, tags, loc, profile_url, recent_posts_stats = row
    
    if not profile_url:
        profile_url = f"https://instagram.com/{handle}"
        
    # Format the Tinder Card using HTML to prevent formatting syntax errors (e.g. unclosed underscores in bios)
    import html
    safe_name = html.escape(name or 'Unknown')
    safe_handle = html.escape(handle or '')
    
    text = f"👤 <b>{safe_name}</b> (@{safe_handle})\n"
    if followers: text += f"👥 <b>Followers:</b> {followers:,}\n"
    if eng: text += f"📈 <b>Engagement:</b> {eng:.2f}%\n"
    if loc: text += f"📍 <b>Location:</b> {html.escape(str(loc))}\n"
    if niche: text += f"🎯 <b>Niche:</b> {html.escape(str(niche))}\n"
    if tags: text += f"🏷️ <b>Tags:</b> {html.escape(str(tags))}\n"
    if bio: text += f"\n📝 <b>Bio:</b> <i>{html.escape(str(bio))}</i>\n"
    
    # Format 12 recent posts/videos metrics
    if recent_posts_stats:
        try:
            import json
            posts = json.loads(recent_posts_stats)
            if posts:
                text += "\n📊 <b>Last 12 Video/Post Metrics (Non-Pinned):</b>\n"
                for idx, p in enumerate(posts, 1):
                    caption = p.get("caption", "").strip()
                    cap_snippet = f" — <i>\"{html.escape(caption)}\"</i>" if caption else ""
                    
                    views_val = p.get("views", 0)
                    if views_val >= 1_000_000:
                        views_str = f"{views_val/1_000_000:.1f}M"
                    elif views_val >= 1_000:
                        views_str = f"{views_val/1_000:.1f}k"
                    else:
                        views_str = str(views_val)
                        
                    likes_val = p.get("likes", 0)
                    if likes_val >= 1_000_000:
                        likes_str = f"{likes_val/1_000_000:.1f}M"
                    elif likes_val >= 1_000:
                        likes_str = f"{likes_val/1_000:.1f}k"
                    else:
                        likes_str = str(likes_val)
                        
                    comments_val = p.get("comments", 0)
                    if comments_val >= 1_000_000:
                        comments_str = f"{comments_val/1_000_000:.1f}M"
                    elif comments_val >= 1_000:
                        comments_str = f"{comments_val/1_000:.1f}k"
                    else:
                        comments_str = str(comments_val)
                        
                    text += f"{idx}. 📺 {views_str} | 💬 {comments_str} | ❤️ {likes_str}{cap_snippet}\n"
        except Exception as e:
            pass
            
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = [
        [InlineKeyboardButton("👎 Skip", callback_data=f"tinder_skip_{cid}"),
         InlineKeyboardButton("👍 Good", callback_data=f"tinder_pass_{cid}")],
        [InlineKeyboardButton("🌱 Make Seed", callback_data=f"tinder_seed_{cid}"),
         InlineKeyboardButton("🚀 Outreach Now", callback_data=f"tinder_outreach_{cid}")],
        [InlineKeyboardButton("🔗 Open Profile", url=profile_url)]
    ]
    
    markup = InlineKeyboardMarkup(keyboard)
    
    if edit_message:
        await edit_message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")

async def cmd_tinder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    await _send_tinder_profile(update, ctx)

async def cmd_export_tinder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    conn = db.get_db()
    c = conn.cursor()
    c.execute("SELECT id, handle, name, followers, engagement_rate, bio, niche, tags, location, profile_url, email FROM creators WHERE stage='discovered' ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("❌ No discovered creators available to review/export right now.")
        return
        
    creators_list = []
    for r in rows:
        email_val = r["email"] or ""
        has_email = bool(email_val and not email_val.startswith("no_email_"))
        creators_list.append({
            "id": r["id"],
            "handle": r["handle"],
            "name": r["name"] or "Unknown",
            "followers": r["followers"] or 0,
            "engagement_rate": r["engagement_rate"] or 0.0,
            "bio": r["bio"] or "",
            "niche": r["niche"] or "",
            "tags": r["tags"] or "",
            "location": r["location"] or "",
            "profile_url": r["profile_url"] or f"https://instagram.com/{r['handle']}",
            "email": email_val if has_email else "",
            "has_email": has_email
        })
        
    import json
    creators_json = json.dumps(creators_list)
    
    html_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Creator Reacher - Bulk Review</title>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0a0e17;
            --surface: rgba(255,255,255,0.03);
            --surface-hover: rgba(255,255,255,0.06);
            --border: rgba(255,255,255,0.07);
            --primary: #818cf8;
            --primary-glow: rgba(129,140,248,0.15);
            --good: #34d399;
            --skip: #f87171;
            --seed: #22d3ee;
            --outreach: #fb923c;
            --text: #f1f5f9;
            --muted: #94a3b8;
            --email-good: rgba(52,211,153,0.12);
            --email-bad: rgba(248,113,113,0.12);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background: var(--bg);
            color: var(--text);
            font-family: 'Plus Jakarta Sans', system-ui, sans-serif;
            min-height: 100vh;
            padding: 24px;
        }
        header {
            max-width: 1400px;
            margin: 0 auto 24px;
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border);
        }
        .logo h1 {
            font-size: 1.75rem;
            font-weight: 700;
            background: linear-gradient(135deg, #a78bfa, #818cf8, #6366f1);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .logo p { color: var(--muted); font-size: 0.85rem; margin-top: 4px; }
        .toolbar {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
        }
        .toolbar input, .toolbar select {
            background: rgba(255,255,255,0.05);
            border: 1px solid var(--border);
            border-radius: 10px;
            color: var(--text);
            padding: 9px 14px;
            font-family: inherit;
            font-size: 0.85rem;
            outline: none;
            transition: border-color 0.2s;
        }
        .toolbar input:focus, .toolbar select:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 3px var(--primary-glow);
        }
        .toolbar input { width: 220px; }
        .toolbar select { min-width: 140px; cursor: pointer; }
        .toolbar select option { background: #1e293b; }
        .bulk-btns { display: flex; gap: 8px; flex-wrap: wrap; }
        .bulk-btn {
            padding: 8px 16px;
            border-radius: 10px;
            border: 1px solid var(--border);
            background: var(--surface);
            color: var(--muted);
            font-size: 0.8rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        .bulk-btn:hover { background: var(--surface-hover); color: var(--text); }
        .bulk-btn.bg-good { border-color: var(--good); color: var(--good); }
        .bulk-btn.bg-good:hover { background: rgba(52,211,153,0.1); }
        .bulk-btn.bg-skip { border-color: var(--skip); color: var(--skip); }
        .bulk-btn.bg-skip:hover { background: rgba(248,113,113,0.1); }

        .stats-bar {
            max-width: 1400px;
            margin: 0 auto 20px;
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
        }
        .stat-pill {
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 0.8rem;
            font-weight: 600;
            background: var(--surface);
            border: 1px solid var(--border);
        }

        .container {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
            gap: 18px;
            max-width: 1400px;
            margin: 0 auto;
            padding-bottom: 130px;
        }
        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 20px;
            display: flex;
            flex-direction: column;
            position: relative;
            transition: all 0.25s ease;
        }
        .card:hover {
            background: var(--surface-hover);
            transform: translateY(-3px);
            box-shadow: 0 12px 32px rgba(0,0,0,0.3);
        }
        .card.active-state { border-color: rgba(129,140,248,0.4); }
        .card.skip-state { border-color: rgba(248,113,113,0.2); }

        .badge-list {
            position: absolute;
            top: 14px; right: 14px;
            display: flex; gap: 5px; flex-wrap: wrap;
        }
        .badge {
            padding: 3px 8px;
            border-radius: 20px;
            font-size: 0.65rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        .badge.good { background: rgba(52,211,153,0.15); color: var(--good); }
        .badge.seed { background: rgba(34,211,238,0.15); color: var(--seed); }
        .badge.outreach { background: rgba(251,146,60,0.15); color: var(--outreach); }
        .badge.skip { background: rgba(248,113,113,0.15); color: var(--skip); }
        .badge.email-yes { background: var(--email-good); color: var(--good); }
        .badge.email-no { background: var(--email-bad); color: var(--skip); }

        .creator-header { margin-bottom: 12px; padding-right: 90px; }
        .creator-header h3 { font-size: 1.15rem; font-weight: 600; }
        .handle-link {
            display: inline-flex; align-items: center; gap: 5px;
            color: var(--primary); text-decoration: none;
            font-size: 0.82rem; font-weight: 500;
            margin-top: 4px; opacity: 0.9;
        }
        .handle-link:hover { opacity: 1; text-decoration: underline; }

        .email-row {
            margin: 8px 0 12px;
            padding: 8px 12px;
            border-radius: 8px;
            font-size: 0.82rem;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .email-row.has-email {
            background: var(--email-good);
            color: var(--good);
        }
        .email-row.no-email {
            background: var(--email-bad);
            color: var(--skip);
            font-style: italic;
        }

        .metrics {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 8px;
            margin-bottom: 12px;
        }
        .metric {
            background: rgba(255,255,255,0.02);
            border: 1px solid rgba(255,255,255,0.04);
            border-radius: 8px;
            padding: 8px 12px;
        }
        .metric-lbl { font-size: 0.72rem; color: var(--muted); margin-bottom: 2px; }
        .metric-val { font-size: 1rem; font-weight: 700; }

        .bio {
            font-size: 0.82rem; color: #cbd5e1;
            line-height: 1.45; margin-bottom: 12px;
            display: -webkit-box; -webkit-line-clamp: 3;
            -webkit-box-orient: vertical; overflow: hidden;
            cursor: pointer;
        }
        .bio.expanded { -webkit-line-clamp: unset; }

        .tags-loc {
            font-size: 0.78rem; color: var(--muted);
            margin-bottom: 14px;
        }

        .actions {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 6px;
            margin-top: auto;
        }
        .actions button {
            border: 1px solid var(--border);
            background: rgba(255,255,255,0.03);
            color: var(--muted);
            padding: 9px 0;
            border-radius: 10px;
            font-size: 0.78rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        .actions button:hover { background: rgba(255,255,255,0.08); color: #fff; }
        .actions button.active.btn-good { background: rgba(52,211,153,0.2); border-color: var(--good); color: var(--good); }
        .actions button.active.btn-seed { background: rgba(34,211,238,0.2); border-color: var(--seed); color: var(--seed); }
        .actions button.active.btn-outreach { background: rgba(251,146,60,0.2); border-color: var(--outreach); color: var(--outreach); }

        .footer-bar {
            position: fixed;
            bottom: 16px; left: 50%;
            transform: translateX(-50%);
            background: rgba(10,14,23,0.9);
            backdrop-filter: blur(20px);
            border: 1px solid var(--border);
            border-radius: 24px;
            padding: 14px 28px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            width: calc(100% - 40px);
            max-width: 900px;
            box-shadow: 0 16px 48px rgba(0,0,0,0.5);
            z-index: 1000;
        }
        .footer-stats { font-size: 0.85rem; font-weight: 500; display: flex; gap: 16px; flex-wrap: wrap; }
        .footer-stats span { white-space: nowrap; }
        .dl-btn {
            background: linear-gradient(135deg, #6366f1, #818cf8);
            color: #fff;
            border: none;
            padding: 10px 24px;
            border-radius: 16px;
            font-weight: 700;
            font-size: 0.88rem;
            cursor: pointer;
            box-shadow: 0 4px 16px rgba(99,102,241,0.3);
            transition: all 0.2s;
        }
        .dl-btn:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(99,102,241,0.4); }
    </style>
</head>
<body>
    <header>
        <div class="logo">
            <h1>Creator Reacher</h1>
            <p>Bulk Review Dashboard</p>
        </div>
        <div class="toolbar">
            <input type="text" id="search" placeholder="Search handle, name, bio..." oninput="applyFilters()">
            <select id="sort-by" onchange="applyFilters()">
                <option value="default">Sort: Default</option>
                <option value="followers-desc">Followers ↓</option>
                <option value="followers-asc">Followers ↑</option>
                <option value="engagement-desc">Engagement ↓</option>
                <option value="engagement-asc">Engagement ↑</option>
                <option value="name-asc">Name A-Z</option>
            </select>
            <select id="filter-email" onchange="applyFilters()">
                <option value="all">All Creators</option>
                <option value="has-email">Has Email ✅</option>
                <option value="no-email">No Email ❌</option>
            </select>
            <div class="bulk-btns">
                <button class="bulk-btn bg-good" onclick="selectAll('good')">✅ All Good</button>
                <button class="bulk-btn bg-skip" onclick="clearAll()">❌ Clear All</button>
            </div>
        </div>
    </header>

    <div class="stats-bar" id="stats-bar"></div>
    <div class="container" id="grid"></div>

    <div class="footer-bar">
        <div class="footer-stats">
            <span>Total: <b id="s-total">0</b></span>
            <span style="color:var(--good)">Good: <b id="s-good">0</b></span>
            <span style="color:var(--seed)">Seed: <b id="s-seed">0</b></span>
            <span style="color:var(--outreach)">Outreach: <b id="s-outreach">0</b></span>
            <span style="color:var(--skip)">Skip: <b id="s-skip">0</b></span>
        </div>
        <button class="dl-btn" onclick="downloadSelections()">📥 Download JSON</button>
    </div>

    <script>
        const creators = __CREATORS_DATA__;
        const sel = {};
        creators.forEach(c => { sel[c.id] = {good:false, seed:false, outreach:false}; });

        function fmt(n) {
            if (n >= 1e6) return (n/1e6).toFixed(1)+'M';
            if (n >= 1e3) return (n/1e3).toFixed(1)+'k';
            return n;
        }

        function toggle(id, action, btn) {
            sel[id][action] = !sel[id][action];
            btn.classList.toggle('active', sel[id][action]);
            const card = document.getElementById('c-'+id);
            const badges = document.getElementById('b-'+id);
            const any = sel[id].good || sel[id].seed || sel[id].outreach;
            badges.innerHTML = '';
            if (any) {
                card.className = 'card active-state';
                if (sel[id].good) badges.innerHTML += '<span class="badge good">Good</span>';
                if (sel[id].seed) badges.innerHTML += '<span class="badge seed">Seed</span>';
                if (sel[id].outreach) badges.innerHTML += '<span class="badge outreach">Outreach</span>';
            } else {
                card.className = 'card skip-state';
                badges.innerHTML = '<span class="badge skip">Auto-Skip</span>';
            }
            updateStats();
        }

        function selectAll(action) {
            creators.forEach(c => {
                sel[c.id][action] = true;
                const card = document.getElementById('c-'+c.id);
                if (card) {
                    card.className = 'card active-state';
                    const badges = document.getElementById('b-'+c.id);
                    badges.innerHTML = '';
                    if (sel[c.id].good) badges.innerHTML += '<span class="badge good">Good</span>';
                    if (sel[c.id].seed) badges.innerHTML += '<span class="badge seed">Seed</span>';
                    if (sel[c.id].outreach) badges.innerHTML += '<span class="badge outreach">Outreach</span>';
                    // Update button states
                    const btns = card.querySelectorAll('.actions button');
                    btns.forEach(btn => {
                        if (btn.classList.contains('btn-'+action)) btn.classList.add('active');
                    });
                }
            });
            updateStats();
        }

        function clearAll() {
            creators.forEach(c => {
                sel[c.id] = {good:false, seed:false, outreach:false};
                const card = document.getElementById('c-'+c.id);
                if (card) {
                    card.className = 'card skip-state';
                    document.getElementById('b-'+c.id).innerHTML = '<span class="badge skip">Auto-Skip</span>';
                    card.querySelectorAll('.actions button').forEach(b => b.classList.remove('active'));
                }
            });
            updateStats();
        }

        function updateStats() {
            let g=0, s=0, o=0, sk=0;
            Object.values(sel).forEach(v => {
                if (v.good) g++;
                if (v.seed) s++;
                if (v.outreach) o++;
                if (!v.good && !v.seed && !v.outreach) sk++;
            });
            document.getElementById('s-good').textContent = g;
            document.getElementById('s-seed').textContent = s;
            document.getElementById('s-outreach').textContent = o;
            document.getElementById('s-skip').textContent = sk;
        }

        function applyFilters() {
            const q = document.getElementById('search').value.toLowerCase();
            const sortBy = document.getElementById('sort-by').value;
            const emailFilter = document.getElementById('filter-email').value;
            const grid = document.getElementById('grid');
            const cards = Array.from(grid.children);

            cards.forEach(card => {
                const search = card.dataset.search || '';
                const hasEmail = card.dataset.hasEmail === 'true';
                let show = search.includes(q);
                if (emailFilter === 'has-email' && !hasEmail) show = false;
                if (emailFilter === 'no-email' && hasEmail) show = false;
                card.style.display = show ? 'flex' : 'none';
            });

            // Sort
            if (sortBy !== 'default') {
                const sorted = cards.sort((a, b) => {
                    const fa = parseFloat(a.dataset.followers || 0);
                    const fb = parseFloat(b.dataset.followers || 0);
                    const ea = parseFloat(a.dataset.engagement || 0);
                    const eb = parseFloat(b.dataset.engagement || 0);
                    const na = (a.dataset.name || '').toLowerCase();
                    const nb = (b.dataset.name || '').toLowerCase();
                    if (sortBy === 'followers-desc') return fb - fa;
                    if (sortBy === 'followers-asc') return fa - fb;
                    if (sortBy === 'engagement-desc') return eb - ea;
                    if (sortBy === 'engagement-asc') return ea - eb;
                    if (sortBy === 'name-asc') return na.localeCompare(nb);
                    return 0;
                });
                sorted.forEach(c => grid.appendChild(c));
            }
        }

        function render() {
            const grid = document.getElementById('grid');
            grid.innerHTML = '';
            document.getElementById('s-total').textContent = creators.length;

            let withEmail = 0, noEmail = 0;
            creators.forEach(c => { c.has_email ? withEmail++ : noEmail++; });
            document.getElementById('stats-bar').innerHTML =
                '<div class="stat-pill">📊 Total: <b>' + creators.length + '</b></div>' +
                '<div class="stat-pill" style="color:var(--good)">📧 With Email: <b>' + withEmail + '</b></div>' +
                '<div class="stat-pill" style="color:var(--skip)">🚫 No Email: <b>' + noEmail + '</b></div>';

            creators.forEach(c => {
                const emailHtml = c.has_email
                    ? '<div class="email-row has-email">📧 ' + c.email + '</div>'
                    : '<div class="email-row no-email">🚫 No email found</div>';

                const d = document.createElement('div');
                d.className = 'card skip-state';
                d.id = 'c-' + c.id;
                d.dataset.search = (c.name + ' ' + c.handle + ' ' + c.bio + ' ' + c.location + ' ' + c.niche + ' ' + c.email).toLowerCase();
                d.dataset.followers = c.followers;
                d.dataset.engagement = c.engagement_rate;
                d.dataset.name = c.name;
                d.dataset.hasEmail = c.has_email;

                d.innerHTML = `
                    <div class="badge-list" id="b-${c.id}"><span class="badge skip">Auto-Skip</span></div>
                    <div>
                        <div class="creator-header">
                            <h3>${c.name}</h3>
                            <a href="${c.profile_url}" target="_blank" class="handle-link">🔗 @${c.handle}</a>
                        </div>
                        ${emailHtml}
                        <div class="metrics">
                            <div class="metric"><div class="metric-lbl">👥 Followers</div><div class="metric-val">${fmt(c.followers)}</div></div>
                            <div class="metric"><div class="metric-lbl">📈 Engagement</div><div class="metric-val">${c.engagement_rate.toFixed(2)}%</div></div>
                        </div>
                        <div class="bio" onclick="this.classList.toggle('expanded')">${c.bio}</div>
                        ${c.location || c.niche ? '<div class="tags-loc">' + (c.location ? '📍 '+c.location+' ' : '') + (c.niche ? '🎯 '+c.niche : '') + '</div>' : ''}
                    </div>
                    <div class="actions">
                        <button class="btn-good" onclick="toggle(${c.id},'good',this)">👍 Good</button>
                        <button class="btn-seed" onclick="toggle(${c.id},'seed',this)">🌱 Seed</button>
                        <button class="btn-outreach" onclick="toggle(${c.id},'outreach',this)">🚀 Outreach</button>
                    </div>`;
                grid.appendChild(d);
            });
        }

        function downloadSelections() {
            const a = document.createElement('a');
            a.href = 'data:text/json;charset=utf-8,' + encodeURIComponent(JSON.stringify(sel));
            a.download = 'selections.json';
            document.body.appendChild(a); a.click(); a.remove();
        }

        render();
        updateStats();
    </script>
</body>
</html>"""

    
    html_content = html_template.replace("__CREATORS_DATA__", creators_json)
    
    import os
    os.makedirs("discovery_exports", exist_ok=True)
    filename = "tinder_review.html"
    filepath = os.path.join("discovery_exports", filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
        
    from telegram import InputFile
    with open(filepath, "rb") as f:
        await update.message.reply_document(
            document=InputFile(f, filename=filename),
            caption="📝 Here is your bulk review HTML! Open it in any browser, make your selections, download the `selections.json` file, and upload it back here."
        )

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    doc = update.message.document
    if not doc.file_name.endswith(".json"):
        return
        
    file_obj = await ctx.bot.get_file(doc.file_id)
    import io
    import json
    
    buf = io.BytesIO()
    await file_obj.download_to_memory(buf)
    buf.seek(0)
    
    try:
        selections = json.loads(buf.read().decode('utf-8'))
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to parse JSON file: {e}")
        return
        
    if not isinstance(selections, dict):
        await update.message.reply_text("❌ Invalid selections format. Expected a JSON dictionary.")
        return
        
    conn = db.get_db()
    c = conn.cursor()
    
    skipped, passed, seeded, outreach_list = 0, 0, 0, []
    
    for cid_str, actions in selections.items():
        try:
            cid = int(cid_str)
        except ValueError:
            continue
            
        if not isinstance(actions, dict):
            continue
            
        good = actions.get("good", False)
        seed = actions.get("seed", False)
        outreach = actions.get("outreach", False)
        
        if not good and not seed and not outreach:
            # None selected -> Skip (Lost)
            c.execute("UPDATE creators SET stage='Lost', notes='Tinder Rejected' WHERE id=?", (cid,))
            skipped += 1
        else:
            c.execute("UPDATE creators SET stage='Passed' WHERE id=?", (cid,))
            passed += 1
            if seed:
                c.execute("UPDATE creators SET tags=COALESCE(tags, '') || ' seed' WHERE id=?", (cid,))
                seeded += 1
            if outreach:
                outreach_list.append(cid)
            
    conn.commit()
    conn.close()
    
    # DM automation disabled
    # if outreach_list:
    #     import dm_automation
    #     dm_automation.queue_bulk_campaign(outreach_list)
        
    total = skipped + passed
    msg = (
        f"✅ <b>Bulk Selections Processed!</b>\n\n"
        f"Total profiles updated: {total}\n"
        f"👍 Passed (Good/Seed/Outreach): {passed}\n"
        f"🌱 Seed Tag Added: {seeded}\n"
        f"🚀 Outreach Queued: {len(outreach_list)}\n"
        f"👎 Auto-Skipped (Lost): {skipped}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════
#           NEW OVERHAUL COMMANDS: Dashboard, Seeds, DM, Analytics
# ═══════════════════════════════════════════════════════════════════

async def cmd_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show comprehensive real-time status dashboard."""
    if not authorized(update.effective_user.id): return
    
    import time as _t
    
    # Gather all stats
    stages = db.get_stage_counts()
    total_creators, with_email = db.get_creators_with_email_count()
    hot = db.get_hot_leads_count()
    seeds_count = db.get_active_seeds_count()
    dm_stats = db.get_dm_queue_stats()
    ai_keys = db.get_ai_keys_status()
    cookies_count = db.get_active_cookies_count()
    apify_count = db.get_active_apify_count()
    
    # Build stage breakdown
    stage_lines = []
    for stage in ["discovered", "Passed", "opener_sent", "replied", "negotiating", "interested", "closed_won", "Lost"]:
        count = stages.get(stage, 0)
        if count > 0:
            emoji = {"discovered": "🔍", "Passed": "✅", "opener_sent": "📧", "replied": "💬",
                     "negotiating": "🤝", "interested": "🎯", "closed_won": "🏆", "Lost": "❌"}.get(stage, "•")
            stage_lines.append(f"  {emoji} {stage}: {count}")
    
    # AI keys status
    ai_line = " | ".join([
        f"{'✅' if v else '❌'} {k.title()}" for k, v in ai_keys.items()
    ])
    
    # Next DM info
    next_dm_line = ""
    if dm_stats["next_dm"]:
        mins = max(0, (dm_stats["next_dm"]["scheduled_time"] - _t.time()) / 60)
        next_dm_line = f"\n  ⏱ Next DM: @{dm_stats['next_dm']['username']} in ~{mins:.0f} min"
    
    text = (
        f"━━━ 📊 <b>Creator Reacher Dashboard</b> ━━━\n\n"
        f"🌱 <b>Seeds:</b> {seeds_count} active\n"
        f"📋 <b>CRM:</b> {total_creators} total ({with_email} w/ email)\n"
        + "\n".join(stage_lines) + "\n\n"
        f"📨 <b>DM Queue:</b> {dm_stats['pending']} pending, {dm_stats['sent_today']} sent today"
        f"{next_dm_line}\n"
        f"❌ Failed: {dm_stats['failed']}\n"
        f"🔥 <b>Hot Leads:</b> {hot} awaiting reply\n\n"
        f"🤖 <b>AI:</b> {ai_line}\n"
        f"🍪 <b>Cookies:</b> {cookies_count} active\n"
        f"📡 <b>Apify:</b> {apify_count} tokens"
    )
    
    # Inline buttons
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Hunt", callback_data="mode_hunting"),
            InlineKeyboardButton("📧 Outreach", callback_data="mode_outreach"),
        ],
        [
            InlineKeyboardButton("🌱 Seeds", callback_data="dash_seeds"),
            InlineKeyboardButton("📊 CRM", callback_data="dash_crm"),
        ],
        [
            InlineKeyboardButton("📨 DM Status", callback_data="dash_dmstatus"),
            InlineKeyboardButton("📈 Analytics", callback_data="dash_analytics"),
        ],
        [
            InlineKeyboardButton("🍪 Cookies", callback_data="dash_cookies"),
            InlineKeyboardButton("⚙️ Settings", callback_data="dash_settings"),
            InlineKeyboardButton("❓ Help", callback_data="dash_help"),
        ],
    ])
    
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def cmd_apifytokens(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    try:
        import discovery
        tokens = discovery.list_apify_tokens()
        if not tokens:
            await update.message.reply_text("No Apify tokens found. Add them using /addapify.")
            return
            
        processing_msg = await update.message.reply_text("<i>Fetching live Apify usage...</i>", parse_mode="HTML")
            
        import httpx
        text = "<b>Apify Tokens Loaded:</b>\n\n"
        
        async with httpx.AsyncClient(timeout=10) as client:
            for i, token in enumerate(tokens):
                t_val = dict(token).get('token', dict(token).get('key', ''))
                if not t_val:
                    text += f"{i+1}. ⚠️ Invalid token entry\n"
                    continue
                    
                masked = _mask_key(t_val)
                # Fetch limits
                try:
                    r = await client.get(f'https://api.apify.com/v2/users/me/limits?token={t_val}')
                    if r.status_code == 200:
                        data = r.json().get('data', {})
                        max_usd = data.get('limits', {}).get('maxMonthlyUsageUsd', 0)
                        curr_usd = data.get('current', {}).get('monthlyUsageUsd', 0)
                        text += f"<b>{i+1}. {masked}</b>\n"
                        text += f"   💳 Usage: ${curr_usd:.2f} / ${max_usd:.2f}\n"
                    else:
                        text += f"<b>{i+1}. {masked}</b>\n   ⚠️ Failed to fetch usage ({r.status_code})\n"
                except Exception as ex:
                    text += f"<b>{i+1}. {masked}</b>\n   ⚠️ Error fetching usage: {ex}\n"
                
                text += "\n"
                
        await processing_msg.edit_text(text, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error reading Apify tokens: {e}")

async def cmd_seeds(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """List all active seed accounts."""
    if not authorized(update.effective_user.id): return
    
    seeds = db.get_active_seeds()
    if not seeds:
        await update.effective_message.reply_text(
            "🌱 <b>No seeds saved yet.</b>\n\n"
            "Add seeds with: <code>/addseed @user1 @user2</code>",
            parse_mode="HTML"
        )
        return
    
    lines = ["🌱 <b>Active Seeds</b>\n"]
    for s in seeds:
        scanned = s.get("last_scanned") or "never"
        found = s.get("profiles_found", 0)
        src = s.get("source", "manual")
        lines.append(
            f"  @{s['username']} — {found} found | last scan: {scanned} | via: {src}"
        )
    lines.append(f"\n<i>Total: {len(seeds)} seeds</i>")
    
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_addseed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Add seed accounts: /addseed @user1 @user2"""
    if not authorized(update.effective_user.id): return
    
    text = update.message.text or ""
    handles = re.findall(r'@?([a-zA-Z0-9_.]+)', text.replace("/addseed", "").strip())
    
    if not handles:
        await update.message.reply_text(
            "Usage: <code>/addseed @user1 @user2 @user3</code>",
            parse_mode="HTML"
        )
        return
    
    added = 0
    for h in handles:
        if db.add_seed(h, source="manual"):
            added += 1
    
    await update.message.reply_text(
        f"🌱 Added {added} seed(s): {', '.join('@' + h for h in handles)}",
        parse_mode="HTML"
    )


async def cmd_setdminterval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set DM sending intervals: /setdminterval 7 18"""
    if not authorized(update.effective_user.id): return
    
    text = (update.message.text or "").replace("/setdminterval", "").strip()
    parts = text.split()
    
    if len(parts) < 2:
        await update.message.reply_text("DM automation is disabled. Email outreach is used instead.")
        return
    
    try:
        min_val = int(parts[0])
        max_val = int(parts[1])
        if min_val < 1 or max_val < min_val:
            raise ValueError("Invalid range")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Invalid format. Use: /setdminterval 7 18")
        return
    
    db.set_setting("dm_min_interval", str(min_val))
    db.set_setting("dm_max_interval", str(max_val))
    
    await update.message.reply_text(
        f"✅ DM interval set to <b>{min_val}-{max_val} minutes</b> (randomized).",
        parse_mode="HTML"
    )


async def cmd_dmstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show DM queue status."""
    if not authorized(update.effective_user.id): return
    await update.message.reply_text("DM automation is disabled. Use email outreach instead via /startqueue.")


async def cmd_analytics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show funnel conversion analytics."""
    if not authorized(update.effective_user.id): return
    
    stages = db.get_stage_counts()
    total = sum(stages.values()) if stages else 0
    
    if total == 0:
        await update.message.reply_text("📈 No creators in the system yet.")
        return
    
    funnel = [
        ("Discovered", stages.get("discovered", 0), "🔍"),
        ("Passed", stages.get("Passed", 0), "✅"),
        ("Opener Sent", stages.get("opener_sent", 0), "📧"),
        ("Replied", stages.get("replied", 0), "💬"),
        ("Negotiating", stages.get("negotiating", 0), "🤝"),
        ("Interested", stages.get("interested", 0), "🎯"),
        ("Won", stages.get("closed_won", 0), "🏆"),
        ("Lost", stages.get("Lost", 0), "❌"),
    ]
    
    lines = ["📈 <b>Funnel Analytics</b>\n"]
    for label, count, emoji in funnel:
        if count == 0 and label not in ("Discovered", "Lost"):
            continue
        pct = (count / total * 100) if total > 0 else 0
        bar_len = int(pct / 5)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        lines.append(f"{emoji} {label:12s}  {count:4d}  {bar} {pct:.0f}%")
    
    # Conversion rates
    discovered = stages.get("discovered", 0) + stages.get("Passed", 0) + stages.get("opener_sent", 0) + stages.get("replied", 0) + stages.get("negotiating", 0) + stages.get("interested", 0) + stages.get("closed_won", 0)
    replied = stages.get("replied", 0) + stages.get("negotiating", 0) + stages.get("interested", 0) + stages.get("closed_won", 0)
    won = stages.get("closed_won", 0)
    sent = stages.get("opener_sent", 0) + replied
    
    lines.append(f"\n<b>Conversion Rates:</b>")
    if sent > 0:
        lines.append(f"  Reply rate: {replied}/{sent} ({replied/sent*100:.1f}%)")
    if replied > 0:
        lines.append(f"  Close rate: {won}/{replied} ({won/replied*100:.1f}%)")
    
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_passall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Pass all discovered creators."""
    if not authorized(update.effective_user.id): return
    
    conn = db.get_db()
    c = conn.cursor()
    c.execute("UPDATE creators SET stage='Passed' WHERE stage='discovered'")
    count = c.rowcount
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"✅ Passed {count} discovered creators to Passed stage.")


async def cmd_skipall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Skip all discovered creators."""
    if not authorized(update.effective_user.id): return
    
    conn = db.get_db()
    c = conn.cursor()
    c.execute("UPDATE creators SET stage='Lost', notes='Bulk skipped' WHERE stage='discovered'")
    count = c.rowcount
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"👎 Skipped {count} discovered creators (moved to Lost).")


async def cmd_outreachall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Queue outreach for all passed creators with emails."""
    if not authorized(update.effective_user.id): return
    conn = db.get_db()
    c = conn.cursor()
    c.execute(
        "SELECT id FROM creators WHERE stage='Passed' AND email IS NOT NULL AND email NOT LIKE 'no_email_%'"
    )
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("❌ No passed creators with emails to queue.")
        return
    
    cids = [r["id"] for r in rows]
    import email_automation
    queued = email_automation.queue_bulk_campaign(cids)
    
    await update.message.reply_text(f"🚀 Queued {queued} personalized emails for outreach.")


async def cmd_retryemails(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Retry all failed emails."""
    if not authorized(update.effective_user.id): return
    conn = db.get_db()
    c = conn.cursor()
    c.execute("SELECT creator_id FROM emails_sent WHERE status='failed'")
    failed = [r[0] for r in c.fetchall()]
    
    if not failed:
        conn.close()
        await update.message.reply_text("✅ No failed emails to retry.")
        return
        
    c.execute("DELETE FROM emails_sent WHERE status='failed'")
    conn.commit()
    conn.close()
    
    import email_automation
    count = email_automation.queue_bulk_campaign(failed)
    
    await update.message.reply_text(f"🔄 Re-queued {count} failed emails.")


async def cmd_previewemail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Preview the email that would be sent: /previewemail @username"""
    if not authorized(update.effective_user.id): return
    
    text = (update.message.text or "").replace("/previewemail", "").strip()
    handles = re.findall(r'@?([a-zA-Z0-9_.]+)', text)
    
    if not handles:
        await update.message.reply_text("Usage: <code>/previewemail @username</code>", parse_mode="HTML")
        return
    
    handle = handles[0]
    conn = db.get_db()
    c = conn.cursor()
    c.execute("SELECT handle, name, bio, tags, engagement_rate, location, is_business, email FROM creators WHERE handle=?", (handle,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        await update.message.reply_text(f"❌ Creator @{handle} not found in CRM.")
        return
    if not row["email"] or "no_email" in row["email"]:
        await update.message.reply_text(f"❌ Creator @{handle} has no valid email address.")
        return
        
    await update.message.reply_text(f"🤖 Generating preview email for @{handle} ({row['email']})...")
    
    cdata = {
        "username": row["handle"],
        "name": row["name"] or handle,
        "bio": row["bio"] or "",
        "tags": row["tags"] or "",
        "engagement_rate": row["engagement_rate"],
        "location": row["location"] or "",
        "is_business": row["is_business"],
    }
    import email_automation
    subject, body = email_automation.generate_personalized_email(cdata)
    
    await update.message.reply_text(
        f"📝 <b>Preview Email for @{handle}:</b>\n\n"
        f"<b>Subject:</b> {subject}\n\n"
        f"<i>{body}</i>\n\n"
        f"<i>(Note: AI generation may vary slightly upon actual sending)</i>",
        parse_mode="HTML"
    )


# PATCH9_REPLY_KEYBOARD
def _mfai_main_kb():
    """Main mode-selector keyboard using inline buttons for a premium feel."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Dashboard", callback_data="dash_dashboard"),
            InlineKeyboardButton("🔍 Hunt Creators", callback_data="mode_hunting")
        ],
        [
            InlineKeyboardButton("📧 CRM / Outreach", callback_data="mode_outreach"),
            InlineKeyboardButton("🍪 Cookies", callback_data="dash_cookies")
        ],
        [
            InlineKeyboardButton("🌱 Seeds", callback_data="dash_seeds"),
            InlineKeyboardButton("⚙️ Settings", callback_data="dash_settings")
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="dash_help")
        ]
    ])

async def cmd_start(update, ctx):
    """Premium Dual-mode entry: shows Hunting/Outreach interactive dashboard."""
    if not authorized(update.effective_user.id):
        return
    uid = update.effective_user.id

async def cmd_ai(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid): return
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        await update.message.reply_text("Usage: /ai <what you want me to do>")
        return
        
    await update.message.reply_text("🤖 Thinking...")
    try:
        import ai_command_orchestrator
    except ImportError:
        await update.message.reply_text("AI orchestrator not available.")
        return
        
    commands = ai_command_orchestrator.generate_ai_commands(prompt)
    
    if not commands:
        await update.message.reply_text("I couldn't figure out which commands to run. Please try rewording.")
        return
        
    await update.message.reply_text(f"Executing commands:\n" + "\n".join(commands))
    
    for cmd_str in commands:
        cmd_str = cmd_str.strip()
        if not cmd_str.startswith("/"): continue
        parts = cmd_str.lstrip("/").split()
        if not parts: continue
        cmd_name = parts[0].lower()
        cmd_args = parts[1:]
        
        handler_found = False
        for group in ctx.application.handlers.values():
            for handler in group:
                from telegram.ext import CommandHandler
                if isinstance(handler, CommandHandler) and cmd_name in handler.commands:
                    # Monkey-patch args
                    old_args = getattr(ctx, "args", [])
                    ctx.args = cmd_args
                    try:
                        await handler.callback(update, ctx)
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error(f"AI execution of {cmd_name} failed: {e}")
                    finally:
                        ctx.args = old_args
                    handler_found = True
                    break
            if handler_found: break

    # Auto-recover onboarded state
    if not db.is_onboarded() and db.get_all_accounts():
        db.mark_onboarded()
        db.clear_pending_action(uid)
        try:
            import reply_watcher, followup_scheduler
            reply_watcher.start_reply_watcher(_reply_notification_callback)
            followup_scheduler.start_scheduler(_followup_notification_callback)
        except Exception:
            pass
    if not db.is_onboarded():
        await _start_onboarding(update, ctx)
        return
    _user_mode[uid] = "main"
    
    # Check AI/API health for a quick status indicator
    keys = db.get_ai_keys_status()
    has_ai = any(keys.values())
    cookies = db.get_active_cookies_count()
    
    status_emoji = "🟢" if (has_ai and cookies > 0) else "🟡"
    
    await update.message.reply_text(
        f"<b>✨ MagicFit AI Creator OS</b> ✨\n\n"
        f"<i>Your premium command center for creator partnerships.</i>\n\n"
        f"<b>System Status:</b> {status_emoji} {'Operational' if status_emoji == '🟢' else 'Check Cookies/AI'}\n"
        f"<b>Active Cookies:</b> {cookies}\n\n"
        f"👇 Select an action below or just tell me what to do in plain English!",
        parse_mode="HTML",
        reply_markup=_mfai_main_kb(),
    )

async def _handle_dashboard_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle inline button clicks from dashboard."""
    q = update.callback_query
    if not authorized(update.effective_user.id): return
    await q.answer()
    
    data = q.data
    if data == "dash_dashboard":
        await cmd_dashboard(update, ctx)
    elif data == "dash_seeds":
        await cmd_seeds(update, ctx)
    elif data == "dash_crm":
        await cmd_crm(update, ctx)
    elif data == "dash_dmstatus":
        await q.message.reply_text("DM automation is disabled.", parse_mode="HTML")
    elif data == "dash_analytics":
        await cmd_analytics(update, ctx)
    elif data == "dash_cookies":
        await cmd_cookies(update, ctx)
    elif data == "dash_settings":
        await cmd_settings(update, ctx)
    elif data == "dash_help":
        await cmd_help(update, ctx)


def main():

    # Validate config
    errors = config.validate_config()
    if errors:
        print("  Configuration errors:")
        for e in errors:
            print(f"  - {e}")
        print("\nFill in your .env file (copy .env.example to .env first).")
        return

    # Initialize DB
    db.init_db()

    # Build bot
    # Longer timeouts + connection pooling for flaky mobile data (Termux)
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=10.0,
    )
    # get_updates uses long-polling, needs its own longer read timeout
    polling_request = HTTPXRequest(
        connection_pool_size=4,
        connect_timeout=30.0,
        read_timeout=40.0,
        write_timeout=30.0,
        pool_timeout=10.0,
    )
    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .request(request)
        .get_updates_request(polling_request)
        .post_init(post_init)
        .build()
    )

    # Command handlers
        # Command handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("ai", cmd_ai))
    
    # New Commands
    application.add_handler(CommandHandler("lookalike", cmd_lookalike))
    application.add_handler(CommandHandler("launch_campaign", cmd_launch_campaign))
    application.add_handler(CommandHandler("files", cmd_files))
    application.add_handler(CommandHandler("removefile", cmd_removefile))
    application.add_handler(CommandHandler("tinder", cmd_tinder))

    # PATCH9_MODE_CMDS_REGISTERED
    application.add_handler(CommandHandler("hunt", cmd_hunt))
    application.add_handler(CommandHandler("outreach", cmd_outreach))
    application.add_handler(CommandHandler("exit", cmd_exit))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CommandHandler("setprompt", cmd_setprompt))
    application.add_handler(CommandHandler("setemailprompt", cmd_setemailprompt))
    application.add_handler(CommandHandler("setemailtemplate", cmd_setemailtemplate))
    application.add_handler(CommandHandler("queue", cmd_queue))
    application.add_handler(CommandHandler("editemail", cmd_editemail))
    application.add_handler(CommandHandler("deleteemail", cmd_deleteemail))

    application.add_handler(CommandHandler("setinterval", cmd_setinterval))
    application.add_handler(CommandHandler("setfollowup", cmd_setfollowup))
    application.add_handler(CommandHandler("setreplycheck", cmd_setreplycheck))
    application.add_handler(CommandHandler("setautoreply", cmd_setautoreply))
    application.add_handler(CommandHandler("accounts", cmd_accounts))
    application.add_handler(CommandHandler("addaccount", cmd_addaccount))
    application.add_handler(CommandHandler("removeaccount", cmd_removeaccount))
    application.add_handler(CommandHandler("addalias", cmd_addalias))
    application.add_handler(CommandHandler("bulkaddalias", cmd_bulkaddalias))
    application.add_handler(CommandHandler("listaliases", cmd_listaliases))
    application.add_handler(CommandHandler("removealias", cmd_removealias))
    application.add_handler(CommandHandler("togglealias", cmd_togglealias))
    application.add_handler(CommandHandler("pausemasteraliases", cmd_pausemasteraliases))
    application.add_handler(CommandHandler("resumemasteraliases", cmd_resumemasteraliases))
    application.add_handler(CommandHandler("updateemail", cmd_updateemail))
    application.add_handler(CommandHandler("aliasesstats", cmd_aliasesstats))
    application.add_handler(CommandHandler("warmupstatus", cmd_warmupstatus))
    application.add_handler(CommandHandler("setlimit", cmd_setlimit))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("report", cmd_report))
    application.add_handler(CommandHandler("fullreport", cmd_fullreport))
    application.add_handler(CommandHandler("pipeline", cmd_pipeline))
    application.add_handler(CommandHandler("queue", cmd_queue))
    application.add_handler(CommandHandler("startqueue", cmd_startqueue))
    application.add_handler(CommandHandler("stopqueue", cmd_stopqueue))
    application.add_handler(CommandHandler("startwatcher", cmd_startwatcher))
    application.add_handler(CommandHandler("stopwatcher", cmd_stopwatcher))
    application.add_handler(CommandHandler("dmlist", cmd_dmlist))
    application.add_handler(CommandHandler("dmdone", cmd_dmdone))
    application.add_handler(CommandHandler("preview", cmd_preview))
    application.add_handler(CommandHandler("checkreplies", cmd_checkreplies))
    application.add_handler(CommandHandler("replystatus", cmd_replystatus))
    application.add_handler(CommandHandler("apikeys", cmd_apikeys))
    application.add_handler(CommandHandler("apifytokens", cmd_apifytokens))
    application.add_handler(CommandHandler("setopenrouter", cmd_setopenrouter))
    application.add_handler(CommandHandler("setopenroutermodel", cmd_setopenroutermodel))
    application.add_handler(CommandHandler("setcftoken", cmd_setcftoken))
    application.add_handler(CommandHandler("setcfzone", cmd_setcfzone))
    application.add_handler(CommandHandler("createaliases", cmd_createaliases))
    application.add_handler(CommandHandler("setgemini", cmd_setgemini))
    application.add_handler(CommandHandler("setmistral", cmd_setmistral))
    application.add_handler(CommandHandler("setgroq", cmd_setgroq))
    application.add_handler(CommandHandler("setnvidia", cmd_setnvidia))
    application.add_handler(CommandHandler("setscrapingdog", cmd_setscrapingdog))
    application.add_handler(CommandHandler("crm", cmd_crm))
    application.add_handler(CommandHandler("creator", cmd_creator))
    application.add_handler(CommandHandler("hot", cmd_hot))
    application.add_handler(CommandHandler("reply", cmd_reply))
    application.add_handler(CommandHandler("resend", cmd_resend))
    application.add_handler(CommandHandler("won", cmd_won))
    application.add_handler(CommandHandler("lost", cmd_lost))
    application.add_handler(CommandHandler("note", cmd_note))
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("bulk", cmd_bulk))
    
    # Discovery commands
    application.add_handler(CommandHandler("exportdms", cmd_exportdms))
    application.add_handler(CommandHandler("export_tinder", cmd_export_tinder))
    application.add_handler(CommandHandler("export", cmd_export_tinder))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("addcookie", cmd_addcookie))
    application.add_handler(CommandHandler("cookies", cmd_cookies))
    application.add_handler(CommandHandler("removecookie", cmd_removecookie))
    application.add_handler(CommandHandler("howtocookies", cmd_howtocookies))
    application.add_handler(CommandHandler("setfilters", cmd_setfilters))
    application.add_handler(CommandHandler("setreach", cmd_setreach))
    application.add_handler(CommandHandler("keywords", cmd_keywords))
    application.add_handler(CommandHandler("scrapemode", cmd_scrapemode))
    application.add_handler(CommandHandler("addapify", cmd_addapify))
    application.add_handler(CommandHandler("apifytokens", cmd_apifytokens))
    application.add_handler(CommandHandler("removeapify", cmd_removeapify))
    application.add_handler(CommandHandler("autoscan", cmd_autoscan_setup))
    application.add_handler(CommandHandler("autoscans", cmd_autoscans))
    application.add_handler(CommandHandler("stopautoscan", cmd_stopautoscan))
    application.add_handler(CommandHandler("scanhistory", cmd_scanhistory))
    application.add_handler(CommandHandler("pushtocrm", cmd_pushtocrm))
    application.add_handler(CommandHandler("cancelscan", cmd_cancelscan))
    application.add_handler(CommandHandler("seen", cmd_seen))
    application.add_handler(CommandHandler("clearseen", cmd_clearseen))
    application.add_handler(CommandHandler("setmaxfollow", cmd_setmaxfollow))
    application.add_handler(CommandHandler("setmaxresults", cmd_setmaxresults))
    application.add_handler(CallbackQueryHandler(_handle_discovery_callback, pattern="^disc_"))
    
    # Mode callbacks
    application.add_handler(CallbackQueryHandler(_handle_mode_callback, pattern="^mode_"))
    
    # Overhaul: new command handlers
    application.add_handler(CommandHandler("dashboard", cmd_dashboard))
    application.add_handler(CommandHandler("d", cmd_dashboard))
    application.add_handler(CommandHandler("seeds", cmd_seeds))
    application.add_handler(CommandHandler("addseed", cmd_addseed))
    application.add_handler(CommandHandler("removeseed", cmd_removeseed))
    application.add_handler(CommandHandler("setdminterval", cmd_setdminterval))
    application.add_handler(CommandHandler("dmstatus", cmd_dmstatus))
    application.add_handler(CommandHandler("analytics", cmd_analytics))
    application.add_handler(CommandHandler("passall", cmd_passall))
    application.add_handler(CommandHandler("skipall", cmd_skipall))
    application.add_handler(CommandHandler("outreachall", cmd_outreachall))
    application.add_handler(CommandHandler("retryemails", cmd_retryemails))
    application.add_handler(CommandHandler("previewemail", cmd_previewemail))
    application.add_handler(CallbackQueryHandler(_handle_dashboard_callback, pattern="^dash_"))

    # Callbacks
    async def _handle_file_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not authorized(update.effective_user.id): return
        
        fname = q.data.replace("getf_", "")
        import os
        from telegram import InputFile
        
        fp = os.path.join("discovery_exports", fname)
        if os.path.exists(fp):
            await q.answer("Uploading file...")
            with open(fp, "rb") as f:
                await ctx.bot.send_document(update.effective_user.id, InputFile(f, filename=fname))
        else:
            await q.answer("File not found!", show_alert=True)

    async def _handle_tinder_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not authorized(update.effective_user.id): return
        
        data = q.data
        cid = data.split("_")[-1]
        action = data.replace(f"_{cid}", "")
        
        conn = db.get_db()
        c = conn.cursor()
        
        if action == "tinder_skip":
            c.execute("UPDATE creators SET stage='Lost', notes='Tinder Rejected' WHERE id=?", (cid,))
        elif action == "tinder_pass":
            c.execute("UPDATE creators SET stage='Passed' WHERE id=?", (cid,))
        elif action == "tinder_seed":
            c.execute("UPDATE creators SET stage='Passed', tags=COALESCE(tags, '') || ' seed' WHERE id=?", (cid,))
        elif action == "tinder_outreach":
            c.execute("UPDATE creators SET stage='Passed' WHERE id=?", (cid,))
            # dm_automation disabled
            pass
            
        conn.commit()
        conn.close()
        
        # Load next profile in the same message
        await _send_tinder_profile(update, ctx, edit_message=q.message)
        await q.answer("Done!")

    application.add_handler(CallbackQueryHandler(_handle_tinder_callback, pattern="^tinder_"))
    application.add_handler(CallbackQueryHandler(_handle_file_callback, pattern="^getf_"))
    application.add_handler(CallbackQueryHandler(handle_callback, pattern="^(onb_|reply_auto_|reply_manual_|reply_close_|send_reply_|edit_reply_|regen_reply_|cancel_reply_|view_creator_|mark_lost_)"))
    application.add_handler(CallbackQueryHandler(handle_callback_extra, pattern="^(reply_default_|reply_custom_)"))

    # Messages
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.Document.ALL, cmd_uploaddms))

    application.add_error_handler(error_handler)

    print("MagicFit Outreach Bot starting...", flush=True)
    logger.info("MagicFit Outreach Bot starting...")
    
    try:
        import inbox_watcher
        # dm_automation.start_dm_worker() # Disabled per user request (moved to manual HTML tool)
        inbox_watcher.start_inbox_watcher(application)
        logger.info("Started Inbox watcher thread. (DM worker disabled)")
    except Exception as e:
        logger.error(f"Failed to start background threads: {e}")
        
    # PATCH9_KEEPALIVE
    import time as _t_ka
    _ka_crashes = 0
    _ka_last = 0.0
    while True:
        try:
            application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
            break
        except Exception as _e_ka:
            _ka_now = _t_ka.time()
            _ka_crashes = _ka_crashes + 1 if _ka_now - _ka_last < 60 else 1
            _ka_last = _ka_now
            if _ka_crashes >= 5:
                print(f" 5 crashes in 60s, giving up: {_e_ka}", flush=True)
                raise
            print(f"  polling crashed: {_e_ka} — restart #{_ka_crashes} in 5s", flush=True)
            _t_ka.sleep(5)


if __name__ == "__main__":
    main()



