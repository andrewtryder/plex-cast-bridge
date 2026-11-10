"""Local control API authentication helpers."""

import secrets
import ipaddress
import os
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

from . import dashboard_sessions

SESSION_COOKIE = "pcb_session"
PAIR_COOKIE = "pcb_pairing"


def require_same_origin(request: Request):
    origin = request.headers.get("origin", "")
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    allowed = {x.strip().rstrip("/") for x in os.getenv("DASHBOARD_ALLOWED_ORIGINS", "").split(",") if x.strip()}
    try:
        parsed = urlsplit(origin)
        hostname = parsed.hostname or ""
        try:
            local = hostname == "localhost" or ipaddress.ip_address(hostname).is_private
        except ValueError:
            local = False
    except ValueError:
        raise HTTPException(status_code=403, detail="Same-origin dashboard request required") from None
    if (not origin or origin != expected or parsed.scheme not in ("http", "https")
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or parsed.username or parsed.password or not (local or origin in allowed)
            or request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none")):
        raise HTTPException(status_code=403, detail="Same-origin dashboard request required")


def set_session_cookie(response, request: Request, token: str):
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=request.url.scheme == "https",
                        samesite="strict", max_age=dashboard_sessions.ABSOLUTE_SECONDS, path="/")


async def verify_local_api(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
):
    settings = getattr(request.app.state, "settings", None)
    local_key = getattr(settings, "local_api_key", None)
    if "api_key" in request.query_params:
        raise HTTPException(status_code=400, detail="API keys in URLs are not accepted")
    if not local_key or not getattr(local_key, "get_secret_value", lambda: "")():
        return None
    expected = local_key.get_secret_value()
    token = x_api_key
    if not token:
        auth_hdr = request.headers.get("Authorization")
        if auth_hdr and auth_hdr.lower().startswith("bearer "):
            token = auth_hdr.split(" ", 1)[1]
    if token and secrets.compare_digest(token, expected):
        return token
    if not token and dashboard_sessions.valid_session(request.cookies.get(SESSION_COOKIE)):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            require_same_origin(request)
        return None
    raise HTTPException(status_code=401, detail="Dashboard approval required or invalid API key")
