"""Route Zulip messages to Kagi Q&A, erg score logging, or adjustment queue."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import zulip

from generate_training_plan import (
    _local_now,
    answer_erg_score_coaching,
    build_athlete_training_context_for_coach,
    delete_erg_score_record,
    delete_gym_log_record,
    find_erg_score_by_id,
    find_erg_score_for_reaction_message,
    find_gym_log_by_id,
    find_gym_log_for_reaction_message,
    find_latest_elaboration_pending_erg_score,
    local_datetime_from_timestamp,
    enqueue_plan_adjustment,
    format_gym_log_confirmation,
    format_gym_session_comparison,
    interpret_coach_message_with_kagi,
    prescribed_gym_section_for_log,
    mark_erg_score_elaboration_sent,
    missing_plan_reply,
    plan_for_date,
    record_erg_score_from_images,
    record_erg_score_from_text,
    record_gym_sessions_from_zulip_for_athletes,
    apply_rpe_follow_up_from_zulip,
    set_erg_score_coach_reply_message_id,
    set_gym_log_coach_reply_message_id,
    week_for_date,
    _parse_erg_score_session_date,
    find_erg_score_by_zulip_message,
    infer_makeup_prescribed_date,
)
from erg_prescription_compare import (
    format_erg_session_comparison,
    format_week_zone_volume_progress,
    prescribed_erg_section_for_log,
)

from coach_bot.config import (
    CoachAthleteCfg,
    format_profile_update_confirmation,
    format_unmatched_sender_help,
    get_config_path,
    listens_all_topics,
    load_bot_config,
    resolve_athlete_for_sender,
    resolve_coach_subject,
    resolve_gym_log_recipients,
    update_athlete_profile_in_config,
)
from coach_bot.erg_score import (
    looks_like_erg_score_text,
    references_nearby_erg_screenshot,
    wants_erg_coaching_elaboration,
)
from coach_bot.followup_triage import should_reply_to_followup
from coach_bot.intents import strip_zulip_mentions, truncate_for_zulip
from coach_bot.listen_window import (
    activate_listen_window as persist_listen_window,
    listen_state_path,
    listen_window_open,
    load_listen_state,
    save_listen_state,
)
from coach_bot.zulip_context import (
    collect_same_sender_session_images,
    fetch_topic_context,
    find_recent_sender_image_message,
)
from coach_bot.zulip_uploads import (
    download_zulip_upload,
    extract_image_upload_urls,
    strip_upload_markdown,
)


class CoachMessageHandler:
    def __init__(
        self,
        cache_dir: Path,
        *,
        bot_user_id: int,
        zulip_client: Optional[zulip.Client] = None,
        kagi_token: str = "",
        zulip_stream: str = "general",
        zulip_topic: str = "",
        bot_full_name: str = "",
        athletes: Optional[List[CoachAthleteCfg]] = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.bot_user_id = bot_user_id
        self.zulip_client = zulip_client
        self.kagi_token = kagi_token.strip()
        self.zulip_stream = zulip_stream
        self.zulip_topic = zulip_topic
        self.athletes = list(athletes or [])
        self._bot_mention_res = _bot_mention_patterns(bot_full_name)
        self._pending_erg_log: Optional[tuple[int, str]] = None
        self._pending_gym_log: Optional[list[tuple[int, str]]] = None

    def consume_pending_erg_log(self) -> Optional[tuple[int, str]]:
        pending = self._pending_erg_log
        self._pending_erg_log = None
        return pending

    def consume_pending_gym_log(self) -> Optional[list[tuple[int, str]]]:
        pending = self._pending_gym_log
        self._pending_gym_log = None
        return pending

    def register_erg_log_coach_reply(self, coach_reply_message_id: int) -> None:
        pending = self.consume_pending_erg_log()
        if pending is None:
            return
        athlete_id, score_id = pending
        set_erg_score_coach_reply_message_id(
            self.cache_dir,
            athlete_id,
            score_id,
            coach_reply_message_id,
        )

    def register_gym_log_coach_reply(self, coach_reply_message_id: int) -> None:
        pending = self.consume_pending_gym_log()
        if not pending:
            return
        for athlete_id, log_id in pending:
            set_gym_log_coach_reply_message_id(
                self.cache_dir,
                athlete_id,
                log_id,
                coach_reply_message_id,
            )

    def _message_topic(self, message: Dict[str, Any]) -> str:
        return (message.get("subject") or "").strip()

    def in_scope_stream(self, message: Dict[str, Any]) -> bool:
        if message.get("type") != "stream":
            return False
        display = message.get("display_recipient") or ""
        if isinstance(display, str) and display != self.zulip_stream:
            return False
        if listens_all_topics(self.zulip_topic):
            return True
        return self._message_topic(message) == self.zulip_topic.strip()

    def in_scope_private(self, message: Dict[str, Any]) -> bool:
        """Direct messages from athletes mapped in config.yaml."""
        if message.get("type") != "private":
            return False
        return self._resolve_athlete(message) is not None

    def bot_mentioned(self, message: Dict[str, Any]) -> bool:
        for uid in message.get("mentions") or []:
            if int(uid) == self.bot_user_id:
                return True
        raw = message.get("content") or ""
        return any(pattern.search(raw) for pattern in self._bot_mention_res)

    def handle(self, message: Dict[str, Any]) -> Optional[str]:
        is_private = message.get("type") == "private"
        if not is_private and not self.in_scope_stream(message):
            return None
        if is_private and not self.in_scope_private(message):
            return None
        raw = (message.get("content") or "").strip()
        image_urls = extract_image_upload_urls(raw)
        if not raw and not image_urls:
            return None
        body = strip_zulip_mentions(strip_upload_markdown(raw))
        ref = self._reference_local_datetime(message)

        if is_private:
            if image_urls:
                return self._handle_erg_score_screenshot(message, image_urls, ref, body)
            athlete = self._resolve_athlete(message)
            if body and athlete and wants_erg_coaching_elaboration(body):
                elaboration = self._handle_erg_score_elaboration(
                    message, ref, body, athlete
                )
                if elaboration is not None:
                    return elaboration
            if body and looks_like_erg_score_text(body):
                return self._handle_erg_score_text(message, ref, body)
            if body:
                rpe_reply = self._handle_gym_rpe_follow_up(body, message, athlete)
                if rpe_reply is not None:
                    return rpe_reply
            if body:
                return self._reply_kagi(body, ref, message, private_dm=True)
            return None

        mentioned = self.bot_mentioned(message)
        if image_urls and mentioned:
            return self._handle_erg_score_screenshot(message, image_urls, ref, body)

        if mentioned and body:
            return self._handle_stream_body(body, message, ref)

        if body and self._listen_window_open(message):
            from gym_program import parse_rpe_follow_up_reply

            if parse_rpe_follow_up_reply(body) is not None:
                athlete = self._resolve_athlete(message)
                return self._handle_gym_rpe_follow_up(body, message, athlete)
            if not self._triage_followup(body):
                return None
            return self._handle_stream_body(body, message, ref)

        return None

    def _handle_stream_body(
        self, body: str, message: Dict[str, Any], ref: datetime
    ) -> Optional[str]:
        athlete = self._resolve_athlete(message)
        if athlete and wants_erg_coaching_elaboration(body):
            elaboration = self._handle_erg_score_elaboration(
                message, ref, body, athlete
            )
            if elaboration is not None:
                return elaboration
        if looks_like_erg_score_text(body):
            return self._handle_erg_score_text(message, ref, body)
        rpe_reply = self._handle_gym_rpe_follow_up(body, message, athlete)
        if rpe_reply is not None:
            return rpe_reply
        nearby = self._try_handle_nearby_erg_screenshot(message, ref, body)
        if nearby is not None:
            return nearby
        return self._reply_kagi(body, ref, message)

    def activate_listen_window(
        self, message: Dict[str, Any], *, now: Optional[datetime] = None
    ) -> None:
        if message.get("type") != "stream":
            return
        stream = message.get("display_recipient") or self.zulip_stream
        if not isinstance(stream, str) or not stream:
            return
        path = listen_state_path(self.cache_dir)
        state = load_listen_state(path)
        persist_listen_window(
            state,
            stream,
            self._message_topic(message),
            now=now or datetime.now(timezone.utc),
        )
        save_listen_state(path, state)

    def _listen_window_open(
        self, message: Dict[str, Any], *, now: Optional[datetime] = None
    ) -> bool:
        stream = message.get("display_recipient") or self.zulip_stream
        if not isinstance(stream, str):
            return False
        state = load_listen_state(listen_state_path(self.cache_dir))
        return listen_window_open(
            state,
            stream,
            self._message_topic(message),
            now=now or datetime.now(timezone.utc),
        )

    def _triage_followup(self, body: str) -> bool:
        token = self.kagi_token
        if not token:
            return should_reply_to_followup(body, use_llm=False)
        try:
            from openrouter_client import call_openrouter
        except ImportError:
            return False

        def llm_call(system: str, user: str) -> str:
            return call_openrouter(
                system=system, user=user, api_key=token, timeout=30
            )

        return should_reply_to_followup(body, llm_call=llm_call)

    def handle_reaction(self, event: Dict[str, Any]) -> Optional[str]:
        """Undo an erg or gym log when the athlete thumbs-downs the coach confirmation."""
        if event.get("type") != "reaction" or event.get("op") != "add":
            return None
        if not _is_thumbs_down_reaction(event):
            return None
        reactor_user_id = _reaction_user_id(event)
        if reactor_user_id is not None and reactor_user_id == self.bot_user_id:
            return None
        message_id = event.get("message_id")
        if message_id is None:
            return None
        reacted_message_id = int(message_id)

        erg_reply = self._handle_erg_thumbs_down(event, reacted_message_id)
        if erg_reply is not None:
            return erg_reply
        return self._handle_gym_thumbs_down(event, reacted_message_id)

    def _handle_erg_thumbs_down(
        self, event: Dict[str, Any], reacted_message_id: int
    ) -> Optional[str]:
        found = find_erg_score_for_reaction_message(
            self.cache_dir, reacted_message_id
        )
        if found is None:
            found = self._find_erg_log_from_coach_message(reacted_message_id)
        if found is None:
            if self._message_looks_like_erg_log_confirmation(reacted_message_id):
                return (
                    "I couldn't find a stored erg log linked to this message "
                    "(it may already have been removed)."
                )
            return None

        athlete_id, record = found
        auth = self._authorize_log_removal(event, athlete_id)
        if auth is not None:
            return auth

        score_id = str(record.get("id") or "")
        if not score_id:
            return None
        reactor = self._resolve_reactor_from_reaction(event)
        athlete_label = str(record.get("athlete_label") or (reactor.label if reactor else ""))
        if not delete_erg_score_record(
            self.cache_dir,
            athlete_id,
            score_id,
            athlete_label=athlete_label,
        ):
            print(
                f"Thumbs-down delete failed for erg score {score_id} "
                f"(athlete {athlete_id})",
                flush=True,
            )
            return (
                f"I found erg log `{score_id}` but couldn't delete it from disk. "
                "Check bot file permissions on the erg score cache."
            )
        session_day = record.get("session_date") or "?"
        return (
            f"Removed the logged erg session (`{score_id}`, {session_day}). "
            "Send a corrected screenshot when you're ready."
        )

    def _handle_gym_thumbs_down(
        self, event: Dict[str, Any], reacted_message_id: int
    ) -> Optional[str]:
        reactor = self._resolve_reactor_from_reaction(event)
        if reactor is None:
            user = event.get("user") or {}
            return format_unmatched_sender_help(
                sender_email=str(user.get("email") or ""),
                sender_full_name=str(user.get("full_name") or ""),
                sender_id=_reaction_user_id(event),
            )
        found = find_gym_log_for_reaction_message(
            self.cache_dir, reacted_message_id, athlete_id=reactor.id
        )
        if found is None:
            found = self._find_gym_log_from_coach_message(
                reacted_message_id, athlete_id=reactor.id
            )
        if found is None:
            if self._message_looks_like_gym_log_confirmation(reacted_message_id):
                return (
                    "Only the athlete who logged that session can remove it with 👎."
                )
            return None

        athlete_id, record = found
        auth = self._authorize_log_removal(event, athlete_id)
        if auth is not None:
            return auth

        log_id = str(record.get("id") or "")
        if not log_id:
            return None
        if not delete_gym_log_record(self.cache_dir, athlete_id, log_id):
            print(
                f"Thumbs-down delete failed for gym log {log_id} "
                f"(athlete {athlete_id})",
                flush=True,
            )
            return (
                f"I found gym log `{log_id}` but couldn't delete it from disk. "
                "Check bot file permissions on the gym log cache."
            )
        session_day = record.get("session_date") or "?"
        return (
            f"Removed the logged gym session (`{log_id}`, {session_day}). "
            "Send a corrected workout when you're ready."
        )

    def _authorize_log_removal(
        self, event: Dict[str, Any], athlete_id: int
    ) -> Optional[str]:
        """Return an error reply when removal is not allowed, else None."""
        reactor = self._resolve_reactor_from_reaction(event)
        if reactor is None:
            user = event.get("user") or {}
            return format_unmatched_sender_help(
                sender_email=str(user.get("email") or ""),
                sender_full_name=str(user.get("full_name") or ""),
                sender_id=_reaction_user_id(event),
            )
        if reactor.id != athlete_id:
            return (
                "Only the athlete who logged that session can remove it with 👎."
            )
        return None

    def _resolve_reactor_from_reaction(
        self, event: Dict[str, Any]
    ) -> Optional[CoachAthleteCfg]:
        user = event.get("user") or {}
        user_id = _reaction_user_id(event)
        _, _, _, _, _, athletes = load_bot_config()
        return resolve_athlete_for_sender(
            athletes or self.athletes,
            sender_email=str(user.get("email") or ""),
            sender_full_name=str(user.get("full_name") or ""),
            sender_id=user_id,
        )

    def _message_looks_like_erg_log_confirmation(self, message_id: int) -> bool:
        if not self.zulip_client:
            return False
        try:
            raw = self.zulip_client.get_raw_message(message_id)
        except Exception:
            return False
        if raw.get("result") != "success":
            return False
        message = raw.get("message") or {}
        if int(message.get("sender_id") or 0) != self.bot_user_id:
            return False
        content = str(message.get("content") or "")
        return bool(
            _parse_logged_erg_score_id(content)
            or "**Logged for " in content
        )

    def _find_erg_log_from_coach_message(
        self, coach_reply_message_id: int
    ) -> Optional[tuple[int, Dict[str, Any]]]:
        if not self.zulip_client:
            return None
        try:
            raw = self.zulip_client.get_raw_message(coach_reply_message_id)
        except Exception as exc:
            print(
                f"Could not load coach message {coach_reply_message_id}: {exc}",
                flush=True,
            )
            return None
        if raw.get("result") != "success":
            return None
        message = raw.get("message") or {}
        if int(message.get("sender_id") or 0) != self.bot_user_id:
            return None
        score_id = _parse_logged_erg_score_id(str(message.get("content") or ""))
        if not score_id:
            return None
        _, _, _, _, _, athletes = load_bot_config()
        for athlete in athletes or self.athletes:
            record = find_erg_score_by_id(self.cache_dir, athlete.id, score_id)
            if record is not None:
                return athlete.id, record
        return None

    def _find_gym_log_from_coach_message(
        self,
        coach_reply_message_id: int,
        *,
        athlete_id: Optional[int] = None,
    ) -> Optional[tuple[int, Dict[str, Any]]]:
        if not self.zulip_client:
            return None
        try:
            raw = self.zulip_client.get_raw_message(coach_reply_message_id)
        except Exception as exc:
            print(
                f"Could not load coach message {coach_reply_message_id}: {exc}",
                flush=True,
            )
            return None
        if raw.get("result") != "success":
            return None
        message = raw.get("message") or {}
        if int(message.get("sender_id") or 0) != self.bot_user_id:
            return None
        log_ids = _parse_logged_gym_session_ids(str(message.get("content") or ""))
        if not log_ids:
            return None
        _, _, _, _, _, athletes = load_bot_config()
        search_athletes = athletes or self.athletes
        if athlete_id is not None:
            search_athletes = [a for a in search_athletes if a.id == athlete_id]
        for log_id in log_ids:
            for athlete in search_athletes:
                record = find_gym_log_by_id(self.cache_dir, athlete.id, log_id)
                if record is not None:
                    return athlete.id, record
        return None

    def _message_looks_like_gym_log_confirmation(self, message_id: int) -> bool:
        if not self.zulip_client:
            return False
        try:
            raw = self.zulip_client.get_raw_message(message_id)
        except Exception:
            return False
        if raw.get("result") != "success":
            return False
        message = raw.get("message") or {}
        if int(message.get("sender_id") or 0) != self.bot_user_id:
            return False
        content = str(message.get("content") or "")
        return bool(
            _parse_logged_gym_session_id(content)
            or "**Logged gym session**" in content
        )

    def _handle_erg_score_screenshot(
        self,
        message: Dict[str, Any],
        image_urls: List[str],
        ref: datetime,
        body: str,
    ) -> str:
        if not self.kagi_token:
            return (
                "Erg score logging requires an LLM: set `OPENROUTER_API_KEY` in the coach bot environment."
            )
        if not self.zulip_client:
            return "Cannot download screenshot: Zulip client not configured."
        athlete = self._resolve_athlete(message)
        if athlete is None:
            sender_id = message.get("sender_id")
            return format_unmatched_sender_help(
                sender_email=str(message.get("sender_email") or ""),
                sender_full_name=str(message.get("sender_full_name") or ""),
                sender_id=int(sender_id) if sender_id is not None else None,
            )
        msg_id = message.get("id")
        zulip_message_id = int(msg_id) if msg_id is not None else None
        session_urls = list(image_urls)
        session_body = body
        if (
            message.get("type") == "stream"
            and len(session_urls) < 3
            and msg_id is not None
            and message.get("sender_id") is not None
            and message.get("timestamp") is not None
        ):
            collected = collect_same_sender_session_images(
                self.zulip_client,
                self.zulip_stream,
                self._message_topic(message),
                sender_id=int(message["sender_id"]),
                around_message_id=int(msg_id),
                around_timestamp=float(message["timestamp"]),
                current_image_urls=session_urls,
                bot_user_id=self.bot_user_id,
                skip_message=self.bot_mentioned,
            )
            session_urls = collected.image_urls
            if collected.adjacent_text:
                session_body = " ".join(
                    part for part in (collected.adjacent_text, body) if part
                ).strip()
        images: List[tuple[bytes, str]] = []
        download_errors: List[str] = []
        for url in session_urls[:3]:
            try:
                image_bytes = download_zulip_upload(self.zulip_client, url)
                images.append((image_bytes, _image_mime_for_url(url)))
            except Exception as exc:
                download_errors.append(str(exc))
        if not images:
            detail = download_errors[0] if download_errors else "Could not download image."
            return f"Could not read erg score from screenshot: {detail}"
        logged_date = ref.date()
        prescribed_date = (
            infer_makeup_prescribed_date(session_body, logged_date) or logged_date
        )
        prescribed_session_text = prescribed_erg_section_for_log(
            self.cache_dir, athlete.id, prescribed_date
        ) or ""
        try:
            if zulip_message_id is not None:
                record = find_erg_score_by_zulip_message(
                    self.cache_dir, athlete.id, zulip_message_id
                )
            else:
                record = None
            if record is None:
                record, _ = record_erg_score_from_images(
                    self.cache_dir,
                    athlete.id,
                    athlete.label,
                    images,
                    self.kagi_token,
                    zulip_message_id=zulip_message_id,
                    zulip_sender_email=str(message.get("sender_email") or ""),
                    recorded_at=ref,
                    session_hint_date=logged_date,
                    athlete_message=session_body,
                    prescribed_session_text=prescribed_session_text,
                )
        except ValueError as exc:
            return str(exc)
        except Exception as exc:
            return f"Sorry, erg score logging failed: {exc}"

        return self._finish_erg_score_log(message, ref, session_body, athlete, record)

    def _try_handle_nearby_erg_screenshot(
        self,
        message: Dict[str, Any],
        ref: datetime,
        body: str,
    ) -> Optional[str]:
        """Follow-up @coach text pointing at a screenshot in a recent prior message."""
        if not references_nearby_erg_screenshot(body):
            return None
        if not self.zulip_client:
            return None
        sender_id = message.get("sender_id")
        msg_id = message.get("id")
        ts = message.get("timestamp")
        if sender_id is None or msg_id is None or ts is None:
            return None
        found = find_recent_sender_image_message(
            self.zulip_client,
            self.zulip_stream,
            self._message_topic(message),
            sender_id=int(sender_id),
            before_message_id=int(msg_id),
            before_timestamp=float(ts),
        )
        if found is None:
            return None
        prior_message, image_urls = found
        prior_body = strip_zulip_mentions(
            strip_upload_markdown(str(prior_message.get("content") or ""))
        )
        combined_body = " ".join(part for part in (prior_body, body) if part).strip()
        return self._handle_erg_score_screenshot(
            prior_message,
            image_urls,
            ref,
            combined_body,
        )

    def _handle_erg_score_text(
        self,
        message: Dict[str, Any],
        ref: datetime,
        body: str,
    ) -> str:
        if not self.kagi_token:
            return (
                "Erg score logging requires an LLM: set `OPENROUTER_API_KEY` in the coach bot environment."
            )
        athlete = self._resolve_athlete(message)
        if athlete is None:
            sender_id = message.get("sender_id")
            return format_unmatched_sender_help(
                sender_email=str(message.get("sender_email") or ""),
                sender_full_name=str(message.get("sender_full_name") or ""),
                sender_id=int(sender_id) if sender_id is not None else None,
            )
        msg_id = message.get("id")
        zulip_message_id = int(msg_id) if msg_id is not None else None
        try:
            if zulip_message_id is not None:
                record = find_erg_score_by_zulip_message(
                    self.cache_dir, athlete.id, zulip_message_id
                )
            else:
                record = None
            if record is None:
                record, _ = record_erg_score_from_text(
                    self.cache_dir,
                    athlete.id,
                    athlete.label,
                    body,
                    self.kagi_token,
                    zulip_message_id=zulip_message_id,
                    zulip_sender_email=str(message.get("sender_email") or ""),
                    recorded_at=ref,
                    session_hint_date=ref.date(),
                )
        except ValueError as exc:
            return str(exc)
        except Exception as exc:
            return f"Sorry, erg score logging failed: {exc}"
        return self._finish_erg_score_log(message, ref, body, athlete, record)

    def _handle_erg_score_elaboration(
        self,
        message: Dict[str, Any],
        ref: datetime,
        body: str,
        athlete: CoachAthleteCfg,
    ) -> Optional[str]:
        pending = find_latest_elaboration_pending_erg_score(
            self.cache_dir, athlete.id
        )
        if pending is None:
            return None
        session_date = _parse_erg_score_session_date(pending) or ref.date()
        plan_record = plan_for_date(self.cache_dir, session_date)
        topic_context = self._topic_context(message)
        try:
            coaching = answer_erg_score_coaching(
                pending,
                plan_record,
                self.cache_dir,
                athlete.id,
                athlete.label,
                self.kagi_token,
                local_datetime=ref,
                topic_context=topic_context or None,
                athlete_message=body or None,
                brief=False,
            )
        except Exception as exc:
            return f"Could not elaborate on that session: {exc}"
        score_id = str(pending.get("id") or "")
        if score_id:
            mark_erg_score_elaboration_sent(self.cache_dir, athlete.id, score_id)
        prescription_block = format_erg_session_comparison(
            self.cache_dir, athlete.id, pending, session_date
        )
        zone_block = format_week_zone_volume_progress(
            self.cache_dir,
            athlete.id,
            session_date,
        )
        deterministic = ""
        if prescription_block:
            deterministic += prescription_block + "\n\n"
        if zone_block:
            deterministic += zone_block + "\n\n"
        header = (
            f"**Session detail for {athlete.label}** "
            f"(`{score_id or '?'}`, "
            f"{pending.get('session_date') or session_date.isoformat()})\n\n"
        )
        return truncate_for_zulip(header + deterministic + coaching.strip())

    def _react_thumbs_up(self, message: Dict[str, Any]) -> None:
        if not self.zulip_client:
            return
        msg_id = message.get("id")
        if msg_id is None:
            return
        try:
            result = self.zulip_client.add_reaction(
                {"message_id": int(msg_id), "emoji_name": "+1"}
            )
            if result.get("result") != "success":
                print(f"Zulip +1 reaction failed: {result}", flush=True)
        except Exception as exc:
            print(f"Zulip +1 reaction failed: {exc}", flush=True)

    def _finish_erg_score_log(
        self,
        message: Dict[str, Any],
        ref: datetime,
        body: str,
        athlete: CoachAthleteCfg,
        record: Dict[str, Any],
    ) -> str:
        self._react_thumbs_up(message)
        session_date = _parse_erg_score_session_date(record) or ref.date()
        prescribed_date = (
            infer_makeup_prescribed_date(body, session_date) or session_date
        )
        plan_record = plan_for_date(self.cache_dir, session_date)
        try:
            coaching = answer_erg_score_coaching(
                record,
                plan_record,
                self.cache_dir,
                athlete.id,
                athlete.label,
                self.kagi_token,
                local_datetime=ref,
                topic_context=None,
                athlete_message=body or None,
                brief=True,
            )
        except Exception as exc:
            coaching = f"Logged your score, but coaching feedback failed: {exc}"

        prescription_block = format_erg_session_comparison(
            self.cache_dir,
            athlete.id,
            record,
            session_date,
            prescribed_session_date=(
                prescribed_date if prescribed_date != session_date else None
            ),
        )
        zone_block = format_week_zone_volume_progress(
            self.cache_dir,
            athlete.id,
            session_date,
        )
        deterministic = ""
        if prescription_block:
            deterministic += prescription_block + "\n\n"
        if zone_block:
            deterministic += zone_block + "\n\n"

        metrics = record.get("metrics") or {}
        header = (
            f"**Logged for {athlete.label}** (`{record.get('id', '?')}`, "
            f"{record.get('session_date') or session_date.isoformat()})\n\n"
        )
        footer_lines: List[str] = []
        if metrics.get("distance_m"):
            footer_lines.append(f"{metrics['distance_m']} m")
        if metrics.get("avg_split_500_fmt"):
            footer_lines.append(f"avg {metrics['avg_split_500_fmt']}")
        if metrics.get("workout_type"):
            footer_lines.append(str(metrics["workout_type"]))
        footer = ""
        if footer_lines:
            footer = "\n\n_" + " · ".join(footer_lines) + "_"
        if body and not looks_like_erg_score_text(body):
            footer += f"\n\n_Athlete note: {body}_"
        score_id = str(record.get("id") or "")
        if score_id:
            self._pending_erg_log = (athlete.id, score_id)
        return truncate_for_zulip(
            header + deterministic + coaching.strip() + footer
        )

    def _handle_gym_rpe_follow_up(
        self,
        body: str,
        message: Dict[str, Any],
        athlete: Optional[CoachAthleteCfg],
    ) -> Optional[str]:
        from gym_program import (
            format_rpe_recorded_confirmation,
            parse_rpe_follow_up_reply,
        )

        rpe = parse_rpe_follow_up_reply(body)
        if rpe is None:
            return None
        if athlete is None:
            return format_unmatched_sender_help(
                sender_email=str(message.get("sender_email") or ""),
                sender_full_name=str(message.get("sender_full_name") or ""),
                sender_id=int(message["sender_id"])
                if message.get("sender_id") is not None
                else None,
            )
        records = apply_rpe_follow_up_from_zulip(
            self.cache_dir,
            athlete.id,
            rpe,
            sender_email=str(message.get("sender_email") or ""),
        )
        if not records:
            return (
                "I couldn't find a recent gym log missing RPE to attach that to."
            )
        return format_rpe_recorded_confirmation(records, rpe)

    def _resolve_athlete(self, message: Dict[str, Any]) -> Optional[CoachAthleteCfg]:
        """Reload athlete map from config so edits apply without restarting the bot."""
        _, _, _, _, _, athletes = load_bot_config()
        sender_id = message.get("sender_id")
        return resolve_athlete_for_sender(
            athletes or self.athletes,
            sender_email=str(message.get("sender_email") or ""),
            sender_full_name=str(message.get("sender_full_name") or ""),
            sender_id=int(sender_id) if sender_id is not None else None,
        )

    def _reference_local_datetime(self, message: Dict[str, Any]) -> datetime:
        ts = message.get("timestamp")
        if ts is not None:
            return local_datetime_from_timestamp(float(ts))
        return _local_now()

    def _topic_context(self, message: Dict[str, Any]) -> str:
        if not self.zulip_client:
            return ""
        msg_id = message.get("id")
        try:
            return fetch_topic_context(
                self.zulip_client,
                self.zulip_stream,
                self._message_topic(message),
                exclude_message_id=int(msg_id) if msg_id is not None else None,
            )
        except Exception as exc:
            print(f"Zulip topic context fetch failed: {exc}", flush=True)
            return ""

    def _reply_kagi(
        self,
        text: str,
        ref: datetime,
        message: Dict[str, Any],
        *,
        private_dm: bool = False,
    ) -> str:
        today = ref.date()
        record = plan_for_date(self.cache_dir, today)
        if not record or not record.plan_text.strip():
            return missing_plan_reply(week_for_date(today).week_id)
        if not self.kagi_token:
            return (
                "Coach Q&A is not configured: set `OPENROUTER_API_KEY` in the coach bot environment."
            )
        topic_context = "" if private_dm else self._topic_context(message)
        _, _, _, _, _, athletes = load_bot_config()
        sender, subject = resolve_coach_subject(
            athletes or self.athletes,
            sender_email=str(message.get("sender_email") or ""),
            sender_full_name=str(message.get("sender_full_name") or ""),
            sender_id=int(message["sender_id"])
            if message.get("sender_id") is not None
            else None,
            message_content=str(message.get("content") or ""),
            bot_user_id=self.bot_user_id,
        )
        if private_dm and sender is not None:
            subject = sender
        sender_label = (
            sender.label
            if sender
            else str(message.get("sender_full_name") or message.get("sender_email") or "")
        )
        subject_label = subject.label if subject else sender_label
        subject_training_context = ""
        if subject:
            subject_training_context = build_athlete_training_context_for_coach(
                self.cache_dir,
                subject.id,
                subject.label,
                today,
            )
        msg_id = message.get("id")
        zulip_message_id = int(msg_id) if msg_id is not None else None
        try:
            interpretation = interpret_coach_message_with_kagi(
                text,
                record,
                self.cache_dir,
                self.kagi_token,
                local_datetime=ref,
                topic_context=topic_context or None,
                sender_label=sender_label or None,
                subject_label=subject_label or None,
                subject_training_context=subject_training_context or None,
                subject_hr_context=subject.hr_zone_context_text() if subject else None,
                subject_athlete_id=subject.id if subject else None,
                private_dm=private_dm,
            )
        except Exception as exc:
            return f"Sorry, I could not reach the LLM API: {exc}"
        reply = interpretation.reply.strip()
        recipients = resolve_gym_log_recipients(
            athletes or self.athletes,
            sender_email=str(message.get("sender_email") or ""),
            sender_full_name=str(message.get("sender_full_name") or ""),
            sender_id=int(message["sender_id"])
            if message.get("sender_id") is not None
            else None,
            message_content=str(message.get("content") or ""),
            bot_user_id=self.bot_user_id,
            private_dm=private_dm,
        )
        if interpretation.intent == "gym_session_log" and recipients:
            session_hint = today
            if interpretation.session_date:
                try:
                    session_hint = date.fromisoformat(
                        interpretation.session_date[:10]
                    )
                except ValueError:
                    session_hint = today
            try:
                gym_records = record_gym_sessions_from_zulip_for_athletes(
                    self.cache_dir,
                    [(a.id, a.label, a.body_weight_kg) for a in recipients],
                    interpretation.workout_text or text,
                    self.kagi_token,
                    zulip_message_id=zulip_message_id,
                    zulip_sender_email=str(message.get("sender_email") or ""),
                    recorded_at=ref,
                    session_hint_date=session_hint,
                    rpe_transcript=text,
                )
                pending = [
                    (int(rec["athlete_id"]), str(rec["id"]))
                    for rec in gym_records
                    if rec.get("id")
                ]
                if pending:
                    self._pending_gym_log = pending
                self._react_thumbs_up(message)
                athlete_by_id = {a.id: a for a in recipients}
                from gym_program import format_rpe_follow_up, gym_log_missing_rpe

                for gym_record in gym_records:
                    athlete = athlete_by_id.get(int(gym_record.get("athlete_id") or 0))
                    parts: List[str] = []
                    if athlete is not None:
                        parts.append(f"**{athlete.label}**")
                    log_note = format_gym_log_confirmation(gym_record)
                    parts.append(log_note)
                    if athlete is not None:
                        prescribed_section = prescribed_gym_section_for_log(
                            self.cache_dir,
                            athlete.id,
                            session_hint,
                        )
                        comparison_note = format_gym_session_comparison(
                            self.cache_dir,
                            athlete.id,
                            gym_record,
                            prescribed_section=prescribed_section,
                            body_weight_kg=athlete.body_weight_kg,
                        )
                        if comparison_note:
                            parts.append(comparison_note)
                    block = "\n\n".join(parts)
                    if block and block not in reply:
                        reply = f"{reply}\n\n{block}" if reply else block
                credited = {
                    int(rec.get("athlete_id") or 0) for rec in gym_records
                }
                for athlete in recipients:
                    if athlete.id not in credited:
                        fail_note = (
                            f"(Could not log gym session for {athlete.label}.)"
                        )
                        if fail_note not in reply:
                            reply = (
                                f"{reply}\n\n{fail_note}" if reply else fail_note
                            )
                if gym_records and gym_log_missing_rpe(gym_records[0]):
                    rpe_note = format_rpe_follow_up()
                    if rpe_note not in reply:
                        reply = f"{reply}\n\n{rpe_note}" if reply else rpe_note
            except ValueError as exc:
                reply = (
                    f"{reply}\n\n(Could not log gym session: {exc})"
                    if reply
                    else f"Could not log gym session: {exc}"
                )
            except Exception as exc:
                reply = (
                    f"{reply}\n\n(Gym session logging failed: {exc})"
                    if reply
                    else f"Gym session logging failed: {exc}"
                )
        elif interpretation.intent == "profile_update" and subject:
            try:
                updated = update_athlete_profile_in_config(
                    get_config_path(),
                    subject.id,
                    body_weight_kg=interpretation.body_weight_kg,
                    max_hr_bpm=interpretation.max_hr_bpm,
                )
                _, _, _, _, _, athletes = load_bot_config()
                refreshed = next(
                    (a for a in athletes if a.id == subject.id),
                    subject,
                )
                note = format_profile_update_confirmation(updated, refreshed)
                if note not in reply:
                    reply = f"{reply}\n\n{note}" if reply else note
            except (PermissionError, OSError) as exc:
                reply = (
                    f"{reply}\n\n(Could not write config.yaml: {exc}. "
                    "Ensure CONFIG_PATH is writable — Docker needs a read-write mount.)"
                    if reply
                    else (
                        "Could not write config.yaml. Ensure CONFIG_PATH is writable "
                        f"({exc})."
                    )
                )
            except ValueError as exc:
                reply = (
                    f"{reply}\n\n(Could not update profile: {exc})"
                    if reply
                    else f"Could not update profile: {exc}"
                )
            except Exception as exc:
                reply = (
                    f"{reply}\n\n(Profile update failed: {exc})"
                    if reply
                    else f"Profile update failed: {exc}"
                )
        elif (
            interpretation.intent == "plan_adjustment"
            and interpretation.pending_adjustment
        ):
            try:
                entry_id = enqueue_plan_adjustment(
                    self.cache_dir,
                    interpretation.pending_adjustment,
                    zulip_message_id=zulip_message_id,
                )
                if entry_id not in reply:
                    reply += (
                        f"\n\n**Queued** for next weekly plan generation "
                        f"(`{entry_id}`)."
                    )
            except ValueError:
                reply += "\n\n(Could not queue plan adjustment: empty text.)"
        return truncate_for_zulip(reply)


_IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
}

_LOGGED_ERG_SCORE_ID_RE = re.compile(
    r"\*\*Logged for .+?\*\* \(`([^`]+)`",
    re.DOTALL,
)
_LOGGED_GYM_SESSION_ID_RE = re.compile(
    r"\*\*Logged gym session\*\* \(`([^`]+)`",
    re.DOTALL,
)
_THUMBS_DOWN_EMOJI_NAMES = frozenset({"thumbs_down", "thumbsdown", "-1"})


def _reaction_user_id(event: Dict[str, Any]) -> Optional[int]:
    user = event.get("user") or {}
    raw = user.get("user_id")
    if raw is None:
        raw = event.get("user_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _is_thumbs_down_reaction(event: Dict[str, Any]) -> bool:
    name = str(event.get("emoji_name") or "").strip().lower()
    if name in _THUMBS_DOWN_EMOJI_NAMES:
        return True
    code = str(event.get("emoji_code") or "").lower()
    return code == "1f44e"


def _parse_logged_erg_score_id(content: str) -> Optional[str]:
    match = _LOGGED_ERG_SCORE_ID_RE.search(content or "")
    if not match:
        return None
    score_id = match.group(1).strip().split(",", 1)[0].strip()
    return score_id or None


def _parse_logged_gym_session_id(content: str) -> Optional[str]:
    ids = _parse_logged_gym_session_ids(content)
    return ids[0] if ids else None


def _parse_logged_gym_session_ids(content: str) -> list[str]:
    ids: list[str] = []
    for match in _LOGGED_GYM_SESSION_ID_RE.finditer(content or ""):
        log_id = match.group(1).strip().split(",", 1)[0].strip()
        if log_id:
            ids.append(log_id)
    return ids


def _image_mime_for_url(url: str) -> str:
    path = url.split("?", 1)[0].lower()
    for ext, mime in _IMAGE_MIME_BY_EXT.items():
        if path.endswith(ext):
            return mime
    return "image/png"


def _bot_mention_patterns(full_name: str) -> list[re.Pattern[str]]:
    names = {full_name.strip(), "coach", "Coach"}
    first = full_name.strip().split()[0] if full_name.strip() else ""
    if first:
        names.add(first)
    patterns: list[re.Pattern[str]] = []
    for name in names:
        if not name:
            continue
        escaped = re.escape(name)
        # Zulip stores bot mentions as @**coach|82** not just @**coach**
        patterns.append(re.compile(rf"@\*{{2}}{escaped}(?:\|\d+)?\*{{2}}", re.I))
    return patterns
