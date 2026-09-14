"""Resident v0.1 backend: FastAPI wiring for the whole resident body.

The app is built by :func:`build_app`, which creates its own store /
memory / mind loop / re-entry engine / scheduler for a given data directory.
The module-level ``app`` (for ``uvicorn resident.main:app``) is
``build_app()`` against the default resident home.
"""
from __future__ import annotations

import json
import os
import random
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .circadian import CircadianOrchestrator
from .continuity import ContinuityEngine
from .event_store import EventStore
from .monitor import (
    monitor_continuity,
    monitor_projects,
    monitor_provenance,
    monitor_questions,
    monitor_retrieval_trace,
    monitor_threads,
    monitor_timeline,
)
from .mind_loop import MindLoop, SYSTEM_PROMPT
from .memory import MemoryRetrieval
from .mental_state import MentalState
from .models import ChatRequest, EventCreate
from .providers import AnthropicMessagesProvider, DeterministicFakeProvider, ModelProvider, ProviderError
from .reentry import ReentryEngine
from .scheduler import CircadianScheduler, WakeScheduler
from .self_revival import RevivalParams
from .sleep import SleepEngine
from .telemetry import Telemetry
from .web_fetch import SafeFetcher
from .world import WORLD_ALL, WorldWindowEngine
from .world_corpus import DEFAULT_WORLD_CORPUS

DATA = Path(os.environ.get("RESIDENT_HOME", str(Path(__file__).resolve().parents[3] / "resident_home")))


def _make_provider() -> ModelProvider:
    """Select the model provider from EXPLICIT environment config only.

    The real provider (Anthropic Messages) is enabled only when
    ``RESIDENT_MODEL_BASE_URL``, ``RESIDENT_MODEL_API_KEY`` and
    ``RESIDENT_MODEL_NAME`` are ALL set. There is deliberately no silent
    fallback to ANTHROPIC_* session variables: the default must stay the
    deterministic fake so tests remain hermetic.
    """
    base_url = os.environ.get("RESIDENT_MODEL_BASE_URL")
    api_key = os.environ.get("RESIDENT_MODEL_API_KEY")
    model = os.environ.get("RESIDENT_MODEL_NAME")
    if base_url and api_key and model:
        return AnthropicMessagesProvider(
            base_url=base_url, model=model, api_key=api_key,
            # reflection prompts are far larger than chat prompts; a small
            # relay can legitimately need minutes — env-tunable
            timeout=float(os.environ.get("RESIDENT_MODEL_TIMEOUT", "30")),
        )
    return DeterministicFakeProvider()


def _make_embedding_stack(store: EventStore, data: Path):
    """v0.2 Step 2 (ADR-0010): build the semantic stack from EXPLICIT env.

    ``RESIDENT_EMBEDDINGS=semantic`` switches the treatment on: the mind's
    retrieval runs on real (or sketch, in experiments) vectors through the
    identity-checked vector cache, with bridge distant / mid-band serendipity /
    cluster concentration. ``RESIDENT_SEMANTIC_SHADOW=1`` (legacy main only)
    attaches the audit-only shadow layer to the MindLoop. Provider config is
    OpenAI-compatible via ``RESIDENT_EMBEDDING_*``; ``sketch`` (default when no
    endpoint is configured) is the deterministic hermetic provider, honestly
    labelled ``local/sketch-semantic-v1``.

    Returns ``(shadow_layer | None, treatment_memory_kwargs | None)`` — the
    audit-only shadow layer for the MindLoop, and the MemoryRetrieval kwargs
    when the treatment is on.
    """
    from .semantic import emit_degraded, make_semantic_stack

    embeddings = os.environ.get("RESIDENT_EMBEDDINGS", "legacy").strip().lower()
    shadow_on = os.environ.get("RESIDENT_SEMANTIC_SHADOW", "0") != "0"
    if embeddings != "semantic" and not shadow_on:
        return None, None

    def _provider():
        from .providers import OpenAICompatibleEmbeddingProvider

        base = os.environ.get("RESIDENT_EMBEDDING_BASE_URL")
        model = os.environ.get("RESIDENT_EMBEDDING_MODEL")
        key = os.environ.get("RESIDENT_EMBEDDING_API_KEY")
        if base and model and key:
            return OpenAICompatibleEmbeddingProvider(
                base_url=base, model=model, api_key=key)
        from .providers import DeterministicSemanticEmbeddingProvider

        return DeterministicSemanticEmbeddingProvider()

    return make_semantic_stack(
        store, os.environ.get("RESIDENT_VECTOR_CACHE", str(data / "vectors.sqlite3")),
        mode=embeddings, shadow=shadow_on, provider=_provider(),
        # the shadow sidecar lives OUTSIDE the event store (never the log);
        # without a path the live app's shadow would record into nowhere
        shadow_log_path=os.environ.get("RESIDENT_SHADOW_LOG") or str(data / "semantic_shadow.jsonl"),
        on_degraded=lambda reason: emit_degraded(  # noqa: E731 (explicit, audited)
            store, reason, context={"mode": "treatment"}),
    )


class WakeRequest(BaseModel):
    trigger: Optional[str] = None


class ReentryRequest(BaseModel):
    since: Optional[str] = None


def _route_distribution(store: EventStore) -> dict[str, int]:
    dist: dict[str, int] = {}
    for e in store.list(10_000, type_prefix="wake.route_selected"):
        r = e.content.get("route")
        if r:
            dist[r] = dist.get(r, 0) + 1
    return dist


def build_app(data_dir: Path | str | None = None) -> FastAPI:
    """Create a fully wired Resident app with its own storage under data_dir."""
    data = Path(data_dir) if data_dir is not None else DATA
    store = EventStore(data / "events.sqlite3")
    provider = _make_provider()
    # v0.2 Step 2 (ADR-0010): the semantic stack — treatment (real-vector
    # retrieval) and/or the audit-only shadow layer. Default: both off, memory
    # byte-identical to the sealed legacy.
    shadow_layer, semantic_memory_kwargs = _make_embedding_stack(store, data)
    memory = MemoryRetrieval(store, **(semantic_memory_kwargs or {}))
    # Phase 5: the mind's dormant self-thread revival bias is ON by default (the
    # "Resident with a rhythm" state); the circadian recognises genuine quiet and
    # thereby *raises the opportunity* — it never forces a selection. Env-override
    # to compare against the A/B control (off).
    self_revival_on = os.environ.get("RESIDENT_SELF_REVIVAL", "1") != "0"
    mind = MindLoop(
        store, memory, provider,
        self_revival=self_revival_on,
        revival=RevivalParams() if self_revival_on else None,
        shadow_semantic=shadow_layer,
    )
    engine = ReentryEngine(store, memory)
    # v0.2 Step 4 (ADR-0012): the continuity layer — the fact layer of an
    # absence (absence.detected / reentry.candidate / reentry.selected|noop).
    # Default off (the sealed behaviour); explicit opt-in via RESIDENT_CONTINUITY.
    continuity_engine = (
        ContinuityEngine(store, memory=memory)
        if os.environ.get("RESIDENT_CONTINUITY", "0") != "0" else None
    )
    sleep_engine = SleepEngine(store, memory=memory, provider=provider)
    telemetry = Telemetry(store, memory.index)
    # Phase 6 (ADR-0008): the World Window — external material as a source of
    # *experience*, not task. Strictly opt-in (default OFF) so the live app and
    # hermetic tests stay unchanged; the corpus is a fixed, in-code, hermetic set
    # (no network). Its OWN rng keeps the mind's draw sequence byte-identical
    # regardless of whether the window opens.
    world_engine = None
    # v0.2 Active Living (ADR-0014): RESIDENT_PROFILE selects the opportunity
    # tier (wake cadence + world rails). Opportunity only — cognitive
    # criteria are untouched by profiles by construction.
    from .profiles import by_name as _profile_by_name, circadian_params as _circ_params, world_params as _world_params

    profile = _profile_by_name(os.environ.get("RESIDENT_PROFILE", "sealed-baseline"))
    if os.environ.get("RESIDENT_WORLD", "0") != "0":
        # v0.2 (ADR-0009): the world *source* is selectable — fixture (the sealed
        # hermetic corpus, the default), replay (a recorded capture, fully
        # offline), or live (the SSRF-guarded real web, only when explicitly
        # enabled). The mind machinery is identical for all three.
        world_kind = os.environ.get("RESIDENT_WORLD_SOURCE", "fixture").strip().lower()
        world_rng = random.Random(int(os.environ.get("RESIDENT_WORLD_SEED", "0")))
        # v0.2 Step 3 (ADR-0011): the World→Question layer — default off (the
        # sealed behaviour); explicit opt-in via RESIDENT_QUESTIONS=on.
        # v0.2 Active Living (ADR-0014): the profile supplies the world's
        # OPPORTUNITY rails (cooldown / daily cap / re-observation horizon).
        world_params = _world_params(profile)
        if os.environ.get("RESIDENT_QUESTIONS", "0") != "0":
            world_params = replace(world_params, enable_questions=True)
        if world_kind in ("replay", "live"):
            from .world_source import SeedEntry, WorldSnapshotStore

            snap_dir = os.environ.get(
                "RESIDENT_WORLD_SNAPSHOTS", str(data / "world_snapshots"))
            snapshot_store = WorldSnapshotStore(snap_dir)
            seeds: list[SeedEntry] = []
            seed_path = os.environ.get("RESIDENT_WORLD_SEEDS")
            if seed_path and os.path.exists(seed_path):
                with open(seed_path, encoding="utf-8") as fh:
                    for entry in json.load(fh):
                        seeds.append(SeedEntry(**entry))
            if world_kind == "replay":
                from .world_source import ReplayWorldSource

                world_engine = WorldWindowEngine(
                    store, memory=memory, params=world_params, rng=world_rng,
                    source=ReplayWorldSource(seeds, snapshot_store))
            else:
                from .world_source import LiveWebWorldSource

                world_engine = WorldWindowEngine(
                    store, memory=memory, params=world_params, rng=world_rng,
                    source=LiveWebWorldSource(
                        seeds, snapshot_store,
                        fetcher=SafeFetcher(allow_proxy_dns=(
                            # explicit opt-in: this host's DNS answers from the
                            # benchmark range (transparent proxy) — the guard
                            # refuses it by default (ADR-0009)
                            os.environ.get("RESIDENT_ALLOW_PROXY_DNS", "0") != "0"))))
        else:
            world_engine = WorldWindowEngine(
                store, memory=memory, corpus=DEFAULT_WORLD_CORPUS, params=world_params,
                rng=world_rng,
            )
    # The live scheduler (Phase 5, ADR-0007): a state-aware beat loop that drives
    # the real engines on the wall clock — what happens each beat (wake /
    # consolidate / nothing) is decided by the evidence-driven circadian state
    # machine, not by a fixed interval.
    orchestrator = CircadianOrchestrator(store, mind, sleep_engine, reentry=engine,
                                         world_engine=world_engine,
                                         params=_circ_params(profile))
    scheduler = CircadianScheduler(
        orchestrator,
        beat_interval=float(os.environ.get("RESIDENT_BEAT_INTERVAL", str(profile.beat_interval_s))),
        jitter=float(os.environ.get("RESIDENT_BEAT_JITTER", "6")),
    )
    # The fixed-interval baseline (kept for comparison / fallback, not the default).
    fixed_scheduler = WakeScheduler(
        mind,
        interval=float(os.environ.get("RESIDENT_WAKE_INTERVAL", "300")),
        jitter=0.1,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if os.environ.get("RESIDENT_AUTO_WAKE") == "1":
            await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()

    app = FastAPI(title="Resident", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.store = store
    app.state.provider = provider
    app.state.memory = memory
    app.state.mind = mind
    app.state.engine = engine
    app.state.sleep_engine = sleep_engine
    app.state.telemetry = telemetry
    app.state.orchestrator = orchestrator
    app.state.world_engine = world_engine  # Phase 6 (ADR-0008); None unless RESIDENT_WORLD set
    app.state.scheduler = scheduler            # the live circadian scheduler (Phase 5)
    app.state.fixed_scheduler = fixed_scheduler  # fixed-interval baseline / fallback

    # ------------------------------------------------------------- health

    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "db": str(store.path),
            "events": store.count(),
            "model": provider.model_id,
            "scheduler_running": scheduler.running,
        }

    # ------------------------------------------------------------- events

    @app.get("/api/events")
    def events(
        limit: int = 100,
        since: str | None = None,
        type: str | None = None,
        order: str = "asc",
    ) -> list[dict]:
        return [e.model_dump() for e in store.list(limit, since=since, type_prefix=type, order=order)]

    @app.get("/api/events/{event_id}")
    def event(event_id: str) -> dict:
        e = store.get(event_id)
        if e is None:
            raise HTTPException(status_code=404, detail="event not found")
        return e.model_dump()

    # -------------------------------------------------------------- chat

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> dict:
        user_evt = store.append(
            EventCreate(
                type="conversation.user_message",
                actor="user",
                visibility="user_visible",
                content={"text": req.message},
                provenance={"source": "webui"},
            )
        )
        # Pre-existing mental state is assembled from the log BEFORE the reply
        # is generated: the user message is important, but it does not erase
        # what Resident was already thinking (ARCHITECTURE.md, "Conversation").
        state = MentalState.from_store(store)
        prompt = {
            "task": "chat",
            "user_message": req.message,
            "mind_brief": state.mind_brief(),
            "recent_life": [
                {"id": e.id, "type": e.type, "text": e.text} for e in state.recent_life(5)
            ],
            # output contract, explicit for real providers (see mind_loop._reflect)
            "output": {
                "format": "strict JSON object",
                "schema": {
                    "reply": "string — what you say back, in Chinese, grounded in mind_brief/recent_life; never invent events that are not recorded",
                },
            },
        }
        try:
            data = json.loads(
                provider.complete(system=SYSTEM_PROMPT, prompt=json.dumps(prompt, ensure_ascii=False))
            )
        except (ValueError, ProviderError):
            data = None
        reply = data.get("reply") if isinstance(data, dict) else None
        if not (isinstance(reply, str) and reply.strip()):
            # never fabricate a conversational memory: an honest miss, recorded
            reply = "（这次我没能把想法整理成一句话——你刚说的这句我已经记下了。）"
        resident_evt = store.append(
            EventCreate(
                type="conversation.resident_message",
                visibility="user_visible",
                content={"text": reply, "mind_brief": state.mind_brief()},
                links={"caused_by": [user_evt.id]},
                provenance={"source": "conversation", "model": provider.model_id},
            )
        )
        store.append(
            EventCreate(
                type="experience.created",
                visibility="private",
                content={"summary": f"对话：{req.message[:120]}"},
                links={"caused_by": [user_evt.id, resident_evt.id]},
                provenance={"source": "conversation"},
            )
        )
        if scheduler.running:
            scheduler.request_wake("conversation")
        # v0.2 Step 4 (ADR-0012): the continuity fact layer runs at the moment
        # the user speaks — it records facts, it does not shape the reply.
        continuity_summary = (
            continuity_engine.on_user_return(user_evt)
            if continuity_engine is not None else None
        )
        return {
            "reply": reply,
            "user_event_id": user_evt.id,
            "resident_event_id": resident_evt.id,
            "continuity": continuity_summary,
        }

    # -------------------------------------------------------------- wake

    @app.post("/api/wake")
    async def wake(req: WakeRequest | None = None) -> dict:
        return await mind.wake_once(req.trigger if req and req.trigger else "manual")

    # -------------------------------------------------------------- life

    @app.get("/api/life")
    def life(limit: int = 40) -> dict:
        recent = [e for e in store.list(1000, order="desc") if e.visibility != "system"][:limit]
        events = [
            {
                "id": e.id,
                "type": e.type,
                "text": e.text,
                "created_at": e.created_at,
                "visibility": e.visibility,
                "thread_id": e.links.get("thread_id"),
                "route": e.metadata.get("route"),
            }
            for e in recent
        ]
        wakes = [
            {
                "run_id": e.content.get("run_id"),
                "route": e.content.get("route"),
                "result": e.content.get("result"),
                "created_at": e.created_at,
            }
            for e in store.list(200, type_prefix="wake.completed", order="desc")
        ]
        return {
            "events": events,
            "wakes": wakes,
            "stats": {
                "events": store.count(),
                "wakes": store.count("wake.started"),
                "noops": store.count("wake.noop"),
                "route_distribution": _route_distribution(store),
            },
        }

    # -------------------------------------------------------------- mind

    @app.get("/api/mind")
    def mind_state() -> dict:
        state = MentalState.from_store(store)
        return {
            "mind_brief": state.mind_brief(),
            "threads": [asdict(t) for t in state.threads.values()],
            "active_threads": [asdict(t) for t in state.active_threads],
            "dormant_threads": [asdict(t) for t in state.dormant_threads],
            "open_questions": [
                {"question_id": q.question_id, "text": q.text, "thread_id": q.thread_id}
                for q in state.open_questions
            ],
            "recent_thoughts": [
                {"id": e.id, "text": e.text, "created_at": e.created_at, "visibility": e.visibility}
                for e in state.current_thoughts(10)
            ],
        }

    # ------------------------------------------------------------ telemetry

    @app.get("/api/telemetry")
    def telemetry_endpoint(window: str = "24h") -> dict:
        """Windowed behavioural + circadian + revival report (``24h`` / ``7d`` /
        ``30d``). Every value is derived from events in ``[now-window, now]``, so
        the report is consistent with the event store and reproducible."""
        windows = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        if window not in windows:
            raise HTTPException(status_code=400, detail=f"window must be one of {sorted(windows)}")
        latest = store.latest()
        now_iso = latest.created_at if latest else datetime.now(timezone.utc).isoformat()
        since_iso = (datetime.fromisoformat(now_iso) - windows[window]).isoformat()
        report = telemetry.window_report(
            since_iso, now_iso=now_iso, window=window,
            circadian_state=orchestrator.machine.state,
        )
        report["scheduler"] = "circadian"
        report["scheduler_running"] = scheduler.running
        return report

    @app.get("/api/circadian")
    def circadian_state() -> dict:
        """The live circadian state machine: current state + recent transitions."""
        machine = orchestrator.machine
        recent = store.list(20, type_prefix="circadian.state_changed", order="desc")
        return {
            "state": machine.state,
            "recent_transitions": [
                {"from": e.content.get("from"), "to": e.content.get("to"),
                 "reasons": e.content.get("reasons"), "created_at": e.created_at}
                for e in recent
            ],
            "beats": orchestrator.beats,
            "wakes": orchestrator.wakes,
            "consolidations": orchestrator.consolidations,
        }

    # ------------------------------------------------------------ reentry

    @app.post("/api/reentry")
    def reentry(req: ReentryRequest | None = None) -> dict:
        return engine.run(req.since if req else None)

    # ------------------------------------------------------------ monitor
    # v0.2 Step 5 (ADR-0013): the Self Monitor — a microscope, not Resident's
    # face. STRICTLY READ-ONLY: GET only, never appends, never retrieves,
    # never wakes, never consolidates. Opening the page must not shake the
    # petri dish; the test suite asserts the store is untouched by every view.

    @app.get("/api/monitor/timeline")
    def monitor_timeline_ep(limit: int = 200, type: str | None = None) -> dict:
        return monitor_timeline(store, limit=limit, type_prefix=type)

    @app.get("/api/monitor/provenance/{event_id}")
    def monitor_provenance_ep(event_id: str, depth: int = 8) -> dict:
        return monitor_provenance(store, event_id, depth=depth)

    @app.get("/api/monitor/threads")
    def monitor_threads_ep() -> dict:
        latest = store.latest()
        return monitor_threads(store, now_iso=latest.created_at if latest else None)

    @app.get("/api/monitor/questions")
    def monitor_questions_ep() -> dict:
        latest = store.latest()
        return monitor_questions(store, now_iso=latest.created_at if latest else None)

    @app.get("/api/monitor/projects")
    def monitor_projects_ep() -> dict:
        latest = store.latest()
        return monitor_projects(store, now_iso=latest.created_at if latest else None)

    @app.get("/api/monitor/continuity")
    def monitor_continuity_ep(limit: int = 20) -> dict:
        return monitor_continuity(store, limit=limit)

    @app.get("/api/monitor/retrieval")
    def monitor_retrieval_ep(limit: int = 40) -> dict:
        latest = store.latest()
        shadow_path = os.environ.get("RESIDENT_SHADOW_LOG") or str(data / "semantic_shadow.jsonl")
        return monitor_retrieval_trace(
            store, limit=limit, now_iso=latest.created_at if latest else None,
            shadow_log_path=shadow_path)

    # --------------------------------------------------------- scheduler

    @app.post("/api/scheduler/start")
    async def scheduler_start() -> dict:
        await scheduler.start()
        return {"running": scheduler.running}

    @app.post("/api/scheduler/stop")
    async def scheduler_stop() -> dict:
        await scheduler.stop()
        return {"running": scheduler.running}

    return app


app = build_app()

# Module-level singletons (the running uvicorn process's body).
store = app.state.store
provider = app.state.provider
memory = app.state.memory
mind = app.state.mind
engine = app.state.engine
scheduler = app.state.scheduler
