"""In-memory OAuth login rate limiting.

State is process-local and is not shared across Cloud Run instances. Prefer a
single min-instance or sticky routing if lockout consistency across replicas
matters; Redis is intentionally deferred.
"""

import time

from fastapi import HTTPException, status

# {client_ip: [timestamp, ...]}
_FAILED_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
MAX_FAILED_LOGINS = 5
LOCKOUT_WINDOW_SECONDS = 300.0


def check_rate_limit(client_ip: str) -> None:
    now = time.time()
    attempts = [t for t in _FAILED_LOGIN_ATTEMPTS.get(client_ip, []) if now - t < LOCKOUT_WINDOW_SECONDS]
    _FAILED_LOGIN_ATTEMPTS[client_ip] = attempts
    if len(attempts) >= MAX_FAILED_LOGINS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authorization attempts. Please wait 5 minutes before trying again.",
        )


def record_failed_attempt(client_ip: str) -> None:
    now = time.time()
    attempts = [t for t in _FAILED_LOGIN_ATTEMPTS.get(client_ip, []) if now - t < LOCKOUT_WINDOW_SECONDS]
    attempts.append(now)
    _FAILED_LOGIN_ATTEMPTS[client_ip] = attempts


def clear_attempts(client_ip: str) -> None:
    _FAILED_LOGIN_ATTEMPTS.pop(client_ip, None)


def reset_for_tests() -> None:
    """Clear all rate-limit state (tests only)."""
    _FAILED_LOGIN_ATTEMPTS.clear()
