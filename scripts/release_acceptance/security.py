import hashlib
import hmac
import os
import secrets
from pathlib import Path

from .constants import PROTECTED_ENV_KEYS

def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _key(state_dir: Path) -> bytes:
    path = state_dir / "fingerprint.key"
    if path.exists():
        os.chmod(path, 0o600)
        value = path.read_bytes()
        if len(value) < 32:
            raise RuntimeError(f"Fingerprint key is invalid: {path}")
        return value
    value = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
    return value


def _fingerprint(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def _fingerprint_bytes(key: bytes, value: bytes) -> str:
    return hmac.new(key, value, hashlib.sha256).hexdigest()


def _fingerprint_bytes(key: bytes, value: bytes) -> str:
    return hmac.new(key, value, hashlib.sha256).hexdigest()


def _read_env(path: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path or not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip("\"'")
    return values


def _protected_fingerprints(key: bytes, values: dict[str, str]) -> dict[str, str]:
    result = {}
    for name in PROTECTED_ENV_KEYS:
        value = values.get(name, "")
        if value:
            result[name] = _fingerprint(key, value)
    return result
