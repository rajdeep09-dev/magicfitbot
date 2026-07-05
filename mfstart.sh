#!/data/data/com.termux/files/usr/bin/bash
# mfstart.sh — Termux launcher for MagicFit bot.
# Handles patch discovery, dependency install, patch application, auto-restart.
set -u

BOT_DIR="${BOT_DIR:-$HOME/magicfit-bot}"
mkdir -p "$BOT_DIR"

# ── 1. Storage permissions (silent if already granted) ─────────
termux-setup-storage 2>/dev/null || true

# ── 2. Find the newest patch zip anywhere sensible ─────────────
Z=""
NEWEST=0
for pat in \
    "$HOME"/storage/downloads/magicfit-patch*.zip \
    /sdcard/Download/magicfit-patch*.zip \
    "$HOME"/storage/shared/Download/magicfit-patch*.zip \
    "$HOME"/downloads/magicfit-patch*.zip \
    "$HOME"/magicfit-patch*.zip \
    /storage/emulated/0/Download/magicfit-patch*.zip
do
    for f in $pat; do
        [ -f "$f" ] || continue
        ts=$(stat -c %Y "$f" 2>/dev/null || echo 0)
        if [ "$ts" -gt "$NEWEST" ]; then
            NEWEST=$ts; Z="$f"
        fi
    done
done

if [ -z "$Z" ]; then
    if [ -f "$BOT_DIR/bot.py" ]; then
        echo "ℹ️  no new patch zip found, running existing bot from $BOT_DIR"
    else
        echo "❌ no patch zip found and no existing install."
        echo "   place magicfit-patch*.zip in ~/storage/downloads then re-run."
        exit 1
    fi
else
    echo "📦 patch: $Z"
    unzip -o "$Z" -d "$BOT_DIR" >/dev/null || {
        echo "❌ unzip failed"; exit 1;
    }
fi

cd "$BOT_DIR" || exit 1

# ── 3. Dependencies with 3-tier fallback ───────────────────────
echo "📚 installing deps…"
if [ -f requirements.txt ]; then
    pip install -q -r requirements.txt --break-system-packages 2>/dev/null \
        || pip install -q -r requirements.txt 2>/dev/null \
        || pip install -q python-telegram-bot==21.6 requests openpyxl dnspython --break-system-packages
else
    pip install -q python-telegram-bot==21.6 requests openpyxl dnspython --break-system-packages
fi

# ── 4. Apply patches idempotently (skip if already applied) ────
run_patch() {
    local script="$1"; local sentinel="$2"
    [ -f "$script" ] || return 0
    if [ -f "$sentinel" ] && [ "$script" -ot "$sentinel" ]; then
        echo "✓ $script already applied"
    else
        echo "🔧 running $script"
        python "$script" && touch "$sentinel"
    fi
}
run_patch apply_patch.py .patched_apply
run_patch speed_patch.py .patched_speed
run_patch patch9.py     .patched_patch9

# ── 5. Sanity-check: bot.py compiles ───────────────────────────
python -c "import ast, sys; ast.parse(open('bot.py',encoding='utf-8').read())" \
    || { echo "❌ bot.py has syntax errors, aborting"; exit 1; }

# ── 6. Run with auto-restart on crash ──────────────────────────
echo "🚀 launching bot.py (Ctrl+C twice to exit)"
CRASH_COUNT=0
LAST_CRASH=0
while true; do
    python bot.py
    EXIT=$?
    NOW=$(date +%s)
    if [ "$EXIT" -eq 0 ]; then
        echo "👋 bot exited cleanly"; break
    fi
    # circuit-breaker: 5 crashes in 60s → give up
    if [ $((NOW - LAST_CRASH)) -lt 60 ]; then
        CRASH_COUNT=$((CRASH_COUNT + 1))
    else
        CRASH_COUNT=1
    fi
    LAST_CRASH=$NOW
    if [ "$CRASH_COUNT" -ge 5 ]; then
        echo "🔥 5 crashes in 60s — something is broken, exiting"
        exit 1
    fi
    echo "⚠️  crashed (exit=$EXIT), restart #$CRASH_COUNT in 5s…"
    sleep 5
done
