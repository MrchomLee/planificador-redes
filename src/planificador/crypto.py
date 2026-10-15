"""Secret encryption at rest (SPEC §3).

A random master key (``Fernet.generate_key()``) lives in the OS credential store via ``keyring``
(service ``planificador-redes``, user ``master-key``). When no usable keyring backend exists (headless Linux,
containers, a locked-down Windows session...) we fall back **silently** to ``<data>/secret.key`` created with
mode 0600. Once a key file exists it always wins, so the app never silently switches keys between runs.

Encrypted values are text: ``"v1:" + Fernet token``. The ``v1`` prefix leaves room for key rotation
(``MultiFernet``) later.

Threat model: protects copies of the data folder (backups, cloud sync, other OS users). It does not protect
against malware running as the same user.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

KEYRING_SERVICE = "planificador-redes"
KEYRING_USERNAME = "master-key"
PREFIX = "v1:"
ENV_NO_KEYRING = "PLANIFICADOR_NO_KEYRING"
KEYRING_TIMEOUT_S = 10.0


class CryptoError(Exception):
    """Raised when a value cannot be decrypted (wrong key, corrupted value, unknown prefix)."""


@dataclass(frozen=True)
class MasterKey:
    key: bytes  # urlsafe-base64 Fernet key (44 bytes)
    source: Literal["keyring", "file", "memory"]


# ------------------------------------------------------------------------------------------- keyring


def keyring_disabled() -> bool:
    return os.environ.get(ENV_NO_KEYRING, "").strip().lower() in {"1", "true", "yes", "si", "sí"}


def _with_timeout[T](fn: Callable[[], T], timeout: float = KEYRING_TIMEOUT_S) -> T:
    """Run ``fn`` in a daemon thread; raise TimeoutError if it does not finish (a keyring can hang)."""
    result: dict[str, object] = {}

    def runner() -> None:
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller thread
            result["error"] = exc

    t = threading.Thread(target=runner, name="keyring-call", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError("keyring call timed out")
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return result.get("value")  # type: ignore[return-value]


def _keyring_get() -> str | None:
    import keyring

    return _with_timeout(lambda: keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME))


def _keyring_set(value: str) -> bool:
    """Store ``value`` and read it back. False when the backend silently drops it (null backend)."""
    import keyring

    _with_timeout(lambda: keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, value))
    return _keyring_get() == value


def _valid_fernet_key(value: str | bytes | None) -> bytes | None:
    if not value:
        return None
    raw = value.encode("ascii", "ignore") if isinstance(value, str) else value
    raw = raw.strip()
    try:
        Fernet(raw)
    except (ValueError, TypeError):
        return None
    return raw


# ------------------------------------------------------------------------------------------ key file


def _read_key_file(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    key = _valid_fernet_key(data)
    if key is None:
        raise CryptoError(f"El archivo de clave {path} está dañado.")
    return key


def _create_key_file(path: Path, key: bytes) -> bytes:
    """Create the key file atomically with 0600 permissions. If it appeared meanwhile, return its key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = _read_key_file(path)
        if existing is None:  # pragma: no cover - deleted between calls
            raise CryptoError("No se pudo crear el archivo de clave.") from None
        return existing
    with os.fdopen(fd, "wb") as fh:
        fh.write(key + b"\n")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - best effort on Windows
        pass
    return key


def load_or_create_master_key(key_file: Path, *, use_keyring: bool | None = None) -> MasterKey:
    """Find or create the master key.

    Order: existing key file → existing keyring entry → new key stored in keyring (verified by read-back) →
    new key file. Any keyring failure (no backend, D-Bus missing, timeout...) falls back to the key file.
    """
    if use_keyring is None:
        use_keyring = not keyring_disabled()

    file_key = _read_key_file(key_file)
    if file_key is not None:
        return MasterKey(file_key, "file")

    if use_keyring:
        try:
            stored = _valid_fernet_key(_keyring_get())
            if stored is not None:
                return MasterKey(stored, "keyring")
            new_key = Fernet.generate_key()
            if _keyring_set(new_key.decode("ascii")):
                log.info("Master key stored in the OS keyring")
                return MasterKey(new_key, "keyring")
            log.info("Keyring backend did not keep the key; using the key file fallback")
        except Exception as exc:  # noqa: BLE001 - any backend failure means "no keyring"
            log.info("Keyring unavailable (%s); using the key file fallback", type(exc).__name__)

    key = _create_key_file(key_file, Fernet.generate_key())
    return MasterKey(key, "file")


# --------------------------------------------------------------------------------------- secret box


class SecretBox:
    """Encrypts/decrypts strings with the master key."""

    def __init__(self, master: MasterKey) -> None:
        self.master = master
        self._fernet = Fernet(master.key)

    @property
    def source(self) -> str:
        return self.master.source

    def encrypt(self, plaintext: str) -> str:
        if not isinstance(plaintext, str):
            raise TypeError("Only str values can be encrypted")
        return PREFIX + self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not isinstance(value, str) or not value.startswith(PREFIX):
            raise CryptoError("Valor cifrado con formato desconocido.")
        try:
            return self._fernet.decrypt(value[len(PREFIX) :].encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise CryptoError("No se pudo descifrar un secreto (¿cambió la clave maestra?).") from exc

    def derive(self, label: str) -> bytes:
        """Deterministic 32-byte subkey for ``label`` (HMAC-SHA256 of the raw master key)."""
        raw = base64.urlsafe_b64decode(self.master.key)
        return hmac.new(raw, f"planificador:{label}".encode(), hashlib.sha256).digest()


# ---------------------------------------------------------------------------------- module singleton

_lock = threading.Lock()
_box: SecretBox | None = None
_box_key_file: Path | None = None


def init(key_file: Path | None = None, *, use_keyring: bool | None = None) -> SecretBox:
    """Initialise (or return the already initialised) box for ``key_file``.

    ``key_file`` defaults to ``<data dir>/secret.key`` of the resolved data dir. Calling again with a
    different key file (tests use a fresh data dir each) re-initialises.
    """
    global _box, _box_key_file
    if key_file is None:
        from planificador.paths import get_paths

        key_file = get_paths().secret_key_file
    key_file = Path(key_file)
    with _lock:
        if _box is not None and _box_key_file == key_file:
            return _box
        _box = SecretBox(load_or_create_master_key(key_file, use_keyring=use_keyring))
        _box_key_file = key_file
        return _box


def set_box(box: SecretBox | None) -> None:
    """Install a specific box (tests) or clear it with ``None``."""
    global _box, _box_key_file
    with _lock:
        _box = box
        _box_key_file = None


def reset() -> None:
    set_box(None)


def get_box() -> SecretBox:
    box = _box
    if box is None:
        return init()
    return box


def memory_box() -> SecretBox:
    """A throwaway in-memory box (never persisted)."""
    return SecretBox(MasterKey(Fernet.generate_key(), "memory"))


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(plaintext: str) -> str:
    return get_box().encrypt(plaintext)


def decrypt(value: str) -> str:
    return get_box().decrypt(value)


def encrypt_optional(plaintext: str | None) -> str | None:
    """``None``/empty stays ``None``."""
    if plaintext is None or plaintext == "":
        return None
    return encrypt(plaintext)


def decrypt_optional(value: str | None, *, on_error: Literal["raise", "none"] = "raise") -> str | None:
    if value is None or value == "":
        return None
    try:
        return decrypt(value)
    except CryptoError:
        if on_error == "none":
            log.warning("A stored secret could not be decrypted; treating it as missing")
            return None
        raise


def session_secret() -> str:
    """Stable secret for the signed session cookie, derived from the master key."""
    return get_box().derive("session-cookie-v1").hex()


def key_source() -> str:
    """``keyring`` / ``file`` / ``memory`` — shown in Ajustes (warn when ``file``)."""
    return get_box().source


def mask_secret(value: str | None, visible: int = 4) -> str:
    """``****abcd`` for display. Empty → ``""``."""
    if not value:
        return ""
    tail = value[-visible:] if len(value) > visible * 2 else ""
    return "****" + tail
