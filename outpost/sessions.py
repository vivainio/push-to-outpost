import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from outpost.agent import push_doc
from outpost.config import Config
from outpost.transcripts import Transcript, TranscriptMessage

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# How far from the end of a session file to read when looking for its last
# message timestamp. Every line (user, assistant, even tool/hook attachment
# lines) carries a top-level "timestamp" field, so a few KB is always enough
# to find one without reading the whole (possibly huge) transcript.
_TAIL_SCAN_BYTES = 16_384

# Individual message blocks (a tool result dumping a whole file, say) are
# truncated to this many characters so one noisy tool call can't blow up the
# size of every push for the rest of the session.
_BLOCK_LIMIT = 4000

# Markdown/html docs live in a D1 text column (unlike zips, which go to R2) —
# cap the whole rendered transcript so a very long-running session can't push
# a row D1 chokes on. Keep the tail: recent messages matter more than the
# start of a long conversation.
_DOCUMENT_LIMIT = 400_000

# Bash commands get a one-line preview instead of being dropped entirely like
# other tool calls — knowing *that* a command ran and roughly what it was is
# worth a phone glance; the full multi-line script isn't.
_COMMAND_SNIPPET_LIMIT = 100


class SessionFile(TypedDict):
    session_id: str
    path: Path
    mtime: float
    provider: str


class TodoItem(TypedDict):
    subject: str
    activeForm: str
    status: str


# TaskCreate only learns its new task's id from the tool_result text ("Task #3
# created successfully: ..."), not from the tool_use call itself — so a create
# has to be staged until that result line arrives.
_TASK_CREATED_RE = re.compile(r"^Task #(\d+) created successfully:")


def _last_entry_time(path: Path) -> float | None:
    """Best-effort: the timestamp of the last entry in a session transcript,
    read from the tail of the file rather than the whole thing so this stays
    cheap even for very long-running sessions."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > _TAIL_SCAN_BYTES:
                f.seek(-_TAIL_SCAN_BYTES, 2)
            data = f.read()
    except OSError:
        return None
    for line in reversed(data.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = entry.get("timestamp")
        if not isinstance(ts, str):
            continue
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return None


def discover_sessions(max_age_seconds: float) -> list[SessionFile]:
    """Find Claude Code and Codex CLI transcripts updated recently."""
    cutoff = time.time() - max_age_seconds
    sessions: list[SessionFile] = []
    sources = (
        ("claude", CLAUDE_PROJECTS_DIR, "*/*.jsonl"),
        ("codex", CODEX_SESSIONS_DIR, "*/*/*/*.jsonl"),
    )
    for provider, directory, pattern in sources:
        if not directory.is_dir():
            continue
        for path in directory.glob(pattern):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            # Cheap pre-filter: a file untouched since before the cutoff can't
            # contain anything newer, no need to open it.
            if mtime < cutoff:
                continue
            # Confirm against the transcript timestamp in case another program
            # merely touched an old file.
            last_entry_time = _last_entry_time(path)
            if last_entry_time is not None and last_entry_time < cutoff:
                continue
            session_id = path.stem
            if provider == "codex":
                # rollout filenames include a date prefix; the trailing UUID is
                # Codex's stable session id.
                match = re.search(
                    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
                    session_id,
                )
                if match:
                    session_id = match.group(1)
            sessions.append(
                {
                    "session_id": session_id,
                    "path": path,
                    "mtime": mtime,
                    "provider": provider,
                }
            )
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


def _bash_snippet(tool_input: dict) -> str | None:
    """A one-line preview of a Bash tool call — just the first line of the
    command, truncated, not the whole (often multi-line) script."""
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    first_line, sep, _rest = command.strip().partition("\n")
    truncated = bool(sep) or len(first_line) > _COMMAND_SNIPPET_LIMIT
    snippet = first_line[:_COMMAND_SNIPPET_LIMIT]
    return f"`$ {snippet}{'...' if truncated else ''}`"


def _render_message(role: str, content: object) -> str | None:
    # Only the conversational text is rendered — tool calls, tool results,
    # and edits are dropped (Bash gets a one-line command preview instead of
    # being dropped entirely, see _bash_snippet). They're what makes a pushed
    # transcript huge and unreadable, and they're not the part worth glancing
    # at from the phone; the actual code changes are already in the repo.
    #
    # Each turn gets its own heading (rather than an inline "**User:**"
    # prefix) so it's actually distinguishable at a glance in the rendered
    # doc — headings get their own font-size/weight in viewer.html's CSS,
    # bold-inline text at the start of a paragraph doesn't stand out from
    # the rest of the paragraph around it. Icon only, no "User"/"Claude"
    # label — the emoji alone is enough to tell them apart and a bare label
    # line wastes vertical space on the phone this is meant for.
    heading = "### \U0001f9d1" if role == "user" else "### \U0001f916"

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
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if text.strip():
                parts.append(_truncate(text))
        elif btype == "tool_use" and block.get("name") == "Bash":
            snippet = _bash_snippet(block.get("input") or {})
            if snippet:
                parts.append(snippet)
    return f"{heading}\n\n" + "\n\n".join(parts) if parts else None


def _tool_result_text(content: object) -> str | None:
    """Extracts the text of a tool_result block's content (a plain string, or
    a list of content blocks like a rendered message)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(parts) if parts else None
    return None


def _update_todos(
    content: object, todos: dict[str, TodoItem], pending_creates: dict[str, dict]
) -> None:
    """Replays TaskCreate/TaskUpdate tool calls to track the todo list's
    current state, mutating `todos` and `pending_creates` in place."""
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use" and block.get("name") == "TaskCreate":
            pending_creates[block.get("id")] = block.get("input") or {}
        elif btype == "tool_use" and block.get("name") == "TaskUpdate":
            task_input = block.get("input") or {}
            task = todos.get(task_input.get("taskId"))
            if task is None:
                continue
            if "status" in task_input:
                task["status"] = task_input["status"]
            if "subject" in task_input:
                task["subject"] = task_input["subject"]
            if "activeForm" in task_input:
                task["activeForm"] = task_input["activeForm"]
        elif btype == "tool_result":
            create_input = pending_creates.pop(block.get("tool_use_id"), None)
            if create_input is None:
                continue
            match = _TASK_CREATED_RE.match(_tool_result_text(block.get("content")) or "")
            if not match:
                continue
            todos[match.group(1)] = {
                "subject": create_input.get("subject", ""),
                "activeForm": create_input.get("activeForm") or create_input.get("subject", ""),
                "status": "pending",
            }


def _render_todos(todos: dict[str, TodoItem]) -> str | None:
    """Renders the current todo list as a checklist — only the live snapshot,
    not the history of how it got there."""
    live = [
        todo
        for _, todo in sorted(todos.items(), key=lambda kv: int(kv[0]))
        if todo["status"] != "deleted"
    ]
    if not live:
        return None
    lines = ["### \U0001f4cb Tasks", ""]
    for todo in live:
        if todo["status"] == "in_progress":
            lines.append(f"- [ ] **{todo['activeForm']}** _(in progress)_")
        else:
            mark = "x" if todo["status"] == "completed" else " "
            lines.append(f"- [{mark}] {todo['subject']}")
    return "\n".join(lines)


def parse_claude_session(path: Path) -> Transcript:
    """Parse a Claude Code JSONL transcript into the common model."""
    cwd = None
    git_branch = None
    ai_title = None
    first_user_text = None
    messages: list[TranscriptMessage] = []
    todos: dict[str, TodoItem] = {}
    pending_creates: dict[str, dict] = {}

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
        if entry.get("isMeta"):
            # Synthetic turns injected by the harness, not the human — skill
            # stub expansions ("Base directory for this skill: ..."), resume
            # markers ("Continue from where you left off."), local-command
            # caveats. Noise, same as tool calls.
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
            messages.append(TranscriptMessage(role=role, text=rendered))
        _update_todos(content, todos, pending_creates)

    title = ai_title or (first_user_text[:60] if first_user_text else path.stem)
    todo_section = _render_todos(todos)
    return Transcript(
        session_id=path.stem,
        title=title,
        cwd=cwd,
        git_branch=git_branch,
        messages=messages,
        sections=[todo_section] if todo_section else [],
    )


def render_transcript(transcript: Transcript) -> tuple[str, str]:
    """Render a provider-neutral transcript as Markdown."""
    header = f"# {transcript.title}\n\n"
    if transcript.cwd:
        header += f"*cwd: `{transcript.cwd}`"
        if transcript.git_branch:
            header += f" · branch: `{transcript.git_branch}`"
        header += "*\n\n---\n\n"
    for section in transcript.sections:
        header += section + "\n\n---\n\n"
    body = "\n\n".join(message.text for message in transcript.messages)
    if len(body) > _DOCUMENT_LIMIT:
        body = f"...[earlier messages truncated]\n\n{body[-_DOCUMENT_LIMIT:]}"
    return transcript.title, header + body


def render_session(path: Path) -> tuple[str, str]:
    return render_transcript(parse_claude_session(path))


def _codex_text(content: object) -> str | None:
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in ("input_text", "output_text"):
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n\n".join(parts) if parts else None


def parse_codex_session(path: Path) -> Transcript:
    """Parse a Codex CLI rollout into the common model."""
    cwd = None
    git_branch = None
    session_id = path.stem
    first_user_text = None
    messages: list[TranscriptMessage] = []

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = entry.get("payload") or {}
        if entry.get("type") == "session_meta":
            cwd = payload.get("cwd") or cwd
            session_id = payload.get("session_id") or payload.get("id") or session_id
            git = payload.get("git") or {}
            git_branch = git.get("branch") or git_branch
            continue
        if entry.get("type") == "turn_context":
            cwd = payload.get("cwd") or cwd
            continue
        if entry.get("type") != "response_item" or payload.get("type") != "message":
            continue
        role = payload.get("role")
        # Developer/system messages contain the harness instructions, not the
        # conversation the user wants to see remotely.
        if role not in ("user", "assistant"):
            continue
        text = _codex_text(payload.get("content"))
        if not text:
            continue
        if role == "user" and first_user_text is None:
            first_user_text = text
        rendered = _render_message(role, text)
        if rendered:
            messages.append(TranscriptMessage(role=role, text=rendered))

    title = first_user_text[:60] if first_user_text else str(session_id)
    return Transcript(
        session_id=str(session_id),
        title=title,
        cwd=cwd,
        git_branch=git_branch,
        messages=messages,
    )


def render_codex_session(path: Path) -> tuple[str, str]:
    return render_transcript(parse_codex_session(path))


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


# Tracks the last-pushed content hash per session, mirroring `agent._last_hashes`
# — only meaningful within a single long-running `run` process.
_last_hashes: dict[str, str] = {}


def push_sessions(config: Config) -> int:
    """Render and push changed Claude Code and Codex CLI transcripts."""
    sessions = discover_sessions(config.session_max_age)
    def cache_key(session: SessionFile) -> str:
        # Keep Claude's existing identifiers stable; namespace Codex to avoid
        # the unlikely case where both products use the same UUID.
        if session["provider"] == "claude":
            return session["session_id"]
        return f"codex:{session['session_id']}"

    current_session_ids = {cache_key(s) for s in sessions}
    for stale_id in set(_last_hashes) - current_session_ids:
        del _last_hashes[stale_id]

    pushed = 0
    for session in sessions:
        key = cache_key(session)
        renderer = render_codex_session if session["provider"] == "codex" else render_session
        title, content = renderer(session["path"])
        content_hash = _content_hash(content)
        if _last_hashes.get(key) == content_hash:
            continue
        push_doc(
            config,
            doc_id=(
                f"session-{session['session_id']}"
                if session["provider"] == "claude"
                else f"session-codex-{session['session_id']}"
            ),
            title=title,
            doc_format="markdown",
            content=content,
        )
        _last_hashes[key] = content_hash
        pushed += 1
    return pushed
