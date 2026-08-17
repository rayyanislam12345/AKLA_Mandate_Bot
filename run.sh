#!/bin/bash
# Wrapper used by cron/launchd to run Mandate Bot with its venv.
set -euo pipefail
cd "$(dirname "$0")"
eval "$(/opt/homebrew/bin/brew shellenv)"
source .venv/bin/activate
python3 -m mandate_bot.main
