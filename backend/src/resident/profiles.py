"""Living profiles (Active Living Experiment, ADR-0014).

A :class:`LivingProfile` tunes **opportunity density only** — how often the
mind is *offered* a wake and how often the world is *offered* to the window.
It must never touch a cognitive criterion: thought formation, question rolls,
revival probability, retrieval ranking, similarity thresholds, self-origin
weights, and every importance/curiosity-style score stay exactly as sealed.
The governing principle (owner, verbatim): **增加机会，不增加欲望**.

The three registered tiers:

- ``sealed-baseline`` — the sealed values, byte-equal to the v0.2 arms. The
  scientific reference; never a product target.
- ``active-v1`` — the candidate living rhythm (the owner's suggested middle
  range). If the experiment validates it, this becomes the daily profile.
- ``dense-debug`` — a dynamics stress test only: high enough to surface echo
  storms / question spam / recency collapse within hours of simulated life.
  Never a long-term personality.
- ``active-v2`` — the ADR-0017 candidate: the active-v1 rhythm plus
  retrieval hygiene default-ON (see ``ACTIVE_V2.cognitive_flags``); the only
  profile carrying cognitive flags, and prospective until promoted live.

Application sites: :meth:`circadian_params` for the orchestrator's cadence,
:meth:`world_params` for the window's opportunity rails (cooldown, daily cap,
and the re-observation horizon — a *world-side* affordance: real pages change
and are re-checked; the sealed default (never re-observe) is preserved by a
huge sentinel so sealed arms stay byte-identical).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from .circadian import CircadianParams
from .world import WORLD_ALL, WorldParams

#: sentinel meaning "never re-observe" (the sealed behaviour)
NEVER = 1e18


@dataclass(frozen=True)
class LivingProfile:
    name: str
    notes: str
    # circadian wake cadence (opportunity)
    active_wake_interval_s: float
    quiet_wake_interval_s: float
    drowsy_wake_interval_s: float
    # world window opportunity rails
    world_cooldown_s: float
    world_max_light_per_day: int
    # after this many days, an observed item may re-enter the frontier
    world_reobservation_days: float = NEVER
    # live-app beat rate (the harness sets its own; beats are the offer rate)
    beat_interval_s: float = 60.0
    # ADR-0017: only the active-v2 candidate carries cognitive (hygiene)
    # flags; every other profile leaves this empty, so the ADR-0014 guarantee
    # (profiles tune opportunity only) holds for the sealed reference arms.
    # MindLoop/SleepEngine defaults stay False, so flag-off behaviour
    # (sealed-baseline, active-v1, dense-debug) is byte-identical to v0.2.
    cognitive_flags: dict = dataclasses.field(default_factory=dict)


SEALED_BASELINE = LivingProfile(
    name="sealed-baseline",
    notes="The v0.2 sealed values. Reference arm; byte-equal to the sealed sweeps.",
    active_wake_interval_s=1 * 3600.0,
    quiet_wake_interval_s=3 * 3600.0,
    drowsy_wake_interval_s=4 * 3600.0,
    world_cooldown_s=2 * 3600.0,
    world_max_light_per_day=8,
    world_reobservation_days=NEVER,
    beat_interval_s=60.0,
)

ACTIVE_V1 = LivingProfile(
    name="active-v1",
    notes="Candidate living rhythm — the owner's suggested middle range.",
    active_wake_interval_s=20 * 60.0,
    quiet_wake_interval_s=60 * 60.0,
    drowsy_wake_interval_s=90 * 60.0,
    world_cooldown_s=45 * 60.0,
    world_max_light_per_day=14,
    world_reobservation_days=2.0,
    beat_interval_s=60.0,
)

DENSE_DEBUG = LivingProfile(
    name="dense-debug",
    notes="Dynamics stress test only — surfaces failure modes in hours of "
          "simulated life; never a long-term personality.",
    active_wake_interval_s=8 * 60.0,
    quiet_wake_interval_s=20 * 60.0,
    drowsy_wake_interval_s=40 * 60.0,
    world_cooldown_s=15 * 60.0,
    world_max_light_per_day=28,
    world_reobservation_days=1.0,
    beat_interval_s=60.0,
)

#: ADR-0017 — the active-v2 candidate: the active-v1 rhythm (identical
#: opportunity rails) plus retrieval hygiene default-ON:
#: - ``recall_pool_hygiene`` (E2): exclude P1 route-meta stubs and P3
#:   echo-copies from the RECALL pool only (the event log stays complete);
#:   the E0 counterfactual showed meta-recall pollution is ~100% supply-side.
#: - ``thread_selection: material`` + ``context_exclude_prev_thread`` (VA):
#:   the only selection fix across rounds 5-12 that never starved or migrated;
#:   kept as the accompanying selection rule.
#: No creation-side filter (E2T starves self life — round 11), no dormant
#: change, no bootstrap knob. Prospective until the live promotion gate.
ACTIVE_V2 = LivingProfile(
    name="active-v2",
    notes="Candidate active profile (ADR-0017): active-v1 rhythm + recall-pool "
          "hygiene (E2) + material selection with context exclusion (VA).",
    active_wake_interval_s=20 * 60.0,
    quiet_wake_interval_s=60 * 60.0,
    drowsy_wake_interval_s=90 * 60.0,
    world_cooldown_s=45 * 60.0,
    world_max_light_per_day=14,
    world_reobservation_days=2.0,
    beat_interval_s=60.0,
    cognitive_flags={
        "recall_pool_hygiene": True,
        "thread_selection": "material",
        "context_exclude_prev_thread": True,
    },
)

#: v1.0 (ADR-0018): the ``active-v3`` candidate — the active-v2 rhythm and
#: cognitive flags, plus the **Agency** (the bounded action layer). This is the
#: FIRST profile to carry ``agency``; in every other profile the flag is absent
#: (or False), so the life loop, memory, and world are byte-identical and the
#: Agency (its service + endpoints) is not attached. ``agency`` is NOT a
#: MindLoop cognitive flag — it is consumed by the runtime wiring to decide
#: whether to mount the Agency (see main.py), never passed to the MindLoop.
ACTIVE_V3 = LivingProfile(
    name="active-v3",
    notes="v1.0 candidate (ADR-0018): active-v2 rhythm + retrieval hygiene "
          "(ADR-0017) + the Agency (bounded action layer, ADR-0018/0019/0020). "
          "First profile with agency=True.",
    active_wake_interval_s=20 * 60.0,
    quiet_wake_interval_s=60 * 60.0,
    drowsy_wake_interval_s=90 * 60.0,
    world_cooldown_s=45 * 60.0,
    world_max_light_per_day=14,
    world_reobservation_days=2.0,
    beat_interval_s=60.0,
    cognitive_flags={
        "recall_pool_hygiene": True,
        "thread_selection": "material",
        "context_exclude_prev_thread": True,
        "agency": True,
    },
)

REGISTRY: dict[str, LivingProfile] = {
    p.name: p for p in (SEALED_BASELINE, ACTIVE_V1, DENSE_DEBUG, ACTIVE_V2, ACTIVE_V3)
}


def by_name(name: str) -> LivingProfile:
    """Resolve a profile by name; unknown names fail loudly (config drift
    must never silently fall back to another tier)."""
    key = (name or "").strip().lower()
    if key not in REGISTRY:
        raise ValueError(f"unknown living profile {name!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[key]


def circadian_params(p: LivingProfile) -> CircadianParams:
    """Profiled wake cadence. Every cognitive threshold (silence descent,
    night bias, wake density, sleep mechanics) stays exactly at the sealed
    default — only the per-state cadence is opportunity."""
    return CircadianParams(
        active_wake_interval_s=p.active_wake_interval_s,
        quiet_wake_interval_s=p.quiet_wake_interval_s,
        drowsy_wake_interval_s=p.drowsy_wake_interval_s,
    )


def world_params(p: LivingProfile, *, base: WorldParams | None = None) -> WorldParams:
    """Profiled world opportunity rails. Cognitive parameters (probabilities,
    thresholds, budgets that shape *desire*) are preserved from the base."""
    return dataclasses.replace(
        base if base is not None else WORLD_ALL,
        cooldown_s=p.world_cooldown_s,
        max_light_per_day=p.world_max_light_per_day,
        reobservation_days=p.world_reobservation_days,
    )
