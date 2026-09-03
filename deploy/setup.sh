#!/usr/bin/env bash
# One-time production setup: venv, deps, optional copy of secrets from a previous install.
set -euo pipefail

COACHBOT="${COACHBOT:-$HOME/coachbot}"
PREVIOUS_INSTALL="${PREVIOUS_INSTALL:-}"

if [[ ! -d "$COACHBOT/erg_strava" ]]; then
  echo "coachbot repo missing at $COACHBOT — sync or git clone first" >&2
  exit 1
fi

mkdir -p "$COACHBOT/erg_strava" "$COACHBOT/coach_bot"

if [[ -n "$PREVIOUS_INSTALL" ]]; then
  for pair in \
    "$PREVIOUS_INSTALL/erg_strava/config.yaml:$COACHBOT/erg_strava/config.yaml" \
    "$PREVIOUS_INSTALL/coach_bot/.env:$COACHBOT/coach_bot/.env" \
    "$PREVIOUS_INSTALL/zuliprc:$COACHBOT/zuliprc"; do
    src="${pair%%:*}"
    dst="${pair##*:}"
    if [[ -f "$src" && ! -e "$dst" ]]; then
      cp -a "$src" "$dst"
      echo "copied $(basename "$dst")"
    fi
  done

  if [[ -d "$PREVIOUS_INSTALL/erg_strava/erg_strava_cache" && ! -e "$COACHBOT/erg_strava/erg_strava_cache" ]]; then
    mkdir -p "$COACHBOT/erg_strava"
    mv "$PREVIOUS_INSTALL/erg_strava/erg_strava_cache" "$COACHBOT/erg_strava/erg_strava_cache"
    echo "moved erg_strava_cache to $COACHBOT/erg_strava/erg_strava_cache"
  fi
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
