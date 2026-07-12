import base64
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
from typing import TypedDict

from outpost import crypto
from outpost.config import Config


class WindowInfo(TypedDict):
    session_name: str
    window_index: int
    window_name: str
    window_active: bool
    session_attached: bool


def list_windows() -> list[WindowInfo]:
    # tmux is optional — a machine that only pushes docs/session transcripts
    # (no tmux use at all) shouldn't need it on PATH.
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-windows",
                "-a",
                "-F",
                "#{session_name}\t#{window_index}\t#{window_name}\t#{window_active}\t#{session_attached}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    windows: list[WindowInfo] = []
    for line in result.stdout.strip().splitlines():
        session_name, window_index, window_name, window_active, session_attached = line.split("\t")
        windows.append(
            {
                "session_name": session_name,
                "window_index": int(window_index),
                "window_name": window_name,
                "window_active": window_active == "1",
                "session_attached": session_attached == "1",
            }
        )
    return windows


def capture(session_name: str, window_index: int, lines: int) -> str:
    target = f"{session_name}:{window_index}"
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-e", "-p", "-t", target, "-S", f"-{lines}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return ""
    return result.stdout if result.returncode == 0 else ""


def current_pane_id() -> str | None:
    """Returns the `session_name:window_index` of the tmux pane this process
    is running in, or None if it's not running inside tmux at all. Used by
    `outpost run` to exclude its own pane from what it pushes — resolved via
    tmux's own `$TMUX_PANE` identity rather than by pattern-matching pane
    content, which is unreliable (any pane whose scrollback happens to
    contain the right substring — e.g. from viewing this file's source, or
    from an unrelated earlier command — would be excluded too)."""
    tmux_pane = os.environ.get("TMUX_PANE")
    if not tmux_pane:
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", tmux_pane, "#{session_name}:#{window_index}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _row_hash(window_name: str, window_active: bool, session_attached: bool, content: str) -> str:
    return hashlib.sha256(
        f"{window_name}\t{window_active}\t{session_attached}\t{content}".encode()
    ).hexdigest()


# Tracks the last-pushed hash per pane so `run`'s loop can skip panes whose
# content hasn't changed since the previous cycle. Only meaningful within a
# single long-running process — each `push` invocation starts fresh.
_last_hashes: dict[str, str] = {}


def push_once(config: Config, exclude_pane_id: str | None = None) -> int:
    """Captures and pushes all live tmux panes, except `exclude_pane_id` (if
    given) — used by `outpost run`'s loop to keep its own noisy pane out of
    the tower. One-shot `outpost push` passes None so it pushes everything,
    including whatever pane it was run from."""
    if not config.encryption_key:
        raise SystemExit(
            "No encryption password set. Run `outpost set-password` before pushing "
            "(pane content must be encrypted before it leaves this machine)."
        )

    windows = list_windows()
    key = base64.b64decode(config.encryption_key)

    live = []
    current_hashes: dict[str, str] = {}
    changes = []
    for w in windows:
        pane_id = f"{w['session_name']}:{w['window_index']}"
        if pane_id == exclude_pane_id:
            continue
        content = capture(w["session_name"], w["window_index"], config.capture_lines)
        live.append(pane_id)
        # Hash the plaintext so change detection isn't defeated by encryption's
        # random per-push IV producing a different ciphertext each time.
        row_hash = _row_hash(w["window_name"], w["window_active"], w["session_attached"], content)
        current_hashes[pane_id] = row_hash
        if _last_hashes.get(pane_id) != row_hash:
            changes.append(
                {
                    "pane_id": pane_id,
                    "session_name": w["session_name"],
                    "window_index": w["window_index"],
                    "window_name": w["window_name"],
                    "window_active": w["window_active"],
                    "session_attached": w["session_attached"],
                    "content": crypto.encrypt(content, key),
                    "encrypted": True,
                }
            )

    if not changes and set(current_hashes) == set(_last_hashes):
        return 0  # nothing changed, nothing closed — skip the network call

    req = urllib.request.Request(
        f"{config.tower_url}/api/push",
        data=json.dumps({"op": "push-tmux", "live": live, "changes": changes}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.push_secret}",
            "Content-Type": "application/json",
            "User-Agent": "outpost-agent/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()

    _last_hashes.clear()
    _last_hashes.update(current_hashes)
    return len(changes)


def verify_key(tower_url: str, push_secret: str) -> bool:
    req = urllib.request.Request(
        f"{tower_url.rstrip('/')}/api/push",
        data=json.dumps({"op": "push-tmux", "live": [], "changes": []}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {push_secret}",
            "Content-Type": "application/json",
            "User-Agent": "outpost-agent/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.HTTPError:
        return False


def push_doc(config: Config, doc_id: str, title: str, doc_format: str, content: str) -> None:
    # Rides /api/push with op="push-doc" (rather than a separate endpoint)
    # so doc pushes reuse the same Cloudflare Access Bypass policy as pane
    # pushes (op="push-tmux").
    if not config.encryption_key:
        raise SystemExit(
            "No encryption password set. Run `outpost set-password` before pushing "
            "(doc content must be encrypted before it leaves this machine)."
        )

    key = base64.b64decode(config.encryption_key)
    body = {
        "op": "push-doc",
        "doc_id": doc_id,
        "title": title,
        "format": doc_format,
        "content": crypto.encrypt(content, key),
        "encrypted": True,
    }
    req = urllib.request.Request(
        f"{config.tower_url}/api/push",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.push_secret}",
            "Content-Type": "application/json",
            "User-Agent": "outpost-agent/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def fetch_encryption_salt(tower_url: str, push_secret: str) -> tuple[str, int]:
    # Rides /api/push with op="get-salt" (rather than GET /api/encryption-salt)
    # so this reaches the server through the same key-gated Access Bypass as
    # every other agent call, instead of needing its own Access exemption.
    req = urllib.request.Request(
        f"{tower_url.rstrip('/')}/api/push",
        data=json.dumps({"op": "get-salt"}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {push_secret}",
            "Content-Type": "application/json",
            "User-Agent": "outpost-agent/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return data["salt"], data["kdf_iterations"]
