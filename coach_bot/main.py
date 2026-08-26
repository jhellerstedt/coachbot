#!/usr/bin/env python3
"""Long-running Zulip coach bot (Kagi Q&A with topic context, adjustment queue)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
for sub in (REPO_ROOT, REPO_ROOT / "erg_strava", REPO_ROOT / "lighties"):
    p = str(sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import zulip

from coach_bot.config import listens_all_topics, load_bot_config
from coach_bot.handler import CoachMessageHandler


def _reply(
    client: zulip.Client,
    content: str,
    message: dict,
    *,
    stream: str,
    topic: str,
) -> Optional[int]:
    """Send a Zulip reply; return the new message id on success."""
    if message.get("type") == "private":
        sender_id = message.get("sender_id")
        if sender_id is None:
            print("Zulip send failed: private reply missing sender_id", flush=True)
            return None
        payload = {
            "type": "private",
            "to": [int(sender_id)],
            "content": content,
        }
    else:
        payload = {
            "type": "stream",
            "to": stream,
            "topic": topic,
            "content": content,
        }
    result = client.send_message(payload)
    if result.get("result") != "success":
        print(f"Zulip send failed: {result}", flush=True)
        return None
    msg_id = result.get("id")
    return int(msg_id) if msg_id is not None else None


def main() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.is_file():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path, override=False)
        except ImportError:
            pass
    cache_dir, stream, topic, plan_tz, zuliprc, athletes = load_bot_config()
    if not zuliprc.is_file():
        raise SystemExit(f"Zulip rc not found: {zuliprc}")

    llm_api_key = os.environ.get("OPENROUTER_API_KEY", "")
    config_path = str(zuliprc)
    client = zulip.Client(config_file=config_path)
    profile = client.get_profile()
    if profile.get("result") != "success":
        msg = str(profile.get("msg", ""))
        if "webhook" in msg.lower():
            raise SystemExit(
                "Zulip credentials are for an incoming webhook bot, which cannot "
                "listen for messages.\n\n"
                "Create a **Generic bot** in your Zulip organization "
                "(Settings → Bots → Add bot → Generic bot), download its zuliprc, "
                "subscribe that bot to your stream, and mount that file as "
                "rrcc-zuliprc (or set ZULIPRC_PATH).\n\n"
                f"API response: {profile}"
            )
        raise SystemExit(f"Could not load bot profile: {profile}")
    bot_user_id = int(profile["user_id"])
    bot_email = profile.get("email", "")
    bot_full_name = str(profile.get("full_name") or "")

    handler = CoachMessageHandler(
        cache_dir,
        bot_user_id=bot_user_id,
        zulip_client=client,
        kagi_token=llm_api_key,
        zulip_stream=stream,
        zulip_topic=topic,
        bot_full_name=bot_full_name,
        athletes=athletes,
    )

    topic_label = f"{stream} (all topics)" if listens_all_topics(topic) else f"{stream}/{topic}"
    print(
        f"Coach bot listening on {topic_label} + athlete DMs "
        f"(cache={cache_dir}, tz={plan_tz}, bot={bot_email})",
        flush=True,
    )

    def process_event(event: dict) -> None:
        if event.get("type") == "reaction":
            reply = handler.handle_reaction(event)
            if not reply:
                return
            message_id = event.get("message_id")
            if message_id is None or not handler.zulip_client:
                return
            try:
                raw = handler.zulip_client.get_raw_message(int(message_id))
            except Exception as exc:
                print(f"Could not load message for reaction reply: {exc}", flush=True)
                return
            if raw.get("result") != "success":
                return
            message = raw.get("message") or {}
            reply_topic = (
                (message.get("subject") or topic).strip()
                if message.get("type") == "stream"
                else topic
            )
            coach_msg_id = _reply(handler.zulip_client, reply, message, stream=stream, topic=reply_topic)
            if coach_msg_id is not None:
                handler.register_erg_log_coach_reply(coach_msg_id)
                handler.register_gym_log_coach_reply(coach_msg_id)
                if message.get("type") == "stream":
                    handler.activate_listen_window(message)
            return
        if event.get("type") != "message":
            return
        message = event.get("message") or {}
        if message.get("sender_email") == bot_email:
            return
        reply = handler.handle(message)
        if reply:
            reply_topic = (
                (message.get("subject") or topic).strip()
                if message.get("type") == "stream"
                else topic
            )
            coach_msg_id = _reply(client, reply, message, stream=stream, topic=reply_topic)
            if coach_msg_id is not None:
                handler.register_erg_log_coach_reply(coach_msg_id)
                handler.register_gym_log_coach_reply(coach_msg_id)
                if message.get("type") == "stream":
                    handler.activate_listen_window(message)

    client.call_on_each_event(process_event, event_types=["message", "reaction"])


if __name__ == "__main__":
    main()
