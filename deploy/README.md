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

`deploy/run_weekly_plan.sh` fast-forwards this clone (`git pull --ff-only`) before the plan run. A failed pull is logged and the job continues on the current checkout. The checkout needs a git remote and an upstream branch; an rsync tree with no `.git` skips the pull.

```cron
45 17 * * 0 /home/USER/coachbot/deploy/run_weekly_plan.sh >> /home/USER/coachbot/erg_strava/cron.log 2>&1
```

### Cron (bot image — daily 05:15)

The Zulip bot does not restart on a timer. Compose sets `restart: unless-stopped`, which only relaunches a stopped container, and that container still runs the image from the last build. Source is copied into the image, so a host `git pull` does not change the running bot until a rebuild.

Add this crontab line so the bot rebuilds when new commits arrive (05:15 server local time). The job leaves the current container running when the checkout is already current, and when `docker compose` build fails.

```cron
15 5 * * * /home/USER/coachbot/deploy/update_bot.sh >> /home/USER/coachbot/erg_strava/cron.log 2>&1
```

The cron user needs Docker socket access, a clean tracked worktree, and a remote that can fast-forward.

Manual rebuild:

```bash
cd ~/coachbot
bash deploy/restart-bot.sh
```

### Maintenance mode

Set `maintenance_mode: true` in `erg_strava/config.yaml`. The Sunday job prints a line and exits before sync, the plot, and weekly plan generation. Athletes ask the bot for one session instead.

The bot container has to see the same `suuntool` binary and Suunto session the host cron uses. `coach_bot/docker-compose.yml` mounts them read-only at `/usr/local/bin/suuntool` and `/suunto/session.json`, and sets `SUUNTOOL_SESSION_FILE`. Override the host paths with `SUUNTOOL_BIN` and `SUUNTOOL_SESSION` if they are not `bin/suuntool` and `~/.config/suuntool/session.json`. Both files must exist before `docker compose up`. If config sets an absolute `suunto.session_file` that is not mounted, that sync fails and the bot uses the cache.

### Sync from a dev machine

```bash
rsync -avz --exclude .venv --exclude erg_strava/erg_strava_cache \
  --exclude erg_strava/config.yaml --exclude coach_bot/.env --exclude zuliprc \
  /path/to/coachbot/ USER@HOST:~/coachbot/
```
