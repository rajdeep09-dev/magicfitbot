"""
Discovery Engine -- Instagram lookalike scraper.

PIPELINE
--------
Cookie mode (master):
  1. Fetch seed profile via web_profile_info (gets user id + basic data)
  2. Fetch their FOLLOWING LIST via /api/v1/friendships/{id}/following/
     (paginated, up to MAX_FOLLOWING accounts -- who they follow = peer network)
  3. For each followed account, fetch their full profile
  4. Score + filter each profile through all filters below
  5. Push matched profiles to CRM

Apify mode (fallback / parallel):
  afanasenko/instagram-profile-scraper with operationMode=analyzeFollowersFollowing

FILTERS (all must pass)
-----------------------
1. Follower range: 10K - 500K (configurable)
   Budget cap: under_50k=$100, 50k_100k=$150, 100k_plus=$300
   100K+ creators accepted only if score justifies $300 flat fee

2. Location: 50%+ non-India audience (proxy via available signals)
   APPROVED: English-speaking countries (US/UK/CA/AU/NZ/IE),
             French-speaking (FR/BE/CH), German-speaking (DE/AT/CH)
   DISQUALIFY: India-dominant signals (country code +91, Devanagari bio,
               India flags/cities, .in domains)

3. Engagement: strict 0.8x reach ratio on last 12 posts
   (avg video views / followers >= 0.8)
   Photo-only accounts: pass if engagement rate >= 3%

4. Niche priority (additive score, not a hard filter):
   ecommerce > business/startup > ai tool reviewers
"""

import os
import re
import csv
import json
import time
import html
import random
import sqlite3
import asyncio
import logging
import requests
import urllib.parse
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Callable, List, Tuple, Dict, Set

try:
    import openpyxl
    _HAVE_XLSX = True
except ImportError:
    _HAVE_XLSX = False

try:
    from apify_client import ApifyClient
    _HAVE_APIFY_CLIENT = True
except ImportError:
    _HAVE_APIFY_CLIENT = False

import database as db

log = logging.getLogger("discovery")

# =====================================================================
#  CONSTANTS
# =====================================================================

EMAIL_RE  = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
EMAIL_OBFUSC_RE = re.compile(
    r"([a-zA-Z0-9._%+-]+)\s*(?:\[at\]|\(at\)|@|\bat\b)\s*"
    r"([a-zA-Z0-9.-]+)\s*(?:\[dot\]|\(dot\)|\.|\bdot\b)\s*"
    r"([a-zA-Z]{2,})", re.IGNORECASE)
DEVA_RE   = re.compile(r"[\u0900-\u097F]")   # Devanagari script = Hindi/India
ARABIC_RE = re.compile(r"[\u0600-\u06FF]")   # Arabic script

# ── Limits (configurable via /setmaxfollow, /setmaxresults) ──────────
MAX_FOLLOWING   = 500   # max accounts to harvest from each seed's following list
MAX_TOTAL       = 1000  # total result cap per scan run
MAX_RELATED     = 100   # max related profiles to discover per seed
COOKIE_TIMEOUT  = 12    # seconds per cookie request
CONCURRENCY     = 15     # concurrent profile fetches
APIFY_ACTOR     = "afanasenko/instagram-profile-scraper"
OUT_DIR         = "discovery_exports"
os.makedirs(OUT_DIR, exist_ok=True)

# ── Follower range defaults ──────────────────────────────────────────
DEFAULT_FOLLOWER_MIN = 10_000
DEFAULT_FOLLOWER_MAX = 500_000

# ── Budget tiers (flat fee) ──────────────────────────────────────────
BUDGET_TIERS = [
    (50_000,  "under_50k",  100),
    (100_000, "50k_100k",   150),
    (999_999, "100k_plus",  300),
]

# ── Location: approved country codes (phone prefix) ─────────────────
APPROVED_PHONE_CODES = {
    "+1", "+44", "+61", "+64", "+353",   # EN: US/CA, UK, AU, NZ, IE
    "+33", "+32", "+41",                  # FR: France, Belgium, Switzerland
    "+49", "+43",                         # DE: Germany, Austria
}
INDIA_PHONE_CODE = "+91"

# ── Location: text signals ───────────────────────────────────────────
INDIA_TEXT_SIGNALS = [
    "india", "bharat", "🇮🇳", "delhi", "mumbai", "bangalore", "bengaluru",
    "hyderabad", "pune", "chennai", "kolkata", "ahmedabad", "surat",
    "jaipur", "lucknow", "kanpur", "nagpur", "indore", "thane",
    "hindi", "telugu", "tamil", "kannada", "marathi", "gujarati",
    "rajasthan", "kerala", "uttar pradesh", "maharashtra", "karnataka",
]
EN_TEXT_SIGNALS = [
    "usa", "united states", "us-based", "🇺🇸", "new york", "los angeles",
    "chicago", "houston", "miami", "san francisco", "seattle", "austin",
    "united kingdom", "london", "manchester", "🇬🇧", "scotland", "england",
    "canada", "toronto", "vancouver", "montreal", "🇨🇦",
    "australia", "sydney", "melbourne", "🇦🇺",
    "new zealand", "🇳🇿", "ireland", "dublin", "🇮🇪",
]
FR_TEXT_SIGNALS = [
    "france", "paris", "🇫🇷", "french", "francais", "bordeaux", "lyon",
    "marseille", "belgium", "bruxelles", "brussels", "🇧🇪",
]
DE_TEXT_SIGNALS = [
    "germany", "deutschland", "berlin", "munich", "münchen", "hamburg",
    "frankfurt", "🇩🇪", "austria", "wien", "vienna", "🇦🇹",
    "schweiz", "zurich", "zürich", "🇨🇭",
]

# City-name fragments that appear in Instagram post location tags
INDIA_CITIES_IN_LOCATIONS = {
    "india", "delhi", "mumbai", "bangalore", "bengaluru", "hyderabad",
    "pune", "chennai", "kolkata", "ahmedabad",
}
APPROVED_CITIES_IN_LOCATIONS = {
    "new york", "los angeles", "london", "paris", "berlin", "toronto",
    "sydney", "melbourne", "chicago", "miami", "houston", "amsterdam",
    "dubai", "singapore", "manchester", "edinburgh", "dublin",
}

# ── Niche priority keywords ──────────────────────────────────────────
# Tier 1 — ecommerce (highest priority)
ECOM_KW = [
    "ecommerce", "e-commerce", "shopify", "dropship", "amazon fba",
    "amazon seller", "etsy", "woocommerce", "print on demand", "pod",
    "product research", "online store", "store owner", "dtc", "direct to consumer",
    "retail", "brand owner", "private label",
]
# Tier 2 — business / startup
BIZ_KW = [
    "entrepreneur", "founder", "startup", "business owner", "ceo",
    "agency owner", "side hustle", "passive income", "build in public",
    "solopreneur", "executive", "operator", "venture", "investing",
    "small business", "digital marketing", "growth hacker",
]
# Tier 3 — AI tool reviewers
AI_KW = [
    "ai tools", "chatgpt", "claude", "gemini", "ai review", "tech review",
    "ai automation", "ai news", "ai apps", "llm", "generative ai",
    "prompt engineering", "ai workflow",
]
# Generic reward (positive but no tier bonus)
GENERIC_REWARD_KW = [
    "build", "automation", "engineer", "tech", "no-code",
    "creator economy", "indie hacker", "saas",
]
# Penalize
PENALIZE_KW = [
    "lifestyle", "beauty", "fitness", "family", "mom", "dad",
    "travel blogger", "fashion", "food blogger", "gym", "influencer coach",
    "motivational", "spirituality", "astrology",
]

# ── Openers by niche ─────────────────────────────────────────────────
OPENER_MAP = {
    "ecommerce":   "Saw your ecommerce content -- MagicFit AI turns product URLs into UGC ads, perfect fit for your audience.",
    "shopify":     "Your Shopify content caught my eye -- we built MagicFit AI specifically for store owners like your followers.",
    "dropship":    "Your dropshipping content is exactly the niche we partner with, would love to collab.",
    "amazon":      "Loved your Amazon seller content -- MagicFit AI generates UGC ad creatives from product URLs, right in your lane.",
    "founder":     "Noticed you are building something of your own -- always love connecting with founders on a collab.",
    "startup":     "Your startup journey content is great, thought you would be a strong fit for a MagicFit AI partnership.",
    "entrepreneur":"Your entrepreneur content resonates -- think there is a strong fit for a collab on MagicFit AI.",
    "ai tools":    "Saw your AI tools content -- MagicFit AI is right in that space, would love to get you a look at it.",
    "chatgpt":     "Your AI/ChatGPT content is exactly the niche we want to be in, think there is a great fit.",
    "automation":  "Your automation content is exactly the kind of niche we love partnering with.",
    "saas":        "Your SaaS/tooling posts caught my eye, feels like a strong fit for what we do.",
    "tech":        "Your tech content is right in our target niche, would love to connect.",
    "no-code":     "Your no-code/build content is exactly the audience we want to reach.",
}
DEFAULT_OPENER = "Came across your profile and loved your content, think there is a great fit for a collab on MagicFit AI."


# =====================================================================
#  DB INIT
# =====================================================================

def init_tables():
    conn = db.get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ig_cookies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sessionid TEXT NOT NULL, csrftoken TEXT NOT NULL,
                label TEXT, active INTEGER DEFAULT 1,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);

            CREATE TABLE IF NOT EXISTS seen_profiles (
                username TEXT PRIMARY KEY,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP);

            CREATE TABLE IF NOT EXISTS discovery_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                seed_list TEXT, hops INTEGER DEFAULT 1,
                profile_count INTEGER DEFAULT 0, email_count INTEGER DEFAULT 0,
                csv_path TEXT, xlsx_path TEXT, outreach_path TEXT,
                scrape_mode TEXT DEFAULT 'hybrid');

            CREATE TABLE IF NOT EXISTS autoscans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seed_list TEXT, hops INTEGER DEFAULT 1,
                interval_hours INTEGER DEFAULT 24, active INTEGER DEFAULT 1,
                last_run TIMESTAMP, next_run TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);

            CREATE TABLE IF NOT EXISTS apify_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL, label TEXT,
                credits_used REAL DEFAULT 0, last_used TIMESTAMP,
                last_error TEXT, active INTEGER DEFAULT 1,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);

            CREATE TABLE IF NOT EXISTS stage_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER, from_stage TEXT, to_stage TEXT,
                changed_by TEXT DEFAULT 'user',
                changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, reason TEXT);

            CREATE INDEX IF NOT EXISTS idx_apify_active  ON apify_tokens(active);
            CREATE INDEX IF NOT EXISTS idx_cookie_active ON ig_cookies(active);
            CREATE INDEX IF NOT EXISTS idx_seen_user     ON seen_profiles(username);
        """)
        for col, typ in [
            ("profile_url","TEXT"), ("location","TEXT"),
            ("is_verified","INTEGER DEFAULT 0"), ("is_business","INTEGER DEFAULT 0"),
            ("post_count","INTEGER DEFAULT 0"), ("engagement_rate","REAL"),
            ("reach_ratio","REAL"), ("score_total","INTEGER DEFAULT 0"),
            ("budget_tier","TEXT"), ("estimated_cost","INTEGER"),
            ("source_seed","TEXT"), ("hop","INTEGER DEFAULT 0"),
            ("discovered_at","TIMESTAMP"), ("won_at","TIMESTAMP"),
            ("lost_at","TIMESTAMP"), ("tags","TEXT"),
            ("location_score","INTEGER DEFAULT 0"),
            ("niche_tier","TEXT"), ("niche_score","INTEGER DEFAULT 0"),
            ("recent_posts_stats", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE creators ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()


# =====================================================================
#  COOKIE CRUD
# =====================================================================

def add_cookie(sessionid, csrftoken, label=None):
    conn = db.get_db()
    try:
        if not label:
            n = conn.execute("SELECT COUNT(*) as c FROM ig_cookies").fetchone()["c"]
            label = f"session{n+1}"
        cur = conn.execute(
            "INSERT INTO ig_cookies(sessionid,csrftoken,label) VALUES(?,?,?)",
            (sessionid, csrftoken, label))
        conn.commit(); return cur.lastrowid
    finally:
        conn.close()

def list_cookies():
    conn = db.get_db()
    try:
        return conn.execute("SELECT * FROM ig_cookies WHERE active=1 ORDER BY id").fetchall()
    finally:
        conn.close()

def remove_cookie(cid):
    conn = db.get_db()
    try:
        conn.execute("DELETE FROM ig_cookies WHERE id=?", (cid,)); conn.commit()
    finally:
        conn.close()

def deactivate_cookie(cid):
    conn = db.get_db()
    try:
        conn.execute("UPDATE ig_cookies SET active=0 WHERE id=?", (cid,))
        conn.commit()
    finally:
        conn.close()

def count_cookies():
    conn = db.get_db()
    try:
        return conn.execute("SELECT COUNT(*) as c FROM ig_cookies WHERE active=1").fetchone()["c"]
    finally:
        conn.close()


# =====================================================================
#  APIFY TOKEN CRUD
# =====================================================================

def add_apify_token(token, label=None):
    conn = db.get_db()
    try:
        if not label:
            n = conn.execute("SELECT COUNT(*) as c FROM apify_tokens").fetchone()["c"]
            label = f"apify_{n+1}"
        cur = conn.execute("INSERT INTO apify_tokens(token,label) VALUES(?,?)", (token, label))
        conn.commit(); return cur.lastrowid
    finally:
        conn.close()

def list_apify_tokens():
    conn = db.get_db()
    try:
        return conn.execute("SELECT * FROM apify_tokens ORDER BY id").fetchall()
    finally:
        conn.close()

def remove_apify_token(tid):
    conn = db.get_db()
    try:
        conn.execute("DELETE FROM apify_tokens WHERE id=?", (tid,)); conn.commit()
    finally:
        conn.close()

def count_active_apify():
    conn = db.get_db()
    try:
        return conn.execute("SELECT COUNT(*) as c FROM apify_tokens WHERE active=1").fetchone()["c"]
    finally:
        conn.close()

def has_apify_available():
    return count_active_apify() > 0

def _next_apify_token():
    conn = db.get_db()
    try:
        row = conn.execute("""SELECT * FROM apify_tokens WHERE active=1
            ORDER BY last_used ASC NULLS FIRST, credits_used ASC LIMIT 1""").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def _mark_apify_used(tid, delta=0.005):
    conn = db.get_db()
    try:
        conn.execute("UPDATE apify_tokens SET last_used=?,credits_used=credits_used+? WHERE id=?",
                     (datetime.utcnow().isoformat(), delta, tid)); conn.commit()
    finally:
        conn.close()

def _mark_apify_error(tid, err):
    deact = any(w in err.lower() for w in ("quota","credit","limit","402","exceeded","403","401"))
    conn = db.get_db()
    try:
        if deact:
            conn.execute("UPDATE apify_tokens SET last_error=?,active=0 WHERE id=?", (err, tid))
        else:
            conn.execute("UPDATE apify_tokens SET last_error=? WHERE id=?", (err, tid))
        conn.commit()
    finally:
        conn.close()

def reactivate_all_apify():
    conn = db.get_db()
    try:
        cur = conn.execute("UPDATE apify_tokens SET active=1,credits_used=0,last_error=NULL")
        conn.commit(); return cur.rowcount
    finally:
        conn.close()


# =====================================================================
#  SEEN PROFILES
# =====================================================================

def mark_seen(usernames):
    if not usernames: return
    conn = db.get_db()
    try:
        now = datetime.utcnow().isoformat()
        conn.executemany("INSERT OR REPLACE INTO seen_profiles(username,last_seen) VALUES(?,?)",
                         [(u, now) for u in usernames]); conn.commit()
    finally:
        conn.close()

def get_seen():
    conn = db.get_db()
    try:
        return {r["username"] for r in conn.execute("SELECT username FROM seen_profiles").fetchall()}
    finally:
        conn.close()

def clear_seen():
    conn = db.get_db()
    try:
        cur = conn.execute("DELETE FROM seen_profiles"); conn.commit(); return cur.rowcount
    finally:
        conn.close()


# =====================================================================
#  SCAN HISTORY / AUTOSCANS
# =====================================================================

def log_scan(seeds, hops, profile_count, email_count,
             csv_path, xlsx_path=None, outreach_path=None, scrape_mode="hybrid"):
    conn = db.get_db()
    try:
        cur = conn.execute(
            """INSERT INTO discovery_scans
               (seed_list,hops,profile_count,email_count,csv_path,xlsx_path,outreach_path,scrape_mode)
               VALUES(?,?,?,?,?,?,?,?)""",
            (",".join(seeds), hops, profile_count, email_count,
             csv_path, xlsx_path or "", outreach_path or "", scrape_mode))
        conn.commit(); return cur.lastrowid
    finally:
        conn.close()

def get_scan_history(limit=10):
    conn = db.get_db()
    try:
        return conn.execute(
            "SELECT * FROM discovery_scans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()

def get_scan(scan_id):
    conn = db.get_db()
    try:
        row = conn.execute("SELECT * FROM discovery_scans WHERE id=?", (scan_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def add_autoscan(seeds, hops=1, interval_hours=24):
    from datetime import timedelta
    conn = db.get_db()
    try:
        nxt = (datetime.utcnow() + timedelta(hours=interval_hours)).isoformat()
        cur = conn.execute(
            "INSERT INTO autoscans(seed_list,hops,interval_hours,next_run) VALUES(?,?,?,?)",
            (",".join(seeds), hops, interval_hours, nxt))
        conn.commit(); return cur.lastrowid
    finally:
        conn.close()

def list_autoscans():
    conn = db.get_db()
    try:
        return conn.execute("SELECT * FROM autoscans WHERE active=1").fetchall()
    finally:
        conn.close()

def stop_autoscan(aid):
    conn = db.get_db()
    try:
        conn.execute("UPDATE autoscans SET active=0 WHERE id=?", (aid,)); conn.commit()
    finally:
        conn.close()

def get_due_autoscans():
    conn = db.get_db()
    try:
        now = datetime.utcnow().isoformat()
        return conn.execute(
            "SELECT * FROM autoscans WHERE active=1 AND (next_run IS NULL OR next_run<=?)",
            (now,)).fetchall()
    finally:
        conn.close()

def mark_autoscan_ran(aid):
    from datetime import timedelta
    conn = db.get_db()
    try:
        row = conn.execute("SELECT interval_hours FROM autoscans WHERE id=?", (aid,)).fetchone()
        if not row: return
        nxt = (datetime.utcnow() + timedelta(hours=row["interval_hours"])).isoformat()
        conn.execute("UPDATE autoscans SET last_run=?,next_run=? WHERE id=?",
                     (datetime.utcnow().isoformat(), nxt, aid)); conn.commit()
    finally:
        conn.close()


# =====================================================================
#  CONFIG
# =====================================================================

def get_cfg():
    return {
        "follower_min":      int(db.get_setting("disc_follower_min", str(DEFAULT_FOLLOWER_MIN))),
        "follower_max":      int(db.get_setting("disc_follower_max", str(DEFAULT_FOLLOWER_MAX))),
        "reward_keywords":   json.loads(db.get_setting("disc_reward_kw",   json.dumps(GENERIC_REWARD_KW))),
        "penalize_keywords": json.loads(db.get_setting("disc_penalize_kw", json.dumps(PENALIZE_KW))),
        "min_reach_ratio":   float(db.get_setting("disc_min_reach",   "0.8")),
        "min_engagement":    float(db.get_setting("disc_min_eng",     "3.0")),
        "scrape_mode":       db.get_setting("disc_scrape_mode", "hybrid"),
        "location_filter":   db.get_setting("disc_location_filter", "on") == "on",
        "max_following":     int(db.get_setting("disc_max_following", str(MAX_FOLLOWING))),
        "max_total":         int(db.get_setting("disc_max_total",     str(MAX_TOTAL))),
    }

def save_cfg(cfg):
    db.set_setting("disc_follower_min",    str(cfg["follower_min"]))
    db.set_setting("disc_follower_max",    str(cfg["follower_max"]))
    db.set_setting("disc_reward_kw",       json.dumps(cfg.get("reward_keywords",   GENERIC_REWARD_KW)))
    db.set_setting("disc_penalize_kw",     json.dumps(cfg.get("penalize_keywords", PENALIZE_KW)))
    db.set_setting("disc_min_reach",       str(cfg.get("min_reach_ratio", 0.8)))
    db.set_setting("disc_min_eng",         str(cfg.get("min_engagement",  3.0)))
    db.set_setting("disc_location_filter", "on" if cfg.get("location_filter", True) else "off")
    if "max_following" in cfg:
        db.set_setting("disc_max_following", str(cfg["max_following"]))
    if "max_total" in cfg:
        db.set_setting("disc_max_total", str(cfg["max_total"]))

def set_scrape_mode(mode):
    if mode not in ("cookie", "apify", "hybrid"):
        raise ValueError(f"Invalid mode: {mode}")
    db.set_setting("disc_scrape_mode", mode)

def set_max_following(n: int):
    db.set_setting("disc_max_following", str(max(50, min(50000, n))))

def set_max_total(n: int):
    db.set_setting("disc_max_total", str(max(50, min(50000, n))))


# =====================================================================
#  DATA MODEL
# =====================================================================

@dataclass
class CreatorProfile:
    username:        str
    full_name:       str
    followers:       int
    following:       int
    bio:             str
    email:           str
    category:        str
    is_verified:     bool
    is_business:     bool
    post_count:      int
    engagement_rate: float
    reach_ratio:     float
    external_url:    str
    profile_url:     str
    bio_score:       int
    cohort:          str
    hop:             int
    source_seed:     str
    # New detailed fields
    niche_tier:      str  = ""    # "ecommerce" | "business" | "ai" | "general"
    niche_score:     int  = 0
    location_score:  int  = 0
    location_hint:   str  = ""    # human-readable location signal
    budget_tier:     str  = ""
    estimated_cost:  int  = 0
    filter_reason:   str  = ""    # why it passed or failed (debug)
    recent_posts_stats: str = ""


# =====================================================================
#  LOCATION FILTER  (the 50%+ non-India rule)
# =====================================================================

def _extract_post_location_names(data: dict) -> List[str]:
    """Pull location tag names from the last 12 posts."""
    names = []
    for edge in data.get("edge_owner_to_timeline_media", {}).get("edges", [])[:12]:
        loc = edge.get("node", {}).get("location")
        if loc and loc.get("name"):
            names.append(loc["name"].lower())
    return names

def score_location(data: dict) -> Tuple[int, str]:
    """
    Score a profile's likely audience location.
    Returns (score, human_hint).

    Scoring:
      +25  phone country code is approved (non-India, approved market)
      -30  phone country code is +91 (India) -- near-certain disqualify
      +15  bio/location text has strong EN/FR/DE signal
      +10  post location tags in approved cities
      -20  bio/location text has India signals
      -15  bio contains Devanagari script
      -20  post locations predominantly India cities

    A total score < 0 = likely India-dominant = DISQUALIFY.
    """
    score = 0
    hints = []

    bio      = (data.get("biography") or "").lower()
    city     = (data.get("city_name")  or "").lower()
    location = (data.get("location")   or city).lower()
    text     = bio + " " + location

    # Phone country code (most reliable signal -- business accounts)
    phone_code = data.get("public_phone_country_code") or ""
    if phone_code:
        if phone_code == INDIA_PHONE_CODE:
            score -= 30
            hints.append(f"phone:{phone_code}=IN")
        elif phone_code in APPROVED_PHONE_CODES:
            score += 25
            hints.append(f"phone:{phone_code}=approved")
        else:
            score -= 5   # unknown country, slight negative
            hints.append(f"phone:{phone_code}=unknown")

    # Devanagari in bio (Hindi/Marathi/etc. = India)
    if DEVA_RE.search(data.get("biography") or ""):
        score -= 15
        hints.append("devanagari-in-bio")

    # India text signals
    india_hits = sum(1 for s in INDIA_TEXT_SIGNALS if s in text)
    if india_hits >= 2:
        score -= 20
        hints.append(f"india-text-x{india_hits}")
    elif india_hits == 1:
        score -= 10
        hints.append("india-text-x1")

    # English country/city signals
    en_hits = sum(1 for s in EN_TEXT_SIGNALS if s in text)
    if en_hits >= 2:
        score += 15
        hints.append(f"en-x{en_hits}")
    elif en_hits == 1:
        score += 8
        hints.append("en-x1")

    # French/German signals
    fr_hits = sum(1 for s in FR_TEXT_SIGNALS if s in text)
    de_hits = sum(1 for s in DE_TEXT_SIGNALS if s in text)
    if fr_hits >= 1:
        score += 10
        hints.append(f"fr-x{fr_hits}")
    if de_hits >= 1:
        score += 10
        hints.append(f"de-x{de_hits}")

    # Post location tags
    post_locs = _extract_post_location_names(data)
    if post_locs:
        india_loc  = sum(1 for n in post_locs if any(c in n for c in INDIA_CITIES_IN_LOCATIONS))
        approved_loc = sum(1 for n in post_locs if any(c in n for c in APPROVED_CITIES_IN_LOCATIONS))
        total_loc  = len(post_locs)
        if total_loc >= 3:
            if india_loc / total_loc >= 0.5:
                score -= 20
                hints.append(f"posts:{india_loc}/{total_loc} india cities")
            elif approved_loc / total_loc >= 0.5:
                score += 10
                hints.append(f"posts:{approved_loc}/{total_loc} approved cities")

    hint = " | ".join(hints) if hints else "no-location-signals"
    return score, hint


# =====================================================================
#  NICHE PRIORITY SCORING
# =====================================================================

def score_niche(bio: str, category: str) -> Tuple[int, str]:
    """
    Score the creator's niche against our priority order:
    ecommerce > business/startup > ai tools > general

    Returns (niche_score, tier_name).
    """
    b = (bio or "").lower() + " " + (category or "").lower()

    ecom_hits = sum(1 for kw in ECOM_KW if kw in b)
    biz_hits  = sum(1 for kw in BIZ_KW  if kw in b)
    ai_hits   = sum(1 for kw in AI_KW   if kw in b)
    gen_hits  = sum(1 for kw in GENERIC_REWARD_KW if kw in b)
    pen_hits  = sum(1 for kw in PENALIZE_KW if kw in b)

    score = 0
    tier  = "general"

    if ecom_hits > 0:
        score += min(ecom_hits, 3) * 12  # up to +36
        tier   = "ecommerce"
    elif biz_hits > 0:
        score += min(biz_hits, 3) * 8   # up to +24
        tier   = "business"
    elif ai_hits > 0:
        score += min(ai_hits, 3) * 6    # up to +18
        tier   = "ai"

    score += min(gen_hits, 3) * 3       # generic bonus up to +9
    score -= pen_hits * 4               # penalize lifestyle etc.

    return score, tier


# =====================================================================
#  ENGAGEMENT / REACH FILTER
# =====================================================================

def extract_recent_posts_stats(data: dict) -> str:
    """Extract individual metrics for the last 12 non-pinned posts/videos."""
    edges = data.get("edge_owner_to_timeline_media", {}).get("edges", [])
    non_pinned = []
    for e in edges:
        n = e.get("node", {})
        if n.get("is_pinned") or n.get("is_pinned_by_owner") or n.get("is_pinned_by_creator"):
            continue
        non_pinned.append(e)
        
    stats_list = []
    for e in non_pinned[:12]:
        n = e.get("node", {})
        likes = (n.get("edge_liked_by", {}).get("count", 0) 
                 or n.get("edge_media_preview_like", {}).get("count", 0) or 0)
        comments = n.get("edge_media_to_comment", {}).get("count", 0) or 0
        views = n.get("video_view_count") or n.get("video_play_count") or 0
        
        caption_edges = n.get("edge_media_to_caption", {}).get("edges", [])
        caption = ""
        if caption_edges:
            caption = caption_edges[0].get("node", {}).get("text", "") or ""
            
        stats_list.append({
            "views": int(views),
            "likes": int(likes),
            "comments": int(comments),
            "caption": caption[:80].replace("\n", " ") # Keep caption clean and short
        })
    import json
    return json.dumps(stats_list)

def compute_engagement(data: dict, followers: int) -> float:
    if followers <= 0: return 0.0
    edges = data.get("edge_owner_to_timeline_media", {}).get("edges", [])
    non_pinned = [e for e in edges if not (e.get("node", {}).get("is_pinned") or e.get("node", {}).get("is_pinned_by_owner") or e.get("node", {}).get("is_pinned_by_creator"))]
    if not non_pinned: return 0.0
    tots = []
    for e in non_pinned[:12]:
        n = e.get("node", {})
        l = (n.get("edge_liked_by", {}).get("count", 0)
             or n.get("edge_media_preview_like", {}).get("count", 0))
        c = n.get("edge_media_to_comment", {}).get("count", 0)
        tots.append(l + c)
    if not tots: return 0.0
    return round(sum(tots) / len(tots) / followers * 100, 2)

def compute_reach_ratio(data: dict, followers: int) -> Optional[float]:
    """Avg video views / followers across last 12 non-pinned posts. None if no video data."""
    if followers <= 0: return None
    edges = data.get("edge_owner_to_timeline_media", {}).get("edges", [])
    non_pinned = [e for e in edges if not (e.get("node", {}).get("is_pinned") or e.get("node", {}).get("is_pinned_by_owner") or e.get("node", {}).get("is_pinned_by_creator"))]
    if not non_pinned: return None
    views = []
    for e in non_pinned[:12]:
        n = e.get("node", {})
        vv = n.get("video_view_count") or n.get("video_play_count")
        if vv is not None:
            views.append(vv)
    if not views: return None
    return round(sum(views) / len(views) / followers, 3)

def estimate_budget(followers: int, engagement: float = 0) -> Tuple[str, int]:
    for cap, tier, base in BUDGET_TIERS:
        if followers <= cap:
            if   engagement >= 5.0: return tier, int(base * 1.5)
            elif engagement >= 3.0: return tier, int(base * 1.2)
            else:                   return tier, base
    return "100k_plus", 300

def assign_cohort(followers: int, niche_score: int, location_score: int) -> str:
    total = niche_score + (5 if location_score > 0 else 0)
    if followers > 100_000 and total >= 15:  return "A"
    if 30_000 <= followers <= 100_000 and total >= 5: return "B"
    if total < 0: return "SKIP"
    return "C"


# =====================================================================
#  MASTER FILTER
# =====================================================================

async def filter_profile(data: dict, seed: str, hop: int, cfg: dict) -> Optional[CreatorProfile]:
    """
    Apply all filters in order. Returns CreatorProfile or None.

    Order (fail-fast):
      1. Followers in range
      2. Location (non-India, approved market)  [hard filter]
      3. Engagement / reach                      [hard filter]
      4. Niche score (soft -- affects cohort but doesn't disqualify)
    """
    followers = data.get("edge_followed_by", {}).get("count", 0)
    fmin = cfg.get("follower_min", DEFAULT_FOLLOWER_MIN)
    fmax = cfg.get("follower_max", DEFAULT_FOLLOWER_MAX)

    # ── 1. Follower range ─────────────────────────────────────────────
    if not (fmin <= followers <= fmax):
        return None

    bio      = data.get("biography", "") or ""
    category = data.get("category_name", "") or data.get("business_category_name", "") or ""

    # ── 2. Location filter ────────────────────────────────────────────
    loc_score, loc_hint = score_location(data)
    if cfg.get("location_filter", True) and loc_score < 0:
        # India-dominant signal: skip
        return None

    # ── 3. Engagement / reach filter ─────────────────────────────────
    eng   = compute_engagement(data, followers)
    reach = compute_reach_ratio(data, followers)

    min_reach = cfg.get("min_reach_ratio", 0.8)
    min_eng   = cfg.get("min_engagement", 3.0)

    if reach is not None:
        # Has video data -- enforce reach ratio strictly
        if reach < min_reach:
            return None
    else:
        # Photo-only account -- enforce engagement rate instead
        if eng < min_eng:
            return None

    # ── 4. Niche scoring (soft) ───────────────────────────────────────
    niche_score, niche_tier = score_niche(bio, category)
    # Still accept general/unknown niche if other signals are strong
    bio_score = niche_score

    cohort = assign_cohort(followers, niche_score, loc_score)
    if cohort == "SKIP":
        return None

    # Email (enhanced — catches obfuscated patterns like "name [at] gmail [dot] com")
    email = await _extract_email_enhanced(
        bio, data.get("public_email") or "", data.get("external_url") or ""
    )

    tier, cost = estimate_budget(followers, eng)
    username   = data.get("username", "")
    posts_stats = extract_recent_posts_stats(data)

    return CreatorProfile(
        username        = username,
        full_name       = data.get("full_name", "") or "",
        followers       = followers,
        following       = data.get("edge_follow", {}).get("count", 0),
        bio             = bio,
        email           = email,
        category        = category,
        is_verified     = bool(data.get("is_verified", False)),
        is_business     = bool(data.get("is_business_account", False)),
        post_count      = data.get("edge_owner_to_timeline_media", {}).get("count", 0),
        engagement_rate = eng,
        reach_ratio     = reach if reach is not None else -1.0,
        external_url    = data.get("external_url", "") or "",
        profile_url     = f"https://instagram.com/{username}",
        bio_score       = bio_score,
        cohort          = cohort,
        hop             = hop,
        source_seed     = seed,
        niche_tier      = niche_tier,
        niche_score     = niche_score,
        location_score  = loc_score,
        location_hint   = loc_hint,
        budget_tier     = tier,
        estimated_cost  = cost,
        recent_posts_stats = posts_stats,
    )


# =====================================================================
#  SMART COOKIE POOL  (per-cookie health tracking)
# =====================================================================

class SmartCookiePool:
    """Cookie pool with per-cookie health tracking, auto-quarantine, and recovery."""

    UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.5 Mobile/15E148 Instagram 302.0.0.23.114"
    )

    def __init__(self, rows):
        self.cookies = []
        for r in rows:
            r_dict = dict(r)
            self.cookies.append({
                "id": r_dict["id"], "sessionid": r_dict["sessionid"],
                "csrftoken": r_dict["csrftoken"], "label": r_dict.get("label", ""),
                "consecutive_fails": 0, "last_success": 0.0,
                "quarantined_until": 0.0, "delay": 4.5,
                "total_ok": 0, "total_429": 0,
            })
        self._idx = 0

    def _is_available(self, c) -> bool:
        return time.time() >= c["quarantined_until"]

    def deactivate(self, c):
        c["quarantined_until"] = float('inf')
        try:
            deactivate_cookie(c["id"])
        except Exception as e:
            log.error(f"failed to deactivate cookie in DB: {e}")

    def next_session(self) -> tuple:
        """Returns (requests.Session, cookie_info_dict) for the healthiest available active cookie."""
        now = time.time()
        active_cookies = [c for c in self.cookies if c["quarantined_until"] != float('inf')]
        if not active_cookies:
            return requests.Session(), {"id": 0, "sessionid": "", "csrftoken": "", "delay": 0.0, "quarantined_until": float('inf'), "label": "dummy"}

        available = [c for c in active_cookies if self._is_available(c)]
        if not available:
            # All quarantined — pick the one with the soonest recovery
            available = sorted(active_cookies, key=lambda c: c["quarantined_until"])
        # Pick cookie with fewest consecutive failures, then least recently used
        available.sort(key=lambda c: (c["consecutive_fails"], -c["last_success"]))
        c = available[0]

        s = requests.Session()
        s.headers.update({
            "User-Agent":      self.UA,
            "Accept":          "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "X-IG-App-ID":    "936619743392459",
            "Referer":         "https://www.instagram.com/",
            "Origin":          "https://www.instagram.com",
        })
        s.cookies.set("sessionid", c["sessionid"], domain=".instagram.com")
        s.cookies.set("csrftoken", c["csrftoken"],  domain=".instagram.com")
        s.headers["X-CSRFToken"] = c["csrftoken"]
        return s, c

    def mark_success(self, c):
        c["consecutive_fails"] = 0
        c["last_success"] = time.time()
        c["total_ok"] += 1
        c["delay"] = max(1.0, c["delay"] * 0.92)

    def mark_rate_limited(self, c):
        c["consecutive_fails"] += 1
        c["total_429"] += 1
        c["delay"] = min(c["delay"] * 2.0, 60.0)
        if c["consecutive_fails"] >= 3:
            c["quarantined_until"] = time.time() + 300  # 5 min quarantine
            log.warning(f"Cookie #{c['id']} ({c['label']}) quarantined for 5min "
                        f"(3 consecutive 429s)")

    def mark_error(self, c):
        c["consecutive_fails"] += 1

    async def wait_for(self, c):
        """Async delay calibrated per-cookie."""
        await asyncio.sleep(c["delay"] + random.uniform(0.2, 0.8))

    @property
    def healthy_count(self):
        now = time.time()
        return sum(1 for c in self.cookies if now >= c["quarantined_until"] and c["quarantined_until"] != float('inf'))

    def stats_str(self):
        ok = sum(c["total_ok"] for c in self.cookies)
        bad = sum(c["total_429"] for c in self.cookies)
        active_count = sum(1 for c in self.cookies if c["quarantined_until"] != float('inf'))
        return f"C:{ok}ok/{bad}err h:{self.healthy_count}/{active_count}"


# =====================================================================
#  COOKIE FETCHERS (enhanced with fallbacks)
# =====================================================================

def _fetch_profile_cookie(session: requests.Session, username: str) -> Tuple[Optional[dict], int]:
    """Fetch a single profile via web_profile_info. Returns (user_dict, http_status)."""
    url = (f"https://www.instagram.com/api/v1/users/web_profile_info/"
           f"?username={username}")
    try:
        resp = session.get(url, timeout=COOKIE_TIMEOUT)
        if resp.status_code == 200:
            user = resp.json().get("data", {}).get("user")
            return user, 200
        return None, resp.status_code
    except requests.exceptions.Timeout:
        return None, 0
    except Exception as e:
        log.warning(f"cookie profile error @{username}: {e}")
        return None, 0


def _fetch_following_cookie(session: requests.Session, user_id: str,
                             max_count: int = MAX_FOLLOWING, prog_cb: Callable = None) -> Tuple[List[str], int]:
    """Fetch the following list of user_id via the friendships API. Returns (list of usernames, status_code)."""
    usernames = []
    next_max_id = None
    url = f"https://www.instagram.com/api/v1/friendships/{user_id}/following/"
    status_code = 200

    while len(usernames) < max_count:
        if prog_cb:
            prog_cb(len(usernames), max_count)
        params = {"count": min(50, max_count - len(usernames))}
        if next_max_id:
            params["max_id"] = next_max_id
        try:
            resp = session.get(url, params=params, timeout=COOKIE_TIMEOUT)
            status_code = resp.status_code
            if resp.status_code != 200:
                log.warning(f"following list HTTP {resp.status_code} for uid {user_id}")
                break
            body = resp.json()
            batch = body.get("users", [])
            if not batch:
                break
            for u in batch:
                uname = u.get("username", "")
                if uname:
                    usernames.append(uname)
            if not body.get("next_max_id"):
                break
            next_max_id = body["next_max_id"]
            time.sleep(random.uniform(0.5, 1.5))  # gentle paging delay
        except Exception as e:
            log.warning(f"following list error uid {user_id}: {e}")
            status_code = 0
            break

    if prog_cb:
        prog_cb(len(usernames), max_count)
    return usernames, status_code


def _fetch_related_profiles_cookie(session: requests.Session, user_id: str) -> List[dict]:
    """
    Fetch Instagram's "Suggested for You" / chained profiles for a user.
    Returns list of raw user dicts (partial profile data with username, id,
    full_name, profile_pic_url, is_verified, etc.).
    These are algorithmically curated lookalikes — the gold standard for prospecting.
    """
    url = f"https://www.instagram.com/api/v1/discover/chained_profiles/?target_id={user_id}"
    try:
        resp = session.get(url, timeout=COOKIE_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            profiles = data.get("profiles", []) or data.get("users", [])
            if not profiles:
                # Try alternate response shape
                profiles = data.get("chained_profiles", [])
            return profiles
        log.debug(f"related profiles HTTP {resp.status_code} for uid {user_id}")
        return []
    except Exception as e:
        log.warning(f"related profiles error uid {user_id}: {e}")
        return []


def _fetch_profile_web_public(username: str) -> Tuple[Optional[dict], int]:
    """
    Cookie-free fallback: fetch public profile data from instagram.com/{username}
    by parsing the embedded JSON in the page source.
    Works for public profiles only. No authentication required.
    """
    url = f"https://www.instagram.com/{username}/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return None, resp.status_code

        text = resp.text

        # Try to find embedded JSON data (multiple patterns Instagram has used)
        for pattern in [
            r'window\._sharedData\s*=\s*({.+?});</script>',
            r'window\.__additionalDataLoaded\s*\([^,]+,\s*({.+?})\)\s*;',
            r'"ProfilePage"\s*:\s*\[({.+?})\]',
        ]:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                try:
                    blob = json.loads(m.group(1))
                    # Navigate to user data depending on structure
                    user = None
                    if "entry_data" in blob:
                        pages = blob["entry_data"].get("ProfilePage", [])
                        if pages:
                            user = pages[0].get("graphql", {}).get("user")
                    elif "graphql" in blob:
                        user = blob["graphql"].get("user")
                    elif "user" in blob:
                        user = blob["user"]
                    if user:
                        return user, 200
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

        return None, 0
    except Exception as e:
        log.debug(f"web public profile error @{username}: {e}")
        return None, 0


async def _fetch_external_url_email(external_url: str) -> str:
    """
    Attempt to extract an email from a profile's external URL
    (Linktree, personal website, etc.).
    Returns email string or empty string. (Async using httpx).
    """
    if not external_url:
        return ""
    import httpx
    try:
        # Use a short timeout of 4 seconds so we don't hang the thread/loop
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            }
            resp = await client.get(external_url, headers=headers)
            if resp.status_code != 200:
                return ""
            text = resp.text[:50000]  # Cap parsing to 50KB
            # Check for mailto: links first (most reliable)
            mailto = re.search(r'mailto:([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', text)
            if mailto:
                return mailto.group(1).lower()
            # Fall back to generic email regex in page text
            clean = re.sub(r'<[^>]+>', ' ', text)
            emails = EMAIL_RE.findall(clean)
            # Filter out common false positives
            blocked = {"example.com", "email.com", "domain.com", "yoursite.com",
                       "wix.com", "sentry.io", "google.com", "facebook.com",
                       "w3.org", "schema.org", "purl.org", "xmlns.com"}
            for em in emails:
                domain = em.split("@")[1].lower()
                if domain not in blocked and not domain.endswith(".png") and not domain.endswith(".js"):
                    return em.lower()
            return ""
    except Exception:
        return ""


async def _extract_email_enhanced(bio: str, public_email: str, external_url: str) -> str:
    """Enhanced email extraction: public_email > bio regex > bio obfuscated > external URL."""
    if public_email:
        return public_email
    if bio:
        # Standard regex
        m = EMAIL_RE.search(bio)
        if m:
            return m.group(0)
        # Obfuscated pattern (name [at] gmail [dot] com)
        m = EMAIL_OBFUSC_RE.search(bio)
        if m:
            return f"{m.group(1)}@{m.group(2)}.{m.group(3)}".lower()
    # External URL scrape (slow, only if no email found)
    if external_url:
        return await _fetch_external_url_email(external_url)
    return ""


# =====================================================================
#  APIFY BACKEND (afanasenko/instagram-profile-scraper)
# =====================================================================

def _apify_run_sync(token: str, run_input: dict, timeout: int = 120) -> List[dict]:
    """Blocking Apify call. Must be called via run_in_executor."""
    if not _HAVE_APIFY_CLIENT:
        raise ImportError("apify-client not installed. pip install apify-client --break-system-packages")
    client = ApifyClient(token)
    
    if run_input.get("operationMode") == "analyzeProfile":
        actor_name = "apify/instagram-profile-scraper"
        new_input = {"usernames": [run_input["username"]]}
        run = client.actor(actor_name).call(run_input=new_input)
    else:
        actor_name = APIFY_ACTOR
        run = client.actor(actor_name).call(run_input=run_input)
        
    if not run:
        return []
    
    if hasattr(run, "default_dataset_id"):
        ds_id = run.default_dataset_id
    elif hasattr(run, "get"):
        ds_id = run.get("defaultDatasetId") or run.get("default_dataset_id")
    else:
        ds_id = run["defaultDatasetId"]
        
    return list(client.dataset(ds_id).iterate_items())


def _normalize_apify(raw: dict) -> dict:
    """Map afanasenko actor output to web_profile_info shape."""
    bio    = raw.get("biography") or raw.get("bio") or ""
    edges  = []
    for post in (raw.get("latestPosts") or [])[:24]:
        node = {
            "edge_liked_by":         {"count": post.get("likesCount",    0) or 0},
            "edge_media_to_comment": {"count": post.get("commentsCount", 0) or 0},
            "is_pinned":             bool(post.get("isPinned") or post.get("is_pinned") or False),
            "edge_media_to_caption": {"edges": [{"node": {"text": post.get("caption", "") or ""}}]}
        }
        vv = post.get("videoViewCount") or post.get("videoPlayCount")
        if vv is not None:
            node["video_view_count"] = vv
        # Location from post
        loc = post.get("locationName") or post.get("location_name") or ""
        if loc:
            node["location"] = {"name": loc}
        edges.append({"node": node})

    return {
        "id":                       str(raw.get("id") or ""),
        "username":                 raw.get("username", ""),
        "full_name":                raw.get("fullName") or raw.get("full_name") or "",
        "biography":                bio,
        "public_email":             raw.get("businessEmail") or raw.get("publicEmail") or "",
        "public_phone_country_code": raw.get("businessPhoneCountryCode") or "",
        "city_name":                raw.get("cityName") or raw.get("city_name") or "",
        "external_url":             raw.get("externalUrl") or "",
        "category_name":            raw.get("businessCategoryName") or raw.get("categoryName") or "",
        "is_verified":              bool(raw.get("verified") or raw.get("isVerified")),
        "is_business_account":      bool(raw.get("isBusinessAccount")),
        "edge_followed_by":         {"count": raw.get("followersCount") or 0},
        "edge_follow":              {"count": raw.get("followsCount")   or 0},
        "edge_owner_to_timeline_media": {
            "count": raw.get("postsCount") or 0,
            "edges": edges,
        },
    }


# =====================================================================
#  PIPELINE  (multi-strategy, concurrent, fault-tolerant)
# =====================================================================

cancel_flag = False

def stop_scrape():
    global cancel_flag
    cancel_flag = True


class ScanStats:
    """Track scan progress for structured reporting."""
    def __init__(self):
        self.related_found = 0
        self.following_found = 0
        self.profiles_scored = 0
        self.passed = 0
        self.cohorts: Dict[str, int] = {}
        self.rejected_followers = 0
        self.rejected_location = 0
        self.rejected_reach = 0
        self.emails_found = 0
        self.c_hits = 0
        self.a_hits = 0
        self.web_hits = 0
        self.ext_emails = 0

    def summary(self, mode: str) -> str:
        lines = [
            f"Seeds processed | Mode: {mode}",
            f"├─ Related profiles found: {self.related_found}",
            f"├─ Following list fetched: {self.following_found}",
            f"├─ Profiles scored: {self.profiles_scored}",
            f"├─ Passed filters: {self.passed}",
        ]
        for k in sorted(self.cohorts.keys()):
            lines.append(f"│  ├─ {k}-tier: {self.cohorts[k]}")
        lines.extend([
            f"├─ Rejected: {self.rejected_followers + self.rejected_location + self.rejected_reach}",
            f"│  ├─ Followers range: {self.rejected_followers}",
            f"│  ├─ Location filter: {self.rejected_location}",
            f"│  └─ Low engagement: {self.rejected_reach}",
            f"├─ Emails found: {self.emails_found} ({int(100*self.emails_found/max(1,self.passed))}%)",
        ])
        if self.ext_emails:
            lines.append(f"│  └─ From external URLs: {self.ext_emails}")
        lines.append(f"└─ Backend: C:{self.c_hits} A:{self.a_hits} Web:{self.web_hits}")
        return "\n".join(lines)


async def _fetch_profile_with_fallback(
    uname: str, pool: SmartCookiePool, has_cookies: bool, has_apify: bool,
    mode: str, loop, stats: ScanStats, semaphore: asyncio.Semaphore
) -> Optional[dict]:
    """
    Fetch a single profile with 3-tier cascading fallback:
    1. Cookie (web_profile_info) — fastest
    2. Public web scrape (no auth) — fallback for public accounts
    3. Apify single-profile fetch — costs credits but always works
    """
    profile_data = None

    async with semaphore:
        # Tier 1: Cookie
        if has_cookies and mode in ("cookie", "hybrid") and pool.healthy_count > 0:
            session, cookie = pool.next_session()
            await pool.wait_for(cookie)
            profile_data, status = await loop.run_in_executor(
                None, _fetch_profile_cookie, session, uname)

            if status == 429:
                pool.mark_rate_limited(cookie)
            elif status in (401, 403):
                pool.deactivate(cookie)
                log.warning(f"Cookie #{cookie['id']} ({cookie['label']}) expired (HTTP {status}). Deactivated.")
            elif profile_data:
                pool.mark_success(cookie)
                stats.c_hits += 1
                return profile_data
            else:
                pool.mark_error(cookie)

        # Tier 2: Public web scrape (free, no auth)
        if not profile_data:
            profile_data, status = await loop.run_in_executor(
                None, _fetch_profile_web_public, uname)
            if profile_data:
                stats.web_hits += 1
                return profile_data

        # Tier 3: Apify single profile (costs credits)
        if not profile_data and has_apify and (mode in ("apify", "hybrid") or (mode == "cookie" and pool.healthy_count == 0)):
            tok = _next_apify_token()
            if tok:
                try:
                    items = await loop.run_in_executor(
                        None, _apify_run_sync, tok["token"], {
                            "username":      uname,
                            "operationMode": "analyzeProfile",
                        }, 60)
                    if items:
                        profile_data = _normalize_apify(items[0])
                        _mark_apify_used(tok["id"])
                        stats.a_hits += 1
                        return profile_data
                except Exception as e:
                    _mark_apify_error(tok["id"], str(e))

    return profile_data


async def run_pipeline(seeds: list, cfg: dict, hops: int,
                       skip_seen: bool, progress_cb: Callable) -> List[CreatorProfile]:
    """
    Master pipeline — multi-strategy, concurrent, fault-tolerant.

    Strategies per seed (run in parallel where possible):
      1. Related/Suggested Profiles (discover/chained_profiles) — niche-matched lookalikes
      2. Following List (friendships API) — who they follow
      3. Apify (analyzeFollowersFollowing) — fallback/supplement

    Each discovered username is then scored through the filter pipeline
    with concurrent processing (up to CONCURRENCY profiles at once).
    """
    global cancel_flag
    cancel_flag = False

    cookie_rows  = list_cookies()
    pool         = SmartCookiePool(cookie_rows)
    seen_before  = get_seen() if skip_seen else set()
    mode         = cfg.get("scrape_mode", "hybrid")
    _has_cookies = bool(cookie_rows)
    _has_apify   = has_apify_available()
    max_follow   = cfg.get("max_following", MAX_FOLLOWING)
    max_total    = cfg.get("max_total", MAX_TOTAL)

    if not _has_cookies and not _has_apify:
        await progress_cb("No backend ready. Add /addcookie or /addapify first.")
        return []

    if mode == "cookie" and not _has_cookies: mode = "apify"
    elif mode == "apify" and not _has_apify:  mode = "cookie"
    elif mode == "hybrid" and not _has_cookies: mode = "apify"
    elif mode == "hybrid" and not _has_apify:   mode = "cookie"

    loop = asyncio.get_event_loop()
    results: List[CreatorProfile] = []
    visited: Set[str] = set()
    stats = ScanStats()
    semaphore = asyncio.Semaphore(CONCURRENCY)

    await progress_cb(
        f"[{mode}] {len(seeds)} seeds | max_follow={max_follow} max_results={max_total}\n"
        f"filters: loc={cfg.get('location_filter',True)} "
        f"reach={cfg.get('min_reach_ratio',0.8)}x eng={cfg.get('min_engagement',3.0)}%\n"
        f"backends: {len(cookie_rows)} cookies, "
        f"{count_active_apify()} apify tokens"
    )

    for seed_idx, seed_raw in enumerate(seeds):
        if cancel_flag or len(results) >= max_total:
            break

        seed = seed_raw.strip().lstrip("@")
        await progress_cb(f"━━ Seed {seed_idx+1}/{len(seeds)}: @{seed} ━━")

        # ── A. Get seed profile + user_id ────────────────────────────
        seed_data = None
        seed_user_id = None
        web_status = "N/A"
        cookie_status = "N/A"
        apify_error = "N/A"

        if _has_cookies and mode in ("cookie", "hybrid"):
            await progress_cb(f"[█████░░░░░░░░░░░░░░░] @{seed} fetching profile (cookie)...")
            session, cookie = pool.next_session()
            await pool.wait_for(cookie)
            seed_data, cookie_status = await loop.run_in_executor(
                None, _fetch_profile_cookie, session, seed)
            if cookie_status == 429:
                pool.mark_rate_limited(cookie)
                await progress_cb(f"@{seed}: cookie rate limited, trying fallbacks...")
            elif cookie_status in (401, 403):
                pool.deactivate(cookie)
                await progress_cb(f"Cookie #{cookie['id']} ({cookie['label']}) expired (HTTP {cookie_status}). Deactivated.")
            elif seed_data:
                pool.mark_success(cookie)
                stats.c_hits += 1
                seed_user_id = seed_data.get("id", "")
            else:
                pool.mark_error(cookie)
                await progress_cb(f"@{seed}: cookie fetch failed (HTTP {cookie_status})")

        if not seed_data and not seed_user_id:
            await progress_cb(f"[░░░░░░░░░░░░░░░░░░░░] @{seed} fetching profile (web)...")
            # Fallback: public web scrape for seed
            seed_data, web_status = await loop.run_in_executor(
                None, _fetch_profile_web_public, seed)
            if seed_data:
                stats.web_hits += 1
                seed_user_id = seed_data.get("id", "")
            else:
                await progress_cb(f"@{seed} web public fetch failed (status: {web_status})")

        if not seed_user_id and _has_apify:
            await progress_cb(f"[██████████░░░░░░░░░░] @{seed} fetching profile (apify)...")
            # Fallback: Apify for seed profile
            tok = _next_apify_token()
            if tok:
                try:
                    items = await loop.run_in_executor(
                        None, _apify_run_sync, tok["token"], {
                            "username": seed,
                            "operationMode": "analyzeProfile",
                        }, 60)
                    if items:
                        seed_data = _normalize_apify(items[0])
                        _mark_apify_used(tok["id"])
                        stats.a_hits += 1
                        seed_user_id = seed_data.get("id", "")
                    else:
                        apify_error = "No items returned"
                        await progress_cb(f"@{seed} apify fetch failed (no items returned)")
                except Exception as e:
                    apify_error = str(e)
                    await progress_cb(f"@{seed} apify fetch failed: {str(e)[:100]}")
                    _mark_apify_error(tok["id"], str(e))
            else:
                await progress_cb(f"@{seed} apify fetch failed: no active apify tokens")

        if not seed_user_id:
            await progress_cb(f"@{seed}: could not resolve profile. (Web={web_status}, Cookie={cookie_status}, Apify={str(apify_error)[:40]}). Skipping.")
            continue

        # ── B. Multi-strategy expansion ──────────────────────────────
        discovery_usernames: List[str] = []
        apify_profiles: Dict[str, dict] = {}

        if hops == 0:
            discovery_usernames = [seed]
            if seed_data:
                apify_profiles[seed] = seed_data

        # Strategy 1: Related/Suggested Profiles (best quality)
        if hops > 0 and _has_cookies and mode in ("cookie", "hybrid") and pool.healthy_count > 0:
            session2, cookie2 = pool.next_session()
            await pool.wait_for(cookie2)
            related_raw = await loop.run_in_executor(
                None, _fetch_related_profiles_cookie, session2, seed_user_id)

            if related_raw:
                pool.mark_success(cookie2)
                for rp in related_raw:
                    uname = rp.get("username", "")
                    if uname and uname not in visited and uname not in seen_before:
                        discovery_usernames.append(uname)
                stats.related_found += len(related_raw)
                await progress_cb(
                    f"@{seed}: {len(related_raw)} related profiles (niche-matched)")

                # For related profiles, if we have enough and they're the cream of the crop,
                # also fetch their related profiles (1-hop expansion on related)
                if len(related_raw) >= 8 and _has_cookies and pool.healthy_count > 0:
                    # Pick top 3 related profiles for sub-expansion
                    sub_seeds = [rp.get("username", "") for rp in related_raw[:3]
                                 if rp.get("username")]
                    for sub_seed in sub_seeds:
                        if cancel_flag or len(discovery_usernames) >= MAX_RELATED:
                            break
                        # Get their related profiles too
                        sub_data, sub_status = await loop.run_in_executor(
                            None, _fetch_profile_cookie, session2, sub_seed)
                        if sub_data and sub_data.get("id"):
                            session3, cookie3 = pool.next_session()
                            await pool.wait_for(cookie3)
                            sub_related = await loop.run_in_executor(
                                None, _fetch_related_profiles_cookie,
                                session3, sub_data["id"])
                            if sub_related:
                                pool.mark_success(cookie3)
                                for rp in sub_related:
                                    uname = rp.get("username", "")
                                    if (uname and uname not in visited
                                            and uname not in seen_before
                                            and uname not in discovery_usernames):
                                        discovery_usernames.append(uname)
                                stats.related_found += len(sub_related)
            else:
                pool.mark_error(cookie2)

        # Strategy 2: Following List (breadth)
        if hops > 0 and _has_cookies and mode in ("cookie", "hybrid") and seed_user_id and pool.healthy_count > 0:
            session4, cookie4 = pool.next_session()
            await pool.wait_for(cookie4)
            
            def following_prog(fetched, total):
                pct = min(100, int(100 * fetched / max(1, total)))
                bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
                msg = f"[{bar}] @{seed} following list ({fetched}/{total})"
                try: asyncio.run_coroutine_threadsafe(progress_cb(msg), loop)
                except Exception: pass

            following, status = await loop.run_in_executor(
                None, _fetch_following_cookie, session4, seed_user_id, max_follow, following_prog)
            if following:
                pool.mark_success(cookie4)
                stats.following_found += len(following)
                for uname in following:
                    if uname not in visited and uname not in seen_before:
                        if uname not in discovery_usernames:
                            discovery_usernames.append(uname)
                await progress_cb(
                    f"@{seed}: {len(following)} in following list")
            else:
                if status in (401, 403):
                    pool.deactivate(cookie4)
                    await progress_cb(f"Cookie #{cookie4['id']} ({cookie4['label']}) expired (HTTP {status}). Deactivated.")
                elif status == 429:
                    pool.mark_rate_limited(cookie4)
                    await progress_cb(f"@{seed}: following fetch rate limited.")
                else:
                    pool.mark_error(cookie4)

        # Strategy 3: Apify following expansion (fallback / supplement)
        if hops > 0 and not discovery_usernames and _has_apify and (mode in ("apify", "hybrid") or (mode == "cookie" and pool.healthy_count == 0)):
            tok = _next_apify_token()
            if tok:
                try:
                    items = await loop.run_in_executor(
                        None, _apify_run_sync, tok["token"], {
                            "username":      seed,
                            "operationMode": "analyzeFollowersFollowing",
                            "resultsType":   "following",
                            "resultsLimit":  max_follow,
                        })
                    _mark_apify_used(tok["id"], 0.02)
                    for item in items:
                        uname = item.get("username", "")
                        if uname and uname not in visited:
                            discovery_usernames.append(uname)
                            apify_profiles[uname] = _normalize_apify(item)
                    stats.a_hits += 1
                    await progress_cb(
                        f"@{seed} (Apify): {len(discovery_usernames)} accounts")
                except Exception as e:
                    err = str(e)
                    await progress_cb(f"Apify error for @{seed}: {err[:60]}")
                    if tok: _mark_apify_error(tok["id"], err)

        if not discovery_usernames:
            await progress_cb(f"@{seed}: no discovery data. Check backends.")
            continue

        await progress_cb(
            f"@{seed}: {len(discovery_usernames)} candidates total "
            f"(related:{stats.related_found} following:{stats.following_found})")

        # ── C. Score each discovered account (concurrent) ────────────
        batch_matched = 0
        batch_size = len(discovery_usernames)

        async def _score_one(i: int, uname: str):
            """Score a single profile — called concurrently."""
            nonlocal batch_matched
            if cancel_flag or len(results) >= max_total:
                return
            if uname in visited or uname in seen_before:
                return
            visited.add(uname)

            # Get full profile data (with fallback chain)
            profile_data = apify_profiles.get(uname)
            if not profile_data:
                profile_data = await _fetch_profile_with_fallback(
                    uname, pool, _has_cookies, _has_apify, mode, loop,
                    stats, semaphore)

            if not profile_data:
                return

            stats.profiles_scored += 1

            # Apply master filter
            prof = await filter_profile(profile_data, seed, 1, cfg)
            if prof:
                results.append(prof)
                batch_matched += 1
                stats.passed += 1
                stats.cohorts[prof.cohort] = stats.cohorts.get(prof.cohort, 0) + 1
                if prof.email:
                    stats.emails_found += 1
            else:
                # Track rejection reason
                followers = profile_data.get("edge_followed_by", {}).get("count", 0)
                fmin = cfg.get("follower_min", DEFAULT_FOLLOWER_MIN)
                fmax = cfg.get("follower_max", DEFAULT_FOLLOWER_MAX)
                if followers < fmin or followers > fmax:
                    stats.rejected_followers += 1
                elif cfg.get("location_filter", True):
                    ls, _ = score_location(profile_data)
                    if ls < 0:
                        stats.rejected_location += 1
                    else:
                        stats.rejected_reach += 1
                else:
                    stats.rejected_reach += 1

        # Process in concurrent batches
        for batch_start in range(0, len(discovery_usernames), CONCURRENCY * 3):
            if cancel_flag or len(results) >= max_total:
                break

            batch_end = min(batch_start + CONCURRENCY * 3, len(discovery_usernames))
            batch = discovery_usernames[batch_start:batch_end]

            tasks = [_score_one(batch_start + j, uname)
                     for j, uname in enumerate(batch)]
            await asyncio.gather(*tasks, return_exceptions=True)

            # Progress report every batch
            pct = min(100, int(100 * len(results) / max(1, max_total // 4)))
            bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
            await progress_cb(
                f"[{bar}] {batch_end}/{batch_size} scored | "
                f"matched:{len(results)} | {pool.stats_str()}"
            )

        await progress_cb(
            f"@{seed} done: {batch_matched} matched from "
            f"{batch_size} candidates")

    # ── 2-hop expansion ──────────────────────────────────────────────
    if hops > 1 and results and not cancel_flag:
        hop2_seeds = [p.username for p in results[:20]]
        await progress_cb(f"2-hop: expanding from {len(hop2_seeds)} matched accounts...")
        cfg2 = dict(cfg)
        hop2_results = await run_pipeline(hop2_seeds, cfg2, 1, True, progress_cb)
        existing = {p.username for p in results}
        for p in hop2_results:
            if p.username not in existing:
                p.hop = 2
                results.append(p)
                existing.add(p.username)

    results.sort(key=lambda p: (p.niche_score, p.location_score, p.engagement_rate), reverse=True)
    mark_seen([p.username for p in results])

    await progress_cb(f"Scan complete!\n{stats.summary(mode)}")
    return results


# =====================================================================
#  CRM PUSH
# =====================================================================

def push_to_crm(profiles: List[CreatorProfile]) -> dict:
    import db
    inserted, dup, skipped = 0, 0, 0
    for p in profiles:
        if getattr(p, "filter_reason", "Passed") != "Passed":
            continue
        try:
            conn = db.get_db()
            if conn.execute("SELECT id FROM creators WHERE LOWER(handle)=?",
                            (p.username.lower(),)).fetchone():
                conn.close(); dup += 1; continue
            if p.email:
                local, at, domain = p.email.rpartition("@")
                if at and domain.lower() in ("gmail.com", "googlemail.com"):
                    local = local.replace(".", "").split("+")[0]
                norm = f"{local}@{domain}".lower()
                if conn.execute("SELECT id FROM creators WHERE LOWER(REPLACE(email,'.',''))=?",
                                (norm,)).fetchone():
                    conn.close(); dup += 1; continue
            conn.close()

            if p.email:
                cid, is_new = db.add_or_get_creator(
                    p.email, name=p.full_name, handle=p.username,
                    platform="instagram", followers=p.followers,
                    bio=p.bio, niche=p.niche_tier, source="discovery",
                    tier=p.budget_tier)
            else:
                conn = db.get_db()
                fallback_email = f"no_email_{p.username}"
                cur  = conn.execute(
                    "INSERT INTO creators(name,handle,platform,email,followers,bio,niche,source,tier,stage)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (p.full_name, p.username, "instagram", fallback_email, p.followers,
                     p.bio, p.niche_tier, "discovery", p.budget_tier, "discovered"))
                conn.commit(); cid = cur.lastrowid; is_new = True; conn.close()

            if is_new:
                conn = db.get_db()
                conn.execute("""UPDATE creators SET
                    stage='discovered', engagement_rate=?, reach_ratio=?,
                    score_total=?, budget_tier=?, estimated_cost=?,
                    source_seed=?, hop=?, profile_url=?,
                    is_verified=?, is_business=?, post_count=?,
                    discovered_at=CURRENT_TIMESTAMP,
                    location_score=?, niche_score=?, niche_tier=?,
                    recent_posts_stats=?
                    WHERE id=?""",
                    (p.engagement_rate,
                     p.reach_ratio if p.reach_ratio >= 0 else None,
                     p.bio_score, p.budget_tier, p.estimated_cost,
                     p.source_seed, p.hop, p.profile_url,
                     int(p.is_verified), int(p.is_business), p.post_count,
                     p.location_score, p.niche_score, p.niche_tier,
                     p.recent_posts_stats, cid))
                conn.commit(); conn.close()
                inserted += 1
            else:
                dup += 1
        except Exception as e:
            log.warning(f"CRM push failed @{p.username}: {e}")
            skipped += 1
    return {"inserted": inserted, "duplicates": dup, "skipped": skipped}


# =====================================================================
#  EXPORT
# =====================================================================

def _opener(p: CreatorProfile) -> str:
    bio_l = (p.bio or "").lower()
    for kw, tmpl in OPENER_MAP.items():
        if kw in bio_l:
            return tmpl
    if p.category:
        return f"Loved what you are doing in {p.category}, think there is a great fit for a MagicFit AI collab."
    return DEFAULT_OPENER

# Alias so both export() and export_all() work (different call sites)
def export(profiles, seeds=None): return export_all(profiles, seeds)

def export_all(profiles: List[CreatorProfile], seeds: List[str] = None) -> dict:
    if not profiles:
        return {"csv": None, "xlsx": None, "outreach": None}
    ts  = int(time.time())
    
    seed_str = ""
    if seeds:
        # Create a safe string from the first 2 seeds
        safe_seeds = [s.replace('@', '').strip() for s in seeds[:2]]
        seed_str = "_".join(safe_seeds) + "_"
        
    cp  = os.path.join(OUT_DIR, f"scan_{seed_str}{ts}.csv")
    xp  = os.path.join(OUT_DIR, f"scan_{seed_str}{ts}.xlsx")
    op  = os.path.join(OUT_DIR, f"outreach_{seed_str}{ts}.csv")

    fields = list(asdict(profiles[0]).keys())

    with open(cp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows([asdict(p) for p in profiles])

    xlsx_path = None
    if _HAVE_XLSX:
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Lookalikes"
        ws.append(fields)
        for p in profiles: ws.append(list(asdict(p).values()))
        for i in range(1, len(fields)+1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = 18
        wb.save(xp); xlsx_path = xp

    outreach_path = None
    with_email = [p for p in profiles if p.email]
    if with_email:
        with open(op, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["email","first_name","instagram_username","niche_tier",
                        "opener","cohort","followers","engagement_rate",
                        "reach_ratio","location_hint","estimated_cost_usd","profile_url"])
            for p in with_email:
                fn = (p.full_name or p.username).split(" ")[0]
                w.writerow([p.email, fn, p.username, p.niche_tier, _opener(p),
                             p.cohort, p.followers, p.engagement_rate,
                             p.reach_ratio, p.location_hint,
                             p.estimated_cost, p.profile_url])
        outreach_path = op

    return {"csv": cp, "xlsx": xlsx_path, "outreach": outreach_path, "timestamp": ts}


async def extract_only(seeds: list, progress_cb: Callable) -> List[str]:
    """
    Extract followed usernames for the given seeds without downloading profile details or filtering.
    """
    import asyncio
    global cancel_flag
    cancel_flag = False

    cookie_rows  = list_cookies()
    pool         = SmartCookiePool(cookie_rows)
    _has_cookies = bool(cookie_rows)
    _has_apify   = has_apify_available()

    if not _has_cookies and not _has_apify:
        await progress_cb("No backend ready. Add /addcookie or /addapify first.")
        return []

    loop = asyncio.get_event_loop()
    all_usernames = set()

    for seed_idx, seed_raw in enumerate(seeds):
        if cancel_flag:
            break

        seed = seed_raw.strip().lstrip("@")
        await progress_cb(f"━━ Extracting Seed {seed_idx+1}/{len(seeds)}: @{seed} ━━")

        # Get seed user ID
        seed_user_id = None
        web_status = "N/A"
        cookie_status = "N/A"
        apify_error = "N/A"

        await progress_cb(f"[░░░░░░░░░░░░░░░░░░░░] @{seed} fetching profile (web)...")
        seed_data, web_status = await loop.run_in_executor(
            None, _fetch_profile_web_public, seed)
        if seed_data:
            seed_user_id = seed_data.get("id", "")
        else:
            await progress_cb(f"@{seed} web public fetch failed (status: {web_status})")

        if not seed_user_id and _has_cookies:
            await progress_cb(f"[█████░░░░░░░░░░░░░░░] @{seed} fetching profile (cookie)...")
            session, cookie = pool.next_session()
            await pool.wait_for(cookie)
            seed_data, cookie_status = await loop.run_in_executor(
                None, _fetch_profile_cookie, session, seed)
            if cookie_status in (401, 403):
                pool.deactivate(cookie)
                await progress_cb(f"@{seed} cookie fetch failed. Cookie #{cookie['id']} expired (HTTP {cookie_status}). Deactivated.")
            elif seed_data:
                seed_user_id = seed_data.get("id", "")
            else:
                await progress_cb(f"@{seed} cookie fetch failed (status: {cookie_status})")

        if not seed_user_id and _has_apify:
            await progress_cb(f"[██████████░░░░░░░░░░] @{seed} fetching profile (apify)...")
            tok = _next_apify_token()
            if tok:
                try:
                    items = await loop.run_in_executor(
                        None, _apify_run_sync, tok["token"], {
                            "username": seed,
                            "operationMode": "analyzeProfile",
                        }, 60)
                    if items:
                        seed_data = _normalize_apify(items[0])
                        _mark_apify_used(tok["id"])
                        seed_user_id = seed_data.get("id", "")
                    else:
                        apify_error = "No items returned"
                        await progress_cb(f"@{seed} apify fetch failed (no items returned)")
                except Exception as e:
                    apify_error = str(e)
                    await progress_cb(f"@{seed} apify fetch failed: {str(e)[:100]}")
                    if tok: _mark_apify_error(tok["id"], str(e))
            else:
                await progress_cb(f"@{seed} apify fetch failed: no active apify tokens")

        if not seed_user_id:
            await progress_cb(f"@{seed}: could not resolve profile. (Web={web_status}, Cookie={cookie_status}, Apify={str(apify_error)[:40]})")
            continue

        # Extract following
        following = []
        if _has_cookies and pool.healthy_count > 0:
            session4, cookie4 = pool.next_session()
            await pool.wait_for(cookie4)

            def following_prog(fetched, total):
                pct = min(100, int(100 * fetched / max(1, total)))
                bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
                msg = f"[{bar}] @{seed} extracting followings ({fetched}/{total})"
                try: asyncio.run_coroutine_threadsafe(progress_cb(msg), loop)
                except Exception: pass

            following, status = await loop.run_in_executor(
                None, _fetch_following_cookie, session4, seed_user_id, MAX_FOLLOWING, following_prog)
            if following:
                pool.mark_success(cookie4)
            else:
                if status in (401, 403):
                    pool.deactivate(cookie4)
                    await progress_cb(f"Cookie #{cookie4['id']} ({cookie4['label']}) expired (HTTP {status}). Deactivated.")
                elif status == 429:
                    pool.mark_rate_limited(cookie4)
                    await progress_cb(f"@{seed} cookie following fetch rate limited (HTTP 429)")
                else:
                    await progress_cb(f"@{seed} cookie following fetch failed (HTTP {status})")

        if not following and _has_apify:
            # Fallback to Apify
            await progress_cb(f"[███████████████░░░░░] @{seed} trying apify fallback for followings...")
            tok = _next_apify_token()
            if tok:
                try:
                    items = await loop.run_in_executor(
                        None, _apify_run_sync, tok["token"], {
                            "username":      seed,
                            "operationMode": "analyzeFollowersFollowing",
                            "resultsType":   "following",
                            "resultsLimit":  MAX_FOLLOWING,
                        })
                    _mark_apify_used(tok["id"], 0.02)
                    for item in items:
                        uname = item.get("username", "")
                        if uname:
                            following.append(uname)
                except Exception as e:
                    await progress_cb(f"@{seed} apify fallback failed: {str(e)[:100]}")
                    if tok: _mark_apify_error(tok["id"], str(e))
            else:
                await progress_cb(f"@{seed} no active apify tokens for fallback")

        if following:
            all_usernames.update(following)
            await progress_cb(f"@{seed}: Extracted {len(following)} usernames.")
        else:
            await progress_cb(f"@{seed}: Failed to extract followings.")

    await progress_cb(f"Extraction complete! Total unique usernames: {len(all_usernames)}")
    return sorted(list(all_usernames))
