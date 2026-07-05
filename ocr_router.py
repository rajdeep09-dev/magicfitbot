"""
OCR Router for screenshots: Gemini Vision → Mistral Vision → NVIDIA Vision → Tesseract.
Extracts email, name, handle, follower count, bio, niche from profile screenshots.
"""

import re
import json
import base64
import logging
import subprocess
import requests
from config import GEMINI_API_KEY as _CFG_GEMINI, MISTRAL_API_KEY as _CFG_MISTRAL, NVIDIA_API_KEY as _CFG_NVIDIA
import database as db

logger = logging.getLogger(__name__)


def _get_gemini_key():
    val = db.get_setting("gemini_api_key", "")
    return val if val else _CFG_GEMINI


def _get_mistral_key():
    val = db.get_setting("mistral_api_key", "")
    return val if val else _CFG_MISTRAL


def _get_nvidia_key():
    val = db.get_setting("nvidia_api_key", "")
    return val if val else _CFG_NVIDIA

EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')

JUNK_EMAILS = {
    "example@email.com", "email@example.com", "name@domain.com",
    "user@example.com", "test@test.com", "info@instagram.com",
    "support@instagram.com", "noreply@instagram.com", "press@instagram.com",
}


def extract_from_screenshot(image_path: str) -> dict:
    """
    Extract creator info from a profile screenshot.
    Tries Gemini Vision, then Mistral Vision (Pixtral Large), then NVIDIA Vision
    (Llama 3.2 90B Vision), then Tesseract.
    Returns: {emails, name, handle, followers, bio, niche, platform, source}
    """
    # Try Gemini Vision first
    if _get_gemini_key():
        try:
            result = _gemini_vision(image_path)
            if result and result.get("emails"):
                result["source"] = "gemini_vision"
                return result
            elif result and (result.get("name") or result.get("bio")):
                # Got some data but no email yet, keep it and try other sources for the email
                result["source"] = "gemini_vision"
                better_emails = _try_other_sources_for_emails(image_path, skip_gemini=True)
                if better_emails:
                    result["emails"] = better_emails
                return result
        except Exception as e:
            logger.warning(f"Gemini Vision failed: {e}")

    # Try Mistral Vision second
    if _get_mistral_key():
        try:
            result = _mistral_vision(image_path)
            if result and result.get("emails"):
                result["source"] = "mistral_vision"
                return result
            elif result and (result.get("name") or result.get("bio")):
                result["source"] = "mistral_vision"
                better_emails = _try_other_sources_for_emails(image_path, skip_gemini=True, skip_mistral=True)
                if better_emails:
                    result["emails"] = better_emails
                return result
        except Exception as e:
            logger.warning(f"Mistral Vision failed: {e}")

    # Try NVIDIA Vision third
    if _get_nvidia_key():
        try:
            result = _nvidia_vision(image_path)
            if result and result.get("emails"):
                result["source"] = "nvidia_vision"
                return result
            elif result and (result.get("name") or result.get("bio")):
                result["source"] = "nvidia_vision"
                tess_result = _tesseract_ocr(image_path)
                if tess_result.get("emails"):
                    result["emails"] = tess_result["emails"]
                return result
        except Exception as e:
            logger.warning(f"NVIDIA Vision failed: {e}")

    # Final fallback to Tesseract
    result = _tesseract_ocr(image_path)
    result["source"] = "tesseract"
    return result


def _try_other_sources_for_emails(image_path: str, skip_gemini=False, skip_mistral=False) -> list:
    """Try remaining providers in order just to recover an email an earlier one missed."""
    if not skip_gemini and _get_gemini_key():
        try:
            r = _gemini_vision(image_path)
            if r and r.get("emails"):
                return r["emails"]
        except Exception:
            pass
    if not skip_mistral and _get_mistral_key():
        try:
            r = _mistral_vision(image_path)
            if r and r.get("emails"):
                return r["emails"]
        except Exception:
            pass
    if _get_nvidia_key():
        try:
            r = _nvidia_vision(image_path)
            if r and r.get("emails"):
                return r["emails"]
        except Exception:
            pass
    tess = _tesseract_ocr(image_path)
    return tess.get("emails", [])


def _gemini_vision(image_path: str) -> dict:
    """Use Gemini 2.5 Flash to read the screenshot."""
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    # Detect mime type from extension
    ext = image_path.lower().split(".")[-1]
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"

    image_b64 = base64.standard_b64encode(image_bytes).decode()

    prompt = """You are looking at a screenshot of a social media profile (Instagram, LinkedIn, Twitter/X, TikTok, YouTube, etc).

Extract the following information and return ONLY valid JSON, no markdown fences:
{
  "emails": ["list of email addresses visible"],
  "name": "creator's display name",
  "handle": "their @username without the @",
  "followers": "follower count as integer (convert 1.2K to 1200, 1.5M to 1500000)",
  "bio": "their bio/description text",
  "niche": "what they post about based on bio and visible content (e.g. 'AI and tech', 'fashion', 'fitness')",
  "platform": "instagram | linkedin | twitter | tiktok | youtube | other"
}

If a field cannot be determined, use null. For followers, return an integer or null.
Only include emails that look real (skip placeholder emails like example@email.com).
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={_get_gemini_key()}"
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime, "data": image_b64}}
            ]
        }],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1024}
    }

    r = requests.post(url, json=payload, timeout=45)
    r.raise_for_status()
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]

    # Parse JSON from response
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return {"emails": [], "raw_text": text}

    parsed = json.loads(text[start:end + 1])

    # Clean up
    if "emails" not in parsed or not isinstance(parsed["emails"], list):
        parsed["emails"] = []
    parsed["emails"] = [e for e in parsed["emails"] if e and e.lower() not in JUNK_EMAILS]

    # Convert followers to int if it's a string
    if "followers" in parsed and parsed["followers"] is not None:
        try:
            if isinstance(parsed["followers"], str):
                parsed["followers"] = _parse_follower_count(parsed["followers"])
        except Exception:
            parsed["followers"] = None

    return parsed


def _mistral_vision(image_path: str) -> dict:
    """Use Mistral's Pixtral Large to read the screenshot. Free Experiment tier, no card."""
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    ext = image_path.lower().split(".")[-1]
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    image_b64 = base64.standard_b64encode(image_bytes).decode()

    prompt = """You are looking at a screenshot of a social media profile (Instagram, LinkedIn, Twitter/X, TikTok, YouTube, etc).

Extract the following information and return ONLY valid JSON, no markdown fences, no extra text:
{
  "emails": ["list of email addresses visible"],
  "name": "creator's display name",
  "handle": "their @username without the @",
  "followers": "follower count as integer (convert 1.2K to 1200, 1.5M to 1500000)",
  "bio": "their bio/description text",
  "niche": "what they post about based on bio and visible content (e.g. 'AI and tech', 'fashion', 'fitness')",
  "platform": "instagram | linkedin | twitter | tiktok | youtube | other"
}

If a field cannot be determined, use null. For followers, return an integer or null.
Only include emails that look real (skip placeholder emails like example@email.com)."""

    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {_get_mistral_key()}", "Content-Type": "application/json"}
    payload = {
        "model": "mistral-medium-latest",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}}
            ]
        }],
        "temperature": 0.1,
        "max_tokens": 1024,
    }

    r = requests.post(url, headers=headers, json=payload, timeout=45)
    r.raise_for_status()
    data = r.json()
    text = data["choices"][0]["message"]["content"]

    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return {"emails": [], "raw_text": text}

    parsed = json.loads(text[start:end + 1])

    if "emails" not in parsed or not isinstance(parsed["emails"], list):
        parsed["emails"] = []
    parsed["emails"] = [e for e in parsed["emails"] if e and e.lower() not in JUNK_EMAILS]

    if "followers" in parsed and parsed["followers"] is not None:
        try:
            if isinstance(parsed["followers"], str):
                parsed["followers"] = _parse_follower_count(parsed["followers"])
        except Exception:
            parsed["followers"] = None

    return parsed


def _nvidia_vision(image_path: str) -> dict:
    """Use NVIDIA NIM's Llama 3.2 90B Vision to read the screenshot. Free, no card."""
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    ext = image_path.lower().split(".")[-1]
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    image_b64 = base64.standard_b64encode(image_bytes).decode()

    prompt = """You are looking at a screenshot of a social media profile (Instagram, LinkedIn, Twitter/X, TikTok, YouTube, etc).

Extract the following information and return ONLY valid JSON, no markdown fences, no extra text:
{
  "emails": ["list of email addresses visible"],
  "name": "creator's display name",
  "handle": "their @username without the @",
  "followers": "follower count as integer (convert 1.2K to 1200, 1.5M to 1500000)",
  "bio": "their bio/description text",
  "niche": "what they post about based on bio and visible content (e.g. 'AI and tech', 'fashion', 'fitness')",
  "platform": "instagram | linkedin | twitter | tiktok | youtube | other"
}

If a field cannot be determined, use null. For followers, return an integer or null.
Only include emails that look real (skip placeholder emails like example@email.com)."""

    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {_get_nvidia_key()}", "Content-Type": "application/json"}
    payload = {
        "model": "meta/llama-3.2-90b-vision-instruct",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}}
            ]
        }],
        "temperature": 0.1,
        "max_tokens": 1024,
    }

    r = requests.post(url, headers=headers, json=payload, timeout=45)
    r.raise_for_status()
    data = r.json()
    text = data["choices"][0]["message"]["content"]

    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return {"emails": [], "raw_text": text}

    parsed = json.loads(text[start:end + 1])

    if "emails" not in parsed or not isinstance(parsed["emails"], list):
        parsed["emails"] = []
    parsed["emails"] = [e for e in parsed["emails"] if e and e.lower() not in JUNK_EMAILS]

    if "followers" in parsed and parsed["followers"] is not None:
        try:
            if isinstance(parsed["followers"], str):
                parsed["followers"] = _parse_follower_count(parsed["followers"])
        except Exception:
            parsed["followers"] = None

    return parsed


def _tesseract_ocr(image_path: str) -> dict:
    """Fallback OCR using Tesseract."""
    result = {
        "emails": [], "name": None, "handle": None,
        "followers": None, "bio": None, "niche": None,
        "platform": "instagram", "raw_text": "",
    }

    try:
        proc = subprocess.run(
            ["tesseract", image_path, "stdout", "--psm", "6"],
            capture_output=True, text=True, timeout=30
        )
        text = proc.stdout
        result["raw_text"] = text

        # Emails
        emails = set(EMAIL_PATTERN.findall(text))
        result["emails"] = [e for e in emails if e.lower() not in JUNK_EMAILS]

        # Handle
        h = re.search(r'@([a-zA-Z0-9_.]{2,30})', text)
        if h:
            result["handle"] = h.group(1)

        # Followers
        fp = re.search(r'([\d,.]+[KkMm]?)\s*(?:followers|Followers)', text)
        if fp:
            result["followers"] = _parse_follower_count(fp.group(1))

        # Bio = first 200 chars
        if text:
            result["bio"] = text[:200].strip()

    except subprocess.TimeoutExpired:
        result["error"] = "OCR timeout"
    except FileNotFoundError:
        result["error"] = "Tesseract not installed"
    except Exception as e:
        result["error"] = str(e)

    return result


def _parse_follower_count(raw) -> int:
    """Convert '1.2K', '500K', '1.5M' to integer."""
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    raw = str(raw).strip().replace(",", "").replace(" ", "")
    multiplier = 1
    if raw and raw[-1].upper() == "K":
        multiplier = 1000
        raw = raw[:-1]
    elif raw and raw[-1].upper() == "M":
        multiplier = 1_000_000
        raw = raw[:-1]
    try:
        return int(float(raw) * multiplier)
    except ValueError:
        return 0


def determine_tier(followers) -> str:
    if not followers:
        return "unknown"
    if followers < 50_000:
        return "under_50k"
    elif followers < 100_000:
        return "50k_100k"
    else:
        return "100k_plus"
