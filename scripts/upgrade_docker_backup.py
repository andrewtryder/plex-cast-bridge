"""Run the standard backup verifier in a network-isolated Docker helper.

This permits the Docker daemon to read 0600 files owned by the bridge user
without changing their ownership. Only the data mount and a private staging
folder are exposed to the helper; the source is always read-only.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _mount(source: Path, target: str, *, readonly: bool) -> str:
    path = str(source.resolve())
    if "," in path or "\n" in path:
        raise ValueError("Docker helper paths containing commas or newlines require manual backup")
    return f"type=bind,src={path},dst={target}" + (",readonly" if readonly else "")


def docker_backup(action: str, image: str, source: Path, backup_dir: Path) -> None:
    if action not in ("create", "compare"):
        raise ValueError("unsupported backup operation")
    script = (Path(__file__).resolve().parent / "upgrade_backup.py").read_text(encoding="utf-8")
    base = ["docker", "run", "--rm", "-i", "--pull", "never", "--network", "none", "--read-only",
            "--tmpfs", "/tmp", "--user", "0", "--entrypoint", "python", "--mount", _mount(source, "/source", readonly=True)]
    if action == "create":
        if (backup_dir / "data.tgz").exists() or (backup_dir / "data.tgz.sha256").exists():
            raise FileExistsError("data backup already exists")
        stage = Path(tempfile.mkdtemp(prefix=".data-backup-", dir=backup_dir))
        try:
            code = script + "\nimport os\nfor n in ('data.tgz','data.tgz.sha256'):\n os.chown('/backup/'+n, int(os.environ['PCB_OWNER_UID']), int(os.environ['PCB_OWNER_GID']))\n"
            cmd = base + ["--mount", _mount(stage, "/backup", readonly=False), "-e", f"PCB_OWNER_UID={os.getuid()}",
                          "-e", f"PCB_OWNER_GID={os.getgid()}", image, "-c",
                          "import sys; exec(compile(sys.stdin.read(), 'upgrade_backup.py', 'exec'))",
                          "create", "/source", "/backup/data.tgz", "--writers-stopped"]
            result = subprocess.run(cmd, input=code, capture_output=True, text=True, timeout=300, check=False)
            if result.returncode:
                raise RuntimeError("The isolated Docker data backup failed; the original data was not changed")
            for name in ("data.tgz", "data.tgz.sha256"):
                os.link(stage / name, backup_dir / name)
        finally:
            shutil.rmtree(stage)
    else:
        cmd = base + ["--mount", _mount(backup_dir, "/backup", readonly=True), image, "-c",
                      "import sys; exec(compile(sys.stdin.read(), 'upgrade_backup.py', 'exec'))",
                      "compare", "/backup/data.tgz", "/source"]
        result = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=300, check=False)
        if result.returncode:
            raise RuntimeError("The isolated Docker backup comparison failed; the original data was not changed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "compare"))
    parser.add_argument("--image", required=True)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    args = parser.parse_args()
    docker_backup(args.action, args.image, args.source, args.backup_dir)
    print("Isolated Docker backup check passed.")


if __name__ == "__main__":
    main()


def read_host_file(image: str, source: Path) -> bytes:
    """Read a single mounted file through Docker without changing its permissions."""
    command = ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
               "--user", "0", "--entrypoint", "python", "--mount", _mount(source, "/input", readonly=True),
               image, "-c", "import sys; b=open('/input','rb').read(16777217); "
               "sys.exit(2) if len(b)>16777216 else sys.stdout.buffer.write(b)"]
    result = subprocess.run(command, capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError("A mounted configuration file could not be read through Docker")
    return result.stdout
