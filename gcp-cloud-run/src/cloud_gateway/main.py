import base64
import logging
import secrets
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .auth import (
    create_authorization_code,
    create_token_pair,
    validate_redirect_uri,
    verify_authorization_code,
    verify_bearer_token,
    verify_refresh_token,
    verify_webhook_api_key,
)
from .config import Settings
from .pubsub_service import PubSubService
from .smart_home import handle_smart_home_request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("fulfillment")

templates_dir = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
static_dir = Path(__file__).resolve().parent / "static"


class WebhookQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Free-text query or song/artist command")
    speaker: str | None = Field(default=None, description="Optional target speaker friendly name")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "settings") or app.state.settings is None:
        app.state.settings = Settings()
    if not hasattr(app.state, "pubsub") or app.state.pubsub is None:
        settings = app.state.settings
        app.state.pubsub = PubSubService(
            project_id=settings.gcp_project_id,
            topic_id=settings.gcp_pubsub_topic_id,
            mock=settings.mock_pubsub,
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


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_pubsub_service(request: Request) -> PubSubService:
    return request.app.state.pubsub


@app.get("/health")
@app.get("/")
async def health(settings: Annotated[Settings, Depends(get_settings)]):
    return {
        "status": "ok",
        "service": "plex-google-home-fulfillment",
        "project": settings.gcp_project_id,
        "topic": settings.gcp_pubsub_topic_id,
    }


# ============================================================================
# OAuth 2.0 Account Linking Endpoints
# ============================================================================


# In-memory rate limiting for OAuth login attempts: {client_ip: [timestamp, ...]}
_FAILED_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
MAX_FAILED_LOGINS = 5
LOCKOUT_WINDOW_SECONDS = 300.0


def _check_rate_limit(client_ip: str):
    now = time.time()
    attempts = [t for t in _FAILED_LOGIN_ATTEMPTS.get(client_ip, []) if now - t < LOCKOUT_WINDOW_SECONDS]
    _FAILED_LOGIN_ATTEMPTS[client_ip] = attempts
    if len(attempts) >= MAX_FAILED_LOGINS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authorization attempts. Please wait 5 minutes before trying again.",
        )


def _record_failed_attempt(client_ip: str):
    now = time.time()
    attempts = [t for t in _FAILED_LOGIN_ATTEMPTS.get(client_ip, []) if now - t < LOCKOUT_WINDOW_SECONDS]
    attempts.append(now)
    _FAILED_LOGIN_ATTEMPTS[client_ip] = attempts


@app.get("/oauth/authorize", response_class=HTMLResponse)
async def oauth_authorize_get(
    request: Request,
    client_id: str,
    redirect_uri: str,
    state: str,
    response_type: str = "code",
    scope: str = "",
    settings: Settings = Depends(get_settings),
):
    validate_redirect_uri(redirect_uri, allow_local=settings.mock_pubsub)
    if client_id != settings.google_client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid client_id. Expected '{settings.google_client_id}'.",
        )

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": response_type,
            "scope": scope,
            "error": None,
        },
    )


@app.post("/oauth/authorize", response_class=HTMLResponse)
async def oauth_authorize_post(
    request: Request,
    passphrase: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: str = Form(...),
    response_type: str = Form("code"),
    scope: str = Form(""),
    settings: Settings = Depends(get_settings),
):
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    validate_redirect_uri(redirect_uri, allow_local=settings.mock_pubsub)
    if client_id != settings.google_client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid client_id.",
        )

    expected_passphrase = settings.admin_passphrase.get_secret_value()
    if not secrets.compare_digest(passphrase, expected_passphrase):
        _record_failed_attempt(client_ip)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "response_type": response_type,
                "scope": scope,
                "error": "Invalid Admin Passphrase. Please try again.",
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Clear rate limit on successful authorization
    _FAILED_LOGIN_ATTEMPTS.pop(client_ip, None)

    auth_code = create_authorization_code(
        client_id=client_id,
        redirect_uri=redirect_uri,
        settings=settings,
        scope=scope,
    )

    # Build redirect URL safely
    parsed_redirect = urllib.parse.urlsplit(redirect_uri)
    query_params = urllib.parse.parse_qsl(parsed_redirect.query)
    query_params.extend([("code", auth_code), ("state", state)])
    new_query = urllib.parse.urlencode(query_params)
    target_url = urllib.parse.urlunsplit((
        parsed_redirect.scheme,
        parsed_redirect.netloc,
        parsed_redirect.path,
        new_query,
        parsed_redirect.fragment,
    ))

    log.info(f"OAuth authorize successful for client '{client_id}'. Redirecting to Google.")
    return RedirectResponse(url=target_url, status_code=status.HTTP_302_FOUND)


@app.post("/oauth/token")
async def oauth_token(
    request: Request,
    grant_type: str = Form(...),
    code: str | None = Form(None),
    redirect_uri: str | None = Form(None),
    client_id: str | None = Form(None),
    client_secret: str | None = Form(None),
    refresh_token: str | None = Form(None),
    authorization: str | None = Header(None, alias="Authorization"),
    settings: Settings = Depends(get_settings),
):
    # Support HTTP Basic Auth for client_id and client_secret
    if authorization and authorization.lower().startswith("basic "):
        try:
            b64_creds = authorization.split(" ", 1)[1]
            decoded = base64.b64decode(b64_creds).decode("utf-8")
            if ":" in decoded:
                client_id, client_secret = decoded.split(":", 1)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Malformed HTTP Basic Authorization header",
                headers={"WWW-Authenticate": "Basic"},
            )

    # Validate client credentials
    expected_cid = settings.google_client_id
    expected_sec = settings.google_client_secret.get_secret_value()

    if client_id != expected_cid:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "invalid_client", "error_description": "Unknown client_id"},
        )

    if not client_secret or not secrets.compare_digest(client_secret, expected_sec):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "invalid_client", "error_description": "Invalid client_secret"},
        )

    if grant_type == "authorization_code":
        if not code:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_request", "error_description": "Missing authorization code"},
            )
        try:
            verify_authorization_code(code, expected_cid, settings, redirect_uri)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": "invalid_grant", "error_description": str(exc.detail)},
            )
        token_data = create_token_pair(settings)
        return JSONResponse(
            content=token_data,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    elif grant_type == "refresh_token":
        if not refresh_token:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_request", "error_description": "Missing refresh_token"},
            )
        verify_refresh_token(refresh_token, settings)
        token_data = create_token_pair(settings)
        return JSONResponse(
            content=token_data,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": "unsupported_grant_type", "error_description": f"Unsupported grant_type: {grant_type}"},
    )


# ============================================================================
# Google Smart Home Fulfillment Endpoint
# ============================================================================


@app.post("/fulfillment")
async def fulfillment(
    request: Request,
    body: dict[str, Any],
    token_claims: dict[str, Any] = Depends(verify_bearer_token),
    settings: Settings = Depends(get_settings),
    pubsub_service: PubSubService = Depends(get_pubsub_service),
):
    try:
        response_data = await handle_smart_home_request(body, settings, pubsub_service)
        return response_data
    except Exception as exc:
        log.exception(f"Error handling fulfillment request: {exc}")
        req_id = body.get("requestId", "")
        return {
            "requestId": req_id,
            "payload": {
                "errorCode": "hardError",
                "debug": str(exc),
            },
        }


# ============================================================================
# Custom Webhook Endpoint
# ============================================================================


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
