"""Secret encryption at rest (SPEC §3).

A random master key (``Fernet.generate_key()``) lives in the OS credential store via ``keyring``
(service ``planificador-redes``, user ``master-key``). When no usable keyring backend exists (headless Linux,
containers, a locked-down Windows session...) on a **fresh** install we fall back silently to
``<data>/secret.key`` created with mode 0600. Once a key file exists it always wins.

The app never silently switches keys between runs: next to the key file, ``secret.key.info.json`` records (in
clear, it is not secret) where the key lives and a short fingerprint of it. When the keyring call *fails*
(timeout, locked keychain, D-Bus hiccup...) as opposed to answering "no entry", and the data folder already
holds encrypted values, we wait/retry and then refuse to start (:class:`MasterKeyUnavailable`, Spanish
message) instead of minting a new key that would make every stored token and app secret unreadable. A
loaded key whose fingerprint differs from the recorded one is refused the same way.
``PLANIFICADOR_RESET_MASTER_KEY=1`` is the explicit escape hatch (start over with a new key).

Encrypted values are text: ``"v1:" + Fernet token``. The ``v1`` prefix leaves room for key rotation
(``MultiFernet``) later.

Threat model: protects copies of the data folder (backups, cloud sync, other OS users). It does not protect
against malware running as the same user.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
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
ENV_RESET_KEY = "PLANIFICADOR_RESET_MASTER_KEY"
KEYRING_TIMEOUT_S = 10.0
#: Extra wait for a slow keyring (unlock prompt, busy PC at logon) when stored secrets depend on its key.
KEYRING_PATIENCE_S = 60.0
KEYRING_RETRY_DELAY_S = 1.0
MARKER_SUFFIX = ".info.json"

_TRUE = {"1", "true", "yes", "si", "sí"}


class CryptoError(Exception):
    """Raised when a value cannot be decrypted (wrong key, corrupted value, unknown prefix)."""


class MasterKeyUnavailable(CryptoError):
    """The key that encrypted the stored secrets cannot be loaded now; a new key is deliberately not created.

    ``str(exc)`` is a Spanish message for the user (printed by the launcher)."""


@dataclass(frozen=True)
class MasterKey:
    key: bytes  # urlsafe-base64 Fernet key (44 bytes)
    source: Literal["keyring", "file", "memory"]


# ------------------------------------------------------------------------------------------- keyring


def keyring_disabled() -> bool:
    return os.environ.get(ENV_NO_KEYRING, "").strip().lower() in _TRUE


def reset_requested() -> bool:
    return os.environ.get(ENV_RESET_KEY, "").strip().lower() in _TRUE


class _BackgroundCall[T]:
    """Run ``fn`` in a daemon thread (a keyring can hang forever) and wait for it with a deadline.

    :meth:`result` can be called again after a timeout to keep waiting for the *same* call (e.g. while the
    user answers a keychain prompt) instead of starting a second one."""

    def __init__(self, fn: Callable[[], T]) -> None:
        self._done = threading.Event()
        self._value: T | None = None
        self._error: BaseException | None = None

        def runner() -> None:
            try:
                self._value = fn()
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller thread
                self._error = exc
            finally:
                self._done.set()

        threading.Thread(target=runner, name="keyring-call", daemon=True).start()

    def result(self, timeout: float) -> T:
        if not self._done.wait(timeout):
            raise TimeoutError("keyring call timed out")
        if self._error is not None:
            raise self._error
        return self._value  # type: ignore[return-value]


def _with_timeout[T](fn: Callable[[], T], timeout: float = KEYRING_TIMEOUT_S) -> T:
    """Run ``fn`` in a daemon thread; raise TimeoutError if it does not finish (a keyring can hang)."""
    return _BackgroundCall(fn).result(timeout)


def _keyring_get_call() -> _BackgroundCall[str | None]:
    import keyring

    return _BackgroundCall(lambda: keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME))


def _keyring_get() -> str | None:
    return _keyring_get_call().result(KEYRING_TIMEOUT_S)


def _keyring_get_patiently(data_at_risk: Callable[[], bool]) -> str | None:
    """Read the keyring entry. When stored secrets depend on the keyring key, a slow call gets more time and a
    failing one is retried once before the error propagates (the caller then refuses to start)."""
    call = _keyring_get_call()
    try:
        return call.result(KEYRING_TIMEOUT_S)
    except TimeoutError:
        if not data_at_risk():
            raise
        log.warning(
            "Esperando al almacén de credenciales del sistema "
            "(desbloquéalo o acepta su aviso si te lo pide)..."
        )
        return call.result(KEYRING_PATIENCE_S)
    except Exception as exc:
        if not data_at_risk():
            raise
        log.warning("Keyring read failed (%s); retrying once", type(exc).__name__)
        time.sleep(KEYRING_RETRY_DELAY_S)
        return _keyring_get_call().result(KEYRING_PATIENCE_S)


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


# ------------------------------------------------------------------------------- key marker (not secret)


@dataclass(frozen=True)
class KeyMarker:
    """Where the master key of a data folder lives and its fingerprint (``<key file>.info.json``)."""

    source: str
    fingerprint: str


def marker_path(key_file: Path) -> Path:
    return key_file.with_name(key_file.name + MARKER_SUFFIX)


def key_fingerprint(key: bytes) -> str:
    """Short, non-reversible id of a key (to notice a key change; reveals nothing usable about the key)."""
    return hashlib.sha256(b"planificador-key-fingerprint:" + key).hexdigest()[:16]


def _read_marker(key_file: Path) -> KeyMarker | None:
    path = marker_path(key_file)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("Key marker %s is unreadable; ignoring it", path)
        return None
    if not isinstance(data, dict) or not data.get("fingerprint"):
        return None
    return KeyMarker(str(data.get("source") or ""), str(data["fingerprint"]))


def _write_marker(key_file: Path, master: MasterKey) -> None:
    path = marker_path(key_file)
    payload = {
        "source": master.source,
        "fingerprint": key_fingerprint(master.key),
        "nota": (
            "No contiene la clave de cifrado: solo dónde está y su huella, para que Planificador nunca "
            "cambie de clave sin avisar."
        ),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:  # pragma: no cover - best effort (read-only folder...)
        log.warning("Could not write the key marker %s", path, exc_info=True)


def _unavailable_message(exc: BaseException) -> str:
    detail = str(exc).strip() or type(exc).__name__
    return (
        "No se ha podido leer la clave de cifrado guardada en el almacén de credenciales del sistema "
        f"({detail}). Tus claves de apps y los tokens de tus cuentas están cifrados con ella, así que "
        "Planificador no va a crear una clave nueva (los perderías). Desbloquea el Administrador de "
        "credenciales / llavero del sistema (o acepta su aviso de acceso) y vuelve a abrir Planificador. "
        + _reset_hint()
    )


def _mismatch_message(master: MasterKey, key_file: Path) -> str:
    where = f"el archivo {key_file}" if master.source == "file" else "el almacén de credenciales del sistema"
    return (
        f"La clave de cifrado que hay en {where} no es la que cifró los secretos de esta carpeta de datos "
        f"({key_file.parent}). Planificador no arranca con ella para no perder tus claves de apps y tokens. "
        "Si has movido o restaurado archivos, vuelve a dejar el secret.key (o la carpeta) original. "
        + _reset_hint()
    )


def _no_keyring_message() -> str:
    return (
        f"{ENV_NO_KEYRING} está activado, pero la clave que cifró los secretos de esta carpeta de datos "
        "está en el almacén de credenciales del sistema. Quita esa variable y vuelve a abrir Planificador. "
        + _reset_hint()
    )


def _reset_hint() -> str:
    return (
        f"Si esa clave se ha perdido para siempre, arranca una vez con {ENV_RESET_KEY}=1: se creará una "
        "clave nueva y tendrás que volver a pegar las claves en Ajustes › Credenciales y reconectar tus "
        "cuentas."
    )


def load_or_create_master_key(
    key_file: Path,
    *,
    use_keyring: bool | None = None,
    has_encrypted_data: Callable[[], bool] | None = None,
) -> MasterKey:
    """Find or create the master key.

    Order: existing key file → existing keyring entry → new key stored in keyring (verified by read-back) →
    new key file. A new key is only minted when nothing depends on the old one: the keyring *answered* that
    it has no entry (first run, or a data folder restored on another PC), or it is unusable and the data
    folder holds no encrypted values yet. ``has_encrypted_data`` tells whether stored secrets exist (the app
    checks the database); without it, an existing key marker counts as "secrets may exist".

    Raises :class:`MasterKeyUnavailable` (instead of silently switching keys) when the keyring call fails or
    the loaded key's fingerprint differs from the recorded one while encrypted data exists. Setting
    ``PLANIFICADOR_RESET_MASTER_KEY=1`` accepts the switch.
    """
    if use_keyring is None:
        use_keyring = not keyring_disabled()
    key_file = Path(key_file)
    marker = _read_marker(key_file)
    reset = reset_requested()
    risk_cache: list[bool] = []

    def data_at_risk() -> bool:
        """Would a different key make stored secrets unreadable?"""
        if not risk_cache:
            if has_encrypted_data is None:
                risk_cache.append(marker is not None)
            else:
                try:
                    risk_cache.append(bool(has_encrypted_data()))
                except Exception:  # noqa: BLE001 - be conservative: assume secrets exist
                    log.warning("Could not check for encrypted values", exc_info=True)
                    risk_cache.append(True)
        return risk_cache[0]

    def adopt(master: MasterKey, *, fresh: bool) -> MasterKey:
        fingerprint = key_fingerprint(master.key)
        if marker is not None and marker.fingerprint != fingerprint:
            if fresh or reset or not data_at_risk():
                log.warning(
                    "Master key changed (%s → %s, source %s); previously stored secrets cannot be decrypted "
                    "and must be entered again",
                    marker.fingerprint,
                    fingerprint,
                    master.source,
                )
            else:
                raise MasterKeyUnavailable(_mismatch_message(master, key_file))
        if marker is None or marker.fingerprint != fingerprint or marker.source != master.source:
            _write_marker(key_file, master)
        return master

    file_key = _read_key_file(key_file)
    if file_key is not None:
        return adopt(MasterKey(file_key, "file"), fresh=False)

    if use_keyring:
        try:
            stored_raw = _keyring_get_patiently(data_at_risk)
        except Exception as exc:  # noqa: BLE001 - any backend failure
            if data_at_risk() and not reset:
                log.error("Keyring unavailable (%s) and stored secrets depend on its key", type(exc).__name__)
                raise MasterKeyUnavailable(_unavailable_message(exc)) from exc
            log.info("Keyring unavailable (%s); using the key file fallback", type(exc).__name__)
        else:
            stored = _valid_fernet_key(stored_raw)
            if stored is not None:
                return adopt(MasterKey(stored, "keyring"), fresh=False)
            # The keyring answered "no entry" (first run, or a data folder copied from another PC).
            if stored_raw:
                log.warning("The keyring entry does not hold a valid key; replacing it")
            new_key = Fernet.generate_key()
            try:
                if _keyring_set(new_key.decode("ascii")):
                    log.info("Master key stored in the OS keyring")
                    return adopt(MasterKey(new_key, "keyring"), fresh=True)
                log.info("Keyring backend did not keep the key; using the key file fallback")
            except Exception as exc:  # noqa: BLE001 - nothing was encrypted with a key we could still read
                log.info("Keyring unavailable (%s); using the key file fallback", type(exc).__name__)
    elif marker is not None and marker.source == "keyring" and data_at_risk() and not reset:
        raise MasterKeyUnavailable(_no_keyring_message())

    generated = Fernet.generate_key()
    key = _create_key_file(key_file, generated)
    return adopt(MasterKey(key, "file"), fresh=key == generated)


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


def init(
    key_file: Path | None = None,
    *,
    use_keyring: bool | None = None,
    has_encrypted_data: Callable[[], bool] | None = None,
) -> SecretBox:
    """Initialise (or return the already initialised) box for ``key_file``.

    ``key_file`` defaults to ``<data dir>/secret.key`` of the resolved data dir. Calling again with a
    different key file (tests use a fresh data dir each) re-initialises. ``has_encrypted_data`` see
    :func:`load_or_create_master_key`.
    """
    global _box, _box_key_file
    if key_file is None:
        from planificador.paths import get_paths

        key_file = get_paths().secret_key_file
    key_file = Path(key_file)
    with _lock:
        if _box is not None and _box_key_file == key_file:
            return _box
        _box = SecretBox(
            load_or_create_master_key(
                key_file, use_keyring=use_keyring, has_encrypted_data=has_encrypted_data
            )
        )
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
