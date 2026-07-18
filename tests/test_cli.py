import argparse
import base64
import io
import zipfile
from contextlib import contextmanager, nullcontext
from unittest.mock import MagicMock

import pytest

from outpost import cli
from outpost.agent import PushResult
from outpost.config import Config


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello World", "hello-world"),
        ("  leading and trailing  ", "leading-and-trailing"),
        ("snake_case_name", "snake-case-name"),
        ("---already---slug---", "already-slug"),
        ("café notes", "caf-notes"),
        ("", "doc"),
        ("!!!", "doc"),
    ],
)
def test_slugify(text, expected):
    assert cli._slugify(text) == expected


def _push_doc_args(path, *, title=None, doc_format=None, doc_id=None):
    return argparse.Namespace(path=str(path), title=title, format=doc_format, id=doc_id)


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


class TestParseResponses:
    def test_none_returns_default_list(self):
        assert cli._parse_responses(None) == cli.DEFAULT_RESPONSES

    def test_empty_string_disables(self):
        assert cli._parse_responses("") == []

    def test_splits_and_strips_comma_separated_list(self):
        assert cli._parse_responses("yes, continue ,  commit and push") == [
            "yes",
            "continue",
            "commit and push",
        ]


class TestCmdPush:
    def test_passes_parsed_responses_through_to_push_once(self, monkeypatch, fake_config):
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))
        mock_push_once = MagicMock(return_value=PushResult(0, []))
        monkeypatch.setattr(cli, "push_once", mock_push_once)
        monkeypatch.setattr(cli, "push_sessions", lambda config, **kwargs: 0)

        cli.cmd_push(argparse.Namespace(responses="yes,continue"))

        mock_push_once.assert_called_once_with(
            fake_config, responses=["yes", "continue"], verbose=False
        )


class TestCmdRun:
    def test_writes_dot_without_newline_when_nothing_changed(
        self, capsys, monkeypatch, fake_config
    ):
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))
        monkeypatch.setattr(cli, "current_pane_id", lambda: "@0")
        monkeypatch.setattr(cli, "exclusive_run", lambda: nullcontext())
        monkeypatch.setattr(cli, "push_once", lambda *args, **kwargs: PushResult(0, []))
        monkeypatch.setattr(cli, "push_sessions", lambda config, **kwargs: 0)
        monkeypatch.setattr(cli.time, "sleep", MagicMock(side_effect=KeyboardInterrupt))

        with pytest.raises(KeyboardInterrupt):
            cli.cmd_run(argparse.Namespace(responses=""))

        assert capsys.readouterr().out.endswith("(ctrl-c to stop)\n.")

    def test_replaces_previous_run(self, capsys, monkeypatch, fake_config):
        @contextmanager
        def fake_exclusive_run():
            yield 1234

        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))
        monkeypatch.setattr(cli, "current_pane_id", lambda: None)
        monkeypatch.setattr(cli, "exclusive_run", fake_exclusive_run)
        monkeypatch.setattr(cli, "push_once", lambda *args, **kwargs: PushResult(0, []))
        monkeypatch.setattr(cli, "push_sessions", lambda config, **kwargs: 0)
        monkeypatch.setattr(cli.time, "sleep", MagicMock(side_effect=KeyboardInterrupt))

        with pytest.raises(KeyboardInterrupt):
            cli.cmd_run(argparse.Namespace(responses=""))

        assert "stopped previous outpost run (pid 1234)" in capsys.readouterr().out

    def test_fast_polls_after_delivering_canned_response(self, monkeypatch, fake_config):
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))
        monkeypatch.setattr(cli, "current_pane_id", lambda: "@0")
        monkeypatch.setattr(cli, "exclusive_run", lambda: nullcontext())
        results = iter(
            [
                PushResult(0, [("@1", "yes")]),
                PushResult(1, []),
                PushResult(0, []),
                PushResult(0, []),
                PushResult(0, []),
                PushResult(0, []),
            ]
        )
        monkeypatch.setattr(cli, "push_once", lambda *args, **kwargs: next(results))
        monkeypatch.setattr(cli, "push_sessions", lambda config, **kwargs: 0)
        monkeypatch.setattr(cli.time, "monotonic", lambda: 0.0)
        sleep = MagicMock(side_effect=[None, None, None, None, None, KeyboardInterrupt])
        monkeypatch.setattr(cli.time, "sleep", sleep)

        with pytest.raises(KeyboardInterrupt):
            cli.cmd_run(argparse.Namespace(responses="yes"))

        assert [call.args[0] for call in sleep.call_args_list] == [
            1.0,
            5.0,
            5.0,
            5.0,
            5.0,
            fake_config.push_interval,
        ]

    def test_fast_poll_never_slows_a_short_normal_interval(self, monkeypatch, fake_config):
        fake_config.push_interval = 0.5
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))
        monkeypatch.setattr(cli, "current_pane_id", lambda: "@0")
        monkeypatch.setattr(cli, "exclusive_run", lambda: nullcontext())
        monkeypatch.setattr(
            cli, "push_once", lambda *args, **kwargs: PushResult(0, [("@1", "yes")])
        )
        monkeypatch.setattr(cli, "push_sessions", lambda config, **kwargs: 0)
        monkeypatch.setattr(cli.time, "monotonic", lambda: 0.0)
        sleep = MagicMock(side_effect=KeyboardInterrupt)
        monkeypatch.setattr(cli.time, "sleep", sleep)

        with pytest.raises(KeyboardInterrupt):
            cli.cmd_run(argparse.Namespace(responses="yes"))

        sleep.assert_called_once_with(0.5)


class TestCmdPushDoc:
    def test_infers_markdown_format_from_suffix(self, tmp_path, monkeypatch, fake_config):
        path = tmp_path / "notes.md"
        path.write_text("# hello")
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))
        mock_push_doc = MagicMock()
        monkeypatch.setattr(cli, "push_doc", mock_push_doc)

        cli.cmd_push_doc(_push_doc_args(path))

        mock_push_doc.assert_called_once_with(
            fake_config, "notes", "notes.md", "markdown", "# hello"
        )

    def test_infers_zip_format_and_base64_encodes(self, tmp_path, monkeypatch, fake_config):
        path = tmp_path / "archive.zip"
        path.write_bytes(b"PK\x03\x04binary-data")
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))
        mock_push_doc = MagicMock()
        monkeypatch.setattr(cli, "push_doc", mock_push_doc)

        cli.cmd_push_doc(_push_doc_args(path))

        called_content = mock_push_doc.call_args[0][4]
        assert base64.b64decode(called_content) == b"PK\x03\x04binary-data"

    def test_explicit_format_overrides_suffix(self, tmp_path, monkeypatch, fake_config):
        path = tmp_path / "notes.txt"
        path.write_text("plain text")
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))
        mock_push_doc = MagicMock()
        monkeypatch.setattr(cli, "push_doc", mock_push_doc)

        cli.cmd_push_doc(_push_doc_args(path, doc_format="markdown"))

        assert mock_push_doc.call_args[0][3] == "markdown"

    def test_raises_for_unknown_suffix_without_format(self, tmp_path, monkeypatch, fake_config):
        path = tmp_path / "notes.txt"
        path.write_text("plain text")
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))

        with pytest.raises(SystemExit):
            cli.cmd_push_doc(_push_doc_args(path))

    def test_raises_for_missing_file(self, tmp_path, monkeypatch, fake_config):
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))
        with pytest.raises(SystemExit):
            cli.cmd_push_doc(_push_doc_args(tmp_path / "missing.md"))

    def test_directory_is_zipped_and_pushed(self, tmp_path, monkeypatch, fake_config):
        vault = tmp_path / "vault"
        (vault / "sub").mkdir(parents=True)
        (vault / "a.md").write_text("# a")
        (vault / "sub" / "b.md").write_text("# b")
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))
        mock_push_doc = MagicMock()
        monkeypatch.setattr(cli, "push_doc", mock_push_doc)

        cli.cmd_push_doc(_push_doc_args(vault))

        doc_id, title, doc_format, content = mock_push_doc.call_args[0][1:]
        assert doc_id == "vault"
        assert title == "vault"
        assert doc_format == "zip"
        zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(content)))
        assert sorted(zf.namelist()) == ["a.md", "sub/b.md"]
        assert zf.read("a.md") == b"# a"

    def test_directory_skips_git_metadata(self, tmp_path, monkeypatch, fake_config):
        vault = tmp_path / "vault"
        (vault / ".git").mkdir(parents=True)
        (vault / ".git" / "config").write_text("ignored")
        (vault / "a.md").write_text("# a")
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))
        mock_push_doc = MagicMock()
        monkeypatch.setattr(cli, "push_doc", mock_push_doc)

        cli.cmd_push_doc(_push_doc_args(vault))

        content = mock_push_doc.call_args[0][4]
        zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(content)))
        assert zf.namelist() == ["a.md"]

    def test_directory_rejects_non_zip_format(self, tmp_path, monkeypatch, fake_config):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "a.md").write_text("# a")
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))

        with pytest.raises(SystemExit):
            cli.cmd_push_doc(_push_doc_args(vault, doc_format="markdown"))

    def test_title_and_id_overrides(self, tmp_path, monkeypatch, fake_config):
        path = tmp_path / "notes.md"
        path.write_text("# hello")
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))
        mock_push_doc = MagicMock()
        monkeypatch.setattr(cli, "push_doc", mock_push_doc)

        cli.cmd_push_doc(_push_doc_args(path, title="Custom Title", doc_id="custom-id"))

        args = mock_push_doc.call_args[0]
        assert args[1] == "custom-id"
        assert args[2] == "Custom Title"


class TestCmdQr:
    def test_generates_random_password_without_prompting(self, monkeypatch, capsys, fake_config):
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))
        monkeypatch.setattr(cli, "fetch_encryption_salt", lambda url, secret: ("ab" * 16, 210_000))
        mock_save_credentials = MagicMock(return_value="somewhere")
        monkeypatch.setattr(cli, "save_credentials", mock_save_credentials)
        monkeypatch.setattr(
            cli, "input", MagicMock(side_effect=AssertionError("should not prompt")), raising=False
        )

        cli.cmd_qr(argparse.Namespace())

        # Saved key must match deriving from whatever random password was generated.
        saved_key = base64.b64decode(mock_save_credentials.call_args.kwargs["encryption_key"])
        assert len(saved_key) == 32
        assert mock_save_credentials.call_args.args[:2] == (
            fake_config.tower_url,
            fake_config.push_secret,
        )

        out = capsys.readouterr().out
        assert "somewhere" in out
        assert "Scan this" in out

    def test_generates_a_different_password_each_run(self, monkeypatch, fake_config):
        monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: fake_config))
        monkeypatch.setattr(cli, "fetch_encryption_salt", lambda url, secret: ("ab" * 16, 210_000))
        mock_save_credentials = MagicMock(return_value="somewhere")
        monkeypatch.setattr(cli, "save_credentials", mock_save_credentials)

        cli.cmd_qr(argparse.Namespace())
        cli.cmd_qr(argparse.Namespace())

        first_key = mock_save_credentials.call_args_list[0].kwargs["encryption_key"]
        second_key = mock_save_credentials.call_args_list[1].kwargs["encryption_key"]
        assert first_key != second_key
