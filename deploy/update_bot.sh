#!/usr/bin/env bash
# Daily cron: fast-forward the checkout and rebuild the bot when commits landed.
# Rebuild uses `up -d --build` without `down`, so a failed image build leaves the
# current container running.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/deploy/pull_updates.sh"
coachbot_pull_then_reexec "$ROOT" "$0" "$@"

if [[ "${COACHBOT_UPDATED:-0}" != 1 ]]; then
  echo "coachbot: no new commits; bot left running"
  exit 0
fi

echo "coachbot: rebuilding bot from $(git -C "$ROOT" rev-parse --short HEAD)"
cd "$ROOT/coach_bot"
docker compose up -d --build
docker compose ps
