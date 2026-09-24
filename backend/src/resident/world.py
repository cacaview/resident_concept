"""The World Window — a restrained, mental-state-gated source of *external*
experience (Phase 6, ADR-0008).

The core principle (the phase brief, verbatim): **World 是经历来源，不是任务来源；
reading ≠ thought；observation ≠ belief.** The World Window is how Resident gets a
*third kind of material* in its head — not what the user gave it, not what it
continued on its own, but what the world *happened to* put in front of it.

What it is **not**
------------------
- Not a news feed, RSS agent, or autonomous task searcher. It does not poll for
  "things to do"; it occasionally *looks at something* when the mind is in a state
  where that would be natural.
- Not a route in the :class:`~resident.mind_loop.MindLoop`. The route policy
  keeps ``world`` pinned to ``-1.0`` so a wake never *chooses* to browse — the
  world window opens as its own, separate mechanism (driven from the circadian
  orchestrator on awake beats), which keeps the mind's own machinery byte-identical
  with or without it (the A/B/C isolation).
- Not a hard-AND gate. A handful of *safety rails* (asleep / daily budget /
  cooldown) suppress it outright, but the decision to actually look is a single
  **soft propensity roll** fed by several signals (circadian state, user silence,
  today's exposure, current thread focus) — so it is neither a giant
  rarely-true conjunction nor a fixed quota. **0 windows in a day is a legitimate
  outcome** ("look at nothing today").

The event chain (every open, provenance-complete)
-------------------------------------------------
``world.window_opened`` → ``world.observation`` → ``world.experience`` →
``impression.formed`` → *(optionally, low-prob + grounded)* ``thought.created``
(origin ``world``) / ``question.created`` / a ``thread.created`` (origin ``world``).

``world.observation`` is the **provenance root**: any later "I saw / read /
discovered X" must back-link a real observation, and the store's dangling-link
invariant makes a world thought *unable* to cite an observation that never
happened. ``reading ≠ thought`` is enforced *structurally*: an open always yields
an observation + experience + (weak) impression, but a **thought forms only on a
real connection** — either a *re-encounter* (a stored impression is reactivated by
a later, related observation — the point of the Impression layer) or an
*association* with an *old, non-world* memory. First exposure with no connection
leaves only a weak impression, i.e. **seen but left nothing**.

v0.2 Step 3 (ADR-0011) — World → Question: ``question.created`` is the **residue
of an unthought connection**. It forms only where a thought *could* have formed
(the same evidence class — a real re-encounter or an own old-memory association)
but did not, and only on a further low-probability roll. Two kinds, both
evidence-linked: ``tension`` (two world experiences of connected material
coexist unintegrated) and ``gap`` (world material meets the resident's own old
memory). A later, independent experience re-meets an open question
(``question.revisited``); a question untouched for ``question_dormant_s`` is
DERIVED-dormant — never a tombstone event, never an answer pipeline, never a
self-rated importance. The layer is default-OFF (``enable_questions=False``):
the sealed arms stay byte-identical, and most observations still leave only an
impression.

Determinism: a seeded ``rng`` (its **own** instance, so the mind's rng stream is
untouched) + the virtual clock make a run reproducible. The corpus is fixed
(:mod:`resident.world_corpus`), so the "world" is the same for every seed. The
question layer has its own ``question_rng`` (default: shares the engine's — one
life, one stream; the experiment arms pass a dedicated stream so the question
roll never perturbs the window-selection trajectory).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime

from .circadian import ASLEEP_STATES, QUIET_STATES
from .event_store import EventStore
from .memory import MemoryRetrieval
from .mental_state import MentalState, ThreadView
from .models import EventCreate
from .origin import SELF_ORIGINS, USER_ORIGINS
from .web_fetch import FetchError
from .world_corpus import DEFAULT_WORLD_CORPUS, WorldCorpus, WorldItem
from .world_source import FixtureWorldSource, WorldSource


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


# Relative frequency of each entry mode among the modes that are *available* on a
# given beat. follow/edge are the common case; alien is genuinely low-frequency
# (陌生领域); accident is a bounded wander along a link.
_MODE_WEIGHTS: dict[str, float] = {"follow": 0.5, "edge": 0.3, "alien": 0.1, "accident": 0.1}
_ENTRY_MODES: tuple[str, ...] = ("follow", "edge", "alien", "accident")

# CJK function characters + punctuation stripped before *focus matching*. The
# lexical embedder tokenises CJK into single characters, so raw cosine is
# dominated by ubiquitous function words (的/有/和/与/…) that appear in nearly
# every text — topical signal (意面, 记忆, deadline) is drowned. Removing them is a
# standard stopword step that makes the follow/edge frontier topical; it is NOT a
# ratio-tuning knob (the thresholds that decide a match stay untouched).
_CJK_STOP = set(
    "的了和与或及在是就都而但也都又很更最不没没有这那他她我你们"
    "啊吧吗呢么什为以之其此该被把将于对从到向往给并且如若虽因所等因"
    "，。：；！？、（）《》""''…—·"
)


def _match_text(s: str) -> str:
    return "".join(ch for ch in s if ch not in _CJK_STOP)


# --------------------------------------------------------------------------- params


@dataclass(frozen=True)
class WorldParams:
    """The shape of the World Window. Per-day numbers are **caps, not quotas** —
    they are safety rails that make runaway browsing impossible, and hitting 0 of
    them in a day is a legitimate, non-failure outcome (the owner constraint).
    None of these is tuned toward a target ratio; they set the *envelope* within
    which the soft propensity decides beat by beat."""

    # --- per-day caps (rails) --------------------------------------------
    max_light_per_day: int = 8        # light fetches (window opens)
    max_deep_per_day: int = 3         # deep reads (durable world thoughts)
    max_alien_per_day: int = 1        # alien-mode opens (genuinely unfamiliar ground)
    max_accident_per_day: int = 2     # accident-mode opens (link walks)

    # --- cooldown between opens (satiety; not a quota, a rest between looks)
    cooldown_s: float = 2 * 3600.0

    # --- the soft propensity (a single scalar the several signals feed) ---
    base_propensity: float = 0.22
    quiet_bonus: float = 0.12          # a recognised quiet circadian state raises it
    silence_bonus_max: float = 0.18     # long user silence raises it (capped)
    silence_full_s: float = 12 * 3600.0  # the silence at which the bonus is full
    focus_penalty: float = 0.10         # an active thread lowers it (a focused mind)
    exposure_penalty_max: float = 0.20  # being near the daily cap lowers it (satiety)
    propensity_cap: float = 0.70

    # --- which entry modes are on (the A/B/C arms are defined by this set) --
    enabled_modes: tuple[str, ...] = _ENTRY_MODES

    # --- the Impression layer ---------------------------------------------
    impression_prob: float = 0.90      # an open leaves a weak impression (not certain)
    promotion_prob: float = 0.35       # a re-encounter promotes a stored impression
    association_prob: float = 0.25     # an open forms a thought via old-memory association

    # --- matching thresholds (lexical; honest, not tuned to a ratio) ------
    focus_threshold: float = 0.06      # min cosine for a text to "match" a corpus topic
    assoc_min_sim: float = 0.05        # min sim to count as a real old-memory association

    # --- re-observation horizon (world-side OPPORTUNITY; ADR-0014) --------
    # An observed item may re-enter the frontier after this many days — real
    # pages change and get re-checked. This widens what the window may be
    # *offered*; the propensity roll still decides. The sealed sentinel
    # (1e18 days) reproduces "never re-observe" byte-identically.
    reobservation_days: float = 1e18

    # --- the question↔self-thread bridge (S4, ADR-0015) --------------------
    # When a question is re-met by world material and a DORMANT self-thread's
    # basis memories resonate with the question's text (assoc_min_sim, the
    # same gate the association path always used), that thread is
    # re-activated — opportunity only, no thought is written. Default off.
    question_thread_bridge: bool = False

    # --- world threads (light) -------------------------------------------
    thread_prob: float = 0.30          # a world thought may seed a trackable thread
    max_world_threads: int = 3

    # --- the Question layer (v0.2 Step 3, ADR-0011) ------------------------
    # Default OFF: the sealed arms must stay byte-identical. When enabled, a
    # question is the residue of an unthought connection — never generated from
    # bare exposure, never self-rated, never answered by a pipeline. The
    # probabilities here are envelope rails in the same family as the ones
    # above, not tuned toward any target ratio.
    enable_questions: bool = False
    question_prob: float = 0.30              # a real connection that formed no thought may leave a question
    question_dormant_s: float = 7 * 86400.0  # untouched this long → derived "dormant" (death; no event)


#: arm B: on-topic + boundary exploration only.
WORLD_FOLLOW_EDGE: WorldParams = WorldParams(enabled_modes=("follow", "edge"))
#: arm C: all four entry modes.
WORLD_ALL: WorldParams = WorldParams(enabled_modes=_ENTRY_MODES)


# --------------------------------------------------------------------------- engine


class WorldWindowEngine:
    """Opens the World Window on awake beats. No state beyond the store + corpus +
    a seeded rng (its own instance, so it never disturbs the mind's rng stream)."""

    def __init__(
        self,
        store: EventStore,
        memory: MemoryRetrieval | None = None,
        corpus: WorldCorpus | None = None,
        params: WorldParams | None = None,
        rng: random.Random | None = None,
        *,
        source: WorldSource | None = None,
        question_rng: random.Random | None = None,
    ):
        self.store = store
        self.memory = memory or MemoryRetrieval(store)
        # v0.2 (ADR-0009): the world *source* is behind a seam — fixture (the
        # sealed corpus), replay, or live web. The engine's cognition is
        # identical for all three; ``corpus=`` still works and means fixture.
        self.corpus = corpus or DEFAULT_WORLD_CORPUS  # kept for back-compat reads
        self.source = source if source is not None else FixtureWorldSource(self.corpus)
        self.params = params or WorldParams()
        self.rng = rng or random.Random()
        # v0.2 Step 3 (ADR-0011): the question layer's own rng. None = share the
        # engine's stream (one life, one stream). Experiment arms pass a
        # dedicated stream so the question roll never perturbs the
        # window-selection trajectory — keeping the on/off comparison controlled.
        self._q_rng = question_rng if question_rng is not None else self.rng
        # item vectors are filled lazily (live sources gain items as pages are
        # observed); the embedder is deterministic, so laziness cannot change
        # any value — only *when* it is computed.
        self._item_vec: dict[str, list[float]] = {}

    # ------------------------------------------------------------------ main

    def maybe_open(self, now_iso: str, *, circadian_state: str | None = None) -> dict:
        """Consider opening the world window at reference time ``now_iso``.

        Returns a small summary dict (``result`` = "opened" | "skipped" | the
        pre-eligibility guard name). Only *eligible-but-declined* windows write a
        ``world.window_skipped`` event; the hard guards (asleep / budget /
        cooldown) are returned, not recorded (the window was not "on the table").
        """
        # ---- hard guards (cheap; no store scan beyond a couple of reads) ----
        if circadian_state in ASLEEP_STATES:
            return {"result": "asleep", "blocker": "asleep", "circadian_state": circadian_state}
        if self._count_opened_today(now_iso) >= self.params.max_light_per_day:
            return {"result": "budget_exhausted", "blocker": "budget_exhausted"}
        last_open = self.store.list(1, type_prefix="world.window_opened", order="desc")
        if last_open:
            t = _parse_iso(last_open[0].created_at)
            now = _parse_iso(now_iso)
            if t is not None and now is not None and (now - t).total_seconds() < self.params.cooldown_s:
                return {"result": "cooldown", "blocker": "cooldown"}

        # ---- eligible: now do the state read + the soft decider -------------
        state = MentalState.from_store(self.store)
        since_user_s = self._since_user_s(now_iso)
        opened_today = self._count_opened_today(now_iso)
        p = self._propensity(state, circadian_state, since_user_s, opened_today)
        eligibility = {
            "circadian_state": circadian_state,
            "since_user_h": round(since_user_s / 3600.0, 2) if since_user_s is not None else None,
            "active_threads": len(state.active_threads),
            "opened_today": opened_today,
            "light_budget": self.params.max_light_per_day,
            "propensity": round(p, 4),
        }
        roll = self.rng.random()
        if roll >= p:
            self._record_skip(now_iso, "propensity_below_threshold", {**eligibility, "roll": round(roll, 4)})
            return {"result": "skipped", "blocker": "propensity_below_threshold", **eligibility, "roll": round(roll, 4)}

        mode, item = self._select(state, now_iso)
        if item is None:
            self._record_skip(now_iso, "no_eligible_mode", eligibility)
            return {"result": "skipped", "blocker": "no_eligible_mode", **eligibility}

        return self._open(now_iso, mode, item, eligibility)

    # ------------------------------------------------------------- propensity

    def _propensity(self, state: MentalState, circadian_state: str | None,
                    since_user_s: float | None, opened_today: int) -> float:
        """One scalar in [0, cap]: the several signals (state, silence, focus,
        exposure) all move it, but it is *one* number feeding *one* roll — the
        explicit guard against a giant, rarely-true AND gate."""
        p = self.params.base_propensity
        if circadian_state in QUIET_STATES:
            p += self.params.quiet_bonus
        if since_user_s is None:
            p += self.params.silence_bonus_max  # never any user activity = very idle
        else:
            p += self.params.silence_bonus_max * min(1.0, since_user_s / self.params.silence_full_s)
        if state.active_threads:
            p -= self.params.focus_penalty
        exposure = opened_today / self.params.max_light_per_day
        p -= self.params.exposure_penalty_max * min(1.0, exposure)
        return max(0.0, min(self.params.propensity_cap, p))

    def _since_user_s(self, now_iso: str) -> float | None:
        last = self.store.list(1, type_prefix="conversation.user_message", order="desc")
        if not last:
            return None
        now = _parse_iso(now_iso)
        t = _parse_iso(last[0].created_at)
        if now is None or t is None:
            return None
        return max(0.0, (now - t).total_seconds())

    # ------------------------------------------------------------- selection

    def _select(self, state: MentalState, now_iso: str) -> tuple[str | None, WorldItem | None]:
        items = self.source.items()
        adj = self.source.topic_adjacency()
        obs = self.store.list(2000, type_prefix="world.observation", order="asc")
        # an item is ineligible while it was observed within the re-observation
        # horizon (sealed sentinel = never eligible again, byte-identical)
        last_obs: dict[str, str] = {}
        for e in obs:
            iid = e.content.get("item_id")
            if iid and (iid not in last_obs or e.created_at > last_obs[iid]):
                last_obs[iid] = e.created_at
        now_t = _parse_iso(now_iso)

        def _eligible(item_id: str) -> bool:
            ts = last_obs.get(item_id)
            if ts is None:
                return True
            t0 = _parse_iso(ts)
            if t0 is None or now_t is None:
                return False
            return (now_t - t0).total_seconds() >= self.params.reobservation_days

        obs_topics = {e.content.get("topic") for e in obs}
        known = obs_topics | self._focus_topics(state)
        neigh = self._neighbor_topics(known, adj) - known

        follow = [it for it in items if _eligible(it.id) and it.topic in known]
        edge = [it for it in items if _eligible(it.id) and it.topic in neigh]
        alien = [it for it in items
                 if _eligible(it.id) and it.topic not in known and it.topic not in neigh]
        accident: list[WorldItem] = []
        if obs:
            last_item = self.source.get(obs[-1].content.get("item_id"))
            if last_item is not None:
                accident = [t for t in self.source.outgoing_links(last_item) if _eligible(t.id)]

        opened_today = self._count_opened_today(now_iso)
        avail: dict[str, list[WorldItem]] = {}
        if "follow" in self.params.enabled_modes and opened_today < self.params.max_light_per_day and follow:
            avail["follow"] = follow
        if "edge" in self.params.enabled_modes and opened_today < self.params.max_light_per_day and edge:
            avail["edge"] = edge
        if ("alien" in self.params.enabled_modes
                and self._count_opened_today(now_iso, "alien") < self.params.max_alien_per_day and alien):
            avail["alien"] = alien
        if ("accident" in self.params.enabled_modes
                and self._count_opened_today(now_iso, "accident") < self.params.max_accident_per_day and accident):
            avail["accident"] = accident
        if not avail:
            return None, None
        mode = self._pick_mode(avail)
        return mode, self.rng.choice(avail[mode])

    def _pick_mode(self, avail: dict[str, list[WorldItem]]) -> str:
        total = sum(_MODE_WEIGHTS.get(m, 0.1) for m in avail)
        r = self.rng.random() * total
        acc = 0.0
        for m in avail:  # deterministic insertion order (follow, edge, alien, accident)
            acc += _MODE_WEIGHTS.get(m, 0.1)
            if r < acc:
                return m
        return next(reversed(avail))

    def _focus_topics(self, state: MentalState) -> set[str]:
        """The corpus topics the mind is *currently on* — from each active thread's
        newest substantive text and the latest user message (the dominant topical
        driver). This is what the **follow** mode reaches for."""
        topics: set[str] = set()
        for t in state.active_threads:
            topic = self._topic_of_text(self._thread_focus_text(t))
            if topic:
                topics.add(topic)
        last_user = self.store.last_user_interaction()
        if last_user is not None:
            topic = self._topic_of_text(last_user.content.get("text", ""))
            if topic:
                topics.add(topic)
        return topics

    def _topic_of_text(self, text: str) -> str | None:
        if not text or not text.strip():
            return None
        qv = self.memory.embedder.embed([_match_text(text)])[0]
        best_topic, best = None, 0.0
        for it in self.source.items():
            s = self.memory.embedder.cosine(qv, self._vec(it))
            if s > best:
                best, best_topic = s, it.topic
        return best_topic if best >= self.params.focus_threshold else None

    def _vec(self, item: WorldItem) -> list[float]:
        """Cached item embedding (deterministic; laziness only defers compute)."""
        vec = self._item_vec.get(item.id)
        if vec is None:
            vec = self.memory.embedder.embed([_match_text(item.text)])[0]
            self._item_vec[item.id] = vec
        return vec

    def _thread_focus_text(self, t: ThreadView) -> str:
        for eid in reversed(t.event_ids):
            e = self.store.get(eid)
            if e is None:
                continue
            if e.type in ("conversation.user_message", "experience.created"):
                txt = e.content.get("text") or e.content.get("summary") or e.text
                if txt:
                    return txt
        return t.title

    def _neighbor_topics(self, topics: set[str], adj: dict[str, set[str]]) -> set[str]:
        out: set[str] = set()
        for t in topics:
            out |= adj.get(t, set())
        return out

    # ------------------------------------------------------------------ open

    def _open(self, now_iso: str, mode: str, item: WorldItem, eligibility: dict) -> dict:
        prov = {"source": "world_window", "item_id": item.id, "item_source": item.source}
        # 1. the decision itself (system bookkeeping, like wake.route_selected)
        opened = self.store.append(EventCreate(
            type="world.window_opened", visibility="system",
            content={
                "entry_mode": mode, "item_id": item.id, "topic": item.topic, "source": item.source,
                "propensity": eligibility.get("propensity"),
                "since_user_h": eligibility.get("since_user_h"),
                "active_threads": eligibility.get("active_threads"),
                        "opened_today": eligibility.get("opened_today"),
                        "light_budget": eligibility.get("light_budget"),
                "circadian_state": eligibility.get("circadian_state"),
            },
            provenance={"source": "world_window"},
        ))
        # 2. materialize the candidate (v0.2: fixture = identity; live = a real,
        #    SSRF-guarded fetch + immutable snapshot; replay = recorded snapshot).
        #    A fetch failure is an *audit event*, never an exception into the
        #    mind: the window opened, looked, and the world did not answer —
        #    it legitimately leaves nothing (reading ≠ thought still holds).
        try:
            material = self.source.observe(
                item, now_iso=now_iso,
                context={"opened_event_id": opened.id, "entry_mode": mode})
        except FetchError as exc:
            self._record_fetch_error(opened, item, exc)
            return {"result": "fetch_failed", "blocker": exc.reason,
                    "entry_mode": mode, "item_id": item.id, "reason": exc.reason}
        item = material.item
        if material.fetch is not None:
            prov["fetch"] = dict(material.fetch)
        # 3. the PROVENANCE ROOT — a neutral observed fact (never a viewpoint)
        obs = self.store.append(EventCreate(
            type="world.observation", visibility="private",
            content={"item_id": item.id, "source": item.source, "topic": item.topic,
                     "title": item.title, "summary": item.summary, "entry_mode": mode},
            links={"caused_by": [opened.id]},
            provenance=prov, metadata={"mode": mode, "origin": "world"},
        ))
        exp = self.store.append(EventCreate(
            type="world.experience", visibility="private",
            content={"summary": f"浏览「{item.title}」（{item.source}）：{item.summary}",
                     "topic": item.topic, "source": item.source, "item_id": item.id},
            links={"caused_by": [obs.id]},
            provenance=prov, metadata={"origin": "world", "mode": mode},
        ))
        # 4. a weak impression (the Impression layer — most readings leave only this)
        impression = None
        if self.rng.random() < self.params.impression_prob:
            impression = self.store.append(EventCreate(
                type="impression.formed", visibility="private",
                content={"impression": f"对「{item.title}」留下了一点模糊印象（{item.topic}）",
                         "item_id": item.id, "topic": item.topic, "source": item.source},
                links={"caused_by": [exp.id], "related_to": [obs.id]},
                provenance=prov, metadata={"origin": "world", "promoted": False, "mode": mode},
            ))
        # 5. a durable thought, only on a real connection (reading ≠ thought)
        thought, kind = self._maybe_thought(item, obs, mode)
        # 6. v0.2 Step 3 (ADR-0011): the QUESTION layer — strictly additive,
        #    default OFF. (a) this experience may re-meet an open question
        #    (honest bookkeeping, like thread.revisited); (b) an open that formed
        #    NO thought may leave a question — but only where a thought could
        #    have formed (real connection evidence) and only on a further
        #    low-prob roll. Most observations still leave only an impression.
        out = {
            "result": "opened", "entry_mode": mode, "item_id": item.id, "topic": item.topic,
            "source": item.source, "observation_id": obs.id, "experience_id": exp.id,
            "impression_id": impression.id if impression else None,
            "thought_id": thought.id if thought else None, "thought_kind": kind,
            "propensity": eligibility.get("propensity"),
        }
        if self.params.enable_questions:
            revisited = self._touch_open_questions(item, obs)
            question = None
            if thought is None and not self._deep_cap_reached(obs.created_at):
                question = self._maybe_question(item, obs, mode)
            out.update({
                "question_id": question.id if question else None,
                "question_kind": question.content.get("kind") if question else None,
                "questions_revisited": revisited,
            })
        return out

    # ---------------------------------------------------------- thoughts

    def _maybe_thought(self, item: WorldItem, obs, mode: str) -> tuple[object | None, str | None]:
        """A world thought forms only on a real connection. (a) a stored impression
        this observation re-activates (the Impression layer), or (b) an association
        with an *old, non-world* memory. Otherwise: reading left only an impression."""
        if self._count_world_thoughts_today(obs.created_at) >= self.params.max_deep_per_day:
            return None, None
        prior = self._find_reactivable_impression(item, obs)
        if prior is not None and self.rng.random() < self.params.promotion_prob:
            thought = self._form_reactivation_thought(item, obs, prior, mode)
            return thought, "reactivation"
        if self.rng.random() < self.params.association_prob:
            assoc = self._recall_related_old_memory(item)
            if assoc is not None:
                thought = self._form_association_thought(item, obs, assoc, mode)
                return thought, "association"
        return None, None

    def _find_reactivable_impression(self, item: WorldItem, obs) -> object | None:
        """The oldest un-promoted impression this item re-activates (same topic, or a
        corpus-linked item). None if the new material connects to nothing stored."""
        connected = {it.topic for it in self.source.outgoing_links(item)} | {item.topic}
        connected_ids = {it.id for it in self.source.outgoing_links(item)} | {item.id}
        promoted = {e.content.get("impression_id") for e in self.store.list(2000, type_prefix="impression.promoted")}
        cands = []
        for imp in self.store.list(2000, type_prefix="impression.formed", order="asc"):
            if imp.id in promoted or (imp.metadata or {}).get("promoted"):
                continue
            if imp.created_at >= obs.created_at:
                continue  # must be a PRIOR impression (a re-encounter, not the same one)
            if imp.content.get("topic") in connected or imp.content.get("item_id") in connected_ids:
                cands.append(imp)
        return cands[0] if cands else None

    def _form_reactivation_thought(self, item: WorldItem, obs, prior, mode: str):
        old_obs_id = (prior.links.get("related_to") or [None])[0]
        old_obs = self.store.get(old_obs_id) if old_obs_id else None
        promoted = self.store.append(EventCreate(
            type="impression.promoted", visibility="private",
            content={"impression_id": prior.id, "reason": "reactivated by re-encounter",
                     "old_item": prior.content.get("item_id"), "new_item": item.id},
            links={"caused_by": [obs.id], "related_to": [prior.id] + ([old_obs.id] if old_obs else [])},
            provenance={"source": "world_window"}, metadata={"origin": "world"},
        ))
        related = [obs.id, prior.id] + ([old_obs.id] if old_obs else [])
        old_title = old_obs.content.get("title") if old_obs is not None else item.title
        text = (
            f"看到「{item.title}」（{item.topic}）时，我忽然想起之前也留意过「"
            f"{old_title}」。两次相隔，再看时我注意到的点不一样了——"
            "这一点让我停下来想一想。"
        )
        thought = self.store.append(EventCreate(
            type="thought.created", visibility="private",
            content={"text": text},
            links={"caused_by": [obs.id, promoted.id], "related_to": related},
            provenance={"source": "world_window", "route": "world"},
            metadata={"route": "world", "origin": "world",
                      "recalled": [old_obs.id] if old_obs else [],
                      "world": {"mode": mode, "kind": "reactivation",
                                "old_item": prior.content.get("item_id")}},
        ))
        self._maybe_world_thread(item, thought)
        return thought

    def _recall_related_old_memory(self, item: WorldItem):
        """A non-world (i.e. the user's / the mind's own) memory this material
        genuinely associates with. World events are excluded, so this is the
        "world + old memory" connection, not world echoing world. A wide net is
        recalled (then filtered) because the resident's own prior world
        observations — near-duplicates of the item — otherwise crowd the old
        memory out of the top-k."""
        for h in self.memory.semantic_near(item.text, n=30):
            e = h.event
            if e.family == "world" or (e.metadata or {}).get("origin") == "world":
                continue
            if h.score >= self.params.assoc_min_sim:
                return e
        return None

    def _form_association_thought(self, item: WorldItem, obs, assoc, mode: str):
        text = (
            f"看到「{item.title}」（{item.topic}，来自 {item.source}）时，我想起了自己记下的"
            f"「{assoc.text[:32]}」——它们似乎都指向同一个地方，值得记一笔。"
        )
        thought = self.store.append(EventCreate(
            type="thought.created", visibility="private",
            content={"text": text},
            links={"caused_by": [obs.id], "related_to": [obs.id, assoc.id]},
            provenance={"source": "world_window", "route": "world"},
            metadata={"route": "world", "origin": "world", "recalled": [assoc.id],
                      "world": {"mode": mode, "kind": "association", "assoc_event": assoc.id}},
        ))
        self._maybe_world_thread(item, thought)
        return thought

    def _maybe_world_thread(self, item: WorldItem, thought) -> None:
        """A world thought may seed (or re-visit) a trackable world thread. Capped,
        low-probability — a world topic is *allowed* to become a private thread, not
        forced to. (Feeds world_thread_creation / revisit_rate.)"""
        existing = [e for e in self.store.list(500, type_prefix="thread.created")
                    if e.content.get("origin") == "world"]
        same_topic = [e for e in existing if e.content.get("topic") == item.topic]
        if same_topic:
            tid = same_topic[0].content.get("thread_id")
            self.store.append(EventCreate(
                type="thread.revisited", visibility="private",
                content={"thread_id": tid, "reason": "world re-encounter"},
                links={"thread_id": tid, "caused_by": [thought.id]},
                provenance={"source": "world_window"},
            ))
            return
        if len(existing) >= self.params.max_world_threads:
            return
        if self.rng.random() < self.params.thread_prob:
            tid = f"thr_world_{item.topic}"
            c = self.store.append(EventCreate(
                type="thread.created", visibility="private",
                content={"thread_id": tid, "title": item.title, "topic": item.topic, "origin": "world"},
                links={"caused_by": [thought.id]},
                provenance={"source": "world_window", "item_id": item.id},
            ))
            self.store.append(EventCreate(
                type="thread.activated", visibility="private",
                content={"thread_id": tid},
                links={"thread_id": tid, "caused_by": [c.id]},
                provenance={"source": "world_window"},
            ))

    # ---------------------------------------------------------- questions
    # (v0.2 Step 3, ADR-0011 — World → Question)

    def _deep_cap_reached(self, now_iso: str) -> bool:
        """The deep-read cap (same counter ``_maybe_thought`` uses). A capped
        mind stops engaging with the material — satiety includes not asking:
        a cap-blocked open leaves an impression, not a question."""
        return self._count_world_thoughts_today(now_iso) >= self.params.max_deep_per_day

    def _maybe_question(self, item: WorldItem, obs, mode: str):
        """A question is the RESIDUE of an unthought connection. The candidate
        detection is the SAME evidence class a world thought requires — a prior
        un-promoted impression this item re-activates, or an own old-memory
        association — never bare exposure, never generation from nothing.
        Priority: tension (world↔world) before gap (world↔own memory); exactly
        one low-prob roll; declined → nothing (seen but left nothing)."""
        prior = self._find_reactivable_impression(item, obs)
        if prior is not None:
            if self._q_rng.random() < self.params.question_prob:
                old_obs_id = (prior.links.get("related_to") or [None])[0]
                old_obs = self.store.get(old_obs_id) if old_obs_id else None
                evidence = [prior] + ([old_obs] if old_obs is not None else [])
                return self._form_question(item, obs, mode, "tension", evidence)
            return None
        assoc = self._recall_related_old_memory(item)
        if assoc is not None and self._q_rng.random() < self.params.question_prob:
            return self._form_question(item, obs, mode, "gap", [assoc])
        return None

    def _form_question(self, item: WorldItem, obs, mode: str, kind: str, evidence: list):
        """The question text is a grounded template over the actual evidence —
        the engine owns existence AND wording (a model may later re-word, never
        decide). No importance/curiosity score exists anywhere: a question's
        value shows up only as later re-meetings (ADR-0011, constraint 5)."""
        if kind == "tension":
            old = next((e for e in evidence if e.type == "world.observation"), None)
            old_title = old.content.get("title") if old is not None else item.title
            old_source = old.content.get("source") if old is not None else item.source
            # name the relation honestly: the reactivation candidate may be
            # same-topic OR corpus-linked (adjacent) — the wording must not
            # claim more connection than the evidence carries
            old_topic = old.content.get("topic") if old is not None else item.topic
            relation = ("是同一个话题" if old_topic == item.topic
                        else "是相关联的两个话题")
            text = (
                f"「{item.title}」（{item.source}）和之前遇见过的「{old_title}」（{old_source}）"
                f"{relation}。两次经历摆在一起，我还没有想清楚它们的关系——"
                "为什么它们会同时成立？"
            )
        else:
            assoc = evidence[0]
            text = (
                f"看到「{item.title}」（{item.topic}），它和我记下的「{(assoc.text or '')[:32]}」"
                "撞在了一起。这两件事为什么会相关？我说不清它们的关系。"
            )
        ev_ids = [e.id for e in evidence]
        return self.store.append(EventCreate(
            type="question.created", visibility="private",
            content={"question": text, "kind": kind, "topic": item.topic,
                     "item_id": item.id, "source": item.source, "status": "open"},
            links={"caused_by": [obs.id], "related_to": ev_ids},
            provenance={"source": "world_window", "route": "world", "question_kind": kind},
            metadata={"route": "world", "origin": "world", "mode": mode,
                      "question": {"kind": kind, "evidence": ev_ids}},
        ))

    def _touch_open_questions(self, item: WorldItem, obs) -> list[str]:
        """ADR-0011 constraint 4: a later, INDEPENDENT experience that hits an
        open question's topic re-meets it — recorded, never scored (salience is
        the derived citation graph). Dormant questions are not touched (they
        have died; the window cannot resurrect them)."""
        touched: list[str] = []
        state = question_state(self.store, now_iso=obs.created_at,
                               dormant_s=self.params.question_dormant_s)
        for q in state["questions"]:
            if q["status"] != "open" or q["topic"] != item.topic:
                continue
            if q["created_at"] >= obs.created_at:
                continue  # cannot re-meet a question from this very open
            self.store.append(EventCreate(
                type="question.revisited", visibility="private",
                content={"question_id": q["question_id"], "topic": item.topic,
                         "item_id": item.id, "reason": "world re-encounter"},
                links={"caused_by": [obs.id], "related_to": [q["question_id"]]},
                provenance={"source": "world_window"},
            ))
            # S4 (ADR-0015): the material bridge. A question re-met by world
            # material may resonate with a DORMANT self-thread's basis
            # memories — that thread is re-activated (opportunity only: it
            # becomes continuable; nothing is thought for it). This is how
            # questions and the self start revisiting each other.
            qev = self.store.get(q["question_id"])
            if qev is not None:
                self._bridge_question_to_self_threads(qev, self.store.list(
                    1, type_prefix="question.revisited", order="desc")[0])
            touched.append(q["question_id"])
        return touched

    def _bridge_question_to_self_threads(self, question, revisit_event) -> None:
        """S4 (ADR-0015): re-activate dormant self-threads whose basis
        memories resonate with the re-met question's text. Structural, not a
        score: the gate is the same ``assoc_min_sim`` the association path
        always used. No thought is written here — the thread merely becomes
        continuable again, with provenance to the re-encounter."""
        if not self.params.question_thread_bridge:
            return
        q_text = question.content.get("question") or ""
        if not q_text.strip():
            return
        qv = self.memory.embedder.embed([q_text])[0]
        lifecycle: dict[str, str] = {}
        basis: dict[str, list[str]] = {}
        for e in self.store.list(3000, order="asc"):
            if e.type == "thread.created":
                if e.content.get("origin") == "self":
                    tid = e.content.get("thread_id")
                    lifecycle[tid] = "created"
                    basis[tid] = list((e.provenance or {}).get("derived_from") or [])
            elif e.type in ("thread.activated", "thread.revisited"):
                tid = e.content.get("thread_id") or (e.links or {}).get("thread_id")
                if tid:
                    lifecycle[tid] = "active"
            elif e.type == "thread.dormant":
                tid = e.content.get("thread_id") or (e.links or {}).get("thread_id")
                if tid:
                    lifecycle[tid] = "dormant"
        for tid, st in lifecycle.items():
            if st != "dormant":
                continue
            best = 0.0
            for bid in basis.get(tid, []):
                b = self.store.get(bid)
                if b is None or not (b.text or "").strip():
                    continue
                bv = self.memory.embedder.embed([b.text])[0]
                best = max(best, self.memory.embedder.cosine(qv, bv))
            if best >= self.params.assoc_min_sim:
                self.store.append(EventCreate(
                    type="thread.activated", visibility="private",
                    content={"thread_id": tid, "reason": "question re-encounter"},
                    # the provenance bridge: the thread's own record now points
                    # at the question that re-woke it, so the next thought on
                    # this thread can genuinely cite the question material
                    links={"thread_id": tid, "caused_by": [revisit_event.id],
                           "related_to": [question.id]},
                    provenance={"source": "world_window"},
                ))

    # ------------------------------------------------------------- bookkeeping

    def _record_skip(self, now_iso: str, blocker: str, eligibility: dict) -> None:
        self.store.append(EventCreate(
            type="world.window_skipped", visibility="system",
            content={"blocker": blocker, **eligibility},
            provenance={"source": "world_window"},
        ))

    def _record_fetch_error(self, opened, item: WorldItem, exc: FetchError) -> None:
        """v0.2 (ADR-0009): a failed/blocked/timed-out fetch is an audit event,
        not an exception. The window opened and looked; the world did not
        answer. The light-budget slot is already spent (a real request may have
        been made) — honest accounting, per the brief's "记录真实 HTTP 请求数量".
        These events are *not* experience: the mind's route context excludes
        them (mind_loop._route_context)."""
        event_type = {
            "fetch_blocked": "world.fetch_blocked",
            "fetch_timeout": "world.fetch_timeout",
        }.get(exc.reason, "world.fetch_failed")
        self.store.append(EventCreate(
            type=event_type, visibility="system",
            content={"blocker": exc.reason, "reason": exc.reason,
                     "item_id": item.id, "detail": exc.detail},
            links={"caused_by": [opened.id]},
            provenance={"source": "world_window"},
        ))

    def _count_opened_today(self, now_iso: str, mode: str | None = None) -> int:
        day = now_iso[:10]
        n = 0
        for e in self.store.list(500, type_prefix="world.window_opened", order="asc"):
            if e.created_at[:10] != day:
                continue
            if mode is None or e.content.get("entry_mode") == mode:
                n += 1
        return n

    def _count_world_thoughts_today(self, now_iso: str) -> int:
        day = now_iso[:10]
        n = 0
        for t in self.store.list(500, type_prefix="thought.created", order="asc"):
            if t.created_at[:10] != day:
                continue
            if (t.metadata or {}).get("origin") == "world":
                n += 1
        return n


# --------------------------------------------------------------------------- telemetry


#: untouched this long → a question is derived-dormant (ADR-0011). An envelope
#: rail for "questions can die", not a tuned value.
DEFAULT_QUESTION_DORMANT_S = 7 * 86400.0


def question_state(
    store: EventStore,
    *,
    now_iso: str | None = None,
    dormant_s: float = DEFAULT_QUESTION_DORMANT_S,
) -> dict:
    """Derived question lifecycle (ADR-0011). Death is a DERIVED state: a
    question with no ``question.revisited`` for ``dormant_s`` is "dormant" —
    no tombstone event is ever written (the log stays append-only and honest).
    Salience is likewise derived (revisit count + last touch), never stored,
    never self-rated. ``now_iso=None`` reads the store's own latest event."""
    created = store.list(5000, type_prefix="question.created", order="asc")
    revisits = store.list(5000, type_prefix="question.revisited", order="asc")
    by_q: dict[str, list] = {}
    for r in revisits:
        by_q.setdefault(r.content.get("question_id"), []).append(r)
    now = _parse_iso(now_iso)
    if now is None:
        latest = store.latest()
        now = _parse_iso(latest.created_at) if latest is not None else None
    items: list[dict] = []
    for q in created:
        rv = by_q.get(q.id, [])
        last_touch = max([q.created_at] + [r.created_at for r in rv])
        status = "open"
        lt = _parse_iso(last_touch)
        if now is not None and lt is not None and (now - lt).total_seconds() > dormant_s:
            status = "dormant"
        items.append({
            "question_id": q.id, "kind": q.content.get("kind"),
            "topic": q.content.get("topic"), "text": q.content.get("question"),
            "created_at": q.created_at, "last_touched_at": last_touch,
            "revisits": len(rv), "status": status,
            "evidence": list((q.metadata or {}).get("question", {}).get("evidence") or []),
        })
    n_open = sum(1 for i in items if i["status"] == "open")
    return {"questions": items, "n_total": len(items),
            "n_open": n_open, "n_dormant": len(items) - n_open}


def world_profile(
    store: EventStore,
    *,
    corpus: WorldCorpus | None = None,
    since_iso: str | None = None,
    now_iso: str | None = None,
    question_dormant_s: float = DEFAULT_QUESTION_DORMANT_S,
) -> dict:
    """Derived world-window telemetry — every value read from the log (provenance-
    complete, reproducible). Mirrors the phase brief's metric list. A no-world store
    (arm A) yields all-zeroes / 0.0, which is the honest reading for "no world".

    ``since_iso`` / ``now_iso`` optionally restrict the read to events created in
    ``[since_iso, now_iso]`` (ISO string comparison, the same convention the
    store's windowed reads use); with both unset it reads the whole life so far."""
    corpus = corpus or DEFAULT_WORLD_CORPUS  # noqa: F841 (kept for a per-corpus topic set if needed)

    def _win(e) -> bool:
        if since_iso is None and now_iso is None:
            return True
        ts = e.created_at
        if since_iso is not None and ts < since_iso:
            return False
        if now_iso is not None and ts > now_iso:
            return False
        return True

    opened = [e for e in store.list(5000, type_prefix="world.window_opened", order="asc") if _win(e)]
    skipped = [e for e in store.list(5000, type_prefix="world.window_skipped", order="asc") if _win(e)]
    obs = [e for e in store.list(5000, type_prefix="world.observation", order="asc") if _win(e)]
    impressions = [e for e in store.list(5000, type_prefix="impression.formed", order="asc") if _win(e)]
    promoted = [e for e in store.list(5000, type_prefix="impression.promoted", order="asc") if _win(e)]
    all_thoughts = [t for t in store.list(5000, type_prefix="thought.created", order="asc") if _win(t)]
    world_thoughts = [t for t in all_thoughts if (t.metadata or {}).get("origin") == "world"]
    world_threads = [e for e in store.list(2000, type_prefix="thread.created")
                     if e.content.get("origin") == "world" and _win(e)]
    world_revisits = [e for e in store.list(2000, type_prefix="thread.revisited")
                      if e.content.get("reason") == "world re-encounter" and _win(e)]
    # v0.2 (ADR-0009): real-fetch audit. Observations carry provenance["fetch"]
    # (live/replay only); failures are world.fetch_* events. All-zero for the
    # sealed fixture path, so the Phase-6 telemetry keys are untouched.
    fetch_meta = [e.provenance.get("fetch") for e in obs
                  if isinstance(e.provenance.get("fetch"), dict)]
    fetch_errors = [e for e in store.list(5000, type_prefix="world.fetch_", order="asc")
                    if _win(e)]
    attempts = len(fetch_meta) + len(fetch_errors)
    reasons = {}
    for e in fetch_errors:
        reasons[e.content.get("reason")] = reasons.get(e.content.get("reason"), 0) + 1
    replay_hits = sum(1 for f in fetch_meta if f.get("replay"))

    n_opened, n_skipped = len(opened), len(skipped)
    days = {e.created_at[:10] for e in opened}
    n_days = len(days)

    mode_dist: dict[str, int] = {}
    for e in obs:
        m = e.content.get("entry_mode")
        mode_dist[m] = mode_dist.get(m, 0) + 1
    blocker_dist: dict[str, int] = {}
    for e in skipped:
        b = e.content.get("blocker")
        blocker_dist[b] = blocker_dist.get(b, 0) + 1

    n_wt = len(world_thoughts)
    n_wt_plus_mem = 0
    for t in world_thoughts:
        rel = t.links.get("related_to") or []
        if isinstance(rel, str):
            rel = [rel]
        for rid in rel:
            ev = store.get(rid)
            if ev is not None and ev.family != "world" and (ev.metadata or {}).get("origin") != "world":
                n_wt_plus_mem += 1
                break

    # v0.2 Step 3 (ADR-0011): the question layer. Counts are window-scoped like
    # the sibling metrics; the lifecycle split is whole-life as of ``now_iso``.
    # All-zero when the layer is off, so the Phase-6 keys are untouched.
    questions = [e for e in store.list(5000, type_prefix="question.created", order="asc") if _win(e)]
    q_revisits = [e for e in store.list(5000, type_prefix="question.revisited", order="asc") if _win(e)]
    q_kind_dist: dict[str, int] = {}
    for q in questions:
        k = q.content.get("kind")
        q_kind_dist[k] = q_kind_dist.get(k, 0) + 1
    q_survived = len({r.content.get("question_id") for r in q_revisits})
    q_state = question_state(store, now_iso=now_iso, dormant_s=question_dormant_s)

    # alien survival: an alien observation that was later revisited (a later obs on
    # the same topic) or whose impression was promoted (a re-encounter days later)
    alien_obs = [e for e in obs if e.content.get("entry_mode") == "alien"]
    alien_survived = 0
    for e in alien_obs:
        topic = e.content.get("topic")
        survived = any(o.created_at > e.created_at and o.content.get("topic") == topic for o in obs)
        if not survived:
            for p in promoted:
                if e.id in (p.links.get("related_to") or []):
                    survived = True
                    break
        if survived:
            alien_survived += 1

    # accident chain depth: the longest run of consecutive accident-mode opens
    modes_seq = [e.content.get("entry_mode") for e in opened]
    best_run = cur = 0
    for m in modes_seq:
        cur = cur + 1 if m == "accident" else 0
        best_run = max(best_run, cur)

    src_counts: dict[str, int] = {}
    topic_counts: dict[str, int] = {}
    for e in obs:
        src_counts[e.content.get("source")] = src_counts.get(e.content.get("source"), 0) + 1
        topic_counts[e.content.get("topic")] = topic_counts.get(e.content.get("topic"), 0) + 1
    src_conc = (max(src_counts.values()) / n_opened) if (n_opened and src_counts) else 0.0
    topic_conc = (max(topic_counts.values()) / n_opened) if (n_opened and topic_counts) else 0.0

    base = len(all_thoughts)
    user_d = sum(1 for t in all_thoughts if (t.metadata or {}).get("origin") in USER_ORIGINS)
    self_o = sum(1 for t in all_thoughts if (t.metadata or {}).get("origin") in SELF_ORIGINS)

    return {
        "n_windows_opened": n_opened,
        "n_windows_skipped": n_skipped,
        "n_eligible": n_opened + n_skipped,
        "n_days_with_activity": n_days,
        "world_exposure_rate": round(n_opened / n_days, 3) if n_days else 0.0,
        "world_to_thought_rate": round(n_wt / n_opened, 3) if n_opened else 0.0,
        "world_to_question_rate": round(len(questions) / n_opened, 3) if n_opened else 0.0,
        "world_noop_rate": round((n_opened - n_wt) / n_opened, 3) if n_opened else 0.0,
        "seen_left_nothing_rate": round(1 - n_wt / n_opened, 3) if n_opened else 0.0,
        "entry_mode_distribution": mode_dist,
        "skip_blocker_distribution": blocker_dist,
        "n_world_thoughts": n_wt,
        "n_world_plus_memory_thoughts": n_wt_plus_mem,
        "cross_source_association_rate": round(n_wt_plus_mem / n_wt, 3) if n_wt else 0.0,
        "n_impressions": len(impressions),
        "n_promoted": len(promoted),
        "impression_promotion_rate": round(len(promoted) / len(impressions), 3) if impressions else 0.0,
        "alien_survival_rate": round(alien_survived / len(alien_obs), 3) if alien_obs else 0.0,
        "accident_max_chain_depth": best_run,
        "n_world_threads": len(world_threads),
        "world_thread_creation_rate": round(len(world_threads) / n_opened, 3) if n_opened else 0.0,
        "world_thread_revisit_rate": round(len(world_revisits) / n_opened, 3) if n_opened else 0.0,
        "source_concentration": round(src_conc, 3),
        "topic_concentration": round(topic_conc, 3),
        "source_distribution": src_counts,
        "topic_distribution": topic_counts,
        "origin_shares": {
            "user_derived": round(user_d / base, 3) if base else 0.0,
            "self_origin": round(self_o / base, 3) if base else 0.0,
            "world_origin": round(len(world_thoughts) / base, 3) if base else 0.0,
            "base": base,
        },
        # v0.2 Step 3 (ADR-0011): question telemetry (additive; all-zero when off)
        "questions": {
            "n_questions": len(questions),
            "kind_distribution": q_kind_dist,
            "n_revisits": len(q_revisits),
            # "survival" = re-met later by an independent experience — NOT answered
            "question_survival_rate": round(q_survived / len(questions), 3) if questions else 0.0,
            "open_now": q_state["n_open"],
            "dormant_now": q_state["n_dormant"],
        },
        # v0.2 live/replay fetch telemetry (additive; fixture = all zeros)
        "live": {
            "source_kind": fetch_meta[0].get("source_kind") if fetch_meta else None,
            "live_fetch_attempts": attempts,
            "live_fetch_success_rate": round(len(fetch_meta) / attempts, 3) if attempts else 0.0,
            "fetch_to_observation_rate": round(len(fetch_meta) / attempts, 3) if attempts else 0.0,
            "blocked_fetches": reasons.get("fetch_blocked", 0),
            "timeout_rate": round(reasons.get("fetch_timeout", 0) / attempts, 3) if attempts else 0.0,
            "dynamic_page_rejection_rate": round(reasons.get("unsupported_content", 0) / attempts, 3) if attempts else 0.0,
            "bytes_received": sum(int(f.get("byte_size") or 0) for f in fetch_meta),
            "redirect_count": sum(int(f.get("redirect_count") or 0) for f in fetch_meta),
            "snapshot_count": len({f.get("snapshot_id") for f in fetch_meta if f.get("snapshot_id")}),
            "replay_hit_rate": round(replay_hits / attempts, 3) if attempts else 0.0,
            "failure_reason_distribution": reasons,
        },
    }
