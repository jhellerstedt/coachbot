#!/usr/bin/env bash
# One-time migration on hellpi: RRC-scripts coach paths -> ~/coachbot
set -euo pipefail

COACHBOT="${COACHBOT:-$HOME/coachbot}"
RRC="${RRC:-$HOME/RRC-scripts}"

if [[ ! -d "$COACHBOT/erg_strava" ]]; then
  echo "coachbot repo missing at $COACHBOT — sync or git clone first" >&2
  exit 1
fi

mkdir -p "$COACHBOT/erg_strava" "$COACHBOT/coach_bot"

# Runtime secrets and data (keep existing files if already migrated)
for pair in \
  "$RRC/erg_strava/config.yaml:$COACHBOT/erg_strava/config.yaml" \
  "$RRC/coach_bot/.env:$COACHBOT/coach_bot/.env" \
  "$RRC/rrcc-zuliprc:$COACHBOT/rrcc-zuliprc"; do
  src="${pair%%:*}"
  dst="${pair##*:}"
  if [[ -f "$src" && ! -e "$dst" ]]; then
    cp -a "$src" "$dst"
    echo "copied $(basename "$dst")"
  fi
done

if [[ -d "$RRC/erg_strava/erg_strava_cache" && ! -e "$COACHBOT/erg_strava/erg_strava_cache" ]]; then
  mkdir -p "$COACHBOT/erg_strava"
  mv "$RRC/erg_strava/erg_strava_cache" "$COACHBOT/erg_strava/erg_strava_cache"
  echo "moved erg_strava_cache to $COACHBOT/erg_strava/erg_strava_cache"
fi

# cache_dir in config.yaml stays ./erg_strava_cache (relative to erg_strava/)

chmod +x "$COACHBOT/deploy/run_weekly_plan.sh"

if [[ ! -d "$COACHBOT/.venv" ]]; then
  python3 -m venv "$COACHBOT/.venv"
fi
# shellcheck disable=SC1091
source "$COACHBOT/.venv/bin/activate"
pip install -q -U pip
pip install -q -r "$COACHBOT/erg_strava/requirements.txt" -r "$COACHBOT/coach_bot/requirements.txt"

echo "Setup done. Next: update crontab and restart coach_bot docker from $COACHBOT"
