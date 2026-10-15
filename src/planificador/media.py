"""Media ingest, metadata, thumbnails and derivatives (SPEC §8).

STUB created by the foundation agent so imports resolve. TODO(media agent): implement.
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from sqlalchemy.orm import Session

from planificador.models import MediaAsset
from planificador.providers.base import MediaInfo


def ingest(db: Session, file_obj: BinaryIO, original_name: str, brand_id: int | None) -> MediaAsset:
    raise NotImplementedError("media.ingest")


def derived_jpeg(asset: MediaAsset, max_bytes: int = 8_000_000) -> Path:
    raise NotImplementedError("media.derived_jpeg")


def to_media_info(asset: MediaAsset) -> MediaInfo:
    raise NotImplementedError("media.to_media_info")
