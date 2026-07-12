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
        assert (
            sessions._render_message("user", "hello there") == "### \U0001f9d1 User\n\nhello there"
        )

    def test_assistant_string_content_uses_different_heading(self):
        rendered = sessions._render_message("assistant", "hi!")
        assert rendered.startswith("### \U0001f916 Claude")

    def test_blank_string_content_returns_none(self):
        assert sessions._render_message("user", "   ") is None

    def test_none_content_returns_none(self):
        assert sessions._render_message("user", None) is None

    def test_list_of_text_blocks(self):
        content = [{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}]
        rendered = sessions._render_message("user", content)
        assert rendered == "### \U0001f9d1 User\n\npart one\n\npart two"

    def test_list_drops_non_text_blocks(self):
        content = [
            {"type": "tool_use", "input": {"huge": "dump"}},
            {"type": "text", "text": "the actual reply"},
        ]
        rendered = sessions._render_message("assistant", content)
        assert rendered == "### \U0001f916 Claude\n\nthe actual reply"

    def test_list_with_only_non_text_blocks_returns_none(self):
        content = [{"type": "tool_use", "input": {}}]
        assert sessions._render_message("assistant", content) is None

    def test_truncates_individual_blocks(self):
        long_text = "y" * (sessions._BLOCK_LIMIT + 50)
        rendered = sessions._render_message("user", long_text)
        assert "truncated" in rendered


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
        # the rendered "### 🧑 User\n\n..." heading it's wrapped in for the body.
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
