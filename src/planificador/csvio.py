"""CSV import/export of posts (SPEC §14).

STUB created by the foundation agent so imports resolve. TODO(CSV agent): implement template download
(UTF-8 with BOM), import with ``;``/``,`` autodetect + preview/validation, and export.
"""

from __future__ import annotations

#: Template columns, in order (SPEC §14).
TEMPLATE_COLUMNS: tuple[str, ...] = (
    "fecha",
    "hora",
    "texto",
    "facebook",
    "instagram",
    "tiktok",
    "youtube",
    "formato_facebook",
    "formato_instagram",
    "formato_tiktok",
    "medios",
    "privacidad_tiktok",
    "primer_comentario",
    "autopublicar",
    "formato_youtube",
    "titulo_youtube",
    "privacidad_youtube",
    "ninos_youtube",
)
