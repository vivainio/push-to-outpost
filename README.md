# push-to-outpost

**outpost** is a remote, read-only viewer for what's happening on your dev
machine: tmux panes, Claude Code and Codex CLI session transcripts, and ad-hoc docs — all
pushed out over HTTPS so you can check on them from your phone or another
computer without opening an inbound connection to anything.

This repo is the public half: the local Python agent that captures and
pushes. The server (a Cloudflare Worker + web UI, gated behind Cloudflare
Access) is closed-source for now — it can be made public if there's
interest. This package is just the CLI that pushes to it, and requires a
server already deployed and reachable.

## Security

- **No inbound connections.** The agent only ever makes outbound HTTPS
  requests; nothing listens on your machine.
- **Push auth**: a per-agent API key, stored server-side only as a SHA-256
  hash. The plaintext secret is shown once at creation and never persisted.
- **Mandatory end-to-end encryption.** Every push is encrypted client-side
  with AES-256-GCM before it's sent, using a key derived from a password
  (`outpost set-password`) via PBKDF2-HMAC-SHA256 (210,000 iterations); the
  agent refuses to push anything until a password has been set. The
  password itself is never transmitted or stored anywhere — only the
  ciphertext reaches the server, and it's decrypted again client-side in
  the browser. That means even whoever runs the outpost server can't read
  your session content from the database; they only ever see encrypted
  bytes.
- **Site access** is gated by Cloudflare Access (separate from anything
  in this repo), so the web UI itself requires its own login.
- **Pushed objects expire after 24 hours** and are deleted server-side;
  nothing is retained long-term.

## Requirements

- Python 3.11+
- [wincred](https://github.com/vivainio/wincred) (`wincred.exe` on `PATH`) —
  this CLI only supports WSL2 + Windows Credential Manager for storing
  config. There's no other credential backend.

## Install

```
pip install push-to-outpost
```

## Usage

```
outpost login              # opens the site, paste in an API key generated there
outpost set-password        # required before pushing — same password entered on the site
outpost qr                  # generates a new random password instead, shown as a QR code to scan on the site
outpost push                # one-off push
outpost run                 # loop forever, pushing every 15s
outpost push-doc notes.md   # push a markdown/html/zip file separately from tmux panes
outpost push-doc notes/     # zip a directory on the fly and push it as one doc
```

`PUSH_INTERVAL` (seconds, default 15), `CAPTURE_LINES` (default 500), and
`SESSION_MAX_AGE_MINUTES` (default 60) can be set as environment variables
to override the defaults.

`outpost run --responses "yes,continue,commit and push"` advertises a fixed
set of canned replies the web UI can send back to a pane (defaults to
`yes,continue,commit and push,1,2,3,y,p,esc,Tab` if the flag is omitted; pass
`--responses ""` to disable). The agent only ever types a response into a
pane if it's a member of this list — even a compromised server can't make it
send anything else. `1`/`2`/`3`/`y`/`p` are menu keypresses with no trailing
Enter (they take effect immediately); `esc` sends Escape without Enter.
`Tab` is sent as an actual keypress rather than typed out literally, still
followed by Enter. Every
applied response is printed (`sent "1" to @3`) so
it's visible whether one was actually delivered.

## License

MIT
