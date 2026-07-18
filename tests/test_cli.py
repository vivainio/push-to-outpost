import argparse
import base64
from unittest.mock import MagicMock

import pytest

from outpost import cli
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
        mock_push_once = MagicMock(return_value=0)
        monkeypatch.setattr(cli, "push_once", mock_push_once)
        monkeypatch.setattr(cli, "push_sessions", lambda config: 0)

        cli.cmd_push(argparse.Namespace(responses="yes,continue"))

        mock_push_once.assert_called_once_with(fake_config, responses=["yes", "continue"])


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
