import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import NamedTuple, TypedDict

from outpost import crypto
from outpost.config import Config


class WindowInfo(TypedDict):
    session_name: str
    window_id: str
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
                "#{session_name}\t#{window_id}\t#{window_index}\t#{window_name}"
                "\t#{window_active}\t#{session_attached}",
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
        session_name, window_id, window_index, window_name, window_active, session_attached = (
            line.split("\t")
        )
        windows.append(
            {
                "session_name": session_name,
                "window_id": window_id,
                "window_index": int(window_index),
                "window_name": window_name,
                "window_active": window_active == "1",
                "session_attached": session_attached == "1",
            }
        )
    return windows


def capture(window_id: str, lines: int) -> str:
    # tmux's window_id (e.g. "@12") is a stable, globally-unique target for
    # the lifetime of the server — unlike "session:window_index", which tmux
    # freely reassigns to a *different* window as soon as an earlier one
    # closes (it fills the gap), which would otherwise let a stale index
    # silently point capture/send at the wrong tab.
    target = window_id
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


# Menu prompts select these options on the keypress itself — a trailing Enter
# would advance past whatever the keypress just brought up.
_NO_ENTER_RESPONSES = {"1", "2", "3", "y", "p", "esc"}

# Codex's TUI treats a rapid stream of unbracketed characters as a paste. If
# Enter immediately follows literal text, it can be absorbed into that paste
# as a newline instead of submitting the composer. Its Unix detection window
# is only a few milliseconds; leave a comfortable human-keypress-sized gap.
_ENTER_DELAY_SECONDS = 0.05


def send_keys(pane_id: str, text: str) -> str | None:
    """Types `text` into a tmux pane, as if the user had typed it, then
    presses Enter to submit it. `-l` sends it literally so shell/readline
    special characters in a canned response aren't interpreted as key names.

    `"Tab"` and `"esc"` are control keypresses, so they're sent as tmux key
    names instead of `-l` text. Enter still follows Tab, while esc is an
    immediate keypress and does not need one.

    Enter is skipped only for `_NO_ENTER_RESPONSES` — single-keypress menu
    selections that take effect immediately, unlike Tab.

    Returns None only when every required tmux command succeeded. Otherwise
    returns an error suitable for reporting to the user."""

    def run_tmux(command: list[str], action: str) -> str | None:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return None
        detail = result.stderr.strip()
        suffix = f": {detail}" if detail else f" (exit {result.returncode})"
        return f"{action} failed{suffix}"

    try:
        if text in {"Tab", "esc"}:
            key = "Escape" if text == "esc" else text
            error = run_tmux(
                ["tmux", "send-keys", "-t", pane_id, key],
                f"sending {key}",
            )
        else:
            error = run_tmux(
                ["tmux", "send-keys", "-l", "-t", pane_id, text],
                "sending text",
            )
        if error:
            return error
        if text not in _NO_ENTER_RESPONSES:
            if text != "Tab":
                time.sleep(_ENTER_DELAY_SECONDS)
            return run_tmux(
                ["tmux", "send-keys", "-t", pane_id, "Enter"],
                "sending Enter after text",
            )
        return None
    except FileNotFoundError:
        return "tmux executable not found"
    except subprocess.TimeoutExpired:
        return "tmux send-keys timed out"


def current_pane_id() -> str | None:
    """Returns the tmux window_id (e.g. "@12") of the pane this process is
    running in, or None if it's not running inside tmux at all. Used by
    `outpost run` to exclude its own pane from what it pushes — resolved via
    tmux's own `$TMUX_PANE` identity rather than by pattern-matching pane
    content, which is unreliable (any pane whose scrollback happens to
    contain the right substring — e.g. from viewing this file's source, or
    from an unrelated earlier command — would be excluded too). window_id
    rather than session:window_index so the exclusion still matches even if
    tab layout shifts between calls (see `capture`)."""
    tmux_pane = os.environ.get("TMUX_PANE")
    if not tmux_pane:
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", tmux_pane, "#{window_id}"],
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


class PushResult(NamedTuple):
    changed: int
    applied: list[tuple[str, str]]
    failed: list[tuple[str, str, str]] = []


def push_once(
    config: Config,
    exclude_pane_id: str | None = None,
    responses: list[str] | None = None,
    verbose: bool = False,
) -> PushResult:
    """Captures and pushes all live tmux panes, except `exclude_pane_id` (if
    given) — used by `outpost run`'s loop to keep its own noisy pane out of
    the tower. One-shot `outpost push` passes None so it pushes everything,
    including whatever pane it was run from.

    `responses` is the CLI's own allowlist of canned strings (e.g. "yes",
    "continue") the web UI is allowed to queue up for a pane — advertised to
    the server so it knows what buttons to show, and re-checked against
    here. That local re-check is what actually enforces the allowlist: even
    a compromised server response can't make this agent type something the
    CLI wasn't configured to accept.

    Returns a `PushResult` — `applied` lists every (pane_id, text) actually
    typed into a pane, so the caller can report it instead of silently
    acting on it."""
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
        pane_id = w["window_id"]
        if pane_id == exclude_pane_id:
            continue
        content = capture(w["window_id"], config.capture_lines)
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

    if verbose:
        print(
            f"[tmux] discovered={len(windows)} live={len(live)} "
            f"excluded={exclude_pane_id or 'none'}",
            file=sys.stderr,
        )
        changed_ids = {change["pane_id"] for change in changes}
        for w in windows:
            window_id = w["window_id"]
            state = (
                "excluded"
                if window_id == exclude_pane_id
                else "changed"
                if window_id in changed_ids
                else "unchanged"
            )
            print(
                f"[tmux] {window_id} {w['session_name']}:{w['window_index']} "
                f"name={w['window_name']!r} active={w['window_active']} "
                f"attached={w['session_attached']} state={state}",
                file=sys.stderr,
            )

    # When canned responses are enabled, the push is also how the agent polls
    # for queued commands — so it can't be skipped just because pane content
    # is unchanged (that's exactly the state a pending "yes"/"continue" is
    # meant to unstick).
    if not changes and not responses and set(current_hashes) == set(_last_hashes):
        if verbose:
            print("[tmux] network request skipped: no changes or polling", file=sys.stderr)
        return PushResult(0, [])  # nothing changed, nothing closed — skip the network call

    body: dict = {"op": "push-tmux", "live": live, "changes": changes}
    if responses:
        body["responses"] = responses
    encoded_body = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        f"{config.tower_url}/api/push",
        data=encoded_body,
        headers={
            "Authorization": f"Bearer {config.push_secret}",
            "Content-Type": "application/json",
            "User-Agent": "outpost-agent/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        status = getattr(resp, "status", "unknown")
    if verbose:
        print(
            f"[tmux] POST /api/push status={status} bytes={len(encoded_body)} "
            f"live={len(live)} changes={len(changes)}",
            file=sys.stderr,
        )
    try:
        resp_data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        resp_data = {}  # older server not yet returning JSON here — no commands to act on

    applied: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []
    if responses:
        allowed = set(responses)
        for pane_id, text in (resp_data.get("commands") or {}).items():
            if pane_id in live and text in allowed:
                error = send_keys(pane_id, text)
                if error:
                    failed.append((pane_id, text, error))
                    if verbose:
                        print(
                            f"[tmux] command {text!r} target={pane_id} failed: {error}",
                            file=sys.stderr,
                        )
                else:
                    applied.append((pane_id, text))
                    if verbose:
                        print(
                            f"[tmux] command {text!r} target={pane_id} delivered",
                            file=sys.stderr,
                        )

    _last_hashes.clear()
    _last_hashes.update(current_hashes)
    return PushResult(len(changes), applied, failed)


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
