"""Command line entry point: ``planificador`` / ``python -m planificador`` (SPEC §2).

- ``--port`` (default 8765), ``--no-browser``, ``--data-dir`` (else ``PLANIFICADOR_DATA_DIR``, else the
  platform default).
- Single instance: an OS-level lock on ``<data>/instance.lock`` (released automatically if the process dies).
  A second launch only opens the browser at the running instance (port read from ``instance.json``).
- Uvicorn bound to **127.0.0.1** only; the browser opens ``http://localhost:<port>/`` once the server is up.
- ``--quitar-contrasena``: removes a forgotten app password and exits (``quitar-contrasena.bat`` on Windows).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import IO, Any

from planificador import DEFAULT_PORT, __version__
from planificador.paths import ENV_DATA_DIR, DataPaths, resolve_data_dir

ENV_NO_BROWSER = "PLANIFICADOR_NO_BROWSER"


# ------------------------------------------------------------------------------------ instance lock


class InstanceLock:
    """Non-blocking exclusive lock on a file, held for the process lifetime.

    Uses ``msvcrt.locking`` on Windows and ``fcntl.flock`` elsewhere, so a crashed process never leaves a
    stale lock. Owner info (pid, port, url) lives in a separate JSON file that stays readable while locked.
    """

    def __init__(self, lock_path: Path, info_path: Path | None = None) -> None:
        self.lock_path = Path(lock_path)
        self.info_path = Path(info_path) if info_path else self.lock_path.with_suffix(".json")
        self._fh: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self, info: dict[str, Any] | None = None) -> bool:
        """Try to take the lock. True on success (and writes ``info``); False if another process holds it."""
        if self._fh is not None:
            return True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+b")  # noqa: SIM115 - kept open while the lock is held
        try:
            _lock_file(fh)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        if info is not None:
            self.write_info(info)
        return True

    def write_info(self, info: dict[str, Any]) -> None:
        tmp = self.info_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(info), encoding="utf-8")
        os.replace(tmp, self.info_path)

    def read_info(self) -> dict[str, Any] | None:
        return read_instance_info(self.info_path)

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        self._fh = None
        try:
            self.info_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            _unlock_file(fh)
        except OSError:
            pass
        fh.close()

    def __enter__(self) -> InstanceLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def read_instance_info(info_path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(info_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _lock_file(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------------------------- CLI


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"puerto no válido: {value!r} (usa un número entre 1 y 65535)")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="planificador",
        description="Planificador de Redes: abre la aplicación en http://localhost.",
    )
    parser.add_argument(
        "--port", type=_port, default=None, help=f"Puerto local (por defecto {DEFAULT_PORT})."
    )
    parser.add_argument("--no-browser", action="store_true", help="No abrir el navegador al arrancar.")
    parser.add_argument(
        "--data-dir", default=None, help="Carpeta de datos (base de datos, medios, registros)."
    )
    parser.add_argument(
        "--quitar-contrasena",
        action="store_true",
        help="Quita la contraseña de acceso (si la has olvidado) y sale.",
    )
    parser.add_argument("--version", action="version", version=f"planificador {__version__}")
    return parser


def remove_password(paths: DataPaths) -> int:
    """``--quitar-contrasena``: clear the optional app password. Whoever can run this on the PC can already
    read the data folder, so it grants nothing new; it also works while the app is running (the server
    re-reads the setting within seconds)."""
    from sqlalchemy.exc import SQLAlchemyError

    from planificador import security
    from planificador.db import Database

    if not paths.db_path.exists():
        print("No hay datos del Planificador en esta carpeta: no hay ninguna contraseña que quitar.")
        return 0
    database = Database(paths.db_path)
    try:
        with database.session_scope() as db:
            had_password = security.password_enabled(db)
            if had_password:
                security.set_password(db, None, commit=False)
    except SQLAlchemyError as exc:
        print(f"No se ha podido quitar la contraseña: {exc}", file=sys.stderr)
        return 1
    finally:
        database.dispose()
    if had_password:
        print(
            "Contraseña quitada: ya puedes entrar sin contraseña. Puedes poner otra en Ajustes › Seguridad."
        )
    else:
        print("La aplicación no tenía contraseña.")
    return 0


def app_url(port: int) -> str:
    return f"http://localhost:{port}/"


STD_INPUT_HANDLE = -10
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080


def disable_console_quick_edit(kernel32: Any = None) -> bool:
    """Windows: turn off QuickEdit on this console. With it on (the conhost default on Windows 10), a click in
    the black window starts a selection that **blocks every write to the console** until Esc/Enter, which
    would freeze the server (access log) and the publishing threads (logging lock). Returns True when the
    mode was changed; any failure (no console, redirected stdin, not Windows) is ignored."""
    if kernel32 is None:
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            kernel32.GetStdHandle.restype = wintypes.HANDLE
            kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
            kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        except Exception:  # pragma: no cover - Windows only
            return False
    try:
        import ctypes

        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE & 0xFFFFFFFF)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return False
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        new_mode = (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE
        if new_mode == mode.value:
            return False
        return bool(kernel32.SetConsoleMode(handle, new_mode))
    except Exception:  # pragma: no cover - best effort
        return False


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):  # pragma: no cover
                pass
    disable_console_quick_edit()


def _open_browser_when_ready(server: Any, url: str, timeout: float = 30.0) -> None:
    def wait_and_open() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if getattr(server, "started", False):
                webbrowser.open(url)
                return
            if getattr(server, "should_exit", False):
                return
            time.sleep(0.2)

    threading.Thread(target=wait_and_open, name="open-browser", daemon=True).start()


class _Terminated(Exception):
    """Raised by our SIGTERM/SIGBREAK handler so ``main`` can clean up (lock + instance.json)."""


def _raise_terminated(signum: int, frame: Any) -> None:
    raise _Terminated(signum)


def _install_termination_handlers() -> None:
    """Uvicorn shuts down gracefully on SIGTERM and then re-raises the signal with the handler that was
    installed before it started. With the default handler that kills the process before our ``finally``
    runs; this handler turns it into an exception instead."""
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _raise_terminated)
        except (ValueError, OSError):  # not the main thread (tests) or unsupported
            return


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(argv)

    if args.data_dir:
        os.environ[ENV_DATA_DIR] = str(Path(args.data_dir).expanduser().resolve())
    open_browser = not args.no_browser and os.environ.get(ENV_NO_BROWSER, "") not in ("1", "true")

    from planificador.config import load_app_settings

    settings = load_app_settings(data_dir=args.data_dir, port=args.port, log_to_console=True)
    paths = DataPaths(resolve_data_dir(settings.data_dir)).ensure()
    if args.quitar_contrasena:
        return remove_password(paths)

    lock = InstanceLock(paths.lock_file, paths.instance_info_file)
    url = app_url(settings.port)
    if not lock.acquire({"pid": os.getpid(), "port": settings.port, "url": url, "version": __version__}):
        info = lock.read_info() or {}
        running_url = str(info.get("url") or app_url(int(info.get("port") or settings.port)))
        print(f"Planificador ya está en marcha en {running_url}")
        if open_browser:
            webbrowser.open(running_url)
        return 0

    try:
        import uvicorn

        from planificador.app import create_app
        from planificador.crypto import MasterKeyUnavailable

        try:
            app = create_app(settings)
        except MasterKeyUnavailable as exc:
            # The stored secrets depend on a key we cannot load right now: say so instead of a traceback.
            print(f"\n{exc}\n", file=sys.stderr)
            return 1
        config = uvicorn.Config(
            app,
            host="127.0.0.1",  # loopback only — never expose the app on the network
            port=settings.port,
            log_config=None,  # our logging setup (rotating file + console) handles uvicorn's loggers
            access_log=True,
            proxy_headers=False,
            server_header=False,
        )
        server = uvicorn.Server(config)
        print(f"Planificador {__version__} en {url}  (datos: {paths.root})")
        print(
            "No cierres esta ventana mientras quieras que se publiquen tus programaciones. Ctrl+C para salir."
        )
        if open_browser:
            _open_browser_when_ready(server, url)
        _install_termination_handlers()
        try:
            server.run()
        except (KeyboardInterrupt, _Terminated):  # uvicorn re-raises the captured signal after shutdown
            print("Planificador detenido.")
            return 0
        except SystemExit:
            # Uvicorn calls sys.exit(3) when it cannot bind the port or the lifespan startup fails; turn
            # that into our Spanish hint instead of an unexplained exit.
            if getattr(server, "started", False):
                raise
        if not server.started:
            print(
                f"No se pudo iniciar el servidor en el puerto {settings.port}. "
                "¿Hay otro programa usándolo? Prueba con --port. Los detalles están en logs/app.log.",
                file=sys.stderr,
            )
            return 1
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
