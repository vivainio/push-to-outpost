import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from outpost import sessions
from outpost.config import Config


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")


@pytest.fixture
def fake_config():
    return Config(
        tower_url="https://example.com",
        push_secret="secret",
        push_interval=15.0,
        capture_lines=2000,
        session_max_age=3600.0,
        encryption_key="abc==",
    )


class TestDiscoverSessions:
    def test_returns_empty_list_when_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sessions, "CLAUDE_PROJECTS_DIR", tmp_path / "does-not-exist")
        assert sessions.discover_sessions(3600) == []

    def test_finds_recent_sessions_and_skips_old_ones(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sessions, "CLAUDE_PROJECTS_DIR", tmp_path)
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        recent = project_dir / "recent-session.jsonl"
        recent.write_text("{}")

        old = project_dir / "old-session.jsonl"
        old.write_text("{}")
        old_time = time.time() - 7200
        os.utime(old, (old_time, old_time))

        found = sessions.discover_sessions(max_age_seconds=3600)

        assert [f["session_id"] for f in found] == ["recent-session"]
        assert found[0]["path"] == recent

    def test_ignores_non_jsonl_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sessions, "CLAUDE_PROJECTS_DIR", tmp_path)
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        (project_dir / "notes.txt").write_text("hello")

        assert sessions.discover_sessions(3600) == []

    def test_skips_old_content_with_a_falsely_recent_mtime(self, tmp_path, monkeypatch):
        # Regression: a file whose content is old but whose mtime was reset
        # to "now" (a backup restore, a sync tool touching it, etc.) must not
        # be resurrected as a recent session.
        monkeypatch.setattr(sessions, "CLAUDE_PROJECTS_DIR", tmp_path)
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        stale = project_dir / "stale-session.jsonl"
        old_ts = "2020-01-01T00:00:00.000Z"
        entry = {"type": "user", "timestamp": old_ts, "message": {"role": "user", "content": "hi"}}
        _write_jsonl(stale, [entry])
        # File was just touched, so mtime looks recent even though its one
        # message is years old.

        assert sessions.discover_sessions(max_age_seconds=3600) == []

    def test_keeps_recent_content_even_with_stale_looking_timestamp_parse_failure(
        self, tmp_path, monkeypatch
    ):
        # If the timestamp can't be parsed for some reason, fall back to
        # trusting mtime rather than silently dropping the session.
        monkeypatch.setattr(sessions, "CLAUDE_PROJECTS_DIR", tmp_path)
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        path = project_dir / "session.jsonl"
        entry = {
            "type": "user",
            "timestamp": "not-a-timestamp",
            "message": {"role": "user", "content": "hi"},
        }
        _write_jsonl(path, [entry])

        found = sessions.discover_sessions(max_age_seconds=3600)
        assert [f["session_id"] for f in found] == ["session"]

    def test_finds_codex_rollouts_and_extracts_session_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sessions, "CLAUDE_PROJECTS_DIR", tmp_path / "no-claude")
        monkeypatch.setattr(sessions, "CODEX_SESSIONS_DIR", tmp_path)
        day = tmp_path / "2026" / "07" / "18"
        day.mkdir(parents=True)
        path = day / "rollout-2026-07-18T12-00-00-12345678-1234-1234-1234-123456789abc.jsonl"
        _write_jsonl(path, [{"timestamp": "2099-01-01T00:00:00Z", "type": "session_meta"}])

        found = sessions.discover_sessions(3600)

        assert found == [
            {
                "session_id": "12345678-1234-1234-1234-123456789abc",
                "path": path,
                "mtime": path.stat().st_mtime,
                "provider": "codex",
            }
        ]


class TestTruncate:
    def test_leaves_short_text_unchanged(self):
        text = "short text"
        assert sessions._truncate(text) == text

    def test_truncates_long_text_with_marker(self):
        text = "x" * (sessions._BLOCK_LIMIT + 100)
        result = sessions._truncate(text)
        assert result.startswith("x" * sessions._BLOCK_LIMIT)
        assert "truncated" in result


class TestRenderMessage:
    def test_string_content(self):
        assert sessions._render_message("user", "hello there") == "### \U0001f9d1\n\nhello there"

    def test_assistant_string_content_uses_different_heading(self):
        rendered = sessions._render_message("assistant", "hi!")
        assert rendered.startswith("### \U0001f916")

    def test_blank_string_content_returns_none(self):
        assert sessions._render_message("user", "   ") is None

    def test_none_content_returns_none(self):
        assert sessions._render_message("user", None) is None

    def test_list_of_text_blocks(self):
        content = [{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}]
        rendered = sessions._render_message("user", content)
        assert rendered == "### \U0001f9d1\n\npart one\n\npart two"

    def test_list_drops_non_text_blocks(self):
        content = [
            {"type": "tool_use", "input": {"huge": "dump"}},
            {"type": "text", "text": "the actual reply"},
        ]
        rendered = sessions._render_message("assistant", content)
        assert rendered == "### \U0001f916\n\nthe actual reply"

    def test_list_with_only_non_text_blocks_returns_none(self):
        content = [{"type": "tool_use", "input": {}}]
        assert sessions._render_message("assistant", content) is None

    def test_truncates_individual_blocks(self):
        long_text = "y" * (sessions._BLOCK_LIMIT + 50)
        rendered = sessions._render_message("user", long_text)
        assert "truncated" in rendered

    def test_bash_tool_use_renders_command_snippet(self):
        content = [{"type": "tool_use", "name": "Bash", "input": {"command": "git status -sb"}}]
        rendered = sessions._render_message("assistant", content)
        assert rendered == "### \U0001f916\n\n`$ git status -sb`"

    def test_bash_snippet_preserves_order_with_surrounding_text(self):
        content = [
            {"type": "text", "text": "Let me check status."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}},
        ]
        rendered = sessions._render_message("assistant", content)
        assert rendered == ("### \U0001f916\n\nLet me check status.\n\n`$ git status`")

    def test_bash_snippet_shows_only_first_line(self):
        content = [
            {"type": "tool_use", "name": "Bash", "input": {"command": "git status\ngit diff"}}
        ]
        rendered = sessions._render_message("assistant", content)
        assert "`$ git status...`" in rendered
        assert "git diff" not in rendered

    def test_bash_snippet_truncates_long_first_line(self):
        long_command = "x" * (sessions._COMMAND_SNIPPET_LIMIT + 50)
        content = [{"type": "tool_use", "name": "Bash", "input": {"command": long_command}}]
        rendered = sessions._render_message("assistant", content)
        assert f"`$ {'x' * sessions._COMMAND_SNIPPET_LIMIT}...`" in rendered

    def test_non_bash_tool_use_still_dropped(self):
        content = [{"type": "tool_use", "name": "Edit", "input": {"command": "git status"}}]
        assert sessions._render_message("assistant", content) is None


class TestFirstTextBlock:
    def test_string_content(self):
        assert sessions._first_text_block("  hello  ") == "hello"

    def test_blank_string_returns_none(self):
        assert sessions._first_text_block("   ") is None

    def test_list_content_joins_text_blocks(self):
        content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        assert sessions._first_text_block(content) == "a\n\nb"

    def test_none_returns_none(self):
        assert sessions._first_text_block(None) is None


def _user_entry(text, cwd=None, git_branch=None):
    entry = {"type": "user", "message": {"role": "user", "content": text}}
    if cwd:
        entry["cwd"] = cwd
    if git_branch:
        entry["gitBranch"] = git_branch
    return entry


def _assistant_entry(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": text}}


class TestRenderSession:
    def test_title_prefers_ai_title_over_first_user_text(self, tmp_path):
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [
                {"type": "ai-title", "aiTitle": "Fix the login bug"},
                _user_entry("please fix the login bug"),
                _assistant_entry("done"),
            ],
        )
        title, _ = sessions.render_session(path)
        assert title == "Fix the login bug"

    def test_title_falls_back_to_clean_first_user_text(self, tmp_path):
        # Regression test: first_user_text must be the raw message text, not
        # the rendered "### 🧑\n\n..." heading it's wrapped in for the body.
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_user_entry("please fix the login bug"), _assistant_entry("done")])
        title, _ = sessions.render_session(path)
        assert title == "please fix the login bug"
        assert "User" not in title
        assert "#" not in title

    def test_title_falls_back_to_path_stem_when_no_text(self, tmp_path):
        path = tmp_path / "empty-session.jsonl"
        _write_jsonl(path, [])
        title, _ = sessions.render_session(path)
        assert title == "empty-session"

    def test_title_truncated_to_60_chars(self, tmp_path):
        path = tmp_path / "session.jsonl"
        long_text = "x" * 200
        _write_jsonl(path, [_user_entry(long_text)])
        title, _ = sessions.render_session(path)
        assert title == long_text[:60]

    def test_header_includes_cwd_and_branch(self, tmp_path):
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [
                _user_entry("hi", cwd="/home/v/project", git_branch="main"),
                _assistant_entry("hello"),
            ],
        )
        _, content = sessions.render_session(path)
        assert "*cwd: `/home/v/project` · branch: `main`*" in content

    def test_header_omits_branch_when_absent(self, tmp_path):
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_user_entry("hi", cwd="/home/v/project"), _assistant_entry("hello")])
        _, content = sessions.render_session(path)
        assert "*cwd: `/home/v/project`*" in content
        assert "branch" not in content

    def test_body_contains_rendered_turns(self, tmp_path):
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_user_entry("hi there"), _assistant_entry("hello back")])
        _, content = sessions.render_session(path)
        assert "hi there" in content
        assert "hello back" in content

    def test_skips_malformed_json_lines(self, tmp_path):
        path = tmp_path / "session.jsonl"
        path.write_text("not json\n" + json.dumps(_user_entry("hi")), encoding="utf-8")
        _, content = sessions.render_session(path)
        assert "hi" in content

    def test_truncates_overlong_body_keeping_the_tail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sessions, "_DOCUMENT_LIMIT", 50)
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [_user_entry("a" * 100), _assistant_entry("b" * 100), _user_entry("z" * 20)],
        )
        _, content = sessions.render_session(path)
        assert "z" * 20 in content
        assert "earlier messages truncated" in content

    def test_skips_skill_expansion_meta_turns(self, tmp_path):
        # A Skill tool call gets its stub content injected back as a synthetic
        # "user" turn (isMeta: true) — not something the human typed. It
        # shouldn't clutter the rendered transcript.
        path = tmp_path / "session.jsonl"
        skill_entry = _user_entry(
            "Base directory for this skill: /home/v/.claude/skills/agent-browser\n"
            "# agent-browser\n\nFast browser automation CLI...\n\nARGUMENTS: take a screenshot"
        )
        skill_entry["isMeta"] = True
        _write_jsonl(
            path, [_user_entry("please take a screenshot"), skill_entry, _assistant_entry("done")]
        )
        _, content = sessions.render_session(path)
        assert "Base directory for this skill" not in content
        assert "please take a screenshot" in content

    def test_meta_turn_does_not_win_title_fallback(self, tmp_path):
        path = tmp_path / "session.jsonl"
        skill_entry = _user_entry("Base directory for this skill: /home/v/.claude/skills/foo")
        skill_entry["isMeta"] = True
        _write_jsonl(path, [skill_entry, _user_entry("the real first message")])
        title, _ = sessions.render_session(path)
        assert title == "the real first message"


class TestRenderCodexSession:
    def test_renders_conversation_and_omits_harness_messages(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        _write_jsonl(
            path,
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "session_id": "codex-id",
                        "cwd": "/work/project",
                        "git": {"branch": "main"},
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "secret harness instructions"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "fix the widget"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                },
            ],
        )

        title, content = sessions.render_codex_session(path)

        assert title == "fix the widget"
        assert "fix the widget" in content
        assert "done" in content
        assert "secret harness instructions" not in content
        assert "*cwd: `/work/project` · branch: `main`*" in content

    def test_omits_environment_context_and_titles_from_first_real_prompt(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        _write_jsonl(
            path,
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "<environment_context>\n"
                                    "  <cwd>/work/project</cwd>\n"
                                    "  <shell>bash</shell>\n"
                                    "</environment_context>"
                                ),
                            }
                        ],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "fix the real problem"}],
                    },
                },
            ],
        )

        title, content = sessions.render_codex_session(path)

        assert title == "fix the real problem"
        assert "environment_context" not in content
        assert "<cwd>" not in content

    def test_strips_injected_agents_instructions_from_mixed_user_turn(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        _write_jsonl(
            path,
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "# AGENTS.md instructions for /work/project\n\n"
                                    "<INSTRUCTIONS>\n"
                                    "Always use the project test runner.\n"
                                    "</INSTRUCTIONS>\n"
                                    "<environment_context>\n"
                                    "  <cwd>/work/project</cwd>\n"
                                    "</environment_context>\n"
                                    "fix the real problem"
                                ),
                            }
                        ],
                    },
                }
            ],
        )

        title, content = sessions.render_codex_session(path)

        assert title == "fix the real problem"
        assert "AGENTS.md instructions" not in content
        assert "Always use the project test runner" not in content
        assert "environment_context" not in content


def _tool_use_entry(role, tool_use_id, name, tool_input):
    return {
        "type": role,
        "message": {
            "role": role,
            "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}],
        },
    }


def _tool_result_entry(tool_use_id, result_text):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": result_text}
            ],
        },
    }


class TestRenderTodos:
    def test_renders_pending_in_progress_and_completed(self, tmp_path):
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [
                _tool_use_entry(
                    "assistant",
                    "t1",
                    "TaskCreate",
                    {"subject": "Fix bug", "activeForm": "Fixing bug"},
                ),
                _tool_result_entry("t1", "Task #1 created successfully: Fix bug"),
                _tool_use_entry(
                    "assistant",
                    "t2",
                    "TaskCreate",
                    {"subject": "Write tests", "activeForm": "Writing tests"},
                ),
                _tool_result_entry("t2", "Task #2 created successfully: Write tests"),
                _tool_use_entry(
                    "assistant", "t3", "TaskUpdate", {"taskId": "1", "status": "completed"}
                ),
                _tool_result_entry("t3", "Updated task #1 status"),
                _tool_use_entry(
                    "assistant",
                    "t4",
                    "TaskCreate",
                    {"subject": "Ship it", "activeForm": "Shipping it"},
                ),
                _tool_result_entry("t4", "Task #3 created successfully: Ship it"),
                _tool_use_entry(
                    "assistant", "t5", "TaskUpdate", {"taskId": "3", "status": "in_progress"}
                ),
                _tool_result_entry("t5", "Updated task #3 status"),
            ],
        )
        _, content = sessions.render_session(path)
        assert "### \U0001f4cb Tasks" in content
        assert "- [x] Fix bug" in content
        assert "- [ ] Write tests" in content
        assert "- [ ] **Shipping it** _(in progress)_" in content

    def test_omits_deleted_tasks(self, tmp_path):
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [
                _tool_use_entry(
                    "assistant", "t1", "TaskCreate", {"subject": "Scrap this", "activeForm": "x"}
                ),
                _tool_result_entry("t1", "Task #1 created successfully: Scrap this"),
                _tool_use_entry(
                    "assistant", "t2", "TaskUpdate", {"taskId": "1", "status": "deleted"}
                ),
                _tool_result_entry("t2", "Updated task #1 status"),
            ],
        )
        _, content = sessions.render_session(path)
        assert "Tasks" not in content
        assert "Scrap this" not in content

    def test_no_todo_section_when_no_tasks(self, tmp_path):
        path = tmp_path / "session.jsonl"
        _write_jsonl(path, [_user_entry("hi"), _assistant_entry("hello")])
        _, content = sessions.render_session(path)
        assert "### \U0001f4cb Tasks" not in content

    def test_only_renders_current_snapshot_not_history(self, tmp_path):
        # A task flipping pending -> in_progress -> completed should appear
        # once, in its final state — not as a trail of every intermediate one.
        path = tmp_path / "session.jsonl"
        _write_jsonl(
            path,
            [
                _tool_use_entry(
                    "assistant",
                    "t1",
                    "TaskCreate",
                    {"subject": "Refactor", "activeForm": "Refactoring"},
                ),
                _tool_result_entry("t1", "Task #1 created successfully: Refactor"),
                _tool_use_entry(
                    "assistant", "t2", "TaskUpdate", {"taskId": "1", "status": "in_progress"}
                ),
                _tool_result_entry("t2", "Updated task #1 status"),
                _tool_use_entry(
                    "assistant", "t3", "TaskUpdate", {"taskId": "1", "status": "completed"}
                ),
                _tool_result_entry("t3", "Updated task #1 status"),
            ],
        )
        _, content = sessions.render_session(path)
        assert content.count("Refactor") == 1
        assert "- [x] Refactor" in content


class TestContentHash:
    def test_deterministic(self):
        assert sessions._content_hash("abc") == sessions._content_hash("abc")

    def test_differs_for_different_content(self):
        assert sessions._content_hash("abc") != sessions._content_hash("abd")


class TestPushSessions:
    def test_pushes_new_session(self, tmp_path, monkeypatch, fake_config):
        monkeypatch.setattr(sessions, "CLAUDE_PROJECTS_DIR", tmp_path)
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        _write_jsonl(project_dir / "s1.jsonl", [_user_entry("hello")])

        mock_push_doc = MagicMock()
        monkeypatch.setattr(sessions, "push_doc", mock_push_doc)

        pushed = sessions.push_sessions(fake_config)

        assert pushed == 1
        mock_push_doc.assert_called_once()
        _, kwargs = mock_push_doc.call_args
        assert kwargs["doc_id"] == "session-s1"

    def test_skips_unchanged_session(self, tmp_path, monkeypatch, fake_config):
        monkeypatch.setattr(sessions, "CLAUDE_PROJECTS_DIR", tmp_path)
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        _write_jsonl(project_dir / "s1.jsonl", [_user_entry("hello")])

        mock_push_doc = MagicMock()
        monkeypatch.setattr(sessions, "push_doc", mock_push_doc)

        assert sessions.push_sessions(fake_config) == 1
        assert sessions.push_sessions(fake_config) == 0
        mock_push_doc.assert_called_once()

    def test_pushes_again_after_content_changes(self, tmp_path, monkeypatch, fake_config):
        monkeypatch.setattr(sessions, "CLAUDE_PROJECTS_DIR", tmp_path)
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        session_path = project_dir / "s1.jsonl"
        _write_jsonl(session_path, [_user_entry("hello")])

        mock_push_doc = MagicMock()
        monkeypatch.setattr(sessions, "push_doc", mock_push_doc)

        assert sessions.push_sessions(fake_config) == 1
        _write_jsonl(session_path, [_user_entry("hello"), _assistant_entry("hi back")])
        assert sessions.push_sessions(fake_config) == 1
        assert mock_push_doc.call_count == 2

    def test_evicts_stale_session_ids(self, tmp_path, monkeypatch, fake_config):
        monkeypatch.setattr(sessions, "CLAUDE_PROJECTS_DIR", tmp_path)
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        session_path = project_dir / "s1.jsonl"
        _write_jsonl(session_path, [_user_entry("hello")])
        monkeypatch.setattr(sessions, "push_doc", MagicMock())

        sessions.push_sessions(fake_config)
        assert "s1" in sessions._last_hashes

        session_path.unlink()
        sessions.push_sessions(fake_config)
        assert "s1" not in sessions._last_hashes
