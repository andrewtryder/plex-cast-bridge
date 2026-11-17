"""Resolve a container's persistent data mount and reject overlapping writers."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any


def _within(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents


def resolve_data_mount(container: dict[str, Any], others: list[dict[str, Any]], data_dir: str) -> Path:
    dest = PurePosixPath(data_dir)
    if not dest.is_absolute() or ".." in dest.parts:
        raise ValueError("persistent data directory must be an absolute container path")
    mounts = container.get("Mounts") or []
    matches = [m for m in mounts if m.get("Destination") and _within(dest, PurePosixPath(m["Destination"]))]
    if not matches:
        raise ValueError("persistent data directory is not mounted")
    mount = max(matches, key=lambda m: len(PurePosixPath(m["Destination"]).parts))
    if mount.get("Type") not in ("bind", "volume") or not mount.get("RW") or not mount.get("Source"):
        raise ValueError("persistent data must be on a writable local bind mount or volume")
    if any(m is not mount and m.get("Destination") and _within(PurePosixPath(m["Destination"]), dest) for m in mounts):
        raise ValueError("nested mounts inside persistent data require a separate backup")
    root = PurePosixPath(mount["Destination"])
    source = (Path(mount["Source"]) / str(dest.relative_to(root))).resolve()
    if not source.is_dir():
        raise ValueError("data mount source is not accessible on this Docker host")
    for other in others:
        if not (other.get("State") or {}).get("Running"):
            continue
        for m in other.get("Mounts") or []:
            if not m.get("RW") or not m.get("Source") or m.get("Type") not in ("bind", "volume"):
                continue
            other_source = Path(m["Source"]).resolve()
            if os.path.commonpath((str(source), str(other_source))) in (str(source), str(other_source)):
                raise ValueError("another running container can write the persistent data mount")
    return source


def main() -> None:
    import argparse
    import json
    import subprocess

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("container")
    parser.add_argument("--data-dir", help="Previously resolved container path; useful after stopping it")
    args = parser.parse_args()

    def run(*args: str) -> str:
        return subprocess.run(["docker", *args], check=True, text=True, capture_output=True, timeout=30).stdout

    container = json.loads(run("inspect", args.container))[0]
    data_dir = args.data_dir or run("exec", args.container, "python", "-c",
                                   "from plex_cast_bridge.catalog_store import DATA_DIR; print(DATA_DIR)").strip()
    running = run("ps", "-q").split()
    running = [cid for cid in running if not container["Id"].startswith(cid)]
    others = json.loads(run("inspect", *running)) if running else []
    print(resolve_data_mount(container, others, data_dir))
    print(data_dir)


if __name__ == "__main__":
    main()
