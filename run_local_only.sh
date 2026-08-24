#!/bin/bash
# Runs all nine sources locally, scheduled via launchd (see
# com.mandatebot.dailyrun.plist / README's Scheduling section).
#
# Two of the nine sources (Punjab, BPPT) are unreachable from GitHub-hosted
# runners' IP ranges (net::ERR_CONNECTION_TIMED_OUT, confirmed
# 2026-08-24) — rather than keep splitting the run across GitHub Actions
# + local, everything just runs here now. The GitHub Actions workflow is
# disabled (schedule removed, kept as workflow_dispatch only) so the two
# don't double-scan the same sources.
#
# `git pull` before running picks up any state committed by a manual run
# or a workflow_dispatch trigger since the last scheduled local run — or
# this run's own save() would silently drop those keys when it rewrites
# the whole file.
set -euo pipefail
cd "$(dirname "$0")"
eval "$(/opt/homebrew/bin/brew shellenv)"
source .venv/bin/activate

git pull --ff-only

python3 -m mandate_bot.main

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
git diff --cached --quiet || git commit -m "Update seen-tender state (local run) [skip ci]"
git push
