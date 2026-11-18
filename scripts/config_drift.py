#!/usr/bin/env python3
"""Development wrapper for the packaged read-only configuration diagnostic."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from plex_cast_bridge.config_drift import main  # noqa: E402

if __name__ == "__main__":
    main()
