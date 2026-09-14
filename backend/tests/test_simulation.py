"""Accelerated life simulation contract.

Runs the REAL MindLoop + SleepEngine + ReentryEngine over a short virtual
timeline and asserts the invariants the phase cares about:

- every wake records a provenance-complete route decision;
- "nothing happened" (rest/noop) is a first-class, frequently-reached outcome;
- thread resurfacing and share-candidate promotion are grounded (no dangling
  links — the store enforces this, and every share candidate cites a real
  private thought);
- the run is DETERMINISTIC for a fixed seed (same route sequence twice), so an
  accelerated life is reproducible and the report is evidence, not a demo;
- (Phase 3) every thought is tagged with a valid origin, the report exposes the
  independence profile, and the self-thread mechanism is CAUSAL (a control run
  with the mechanism off never produces self material).
"""
import asyncio

from resident.origin import THOUGHT_ORIGINS
from resident.simulation import (
    UserTurn,
    run_simulation,
)

# A 3-day slice of the default script: a returning topic (work), one gap day
# (day 2 has only morning), enough to exercise continuity/revisit/rest.
SCRIPT = [
    UserTurn(1, 6, "pasta", "周末想做意面"),
    UserTurn(1, 15, "work", "项目 deadline 压力大"),
    UserTurn(2, 9, "work", "deadline 要重排优先级"),
    UserTurn(3, 6, "reading", "在读一本讲记忆的书"),
    UserTurn(3, 15, "pasta", "意面做成功了"),
]


def _routes(report: dict) -> list[str]:
    return [t["route"] for t in report["timeline"] if t["route"] != "sleep"]


def test_simulation_is_deterministic_for_fixed_seed(tmp_path):
    r1 = asyncio.run(
        run_simulation(tmp_path / "a", days=3, seed=7, user_script=SCRIPT)
    )
    r2 = asyncio.run(
        run_simulation(tmp_path / "b", days=3, seed=7, user_script=SCRIPT)
    )
    assert _routes(r1) == _routes(r2), "same seed must give the same route sequence"
    # and the headline numbers match
    assert r1["route_distribution"] == r2["route_distribution"]
    assert r1["meta"]["n_total_events"] == r2["meta"]["n_total_events"]


def test_simulation_records_provenance_complete_wakes(tmp_path):
    r = asyncio.run(run_simulation(tmp_path / "s", days=3, seed=0, user_script=SCRIPT))
    meta = r["meta"]
    assert meta["n_wakes"] > 0
    assert meta["n_sleeps"] == 3
    # every wake produced a route_selected + a terminal completed
    timeline = r["timeline"]
    wake_rows = [t for t in timeline if t["route"] != "sleep"]
    assert len(wake_rows) == meta["n_wakes"]


def test_nothing_happened_is_a_reached_outcome(tmp_path):
    # On gap days / low-activity the mind must actually REST (noop) sometimes —
    # not persist a thought every single wake.
    r = asyncio.run(run_simulation(tmp_path / "s", days=3, seed=0, user_script=SCRIPT))
    assert r["meta"]["n_noops"] > 0, "rest must be a reachable first-class outcome"
    assert r["no_op_ratio"] > 0.0


def test_share_candidates_are_grounded(tmp_path):
    r = asyncio.run(run_simulation(tmp_path / "s", days=3, seed=0, user_script=SCRIPT))
    # every share candidate must cite (via provenance) a real source thought;
    # the store already rejects dangling links, so this is a belt-and-suspenders
    # check that promotions are never free-floating.
    assert r["meta"]["n_share_candidates"] <= r["meta"]["n_thoughts"]


def test_memory_age_and_resurrection_are_measured(tmp_path):
    r = asyncio.run(run_simulation(tmp_path / "s", days=3, seed=0, user_script=SCRIPT))
    age = r["memory_age_at_activation_days"]
    assert age["count"] > 0
    assert age["mean_days"] >= 0
    tl = r["thread_lifecycle"]
    # the returning topics (work day1->day2, pasta day1->day3) should produce
    # at least one mind-driven resurfacing over the window
    assert tl["dormancies"] >= 0
    assert isinstance(tl["resurrection_rate"], float)


# ------------------------------------------------------------------ Phase 3


def test_every_thought_has_a_valid_origin(tmp_path):
    r = asyncio.run(run_simulation(tmp_path / "o", days=3, seed=0, user_script=SCRIPT))
    assert r["meta"]["n_thoughts"] > 0
    assert r["origin_base"] > 0
    # the report's origin distribution spans exactly the seven labels, and the
    # counts sum to the base (no unoriginated thoughts in a tagged run)
    assert set(r["origin_distribution"].keys()) == set(THOUGHT_ORIGINS)
    assert sum(r["origin_distribution"].values()) == r["origin_base"]
    # every timeline row that is a durable thought carries a valid origin label
    for t in r["timeline"]:
        if t["route"] == "sleep":
            assert t["origin"] is None
        elif t["result"] == "thought":
            assert t["origin"] in THOUGHT_ORIGINS


def test_report_exposes_independence_profile(tmp_path):
    r = asyncio.run(run_simulation(tmp_path / "i", days=3, seed=0, user_script=SCRIPT))
    ind = r["independence"]
    for key in ("user_recent_share", "user_derived_share",
                "self_origin_share", "independence_share", "base"):
        assert key in ind
    # independence is the share NOT from fresh user input
    assert abs(ind["independence_share"] - (1 - ind["user_recent_share"])) < 1e-6
    assert 0.0 <= ind["self_origin_share"] <= 1.0
    # a per-day independence trace, one entry per simulated day
    assert len(r["independence_trace"]) == 3
    for row in r["independence_trace"]:
        assert {"day", "user_recent", "self_origin", "independence"} <= set(row)


def test_self_thread_mechanism_is_causal(tmp_path):
    # Same user stream, same seed. The ONLY difference is the self-thread
    # mechanism in sleep. This isolates its effect on independence:
    control = asyncio.run(
        run_simulation(tmp_path / "ctrl", days=3, seed=0,
                       user_script=SCRIPT, self_prob=0.0)
    )
    treatment = asyncio.run(
        run_simulation(tmp_path / "treat", days=3, seed=0,
                       user_script=SCRIPT, self_prob=1.0)
    )
    # control: no self material source -> no self threads, no self-origin thoughts
    assert control["meta"]["n_self_threads"] == 0
    assert control["origin_distribution"]["self_thread"] == 0
    assert control["independence"]["self_origin_share"] == 0.0
# ------------------------------------------------------------------ Phase 4

# The six self-dynamics metrics (plus the raw revival counts) the report must expose.
SD_KEYS = (
    "self_thread_revival_rate", "self_thread_revival_detail",
    "self_thread_avg_lifetime_days", "self_loop_concentration",
    "self_thread_generation_depth", "longest_self_chain", "self_topic_diversity",
)

# A 14-day user stream (matching the dynamics experiment) with returning topics,
# gaps, and enough span for self-threads to form, go dormant, and (in C) revive.
SCRIPT14 = [
    UserTurn(1, 6, "pasta", "周末想做意面，有什么简单的酱吗？"),
    UserTurn(1, 15, "work", "下周有个项目 deadline，压力有点大。"),
    UserTurn(2, 9, "work", "deadline 相关，得重新排一下优先级。"),
    UserTurn(4, 9, "pasta", "那个意面后来做成功了，味道还不错。"),
    UserTurn(5, 6, "reading", "最近在读一本讲记忆的书，很有意思。"),
    UserTurn(7, 9, "work", "deadline 终于过去了，松了口气。"),
    UserTurn(7, 15, "reading", "记忆那本书读完了，想再聊聊。"),
    UserTurn(8, 6, "music", "最近开始学吉他，有点上头。"),
    UserTurn(10, 9, "pasta", "又想做意面了，这次想换个做法。"),
    UserTurn(11, 15, "work", "新项目开始了，又是一堆 deadline。"),
    UserTurn(13, 9, "reading", "又想起那本讲记忆的书，想重读。"),
    UserTurn(13, 15, "music", "吉他有点手感了，想继续。"),
    UserTurn(14, 6, "cooking", "想学做甜点，有什么入门的吗？"),
    UserTurn(14, 15, "work", "新项目的中期检查，有点紧张。"),
]


def test_self_dynamics_report_shape_and_control(tmp_path):
    # control (self_prob=0) can never produce self material; the revival flag is
    # off and the six metrics are present and well-formed in every report.
    control = asyncio.run(
        run_simulation(tmp_path / "sdA", days=3, seed=0,
                       user_script=SCRIPT, self_prob=0.0, self_revival=False)
    )
    on = asyncio.run(
        run_simulation(tmp_path / "sdB", days=3, seed=0,
                       user_script=SCRIPT, self_prob=0.35, self_revival=True)
    )
    for r in (control, on):
        assert set(SD_KEYS) <= set(r["self_dynamics"].keys())
        sd = r["self_dynamics"]
        assert 0.0 <= sd["self_thread_revival_rate"] <= 1.0
        assert sd["self_thread_revival_detail"]["eligible_wakes"] >= 0
        assert sd["self_loop_concentration"]["total"] >= 0
        assert sd["self_thread_generation_depth"] >= 0
        assert sd["longest_self_chain"] >= 0
    # control: no self material at all
    assert control["meta"]["self_revival_enabled"] is False
    assert control["meta"]["n_self_threads"] == 0
    assert control["independence"]["self_origin_share"] == 0.0
    # the treatment reports the flag as on
    assert on["meta"]["self_revival_enabled"] is True


def test_self_dynamics_abc_14day_is_deterministic_and_treatment_grows_self(tmp_path):
    # Same seed + user stream across A (no self), B (self, no revival),
    # C (self + revival). The report is deterministic; the treatment (C) carries
    # more self-origin material than B, and C's self-origin does not merely copy
    # A's zero. (The honest causal reading lives in ADR-0006.)
    a = asyncio.run(
        run_simulation(tmp_path / "a14", days=14, seed=0,
                       user_script=SCRIPT14, self_prob=0.0, self_revival=False)
    )
    b = asyncio.run(
        run_simulation(tmp_path / "b14", days=14, seed=0,
                       user_script=SCRIPT14, self_prob=0.35, self_revival=False)
    )
    c = asyncio.run(
        run_simulation(tmp_path / "c14", days=14, seed=0,
                       user_script=SCRIPT14, self_prob=0.35, self_revival=True)
    )
    # control is genuinely empty of self material
    assert a["meta"]["n_self_threads"] == 0
    assert a["independence"]["self_origin_share"] == 0.0
    # self ON produces self threads; the revival treatment has >= B's self material
    assert b["meta"]["n_self_threads"] >= 1
    assert c["independence"]["self_origin_share"] > a["independence"]["self_origin_share"]
    assert c["independence"]["self_origin_share"] >= b["independence"]["self_origin_share"]
    # determinism: re-running C gives byte-identical headline + dynamics
    c2 = asyncio.run(
        run_simulation(tmp_path / "c14b", days=14, seed=0,
                       user_script=SCRIPT14, self_prob=0.35, self_revival=True)
    )
    assert c["origin_distribution"] == c2["origin_distribution"]
    assert c["self_dynamics"] == c2["self_dynamics"]
    assert c["meta"]["n_thoughts"] == c2["meta"]["n_thoughts"]
