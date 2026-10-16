"""Spreadsheet-safe CSV cells (CSV/formula injection), shared by every CSV export.

Excel and LibreOffice run a cell that starts with ``=``, ``+``, ``-`` or ``@`` as a formula (``- hola`` and
``+info en bio`` included, which then show ``#NAME?``), and a tab/CR prefix can smuggle one too. Such text
gets a leading apostrophe, which spreadsheets hide and treat as "this is text". :func:`unprotect_cell` undoes
it on re-import.
"""

from __future__ import annotations

FORMULA_PREFIXES = "=+-@\t\r"


def protect_cell(value: str) -> str:
    """Prefix text that a spreadsheet would evaluate as a formula. A lone ``-`` stays as is."""
    if value and value[0] in FORMULA_PREFIXES and not (value == "-"):
        return "'" + value
    return value


def unprotect_cell(text: str) -> str:
    """Inverse of :func:`protect_cell` for already stripped text (``'=1`` → ``=1``)."""
    if len(text) > 1 and text[0] == "'" and text[1] in "=+-@":
        return text[1:]
    return text
