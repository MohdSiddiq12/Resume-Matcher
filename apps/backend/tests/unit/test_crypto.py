"""Tests for the Fernet API-key encryption helper."""

import pytest
from cryptography.fernet import Fernet

from app import crypto
from app.config import settings


@pytest.fixture
def isolated_secret(tmp_path, monkeypatch):
    """Point the secret at a temp dir so tests don't touch the real one."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    crypto.reset_cache()
    yield tmp_path
    crypto.reset_cache()


class TestCrypto:
    def test_uses_configured_environment_key_without_writing_file(
        self, isolated_secret, monkeypatch
    ):
        key = Fernet.generate_key().decode("ascii")
        monkeypatch.setattr(settings, "encryption_key", key)

        ciphertext = crypto.encrypt("sk-stable")

        assert crypto.decrypt(ciphertext) == "sk-stable"
        assert not (isolated_secret / ".secret_key").exists()

    def test_invalid_configured_key_fails_without_replacement(
        self, isolated_secret, monkeypatch
    ):
        monkeypatch.setattr(settings, "encryption_key", "invalid-key")

        with pytest.raises(ValueError, match="ENCRYPTION_KEY"):
            crypto.encrypt("sk-secret")

        assert not (isolated_secret / ".secret_key").exists()

    def test_configured_key_change_reloads_key(self, isolated_secret, monkeypatch):
        first_key = Fernet.generate_key().decode("ascii")
        second_key = Fernet.generate_key().decode("ascii")
        monkeypatch.setattr(settings, "encryption_key", first_key)
        ciphertext = crypto.encrypt("sk-secret")

        monkeypatch.setattr(settings, "encryption_key", second_key)
        assert crypto.decrypt(ciphertext) == ""

    def test_absent_configured_key_uses_local_file_fallback(
        self, isolated_secret, monkeypatch
    ):
        monkeypatch.setattr(settings, "encryption_key", None)
        ciphertext = crypto.encrypt("sk-local")

        assert crypto.decrypt(ciphertext) == "sk-local"
        assert (isolated_secret / ".secret_key").exists()

    def test_round_trip(self, isolated_secret):
        ciphertext = crypto.encrypt("sk-super-secret")
        assert ciphertext  # non-empty
        assert ciphertext != "sk-super-secret"  # actually encrypted
        assert crypto.decrypt(ciphertext) == "sk-super-secret"

    def test_secret_file_created_with_600_perms(self, isolated_secret):
        crypto.encrypt("x")
        secret = isolated_secret / ".secret_key"
        assert secret.exists()
        # Owner read/write only (mode bits 0o600). Skip the check on platforms
        # that don't support chmod semantics.
        mode = secret.stat().st_mode & 0o777
        assert mode == 0o600

    def test_empty_inputs(self, isolated_secret):
        assert crypto.encrypt("") == ""
        assert crypto.decrypt("") == ""

    def test_undecryptable_returns_empty(self, isolated_secret):
        # Garbage / wrong-secret ciphertext must not raise — treated as empty.
        assert crypto.decrypt("not-a-valid-token") == ""

    def test_rotated_secret_yields_empty(self, isolated_secret, monkeypatch, tmp_path):
        ciphertext = crypto.encrypt("sk-secret")
        # Simulate a lost/rotated secret: new empty data dir.
        new_dir = tmp_path / "rotated"
        new_dir.mkdir()
        monkeypatch.setattr(settings, "data_dir", new_dir)
        crypto.reset_cache()
        assert crypto.decrypt(ciphertext) == ""
