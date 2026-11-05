import asyncio
import json
import logging

from collections import Counter
from google.cloud import pubsub_v1

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
                    self.attempts[msg_id] += 1
                    attempts = self.attempts[msg_id]
                    if attempts >= MAX_MESSAGE_RETRIES:
                        log.error(
                            "Pub/Sub message %s ('%s') failed %d times (%s). Acknowledging and discarding poison pill.",
                            msg_id,
                            data.get("action"),
                            attempts,
                            exc,
                        )
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

        self.future = self.client.subscribe(path, callback=callback)

    def close(self):
        self.future.cancel()
        self.client.close()
