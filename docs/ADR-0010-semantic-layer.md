# ADR-0010: Real Embeddings + the Chinese Semantic Layer (Shadow-first)

Date: 2026-09-12 · Status: Accepted · Extends ADR-0004 (multi-path memory)
and ADR-0007 (telemetry); the first ADR that **deliberately permits** a change
to the sealed mind dynamics — which is why it ships behind a shadow.

## Context

v0.2 Step 2's brief: give Resident a different way to associate, then observe
whether it becomes a different Resident. Constraints, verbatim:

- 主决策仍走 legacy — the semantic layer first runs as a **shadow** that only
  records; switching retrieval on is a later, explicitly-comparable treatment.
- 中文语义层不是"大模型给标签" — topics come from **embeddings +
  deterministic clustering**; an LLM may attach a display label, but labels
  are not identity and cannot change clustering.
- "语义过于优秀" is a real risk — distant must NOT be lowest-cosine and
  serendipity must NOT be random-farthest; the interesting recall is
  *semantically far but explainably bridged*.
- Provider outages must degrade **explicitly** (audited), never crash the
  MindLoop, never silently change semantics.
- 不以"指标提高"为成功标准；所有负结果保留；不为逼近 v0.1 数值调权重。

## Decisions

### 1. Versioned, identity-checked vector cache (`vector_cache.py`)

Every cached vector is keyed by the full identity tuple — provider, model,
version, dimension, normalization — **plus the sha256 of the exact text**.
A model change, version change, or text change can never serve an incompatible
vector (tested). Vectors are stored as **float64** so a cache roundtrip is
bit-exact: a 32-bit cache would make re-runs differ from first runs in the low
digits and silently break experiment determinism. Storage is one sqlite file
per namespace (a sim dir, or the resident home).

### 2. The deterministic sketch provider

`DeterministicSemanticEmbeddingProvider` (`local/sketch-semantic-v1`): seeded
signed random projections over CJK bigrams + word tokens — a genuinely
different algorithm family from the legacy unigram hashing, with similarity
geometry closer to a real embedder. It powers hermetic tests and, when no
embedding endpoint is configured, the recorded experiments (honestly labelled
in every provenance record — never presented as a real model). The real
provider is the existing `OpenAICompatibleEmbeddingProvider` (opt-in via
`RESIDENT_EMBEDDING_*`); it is drop-in and hermetically tested.
**Cluster-threshold honesty**: vector families differ wildly in geometry, so a
provider may declare `cluster_tau_hint` (the sketch family: 0.25, calibrated
to its near-orthogonal geometry — pairwise p50≈0, max≈0.41 on short texts);
the legacy fallback stays `CLUSTER_TAU` (0.55). Hints are calibrated to the
*geometry*, never to a behavioural metric.

### 3. Shadow mode — and why shadow records never enter the event log

`SemanticLayer.shadow_compare` answers the wake's own retrieval question twice
(legacy hashing vs real vectors) and also compares the DISTANT selections
(legacy farthest vs bridge-scored), recording: top-k lists, overlap@k, mean
rank displacement, top-1 divergence, selected-memory age difference, thread
selection divergence, cluster membership of both top-1s, and the bridge rate.

**The records go to a JSONL sidecar, not the event log.** The first
implementation appended `semantic.shadow_compared` (system) to the log — and
the isolation test caught it: `MemoryRetrieval.has_recent` slices a *raw*
recent window before filtering system visibility, so audit events inside that
window squeezed out real candidates and flipped `has_recent_user_message` →
different route scores → a different mind (self_origin 0.071 → 0.0 in a 3-day
run). Fixing the window-slicing itself would have changed sealed behaviour.
The sidecar makes non-perturbation **structural**: the log never sees shadow
data; each sidecar entry still carries its `wake_id` for provenance. The
`semantic.*` exclusions in `MindLoop._route_context` remain as protective
no-ops. Verified: shadow on/off ⇒ byte-identical report (all metrics, all
seeds tested).

### 4. Treatment mode — semantics-aware strategies, legacy byte-preserved

`MemoryRetrieval(..., semantic_layer=layer)` switches three strategies:

- **distant → bridge-scored.** Candidates are ranked by distance from the
  focus centroid, but surfaced first when an *intermediate* memory (mid-band
  to the focus) reaches them — `min(sim(e,m), sim(m,focus)) ≥ MIN_BRIDGE`
  over the top mid-band intermediates. That is "semantically far with an
  explainable path back" (LLM memory ↔ human memory ↔ sleep ↔ circadian
  biology), not a random far. The bridge evidence (via-event id, strength) is
  recorded in the hit reason; a pure-far fallback is flagged `no bridge found`.
- **serendipity → mid-band.** The chance find is drawn from the similarity
  band around the focus — not near, not pure-far — so it stays explainably
  adjacent.
- **topic_concentration → cluster-based.** The route policy's
  open-endedness guard reads the share of the recent window in its dominant
  *semantic cluster*, not the share of event-family names — topic entropy
  finally has semantic meaning.

The legacy paths are the default and are byte-identical (proven by the sealed
regression below).

### 5. Explicit degradation

`ResilientEmbedding` (cache → provider → `EmbeddingUnavailable`) plus
`emit_degraded` (at most one mark per wake) write a sidecar degraded-record
with the reason. Vector-needing paths (semantic_near, distant, serendipity,
cluster concentration, recall) return **empty/neutral** — no silent fallback
to legacy vectors (that would quietly change semantics), no crash into the
MindLoop. Vector-free paths (e.g. `forgotten`, age-based) keep working — the
degradation is surgical.

### 6. Experiment harness

`make_semantic_stack(store, cache_dir, mode=…, shadow=…)` is the single
builder (env-independent; `main.py` parses `RESIDENT_EMBEDDINGS` /
`RESIDENT_SEMANTIC_SHADOW` and delegates); all three simulation builders
(`simulation.run_simulation`, `circadian_sim.run_circadian_simulation`,
`world_sim.run_world_simulation`) take `embeddings`/`shadow` and cache vectors
under their output dir, so any treatment run replays deterministically.
`scripts/semantic_experiment.py` runs the three-phase comparison and writes
the case studies; `semantic_profile` derives the brief's metric list
(legacy_semantic_overlap, rank_displacement, cluster_entropy, cluster_lifetime,
cross_cluster_recall_rate, semantic_memory_age, thread_selection_divergence,
distant_bridge_rate, serendipity_survival_rate).

## Consequences

**Good.** Resident now has a second, meaning-shaped associative fabric that
can be compared against the sealed one wake-by-wake ("what would it have
thought of today?"), switched on for treatment experiments, and rolled back
byte-exactly. The topic view gives `topic_entropy` real semantic content while
keeping cluster identity vector-derived and label-free.

**Costs / honest limits.**
- The recorded Step-2 experiments run on the **sketch** provider: no
  embeddings endpoint was configured in this session (probed once). The
  methodology, cache, and degradation behaviour are provider-agnostic and
  hermetically tested with the real provider shape; swapping the endpoint in
  re-runs the same experiments on cached real vectors — determinism preserved
  by the cache.
- Leader clustering is order-dependent (deterministic given store order, but
  a connected-component clustering would merge chains differently); cluster
  identity is stable via the seed event, ordinals are display-only.
- The bridge proxy (one intermediate hop over a bounded mid-band pool) is an
  approximation of "explainable bridge"; deeper chains are future work.

## Verification at seal time

- 19 hermetic tests (`tests/test_semantic.py`): cache identity + no
  incompatible reuse + dim mismatch; cache-first wrapper with exact roundtrip;
  degraded mode (sidecar marks, surgical empty results, vector-free paths
  alive); deterministic meaning-shaped clusters; label-never-identity;
  shadow divergence recorded in the sidecar and **absent from the log**;
  shadow on/off ⇒ identical mind; treatment deterministic and genuinely
  different; the brief's telemetry keys present.
- Sealed regression: full suite green (331); the 12-cell Phase-6 A/B/C
  world regression byte-identical with all Step-2 code in place.
- The three-phase experiment (`scripts/semantic_experiment.py`) and its
  findings live in `docs/SEMANTIC_DIVERGENCE_REPORT.md`.
