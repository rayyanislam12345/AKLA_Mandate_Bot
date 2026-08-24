#!/bin/bash
# Runs Punjab + BPPT only, scheduled locally via launchd (see
# com.mandatebot.dailyrun.plist / README's Scheduling section).
#
# These two sources are excluded from the GitHub Actions workflow
# (daily-run.yml) because they're unreachable from GitHub-hosted runners'
# IP ranges — net::ERR_CONNECTION_TIMED_OUT, confirmed 2026-08-24, even
# after generously raising the navigation timeout. Both work fine from a
# normal local network, so they run here instead.
#
# Both this script and the GitHub Actions workflow read/write the same
# state/seen.json, committing it back to the repo after each run. Since
# GitHub Actions now never touches Punjab/BPPT and this script only ever
# runs those two, the two processes add disjoint sets of keys — but a
# `git pull` before running is still required so this run's in-memory
# state includes whatever GitHub Actions has committed since the last
# local run, or its own save() would silently drop those keys when it
# rewrites the whole file.
set -euo pipefail
cd "$(dirname "$0")"
eval "$(/opt/homebrew/bin/brew shellenv)"
source .venv/bin/activate

git pull --ff-only

python3 -m mandate_bot.main --source punjab,bppt

if [ -f secrets.yaml ]; then
    SUPABASE_URL=$(python3 -c "import yaml; print((yaml.safe_load(open('secrets.yaml')) or {}).get('supabase', {}).get('url', ''))")
    SUPABASE_SERVICE_ROLE_KEY=$(python3 -c "import yaml; print((yaml.safe_load(open('secrets.yaml')) or {}).get('supabase', {}).get('service_role_key', ''))")
    if [ -n "$SUPABASE_URL" ] && [ -n "$SUPABASE_SERVICE_ROLE_KEY" ]; then
        export SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY
        python3 sync_to_supabase.py
    else
        echo "No [supabase] block in secrets.yaml — skipping Supabase sync for this local run."
    fi
else
    echo "No secrets.yaml found — skipping Supabase sync for this local run."
fi

git add state/seen.json
git pull --rebase --autostash
git diff --cached --quiet || git commit -m "Update seen-tender state (local: punjab, bppt) [skip ci]"
git push
