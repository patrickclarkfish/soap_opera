"""Ordered registry of migration steps.

Each entry maps a target schema version to the function that migrates the
database from (version - 1) to version. The runner in ../database.py applies
these strictly in order, one at a time, never jumping ahead. Add new
migrations by adding a new module and a new entry here -- never edit an
existing entry's function once it has shipped.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.engine import Connection

from . import m001_initial, m002_medication_regimen

MIGRATIONS: dict[int, Callable[[Connection], None]] = {
    1: m001_initial.apply,
    2: m002_medication_regimen.apply,
}

SCHEMA_VERSION = max(MIGRATIONS)
