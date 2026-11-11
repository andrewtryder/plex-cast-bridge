"""OAuth 2.0 account-linking routes for Google Home."""

import base64
import logging
import secrets
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .auth import (
    create_access_token,
    create_authorization_code,
    validate_redirect_uri,
    verify_authorization_code,
)
from .config import Settings
from .rate_limit import check_rate_limit, clear_attempts, record_failed_attempt
from .refresh_tokens import validate_refresh_token

log = logging.getLogger("fulfillment")

templates_dir = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

router = APIRouter()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def _issue_auth_redirect(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scope: str,
    settings: Settings,
) -> RedirectResponse:
    auth_code = create_authorization_code(
        client_id=client_id,
        redirect_uri=redirect_uri,
        settings=settings,
        scope=scope,
    )
    parsed_redirect = urllib.parse.urlsplit(redirect_uri)
    query_params = urllib.parse.parse_qsl(parsed_redirect.query)
    query_params.extend([("code", auth_code), ("state", state)])
    new_query = urllib.parse.urlencode(query_params)
    target_url = urllib.parse.urlunsplit(
        (
            parsed_redirect.scheme,
            parsed_redirect.netloc,
            parsed_redirect.path,
            new_query,
            parsed_redirect.fragment,
        )
    )
    log.info("OAuth authorize successful for client '%s'. Redirecting to Google.", client_id)
    return RedirectResponse(url=target_url, status_code=status.HTTP_302_FOUND)


@router.get("/oauth/authorize", response_class=HTMLResponse)
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

    if response_type != "code":
        raise HTTPException(status_code=400, detail="Unsupported response_type")
    check_rate_limit(request.client.host if request.client else "unknown")
    approval = await run_in_threadpool(
        request.app.state.approval_store.create,
        client_id=client_id, redirect_uri=redirect_uri, state=state, scope=scope,
    )
    return templates.TemplateResponse(
        request=request, name="pairing.html", context=approval,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


class PairingTicket(BaseModel):
    ticket: str = Field(min_length=32, max_length=128)


class PairingApproval(PairingTicket):
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


@router.post("/oauth/pairing/status")
def pairing_status(body: PairingTicket, request: Request):
    return JSONResponse({"status": request.app.state.approval_store.status(body.ticket)},
                        headers={"Cache-Control": "no-store"})


@router.post("/oauth/pairing/complete")
def pairing_complete(request: Request, ticket: str = Form(...), settings: Settings = Depends(get_settings)):
    approval = request.app.state.approval_store.consume(ticket)
    if approval is None:
        raise HTTPException(status_code=400, detail="Approval missing, expired, or already used")
    validate_redirect_uri(approval["redirect_uri"], allow_local=settings.mock_pubsub)
    if approval["client_id"] != settings.google_client_id:
        raise HTTPException(status_code=400, detail="Invalid client_id")
    return _issue_auth_redirect(settings=settings, **approval)


def verify_pairing_key(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> None:
    configured = request.app.state.settings.catalog_write_key
    expected = configured.get_secret_value() if configured else ""
    if not expected or not x_api_key or not secrets.compare_digest(expected, x_api_key):
        raise HTTPException(status_code=401, detail="Invalid pairing key")


@router.get("/internal/oauth/pending")
def pending_pairing(request: Request, api_key: None = Depends(verify_pairing_key)):
    return JSONResponse({"pending": request.app.state.approval_store.pending()},
                        headers={"Cache-Control": "no-store"})


@router.post("/internal/oauth/approve")
def approve_pairing(body: PairingApproval, request: Request, api_key: None = Depends(verify_pairing_key)):
    if not request.app.state.approval_store.approve(body.ticket, body.code):
        raise HTTPException(status_code=409, detail="Approval expired or code did not match")
    return {"status": "approved"}


@router.post("/oauth/authorize", response_class=HTMLResponse)
async def oauth_authorize_post(
    request: Request,
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: str = Form(...),
    response_type: str = Form("code"),
    scope: str = Form(""),
    passphrase: str | None = Form(None),
    settings: Settings = Depends(get_settings),
):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(client_ip)

    validate_redirect_uri(redirect_uri, allow_local=settings.mock_pubsub)
    if client_id != settings.google_client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid client_id.",
        )

    expected_passphrase = settings.admin_passphrase.get_secret_value()
    if not passphrase or not secrets.compare_digest(passphrase, expected_passphrase):
        record_failed_attempt(client_ip)
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
                "require_passphrase": True,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    clear_attempts(client_ip)
    return _issue_auth_redirect(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        scope=scope,
        settings=settings,
    )


@router.post("/oauth/token")
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

    expected_cid = settings.google_client_id
    expected_sec = settings.google_client_secret.get_secret_value()

    if client_id != expected_cid:
        log.warning(
            "OAuth token rejected: unknown client_id got=%r expected=%r",
            client_id,
            expected_cid,
        )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "invalid_client", "error_description": "Unknown client_id"},
        )

    secret_ok = bool(
        client_secret
        and len(client_secret) == len(expected_sec)
        and secrets.compare_digest(client_secret, expected_sec)
    )
    if not secret_ok:
        log.warning(
            "OAuth token rejected: invalid client_secret (got_len=%s expected_len=%s basic_auth=%s)",
            len(client_secret or ""),
            len(expected_sec),
            bool(authorization and authorization.lower().startswith("basic ")),
        )
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
        catalog_store = getattr(request.app.state, "catalog_store", None)
        token_store = getattr(request.app.state, "oauth_token_store", None)
        if token_store is None:
            return JSONResponse(status_code=503, content={"error": "temporarily_unavailable"})
        if catalog_store is not None:
            try:
                catalog_store.set_linked(True)
            except Exception:
                log.exception("Could not persist account link")
                return JSONResponse(status_code=503, content={"error": "temporarily_unavailable"})
        try:
            durable_refresh_token = token_store.issue()
        except Exception:
            log.exception("Could not persist Google Home refresh token")
            return JSONResponse(status_code=503, content={"error": "temporarily_unavailable"})
        access_token, expires_in = create_access_token(settings)
        token_data = {
            "token_type": "Bearer",
            "access_token": access_token,
            "refresh_token": durable_refresh_token,
            "expires_in": expires_in,
        }
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
        catalog_store = getattr(request.app.state, "catalog_store", None)
        token_store = getattr(request.app.state, "oauth_token_store", None)
        if token_store is None or (catalog_store is not None and not catalog_store.is_linked()):
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "Account is unlinked"},
            )
        try:
            validate_refresh_token(refresh_token, settings, token_store)
        except HTTPException as exc:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": str(exc.detail)},
            )
        except Exception:
            log.exception("Could not validate durable Google Home refresh token")
            return JSONResponse(status_code=503, content={"error": "temporarily_unavailable"})
        access_token, expires_in = create_access_token(settings)
        token_data = {
            "token_type": "Bearer",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": expires_in,
        }
        return JSONResponse(
            content=token_data,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": "unsupported_grant_type", "error_description": f"Unsupported grant_type: {grant_type}"},
    )
