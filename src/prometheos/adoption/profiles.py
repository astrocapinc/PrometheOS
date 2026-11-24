"""Adoption profiles: the declarative part of the analyzer pack (A05, CP17; R4 sections 3.2 and
3.6; review cp17-234-a F14).

A profile is data (``AdoptionProfile``): words a research group uses for a file's status and
the tokens it puts in versioned file names. Two declarative analyzers apply it:
``profile.status_terms`` reads the profile's status words in notes exactly as the builtin
statement reader reads its own (whole words, direction, negation), and
``profile.version_tokens`` strips the profile's tokens from names so that ``sweep_run12`` and
``sweep_run13`` share a stem. Profiles ship with the release here; they never live in the adopted
folder. A scan names its profile (``ScanConfig.profile``); an unknown name is refused.
"""

from __future__ import annotations

import re

from prometheos.adoption.contracts import AdoptionProfile, Vocabulary
from prometheos.domain.models import PrometheosError

RANKING = ["R1", "R2", "R3", "R4", "R5", "R7", "R8"]

BUILTIN: dict[str, AdoptionProfile] = {
    p.id: p
    for p in (
        AdoptionProfile(
            id="generic",
            version="1.0.0",
            extends=None,
            summary="Any research folder: the builtin vocabulary only",
            vocabulary=Vocabulary(),
            ranking=RANKING,  # type: ignore[arg-type]
        ),
        AdoptionProfile(
            id="matlab-research",
            version="1.0.0",
            extends="generic",
            summary="MATLAB and Simulink research folders: run numbers and frozen results",
            vocabulary=Vocabulary(
                status_terms={"frozen": "final_claimed", "broken": "failed"},
                version_tokens=["run#", "iter#", "trial#"],
            ),
            ranking=RANKING,  # type: ignore[arg-type]
        ),
    )
}


def get(profile_id: str) -> AdoptionProfile:
    profile = BUILTIN.get(profile_id)
    if profile is None:
        raise PrometheosError(
            "validation_error",
            "unknown adoption profile",
            {"profile": profile_id[:64], "known": sorted(BUILTIN)},
        )
    return profile


def token_patterns(profile: AdoptionProfile | None) -> list[re.Pattern[str]]:
    """Each version token as a pattern for the end of a name stem (``#`` is a number)."""
    if profile is None:
        return []
    out = []
    for token in profile.vocabulary.version_tokens:
        body = re.escape(token).replace(r"\#", r"\d{1,9}").replace("#", r"\d{1,9}")
        out.append(re.compile(rf"[_\- ]?{body}$"))
    return out
