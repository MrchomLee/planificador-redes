"""Per-network validation rules, formats and options (SPEC §7).

STUB created by the foundation agent so imports resolve. TODO(rules agent): replace with the data-driven
implementation (limits table with research-doc citations, Spanish messages).
"""

from __future__ import annotations

from planificador.providers.base import FormatSpec, Issue, TargetSpec

#: network -> formats. TODO(rules agent): fill from SPEC §7.
FORMATS: dict[str, list[FormatSpec]] = {}


def validate_target(target: TargetSpec) -> list[Issue]:
    raise NotImplementedError("rules.validate_target")


def default_options(network: str, format: str) -> dict:
    raise NotImplementedError("rules.default_options")


def char_limit(network: str, format: str) -> int:
    raise NotImplementedError("rules.char_limit")
