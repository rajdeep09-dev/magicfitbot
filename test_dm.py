import sqlite3
import requests
import json
import time
import random
import urllib.parse
import os

# Connect to database and get the existing cookie
db_path = r"c:\Users\Nabir Hossain\OneDrive\antigravity tele\outreach.db"
try:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT sessionid, csrftoken FROM ig_cookies WHERE label='jahidm' LIMIT 1")
    row = c.fetchone()
    conn.close()
except Exception as e:
    print(f"Error accessing database: {e}")
    exit(1)

if not row:
    print("Could not find 'jahidm' cookie in database!")
    exit(1)

sessionid, csrftoken = row
sessionid = urllib.parse.unquote(sessionid)

print(f"--- Cookie Loaded Successfully (Session: {sessionid[:15]}...) ---")

target_username = "rajdeep.0.21"

# Prepare the authenticated session
s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Instagram 302.0.0.23.114",
    "X-CSRFToken": csrftoken,
    "Cookie": f"sessionid={sessionid}; csrftoken={csrftoken};"
})

print(f"\n1. Looking up User ID for @{target_username} using COOKIES...")
search_url = f"https://www.instagram.com/web/search/topsearch/?context=blended&query={target_username}"
search_res = s.get(search_url, timeout=10)

uid = None
print(f"Search API Status: {search_res.status_code}")
if search_res.status_code == 200:
    try:
        users = search_res.json().get("users", [])
        for u in users:
            if u.get("user", {}).get("username") == target_username:
                uid = u.get("user", {}).get("pk")
                break
    except requests.exceptions.JSONDecodeError:
        print("Failed to decode JSON. Instagram returned HTML instead:")
        print(search_res.text[:200])
else:
    print(f"Failed response body: {search_res.text[:200]}")

# Fallback: web_profile_info
if not uid:
    print("\nFallback: trying web_profile_info...")
    profile_url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={target_username}"
    prof_res = s.get(profile_url, headers={"X-IG-App-ID": "936619743392459"}, timeout=10)
    print(f"Profile API Status: {prof_res.status_code}")
    if prof_res.status_code == 200:
        try:
            uid = prof_res.json().get("data", {}).get("user", {}).get("id")
        except requests.exceptions.JSONDecodeError:
            print("Failed to decode JSON for profile API.")
            print(prof_res.text[:200])

if not uid:
    print(f"Failed to find User ID. Instagram is blocking the requests (either via IP or your cookie requires verification).")
    exit(1)

print(f"SUCCESS: Found User ID for @{target_username} -> {uid}")

print(f"\n2. Testing Following Extraction via Cookies...")
following_url = f"https://www.instagram.com/api/v1/friendships/{uid}/following/?count=10"
f_res = s.get(following_url, headers={"X-IG-App-ID": "936619743392459"}, timeout=10)
print(f"Extraction API Status: {f_res.status_code}")
if f_res.status_code == 200:
    try:
        f_data = f_res.json()
        users = f_data.get("users", [])
        print(f"SUCCESS: Extracted {len(users)} accounts they are following! Here are the first 3:")
        for u in users[:3]:
            print(f" - @{u.get('username')}")
    except requests.exceptions.JSONDecodeError:
        print(f"Failed to parse following JSON. Response: {f_res.text[:100]}")
else:
    print(f"FAILED to extract following. Cookies might be expired or IP blocked (Response: {f_res.text[:100]})")

print(f"\n3. Testing Sending DM via Cookies...")
payload = {
    "recipient_users": f"[[{uid}]]",
    "action": "send_item",
    "is_shh_mode": "0",
    "send_attribution": "direct_thread",
    "client_context": str(int(time.time() * 1000)) + str(random.randint(1000, 9999)),
    "text": "Hello! Testing direct message from existing cookies via MagicFit Bot.",
}
post_headers = s.headers.copy()
post_headers["Content-Type"] = "application/x-www-form-urlencoded"

send_res = s.post(
    "https://i.instagram.com/api/v1/direct_v2/threads/broadcast/text/",
    headers=post_headers, data=payload, timeout=10
)
print(f"Send DM Status: {send_res.status_code}")
if send_res.status_code == 200:
    print(f"SUCCESS: DM sent successfully to @{target_username}!")
else:
    print(f"FAILED to send DM. Response: {send_res.text[:150]}")
