# Living Baseline — 观察 1–2 周，再设计 Phase 7

**Status:** adopted 2026-09-12 (post `v0.2-step5`), per the owner's reordering:
**not** going straight into Phase 7 Private Projects.

## Why this window exists

Phase 7's real question is not "让它做项目", but:

> **什么样的长期未完成结构，足以自然跨过"想一想"这条线，变成"我想试试看"。**

If we write `question → score → private_project` now, we are back to a
traditional agent. Every good result so far came from **机制先克制，数据再告诉
我们哪里真的活了** (fixture world → seen_left_nothing 0.55–0.65; shadow-first
→ "transcription vs the thing itself"; question-as-residue; noop-first
re-entry). So: Resident now lives — with world, questions, continuity, and a
microscope — and Phase 7's genesis criteria get **abstracted from observed
behavior**, not declared.

The hypothesized genesis pattern to watch for (from the owner):

```text
question → dormant → 被另一经历重新撞上 → thought → 又过几天再次出现
        → 跨来源证据增加 → self-thread 延伸 → 仍然没有消失
```

## The rule

**心智代码零改动 during the window.** No parameter tuned, no mind machinery
added, no "small fix" to the loop. Anything that changes the mind starts a
NEW baseline. Observation-side work (monitor views, this kit) is allowed —
it writes nothing.

## How to run the living Resident

```bash
cd backend
RESIDENT_WORLD=1 \
RESIDENT_WORLD_SOURCE=live \
RESIDENT_WORLD_SEEDS=seeds/world_seeds.json \
RESIDENT_ALLOW_PROXY_DNS=1 \
RESIDENT_QUESTIONS=on \
RESIDENT_CONTINUITY=on \
RESIDENT_SEMANTIC_SHADOW=1 \
RESIDENT_MODEL_BASE_URL=... RESIDENT_MODEL_API_KEY=... RESIDENT_MODEL_NAME=... \
make backend
```

- `RESIDENT_ALLOW_PROXY_DNS=1` is **required on this host** (its DNS answers
  from the benchmark range; ADR-0009 refuses that by default).
- Seeds are a fixed 18-URL frontier (`backend/seeds/world_seeds.json`, all
  verified reachable in the Step-1 live smoke); accident mode may discover
  real outbound links beyond it — each re-passes the SSRF guard and budget.
- **fixture vs live:** the sealed fixture corpus is 20 items — the world
  exhausts it in ~3 days (`no_eligible_mode` skips after that). Any window
  longer than 3 days should run **live**.
- `RESIDENT_MODEL_*` optional (the engine-level baseline metrics are
  provider-independent; the real model changes the thought *text*, not the
  machinery).
- Data lands in `resident_home/`: `events.sqlite3`, `world_snapshots/`,
  `semantic_shadow.jsonl`.

## Daily & cadence

- **Daily (~5 min):** open the 显微镜 Monitor — noop share on the timeline,
  new questions, thread revivals, continuity windows, retrieval trace
  (top-1 分歧 rows are where "transcription vs the thing itself" shows).
- **Day 1 / 7 / 14:** run the analysis kit and **keep every output** (never
  overwrite — they are the baseline's readings):

```bash
cd backend
.venv/bin/python scripts/living_baseline.py \
  ../resident_home/events.sqlite3 \
  --shadow ../resident_home/semantic_shadow.jsonl \
  --json scripts/out/baseline_day7.json
```

The script is read-only (it asserts the store is untouched) and answers the
four questions: `questions` (谁自己活下来了), `threads` (谁反复回来 + 重现
间隔), `world_traces` (谁的痕迹跨过了 1 天 / 被引用不止一次), `continuity`
(候选类别分布 + selected/noop), `shadow` (legacy vs semantic), plus `vitals`
as the honest frame.

## Provider switch (2026-09-12 15:36 local) — pre-baseline voided and re-registered

At the owner's direction the resident now runs the **real model** from the
local Claude Code configuration (qwen3.8 via the owner's relay; credentials
live in git-ignored `backend/.env`, 0600, never committed or logged; verified
with exactly one live call). Consequences, handled explicitly:

- the first registered baseline (fake provider, 06:56–07:36 UTC, 11 events)
  is **voided**: its store is archived intact at
  `resident_home/archive/events_pre_baseline_fakeprovider_20260912.sqlite3`
  (nothing deleted);
- the registered baseline **restarted** at 07:36:44 UTC with a fresh store and
  a new config fingerprint (`b9253b76165c47a0`) — the model is now part of the
  observed configuration and must not change mid-window;
- the engine-level machinery is unchanged (wake / retrieval / question /
  continuity code untouched — the discipline stands); what changes is the
  *content* of thoughts, which is exactly what a real-life baseline should
  observe. Model-failure liveness was verified: `_provider_json` degrades to
  rest on `ProviderError`, and the scheduler never lets one bad beat kill the
  loop.

## Model-switch completion (2026-09-12 ~08:00 UTC)

The first functional chat exposed three gaps left by the implicit
fake-provider contract; all fixed as part of the switch (machinery untouched,
373 tests + sealed regression green after):

1. the wake **reflection contract** (`{"text","decision","links","visibility"}`)
   was never stated in the prompt — the fake provider hardcoded it, so every
   real-model wake silently rested ("no durable content"). The contract is now
   an explicit `output` block in the reflection prompt; both decisions are
   stated as legitimate (the first wording accidentally primed noop);
2. the **chat endpoint** 500'd on any reply missing `"reply"` — tolerant parse
   + an honest fallback reply (never a fabricated memory);
3. the provider **timeout** (hardcoded 30s) was too small for reflection-sized
   prompts on the owner's relay — now `RESIDENT_MODEL_TIMEOUT` (120s in .env).

First two real thoughts landed at 07:52/07:57 UTC (quoted in the day-1
reading): the resident read its own wake/noop stats and connected the memory
article to its own condition — grounded, no fabrication.

## The observation discipline (owner directive, 2026-09-12)

Until **Day 7 = 2026-09-19**, exactly three duties and nothing else:

1. **Keep the process alive.** Runtime-infrastructure fixes are allowed;
   ANY code touching wake / retrieval / question / continuity behavior is
   untouchable.
2. **Watch, never intervene.** The Monitor may be read; do not start chats
   about an interesting thread or question — human reinforcement would
   contaminate exactly the behavior being observed.
3. **Preserve every raw artifact.** Day 1/7/14 JSONs, the manifest, the
   server log, and the shadow sidecar are never overwritten or deleted.

The scarce thing to look for is one chain — *world observation → question →
dormant/survive → unrelated encounter → revisit → self-thought → returns
again days later*. One fossil instance outranks any average metric. E0 (the
de-pollution counterfactual) stays in the candidate zone: no treatment runs
before the day-7 reading, which will tell us whether the pollution is
experimental noise or a systematic distortion of living association.
**Next architecture decision point: Day 7 = 2026-09-19.**

## What would justify starting Phase 7

Not a date — a **pattern in the data**, e.g. (to be confirmed, not assumed):
a question that went dormant, was re-struck by an unrelated source, spawned
a thought, returned days later with cross-source evidence, and fed a
self-thread that kept extending. If the pattern never appears in 2 weeks,
that is the equally valuable answer: the genesis condition as hypothesized is
wrong, and Phase 7 waits or redesigns. Self Maintenance / Self Modification
judgments stay on the 2–4-week window and are a separate decision.
