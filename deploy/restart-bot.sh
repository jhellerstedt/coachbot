#!/usr/bin/env bash
set -euo pipefail

COACHBOT="${COACHBOT:-$HOME/coachbot}"
cd "$COACHBOT/coach_bot"

docker compose down || true
docker compose up -d --build

docker compose ps
