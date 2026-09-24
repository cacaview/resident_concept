# ADR-0023 — Circadian clock: track the host's local wall clock, not the UTC instant

**Status:** Accepted (2026-09-17).

## Context

The circadian machine's day/night windows are driven by `CircadianEvidence.local_hour`
(`circadian.py`): the **night window** (22:00–06:00) eases the sleep thresholds, and
the **morning wake window** (06:00–09:00) both enables a re-open from sleep and keeps
a freshly re-opened mind "up and about" through the morning.

`local_hour` was computed in `assemble_evidence` as `now.hour` where `now` is the
store's reference instant. That instant is always written in **UTC**: every
`created_at` in the store carries a `+00:00` offset, and the fallback reference is
`datetime.now(timezone.utc).isoformat()` (`main.py`). So despite its name,
`local_hour` was the **UTC** hour-of-day — not the machine's local hour.

On a UTC machine the two coincide, so the discrepancy was invisible in the sim and in
CI. On the live Windows host — **UTC+8 (Beijing)** — the resident's whole circadian
cycle ran 8 hours off:

- its "morning wake window" (06:00–09:00) fired at 06:00–09:00 **UTC** =
  **14:00–17:00 Beijing** (its actual afternoon), and
- its "night window" (22:00–06:00 UTC) covered **06:00–14:00 Beijing** (its actual
  morning and early afternoon).

The observable consequence (the 2026-09-17 symptom): with zero user activity, the
evidence-driven machine latched to SLEEP and **stayed asleep through the resident's
real daytime** — because, on the UTC clock, "daytime" in Beijing was still inside the
UTC night window. The field was even mis-named: `local_hour` was documented as
"hour-of-day in the reference zone", but the reference zone was silently UTC.

## Decision

Compute `local_hour` from the **host's local timezone**, not the raw UTC hour:

```python
now = _parse_iso(now_iso)                     # absolute UTC instant (store clock)
if now is not None and now.tzinfo is None:    # defensive: a naive now is read as UTC
    now = now.replace(tzinfo=timezone.utc)
_local = now.astimezone() if now else datetime.now().astimezone()
hour = _local.hour + _local.minute / 60.0     # hour-of-day in the machine's local tz
```

The absolute UTC instant `now` is **unchanged** for everything else: the `since_*`
deltas (user silence, meaningful self-activity, external activity, consolidation) are
timezone-invariant *durations* and keep using `now`. Only the hour-of-day — hence the
night/wake windows — is timezone-local. A naive `now` (which in practice never occurs;
the store clock is always aware) is read as UTC so the same `astimezone()` path holds.

The clock remains what it was designed to be: a **bias, never the decider**. A left-alone
mind still sleeps on sustained user silence regardless of the hour (the descent logic is
untouched); the local clock only makes that bias and the morning re-open land at the
correct time of day.

## Consequences

- **The resident's day/night now tracks the machine's local wall clock.** On the live
  Windows host (UTC+8) it rests at local night and re-opens in the local morning
  (06:00–09:00 Beijing), as the windows were always meant to do. The 2026-09-17
  "asleep all day" symptom is a direct product of this UTC reference and is corrected.
- **Unchanged on a UTC host.** Where the machine is UTC, local == UTC, so the sim, CI,
  and any UTC deployment behave byte-identically to before — no regression there. The
  behavior change is *portability* (correct local time on a non-UTC host), not a change
  to the decision logic.
- **The `since_*` evidence is untouched.** User-return and external-resume wake triggers
  are duration-based and do not shift; only the hour-of-day (night/wake window bias and
  the morning re-open window) is re-referenced.
- **Test evidence.** New
  `tests/test_circadian.py::test_assemble_evidence_local_hour_tracks_host_timezone`
  asserts `local_hour` equals the UTC hour shifted by the host's UTC offset (mod 24).
  It is a portability guard: non-trivial on a non-UTC host (the live box is UTC+8),
  coincident on a UTC host.
- **Live-observation note.** This is a deliberate, owner-directed intervention on the
  running active-v3 Day-7 window (T0 2026-09-16, stop 2026-09-23): the clock is
  corrected and the service restarted mid-window so the resident's rhythm follows the
  host clock. Recorded here for provenance; the window's earlier beats ran on the UTC
  reference.
