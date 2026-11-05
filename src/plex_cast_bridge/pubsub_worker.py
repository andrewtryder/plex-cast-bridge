import asyncio
import json
import logging

import os
import time
from pathlib import Path
from collections import Counter
from google.cloud import pubsub_v1

DEAD_LETTER_LOG = Path(
    os.getenv("DEAD_LETTER_LOG") or (Path(__file__).resolve().parent.parent.parent / "dead_letter_queue.jsonl")
)

def _record_dead_letter(msg_id: str, payload: dict, exc: Exception):
    try:
        record = {
            "timestamp": int(time.time()),
            "message_id": msg_id,
            "payload": payload,
            "error": str(exc),
        }
        DEAD_LETTER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DEAD_LETTER_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        log.warning("Archived permanently failing message %s to %s", msg_id, DEAD_LETTER_LOG)
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
            future = asyncio.run_coroutine_threadsafe(dispatch(data), loop)

            def complete(result):
                try:
                    result.result()
                except Exception as exc:
                    native_attempt = getattr(message, "delivery_attempt", None)
                    if native_attempt is not None:
                        # GCP Pub/Sub subscription dead-letter policy handles durable retry & routing to DLQ
                        log.warning(
                            "Pub/Sub message %s failed (native attempt %d, error: %s). Requesting redelivery via subscription DLQ policy.",
                            msg_id,
                            native_attempt,
                            exc,
                        )
                        message.nack()
                    else:
                        # Local durable fallback for subscriptions without native DLQ attached
                        self.attempts[msg_id] += 1
                        attempts = self.attempts[msg_id]
                        if attempts >= MAX_MESSAGE_RETRIES:
                            log.error(
                                "Pub/Sub message %s ('%s') failed %d times (%s). Retaining in %s before acknowledging.",
                                msg_id,
                                data.get("action"),
                                attempts,
                                exc,
                                DEAD_LETTER_LOG,
                            )
                            _record_dead_letter(msg_id, data, exc)
                            self.attempts.pop(msg_id, None)
                            message.ack()
                        else:
                            log.warning(
                                "Pub/Sub message %s failed (attempt %d/%d: %s); requesting redelivery.",
                                msg_id,
                                attempts,
                                MAX_MESSAGE_RETRIES,
                                exc,
                            )
                            message.nack()
                else:
                    self.attempts.pop(msg_id, None)
                    message.ack()

            future.add_done_callback(complete)

        self._callback = callback
        self.future = self.client.subscribe(path, callback=callback)

    def close(self):
        self.future.cancel()
        self.client.close()
