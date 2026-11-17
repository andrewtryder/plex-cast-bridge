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
