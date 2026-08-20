## Hellpi deployment

Host: `georgemcfly@hellpi`, install path: `~/coachbot`.

### Initial setup (after rsync or git clone)

```bash
cd ~/coachbot
bash deploy/hellpi-setup.sh
```

Copies `config.yaml`, `.env`, `rrcc-zuliprc`, and `erg_strava_cache` from `~/RRC-scripts` if present.

### Cron (weekly plan — Sunday 17:45)

```cron
45 17 * * 0 /home/georgemcfly/coachbot/deploy/run_weekly_plan.sh >> /home/georgemcfly/coachbot/erg_strava/cron.log 2>&1
```

### Docker (Zulip coach bot)

```bash
cd ~/coachbot
bash deploy/hellpi-restart-bot.sh
```

### Sync from dev machine

```bash
rsync -avz --exclude .venv --exclude erg_strava/erg_strava_cache \
  --exclude erg_strava/config.yaml --exclude coach_bot/.env --exclude rrcc-zuliprc \
  /Users/jack/GitHub/coachbot/ georgemcfly@hellpi:~/coachbot/
```
