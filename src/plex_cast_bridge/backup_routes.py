"""Safe local-dashboard backup export and import routes."""

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

router = APIRouter()


def _data_dir() -> Path:
    """Read the legacy seam lazily so existing routes.DATA_DIR patches work."""
    from . import routes

    return routes.DATA_DIR


@router.get("/api/backup")
async def backup_export(request: Request):
    """Download non-secret configuration backup (excludes tokens, PEMs, SA keys)."""
    data_dir = _data_dir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        meta = {
            "schema_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "includes_secrets": False,
        }
        zf.writestr("backup_meta.json", json.dumps(meta, indent=2))
        cat = data_dir / "catalog.json"
        if cat.exists():
            zf.writestr("catalog.json", cat.read_text(encoding="utf-8"))
        app_settings = data_dir / "app_settings.json"
        if app_settings.exists():
            zf.writestr("app_settings.json", app_settings.read_text(encoding="utf-8"))
        for name in ("google_home_setup.json", "google_home_sync.json"):
            path = data_dir / name
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                data.pop("catalog_write_key", None)
                zf.writestr(name, json.dumps(data, indent=2))
        settings = request.app.state.settings
        zf.writestr(
            "settings_public.json",
            json.dumps(
                {
                    "default_cast_volume": settings.default_cast_volume,
                    "max_cast_volume": settings.max_cast_volume,
                    "night_cast_volume": settings.night_cast_volume,
                    "gcp_project_id": settings.gcp_project_id,
                    "enable_pubsub_listener": settings.enable_pubsub_listener,
                    "cloud_gateway_url": settings.cloud_gateway_url,
                },
                indent=2,
            ),
        )
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=plex-cast-bridge-backup.zip"},
    )


@router.post("/api/backup/restore/preview")
async def backup_restore_preview(request: Request):
    """Preview a non-secret backup zip without writing files."""
    form = await request.form()
    upload = form.get("file")
    if upload is None:
        raise HTTPException(status_code=400, detail="file required")
    raw = await upload.read()
    if len(raw) > 20_000_000:
        raise HTTPException(status_code=400, detail="archive too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="invalid zip") from exc
    names = zf.namelist()
    for name in names:
        if name.startswith("/") or ".." in Path(name).parts:
            raise HTTPException(status_code=400, detail="path traversal rejected")
    meta = {}
    if "backup_meta.json" in names:
        meta = json.loads(zf.read("backup_meta.json"))
    if meta.get("includes_secrets"):
        raise HTTPException(
            status_code=400, detail="refusing backup that claims to include secrets"
        )
    blocked = {"plex_connection.json", "device_private.pem", "gcp-credentials.json"}
    would_write = [
        name for name in names if Path(name).name not in blocked and name != "backup_meta.json"
    ]
    return {
        "schema_version": meta.get("schema_version"),
        "created_at": meta.get("created_at"),
        "files": would_write,
        "blocked": sorted(blocked.intersection({Path(name).name for name in names})),
        "writable": True,
    }


@router.post("/api/backup/restore")
async def backup_restore(request: Request):
    """Apply a non-secret backup. Requires confirm=true."""
    form = await request.form()
    if str(form.get("confirm") or "").lower() not in {"1", "true", "yes"}:
        raise HTTPException(status_code=400, detail="confirm=true required")
    upload = form.get("file")
    if upload is None:
        raise HTTPException(status_code=400, detail="file required")
    raw = await upload.read()
    if len(raw) > 20_000_000:
        raise HTTPException(status_code=400, detail="archive too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="invalid zip") from exc
    allowed = {
        "catalog.json",
        "app_settings.json",
        "google_home_setup.json",
        "google_home_sync.json",
        "settings_public.json",
    }
    written = []
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in zf.namelist():
        if name.startswith("/") or ".." in Path(name).parts:
            raise HTTPException(status_code=400, detail="path traversal rejected")
        base = Path(name).name
        if base not in allowed:
            continue
        target = data_dir / base
        data = zf.read(name)
        if base.endswith(".json"):
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                parsed.pop("catalog_write_key", None)
                parsed.pop("plex_token", None)
                data = (json.dumps(parsed, indent=2) + "\n").encode("utf-8")
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        written.append(base)
    return {"restored": written}
