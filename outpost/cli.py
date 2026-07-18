import argparse
import base64
import re
import secrets
import sys
import time
import urllib.error
import webbrowser
from pathlib import Path

import qrcode

from outpost.agent import current_pane_id, fetch_encryption_salt, push_doc, push_once, verify_key
from outpost.config import Config, save_credentials
from outpost.crypto import derive_key, fingerprint
from outpost.sessions import push_sessions

DEFAULT_TOWER_URL = "https://outpost.vivainio.workers.dev"

# Canned responses the web UI can queue up per pane, delivered on the agent's
# next poll and typed in via tmux send-keys. This is the CLI's own allowlist
# (advertised to the server, then re-checked against on receipt) — the server
# never gets to introduce a new one, only pick among these.
DEFAULT_RESPONSES = ["yes", "continue", "commit and push"]


def _parse_responses(raw: str | None) -> list[str]:
    if raw is None:
        return list(DEFAULT_RESPONSES)
    if not raw.strip():
        return []  # explicit empty string disables the feature
    return [r.strip() for r in raw.split(",") if r.strip()]


FORMAT_BY_SUFFIX = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "html",
    ".htm": "html",
    ".zip": "zip",
}


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug or "doc"


def cmd_login(args: argparse.Namespace) -> None:
    tower_url = args.tower_url.rstrip("/")
    print(f'Opening {tower_url} — sign in, click "API keys", then generate one.')
    webbrowser.open(tower_url)
    while True:
        secret = input("Paste the API key here: ").strip()
        if not secret:
            print("No key entered, try again (ctrl-c to abort).")
            continue
        if not re.fullmatch(r"[0-9a-fA-F]+", secret):
            print(
                "That doesn't look like a valid key (unexpected characters) "
                "— check your paste and try again."
            )
            continue
        print("Verifying key...")
        try:
            valid = verify_key(tower_url, secret)
        except (urllib.error.URLError, OSError) as exc:
            print(f"Couldn't reach the server ({exc}) — try again.")
            continue
        if not valid:
            print("That key was rejected by the server. Check it and try again.")
            continue
        break
    location = save_credentials(tower_url, secret)
    print(f"Saved to {location}. You can now run: outpost run")


def _fetch_salt(config: Config) -> tuple[str, int]:
    print("Fetching encryption salt...")
    try:
        return fetch_encryption_salt(config.tower_url, config.push_secret)
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"Couldn't reach the server ({exc}). Try again.")


def _apply_password(config: Config, password: str) -> str:
    salt, iterations = _fetch_salt(config)
    key = derive_key(password, salt, iterations)
    encryption_key = base64.b64encode(key).decode()
    return save_credentials(config.tower_url, config.push_secret, encryption_key=encryption_key)


def cmd_set_password(args: argparse.Namespace) -> None:
    config = Config.from_env()
    while True:
        password = input("Password (same one you'll enter on the website): ").strip()
        if password:
            break
        print("No password entered, try again (ctrl-c to abort).")

    location = _apply_password(config, password)
    print(f"Saved to {location}. Pane content will now be encrypted before it's pushed.")


def cmd_qr(args: argparse.Namespace) -> None:
    config = Config.from_env()
    # A random password beats a typed one here: it's never entered by hand
    # anywhere, only ever transferred by scanning the QR code below, so
    # there's no reason for it to be short or memorable.
    password = secrets.token_hex(16)
    location = _apply_password(config, password)

    qr = qrcode.QRCode(border=1)
    qr.add_data(password)
    qr.make(fit=True)
    print()
    qr.print_ascii(invert=True)
    print(f"\nFingerprint: {fingerprint(password)}")
    print(f"New encryption password generated and saved to {location}.")
    print('Scan this with the website\'s password panel ("Scan QR code") to unlock it there —')
    print("the fingerprint shown there afterward should match the one above.")


def cmd_push(args: argparse.Namespace) -> None:
    config = Config.from_env()
    responses = _parse_responses(args.responses)
    count = push_once(config, responses=responses)
    session_count = push_sessions(config)
    print(f"pushed {count} window(s) and {session_count} session(s) to {config.tower_url}")


def cmd_push_doc(args: argparse.Namespace) -> None:
    path = Path(args.path)
    if not path.is_file():
        raise SystemExit(f"No such file: {path}")

    doc_format = args.format or FORMAT_BY_SUFFIX.get(path.suffix.lower())
    if not doc_format:
        raise SystemExit(
            f"Can't infer format from {path.suffix!r} — "
            "pass --format {markdown,html,zip} explicitly."
        )

    title = args.title or path.name
    doc_id = args.id or _slugify(path.stem)

    if doc_format == "zip":
        content = base64.b64encode(path.read_bytes()).decode()
    else:
        content = path.read_text(encoding="utf-8")

    config = Config.from_env()
    push_doc(config, doc_id, title, doc_format, content)
    print(f"pushed doc {doc_id!r} ({doc_format}) to {config.tower_url}")


def cmd_run(args: argparse.Namespace) -> None:
    config = Config.from_env()
    self_pane_id = current_pane_id()
    responses = _parse_responses(args.responses)
    print(f"pushing to {config.tower_url} every {config.push_interval}s (ctrl-c to stop)")
    if responses:
        print(f"canned responses enabled: {', '.join(responses)}")
    while True:
        start = time.monotonic()
        try:
            count = push_once(config, exclude_pane_id=self_pane_id, responses=responses)
            session_count = push_sessions(config)
            if count or session_count:
                print(f"pushed {count} changed window(s), {session_count} changed session(s)")
            else:
                print("no changes, skipped")
        except urllib.error.URLError as exc:
            print(f"push failed: {exc}", file=sys.stderr)
        elapsed = time.monotonic() - start
        time.sleep(max(0.0, config.push_interval - elapsed))


def main() -> None:
    parser = argparse.ArgumentParser(prog="outpost", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    login_parser = sub.add_parser(
        "login", help="Open the site, generate an API key, and save it via wincred"
    )
    login_parser.add_argument("--tower-url", default=DEFAULT_TOWER_URL, help="Site URL")
    login_parser.set_defaults(func=cmd_login)

    set_password_parser = sub.add_parser(
        "set-password",
        help="Set the shared encryption password (enter the same one on the website)",
    )
    set_password_parser.set_defaults(func=cmd_set_password)

    qr_parser = sub.add_parser(
        "qr", help="Generate a random encryption password, shown as a QR code to scan on the site"
    )
    qr_parser.set_defaults(func=cmd_qr)

    responses_help = (
        "Comma-separated canned responses the web UI can send back to a pane "
        f'(default: "{", ".join(DEFAULT_RESPONSES)}"; pass "" to disable)'
    )

    push_parser = sub.add_parser("push", help="Push a single snapshot and exit")
    push_parser.add_argument("--responses", default=None, help=responses_help)
    push_parser.set_defaults(func=cmd_push)

    run_parser = sub.add_parser("run", help="Push snapshots on a loop until stopped")
    run_parser.add_argument("--responses", default=None, help=responses_help)
    run_parser.set_defaults(func=cmd_run)

    push_doc_parser = sub.add_parser(
        "push-doc", help="Push a markdown/html/zip file, rendered separately from tmux sessions"
    )
    push_doc_parser.add_argument("path", help="Path to a .md, .html, or .zip file")
    push_doc_parser.add_argument("--title", help="Display title (default: filename)")
    push_doc_parser.add_argument(
        "--format",
        choices=["markdown", "html", "zip"],
        help="Override format inferred from the file extension",
    )
    push_doc_parser.add_argument(
        "--id", help="Stable doc id to upsert on re-push (default: slugified filename)"
    )
    push_doc_parser.set_defaults(func=cmd_push_doc)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
