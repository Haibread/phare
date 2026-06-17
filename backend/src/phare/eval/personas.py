"""Synthetic personas with known taste — the persona guardrail suite.

Each persona watches a few catalog titles (to seed a taste centroid) and carries an explicit
taste profile (so the suite needs no LLM). The assertions are deterministic regression catchers:
hard-avoids must never surface, watched titles must never be recommended.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# tmdb_ids reference the offline sample catalog (see catalog/sample.py).
_HEREDITARY, _GET_OUT = 493922, 419430
_GODFATHER, _THERE_WILL_BE_BLOOD = 238, 8967
_GRAND_BUDAPEST, _SUPERBAD = 120467, 8363
_ARRIVAL, _EX_MACHINA, _INTERSTELLAR = 329865, 264660, 157336


@dataclass(frozen=True)
class Persona:
    """A known-taste viewer: watch history + explicit taste + forbidden genres."""

    name: str
    watched: list[tuple[int, float]]  # (tmdb_id, rating out of 10)
    taste: dict[str, Any] = field(default_factory=dict)
    forbidden_genres: tuple[str, ...] = ()


PERSONAS: list[Persona] = [
    Persona(
        name="horror-fan",
        watched=[(_HEREDITARY, 9.0), (_GET_OUT, 8.0)],
        taste={"affinities": {"Horror": 0.9}, "likes": ["horror"], "confidence": 0.7},
    ),
    Persona(
        name="gore-avoider",
        watched=[(_GODFATHER, 9.0), (_THERE_WILL_BE_BLOOD, 8.0)],
        taste={"affinities": {"Drama": 0.8}, "hard_avoids": ["Horror"], "confidence": 0.7},
        forbidden_genres=("Horror",),
    ),
    Persona(
        name="comedy-only",
        watched=[(_GRAND_BUDAPEST, 9.0), (_SUPERBAD, 8.0)],
        taste={"affinities": {"Comedy": 0.9}, "hard_avoids": ["Horror"], "confidence": 0.6},
        forbidden_genres=("Horror",),
    ),
    Persona(
        name="scifi-fan",
        watched=[(_ARRIVAL, 9.0), (_EX_MACHINA, 8.0), (_INTERSTELLAR, 8.0)],
        taste={"affinities": {"Science Fiction": 0.9}, "confidence": 0.7},
    ),
]
