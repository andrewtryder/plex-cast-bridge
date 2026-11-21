"""Read-only release baseline and smoke checks for an existing installation.

Run on the Docker host (or inside the LXC) so the default API URL is local.
Reports and the HMAC key are kept outside the repository with private modes.
No playback or provisioning command is issued by this tool.
"""

import subprocess

from .capture import capture, capture_cloud
from .cli import main
from .cloud import _cloud_capture
from .compare import compare
from .constants import (
    API_PATHS,
    CLOUD_IDENTITY_PATHS,
    DEFAULT_STATE_DIR,
    LOCAL_IDENTITY_PATHS,
    PROTECTED_ENV_KEYS,
)
from .docker import (
    _catalog_snapshot_capture,
    _configuration_drift_capture,
    _container_capture,
    _valid_drift,
)
from .http_util import _api_capture, _get_json, _run_json
from .local_state import (
    _backup_capture,
    _credentials_capture,
    _google_home_sync_capture,
    _plex_connection_capture,
    _sqlite_integrity,
)
from .security import (
    _fingerprint,
    _fingerprint_bytes,
    _key,
    _private_dir,
    _protected_fingerprints,
    _read_env,
)

__all__ = [
    "API_PATHS",
    "CLOUD_IDENTITY_PATHS",
    "DEFAULT_STATE_DIR",
    "LOCAL_IDENTITY_PATHS",
    "PROTECTED_ENV_KEYS",
    "_api_capture",
    "_backup_capture",
    "_catalog_snapshot_capture",
    "_cloud_capture",
    "_configuration_drift_capture",
    "_container_capture",
    "_credentials_capture",
    "_fingerprint",
    "_fingerprint_bytes",
    "_get_json",
    "_google_home_sync_capture",
    "_key",
    "_plex_connection_capture",
    "_private_dir",
    "_protected_fingerprints",
    "_read_env",
    "_run_json",
    "_sqlite_integrity",
    "_valid_drift",
    "capture",
    "capture_cloud",
    "compare",
    "main",
    "subprocess",
]
