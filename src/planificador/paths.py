"""Data directory resolution (SPEC §2).

Resolution order for the data root:

1. explicit argument (``--data-dir`` / ``create_app`` override),
2. ``PLANIFICADOR_DATA_DIR`` environment variable,
3. Windows: ``%LOCALAPPDATA%\\PlanificadorRedes``,
4. elsewhere: ``$XDG_DATA_HOME/planificador-redes`` or ``~/.local/share/planificador-redes``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ENV_DATA_DIR = "PLANIFICADOR_DATA_DIR"
WINDOWS_DIR_NAME = "PlanificadorRedes"
POSIX_DIR_NAME = "planificador-redes"

DB_FILENAME = "planificador.sqlite3"

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
TEMPLATES_DIR = PACKAGE_DIR / "templates"


def default_data_dir() -> Path:
    """Platform default data dir (ignores the environment override)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / WINDOWS_DIR_NAME
        return Path.home() / "AppData" / "Local" / WINDOWS_DIR_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / POSIX_DIR_NAME
    return Path.home() / ".local" / "share" / POSIX_DIR_NAME


def resolve_data_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Return the absolute data root, honouring the explicit value and the env override."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        return Path(env).expanduser().resolve()
    return default_data_dir()


@dataclass(frozen=True)
class DataPaths:
    """All well-known locations inside the data dir."""

    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / DB_FILENAME

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def originals_dir(self) -> Path:
        return self.media_dir / "originals"

    @property
    def thumbs_dir(self) -> Path:
        return self.media_dir / "thumbs"

    @property
    def derived_dir(self) -> Path:
        return self.media_dir / "derived"

    @property
    def avatars_dir(self) -> Path:
        return self.media_dir / "avatars"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "app.log"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def secret_key_file(self) -> Path:
        return self.root / "secret.key"

    @property
    def lock_file(self) -> Path:
        return self.root / "instance.lock"

    @property
    def instance_info_file(self) -> Path:
        """JSON with pid/port of the running instance (readable while the lock is held)."""
        return self.root / "instance.json"

    def ensure(self) -> DataPaths:
        """Create every directory (idempotent). Returns self for chaining."""
        for d in (
            self.root,
            self.media_dir,
            self.originals_dir,
            self.thumbs_dir,
            self.derived_dir,
            self.avatars_dir,
            self.tmp_dir,
            self.bin_dir,
            self.logs_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        return self


def get_paths(root: str | os.PathLike[str] | None = None) -> DataPaths:
    """Build :class:`DataPaths` for ``root`` (or the resolved default). Does not create directories."""
    return DataPaths(resolve_data_dir(root))
