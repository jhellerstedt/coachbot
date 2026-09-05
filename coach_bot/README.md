# Coach bot (Zulip + Docker)

Long-running bot that:

- Answers **@-mention messages** via Kagi FastGPT using strategic goals, training summary, the current week's `plan_text`, and recent Zulip topic messages (including "today's session" / reschedules discussed in the thread)
- Logs **@-mention erg score screenshots** to each athlete's performance cache, then replies with coaching vs prescribed session and erg history

Inputs are routed by a cost/accuracy-aware conditional router (`erg_strava/document_router.py`, `process_input`):

- **Text-only** (no image) → OpenRouter Auto Router (`OPENROUTER_MODEL`, default `openrouter/auto`) selects a model for the prompt.
- **Image present** → the screenshot goes straight to a vision-capable model via the same Auto Router (`OPENROUTER_VISION_MODEL`, default `openrouter/auto`) which reads it directly. There is **no local OCR** (no Tesseract/Pillow); the MLLM does the visual reasoning and returns the same JSON metrics the cache expects.
- Logs **gym/strength transcripts** from @-mentions or private DMs (LLM intent `gym_session_log` → parse sets/reps/weights → cache under `athlete_{id}/gym_logs/`)
- Updates **private DM body weight / max HR** (LLM intent `profile_update` → writes `body_weight_kg` / `max_hr_bpm` in `config.yaml`)
- Queues **@-mention plan-change requests** (e.g. "reduce Thursday volume next week") for the next `strava_erg_hr_plot.py` weekly plan run

## Prerequisites

1. Weekly plans in `erg_strava/erg_strava_cache/weekly_plans/` (same `cache_dir` as `config.yaml`; run `strava_erg_hr_plot.py` with `OPENROUTER_API_KEY`).
2. `erg_strava/config.yaml` with `cache_dir`, `plan_timezone`, and per-athlete `zulip_email` (for erg score screenshot logging).
3. **Generic Zulip bot** (not an incoming webhook) and its `zuliprc` at the repo root.
   - Settings → Bots → **Add bot** → **Generic bot** → download `zuliprc`.
   - Incoming webhook bots only post via a URL; they cannot use the event queue this service needs.
   - If `send_to_zulip.py` already has a zuliprc, it may be a different bot type; the coach bot needs its own generic bot (or replace credentials if you use one bot for both).
4. Subscribe that bot to stream `general` (or set `ZULIP_STREAM`). By default the bot listens on **all topics** in that stream; set `ZULIP_TOPIC=project-640` to restrict to one topic.
5. `coach_bot/.env` with `OPENROUTER_API_KEY` (required for coach replies and weekly plan). Copy `coach_bot/.env.example` → `coach_bot/.env`.

## Run with Docker

From the repo root:

```bash
cp coach_bot/.env.example coach_bot/.env
# edit coach_bot/.env — set OPENROUTER_API_KEY
docker compose -f coach_bot/docker-compose.yml up --build
```

Mounts:

- `erg_strava/erg_strava_cache` → plan JSON and adjustment queue (mounted as `/data/cache` in the container)
- `zuliprc` → bot credentials
- `erg_strava/config.yaml` → timezone and cache path

## Run locally

```bash
cp coach_bot/.env.example coach_bot/.env
# edit coach_bot/.env — set OPENROUTER_API_KEY
export PYTHONPATH=".:erg_strava:lighties"
python -m coach_bot.main
```

## Example messages (stream `general`, any topic)

| Message | Behavior |
|---------|----------|
| `@coach today's session` | Kagi answer using plan + recent Zulip topic context (honours reschedules) |
| `@coach` + erg screenshot | Vision MLLM reads the screenshot + logs score, react with :+1:, then **brief** coaching (reply `@coach for more detail` for full analysis) |
| `@coach` + typed erg summary | Same as screenshot when message includes distance, split, duration (no image needed); also reacts :+1: |
| `@coach more detail` (within 72h of a log) | Full coaching on the most recent logged erg session |
| `@coach gym today: **Bench Press:** 8r 40, …` | LLM classifies `gym_session_log` → parsed to `{cache_dir}/athlete_{id}/gym_logs/*.json`; confirmation reply with tonnage |
| `What HR zone is Tuesday erg?` | Kagi answer with plan + strategic context |
| `@coach rate my A/B split: …` | Kagi answer (not limited to `?` or question words) |
| `@CoachBot reduce Thursday volume next week` | Queued in `erg_strava/erg_strava_cache/plan_adjustments/pending.jsonl` |

**Private DMs** (matched athlete via `zulip_email` in config):

| Message | Behavior |
|---------|----------|
| Gym transcript (`**Bench Press:** 8r 40, 5r 50, …`) | Same as stream gym logging (works in DMs too) |
| `body weight 82 kg` / `max HR 185` | LLM classifies `profile_update` → updates athlete row in `config.yaml`; confirmation includes HR zone targets when max HR changes (**DM only**) |
| Erg screenshot or typed erg summary | Same as stream @-mention erg logging |
| `more detail` / `elaborate` (after a log) | Full coaching on the latest logged session |
| Other training question | Coaching reply (no queue unless plan change) |

DM gym logs feed weekly tonnage summaries and adherence reviews alongside Strava/Suunto gym activities.

Adjustments are consumed when the weekly pipeline saves a new plan (`get_kagi_weekly_plan` includes them, then `pending.jsonl` is cleared).

When `strava_erg_hr_plot.py` generates a new weekly plan, it also pulls Zulip messages from the configured plan topic (`project-640` by default) **since the last Strava session end** (or since the previous plan was generated) and sends them to Kagi as feedback context.
