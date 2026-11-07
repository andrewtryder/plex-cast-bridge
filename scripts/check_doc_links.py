#!/usr/bin/env python3
"""Verify relative Markdown links under docs/ and README resolve on disk."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def iter_markdown() -> list[Path]:
    files = [ROOT / "README.md"]
    files.extend(sorted((ROOT / "docs").rglob("*.md")))
    return [p for p in files if p.is_file()]


def check_file(path: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    for match in LINK_RE.finditer(text):
        target = match.group(2).strip()
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        # Strip anchors / query
        rel = target.split("#", 1)[0].split("?", 1)[0]
        if not rel:
            continue
        resolved = (path.parent / rel).resolve()
        try:
            resolved.relative_to(ROOT)
        except ValueError:
            errors.append(f"{path.relative_to(ROOT)}: link escapes repo: {target}")
            continue
        if not resolved.exists():
            errors.append(f"{path.relative_to(ROOT)}: missing {target}")
    return errors


def main() -> int:
    errors: list[str] = []
    for path in iter_markdown():
        errors.extend(check_file(path))
    if errors:
        print("Broken relative Markdown links:")
        for err in errors:
            print(f"  {err}")
        return 1
    print(f"Checked {len(iter_markdown())} Markdown files: all relative links OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
