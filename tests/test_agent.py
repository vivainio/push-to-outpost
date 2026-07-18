import base64
import json
import subprocess
import urllib.error
from unittest.mock import MagicMock

import pytest

from outpost import agent
from outpost.config import Config


@pytest.fixture
def fake_config():
    return Config(
        tower_url="https://example.com",
        push_secret="secret",
        push_interval=15.0,
        capture_lines=2000,
        session_max_age=3600.0,
        encryption_key=base64.b64encode(b"0" * 32).decode(),
    )


def _tmux_windows_output(*rows):
    # Matches the tab-separated -F format list_windows() passes to tmux.
    return "\n".join("\t".join(str(v) for v in row) for row in rows)


class TestListWindows:
    def test_parses_tmux_output(self, monkeypatch):
        stdout = _tmux_windows_output(
            ("main", "@0", 0, "editor", "1", "1"),
            ("main", "@1", 1, "shell", "0", "1"),
        )
        monkeypatch.setattr(
            agent.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=stdout, stderr=""),
        )

        windows = agent.list_windows()

        assert windows == [
            {
                "session_name": "main",
                "window_id": "@0",
                "window_index": 0,
                "window_name": "editor",
                "window_active": True,
                "session_attached": True,
            },
            {
                "session_name": "main",
                "window_id": "@1",
                "window_index": 1,
                "window_name": "shell",
                "window_active": False,
                "session_attached": True,
            },
        ]

    def test_returns_empty_list_when_tmux_fails(self, monkeypatch):
        monkeypatch.setattr(
            agent.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="no server"),
        )
        assert agent.list_windows() == []

    def test_returns_empty_list_when_tmux_not_installed(self, monkeypatch):
        def raise_not_found(*a, **k):
            raise FileNotFoundError("tmux")

        monkeypatch.setattr(agent.subprocess, "run", raise_not_found)
        assert agent.list_windows() == []


class TestCapture:
    def test_returns_stdout_on_success(self, monkeypatch):
        monkeypatch.setattr(
            agent.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="pane content\n", stderr=""),
        )
        assert agent.capture("@0", 2000) == "pane content\n"

    def test_returns_empty_string_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            agent.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="no pane"),
        )
        assert agent.capture("@0", 2000) == ""

    def test_returns_empty_string_when_tmux_not_installed(self, monkeypatch):
        def raise_not_found(*a, **k):
            raise FileNotFoundError("tmux")

        monkeypatch.setattr(agent.subprocess, "run", raise_not_found)
        assert agent.capture("@0", 2000) == ""


class TestCurrentPaneId:
    def test_returns_none_when_not_in_tmux(self, monkeypatch):
        monkeypatch.delenv("TMUX_PANE", raising=False)
        assert agent.current_pane_id() is None

    def test_resolves_pane_id_via_tmux_display_message(self, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%3")
        monkeypatch.setattr(
            agent.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="@1\n", stderr=""),
        )
        assert agent.current_pane_id() == "@1"

    def test_returns_none_when_tmux_display_message_fails(self, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%3")
        monkeypatch.setattr(
            agent.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="pane not found"),
        )
        assert agent.current_pane_id() is None

    def test_returns_none_when_tmux_not_installed(self, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%3")

        def raise_not_found(*a, **k):
            raise FileNotFoundError("tmux")

        monkeypatch.setattr(agent.subprocess, "run", raise_not_found)
        assert agent.current_pane_id() is None


class TestSendKeys:
    def test_sends_literal_text_then_enter(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            agent.subprocess,
            "run",
            lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        )
        agent.send_keys("main:0", "commit and push")
        assert calls == [
            ["tmux", "send-keys", "-l", "-t", "main:0", "commit and push"],
            ["tmux", "send-keys", "-t", "main:0", "Enter"],
        ]

    def test_silently_ignores_missing_tmux(self, monkeypatch):
        def raise_not_found(*a, **k):
            raise FileNotFoundError("tmux")

        monkeypatch.setattr(agent.subprocess, "run", raise_not_found)
        agent.send_keys("main:0", "yes")  # must not raise

    def test_sends_tab_as_a_keypress_then_enter(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            agent.subprocess,
            "run",
            lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        )
        agent.send_keys("main:0", "Tab")
        assert calls == [
            ["tmux", "send-keys", "-t", "main:0", "Tab"],
            ["tmux", "send-keys", "-t", "main:0", "Enter"],
        ]


class TestRowHash:
    def test_deterministic(self):
        h1 = agent._row_hash("editor", True, True, "content")
        h2 = agent._row_hash("editor", True, True, "content")
        assert h1 == h2

    def test_differs_when_content_changes(self):
        h1 = agent._row_hash("editor", True, True, "content one")
        h2 = agent._row_hash("editor", True, True, "content two")
        assert h1 != h2


def _mock_urlopen(response_body: bytes = b"{}", status: int = 200):
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_body
    mock_resp.status = status
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_resp
    mock_cm.__exit__.return_value = False
    return MagicMock(return_value=mock_cm)


class TestPushOnce:
    def test_raises_without_encryption_key(self, fake_config):
        fake_config.encryption_key = None
        with pytest.raises(SystemExit):
            agent.push_once(fake_config)

    def test_returns_zero_when_nothing_changed_and_no_network_call(self, monkeypatch, fake_config):
        monkeypatch.setattr(agent, "list_windows", lambda: [])
        mock_urlopen = _mock_urlopen()
        monkeypatch.setattr(agent.urllib.request, "urlopen", mock_urlopen)

        assert agent.push_once(fake_config) == agent.PushResult(0, [])
        mock_urlopen.assert_not_called()

    def test_pushes_changed_pane(self, monkeypatch, fake_config):
        monkeypatch.setattr(
            agent,
            "list_windows",
            lambda: [
                {
                    "session_name": "main",
                    "window_id": "@0",
                    "window_index": 0,
                    "window_name": "editor",
                    "window_active": True,
                    "session_attached": True,
                }
            ],
        )
        monkeypatch.setattr(agent, "capture", lambda *a, **k: "some pane output")
        mock_urlopen = _mock_urlopen()
        monkeypatch.setattr(agent.urllib.request, "urlopen", mock_urlopen)

        result = agent.push_once(fake_config)

        assert result.changed == 1
        mock_urlopen.assert_called_once()
        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data)
        assert body["op"] == "push-tmux"
        assert body["live"] == ["@0"]
        assert len(body["changes"]) == 1
        assert body["changes"][0]["encrypted"] is True

    def test_excludes_pane_by_exact_id_not_content(self, monkeypatch, fake_config):
        # The excluded pane is identified by pane id, not by sniffing its
        # captured content — a pane whose scrollback happens to contain
        # push-related text (e.g. from viewing source, or an old command)
        # must NOT be excluded unless its id matches exactly.
        monkeypatch.setattr(
            agent,
            "list_windows",
            lambda: [
                {
                    "session_name": "main",
                    "window_id": "@0",
                    "window_index": 0,
                    "window_name": "outpost",
                    "window_active": True,
                    "session_attached": True,
                },
                {
                    "session_name": "main",
                    "window_id": "@1",
                    "window_index": 1,
                    "window_name": "editor",
                    "window_active": True,
                    "session_attached": True,
                },
            ],
        )
        monkeypatch.setattr(
            agent,
            "capture",
            lambda *a, **k: f"pushing to {fake_config.tower_url} every 15s (ctrl-c to stop)",
        )
        mock_urlopen = _mock_urlopen()
        monkeypatch.setattr(agent.urllib.request, "urlopen", mock_urlopen)

        result = agent.push_once(fake_config, exclude_pane_id="@0")

        assert result.changed == 1
        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data)
        assert body["live"] == ["@1"]

    def test_no_exclusion_when_exclude_pane_id_is_none(self, monkeypatch, fake_config):
        monkeypatch.setattr(
            agent,
            "list_windows",
            lambda: [
                {
                    "session_name": "main",
                    "window_id": "@0",
                    "window_index": 0,
                    "window_name": "outpost",
                    "window_active": True,
                    "session_attached": True,
                }
            ],
        )
        monkeypatch.setattr(
            agent,
            "capture",
            lambda *a, **k: f"pushing to {fake_config.tower_url} every 15s (ctrl-c to stop)",
        )
        mock_urlopen = _mock_urlopen()
        monkeypatch.setattr(agent.urllib.request, "urlopen", mock_urlopen)

        assert agent.push_once(fake_config).changed == 1
        mock_urlopen.assert_called_once()

    def test_skips_unchanged_pane_on_second_call(self, monkeypatch, fake_config):
        monkeypatch.setattr(
            agent,
            "list_windows",
            lambda: [
                {
                    "session_name": "main",
                    "window_id": "@0",
                    "window_index": 0,
                    "window_name": "editor",
                    "window_active": True,
                    "session_attached": True,
                }
            ],
        )
        monkeypatch.setattr(agent, "capture", lambda *a, **k: "steady output")
        mock_urlopen = _mock_urlopen()
        monkeypatch.setattr(agent.urllib.request, "urlopen", mock_urlopen)

        first = agent.push_once(fake_config)
        second_call_count_before = mock_urlopen.call_count
        second = agent.push_once(fake_config)

        assert first.changed == 1
        # Unchanged content means no changes to push, but since `live` set is
        # identical the whole request is skipped (no new network call).
        assert second.changed == 0
        assert mock_urlopen.call_count == second_call_count_before

    def test_includes_responses_in_payload(self, monkeypatch, fake_config):
        monkeypatch.setattr(agent, "list_windows", lambda: [])
        mock_urlopen = _mock_urlopen()
        monkeypatch.setattr(agent.urllib.request, "urlopen", mock_urlopen)

        agent.push_once(fake_config, responses=["yes", "continue"])

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data)
        assert body["responses"] == ["yes", "continue"]

    def test_does_not_skip_network_call_when_responses_enabled(self, monkeypatch, fake_config):
        # Otherwise a quiet pane (sitting at a prompt, unchanged) would never
        # be polled for a queued command.
        monkeypatch.setattr(agent, "list_windows", lambda: [])
        mock_urlopen = _mock_urlopen()
        monkeypatch.setattr(agent.urllib.request, "urlopen", mock_urlopen)

        agent.push_once(fake_config, responses=["yes"])

        mock_urlopen.assert_called_once()

    def test_sends_keys_for_allowed_queued_command(self, monkeypatch, fake_config):
        monkeypatch.setattr(
            agent,
            "list_windows",
            lambda: [
                {
                    "session_name": "main",
                    "window_id": "@0",
                    "window_index": 0,
                    "window_name": "editor",
                    "window_active": True,
                    "session_attached": True,
                }
            ],
        )
        monkeypatch.setattr(agent, "capture", lambda *a, **k: "some output")
        response = json.dumps({"commands": {"@0": "yes"}}).encode()
        monkeypatch.setattr(agent.urllib.request, "urlopen", _mock_urlopen(response_body=response))
        sent = []
        monkeypatch.setattr(agent, "send_keys", lambda pane_id, text: sent.append((pane_id, text)))

        result = agent.push_once(fake_config, responses=["yes", "continue"])

        assert sent == [("@0", "yes")]
        # The caller (cli.py) needs this to report what actually got applied,
        # rather than only knowing pane content changed.
        assert result.applied == [("@0", "yes")]

    def test_ignores_queued_command_not_in_allowlist(self, monkeypatch, fake_config):
        # Local re-check: even if the server (or a compromised UI/request)
        # returns something outside the CLI's own configured set, it must
        # never be typed into a pane.
        monkeypatch.setattr(
            agent,
            "list_windows",
            lambda: [
                {
                    "session_name": "main",
                    "window_id": "@0",
                    "window_index": 0,
                    "window_name": "editor",
                    "window_active": True,
                    "session_attached": True,
                }
            ],
        )
        monkeypatch.setattr(agent, "capture", lambda *a, **k: "some output")
        response = json.dumps({"commands": {"@0": "rm -rf /"}}).encode()
        monkeypatch.setattr(agent.urllib.request, "urlopen", _mock_urlopen(response_body=response))
        sent = []
        monkeypatch.setattr(agent, "send_keys", lambda pane_id, text: sent.append((pane_id, text)))

        result = agent.push_once(fake_config, responses=["yes", "continue"])

        assert sent == []
        assert result.applied == []

    def test_ignores_queued_command_for_pane_no_longer_live(self, monkeypatch, fake_config):
        monkeypatch.setattr(agent, "list_windows", lambda: [])
        response = json.dumps({"commands": {"@0": "yes"}}).encode()
        monkeypatch.setattr(agent.urllib.request, "urlopen", _mock_urlopen(response_body=response))
        sent = []
        monkeypatch.setattr(agent, "send_keys", lambda pane_id, text: sent.append((pane_id, text)))

        agent.push_once(fake_config, responses=["yes"])

        assert sent == []

    def test_ignores_commands_when_responses_not_enabled(self, monkeypatch, fake_config):
        monkeypatch.setattr(
            agent,
            "list_windows",
            lambda: [
                {
                    "session_name": "main",
                    "window_id": "@0",
                    "window_index": 0,
                    "window_name": "editor",
                    "window_active": True,
                    "session_attached": True,
                }
            ],
        )
        monkeypatch.setattr(agent, "capture", lambda *a, **k: "some output")
        response = json.dumps({"commands": {"@0": "yes"}}).encode()
        monkeypatch.setattr(agent.urllib.request, "urlopen", _mock_urlopen(response_body=response))
        sent = []
        monkeypatch.setattr(agent, "send_keys", lambda pane_id, text: sent.append((pane_id, text)))

        agent.push_once(fake_config)

        assert sent == []


class TestVerifyKey:
    def test_true_on_200(self, monkeypatch):
        monkeypatch.setattr(agent.urllib.request, "urlopen", _mock_urlopen(status=200))
        assert agent.verify_key("https://example.com", "secret") is True

    def test_false_on_http_error(self, monkeypatch):
        def raise_http_error(*a, **k):
            raise urllib.error.HTTPError(
                "https://example.com/api/push", 401, "Unauthorized", {}, None
            )

        monkeypatch.setattr(agent.urllib.request, "urlopen", raise_http_error)
        assert agent.verify_key("https://example.com", "bad-secret") is False


class TestPushDoc:
    def test_raises_without_encryption_key(self, fake_config):
        fake_config.encryption_key = None
        with pytest.raises(SystemExit):
            agent.push_doc(fake_config, "doc-1", "Title", "markdown", "content")

    def test_sends_encrypted_body(self, monkeypatch, fake_config):
        mock_urlopen = _mock_urlopen()
        monkeypatch.setattr(agent.urllib.request, "urlopen", mock_urlopen)

        agent.push_doc(fake_config, "doc-1", "Title", "markdown", "# hello")

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data)
        assert body["op"] == "push-doc"
        assert body["doc_id"] == "doc-1"
        assert body["title"] == "Title"
        assert body["format"] == "markdown"
        assert body["encrypted"] is True
        assert ":" in body["content"]


class TestFetchEncryptionSalt:
    def test_parses_salt_and_iterations(self, monkeypatch):
        response = json.dumps({"salt": "deadbeef", "kdf_iterations": 210_000}).encode()
        monkeypatch.setattr(agent.urllib.request, "urlopen", _mock_urlopen(response_body=response))

        salt, iterations = agent.fetch_encryption_salt("https://example.com", "secret")

        assert salt == "deadbeef"
        assert iterations == 210_000
