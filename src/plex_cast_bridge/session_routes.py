"""Password-free dashboard pairing and browser sessions."""

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import dashboard_sessions as store
from .api_auth import PAIR_COOKIE, SESSION_COOKIE, require_same_origin, set_session_cookie, verify_local_api

router = APIRouter(prefix="/api/session", tags=["dashboard-session"])


class Approval(BaseModel):
    code: str = Field(pattern=r"^\d{8}$")


def _configured_key(request: Request) -> str:
    value = getattr(getattr(request.app.state, "settings", None), "local_api_key", None)
    key = value.get_secret_value() if value else ""
    if not key:
        raise HTTPException(status_code=403, detail="Dashboard pairing requires LOCAL_API_KEY configuration")
    return key


@router.post("/migrate")
async def migrate(request: Request, response: Response):
    require_same_origin(request)
    key = _configured_key(request)
    supplied = request.headers.get("x-api-key", "")
    if not supplied or not secrets.compare_digest(supplied, key):
        raise HTTPException(status_code=401, detail="Invalid existing dashboard credential")
    set_session_cookie(response, request, store.create_session())
    return {"status": "authenticated"}


@router.post("/request")
async def request_pairing(request: Request, response: Response):
    require_same_origin(request)
    _configured_key(request)
    try:
        token, code = store.request_pairing(request.client.host if request.client else "unknown")
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    response.set_cookie(PAIR_COOKIE, token, httponly=True, secure=request.url.scheme == "https",
                        samesite="strict", max_age=store.PAIR_SECONDS, path="/api/session")
    response.headers["Cache-Control"] = "no-store"
    return {"code": code, "expires_in": store.PAIR_SECONDS}


@router.post("/claim")
async def claim(request: Request, response: Response):
    require_same_origin(request)
    _configured_key(request)
    token = store.claim(request.cookies.get(PAIR_COOKIE))
    response.headers["Cache-Control"] = "no-store"
    if token is None:
        return {"status": "pending"}
    set_session_cookie(response, request, token)
    response.delete_cookie(PAIR_COOKIE, path="/api/session")
    return {"status": "authenticated"}


@router.get("/pending", dependencies=[Depends(verify_local_api)])
async def pending(request: Request):
    _configured_key(request)
    return {"requests": store.pending_pairings()}


@router.post("/approve", dependencies=[Depends(verify_local_api)])
async def approve(body: Approval, request: Request):
    _configured_key(request)
    if not store.approve(body.code):
        raise HTTPException(status_code=404, detail="Approval request expired or already used")
    return {"status": "approved"}


@router.post("/logout", dependencies=[Depends(verify_local_api)])
async def logout(request: Request, response: Response):
    require_same_origin(request)
    store.revoke_session(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "signed_out"}


@router.post("/revoke-others", dependencies=[Depends(verify_local_api)])
async def revoke_others(request: Request):
    require_same_origin(request)
    store.revoke_other_sessions(request.cookies.get(SESSION_COOKIE))
    return {"status": "revoked"}
