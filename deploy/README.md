## Production deployment

Install path: `~/coachbot` (override with `COACHBOT`). Put `suuntool` on `PATH` or at `~/coachbot/bin/suuntool`.

### Initial setup (after rsync or git clone)

```bash
cd ~/coachbot
bash deploy/setup.sh
```

To copy `config.yaml`, `.env`, `zuliprc`, and `erg_strava_cache` from a previous checkout:

```bash
PREVIOUS_INSTALL=/path/to/old/checkout bash deploy/setup.sh
```

If credentials still use an older filename, rename or symlink them to `zuliprc` at the repo root (or set `ZULIPRC_PATH`).

### Cron (weekly plan — Sunday 17:45)

```cron
45 17 * * 0 /home/USER/coachbot/deploy/run_weekly_plan.sh >> /home/USER/coachbot/erg_strava/cron.log 2>&1
```

### Docker (Zulip coach bot)

```bash
cd ~/coachbot
bash deploy/restart-bot.sh
```

### Sync from a dev machine

```bash
rsync -avz --exclude .venv --exclude erg_strava/erg_strava_cache \
  --exclude erg_strava/config.yaml --exclude coach_bot/.env --exclude zuliprc \
  /path/to/coachbot/ USER@HOST:~/coachbot/
```
