"""Credential storage. Nothing here may ever reach settings.json or a log.

``tests/conftest.py`` pins ``MOXASERIAL_SECRET_BACKEND=file`` for every
test, so the real macOS keychain and the Windows Credential Manager are
never touched; the platform backends are exercised through fakes.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moxaserial import secrets as secrets_mod  # noqa: E402
from moxaserial.secrets import (  # noqa: E402
    FileBackend,
    KeychainBackend,
    SecretError,
    SecretStore,
    service_name,
)


@pytest.fixture
def store(tmp_path):
    return SecretStore(FileBackend(tmp_path / "credentials.json"))


class TestRoundTrip:
    def test_set_then_get(self, store):
        store.set("m1", "admin", "hunter2")
        assert store.get("m1") == {"username": "admin", "password": "hunter2"}

    def test_an_empty_password_is_still_a_credential(self, store):
        store.set("m1", "admin", "")
        assert store.has("m1")
        assert store.get("m1")["password"] == ""

    def test_missing_machine_reads_as_none(self, store):
        assert store.get("nope") is None
        assert store.has("nope") is False
        assert store.username("nope") == ""

    def test_machines_do_not_share_credentials(self, store):
        store.set("m1", "admin", "one")
        store.set("m2", "operator", "two")
        assert store.get("m1")["password"] == "one"
        assert store.get("m2") == {"username": "operator", "password": "two"}

    def test_set_overwrites(self, store):
        store.set("m1", "admin", "old")
        store.set("m1", "admin", "new")
        assert store.get("m1")["password"] == "new"

    def test_clear(self, store):
        store.set("m1", "admin", "x")
        assert store.clear("m1") is True
        assert store.get("m1") is None
        assert store.clear("m1") is False

    def test_an_empty_machine_id_is_refused(self, store):
        with pytest.raises(SecretError):
            store.set("", "admin", "x")
        assert store.get("") is None
        assert store.clear("") is False

    def test_awkward_characters_survive(self, store):
        password = 'pä$$: "w\\ord" \n\t☃'
        store.set("m1", "admin user", password)
        assert store.get("m1") == {"username": "admin user", "password": password}


class TestFileBackend:
    def test_file_is_created_0600(self, store, tmp_path):
        store.set("m1", "admin", "x")
        path = tmp_path / "credentials.json"
        assert path.exists()
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"expected 0600, got {mode:o}"

    def test_file_says_out_loud_that_it_is_weaker(self, store, tmp_path):
        store.set("m1", "admin", "x")
        data = json.loads((tmp_path / "credentials.json").read_text())
        assert "NOT ENCRYPTED" in data["_warning"]

    def test_no_temp_file_is_left_behind(self, store, tmp_path):
        store.set("m1", "admin", "x")
        assert list(tmp_path.glob("*.tmp")) == []

    def test_a_corrupt_file_is_ignored_rather_than_fatal(self, tmp_path):
        path = tmp_path / "credentials.json"
        path.write_text("{not json", encoding="utf-8")
        store = SecretStore(FileBackend(path))
        assert store.get("m1") is None
        store.set("m1", "admin", "x")
        assert store.get("m1")["password"] == "x"

    def test_a_hand_written_bare_password_still_reads(self, tmp_path):
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"machines": {"m1": "plain"}}), encoding="utf-8")
        store = SecretStore(FileBackend(path))
        assert store.get("m1") == {"username": "admin", "password": "plain"}

    def test_default_path_is_under_the_app_data_dir(self, isolated_app_data):
        backend = FileBackend()
        assert str(backend.path).startswith(str(isolated_app_data))


class TestDescribe:
    def test_describe_never_leaks_a_secret(self, store):
        store.set("m1", "admin", "hunter2")
        blob = json.dumps(store.describe())
        assert "hunter2" not in blob
        assert "admin" not in blob

    def test_file_backend_reports_itself_as_insecure(self, store):
        info = store.describe()
        assert info["backend"] == "file"
        assert info["secure"] is False
        assert "NOT ENCRYPTED" in info["warning"]

    def test_backend_name_and_secure_are_exposed(self, store):
        assert store.backend_name == "file"
        assert store.secure is False


class TestServiceNaming:
    def test_service_name_is_namespaced_per_machine(self):
        assert service_name("abc123") == "MoxaSerial:abc123"


class TestBackendSelection:
    def test_env_forces_the_file_backend(self, monkeypatch):
        monkeypatch.setenv("MOXASERIAL_SECRET_BACKEND", "file")
        assert secrets_mod.default_backend().name == "file"

    def test_env_can_force_the_keychain(self, monkeypatch):
        monkeypatch.setenv("MOXASERIAL_SECRET_BACKEND", "keychain")
        assert secrets_mod.default_backend().name == "macos-keychain"

    def test_no_env_on_a_mac_picks_the_keychain(self, monkeypatch):
        monkeypatch.delenv("MOXASERIAL_SECRET_BACKEND", raising=False)
        monkeypatch.setattr(secrets_mod.sys, "platform", "darwin")
        monkeypatch.setattr(secrets_mod.os.path, "exists", lambda p: True)
        assert secrets_mod.default_backend().name == "macos-keychain"

    def test_a_plain_linux_box_falls_back_to_the_file(self, monkeypatch):
        monkeypatch.delenv("MOXASERIAL_SECRET_BACKEND", raising=False)
        monkeypatch.setattr(secrets_mod.sys, "platform", "linux")
        monkeypatch.setattr(secrets_mod.os, "name", "posix")
        assert secrets_mod.default_backend().name == "file"


class FakeRun:
    """Stands in for ``/usr/bin/security`` so the real keychain is safe."""

    def __init__(self):
        self.items: dict[tuple[str, str], str] = {}
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        verb = args[0]
        opts: dict[str, str] = {}
        rest = list(args[1:])
        while rest:
            flag = rest.pop(0)
            if flag == "-U":  # a bare flag, not an option with a value
                continue
            opts[flag] = rest.pop(0) if rest else ""
        key = (opts.get("-a", ""), opts.get("-s", ""))

        class Done:
            def __init__(self, code, out=b""):
                self.returncode, self.stdout, self.stderr = code, out, b""

        if verb == "add-generic-password":
            self.items[key] = opts.get("-w", "")
            return Done(0)
        if verb == "find-generic-password":
            if key in self.items:
                return Done(0, self.items[key].encode() + b"\n")
            return Done(44)
        if verb == "delete-generic-password":
            return Done(0) if self.items.pop(key, None) is not None else Done(44)
        return Done(1)


class TestKeychainBackend:
    @pytest.fixture
    def backend(self, monkeypatch):
        backend = KeychainBackend()
        fake = FakeRun()
        monkeypatch.setattr(backend, "_run", fake)
        backend.fake = fake
        return backend

    def test_round_trip(self, backend):
        store = SecretStore(backend)
        store.set("m1", "admin", "secret")
        assert store.get("m1") == {"username": "admin", "password": "secret"}
        assert store.clear("m1") is True
        assert store.get("m1") is None

    def test_write_uses_the_documented_argv(self, backend):
        SecretStore(backend).set("m1", "admin", "secret")
        args = backend.fake.calls[0]
        assert args[0] == "add-generic-password"
        assert "-U" in args
        assert args[args.index("-a") + 1] == "m1"
        assert args[args.index("-s") + 1] == "MoxaSerial:m1"

    def test_a_missing_item_is_not_an_error(self, backend):
        assert SecretStore(backend).get("nope") is None

    def test_a_failing_keychain_raises_on_write(self, backend, monkeypatch):
        class Done:
            returncode, stdout, stderr = 1, b"", b"User interaction is not allowed."

        monkeypatch.setattr(backend, "_run", lambda args: Done())
        with pytest.raises(SecretError, match="refused"):
            SecretStore(backend).set("m1", "admin", "x")

    def test_an_unreachable_binary_reads_as_no_credentials(self, monkeypatch):
        backend = KeychainBackend()

        def boom(args):
            raise SecretError("no such binary")

        monkeypatch.setattr(backend, "_run", boom)
        assert SecretStore(backend).get("m1") is None
