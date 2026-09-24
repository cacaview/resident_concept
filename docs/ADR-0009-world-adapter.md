# ADR-0009: Real World Adapter — a Safe, Auditable, Replayable Sense Organ

Date: 2026-09-11 · Status: Accepted · Supersedes the *world-input* part of
ADR-0005 (the corpus stays as the fixture arm); leaves ADR-0008's World Window
mechanics untouched.

## Context

Phase 6 (ADR-0008) gave Resident a World Window whose material came from a
**fixed, in-code corpus** — a hermetic stand-in for "the world". v0.1 is sealed
(tag `v0.1`, docs/RELEASE-v0.1.md); v0.2 Step 1's brief: replace the *input
source* with the real internet, under three hard conditions:

1. **Do not touch the sealed mind.** "本阶段禁止修改已封板的心智架构" — Mind /
   World Window / Impression / Thought mechanics stay byte-identical; only the
   world *input source* is swapped. Two surgical, provably fixture-no-op
   exclusions are recorded below.
2. **Live capture + replay.** The real web is not reproducible; the
   deterministic simulation and regression system must not become
   non-deterministic. Every live fetch must produce an **immutable snapshot**,
   replayable offline, so "why did it think this on Sept 14?" stays answerable.
3. **The organ is not the will.** v0.2 Step 1 solves only "given a URL, can
   Resident safely/truly/replayably *experience* it?" — not "why did it choose
   to go there?" Search engines and autonomous exploration stay out.

Standing constraints (verbatim, still in force): reading ≠ thought;
observation ≠ belief; no continuous news feed / RSS agent / autonomous task
searcher; no Playwright/browser automation, no login/cookies, no binary
downloads, no publishing; budgets are caps (0/day legal); no ratio tuning;
provenance for every claim.

## Decision

### 1. A `WorldSource` seam — the Window cannot tell fixture from live

`world_source.WorldSource` is the small read surface the World Window already
implicitly used (the shape of `WorldCorpus`): `items() / get() /
outgoing_links() / topic_adjacency()` plus one new call, `observe(item, *,
now_iso, context)` — *materialize* a candidate into real content.

- `FixtureWorldSource(corpus)` — the sealed corpus; `observe` is the identity.
- `ReplayWorldSource(seeds, snapshots)` — serves recorded snapshots fully
  offline; a URL outside the recording fails *deterministically*
  (`not_in_snapshot`), never a live request.
- `LiveWebWorldSource(seeds, snapshots, fetcher)` — fetches via the
  SSRF-guarded fetcher, writes an immutable snapshot, and derives accident
  candidates from the page's **real outbound links**.

`WorldWindowEngine(corpus=…)` keeps working (it means fixture); engines built
with `source=…` run the identical cognition. The frontier logic, propensity
roll, rng draw order, event chain and metrics are unchanged — proven by the
sealed regression (all 12 Phase-6 A/B/C cells byte-identical).

### 2. Live capture + replay

Every live fetch persists one immutable, content-addressed JSON snapshot
(`WorldSnapshotStore`, atomic write, first-write-wins):

```text
requested_url, final_url, fetched_at, status, content_type, source_type,
title, text (normalized), excerpt, content_hash (sha256 of raw bytes),
redirect_chain, byte_size, duration_ms, outbound_links, source_kind,
declared {url, source, topic}, context {opened_event_id, entry_mode, item_id}
```

`snapshot_id = "snap_" + sha256(final_url|content_hash|title|source_type)` —
stable and deduping. The **event log remains the provenance record of record**
(each `world.observation` carries `provenance.fetch.snapshot_id`; the
dangling-link invariant still makes a fabricated observation impossible); the
snapshot embeds the requesting event id so snapshot → event → thought traces
both ways. Replay reconstructs the exact item set (seeds + snapshot-derived
candidates + real outbound links) after restart — the snapshot store, not
memory, is the record.

### 3. The SSRF-guarded fetcher (`web_fetch.SafeFetcher`, stdlib only)

- Scheme allowlist (http/https), port allowlist (80/443), no credentials in
  URLs, `Accept` limited to document types.
- **IP guard**: loopback, RFC1918/private, link-local (incl. cloud metadata
  <internal-ip>), CGNAT 100.64/10 (explicit — not `is_private` on all
  Pythons), ULA, multicast/reserved/unspecified, IPv4-mapped IPv6 — checked on
  the literal host, then on **every** address DNS returns.
- **DNS-rebinding defense**: after validation the fetcher dials the *validated
  IP* (pinned connection; TLS SNI + certificate validation stay bound to the
  hostname) and never re-resolves. Each redirect hop is re-validated from
  scratch (scheme/port/IP) and counted (cap 5).
- **Caps**: connect timeout 10 s, total timeout 30 s, raw response 2 MB,
  *decompressed* 5 MB (gzip-bomb guard via bounded streaming inflate), 50
  links/page.
- **Content allowlist**: HTML/XHTML, text/plain, JSON, RSS/Atom (RSS item
  links become outbound links — real accident fodder). An explicit non-allow
  content type is refused without sniffing; sniffing happens only when the
  header is missing/generic. No scripts executed, no binaries, no browser.
- **Failure is a value, not a crash**: typed `FetchError` subclasses map 1:1 to
  audit events — `world.fetch_blocked` / `world.fetch_timeout` /
  `world.fetch_failed` — after which the window legitimately no-ops.

### 4. Honest accounting of failures

The window opens (a real request may have been made) → fetch fails → the
budget slot **is spent** and the audit event records reason + detail. A failed
fetch is "it looked and the world did not answer", not an experience.

### 5. Two surgical exclusions in the sealed mind code (fixture-proven no-ops)

`MindLoop._route_context` now excludes `world.fetch_*` alongside
`world.window_*` from "new experience since last wake" and from `total_events`.
Rationale: fetch *audit* is not experience — counting it would let network
failures shift the mind's route. In fixture mode no such events exist, so the
change is a no-op; the sealed regression proves all 12 cells byte-identical.
(`circadian._since_meaningful` already filters `visibility == "system"`, which
covers fetch audit without any edit.)

### 6. Live wiring is explicit and default-off

`RESIDENT_WORLD=1` enables the window (Phase 6); `RESIDENT_WORLD_SOURCE`
selects `fixture` (default) / `replay` / `live`; `RESIDENT_WORLD_SEEDS` points
at a bounded seed file (`[{url, source, topic, title?, summary?}, …]`) — a
fixed frontier, not a crawler; `RESIDENT_WORLD_SNAPSHOTS` selects the capture
directory. A missing seed file means no candidates: the live organ simply
looks at nothing.

### 7. Budgets and telemetry

Phase-6 budget system unchanged (light ≤8, deep ≤3, alien ≤1, accident ≤2 per
day; 0/day legal) — now counting **real HTTP attempts** (a failed fetch spends
its slot). New `world_profile(...)[live]` section (additive; all-zero for the
fixture): `live_fetch_attempts`, `live_fetch_success_rate`,
`fetch_to_observation_rate`, `blocked_fetches`, `timeout_rate`,
`dynamic_page_rejection_rate`, `bytes_received`, `redirect_count`,
`snapshot_count`, `replay_hit_rate`, `failure_reason_distribution`. All Phase-6
world telemetry keys are unchanged.

## Consequences

**Good.** Resident's `world.observation` events can now refer to real web
documents with full capture provenance; experiments stay deterministic
(fixture or replay arms); network dirt (404s, timeouts, redirects, private-IP
traps, gzip bombs) is contained below the mind.

**Costs / honest limits.**
- The live seed list is *declared*, so follow/edge/alien selection runs on
  declared topics until content arrives; a v0.2 lexical classifier may re-tag a
  fetched page's topic (both declared and classified topics are recorded).
  Real embeddings (Step 2) may change this classification — regression sims
  will be re-run then, per the v0.2 roadmap.
- `edge` on the live web is structurally weak until pages are observed
  (adjacency grows from real content, not from declarations).
- The real transport's TLS/DNS behavior is not exercised in unit tests (the
  seam is faked); the live smoke test covers it once, engineering-only.

## Verification at seal time

- 36 new hermetic tests (`tests/test_world_source.py`): SSRF (schemes, ports,
  literal private/loopback/link-local/metadata/CGNAT, dangerous DNS, mixed
  records, redirect re-validation, pinned-IP dialing, redirect limit), caps
  (raw, gzip-bomb, timeouts, network errors), parsers (HTML incl. malformed,
  RSS, JSON, MIME strictness, sniffing policy, 404/500), snapshot store
  (stability, immutability), replay == capture, replay-miss determinism,
  engine integration (failure → audit event + no observation + spent slot +
  route-context invisibility; orchestrator beats never crash the mind;
  accident uses only really-observed links), live telemetry.
- Sealed regression: full pytest suite green; the 12 Phase-6 A/B/C cells
  re-run with the refactored engine are byte-identical to the sealed reports
  except the documented additive `world.live` key.
- A one-shot live smoke over ~15 public URLs validates the engineering chain
  only (statuses, bytes, extraction, failures) — no cognitive claims.
