"""ADR-0014 / ADR-0017 profile discipline tests.

The sealed reference arms must stay byte-identical to v0.2: no cognitive
flags, and only the active-v2 candidate (ADR-0017) carries hygiene flags.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resident.profiles import REGISTRY, by_name


def test_registry_contains_active_v2():
    assert "active-v2" in REGISTRY


def test_sealed_arms_have_no_cognitive_flags():
    for name in ("sealed-baseline", "active-v1", "dense-debug"):
        assert by_name(name).cognitive_flags == {}, name


def test_active_v2_hygiene_flags_default_on():
    p = by_name("active-v2")
    assert p.cognitive_flags == {
        "recall_pool_hygiene": True,
        "thread_selection": "material",
        "context_exclude_prev_thread": True,
    }


def test_active_v2_rhythm_equals_active_v1():
    """active-v2 changes hygiene only — the opportunity rails stay at the
    active-v1 values so the battery isolates the hygiene variable."""
    v1, v2 = by_name("active-v1"), by_name("active-v2")
    for field in ("active_wake_interval_s", "quiet_wake_interval_s",
                  "drowsy_wake_interval_s", "world_cooldown_s",
                  "world_max_light_per_day", "world_reobservation_days",
                  "beat_interval_s"):
        assert getattr(v1, field) == getattr(v2, field), field


def test_unknown_profile_fails_loudly():
    try:
        by_name("no-such-profile")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown profile must fail loudly")
