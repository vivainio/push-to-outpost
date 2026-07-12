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
