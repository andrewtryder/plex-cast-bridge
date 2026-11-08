import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import verify_bearer_token, verify_webhook_api_key
from .catalog_sync import CatalogCloudStore
from .config import Settings
from .oauth_routes import router as oauth_router
from .pubsub_service import PubSubService
from .smart_home import handle_smart_home_request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("fulfillment")

static_dir = Path(__file__).resolve().parent / "static"


class WebhookQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Free-text query or song/artist command")
    speaker: str | None = Field(default=None, description="Optional target speaker friendly name")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "settings") or app.state.settings is None:
        app.state.settings = Settings()
    settings = app.state.settings
    if not hasattr(app.state, "pubsub") or app.state.pubsub is None:
        app.state.pubsub = PubSubService(
            project_id=settings.gcp_project_id,
            topic_id=settings.gcp_pubsub_topic_id,
            mock=settings.mock_pubsub,
        )
    if not hasattr(app.state, "catalog_store") or app.state.catalog_store is None:
        app.state.catalog_store = CatalogCloudStore(
            project_id=settings.gcp_project_id,
            bucket_name=settings.catalog_bucket,
            mock=settings.mock_pubsub or settings.environment in {"test", "development"},
        )
    log.info(
        f"Started Plex Google Home Fulfillment service (Project: {app.state.settings.gcp_project_id}, Topic: {app.state.settings.gcp_pubsub_topic_id})"
    )
    yield


app = FastAPI(
    title="Plex Google Home Fulfillment",
    description="Fulfillment webhook and personal OAuth2 server for Google Home Plex integration",
    version="0.1.0",
    lifespan=lifespan,
)

if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(oauth_router)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_pubsub_service(request: Request) -> PubSubService:
    return request.app.state.pubsub


def get_catalog_store(request: Request) -> CatalogCloudStore:
    return request.app.state.catalog_store


class CatalogPutRequest(BaseModel):
    version: int = Field(..., ge=1)
    catalog: dict[str, Any]
    etag: str | None = None
    request_sync: bool = True


@app.get("/health")
@app.get("/")
async def health(settings: Annotated[Settings, Depends(get_settings)]):
    return {
        "status": "ok",
        "service": "plex-google-home-fulfillment",
        "project": settings.gcp_project_id,
        "topic": settings.gcp_pubsub_topic_id,
    }


@app.put("/internal/catalog")
async def put_catalog(
    payload: CatalogPutRequest,
    api_key: str = Depends(verify_webhook_api_key),
    settings: Settings = Depends(get_settings),
    catalog_store: CatalogCloudStore = Depends(get_catalog_store),
):
    # Prefer dedicated catalog write key when configured; webhook key still accepted.
    expected = None
    if settings.catalog_write_key and settings.catalog_write_key.get_secret_value():
        expected = settings.catalog_write_key.get_secret_value()
    if expected and api_key != expected and api_key != settings.get_webhook_key():
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="Invalid catalog write key")
    try:
        meta = catalog_store.put_catalog(payload.catalog, payload.version, etag=payload.etag)
    except ValueError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail=str(exc)) from exc
    sync_result = {"state": "skipped", "detail": "request_sync=false"}
    if payload.request_sync:
        # Mark linked again when a bridge successfully publishes a catalog.
        catalog_store.set_linked(True)
        sync_result = catalog_store.request_sync()
    return {"status": "ok", "meta": meta.to_dict(), "request_sync": sync_result}


@app.post("/internal/request-sync")
async def manual_request_sync(
    api_key: str = Depends(verify_webhook_api_key),
    catalog_store: CatalogCloudStore = Depends(get_catalog_store),
):
    return catalog_store.request_sync(force=True)


@app.post("/fulfillment")
async def fulfillment(
    request: Request,
    body: dict[str, Any],
    token_claims: dict[str, Any] = Depends(verify_bearer_token),
    settings: Settings = Depends(get_settings),
    pubsub_service: PubSubService = Depends(get_pubsub_service),
    catalog_store: CatalogCloudStore = Depends(get_catalog_store),
):
    try:
        response_data = await handle_smart_home_request(
            body, settings, pubsub_service, catalog_store=catalog_store
        )
        return response_data
    except Exception as exc:
        log.exception(f"Error handling fulfillment request: {exc}")
        req_id = body.get("requestId", "")
        return {
            "requestId": req_id,
            "payload": {
                "errorCode": "hardError",
            },
        }

@app.post("/webhook/query")
async def webhook_query(
    request: Request,
    payload: WebhookQueryRequest,
    api_key: str = Depends(verify_webhook_api_key),
    settings: Settings = Depends(get_settings),
    pubsub_service: PubSubService = Depends(get_pubsub_service),
):
    target_speaker = payload.speaker or settings.default_speaker_name
    pubsub_payload = {
        "action": "play",
        "query": payload.query,
        "speaker": target_speaker,
        "source": "webhook",
    }
    log.info(f"Webhook query received: {payload.query} (speaker={target_speaker})")
    msg_id = await pubsub_service.publish(pubsub_payload)
    return {
        "status": "queued",
        "message_id": msg_id,
        "action": "play",
        "query": payload.query,
        "speaker": target_speaker,
    }
