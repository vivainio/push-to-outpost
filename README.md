# push-to-outpost

**outpost** is a remote, read-only viewer for what's happening on your dev
machine: tmux panes, Claude Code session transcripts, and ad-hoc docs — all
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
- **Optional end-to-end encryption** (`outpost set-password`): pane/doc
  content is encrypted client-side with AES-256-GCM before it's sent, using
  a key derived from your password via PBKDF2-HMAC-SHA256 (210,000
  iterations). The password itself is never transmitted or stored anywhere
  — only the ciphertext reaches the server, and it's decrypted again
  client-side in the browser. That means even whoever runs the outpost
  server can't read your session content from the database; they only
  ever see encrypted bytes.
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
outpost push                # one-off push
outpost run                 # loop forever, pushing every 15s
outpost push-doc notes.md   # push a markdown/html/zip file separately from tmux panes
outpost set-password        # enable end-to-end encryption (same password entered on the site)
```

`PUSH_INTERVAL` (seconds, default 15), `CAPTURE_LINES` (default 500), and
`SESSION_MAX_AGE_MINUTES` (default 60) can be set as environment variables
to override the defaults.

## License

MIT
