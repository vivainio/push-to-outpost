import json
import subprocess

import pytest

from outpost import config


class FakeWincred:
    """Stands in for wincred.exe by keeping an in-memory store, so tests don't
    need Windows Credential Manager (or WSL2) to run."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def run(self, args, **kwargs):
        assert args[0] == "wincred.exe"
        if args[1] == "get":
            target = args[2]
            if target in self.store:
                return subprocess.CompletedProcess(args, 0, stdout=self.store[target], stderr="")
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")
        if args[1] == "set":
            target = args[2]
            self.store[target] = kwargs["input"]
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected wincred args: {args}")


@pytest.fixture
def fake_wincred(monkeypatch):
    fake = FakeWincred()
    monkeypatch.setattr(config.shutil, "which", lambda name: "/usr/bin/wincred.exe")
    monkeypatch.setattr(config.subprocess, "run", fake.run)
    return fake


def test_wincred_available_true(fake_wincred):
    assert config._wincred_available() is True


def test_wincred_available_false(monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    assert config._wincred_available() is False


def test_save_and_load_credentials_round_trip(fake_wincred):
    location = config.save_credentials("https://example.com", "secret123")
    assert "outpost:config" in location

    loaded = config._load_credentials()
    assert loaded == {"tower_url": "https://example.com", "push_secret": "secret123"}


def test_save_credentials_with_encryption_key(fake_wincred):
    config.save_credentials("https://example.com", "secret123", encryption_key="abc==")
    loaded = config._load_credentials()
    assert loaded["encryption_key"] == "abc=="


def test_save_credentials_preserves_existing_encryption_key_on_relogin(fake_wincred):
    config.save_credentials("https://example.com", "secret123", encryption_key="abc==")
    # Re-login without passing an encryption_key shouldn't clear the saved one.
    config.save_credentials("https://example.com", "new-secret")
    loaded = config._load_credentials()
    assert loaded["push_secret"] == "new-secret"
    assert loaded["encryption_key"] == "abc=="


def test_load_credentials_returns_none_when_nothing_stored(fake_wincred):
    assert config._load_credentials() is None


def test_load_credentials_returns_none_when_wincred_unavailable(monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    assert config._load_credentials() is None


def test_load_credentials_returns_none_on_invalid_json(fake_wincred):
    fake_wincred.store[config.WINCRED_TARGET] = "not json"
    assert config._load_credentials() is None


def test_config_from_env_raises_without_wincred(monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit):
        config.Config.from_env()


def test_config_from_env_raises_without_credentials(fake_wincred):
    with pytest.raises(SystemExit):
        config.Config.from_env()


def test_config_from_env_uses_defaults(fake_wincred, monkeypatch):
    monkeypatch.delenv("PUSH_INTERVAL", raising=False)
    monkeypatch.delenv("CAPTURE_LINES", raising=False)
    monkeypatch.delenv("SESSION_MAX_AGE_MINUTES", raising=False)
    fake_wincred.store[config.WINCRED_TARGET] = json.dumps(
        {"tower_url": "https://example.com/", "push_secret": "secret123"}
    )

    cfg = config.Config.from_env()

    assert cfg.tower_url == "https://example.com"  # trailing slash stripped
    assert cfg.push_secret == "secret123"
    assert cfg.push_interval == 15.0
    assert cfg.capture_lines == 500
    assert cfg.session_max_age == 3600.0
    assert cfg.encryption_key is None


def test_config_from_env_respects_overrides(fake_wincred, monkeypatch):
    monkeypatch.setenv("PUSH_INTERVAL", "5")
    monkeypatch.setenv("CAPTURE_LINES", "800")
    monkeypatch.setenv("SESSION_MAX_AGE_MINUTES", "2")
    fake_wincred.store[config.WINCRED_TARGET] = json.dumps(
        {
            "tower_url": "https://example.com",
            "push_secret": "secret123",
            "encryption_key": "abc==",
        }
    )

    cfg = config.Config.from_env()

    assert cfg.push_interval == 5.0
    assert cfg.capture_lines == 800
    assert cfg.session_max_age == 120.0
    assert cfg.encryption_key == "abc=="
