#!/usr/bin/env bash
# Last two steps, the ones that need a human.
#   1. grants the gh token the `workflow` scope (opens a browser, ~20 seconds)
#   2. pushes the cron workflow and kicks off the first run
set -euo pipefail
cd "$(dirname "$0")"

echo "==> 1/3  granting 'workflow' scope to your gh token"
if gh auth status 2>&1 | grep -q "workflow"; then
  echo "    already granted, skipping"
else
  gh auth refresh -h github.com -s workflow
fi

echo "==> 2/3  pushing the cron workflow"
git push origin main

echo "==> 3/3  triggering the first run"
gh workflow run track.yml
sleep 12
gh run list --workflow track.yml --limit 3

cat <<'MSG'

Done. The cron now runs every 5 minutes on GitHub's servers.

If you haven't yet: open Telegram, find @codexresettrackerbot, press Start.
Until you do, the API answers "chat not found" and nothing can be delivered.
The first successful run sends a "tracker armed" message so you'll know.

  gh run list --workflow track.yml     # recent runs
  gh run watch                         # follow the current one
MSG
