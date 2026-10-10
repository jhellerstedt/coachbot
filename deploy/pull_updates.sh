#!/usr/bin/env bash
# Fast-forward this checkout before a cron job runs.
# Source from deploy scripts; do not execute directly.

# coachbot_pull ROOT
# Prints a line when HEAD moves. Returns 0 if the revision changed, 1 otherwise.
# A failed pull is logged and does not abort the caller.
coachbot_pull() {
  local root="$1"
  export GIT_TERMINAL_PROMPT=0

  if ! git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "coachbot: $root is not a git checkout; skipping pull" >&2
    return 1
  fi

  local before after
  before="$(git -C "$root" rev-parse HEAD)"
  if ! git -C "$root" pull --ff-only; then
    echo "coachbot: git pull --ff-only failed; continuing with $(git -C "$root" rev-parse --short HEAD)" >&2
    return 1
  fi

  after="$(git -C "$root" rev-parse HEAD)"
  if [[ "$before" == "$after" ]]; then
    return 1
  fi

  echo "coachbot: updated $(git -C "$root" rev-parse --short "$before") -> $(git -C "$root" rev-parse --short "$after")"
  if git -C "$root" diff --name-only "$before" "$after" | grep -E -q '(^|/)requirements\.txt$'; then
    if [[ -x "$root/.venv/bin/pip" ]]; then
      "$root/.venv/bin/pip" install -q -r "$root/erg_strava/requirements.txt" -r "$root/coach_bot/requirements.txt"
    else
      echo "coachbot: requirements changed but $root/.venv is missing; skip pip install" >&2
    fi
  fi
  return 0
}

# Pull once, then replace this process with the same script so the updated copy runs.
# Sets COACHBOT_UPDATED=1 when HEAD moved. A second entry (COACHBOT_PULLED=1) returns.
coachbot_pull_then_reexec() {
  local root="$1"
  local script="$2"
  shift 2

  if [[ "${COACHBOT_PULLED:-}" == 1 ]]; then
    return 0
  fi
  export COACHBOT_PULLED=1
  if coachbot_pull "$root"; then
    export COACHBOT_UPDATED=1
  else
    export COACHBOT_UPDATED=0
  fi
  exec "$script" "$@"
}
