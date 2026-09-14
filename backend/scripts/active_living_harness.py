#!/usr/bin/env python3
"""Accelerated Living Harness (ADR-0014) — compare opportunity tiers in
simulated days, not real weeks.

Design invariants (owner directive):
- TIME REALLY PASSES: a VirtualClock drives every engine, so dormancy,
  absence, cooldown, circadian cadence, revival and thread-age all read
  *simulated* time. Higher tiers are NOT "call MindLoop more often per
  second" — they are the same mind given more *offers* per simulated day.
- Independent stores per run; the real resident_home is never touched.
- Replay world source over the REAL captured snapshots (deterministic,
  network-free); the re-observation horizon comes from the profile.
- Deterministic fake provider: cognitive criteria identical across arms —
  only opportunity density varies. (The real model keeps living in real
  time separately; this harness isolates density from model variance.)
- Every run is kept: out/runs/<arm>/<fingerprint>/seed<N>/ — no cherry-
  picking, every parameter iteration leaves its results on disk.
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import statistics
import time
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resident.circadian import CircadianOrchestrator, CircadianStateMachine  # noqa: E402
from resident.continuity import ContinuityEngine  # noqa: E402
from resident.event_store import EventStore  # noqa: E402
from resident.memory import MemoryRetrieval  # noqa: E402
from resident.mind_loop import MindLoop, _META_PREFIXES  # noqa: E402
META_PREFIXES = tuple(_META_PREFIXES)
from resident.profiles import REGISTRY, circadian_params, world_params  # noqa: E402
from resident.providers import AnthropicMessagesProvider  # noqa: E402
from resident.reentry import ReentryEngine  # noqa: E402
from resident.self_revival import RevivalParams  # noqa: E402
from resident.semantic import make_semantic_stack  # noqa: E402
from resident.simulation import (  # noqa: E402
    DEFAULT_USER_SCRIPT, SIM_START, VirtualClock, _inject_turn,
)
from resident.sleep import SleepEngine  # noqa: E402
from resident.world import (  # noqa: E402
    WORLD_ALL, WorldWindowEngine, question_state, world_profile,
)
from resident.world_source import (  # noqa: E402
    ReplayWorldSource, SeedEntry, WorldSnapshotStore,
)

REPO = Path(__file__).resolve().parents[2]
SNAPSHOTS = Path(__import__('os').environ.get(
    'HARNESS_SNAPSHOTS', str(REPO / 'resident_home' / 'world_snapshots')))
SEEDS = Path(__file__).resolve().parents[1] / "seeds" / "world_seeds.json"

#: Active Living revival variants (ADR-0015). Every variant uses the
#: active-v1 profile; S1/S2 additionally time-anchor the consolidation pool
#: (the creation layer broke at density — see the localization in the report).
REVIVAL_VARIANTS: dict[str, dict] = {
    "S0": {},
    "S1": {"gate_mode": "dormancy"},
    "S2-12h": {"gate_mode": "dormancy", "opportunity_clock_s": 12 * 3600.0},
    "S2-18h": {"gate_mode": "dormancy", "opportunity_clock_s": 18 * 3600.0},
    "S2-24h": {"gate_mode": "dormancy", "opportunity_clock_s": 24 * 3600.0},
    # round 2 (self-life diversity): S3 material-grounded thread selection;
    # S3S4 adds the question↔self-thread material bridge
    "S3": {"thread_selection": "material"},
    "S3S4": {"thread_selection": "material", "question_thread_bridge": True},
    # round 3: temporary yielding (owner-sanctioned refractory) + the bridge
    # carries the question id in its provenance (one-hop citable chain)
    "S3R": {"thread_selection": "material", "thread_yield_s": 12 * 3600.0},
    "S3S4R": {"thread_selection": "material", "thread_yield_s": 12 * 3600.0,
              "question_thread_bridge": True},
    # round 5 (self_thread_concentration localization): selection-rule
    # counterfactuals against the seed1 monopoly (S3R baseline 0.845).
    # VA context-exclusion: the thread continued in the immediately previous
    #   continuity wake cannot be selected again (breaks the soft feedback
    #   loop where it wrote the present context the rule reads).
    # VB dormant-material: the revisit route picks dormant threads by the
    #   same material-match rule as active selection (not most-recent).
    # VC substantive-context: the present context for selection is only
    #   world/question/user material — self-written thread-continuation
    #   thoughts are excluded from the context read.
    "VA": {"thread_selection": "material", "context_exclude_prev_thread": True},
    "VB": {"thread_selection": "material", "dormant_selection": "material"},
    "VC": {"thread_selection": "material", "substantive_context": True},
    # round 6 (self_thread_concentration): VD thread-focus hygiene — the same
    #   soft self-feedback loop as VC, attacked on the THREAD side: a thread's
    #   matched/recall text is computed only from its substantive events (no
    #   title fallback; titles can be built from route-meta memory text).
    #   Present-context computation stays as in VA (no context stripping).
    "VD": {"thread_selection": "material", "context_exclude_prev_thread": True,
           "thread_query_substantive_only": True},
    # round 7 (self_thread_concentration): VE creation-side hygiene + revisit
    #   material. The self-thread title is built from memory text with NO
    #   meta-prefix filter in sleep.py `_best_cross_topic_pair`, so titles like
    #   「[continuity] 继」与「[continuity] 继」之间 are meta-text. VE filters
    #   the creation pool to substantive text (thread_title_meta_filter),
    #   keeps VA's context-exclusion (proven safe), and retests VB's dormant
    #   material-match — previously refuted SOLELY because the embedded titles
    #   were meta-text. VD's thread_query_substantive_only stays OFF (refuted:
    #   recall query degradation).
    "VE": {"thread_selection": "material", "context_exclude_prev_thread": True,
           "dormant_selection": "material", "thread_title_meta_filter": True},
    # round 7 diagnostic (post-hoc localization, declared AFTER the VE seed1
    # FAIL): VE-CF = VA + creation filter ONLY (no dormant material). Isolates
    # whether the seed1 creation collapse (1 self thread vs VA's 4) comes from
    # the creation-side filter itself or from its interaction with dormant
    # material selection.
    "VECF": {"thread_selection": "material", "context_exclude_prev_thread": True,
             "thread_title_meta_filter": True},
    # round 8 (self_thread_concentration): VE2 = the unified hypothesis from
    # E0 + rounds 5-7. Concentration monopolies are downstream of meta fuel:
    # ~90% of the recall pool text is route-meta (E0), so selection rules
    # starve. E2 recall-pool hygiene (recall_pool_hygiene) removes P1
    # route-meta stubs and P3 echo-copies from the RECALL POOL for every
    # route (retrieval-only; the event log stays complete), on top of VE's
    # proven combination (material selection + context exclusion + dormant
    # material + title filter). E0's caution: enrichment without hygiene
    # RAISES echo share — hygiene first, then selection.
    "VE2": {"thread_selection": "material", "context_exclude_prev_thread": True,
            "dormant_selection": "material", "thread_title_meta_filter": True,
            "recall_pool_hygiene": True},
    # round 8 isolation arm: E2 hygiene ONLY (no selection-rule changes) on
    # the sealed baseline, so VE2's combined effect is interpretable as
    # hygiene + selection rather than hygiene alone.
    "E2": {"recall_pool_hygiene": True},
    # round 11 (self_thread_concentration, opportunity path): E2T = E2 +
    # creation-side title hygiene ONLY (the sleep.py predicate-before-cap fix
    # from round 9). NO dormant material change, NO context exclusion, NO
    # bootstrap knob. Hypothesis: with clean recall (E2) and the multi-source
    # replay world, substantive self threads grow organically over 28 days
    # and dilute the stub monopoly — no selection surgery.
    "E2T": {"recall_pool_hygiene": True, "thread_title_meta_filter": True},
    # round 9 (self_thread_concentration): VE3 = VE2's flag set (E2 recall-pool
    # hygiene + VA material selection + context exclusion + VB dormant
    # material + creation-side title filter) with NO selection-side exclusion
    # (there is none in code — the filter reaches only SleepEngine). The
    # differentiator vs VE2 is the round-9 sleep.py fix localized in
    # out/conc_round9/diagnosis.md: the title filter's predicate is applied to
    # the 2.5d creation pool BEFORE the [:400] bounded slice, so the sparse
    # substantive text (experience summaries) is no longer evicted by the
    # dense route-meta stubs before the pair function ever sees it. Same
    # predicate, same events, no thresholds/quotas/selection changes.
    "VE3": {"thread_selection": "material", "context_exclude_prev_thread": True,
            "dormant_selection": "material", "thread_title_meta_filter": True,
            "recall_pool_hygiene": True},
    # round 10 (self_thread_concentration): VE4 = VE3 + zero_history_bootstrap
    # (see out/conc_round9/diagnosis.md + out/conc_round10/note_round10.md).
    # VE3 fixed creation (4 substantive threads) but a newborn self thread has
    # zero _HISTORY_TYPES events, so _has_real_history (MIN_SELF_EVENTS=1)
    # blocks revival forever and VB material selection never picks it — a
    # second-gate cold-start deadlock. zero_history_bootstrap: while any
    # dormant self thread has zero real history, the revisit pick uses the
    # sealed recency fallback (_most_recent) — the accidental E2 bootstrap —
    # so the newborn gets its FIRST revisit and then competes on material like
    # everything else. Opportunity rule, not a quota; default False.
    "VE4": {"thread_selection": "material", "context_exclude_prev_thread": True,
            "dormant_selection": "material", "thread_title_meta_filter": True,
            "recall_pool_hygiene": True, "zero_history_bootstrap": True},
    # round 12 (self_thread_concentration, final selection-rule combination):
    # E2VA = E2 recall-pool hygiene (clean fuel, proven volume-safe at 14d,
    # stub-monopoly-amplifying at 28d multisrc) + VA context exclusion (the
    # ONLY selection fix across rounds 5-10 that never starved or migrated:
    # 14d seed1 0.845→0.667, volume held 27-63, no flags). Never tested
    # combined, never tested at 28d multisrc. No creation-side filter
    # (E2T starves), no dormant change, no bootstrap knob.
    "E2VA": {"thread_selection": "material", "context_exclude_prev_thread": True,
             "recall_pool_hygiene": True},
    # round 13 (self_thread_concentration, REAL-MODEL regime): R13 = the
    # active-v2:S0 profile defaults (merged under the variant config) + the two
    # sealed-default knobs from rm_battery_v2_28d/DOMINANT_THREAD_ANALYSIS.md:
    # - revisit_context_exclude_self: the revisit route's material-match
    #   justification context excludes the candidate dormant threads' OWN prior
    #   thoughts (VA/VC context-exclusion hygiene extended to the revisit
    #   self-justification path) — a dormant thread must be re-derivable from
    #   CURRENT world/question/user context, not from itself.
    # - rest_assoc_require_distinct: rest/noop-path association thoughts
    #   (world reactivation/association templates) are emitted only when the
    #   juxtaposed items are distinct (different event ids AND non-identical
    #   normalized text); otherwise silent rest. No degenerate 「X」想起「X」.
    # The consolidation duplicate-thread fix in sleep.py is a data-quality FIX
    # (always on), not a knob.
    "R13": {"revisit_context_exclude_self": True,
            "rest_assoc_require_distinct": True},
}
POOL_DAYS = 2.5  # time-anchored creation pool (sealed-era span)


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def _ts(iso):
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def _shannon(counter: Counter) -> float:
    n = sum(counter.values())
    if not n or len(counter) <= 1:
        return 0.0
    return round(-sum((c / n) * math.log2(c / n) for c in counter.values())
                 / math.log2(len(counter)), 3)


def _cites_question(store, rid, targets) -> bool:
    """True iff the recalled event id IS a target, or its own provenance links
    to one (one-hop walk — e.g. a bridge activation carrying the id of the
    question that re-woke its thread)."""
    if isinstance(rid, str) and rid in targets:
        return True
    e = store.get(rid) if isinstance(rid, str) else None
    if e is None:
        return False
    rel = e.links.get("related_to") or []
    if isinstance(rel, str):
        rel = [rel]
    return bool(set(rel) & set(targets))


def harness_seeds(snapshots: WorldSnapshotStore) -> list[SeedEntry]:
    """Derive the replay frontier from the world's REAL captures: the latest
    status-200 snapshot per distinct requested_url. A harness seed set made of
    anything else would burn the honest budget on fetch failures."""
    latest: dict[str, dict] = {}
    for sid in snapshots.all_ids():
        rec = snapshots.load(sid) or {}
        if rec.get("status") != 200:
            continue
        url = rec.get("requested_url")
        cur = latest.get(url)
        if cur is None or (rec.get("fetched_at") or "") > (cur.get("fetched_at") or ""):
            latest[url] = rec
    out = []
    for url, rec in sorted(latest.items()):
        declared = rec.get("declared") or {}
        out.append(SeedEntry(
            url=url,
            source=declared.get("source") or rec.get("source_type") or "replay",
            topic=declared.get("topic") or rec.get("source_type") or "reference",
            title=rec.get("title") or "",
            summary=(rec.get("text") or "")[:120],
        ))
    return out


class ReplayableOnlySource(ReplayWorldSource):
    """A replay source can only *offer* what it can actually replay: items
    whose URL has a capture. Absorbed outbound links stay discovered (they
    are real links of real pages) but are not offered — observing them in
    replay would deterministically fail and burn the honest budget."""

    def __init__(self, seeds: list[SeedEntry], snapshots: WorldSnapshotStore):
        super().__init__(seeds, snapshots)
        self._replayable = set()
        for sid in snapshots.all_ids():
            rec = snapshots.load(sid) or {}
            if rec.get("status") == 200 and rec.get("requested_url"):
                self._replayable.add(rec["requested_url"])

    def items(self):
        return tuple(it for it in super().items()
                     if self._url_by_id.get(it.id, "") in self._replayable)


def _real_provider_if_configured():
    """Harness real-model opt-in: EXACTLY main.py:_make_provider semantics.

    All three RESIDENT_MODEL_* env vars must be set (sourced from backend/.env
    by the caller — the key is never logged or committed). When unset, return
    None and run_cell stays byte-identical to the deterministic fake.
    """
    base_url = os.environ.get("RESIDENT_MODEL_BASE_URL")
    api_key = os.environ.get("RESIDENT_MODEL_API_KEY")
    model = os.environ.get("RESIDENT_MODEL_NAME")
    if base_url and api_key and model:
        return AnthropicMessagesProvider(
            base_url=base_url, model=model, api_key=api_key,
            timeout=float(os.environ.get("RESIDENT_MODEL_TIMEOUT", "30")),
        )
    return None


def run_cell(out: Path, arm: str, *, days: int, beat_minutes: int, seed: int,
             questions: bool = True, continuity: bool = True,
             variant: str = "S0") -> dict:
    t_wall0 = time.monotonic()
    provider = _real_provider_if_configured()
    profile = REGISTRY[arm]
    # ADR-0017: profile-level default flags (active-v2 only) are applied
    # UNDER the explicit variant config — an explicit variant always wins, so
    # e.g. active-v2:S0 runs the profile defaults and active-v2:E2VA equals
    # active-v1:E2VA. Flag-off profiles (sealed-baseline, active-v1,
    # dense-debug) have empty cognitive_flags and are byte-identical to v0.2.
    variant_cfg = {**getattr(profile, "cognitive_flags", {}),
                   **REVIVAL_VARIANTS[variant]}
    cfg = {
        "days": days, "beat_minutes": beat_minutes, "seed": seed,
        "variant": variant, "consolidation_pool_days": POOL_DAYS,
        "profile_cognitive_flags": dict(getattr(profile, "cognitive_flags", {})),
        "active_wake_interval_s": profile.active_wake_interval_s,
        "quiet_wake_interval_s": profile.quiet_wake_interval_s,
        "drowsy_wake_interval_s": profile.drowsy_wake_interval_s,
        "world_cooldown_s": profile.world_cooldown_s,
        "world_max_light_per_day": profile.world_max_light_per_day,
        "world_reobservation_days": profile.world_reobservation_days,
        "questions": questions, "continuity": continuity,
        "provider": provider.model_id if provider is not None else "fake/deterministic-v0",
        "world_source": "replay(real snapshots)",
    }
    if provider is not None:
        # Real-model runs are behaviour-nondeterministic (model sampling);
        # recorded in the fingerprint-input config. Fake configs stay
        # byte-identical to their pre-opt-in fingerprints (no new key).
        cfg["nondeterminism_note"] = ("real model: behaviour-nondeterministic "
                                      "across identical configs")
    fp = hashlib.sha256(json.dumps({"arm": arm, **cfg}, sort_keys=True)
                        .encode()).hexdigest()[:8]
    run_dir = out / "runs" / arm / fp / f"seed{seed}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(
        json.dumps({"arm": arm, "fingerprint": fp, "profile": profile.name,
                    "commit": _commit(), **cfg},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    db = run_dir / "world.sqlite3"
    if db.exists():
        db.unlink()

    clock = VirtualClock(SIM_START)
    store = EventStore(db, now_fn=lambda: clock.now)
    shadow_layer, semantic_kwargs = make_semantic_stack(
        store, run_dir / "vectors.sqlite3", mode="legacy", shadow=True,
        shadow_log_path=run_dir / "semantic_shadow.jsonl")
    mem = MemoryRetrieval(store, **(semantic_kwargs or {}))
    mind = MindLoop(store, memory=mem, rng=random.Random(seed),
                    provider=provider,
                    self_revival=True,
                    revival=RevivalParams(**{k: v for k, v in variant_cfg.items()
                                             if k in ("gate_mode", "opportunity_clock_s")}),
                    shadow_semantic=shadow_layer,
                    thread_selection=variant_cfg.get("thread_selection", "recent"),
                    thread_yield_s=variant_cfg.get("thread_yield_s"),
                    context_exclude_prev_thread=variant_cfg.get(
                        "context_exclude_prev_thread", False),
                    dormant_selection=variant_cfg.get("dormant_selection", "recent"),
                    substantive_context=variant_cfg.get("substantive_context", False),
                    thread_query_substantive_only=variant_cfg.get(
                        "thread_query_substantive_only", False),
                    recall_pool_hygiene=variant_cfg.get(
                        "recall_pool_hygiene", False),
                    zero_history_bootstrap=variant_cfg.get(
                        "zero_history_bootstrap", False),
                    revisit_context_exclude_self=variant_cfg.get(
                        "revisit_context_exclude_self", False))
    sleep_engine = SleepEngine(store, memory=mem, rng=random.Random(seed + 1000),
                               consolidation_pool_days=POOL_DAYS,
                               thread_title_meta_filter=variant_cfg.get(
                                   "thread_title_meta_filter", False),
                               recall_pool_hygiene=variant_cfg.get(
                                   "recall_pool_hygiene", False))
    reentry = ReentryEngine(store, memory=mem)
    machine = CircadianStateMachine(store)

    seeds = harness_seeds(WorldSnapshotStore(SNAPSHOTS))
    source = ReplayableOnlySource(seeds, WorldSnapshotStore(SNAPSHOTS))
    wparams = world_params(profile, base=WORLD_ALL)
    if questions:
        import dataclasses
        wparams = dataclasses.replace(wparams, enable_questions=True)
    if variant_cfg.get("question_thread_bridge"):
        import dataclasses
        wparams = dataclasses.replace(wparams, question_thread_bridge=True)
    if variant_cfg.get("rest_assoc_require_distinct"):
        import dataclasses
        wparams = dataclasses.replace(wparams, rest_assoc_require_distinct=True)
    world_engine = WorldWindowEngine(
        store, memory=mem, params=wparams,
        rng=random.Random(seed + 2000),
        question_rng=random.Random(seed + 3000),
        source=source)
    orch = CircadianOrchestrator(store, mind, sleep_engine, reentry=reentry,
                                 machine=machine, world_engine=world_engine,
                                 params=circadian_params(profile))
    continuity_engine = ContinuityEngine(store, memory=mem) if continuity else None

    threads: dict[str, str] = {}
    turns = sorted(((SIM_START + timedelta(days=t.day - 1, hours=t.hour), t)
                    for t in DEFAULT_USER_SCRIPT), key=lambda x: x[0])
    turn_idx = 0
    step = timedelta(minutes=beat_minutes)
    now = SIM_START
    end = SIM_START + timedelta(days=days)
    while now <= end:
        clock.set(now)
        while turn_idx < len(turns) and turns[turn_idx][0] <= now:
            u = _inject_turn(store, turns[turn_idx][1], threads)
            if continuity_engine is not None and u is not None:
                continuity_engine.on_user_return(u)
            turn_idx += 1
        asyncio.run(orch.beat(now.isoformat()))
        now += step

    report = build_report(store, arm, cfg, fp, days, run_dir,
                          wall_seconds=time.monotonic() - t_wall0)
    (run_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    return report


def build_report(store: EventStore, arm: str, cfg: dict, fp: str,
                 days: int, run_dir: Path, wall_seconds: float = 0.0) -> dict:
    latest = store.latest()
    all_ev = store.list(40000, order="asc")
    n_days = max(days, 1)

    wakes = [e for e in all_ev if e.type == "wake.started"]
    # a genuine noop = a completed wake whose result is noop (wake.noop and
    # wake.completed are both appended for one resting wake — never sum them)
    noops = [e for e in all_ev if e.type == "wake.completed"
             and e.content.get("result") == "noop"]
    # provider errors: reflection ProviderError -> honest noop with the
    # "model provider unavailable during reflection; resting" reason
    noop_reasons = Counter(e.content.get("reason") for e in all_ev
                           if e.type == "wake.noop")
    provider_errors = sum(v for k, v in noop_reasons.items()
                          if k and "provider unavailable" in k)
    thoughts = [e for e in all_ev if e.type == "thought.created"]
    obs = [e for e in all_ev if e.type == "world.observation"]
    impressions = [e for e in all_ev if e.type == "impression.formed"]
    questions = [e for e in all_ev if e.type == "question.created"]
    q_revisits = [e for e in all_ev if e.type == "question.revisited"]
    q_state = question_state(store, now_iso=latest.created_at if latest else None)
    routes = Counter(e.content.get("route") for e in all_ev
                     if e.type == "wake.route_selected")
    route_sel = [e for e in all_ev if e.type == "wake.route_selected"]
    revival_opps = sum(1 for e in route_sel if e.content.get("self_revival_candidate"))
    revisit_wakes = sum(1 for e in route_sel if e.content.get("route") == "revisit")
    self_threads_created = sum(1 for e in all_ev if e.type == "thread.created"
                               and e.content.get("origin") == "self")
    topics = Counter(e.content.get("topic") for e in obs)
    origins = Counter((e.metadata or {}).get("origin") for e in thoughts)

    cited: Counter = Counter()
    recall_ages, meta_recall = [], 0
    id_ts = {e.id: _ts(e.created_at) for e in all_ev}
    for t in thoughts:
        t_t = _ts(t.created_at)
        recalled = (t.metadata or {}).get("recalled") or []
        for rid in (recalled if isinstance(recalled, list) else [recalled]):
            cited[rid] += 1
            src_t = id_ts.get(rid)
            if t_t and src_t:
                recall_ages.append((t_t - src_t).total_seconds() / 86400.0)
            src = store.get(rid)
            if src is not None and (src.text or "").startswith(META_PREFIXES):
                meta_recall += 1
    n_recall_rows = sum(len((t.metadata or {}).get("recalled") or [])
                        for t in thoughts)

    thread_revisits = defaultdict(list)
    for e in all_ev:
        if e.type == "thread.revisited":
            thread_revisits[e.content.get("thread_id")].append(_ts(e.created_at))
    intervals = []
    for times in thread_revisits.values():
        times = sorted(t for t in times if t)
        intervals += [(b - a).total_seconds() / 86400.0
                      for a, b in zip(times, times[1:])]

    obs_ts = {o.id: _ts(o.created_at) for o in obs}
    trace_days = []
    for t in thoughts:
        t_t = _ts(t.created_at)
        for rid in (t.metadata or {}).get("recalled") or []:
            if rid in obs_ts and t_t and obs_ts[rid]:
                trace_days.append((t_t - obs_ts[rid]).total_seconds() / 86400.0)

    cont_events = [e for e in all_ev if e.type.startswith("reentry.")]
    cont_abs = [e for e in all_ev if e.type == "absence.detected"]
    night_wakes = sum(1 for w in wakes
                      if w.created_at[11:13] >= "22" or w.created_at[11:13] < "06")

    # H1 genesis chains: a question re-struck from >=2 distinct sources across
    # >=7 days with a later self-thought engaging the question's material —
    # and PROVENANCE INDEPENDENT OF THE USER: no evidence or revisit may come
    # from the conversation family (a chain that needs the user prompt to
    # stand does not count)
    h1 = []
    for q in questions:
        ev_ids = ((q.metadata or {}).get("question") or {}).get("evidence") or []
        ev_sources = set()
        ev_ok = True
        for eid in ev_ids:
            e = store.get(eid)
            if e is None:
                continue
            if e.family == "conversation":
                ev_ok = False
            ev_sources.add(e.content.get("source") or e.type)
        q_t = _ts(q.created_at)
        rvs = [r for r in q_revisits if r.content.get("question_id") == q.id]
        revisit_ok = True
        for r in rvs:
            for cid in (r.links.get("caused_by") or []):
                ce = store.get(cid)
                if ce is None or ce.type != "world.observation":
                    revisit_ok = False
                else:
                    # multi-source substance: each revisit's causing
                    # world.observation contributes its source to the chain's
                    # source set (measurement alignment with the docstring's
                    # "re-struck from >=2 distinct sources"; mirrors
                    # SELF_ORIGIN_REPORT §七-九 provenance walk). caused_by
                    # must already be world.observation, so this never
                    # imports conversation provenance.
                    ev_sources.add(ce.content.get("source") or ce.type)
        span = None
        if q_t and rvs:
            last = max((_ts(r.created_at) for r in rvs), default=None)
            if last:
                span = (last - q_t).total_seconds() / 86400.0
        # criterion (d) is PER-QUESTION: a self-origin thought that engages
        # THIS question's material — its recalled ids (one-hop provenance via
        # _cites_question) or an explicit related_to link to the question or
        # its birth evidence. (Previously a stale loop variable `t` leaked
        # from the trace_days loop above, testing the log's LAST thought
        # against every question — see out/confirm28_multisrc/H1_FORENSICS.md §5.)
        q_targets = set(list(ev_ids) + [q.id])
        citing = []
        for t in thoughts:
            if (t.metadata or {}).get("origin") not in ("self_thread", "private_project"):
                continue
            hit = any(_cites_question(store, rid, q_targets)
                      for rid in ((t.metadata or {}).get("recalled") or []))
            if not hit:
                rel = t.links.get("related_to") or []
                rel = [rel] if isinstance(rel, str) else rel
                hit = bool(set(rel) & q_targets)
            if hit:
                citing.append(t.id)
        linked_self = bool(citing)
        if (len(rvs) >= 2 and len(ev_sources) >= 2 and span and span >= 7.0
                and linked_self and ev_ok and revisit_ok):
            h1.append({"question_id": q.id, "topic": q.content.get("topic"),
                       "revisits": len(rvs), "span_days": round(span, 2),
                       "sources": sorted(ev_sources),
                       "citing_thoughts": citing[:5]})

    wprof = world_profile(store)
    n_q_total, n_q_dormant = q_state["n_total"], q_state["n_dormant"]
    self_thoughts = [t for t in thoughts
                     if (t.metadata or {}).get("origin") in ("self_thread", "private_project")]
    self_thread_counts = Counter(
        t.links.get("thread_id") or t.content.get("thread_id") for t in self_thoughts)
    self_thread_concentration = (round(max(self_thread_counts.values()) / len(self_thoughts), 3)
                                 if self_thoughts else None)
    mean_recall_age = round(statistics.mean(recall_ages), 2) if recall_ages else None
    top_cited = cited.most_common(1)
    echo_share = round(top_cited[0][1] / len(thoughts), 3) if thoughts and top_cited else 0.0

    failures = {
        "echo_storm": echo_share > 0.5,
        "question_spam": len(questions) / n_days > 2.0,
        "world_addiction": bool(thoughts) and (origins.get("world", 0) / len(thoughts)) > 0.6,
        "recency_collapse": (mean_recall_age is not None and mean_recall_age < 0.5
                             and days >= 5),
        "noop_extinction": len(wakes) > 20 and (len(noops) / len(wakes)) < 0.05,
        "circadian_flattening": (len(wakes) > 20 and night_wakes / len(wakes) > 0.25),
        "meta_amplification": bool(n_recall_rows) and (meta_recall / n_recall_rows) > 0.3,
    }

    shadow_path = run_dir / "semantic_shadow.jsonl"
    shadow_overlap, shadow_top1, shadow_n = None, None, 0
    if shadow_path.exists():
        ovs, t1 = [], 0
        for line in shadow_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            shadow_n += 1
            if isinstance(r.get("overlap"), (int, float)):
                k = max(len(r.get("legacy_top") or []), 1)
                ovs.append(float(r["overlap"]) / k)  # raw overlap count → fraction @k
            if r.get("top1_differs"):
                t1 += 1
        if ovs:
            shadow_overlap = round(statistics.mean(ovs), 3)
            shadow_top1 = round(t1 / shadow_n, 3)

    return {
        "arm": arm, "fingerprint": fp, "config": cfg, "days": days,
        "vitals": {
            "n_events": store.count(),
            "wakes": len(wakes), "wakes_per_day": round(len(wakes) / n_days, 2),
            "meaningful_wakes_per_day": round(len(thoughts) / n_days, 2),
            "noop_ratio": round(len(noops) / len(wakes), 3) if wakes else None,
            "world_obs_per_day": round(len(obs) / n_days, 2),
            "impressions_per_day": round(len(impressions) / n_days, 2),
            "thoughts_per_day": round(len(thoughts) / n_days, 2),
            "night_wake_share": round(night_wakes / len(wakes), 3) if wakes else None,
            "provider_errors": provider_errors,
            "provider_error_rate": (round(provider_errors / len(wakes), 3)
                                    if wakes else None),
            "noop_reasons": dict(noop_reasons.most_common(5)),
            "wall_seconds": round(wall_seconds, 1),
        },
        "cognition": {
            "questions_total": len(questions),
            "questions_per_day": round(len(questions) / n_days, 2),
            "question_revisit_rate": (round(len([q for q in q_state["questions"]
                                                 if q["revisits"]]) / n_q_total, 3)
                                      if n_q_total else None),
            "question_dormant_rate": (round(n_q_dormant / n_q_total, 3)
                                      if n_q_total else None),
            "thread_revisits": sum(len(v) for v in thread_revisits.values()),
            "thread_recurrence_interval_days": (
                round(statistics.median(intervals), 2) if intervals else None),
            "origin_shares": ({k: round(v / len(thoughts), 3)
                               for k, v in origins.items()} if thoughts else {}),
            "route_entropy": _shannon(routes),
            "topic_entropy": _shannon(topics),
            "world_to_thought_rate": wprof["world_to_thought_rate"],
            "cross_source_association": wprof["cross_source_association_rate"],
            "seen_left_nothing_rate": wprof["seen_left_nothing_rate"],
        },
        "memory": {
            "mean_recall_age_days": mean_recall_age,
            "meta_recall_share": (round(meta_recall / n_recall_rows, 3)
                                  if n_recall_rows else None),
            "echo_share": echo_share,
            "world_trace_lifetime_days_mean": (
                round(statistics.mean(trace_days), 2) if trace_days else None),
            "world_trace_lifetime_days_max": (
                round(max(trace_days), 2) if trace_days else None),
        },
        "continuity": {
            "absences": len(cont_abs),
            "candidates": sum(1 for e in cont_events if e.type == "reentry.candidate"),
            "selected": sum(1 for e in cont_events if e.type == "reentry.selected"),
            "noop": sum(1 for e in cont_events if e.type == "reentry.noop"),
        },
        "revival": {
            "opportunities": revival_opps,
            "self_threads_created": self_threads_created,
            "revisit_wakes": revisit_wakes,
            "self_thoughts": len(self_thoughts),
            "self_thread_concentration": self_thread_concentration,
        },
        "world_fetch": wprof["live"],
        "shadow": {"records": shadow_n, "overlap_mean": shadow_overlap,
                   "top1_divergence": shadow_top1},
        "h1_genesis_chains": {"count": len(h1), "detail": h1},
        "failure_modes": failures,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Accelerated Living Harness (ADR-0014)")
    ap.add_argument("--arms", default="sealed-baseline,active-v1,dense-debug")
    ap.add_argument("--variants", default="S0",
                    help="revival variants (ADR-0015): S0,S1,S2-12h,S2-18h,S2-24h")
    ap.add_argument("--seeds", default="0,1,2,3")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--beat-minutes", type=int, default=5)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent
                                         / "out" / "active_living"))
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    for v in variants:
        if v not in REVIVAL_VARIANTS:
            raise SystemExit(f"unknown variant {v!r}; known: {sorted(REVIVAL_VARIANTS)}")
    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    from concurrent.futures import ProcessPoolExecutor
    combos = [(arm, v, seed) for arm in arms for v in variants for seed in seeds]
    results = {}
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_cell, out, arm, days=args.days,
                          beat_minutes=args.beat_minutes, seed=seed,
                          variant=variant): (arm, variant, seed)
                for arm, variant, seed in combos}
        for fut in futs:
            arm, variant, seed = futs[fut]
            results[f"{arm}:{variant}/seed{seed}"] = fut.result()
            print(f"[done] {arm}:{variant}/seed{seed}", flush=True)

    labels = [f"{arm}:{v}" for arm in arms for v in variants]
    metrics = [
        ("wakes/day", lambda r: r["vitals"]["wakes_per_day"]),
        ("meaningful_wakes/day", lambda r: r["vitals"]["meaningful_wakes_per_day"]),
        ("noop_ratio", lambda r: r["vitals"]["noop_ratio"]),
        ("world_obs/day", lambda r: r["vitals"]["world_obs_per_day"]),
        ("questions/day", lambda r: r["cognition"]["questions_per_day"]),
        ("question_revisit_rate", lambda r: r["cognition"]["question_revisit_rate"]),
        ("question_dormant_rate", lambda r: r["cognition"]["question_dormant_rate"]),
        ("thread_recurrence_days", lambda r: r["cognition"]["thread_recurrence_interval_days"]),
        ("route_entropy", lambda r: r["cognition"]["route_entropy"]),
        ("topic_entropy", lambda r: r["cognition"]["topic_entropy"]),
        ("self_origin_share", lambda r: r["cognition"]["origin_shares"].get("self_thread", 0.0)),
        ("world_origin_share", lambda r: r["cognition"]["origin_shares"].get("world", 0.0)),
        ("user_derived_share", lambda r: r["cognition"]["origin_shares"].get("user_recent", 0.0)
         + r["cognition"]["origin_shares"].get("user_old", 0.0)),
        ("cross_source_assoc", lambda r: r["cognition"]["cross_source_association"]),
        ("seen_left_nothing", lambda r: r["cognition"]["seen_left_nothing_rate"]),
        ("mean_recall_age_days", lambda r: r["memory"]["mean_recall_age_days"]),
        ("meta_recall_share", lambda r: r["memory"]["meta_recall_share"]),
        ("world_trace_lifetime_max", lambda r: r["memory"]["world_trace_lifetime_days_max"]),
        ("reentry_selected", lambda r: r["continuity"]["selected"]),
        ("reentry_noop", lambda r: r["continuity"]["noop"]),
        ("shadow_overlap", lambda r: r["shadow"]["overlap_mean"]),
        ("h1_chains", lambda r: r["h1_genesis_chains"]["count"]),
        ("revival_opportunities", lambda r: r["revival"]["opportunities"]),
        ("self_threads_created", lambda r: r["revival"]["self_threads_created"]),
        ("self_thoughts", lambda r: r["revival"]["self_thoughts"]),
        ("self_thread_concentration", lambda r: r["revival"]["self_thread_concentration"]),
    ]
    table = {}
    for label, fn in metrics:
        row = {}
        for lab in labels:
            vals = [fn(results[f"{lab}/seed{s}"]) for s in seeds
                    if f"{lab}/seed{s}" in results]
            vals = [float(v) for v in vals if v is not None]
            row[lab] = round(sum(vals) / len(vals), 3) if vals else None
        table[label] = row
    failure_flags = {}
    for lab in labels:
        flags = set()
        for s in seeds:
            flags |= {k for k, v in results[f"{lab}/seed{s}"]["failure_modes"].items() if v}
        failure_flags[lab] = sorted(flags)

    comparison = {
        "arms": arms, "variants": variants, "seeds": seeds, "days": args.days,
        "beat_minutes": args.beat_minutes,
        "metrics": table, "failure_modes_by_arm": failure_flags,
        "note": ("activity frontier = the lowest tier with a clearly richer life "
                 "AND none of the failure modes; every run kept on disk"),
    }
    (out / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")

    hdr = f"{'metric':<28}" + "".join(f"{a:>18}" for a in labels)
    print(hdr)
    for label, _ in metrics:
        print(f"{label:<28}" + "".join(f"{str(table[label][a]):>18}" for a in labels))
    for lab in labels:
        print(f"failure_modes[{lab}]:", failure_flags[lab] or "none")
    print(f"\n[comparison written to {out / 'comparison.json'}]")


if __name__ == "__main__":
    main()
