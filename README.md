# push-to-outpost

Local push agent for [outpost](https://github.com/vivainio) — pushes tmux
pane snapshots, Claude Code session transcripts, and ad-hoc docs to a
remote outpost site over HTTPS. No inbound connections to your machine.

Requires an outpost server already deployed and reachable — this package
is just the CLI that pushes to it.

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

`PUSH_INTERVAL` (seconds, default 15), `CAPTURE_LINES` (default 2000), and
`SESSION_MAX_AGE_MINUTES` (default 60) can be set as environment variables
to override the defaults.

## License

MIT
