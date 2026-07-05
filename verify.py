"""Quick check that patch9 is active. Run from bot dir."""
import ast, os, re

BOT = "bot.py"
if not os.path.exists(BOT):
    print("❌ Run this from your bot directory"); raise SystemExit(1)

c = open(BOT, encoding="utf-8").read()

checks = [
    ("PATCH9_REPLY_KEYBOARD",   "Reply keyboard installed"),
    ("PATCH9_MODE_CMDS_REGISTERED", "/hunt /outreach /exit registered"),
    ("PATCH9_NL_ROUTER",        "Natural language router active"),
    ("PATCH9_KEEPALIVE",        "Keep-alive wrapper installed"),
    ("_mfai_extract_handles",   "Handle extractor present"),
    ("_mfai_route",             "Router function present"),
    ("_mfai_run_scan",          "Scan runner present"),
]

failed = 0
for marker, desc in checks:
    if marker in c:
        print(f"  ✅ {desc}")
    else:
        print(f"  ❌ {desc}  (missing: {marker})")
        failed += 1

try:
    ast.parse(c)
    lc = sum(1 for _ in open(BOT, encoding="utf-8"))
    print(f"\n  ✅ bot.py parses ({lc} lines)")
except SyntaxError as e:
    print(f"\n  ❌ syntax error line {e.lineno}: {e.msg}")
    failed += 1

if failed == 0:
    print("\n🎉 patch9 fully active. Now try:")
    print('   • Tap 🔍 /hunt button')
    print('   • Type: hunt @user1 @user2')
    print('   • Type: start hunting')
else:
    print(f"\n⚠️  {failed} check(s) failed. Re-run: python patch9.py")
