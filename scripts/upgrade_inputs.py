"""Back up reviewed Compose and credential input files without printing them."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from upgrade_backup import create


def create_inputs_backup(inputs: dict[str, str | None], archive: Path) -> None:
    """Archive exactly the input bytes inspected at review; abort on a change."""
    with tempfile.TemporaryDirectory(prefix="pcb-config-backup-") as temp:
        stage = Path(temp)
        manifest = []
        for index, (name, expected) in enumerate(sorted(inputs.items())):
            path = Path(name)
            try:
                value = path.read_bytes()
            except FileNotFoundError:
                value = None
            if (value.hex() if value is not None else None) != expected:
                raise ValueError("A reviewed configuration input changed; create a new plan")
            entry = {"source": name, "file": None}
            if value is not None:
                entry["file"] = f"input-{index}"
                fd = os.open(stage / entry["file"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(value)
            manifest.append(entry)
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        def recheck() -> None:
            for name, expected in inputs.items():
                try:
                    current = Path(name).read_bytes().hex()
                except FileNotFoundError:
                    current = None
                if current != expected:
                    raise ValueError("A reviewed configuration input changed; create a new plan")

        recheck()
        create(stage, archive)
        try:
            recheck()
        except (ValueError, OSError):
            archive.unlink(missing_ok=True)
            archive.with_name(archive.name + ".sha256").unlink(missing_ok=True)
            raise


def verify_inputs_backup(inputs: dict[str, str | None], archive: Path) -> None:
    import tarfile
    from upgrade_backup import verify

    verify(archive)
    with tarfile.open(archive, "r:gz") as tar:
        manifest_file = tar.extractfile("data/manifest.json")
        if manifest_file is None:
            raise ValueError("Configuration backup manifest is missing")
        manifest = json.load(manifest_file)
        if not isinstance(manifest, list) or len(manifest) != len(inputs):
            raise ValueError("Configuration backup manifest is invalid")
        captured = {}
        for entry in manifest:
            if not isinstance(entry, dict) or not isinstance(entry.get("source"), str) or entry["source"] in captured:
                raise ValueError("Configuration backup manifest is invalid")
            filename = entry.get("file")
            if filename is None:
                value = None
            else:
                if not isinstance(filename, str) or not filename.startswith("input-") or not filename[6:].isdigit():
                    raise ValueError("Configuration backup manifest is invalid")
                content = tar.extractfile("data/" + filename)
                if content is None:
                    raise ValueError("Configuration backup input is missing")
                value = content.read().hex()
            captured[entry["source"]] = value
        if captured != inputs:
            raise ValueError("Configuration backup differs from the reviewed inputs")
