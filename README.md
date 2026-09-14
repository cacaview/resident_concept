# Resident

**Resident is not an assistant waiting for prompts.**

Resident is an experimental persistent digital individual with its own timeline, autobiographical memory, unfinished thoughts, interests, private projects, and gradual development.

> When you leave, its time does not stop. When you return, both of you may have something new to say.

## v0.1 — Proof of Life (SEALED 2026-09-11, tag `v0.1`)

v0.1 is **frozen**: no new features will be added to it. The complete loop it
set out to prove:

```text
conversation → experience → user leaves → sleep → autonomous wake
→ remember → wander → optional exploration → persistent experience
→ sleep → user returns → optional proactive re-entry
```

Success is not “it completed a task”. Success is that something real happened
while the user was away, it is traceable, and it can affect the next interaction.

The twelve sealed claims — persistent second timeline, autobiographical event
history, stateful memory, mind wandering, sleep/consolidation, self-origin
threads, circadian rhythm, dormant thought revival, serendipity, external
world experiences, provenance, authentic no-op — are each stated **with
evidence** in [`docs/RELEASE-v0.1.md`](docs/RELEASE-v0.1.md), together with
the negative results we deliberately preserved (World ≠ Self Engine;
`seen_left_nothing` ≈ 0.55–0.65).

### Run it

```bash
make backend                       # live API on :8010 (circadian scheduler)
cd frontend && npm run dev         # WebUI on :5173 (use localhost, not 127.0.0.1)
cd backend && .venv/bin/python -m pytest -q   # 373 tests
```

The WebUI has two views: **Resident** (the conversation — its face) and
**显微镜 Monitor** (v0.2 Step 5 — the read-only microscope: timeline,
provenance inspector, threads & questions, retrieval trace, continuity;
it never writes, retrieves, or wakes — ADR-0013).

Current phase: **Living Baseline** — Resident lives 1–2 weeks under
observation before Phase 7 is designed (`docs/LIVING_BASELINE.md`,
analysis kit: `backend/scripts/living_baseline.py`).

Opt-ins: `RESIDENT_WORLD=1` (World Window; `RESIDENT_WORLD_SOURCE=replay|live`
for the v0.2 adapter), `RESIDENT_EMBEDDINGS=semantic` / `RESIDENT_SEMANTIC_SHADOW=1`
(v0.2 Step 2 semantic layer), `RESIDENT_QUESTIONS=on` (v0.2 Step 3
World→Question), `RESIDENT_CONTINUITY=on` (v0.2 Step 4 continuity across
absence), `RESIDENT_MODEL_*` (real model provider),
`RESIDENT_AUTO_WAKE=1` (set by `make backend`).

### Read first

1. `VISION.md`
2. `ARCHITECTURE.md`
3. `GENOME.md`
4. `EVENT_MODEL.md`
5. `DEVELOPMENT.md`
6. `CLAUDE.md`
7. `PROMPT_FOR_CLAUDE_CODE.md`
8. `docs/RELEASE-v0.1.md` — the v0.1 seal (claims + evidence)
9. `docs/TODO.md` — v0.2 roadmap: *From Simulation to Habitat*
