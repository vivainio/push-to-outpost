import hashlib
import json
import time
from pathlib import Path
from typing import TypedDict

from outpost.agent import push_doc
from outpost.config import Config

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Individual message blocks (a tool result dumping a whole file, say) are
# truncated to this many characters so one noisy tool call can't blow up the
# size of every push for the rest of the session.
_BLOCK_LIMIT = 4000

# Markdown/html docs live in a D1 text column (unlike zips, which go to R2) —
# cap the whole rendered transcript so a very long-running session can't push
# a row D1 chokes on. Keep the tail: recent messages matter more than the
# start of a long conversation.
_DOCUMENT_LIMIT = 400_000


class SessionFile(TypedDict):
    session_id: str
    path: Path
    mtime: float


def discover_sessions(max_age_seconds: float) -> list[SessionFile]:
    """Find Claude Code session transcripts modified within the last `max_age_seconds`."""
    if not CLAUDE_PROJECTS_DIR.is_dir():
        return []
    cutoff = time.time() - max_age_seconds
    sessions: list[SessionFile] = []
    for path in CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        sessions.append({"session_id": path.stem, "path": path, "mtime": mtime})
    return sessions


def _truncate(text: str) -> str:
    if len(text) <= _BLOCK_LIMIT:
        return text
    return text[:_BLOCK_LIMIT] + f"\n...[truncated, {len(text)} chars total]"


def _first_text_block(content: object) -> str | None:
    """Extracts the raw (unheaded) text of a message, for use as a title fallback."""
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text.strip():
                parts.append(text)
    return "\n\n".join(parts) if parts else None


def _render_message(role: str, content: object) -> str | None:
    # Only the conversational text is rendered — tool calls, tool results,
    # and edits are dropped. They're what makes a pushed transcript huge and
    # unreadable, and they're not the part worth glancing at from the phone;
    # the actual code changes are already in the repo.
    #
    # Each turn gets its own heading (rather than an inline "**User:**"
    # prefix) so it's actually distinguishable at a glance in the rendered
    # doc — headings get their own font-size/weight in viewer.html's CSS,
    # bold-inline text at the start of a paragraph doesn't stand out from
    # the rest of the paragraph around it.
    heading = "### \U0001f9d1 User" if role == "user" else "### \U0001f916 Claude"

    if isinstance(content, str):
        if not content.strip():
            return None
        return f"{heading}\n\n{_truncate(content)}"

    if not isinstance(content, list):
        return None

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text", "")
            if text.strip():
                parts.append(_truncate(text))
    return f"{heading}\n\n" + "\n\n".join(parts) if parts else None


def render_session(path: Path) -> tuple[str, str]:
    """Renders a session's JSONL transcript to markdown. Returns (title, content)."""
    cwd = None
    git_branch = None
    ai_title = None
    first_user_text = None
    rendered_messages: list[str] = []

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = entry.get("type")
        if etype in ("ai-title", "agent-name"):
            ai_title = entry.get("aiTitle") or entry.get("agentName") or ai_title
            continue
        if etype not in ("user", "assistant"):
            continue

        cwd = entry.get("cwd") or cwd
        git_branch = entry.get("gitBranch") or git_branch
        message = entry.get("message") or {}
        role = message.get("role", etype)
        content = message.get("content")
        if first_user_text is None and role == "user":
            first_user_text = _first_text_block(content)
        rendered = _render_message(role, content)
        if rendered:
            rendered_messages.append(rendered)

    title = ai_title or (first_user_text[:60] if first_user_text else path.stem)
    header = f"# {title}\n\n"
    if cwd:
        header += f"*cwd: `{cwd}`"
        if git_branch:
            header += f" · branch: `{git_branch}`"
        header += "*\n\n---\n\n"

    body = "\n\n".join(rendered_messages)
    if len(body) > _DOCUMENT_LIMIT:
        body = f"...[earlier messages truncated]\n\n{body[-_DOCUMENT_LIMIT:]}"
    return title, header + body


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


# Tracks the last-pushed content hash per session, mirroring `agent._last_hashes`
# — only meaningful within a single long-running `run` process.
_last_hashes: dict[str, str] = {}


def push_sessions(config: Config) -> int:
    """Renders and pushes any Claude Code session transcript that changed since
    the last cycle. Returns the number of sessions pushed."""
    sessions = discover_sessions(config.session_max_age)
    current_session_ids = {s["session_id"] for s in sessions}
    for stale_id in set(_last_hashes) - current_session_ids:
        del _last_hashes[stale_id]

    pushed = 0
    for session in sessions:
        title, content = render_session(session["path"])
        content_hash = _content_hash(content)
        if _last_hashes.get(session["session_id"]) == content_hash:
            continue
        push_doc(
            config,
            doc_id=f"session-{session['session_id']}",
            title=title,
            doc_format="markdown",
            content=content,
        )
        _last_hashes[session["session_id"]] = content_hash
        pushed += 1
    return pushed
