#!/usr/bin/env bash
# Weekly erg plan + Strava/Suunto sync (production cron).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi

export PYTHONPATH="$ROOT:$ROOT/erg_strava:$ROOT/lighties"
export CONFIG_PATH="${CONFIG_PATH:-$ROOT/erg_strava/config.yaml}"

if [[ -f "$ROOT/coach_bot/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/coach_bot/.env"
  set +a
fi

exec python3 "$ROOT/erg_strava/strava_erg_hr_plot.py" "$@"
