FROM python:3.12-slim

WORKDIR /app

COPY coach_bot/requirements.txt /app/coach_bot/requirements.txt
RUN pip install --no-cache-dir -r /app/coach_bot/requirements.txt

COPY erg_strava/*.py /app/erg_strava/
COPY coach_bot /app/coach_bot
COPY lighties/send_to_zulip.py /app/lighties/send_to_zulip.py
COPY lighties/zulip_topic_context.py /app/lighties/zulip_topic_context.py

ENV PYTHONPATH=/app:/app/erg_strava:/app/lighties
ENV CONFIG_PATH=/app/config.yaml
ENV CACHE_DIR=/data/cache
ENV ZULIPRC_PATH=/app/rrcc-zuliprc

CMD ["python", "-m", "coach_bot.main"]
