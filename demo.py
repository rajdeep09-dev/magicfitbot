import database as db
import ai_router
import html_exporter

def run_demo():
    print("Generating demo...")
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM creators WHERE stage='discovered' OR stage='Passed' LIMIT 50").fetchall()
    conn.close()
    
    if not rows:
        print("No creators found for demo. Taking dummy data...")
        rows = [
            {"name": "Alice", "handle": "alice", "platform": "ig", "bio": "fitness trainer", "niche": "fitness", "followers": 150000},
            {"name": "Bob", "handle": "bob", "platform": "ig", "bio": "travel vlogger", "niche": "travel", "followers": 75000}
        ]
        
    info_list = []
    for r in rows:
        info_list.append({
            "name": r["name"],
            "handle": r["handle"],
            "platform": r["platform"],
            "bio": r["bio"],
            "niche": r["niche"],
            "followers": r["followers"] if "followers" in r.keys() else 0
        })
        
    print(f"Batch processing {len(info_list)} creators...")
    all_hooks = ai_router.generate_batched_dm_hooks(info_list)
    
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
        
    artifact_path = r"C:\Users\Nabir Hossain\.gemini\antigravity-ide\brain\d6eee5bb-71e9-45bb-9b8c-e8e7b6f02346\demo_magicfit_dm_tool.html"
    html_exporter.generate_dm_tool(creators_list, artifact_path)
    print(f"Demo created at {artifact_path}")

if __name__ == "__main__":
    run_demo()
