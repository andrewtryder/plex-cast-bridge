"""Local control API authentication helpers."""

import secrets

from fastapi import Header, HTTPException, Request


async def verify_local_api(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
):
    settings = getattr(request.app.state, "settings", None)
    local_key = getattr(settings, "local_api_key", None)
    if not local_key or not getattr(local_key, "get_secret_value", lambda: "")():
        return None
    expected = local_key.get_secret_value()
    token = x_api_key or request.query_params.get("api_key")
    if not token:
        auth_hdr = request.headers.get("Authorization")
        if auth_hdr and auth_hdr.lower().startswith("bearer "):
            token = auth_hdr.split(" ", 1)[1]
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: invalid or missing local API key (pass X-API-Key or ?api_key=)",
        )
    return token
