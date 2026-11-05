import json
import logging
from typing import Any
from starlette.concurrency import run_in_threadpool

log = logging.getLogger(__name__)


class PubSubService:
    def __init__(self, project_id: str, topic_id: str, mock: bool = False):
        self.project_id = project_id
        self.topic_id = topic_id
        self.mock = mock
        self._publisher = None
        self._topic_path = None

        if not self.mock:
            try:
                from google.cloud import pubsub_v1
                self._publisher = pubsub_v1.PublisherClient()
                self._topic_path = self._publisher.topic_path(self.project_id, self.topic_id)
                log.info(f"Initialized Pub/Sub publisher for topic: {self._topic_path}")
            except Exception as exc:
                log.error(f"Failed to initialize Google Pub/Sub publisher for topic '{self.project_id}/{self.topic_id}': {exc}")
                raise RuntimeError(
                    f"Pub/Sub publisher initialization failed for topic '{self.topic_id}'. "
                    "Refusing to silently drop commands in production. (Set MOCK_PUBSUB=true for offline testing)."
                ) from exc

    def publish_sync(self, payload: dict[str, Any]) -> str:
        if self.mock or not self._publisher or not self._topic_path:
            log.info(f"[MOCK PUBSUB] Publishing payload: {payload}")
            return f"mock-{int(id(payload))}"

        data = json.dumps(payload).encode("utf-8")
        future = self._publisher.publish(self._topic_path, data)
        # Timeout after 2 seconds to fulfill latency requirement
        message_id = future.result(timeout=2.0)
        log.info(f"Published message {message_id} to {self._topic_path}")
        return str(message_id)

    async def publish(self, payload: dict[str, Any]) -> str:
        return await run_in_threadpool(self.publish_sync, payload)
