# MagicFit AI Outreach Bot

AI-powered Telegram bot for automated influencer outreach.

## What it does

- Send a screenshot, profile link, or email → bot extracts everything and sends a personalized outreach email
- **Gemini Vision OCR** reads screenshots (falls back to Tesseract)
- **Gemini / Groq / NVIDIA NIM** generates unique emails per creator (no templates)
- **Multi-Gmail rotation** - add unlimited accounts, per-account daily limits
- **IMAP reply detection** - polls every 5 min, alerts you on Telegram with Auto/Manual buttons
- **Auto-reply flow** - bot drafts contextual responses, you approve or edit
- **Follow-up scheduler** - 3 followups by default (3 days, then 2 days)
- **Full reporting** in Telegram

## ⚠️ SECURITY FIRST

If you previously shared your bot token anywhere:
1. Go to [@BotFather](https://t.me/BotFather)
2. `/mybots` → your bot → API Token → **Revoke current token**
3. Generate a new one
4. Paste the NEW token into `.env` (never paste it in chats)

## Quick Setup

### Step 1: Get your credentials

| What | Where | Cost |
|------|-------|------|
| Telegram bot token | [@BotFather](https://t.me/BotFather) → `/newbot` | Free |
| Your Telegram user ID | [@userinfobot](https://t.me/userinfobot) → `/start` | Free |
| Gemini API key | [aistudio.google.com](https://aistudio.google.com) | Free, no card |
| Groq API key | [console.groq.com](https://console.groq.com) | Free, no card |
| NVIDIA NIM key | [build.nvidia.com](https://build.nvidia.com) | Free, no card |
| Scrapingdog key | [scrapingdog.com](https://scrapingdog.com) | Free 1000 credits/mo |
| Gmail app password | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (needs 2FA) | Free |

You only need **one** AI key minimum (Gemini recommended). The bot uses the fallback chain.

### Step 2: Configure

Copy `.env.example` to `.env` and fill in:

```
TELEGRAM_BOT_TOKEN=your_token_here
ALLOWED_USER_ID=your_telegram_user_id
GEMINI_API_KEY=your_gemini_key
GROQ_API_KEY=your_groq_key
NVIDIA_API_KEY=your_nvidia_key
SCRAPINGDOG_API_KEY=your_scrapingdog_key
```

### Step 3: Deploy to Pella

1. Zip all files (`bot.py`, `config.py`, `database.py`, etc.) + `.env` + `requirements.txt`
2. Go to [pella.app/new](https://pella.app/new)
3. Sign up (no credit card needed)
4. Choose "File Upload"
5. Upload your zip (max 30MB - this project is ~50KB)
6. Pella detects Python automatically
7. Set the **start command** to: `python bot.py`
8. Hit deploy

**⚠️ Pella free tier requires manual renewal every 24h.** Log into pella.app daily to click renew, or upgrade to paid ($1.25/GB RAM).

### Step 4: First chat with the bot

Open your bot in Telegram → `/start` → walk through onboarding:
1. Add your Gmail account(s) with app passwords
2. Set followup cadence
3. Pick preview or trust mode for auto-replies
4. Add Scrapingdog key

Done. Now send screenshots / links / emails.

## Usage

### Add a creator (3 ways)

**Screenshot**: Take a screenshot of any creator's profile bio. Send to bot. Gemini Vision reads everything, generates personalized email, sends it.

**Link**: Paste an Instagram URL like `instagram.com/username`. Bot uses Scrapingdog to get bio + email.

**Email**: Just paste `creator@example.com`. Bot generates and sends.

### When a creator replies

Bot detects via IMAP, sends you a Telegram notification with buttons:
- **🤖 Auto Reply** → AI drafts response → you approve/edit/regenerate → send
- **✋ Manual** → marks as your responsibility, bot stays quiet
- **❌ Closed** → marks deal dead, cancels followups

### Commands cheat sheet

```
/start              Initial setup
/help               All commands
/report             Today's stats
/fullreport         7-day breakdown  
/pipeline           Creators by stage
/queue              Queue status
/accounts           List Gmail accounts
/addaccount         Add Gmail account
/setlimit           Per-account daily limit
/setprompt          Edit AI system prompt
/setfollowup 3 2 3  Customize follow-up cadence
/setinterval 120 420  Send delay min/max seconds
/setautoreply       preview or trust mode
/dmlist             Pending DM reminders
/preview Name       Preview an opener email
```

## File structure

```
magicfit-bot/
├── bot.py                    Main Telegram interface
├── config.py                  Loads .env
├── database.py                SQLite schema + helpers
├── ai_router.py               Gemini → Groq → NVIDIA fallback
├── ocr_router.py              Gemini Vision → Tesseract
├── scrapers.py                Instagram + generic scraping
├── email_sender.py            Multi-account SMTP + rotation
├── reply_watcher.py           IMAP polling
├── followup_scheduler.py      Background followup sender
├── templates_fallback.py      Last-resort if all AI fails
├── requirements.txt
├── .env.example               Template
└── README.md
```

## Hosting alternatives if Pella's renewals annoy you

| Host | Free 24/7? | Setup |
|------|-----------|-------|
| **Your laptop** | If always on | `python bot.py` |
| **Old Android phone** | Yes | Install Termux, run from phone |
| **HuggingFace Spaces** | Yes | More setup, but truly free 24/7 |
| **Render.com** | Sleeps after 15 min idle | Easy deploy, no card |
| **Pella** | Needs daily renewal | Easiest deploy |

## Troubleshooting

**"Auth failed for email@gmail.com"** → App password is wrong. Regenerate at myaccount.google.com/apppasswords. Remove spaces.

**"No email found in screenshot"** → Take a clearer screenshot of the bio section, or paste the email directly.

**"Scrapingdog rate limit hit"** → Free tier is 1000/mo. Use screenshots instead for the rest of the month.

**Bot stops responding** → Pella tier needs renewal. Or restart deploy.

**"At least one AI key required"** → Add Gemini key minimum to `.env`.

## What this costs

**$0**. Everything is free tier:
- Gemini: 1,500 req/day free
- Groq: free with no card
- NVIDIA NIM: free, 40 req/min
- Gmail SMTP: free, 500 emails/day per account
- Scrapingdog: free 1,000 credits/month
- Pella: free with daily renewal
- Telegram Bot API: free

## License

Personal use. Don't sell/redistribute.
