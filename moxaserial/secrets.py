"""Per-machine web-console credentials, kept out of ``settings.json``.

The NPort's web console needs a login before it will show a port's
operation mode or its line settings, and later for auto-configuration.
Those credentials are the operator's, not the add-in's, so they never go
into the settings file, never appear in a log line, and never cross the
bridge - the UI only ever learns a boolean ``has_credentials``.

Backends, in order of preference:

* **macOS** - the login keychain via ``/usr/bin/security``.
* **Windows** - Credential Manager via ``advapi32`` (``CredWriteW`` /
  ``CredReadW`` / ``CredDeleteW``), ``CRED_TYPE_GENERIC``.
* **anything else** - a 0600 file under the app data directory. This is
  *weaker*: it is obfuscated, not encrypted, and anyone who can read the
  file as that user can read the password. :meth:`SecretStore.describe`
  reports ``secure: false`` so the UI can say so out loud.

The keychain/credential entry is keyed ``MoxaSerial:<machine_id>`` and
holds a small JSON blob with both the username and the password, so the
username is protected too and a machine can be renamed freely.

``MOXASERIAL_SECRET_BACKEND=file`` forces the file backend (the test
suite uses it, so no test ever touches a real keychain).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from moxaserial import paths
from moxaserial.log import get_logger

log = get_logger("secrets")

SERVICE_PREFIX = "MoxaSerial"
_BACKEND_ENV = "MOXASERIAL_SECRET_BACKEND"
_FILE_NAME = "credentials.json"

_FILE_WARNING = (
    "NOT ENCRYPTED. This file is a fallback for systems with no OS credential "
    "store. It is readable by anyone who can read it as this user. Delete it and "
    "re-enter the credentials on a machine with a keychain if that matters."
)


class SecretError(Exception):
    """The credential store refused to read or write."""


def service_name(machine_id: str) -> str:
    return f"{SERVICE_PREFIX}:{machine_id}"


class _Backend:
    name = "none"
    secure = False

    def load(self, machine_id: str) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError

    def store(self, machine_id: str, blob: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def delete(self, machine_id: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class KeychainBackend(_Backend):
    """macOS login keychain through ``/usr/bin/security``."""

    name = "macos-keychain"
    secure = True
    binary = "/usr/bin/security"

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(  # noqa: S603 - fixed binary, no shell
                [self.binary, *args], capture_output=True, timeout=20, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecretError(f"The macOS keychain is not reachable: {exc}") from exc

    def load(self, machine_id: str) -> str | None:
        done = self._run(
            ["find-generic-password", "-a", machine_id, "-s", service_name(machine_id), "-w"]
        )
        if done.returncode != 0:
            return None
        return done.stdout.decode("utf-8", "replace").rstrip("\n")

    def store(self, machine_id: str, blob: str) -> None:
        # -U updates an existing item instead of failing. The secret does
        # ride on the argv of a short-lived child process; macOS offers no
        # stdin form of this command, and the alternative (a temp file) is
        # worse. It is never written to a log or a file by us.
        done = self._run(
            [
                "add-generic-password",
                "-U",
                "-a", machine_id,
                "-s", service_name(machine_id),
                "-l", f"{SERVICE_PREFIX} {machine_id}",
                "-D", "application password",
                "-w", blob,
            ]
        )
        if done.returncode != 0:
            raise SecretError(
                "The macOS keychain refused to store the credentials "
                f"({done.stderr.decode('utf-8', 'replace').strip() or done.returncode})."
            )

    def delete(self, machine_id: str) -> bool:
        done = self._run(
            ["delete-generic-password", "-a", machine_id, "-s", service_name(machine_id)]
        )
        return done.returncode == 0


class WindowsCredentialBackend(_Backend):
    """Windows Credential Manager through ``advapi32`` and :mod:`ctypes`."""

    name = "windows-credential-manager"
    secure = True

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._advapi = ctypes.WinDLL("advapi32", use_last_error=True)

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        self._CREDENTIAL = CREDENTIAL
        self._PCREDENTIAL = ctypes.POINTER(CREDENTIAL)

        self._advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(self._PCREDENTIAL)
        ]
        self._advapi.CredReadW.restype = wintypes.BOOL
        self._advapi.CredWriteW.argtypes = [self._PCREDENTIAL, wintypes.DWORD]
        self._advapi.CredWriteW.restype = wintypes.BOOL
        self._advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._advapi.CredDeleteW.restype = wintypes.BOOL
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi.CredFree.restype = None

    def load(self, machine_id: str) -> str | None:
        ctypes = self._ctypes
        pcred = self._PCREDENTIAL()
        ok = self._advapi.CredReadW(
            service_name(machine_id), self.CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)
        )
        if not ok:
            return None
        try:
            cred = pcred.contents
            size = int(cred.CredentialBlobSize)
            if size <= 0:
                return ""
            raw = ctypes.string_at(cred.CredentialBlob, size)
            return raw.decode("utf-16-le", "replace")
        finally:
            self._advapi.CredFree(pcred)

    def store(self, machine_id: str, blob: str) -> None:
        ctypes = self._ctypes
        encoded = blob.encode("utf-16-le")
        buffer = ctypes.create_string_buffer(encoded, len(encoded))
        cred = self._CREDENTIAL()
        cred.Flags = 0
        cred.Type = self.CRED_TYPE_GENERIC
        cred.TargetName = service_name(machine_id)
        cred.Comment = f"{SERVICE_PREFIX} web console credentials"
        cred.CredentialBlobSize = len(encoded)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
        cred.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = machine_id
        if not self._advapi.CredWriteW(ctypes.byref(cred), 0):
            raise SecretError(
                "Windows Credential Manager refused to store the credentials "
                f"(error {ctypes.get_last_error()})."
            )

    def delete(self, machine_id: str) -> bool:
        return bool(
            self._advapi.CredDeleteW(service_name(machine_id), self.CRED_TYPE_GENERIC, 0)
        )


class FileBackend(_Backend):
    """0600 JSON file. Weaker than a keychain, and says so."""

    name = "file"
    secure = False

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path or (paths.app_data_dir() / _FILE_NAME)

    def _read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Credential file is not valid JSON; ignoring it.")
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        # Create with 0600 from the start - never a window where it is 0644.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def load(self, machine_id: str) -> str | None:
        with self._lock:
            entry = self._read().get("machines", {}).get(machine_id)
        return entry if isinstance(entry, str) else None

    def store(self, machine_id: str, blob: str) -> None:
        with self._lock:
            data = self._read()
            data["_warning"] = _FILE_WARNING
            data.setdefault("machines", {})
            if not isinstance(data["machines"], dict):
                data["machines"] = {}
            data["machines"][machine_id] = blob
            self._write(data)

    def delete(self, machine_id: str) -> bool:
        with self._lock:
            data = self._read()
            machines = data.get("machines")
            if not isinstance(machines, dict) or machine_id not in machines:
                return False
            machines.pop(machine_id)
            self._write(data)
            return True


def default_backend() -> _Backend:
    forced = (os.environ.get(_BACKEND_ENV) or "").strip().lower()
    if forced == "file":
        return FileBackend()
    if forced in ("keychain", "macos", "macos-keychain"):
        return KeychainBackend()
    if forced in ("windows", "wincred", "credential-manager"):
        return WindowsCredentialBackend()
    if sys.platform == "darwin" and os.path.exists(KeychainBackend.binary):
        return KeychainBackend()
    if os.name == "nt":
        try:
            return WindowsCredentialBackend()
        except Exception as exc:  # noqa: BLE001 - never block on a missing DLL
            log.warning("Windows Credential Manager unavailable (%s); using the file store.", exc)
    return FileBackend()


class SecretStore:
    """Per-machine ``{username, password}``, stored by the OS where possible.

    Every method is keyed by the machine id from ``settings.json``; the
    credentials themselves live somewhere else entirely.
    """

    def __init__(self, backend: _Backend | None = None) -> None:
        self._backend = backend or default_backend()
        self._lock = threading.RLock()

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def secure(self) -> bool:
        return bool(self._backend.secure)

    def describe(self) -> dict[str, Any]:
        """Safe to put in a bridge payload - no secrets, ever."""
        info: dict[str, Any] = {
            "backend": self._backend.name,
            "secure": self._backend.secure,
        }
        if isinstance(self._backend, FileBackend):
            info["path"] = str(self._backend.path)
            info["warning"] = _FILE_WARNING
        return info

    # -- read ------------------------------------------------------------
    def get(self, machine_id: str) -> dict[str, str] | None:
        """``{"username": ..., "password": ...}`` or ``None``."""
        mid = str(machine_id or "").strip()
        if not mid:
            return None
        with self._lock:
            try:
                blob = self._backend.load(mid)
            except SecretError as exc:
                log.warning("Could not read the credentials for %s: %s", mid, exc)
                return None
        if not blob:
            return None
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            # An entry written by hand, or by an older build: treat the
            # whole value as the password with the default account.
            return {"username": "admin", "password": blob}
        if not isinstance(data, dict):
            return None
        return {
            "username": str(data.get("username", "") or ""),
            "password": str(data.get("password", "") or ""),
        }

    def has(self, machine_id: str) -> bool:
        return self.get(machine_id) is not None

    def username(self, machine_id: str) -> str:
        """The stored account name, or "". Not a secret."""
        creds = self.get(machine_id)
        return creds["username"] if creds else ""

    # -- write -----------------------------------------------------------
    def set(self, machine_id: str, username: str, password: str) -> None:
        mid = str(machine_id or "").strip()
        if not mid:
            raise SecretError("Cannot store credentials without a machine id.")
        blob = json.dumps(
            {"username": str(username or ""), "password": str(password or "")},
            separators=(",", ":"),
        )
        with self._lock:
            self._backend.store(mid, blob)
        # Deliberately logs the account name only, never the password.
        log.info(
            "Stored web-console credentials for machine %s (account '%s') in the %s store.",
            mid, username, self._backend.name,
        )

    def clear(self, machine_id: str) -> bool:
        mid = str(machine_id or "").strip()
        if not mid:
            return False
        with self._lock:
            removed = self._backend.delete(mid)
        if removed:
            log.info("Cleared the stored web-console credentials for machine %s.", mid)
        return removed
