import os

import pytest
from fastapi.testclient import TestClient

# Local Settings refuse to start without LOCAL_API_KEY unless this opt-in is set.
# TestClient triggers the local app lifespan when local tests import plex_cast_bridge.main.
os.environ.setdefault("ALLOW_UNAUTHENTICATED_LOCAL_API", "true")
os.environ.setdefault("PCB_DISABLE_BACKGROUND_DISCOVERY", "true")
os.environ.setdefault("SPEAKER_DISCOVERY_INTERVAL_SEC", "0")
# Avoid multi-minute Plex retries against unreachable plex.local during TestClient lifespan.
os.environ["PLEX_TOKEN"] = ""
os.environ.setdefault("PCB_PLEX_STARTUP_ATTEMPTS", "1")

from cloud_gateway.auth import create_token_pair
from cloud_gateway.catalog_sync import CatalogCloudStore
from cloud_gateway.config import Settings
from cloud_gateway.main import app
from cloud_gateway.pubsub_service import PubSubService


@pytest.fixture
def test_settings(tmp_path):
    catalog_file = tmp_path / "catalog.json"
    catalog_file.write_text(
        """{
          "genres": ["Rock", "Jazz"],
          "artists": ["Nirvana", "The Beatles", "Kruder & Dorfmeister", "DJ Shadow"],
          "themes": ["Upbeat 90s rock"],
          "inputs": ["Master Bedroom", "Kitchen"],
          "speakers": [
            {
              "id": "plex-jukebox:Master Bedroom",
              "name": "Plex",
              "nicknames": ["Master Bedroom Plex", "Plex"],
              "roomHint": "Master Bedroom",
              "speaker_name": "Master Bedroom",
              "enabled": true
            },
            {
              "id": "plex-jukebox:Kitchen",
              "name": "Plex",
              "nicknames": ["Kitchen Plex", "Plex"],
              "roomHint": "Kitchen",
              "speaker_name": "Kitchen",
              "enabled": true
            },
            {
              "id": "plex-jukebox:Great Room",
              "name": "Plex",
              "nicknames": ["Great Room Plex", "Plex"],
              "roomHint": "Great Room",
              "speaker_name": "Great Room",
              "enabled": false
            }
          ]
        }"""
    )
    return Settings(
        GOOGLE_CLIENT_ID="test-client-id",
        GOOGLE_CLIENT_SECRET="test-client-secret",
        ADMIN_PASSPHRASE="test-passphrase",
        OAUTH_SIGNING_SECRET="test-signing-secret-key-1234567890",
        WEBHOOK_API_KEY="test-webhook-key",
        GCP_PROJECT_ID="test-project",
        GCP_PUBSUB_TOPIC_ID="test-topic",
        DEFAULT_SPEAKER_NAME="Living Room",
        CATALOG_PATH=str(catalog_file),
        MOCK_PUBSUB=True,
        ENVIRONMENT="test",
        USE_CLOUD_CATALOG=True,
        CATALOG_WRITE_KEY="test-catalog-key",
        REQUIRE_ADMIN_PASSPHRASE=True,
    )


@pytest.fixture
def client(test_settings):
    app.state.settings = test_settings
    app.state.pubsub = PubSubService(
        project_id=test_settings.gcp_project_id,
        topic_id=test_settings.gcp_pubsub_topic_id,
        mock=True,
    )
    # Fresh mock store per test so catalog uploads cannot leak across cases.
    app.state.catalog_store = CatalogCloudStore(
        project_id=test_settings.gcp_project_id,
        mock=True,
        allow_memory_fallback=True,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers(test_settings):
    tokens = create_token_pair(test_settings)
    return {"Authorization": f"Bearer {tokens['access_token']}"}
