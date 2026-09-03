# coachbot

Zulip rowing coach bot with LLM-generated weekly training plans, erg score logging, and gym session tracking.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r erg_strava/requirements.txt
pip install -r coach_bot/requirements.txt

cp erg_strava/config.example.yaml erg_strava/config.yaml
cp coach_bot/.env.example coach_bot/.env
# Edit config.yaml and .env (OpenRouter API key, athletes, cache_dir)
```

Place your Zulip bot credentials at repo root as `zuliprc` (or set `ZULIPRC_PATH`).

## Run

```bash
export PYTHONPATH=".:erg_strava:lighties"
python -m coach_bot.main
```

Weekly plan pipeline (cron):

```bash
python erg_strava/strava_erg_hr_plot.py
```

Docker:

```bash
cp coach_bot/.env.example coach_bot/.env
docker compose -f coach_bot/docker-compose.yml up --build
```

## Production

See [deploy/README.md](deploy/README.md). On the server:

- **Bot:** `~/coachbot` → `docker compose` in `coach_bot/`
- **Weekly cron:** `deploy/run_weekly_plan.sh` (Sun 17:45 Melbourne)

## Session library

Curated erg sessions live in `erg_strava/data/erg_sessions/curated/`. Promote a logged session:

```bash
python -m session_library promote --score-id <id> --athlete-id <id> --cache-dir ./erg_strava/erg_strava_cache
python -m session_library list
python -m session_library validate
```

## Tests

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/ coach_bot/tests/ -q
```
