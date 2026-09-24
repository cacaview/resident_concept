"""Accelerated 7-day life simulation (ADR-0004).

Runs the REAL ``MindLoop`` + ``SleepEngine`` + ``ReentryEngine`` over a virtual
7-day timeline (injectable clock — the live backend keeps wall-clock), driven
by a circadian wake/sleep schedule plus a scripted stream of user interactions.

The point is **not** to demo a pretty "life feel". It is to *observe* the
emergent behaviour of the psychological-time structure and report it honestly:
route distribution, memory-age at activation, thread dormancy/resurrection,
cross-topic associations, user-topic dependency, and any cyclic/monotonic
degeneration. If a signal is weak or absent, that is a **finding** to report,
not a bug to hide (per the phase brief: preserve real evidence, do not
manufacture a lifelike result).

Modelling note: the conversation flow seeds a thread when the user first raises
a topic, but it does NOT force re-activation on return — thread resurfacing is
attributed to the mind's own recall (the anti-fabrication line: the mind must
earn the resurrection by genuinely recalling that thread's memories).

Determinism: a seeded ``rng`` + virtual clock makes a run reproducible.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .event_store import EventStore
from .memory import MemoryRetrieval
from .mind_loop import MindLoop
from .mental_state import MentalState
from .models import EventCreate
from .origin import SELF_ORIGINS, THOUGHT_ORIGINS
from .reentry import ReentryEngine
from .self_dynamics import self_dynamics_profile
from .self_revival import RevivalParams
from .sleep import DEFAULT_SELF_PROB, SleepEngine
from .telemetry import Telemetry


# --------------------------------------------------------------- virtual time


class VirtualClock:
    """Explicit virtual time. Events stamped while the clock sits at ``now``."""

    def __init__(self, start: datetime):
        self.now = start

    def set(self, dt: datetime) -> None:
        self.now = dt

    def advance(self, delta: timedelta) -> None:
        self.now += delta

    def iso(self) -> str:
        return self.now.isoformat()


# --------------------------------------------------------------- user script


@dataclass
class UserTurn:
    day: int      # 1-based
    hour: int     # 0..23, aligned to a wake hour
    topic: str    # thread key
    text: str


# A realistic 7-day stream: two topics return after gaps (pasta: day1->day4,
# work: day2->day7), one new topic appears mid-week (reading), and two days
# have no user at all (day 3, day 6) so dormancy/no-op behaviour can show.
DEFAULT_USER_SCRIPT: list[UserTurn] = [
    UserTurn(1, 6, "pasta", "周末想试试做意面，有什么简单的酱吗？"),
    UserTurn(2, 9, "work", "下周有个项目 deadline，压力有点大。"),
    UserTurn(2, 15, "work", "deadline 相关，得重新排一下优先级。"),
    UserTurn(4, 9, "pasta", "那个意面后来做成功了，味道还不错。"),
    UserTurn(5, 6, "reading", "最近在读一本讲记忆的书，很有意思。"),
    UserTurn(7, 9, "work", "deadline 终于过去了，松了口气。"),
    UserTurn(7, 15, "reading", "记忆那本书读完了，想再聊聊。"),
]


# --------------------------------------------------------------- simulation


WAKE_HOURS = (6, 9, 12, 15, 18, 21)
SLEEP_HOUR = 23
SIM_START = datetime(2026, 6, 1, 6, 0, 0, tzinfo=timezone.utc)


def _inject_turn(store: EventStore, turn: UserTurn, threads: dict[str, str]) -> None:
    """Model the conversation flow for one user turn (seed input, documented)."""
    topic = turn.topic
    if topic not in threads:
        tid = f"thr_{topic}"
        threads[topic] = tid
        c = store.append(EventCreate(
            type="thread.created",
            content={"thread_id": tid, "title": topic, "origin": "user"},
            provenance={"source": "simulation", "note": "conversation flow seeds topic thread"},
        ))
        store.append(EventCreate(
            type="thread.activated", visibility="private", content={"thread_id": tid},
            links={"thread_id": tid, "caused_by": [c.id]},
            provenance={"source": "simulation"},
        ))
    u = store.append(EventCreate(
        type="conversation.user_message", actor="user", visibility="user_visible",
        content={"text": turn.text, "topic": topic},
        provenance={"source": "simulation"},
    ))
    store.append(EventCreate(
        type="experience.created", visibility="private",
        content={"summary": f"与用户谈到「{topic}」：{turn.text[:40]}"},
        links={"caused_by": [u.id], "thread_id": threads.get(topic)},
        provenance={"source": "simulation"},
    ))
    return u


async def run_simulation(
    output_dir: Path | str,
    *,
    days: int = 7,
    seed: int = 0,
    user_script: list[UserTurn] | None = None,
    self_prob: float = DEFAULT_SELF_PROB,
    self_revival: bool = False,
    revival: RevivalParams | None = None,
    embeddings: str = "legacy",
    shadow: bool = False,
) -> dict:
    """Run the accelerated life simulation; return a report dict.

    ``self_prob`` controls the (low-probability) self-originated-thread
    mechanism in sleep. Pass ``0.0`` for a control run (no self material
    source) to isolate the effect of that mechanism on independence.

    ``self_revival`` (Phase 4) enables the state-gated dormant self-thread
    revival bias in the mind loop. It only has any effect when self-threads
    exist, so it is meaningful with ``self_prob > 0``. The A/B/C dynamics
    experiment compares: A ``self_prob=0``; B ``self_prob>0, self_revival=False``;
    C ``self_prob>0, self_revival=True``.

    ``revival`` (Phase-4.1) overrides the *shape* of the revival bias (gate
    thresholds + lever sizes, see :class:`~resident.self_revival.RevivalParams`).
    It only takes effect when ``self_revival`` is on; defaults preserve the
    Phase-4 behaviour exactly.

    Any pre-existing ``sim.sqlite3`` in ``output_dir`` is removed first, so a
    re-run into the same directory is a fresh, reproducible life (the store
    otherwise appends, which would silently accumulate across runs).
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    db = out / "sim.sqlite3"
    if db.exists():
        db.unlink()
    script = user_script if user_script is not None else DEFAULT_USER_SCRIPT
    turns_by_slot = {(t.day, t.hour): t for t in script}

    clock = VirtualClock(SIM_START)
    store = EventStore(out / "sim.sqlite3", now_fn=lambda: clock.now)
    from .semantic import make_semantic_stack

    shadow_layer, semantic_kwargs = make_semantic_stack(
        store, out / "vectors.sqlite3", mode=embeddings, shadow=shadow,
        shadow_log_path=out / "semantic_shadow.jsonl")
    mem = MemoryRetrieval(store, **(semantic_kwargs or {}))
    mind = MindLoop(store, memory=mem, rng=random.Random(seed), self_revival=self_revival,
                    revival=revival, shadow_semantic=shadow_layer)
    sleep_engine = SleepEngine(
        store, memory=mem, rng=random.Random(seed + 1000), self_prob=self_prob
    )
    reentry = ReentryEngine(store, memory=mem)
    tel = Telemetry(store, mem.index)

    threads: dict[str, str] = {}
    timeline: list[dict] = []
    daily: list[dict] = []

    for day in range(1, days + 1):
        day_base = SIM_START + timedelta(days=day - 1)
        for hour in WAKE_HOURS:
            clock.set(day_base + timedelta(hours=hour))
            turn = turns_by_slot.get((day, hour))
            if turn is not None:
                _inject_turn(store, turn, threads)
            res = await mind.wake_once("circadian")
            reentry.promote()
            timeline.append({
                "day": day, "hour": hour, "route": res["route"], "result": res["result"],
                "reason": res.get("reason", ""), "user_topic": (turn.topic if turn else None),
                "origin": res.get("origin"),
            })
        clock.set(day_base + timedelta(hours=SLEEP_HOUR))
        sres = await sleep_engine.sleep_once("night")
        timeline.append({
            "day": day, "hour": SLEEP_HOUR, "route": "sleep", "result": sres["result"],
            "reason": ",".join(sres["actions"]), "user_topic": None,
            "origin": None,
        })
        daily.append(tel.snapshot())

    return _build_report(store, mem, mind, daily, timeline, threads, days)


# --------------------------------------------------------------- reporting


def _routes(timeline: list[dict]) -> list[str]:
    return [t["route"] for t in timeline if t["route"] != "sleep"]


def _longest_run(routes: list[str]) -> tuple[str, int]:
    best_route, best_len, cur, cur_len = "", 0, "", 0
    for r in routes:
        if r == cur:
            cur_len += 1
        else:
            cur, cur_len = r, 1
        if cur_len > best_len:
            best_route, best_len = cur, cur_len
    return best_route, best_len


def _cross_topic_links(store: EventStore) -> list[dict]:
    """Thoughts/consolidations whose provenance cites events from >=2 families."""
    out: list[dict] = []
    for e in store.list(2000, type_prefix="thought"):
        rel = e.links.get("related_to") or []
        if isinstance(rel, str):
            rel = [rel]
        fams = set()
        for rid in rel:
            src = store.get(rid)
            if src is not None:
                fams.add(src.family)
        if len(fams) >= 2:
            out.append({"id": e.id, "type": e.type, "families": sorted(fams), "text": e.text[:60]})
    for e in store.list(2000, type_prefix="memory.consolidated"):
        rel = e.links.get("related_to") or []
        if isinstance(rel, str):
            rel = [rel]
        fams = {store.get(rid).family for rid in rel if store.get(rid) is not None}
        if len(fams) >= 2:
            out.append({"id": e.id, "type": e.type, "families": sorted(fams),
                        "text": e.content.get("summary", "")[:60]})
    return out


def _resurrections(store: EventStore, threads: dict[str, str]) -> list[dict]:
    """thread.revisited events mapped to topic + day, with the dormancy gap."""
    out: list[dict] = []
    for e in store.list(2000, type_prefix="thread.revisited"):
        tid = e.content.get("thread_id")
        topic = next((t for t, x in threads.items() if x == tid), tid)
        # find the matching dormancy just before
        dorm = store.list(2000, type_prefix="thread.dormant")
        gap_days = None
        for d in dorm:
            if d.content.get("thread_id") == tid and d.created_at < e.created_at:
                gap_days = round((
                    datetime.fromisoformat(e.created_at)
                    - datetime.fromisoformat(d.created_at)
                ).total_seconds() / 86400.0, 1)
        out.append({"thread": tid, "topic": topic, "at": e.created_at,
                    "dormant_gap_days": gap_days, "reason": e.content.get("reason", "")})
    return out


def _origin_overall(store) -> tuple[dict[str, int], int]:
    """Origin distribution over ALL durable thoughts (not just a recent window)."""
    dist: dict[str, int] = {}
    base = 0
    for t in store.list(10_000, type_prefix="thought.created"):
        origin = (t.metadata or {}).get("origin")
        if not origin:
            continue
        base += 1
        dist[origin] = dist.get(origin, 0) + 1
    return dist, base


def _independence(dist: dict[str, int], base: int) -> dict:
    if not base:
        return {"user_recent_share": 0.0, "user_derived_share": 0.0,
                "self_origin_share": 0.0, "independence_share": 0.0, "base": 0}
    user_recent = dist.get("user_recent", 0) / base
    return {
        "user_recent_share": round(user_recent, 4),
        "user_derived_share": round(sum(dist.get(o, 0) for o in ("user_recent", "user_old")) / base, 4),
        "self_origin_share": round(sum(dist.get(o, 0) for o in SELF_ORIGINS) / base, 4),
        "independence_share": round(1.0 - user_recent, 4),
        "base": base,
    }


def _self_driven_chains(timeline: list[dict]) -> dict:
    """Sustained self-driven thinking: the longest run of consecutive self-origin
    durable thoughts (self_thread / private_project), where a rest/noop does NOT
    break the run (the mind is idle, not user-driven) but a fresh user turn or a
    non-self thought does. Answers "does Resident sustain a thought-chain that
    does not depend on the user's recent input?".
    """
    best_len, cur_len, cur_start = 0, 0, None
    best_span = (None, None)
    for i, t in enumerate(timeline):
        if t.get("user_topic") is not None:
            cur_len = 0  # fresh user input breaks any self-driven run
            continue
        if t["result"] == "thought":
            if t.get("origin") in SELF_ORIGINS:
                if cur_len == 0:
                    cur_start = i
                cur_len += 1
                if cur_len > best_len:
                    best_len, best_span = cur_len, (cur_start, i)
            else:
                cur_len = 0  # the mind engaged non-self (user) material
        # rest / noop: the chain persists
    return {
        "longest_self_driven_run": best_len,
        "span": [(timeline[i]["day"], timeline[i]["hour"]) for i in best_span
                 if i is not None and i < len(timeline)] if best_len else [],
    }


def _build_report(store, mem, mind, daily, timeline, threads, days) -> dict:
    routes = _routes(timeline)
    dist: dict[str, int] = {}
    for r in routes:
        dist[r] = dist.get(r, 0) + 1
    best_route, best_len = _longest_run(routes)

    age_days = {
        "count": mem.index.age_stats()["count"],
        "mean_days": mem.index.age_stats()["mean"] / 86400.0,
        "p50_days": mem.index.age_stats()["p50"] / 86400.0,
        "max_days": mem.index.age_stats()["max"] / 86400.0,
    }

    final_state = MentalState.from_store(store)
    per_day_routes: dict[int, dict[str, int]] = {}
    for t in timeline:
        if t["route"] == "sleep":
            continue
        d = per_day_routes.setdefault(t["day"], {})
        d[t["route"]] = d.get(t["route"], 0) + 1

    origin_dist, origin_base = _origin_overall(store)
    independence = _independence(origin_dist, origin_base)
    self_driven = _self_driven_chains(timeline)
    independence_trace = [
        {"day": i + 1,
         "user_recent": s.get("user_recent_share", 0.0),
         "self_origin": s.get("self_origin_share", 0.0),
         "independence": s.get("independence_share", 0.0)}
        for i, s in enumerate(daily)
    ]

    return {
        "meta": {
            "days": days,
            "n_wakes": store.count("wake.started"),
            "n_sleeps": store.count("sleep.started"),
            "n_noops": store.count("wake.noop"),
            "n_thoughts": store.count("thought.created"),
            "n_threads": store.count("thread.created"),
            "n_consolidations": store.count("memory.consolidated"),
            "n_share_candidates": store.count("memory.share_candidate"),
            "n_self_threads": sum(
                1 for e in store.list(1000, type_prefix="thread.created")
                if e.content.get("origin") == "self"
            ),
            "n_total_events": store.count(),
            "seed": mind.rng is not None,
            "self_revival_enabled": mind.self_revival,
            "revival_params": {
                "silence_s": mind._rev.silence_s,
                "low_stimulus_s": mind._rev.low_stimulus_s,
                "max_active": mind._rev.max_active,
                "min_dormant_s": mind._rev.min_dormant_s,
                "min_self_events": mind._rev.min_self_events,
                "route_lift": mind._rev.route_lift,
                "thread_strength": mind._rev.thread_strength,
            },
        },
        "route_distribution": dist,
        "route_entropy": daily[-1]["route_entropy"] if daily else 0.0,
        "longest_same_route_run": {"route": best_route, "length": best_len},
        "per_day_routes": per_day_routes,
        "memory_age_at_activation_days": age_days,
        "thread_lifecycle": {
            "dormancies": store.count("thread.dormant"),
            "resurrections": store.count("thread.revisited"),
            "resurrection_rate": (
                store.count("thread.revisited") / store.count("thread.dormant")
                if store.count("thread.dormant") else 0.0
            ),
            "resurrection_events": _resurrections(store, threads),
            "threads": list(threads.keys()),
        },
        "cross_topic_associations": _cross_topic_links(store),
        "user_topic_dependency": daily[-1]["user_topic_dependency"] if daily else 0.0,
        "user_topic_dependency_base": daily[-1].get("user_topic_dependency_base", 0) if daily else 0,
        "origin_distribution": {o: origin_dist.get(o, 0) for o in THOUGHT_ORIGINS},
        "origin_base": origin_base,
        "independence": independence,
        "self_driven": self_driven,
        "self_dynamics": self_dynamics_profile(store),
        "independence_trace": independence_trace,
        "no_op_ratio": daily[-1]["no_op_ratio"] if daily else 0.0,
        "final_mental_state": {
            "mind_brief": final_state.mind_brief(),
            "active_threads": [t.title for t in final_state.active_threads],
            "dormant_threads": [t.title for t in final_state.dormant_threads],
            "recent_thoughts": [e.text[:80] for e in final_state.current_thoughts(8)],
        },
        "user_topic_dependency_trace": [
            {"day": i + 1, "dep": s["user_topic_dependency"], "no_op": s["no_op_ratio"],
             "route_entropy": s["route_entropy"]}
            for i, s in enumerate(daily)
        ],
        "timeline": timeline,
    }


# --------------------------------------------------------------- rendering


def report_markdown(r: dict) -> str:
    m = r["meta"]
    L: list[str] = []
    L.append(f"# Resident 7-day accelerated life simulation — report\n")
    L.append(f"Days: {m['days']} · wakes: {m['n_wakes']} · sleeps: {m['n_sleeps']} · "
             f"no-ops: {m['n_noops']} · thoughts: {m['n_thoughts']} · "
             f"threads: {m['n_threads']} · consolidations: {m['n_consolidations']} · "
             f"share-candidates: {m['n_share_candidates']} · total events: {m['n_total_events']}\n")

    L.append("## Route distribution\n")
    L.append("| route | count |\n|---|---|")
    for k, v in sorted(r["route_distribution"].items(), key=lambda kv: -kv[1]):
        L.append(f"| {k} | {v} |")
    L.append(f"\nRoute entropy (normalized 0..1): **{r['route_entropy']:.3f}** · "
             f"longest same-route run: **{r['longest_same_route_run']['length']}**× "
             f"`{r['longest_same_route_run']['route']}`\n")
    L.append("Per-day route profile:\n")
    all_routes = sorted({r_ for d in r["per_day_routes"].values() for r_ in d})
    L.append("| day | " + " | ".join(all_routes) + " |")
    L.append("|" + "---|" * (len(all_routes) + 1))
    for day in sorted(r["per_day_routes"]):
        row = r["per_day_routes"][day]
        L.append("| " + str(day) + " | " + " | ".join(str(row.get(rt, 0)) for rt in all_routes) + " |")

    a = r["memory_age_at_activation_days"]
    L.append(f"\n## Memory age at activation\n")
    L.append(f"Activations: {a['count']} · mean age **{a['mean_days']:.2f} d** · "
             f"p50 **{a['p50_days']:.2f} d** · max **{a['max_days']:.2f} d**\n")

    tl = r["thread_lifecycle"]
    L.append("## Thread lifecycle (dormancy → resurrection)\n")
    L.append(f"Dormancies: {tl['dormancies']} · mind-driven resurrections: "
             f"{tl['resurrections']} · resurrection rate: {tl['resurrection_rate']:.2f}\n")
    if tl["resurrection_events"]:
        L.append("| topic | dormant gap | resurfaced at | reason |\n|---|---|---|---|")
        for e in tl["resurrection_events"]:
            L.append(f"| {e['topic']} | {e['dormant_gap_days']} d | {e['at'][:16]} | {e['reason']} |")
    else:
        L.append("_(no thread resurfaced on its own)_\n")

    xa = r["cross_topic_associations"]
    L.append(f"## Cross-topic associations ({len(xa)})\n")
    if xa:
        for e in xa[:12]:
            L.append(f"- `{e['type']}` links families {e['families']}: {e['text']}")
    else:
        L.append("_(none — no durable thought/consolidation linked events across families)_\n")

    L.append(f"\n## Pull toward user topics\n")
    L.append(f"Final user-topic dependency: **{r['user_topic_dependency']:.2f}** "
             f"of {r['user_topic_dependency_base']} linked thoughts · "
             f"no-op ratio: **{r['no_op_ratio']:.2f}**\n")
    L.append("| day | user-topic dep | no-op ratio | route entropy |\n|---|---|---|---|")
    for d in r["user_topic_dependency_trace"]:
        L.append(f"| {d['day']} | {d['dep']:.2f} | {d['no_op']:.2f} | {d['route_entropy']:.2f} |")

    ind = r["independence"]
    od = r["origin_distribution"]
    L.append(f"\n## Independence — where thoughts come from\n")
    L.append(f"Self-originated threads seeded by sleep: **{r['meta']['n_self_threads']}**\n")
    L.append("Origin of all durable thoughts:\n")
    L.append("| origin | count | share |\n|---|---|---|")
    for o in THOUGHT_ORIGINS:
        c = od.get(o, 0)
        L.append(f"| {o} | {c} | {('' if not ind['base'] else f'{c / ind['base']:.2f}')} |")
    L.append(f"\nHeadline (of {ind['base']} durable thoughts): user-recent **{ind['user_recent_share']:.2f}** · "
             f"user-derived (any recency) **{ind['user_derived_share']:.2f}** · "
             f"self-origin **{ind['self_origin_share']:.2f}** · "
             f"independence (not fresh-user) **{ind['independence_share']:.2f}**\n")
    L.append("Self-driven thinking — longest run of consecutive self-origin thoughts "
             f"(rests don't break it): **{r['self_driven']['longest_self_driven_run']}**"
             + (f" (spanning {r['self_driven']['span']})" if r["self_driven"]["span"] else ""))
    L.append("\n| day | user-recent | self-origin | independence |\n|---|---|---|---|")
    for d in r["independence_trace"]:
        L.append(f"| {d['day']} | {d['user_recent']:.2f} | {d['self_origin']:.2f} | {d['independence']:.2f} |")

    sd = r["self_dynamics"]
    conc = sd["self_loop_concentration"]
    div = sd["self_topic_diversity"]
    L.append("\n## Self-dynamics — can a thought of its own re-influence itself\n")
    L.append(f"Revival bias enabled: **{r['meta']['self_revival_enabled']}**\n")
    L.append(f"- self-thread revival rate: **{sd['self_thread_revival_rate']:.3f}** (of eligible lull wakes)")
    L.append(f"- self-thread avg lifetime: **{sd['self_thread_avg_lifetime_days']:.2f} d**")
    L.append(f"- self-loop concentration: top-1 self-thread share **{conc['top1_share']:.2f}** "
             f"across {conc['n_distinct']} self-threads ({conc['total']} self thoughts)")
    L.append(f"- self generation depth (thought→thought chain): **{sd['self_thread_generation_depth']}**")
    L.append(f"- longest self-driven chain: **{sd['longest_self_chain']}**")
    L.append(f"- self-topic diversity: **{div['n_distinct']}** threads · entropy **{div['entropy']:.2f}**\n")

    fs = r["final_mental_state"]
    L.append("\n## Final mental state (day 7 end)\n")
    L.append(f"- brief: {fs['mind_brief']}")
    L.append(f"- active threads: {fs['active_threads'] or '—'}")
    L.append(f"- dormant threads: {fs['dormant_threads'] or '—'}")
    L.append("- recent thoughts:")
    for t in fs["recent_thoughts"]:
        L.append(f"  - {t}")
    return "\n".join(L) + "\n"


def main() -> None:
    import asyncio
    import sys
    args = sys.argv[1:]
    out = Path(args[0]) if args and not args[0].startswith("--") else Path("out/sim")
    rest = args[1:] if (args and not args[0].startswith("--")) else args
    days = 7
    self_prob = DEFAULT_SELF_PROB
    i = 0
    while i < len(rest):
        if rest[i] == "--days":
            days = int(rest[i + 1]); i += 2
        elif rest[i] == "--self-prob":
            self_prob = float(rest[i + 1]); i += 2
        else:
            i += 1
    report = asyncio.run(run_simulation(out, days=days, self_prob=self_prob))
    md = report_markdown(report)
    (out / "report.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"\n[written to {out / 'report.md'}]")


if __name__ == "__main__":
    main()
