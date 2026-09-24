"""The Agency — Resident's bounded action layer (ADR-0018 / 0019 / 0020).

A peripheral organ, not a Mind route: an intent is *given* to
:class:`AgencyService`, judged by the deterministic :class:`ActionPolicy`,
executed by the isolated :class:`PyClawRunner` (the ``py-claw`` subprocess),
and written back as objective ``intent.*`` / ``action.*`` events. The Mind never
grants itself permission; it can only *propose*, which the policy decides on.
"""
from __future__ import annotations

from .policy import (
    DEFAULT_ALLOWED_CAPABILITIES,
    DEFAULT_TIMEOUT_S,
    KNOWN_CAPABILITIES,
    ActionPolicy,
    PolicyDecision,
)
from .runner import PyClawRunner, RunResult
from .service import AgencyService

__all__ = [
    "ActionPolicy",
    "PolicyDecision",
    "PyClawRunner",
    "RunResult",
    "AgencyService",
    "KNOWN_CAPABILITIES",
    "DEFAULT_ALLOWED_CAPABILITIES",
    "DEFAULT_TIMEOUT_S",
]
