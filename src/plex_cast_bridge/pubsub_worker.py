import asyncio
import json
import logging
import os
import time
from collections import Counter
from pathlib import Path

from google.cloud import pubsub_v1

from . import activity


def get_dead_letter_log_path() -> Path:
    env_dlq = os.getenv("DEAD_LETTER_LOG")
    if env_dlq:
        return Path(env_dlq)
    root = Path(__file__).resolve().parent.parent.parent
    data_dir = Path(os.getenv("DATA_DIR") or ("/data" if Path("/data").is_dir() else (root / "data" if (root / "data").is_dir() else root)))
    return data_dir / "dead_letter_queue.jsonl"


DEAD_LETTER_LOG = get_dead_letter_log_path()


def _record_dead_letter(msg_id: str, payload: dict, exc: Exception):
    try:
        target = DEAD_LETTER_LOG or get_dead_letter_log_path()
        record = {
            "timestamp": int(time.time()),
            "message_id": msg_id,
            "payload": payload,
            "error": str(exc),
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        log.warning("Archived permanently failing message %s to %s", msg_id, target)
    except Exception as log_err:
        log.error("Could not write to local dead letter log: %s", log_err)


log = logging.getLogger(__name__)

VALID_ACTIONS = {"play", "pause", "resume", "next", "previous", "stop", "volume"}
MAX_MESSAGE_RETRIES = 3


class PubSubWorker:
    def __init__(self, settings, loop, dispatch):
        if not settings.gcp_project_id or not settings.gcp_pubsub_subscription_id:
            raise ValueError("GCP_PROJECT_ID and GCP_PUBSUB_SUBSCRIPTION_ID are required when Pub/Sub is enabled")
        self.client = pubsub_v1.SubscriberClient()
        self.attempts: Counter[str] = Counter()
        path = self.client.subscription_path(settings.gcp_project_id, settings.gcp_pubsub_subscription_id)

        def callback(message):
            msg_id = message.message_id
            try:
                data = json.loads(message.data.decode("utf-8"))
                if not isinstance(data, dict) or data.get("action") not in VALID_ACTIONS:
                    raise ValueError(f"Invalid Pub/Sub action: {data.get('action') if isinstance(data, dict) else data}")
                if data["action"] == "play" and not isinstance(data.get("query"), str):
                    raise ValueError("Play requires a query")
            except (ValueError, UnicodeError):
                log.exception("Discarding invalid Pub/Sub message %s", msg_id)
                message.ack()
                return
            action = data["action"]
            speaker = data.get("speaker")
            device_id = data.get("device_id")
            # Keep voice searches out of logs: play payloads can contain private
            # artist or track names. The action and selected destination are
            # enough to trace routing through the local worker.
            log.info(
                "Google Home command received action=%s speaker=%r device_id=%r",
                action,
                speaker,
                device_id,
            )
            future = asyncio.run_coroutine_threadsafe(dispatch(data), loop)
            activity.record("Google Home", f"{action} received", speaker)

            def complete(result):
                try:
                    result.result()
                except Exception as exc:
                    activity.record("Google Home", f"{action} failed", speaker)
                    log.error(
                        "Google Home command failed action=%s speaker=%r device_id=%r error_type=%s",
                        action,
                        speaker,
                        device_id,
                        type(exc).__name__,
                    )
                    native_attempt = getattr(message, "delivery_attempt", None)
                    if native_attempt is not None:
                        # GCP Pub/Sub subscription dead-letter policy handles durable retry & routing to DLQ
                        log.warning(
                            "Pub/Sub message %s failed (native attempt %d, error_type=%s). Requesting redelivery via subscription DLQ policy.",
                            msg_id,
                            native_attempt,
                            type(exc).__name__,
                        )
                        message.nack()
                    else:
                        # Local durable fallback for subscriptions without native DLQ attached
                        self.attempts[msg_id] += 1
                        attempts = self.attempts[msg_id]
                        if attempts >= MAX_MESSAGE_RETRIES:
                            log.error(
                                "Pub/Sub message %s ('%s') failed %d times (error_type=%s). Retaining in %s before acknowledging.",
                                msg_id,
                                action,
                                attempts,
                                type(exc).__name__,
                                DEAD_LETTER_LOG,
                            )
                            _record_dead_letter(msg_id, data, exc)
                            self.attempts.pop(msg_id, None)
                            message.ack()
                        else:
                            log.warning(
                                "Pub/Sub message %s failed (attempt %d/%d, error_type=%s); requesting redelivery.",
                                msg_id,
                                attempts,
                                MAX_MESSAGE_RETRIES,
                                type(exc).__name__,
                            )
                            message.nack()
                else:
                    activity.record("Google Home", f"{action} completed", speaker)
                    log.info(
                        "Google Home command completed action=%s speaker=%r device_id=%r",
                        action,
                        speaker,
                        device_id,
                    )
                    self.attempts.pop(msg_id, None)
                    message.ack()

            future.add_done_callback(complete)

        self._callback = callback
        self.future = self.client.subscribe(path, callback=callback)

    def close(self):
        self.future.cancel()
        self.client.close()
