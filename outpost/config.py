import json
import os
import shutil
import subprocess
from dataclasses import dataclass

WINCRED_TARGET = "outpost:config"


def _wincred_available() -> bool:
    return shutil.which("wincred.exe") is not None


def _wincred_get(target: str) -> str | None:
    result = subprocess.run(
        ["wincred.exe", "get", target], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _wincred_set(target: str, secret: str) -> None:
    subprocess.run(["wincred.exe", "set", target], input=secret, text=True, check=True)


def _require_wincred() -> None:
    if not _wincred_available():
        raise SystemExit(
            "outpost requires wincred.exe on PATH (WSL2 + Windows Credential "
            "Manager — see https://github.com/vivainio/wincred)."
        )


def save_credentials(tower_url: str, push_secret: str, encryption_key: str | None = None) -> str:
    _require_wincred()
    payload = {"tower_url": tower_url, "push_secret": push_secret}
    if encryption_key is None:
        # Preserve an existing encryption key across e.g. a re-login,
        # unless the caller explicitly wants to set/clear one.
        existing = _load_credentials()
        if existing and existing.get("encryption_key"):
            encryption_key = existing["encryption_key"]
    if encryption_key:
        payload["encryption_key"] = encryption_key
    _wincred_set(WINCRED_TARGET, json.dumps(payload))
    return f"Windows Credential Manager ({WINCRED_TARGET})"


def _load_credentials() -> dict[str, str] | None:
    if not _wincred_available():
        return None
    raw = _wincred_get(WINCRED_TARGET)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


@dataclass
class Config:
    tower_url: str
    push_secret: str
    push_interval: float
    capture_lines: int
    session_max_age: float
    encryption_key: str | None = None

    @classmethod
    def from_env(cls) -> "Config":
        _require_wincred()
        creds = _load_credentials()
        if not creds:
            raise SystemExit("No credentials found. Run `outpost login` first.")
        return cls(
            tower_url=creds["tower_url"].rstrip("/"),
            push_secret=creds["push_secret"],
            push_interval=float(os.environ.get("PUSH_INTERVAL", "15")),
            capture_lines=int(os.environ.get("CAPTURE_LINES", "2000")),
            session_max_age=float(os.environ.get("SESSION_MAX_AGE_MINUTES", "60")) * 60,
            encryption_key=creds.get("encryption_key"),
        )
