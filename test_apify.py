import requests
import database as db

conn = db.get_db()
row = conn.execute("SELECT token FROM apify_tokens LIMIT 1").fetchone()
conn.close()

if row:
    token = row["token"]
    print(f"Token: {token[:10]}...")
    
    # Test limits
    resp = requests.get(f"https://api.apify.com/v2/users/me/limits?token={token}")
    print("\n--- LIMITS ---")
    print(resp.status_code)
    try:
        print(resp.json())
    except:
        print(resp.text)
        
    # Test monthly usage
    resp2 = requests.get(f"https://api.apify.com/v2/users/me/usage/monthly?token={token}")
    print("\n--- MONTHLY USAGE ---")
    print(resp2.status_code)
    try:
        print(resp2.json())
    except:
        print(resp2.text)
else:
    print("No tokens found.")
