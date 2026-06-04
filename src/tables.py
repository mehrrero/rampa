"""Per-city table naming, shared by the initialize script and the API.

`scripts/initialize.py` writes one ``nodes_<city>`` / ``edges_<city>`` table
pair per city into the DuckDB; the API's ``Network`` reads them back. Both
sides must derive the same table names from a city name, so the mapping lives
here in a dependency-light module (no pandana / osmnx imports) that either side
can import cheaply.
"""

from __future__ import annotations

import re


def slugify_city(name: str) -> str:
    """City name → safe SQL identifier fragment (lowercase, ``[0-9a-z_]``).

    Folds anything that isn't an ASCII alphanumeric into single underscores and
    trims leading/trailing ones, so ``"Castelló de la Plana"`` →
    ``"castell_de_la_plana"``. Raises if nothing usable remains.
    """
    slug = re.sub(r"[^0-9a-z]+", "_", str(name).strip().lower()).strip("_")
    if not slug:
        raise ValueError(f"city name {name!r} has no usable characters for a table name")
    return slug


def city_tables(name: str) -> tuple[str, str]:
    """Return the ``(nodes_table, edges_table)`` pair for a city name."""
    slug = slugify_city(name)
    return f"nodes_{slug}", f"edges_{slug}"
