#!/usr/bin/env python3
"""Read-only release baseline and smoke checks for an existing installation.

Run on the Docker host (or inside the LXC) so the default API URL is local.
Reports and the HMAC key are kept outside the repository with private modes.
No playback or provisioning command is issued by this tool.
"""

from __future__ import annotations

import sys

from release_acceptance.cli import main

from release_acceptance import (  # noqa: F401
    PROTECTED_ENV_KEYS,
    _catalog_snapshot_capture,
    _configuration_drift_capture,
)

if __name__ == "__main__":
    sys.exit(main())
