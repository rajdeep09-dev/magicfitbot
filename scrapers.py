"""
Profile link scrapers.
- Instagram: Scrapingdog screenshot API → Gemini Vision analysis
- Instagram API: Scrapingdog Instagram profile endpoint
- Other platforms: basic requests scraping
"""

import re
import json
import os
import logging
import tempfile
import requests
from urllib.parse import urlparse
from config import SCRAPINGDOG_API_KEY
import database as db

logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')

JUNK_EMAILS = {
    "info@instagram.com", "support@instagram.com", "noreply@instagram.com",
    "press@instagram.com", "example@email.com",
}


def get_scrapingdog_key():
    """Get Scrapingdog key from DB setting or config."""
    key = db.get_setting("scrapingdog_api_key", "")
    if key:
        return key
    return SCRAPINGDOG_API_KEY


def scrape_profile(url: str) -> dict:
    """
    Scrape any profile URL. Routes to right scraper based on platform.
    Returns: {emails, name, handle, followers, bio, niche, platform, screenshot_path}
    """
    platform = _detect_platform(url)
    handle = _extract_handle_from_url(url)

    if platform == "instagram":
        return _scrape_instagram(url, handle)
    else:
        return _scrape_generic(url, platform, handle)


def screenshot_and_analyze(url: str) -> dict:
    """
    Take a screenshot of any URL via Scrapingdog, then analyze with Gemini Vision.
    Returns: {emails, name, handle, followers, bio, niche, platform, screenshot_path}
    """
    api_key = get_scrapingdog_key()
    if not api_key:
        return {"error": "No Scrapingdog API key configured. Set it with /setscrapingdog"}

    result = {
        "emails": [], "name": None, "handle": _extract_handle_from_url(url),
        "followers": None, "bio": None, "niche": None,
        "platform": _detect_platform(url), "url": url,
        "screenshot_path": None,
    }

    try:
        # Step 1: Screenshot via Scrapingdog
        logger.info(f"Taking screenshot of {url}")
        params = {
            "api_key": api_key,
            "url": url,
            "fullPage": "false",
            "width": "1920",
            "height": "1080",
            "wait_until": "domcontentloaded",
            "format": "png",
        }
        resp = requests.get(
            "https://api.scrapingdog.com/screenshot",
            params=params,
            timeout=60,
        )

        if resp.status_code != 200:
            result["error"] = f"Scrapingdog screenshot failed (HTTP {resp.status_code})"
            return result

        # Save screenshot to temp file
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.write(resp.content)
        tmp.close()
        result["screenshot_path"] = tmp.name
        logger.info(f"Screenshot saved: {tmp.name} ({len(resp.content)} bytes)")

        # Step 2: Analyze with Gemini Vision
        from ocr_router import extract_from_screenshot
        ocr_result = extract_from_screenshot(tmp.name)

        result["emails"] = ocr_result.get("emails", [])
        result["name"] = ocr_result.get("name")
        result["handle"] = ocr_result.get("handle") or result["handle"]
        result["followers"] = ocr_result.get("followers")
        result["bio"] = ocr_result.get("bio")
        result["niche"] = ocr_result.get("niche")

    except requests.exceptions.Timeout:
        result["error"] = "Scrapingdog screenshot timed out (60s). Try again."
    except Exception as e:
        result["error"] = f"Screenshot + analyze failed: {e}"
        logger.warning(f"screenshot_and_analyze error: {e}")

    return result


def _scrape_instagram(url: str, handle: str) -> dict:
    """Try Scrapingdog Instagram API first, then screenshot fallback."""
    result = {
        "emails": [], "name": None, "handle": handle,
        "followers": None, "bio": None, "niche": None,
        "platform": "instagram", "url": url,
    }

    api_key = get_scrapingdog_key()
    if not api_key:
        result["error"] = "No Scrapingdog key. Send a screenshot instead, or set key with /setscrapingdog"
        return result

    if not handle:
        result["error"] = "Could not extract handle from URL"
        return result

    # Try Instagram profile API first
    try:
        api_url = "https://api.scrapingdog.com/instagram/profile"
        params = {"api_key": api_key, "username": handle}
        r = requests.get(api_url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()

        if isinstance(data, dict):
            result["name"] = data.get("full_name") or data.get("name")
            result["followers"] = data.get("followers") or data.get("follower_count") or data.get("edge_followed_by", {}).get("count")
            result["bio"] = data.get("biography") or data.get("bio")

            if result["bio"]:
                emails = EMAIL_PATTERN.findall(result["bio"])
                result["emails"] = [e for e in emails if e.lower() not in JUNK_EMAILS]

            biz_email = data.get("business_email") or data.get("public_email")
            if biz_email and biz_email.lower() not in JUNK_EMAILS:
                if biz_email not in result["emails"]:
                    result["emails"].append(biz_email)

            if result["followers"]:
                try:
                    result["followers"] = int(result["followers"])
                except (ValueError, TypeError):
                    result["followers"] = None

            if result["emails"] or result["name"]:
                return result

    except Exception as e:
        logger.warning(f"IG profile API failed: {e}")

    # Fallback: screenshot the Instagram page and analyze with vision
    logger.info(f"Falling back to screenshot for @{handle}")
    ss_result = screenshot_and_analyze(url)
    # Merge any data we got from API with screenshot results
    for key in ["name", "handle", "followers", "bio", "niche"]:
        if not result.get(key) and ss_result.get(key):
            result[key] = ss_result[key]
    if ss_result.get("emails"):
        for email in ss_result["emails"]:
            if email not in result["emails"]:
                result["emails"].append(email)
    if ss_result.get("error"):
        result["error"] = ss_result["error"]
    if ss_result.get("screenshot_path"):
        result["screenshot_path"] = ss_result["screenshot_path"]

    return result


def _scrape_generic(url: str, platform: str, handle: str) -> dict:
    """Generic scraper for LinkedIn, Twitter, TikTok, YouTube, personal sites."""
    result = {
        "emails": [], "name": None, "handle": handle,
        "followers": None, "bio": None, "niche": None,
        "platform": platform, "url": url,
    }

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        text = r.text

        emails = set(EMAIL_PATTERN.findall(text))
        emails = [e for e in emails if e.lower() not in JUNK_EMAILS]
        emails = [e for e in emails if not _is_asset_email(e)]
        result["emails"] = emails

        og_title = re.search(r'<meta property="og:title" content="([^"]+)"', text)
        if og_title:
            result["name"] = og_title.group(1)

        og_desc = re.search(r'<meta property="og:description" content="([^"]+)"', text)
        if og_desc:
            result["bio"] = og_desc.group(1)[:300]

        fp = re.search(r'([\d,.]+[KkMm]?)\s*(?:followers|Followers)', text)
        if fp:
            from ocr_router import _parse_follower_count
            result["followers"] = _parse_follower_count(fp.group(1))

    except Exception as e:
        result["error"] = f"Scraping failed: {e}"
        logger.warning(f"Generic scrape error for {url}: {e}")

    return result


def _detect_platform(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    if "instagram" in domain:
        return "instagram"
    elif "linkedin" in domain:
        return "linkedin"
    elif "twitter" in domain or "x.com" in domain:
        return "twitter"
    elif "tiktok" in domain:
        return "tiktok"
    elif "youtube" in domain or "youtu.be" in domain:
        return "youtube"
    return "other"


def _extract_handle_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    path = parsed.path.strip("/").split("/")
    if path and path[0]:
        handle = path[0]
        if handle in ("in", "company", "user", "@"):
            if len(path) > 1:
                return path[1].lstrip("@")
        return handle.lstrip("@")
    return None


def _is_asset_email(email: str) -> bool:
    junk_extensions = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js"}
    for ext in junk_extensions:
        if ext in email.lower():
            return True
    return False
