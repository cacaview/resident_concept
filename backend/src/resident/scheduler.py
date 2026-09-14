"""Jittered, skippable wake scheduling (ARCHITECTURE.md, "Scheduling").

Timer wakes are low-frequency and best-effort:

- Each sleep is ``interval + jitter`` with the jitter drawn from a seeded
  ``random.Random`` (injectable for tests) — timer wakes are *jittered*.
- If a wake is already in flight, a new timer wake is *skipped* rather than
  queued — a tick that finds the mind busy returns ``{"skipped": True}``.
- Out-of-band wakes (e.g. "the user just messaged") are requested via
  :meth:`request_wake`, which wakes the loop without waiting for the next
  timer delay.
- One bad wake never kills the loop: each cycle's exceptions are caught.

Thread-safety choice for :meth:`request_wake`: a simple flag + ``asyncio.Event``
is published on the scheduler's event loop via ``call_soon_threadsafe`` when a
loop is running (safe from any thread); otherwise the flag is written directly
(CPython's GIL makes the attribute write atomic) and is picked up when the
loop starts.

Phase 5 (ADR-0007) adds :class:`CircadianScheduler`: a state-aware live
scheduler that drives a :class:`resident.circadian.CircadianOrchestrator` on the
wall clock. Unlike the fixed-interval :class:`WakeScheduler` above, *what*
happens each beat (wake / consolidate / nothing) is decided by the evidence
circadian state machine, not by "tick every N seconds and wake".
"""
from __future__ import annotations

import asyncio
import contextlib
import random
from datetime import datetime, timezone

from .mind_loop import MindLoop


class WakeScheduler:
    """Background loop that periodically (and on request) runs one wake."""

    def __init__(
        self,
        mind: MindLoop,
        *,
        interval: float = 300.0,
        jitter: float = 0.0,
        rng: random.Random | None = None,
    ):
        self.mind = mind
        self.interval = float(interval)
        self.jitter = float(jitter)
        self.rng = rng or random.Random()
        self._task: asyncio.Task | None = None
        self._loop_ref: asyncio.AbstractEventLoop | None = None
        self._poke = asyncio.Event()
        self._pending = False
        self._pending_trigger = "event"
        self._stopping = False
        self._in_flight = False
        # diagnostics
        self.wakes = 0
        self.skipped = 0
        self.errors = 0

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Start the background loop. Idempotent."""
        if self.running:
            return
        self._stopping = False
        self._loop_ref = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._loop(), name="resident-wake-scheduler")

    async def stop(self) -> None:
        """Cancel the loop and wait for it (and any in-flight wake) to end.
        Idempotent."""
        if self._task is None:
            return
        self._stopping = True
        self._poke.set()  # get the loop out of its sleep quickly
        task, self._task = self._task, None
        self._loop_ref = None
        # v0.1 wakes are synchronous, so an in-flight wake finishes before the
        # loop reaches its next await; awaiting the task waits for that.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.cancel()
            await task
        self._poke.clear()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # --------------------------------------------------------------- ticking

    def next_delay(self) -> float:
        """Sleep for the next cycle: interval +/- uniform jitter, >= 0.1s."""
        delay = self.interval + (self.rng.random() * 2 - 1) * self.jitter
        return max(0.1, delay)

    def request_wake(self, trigger: str = "event") -> None:
        """Ask for an out-of-band wake (thread-safe; see module docstring)."""

        def _set() -> None:
            self._pending = True
            self._pending_trigger = trigger
            self._poke.set()

        if self._loop_ref is not None and self._loop_ref.is_running():
            self._loop_ref.call_soon_threadsafe(_set)
        else:
            _set()

    async def tick(self, trigger: str = "timer") -> dict:
        """Run ONE guarded wake. If a wake is in flight, skip it."""
        if self._in_flight:
            self.skipped += 1
            return {"skipped": True}
        self._in_flight = True
        try:
            # Yield once so a concurrent tick observes the in-flight flag
            # (v0.1 wakes are synchronous; without this, two tasks started
            # together would both see an idle mind and both run).
            await asyncio.sleep(0)
            result = await self.mind.wake_once(trigger)
            self.wakes += 1
            return result
        finally:
            self._in_flight = False

    # ------------------------------------------------------------------ loop

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.wait_for(self._poke.wait(), timeout=self.next_delay())
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                if self._stopping:
                    return
                raise
            self._poke.clear()
            trigger = self._pending_trigger if self._pending else "timer"
            self._pending = False
            try:
                await self.tick(trigger)
            except Exception:
                # A bad wake must never kill the loop.
                self.errors += 1


class CircadianScheduler:
    """Live, state-aware scheduler (ADR-0007).

    Runs a :class:`resident.circadian.CircadianOrchestrator` on the **wall
    clock**: each beat (``beat_interval ± jitter`` seconds of real time) it
    hands the current time to the orchestrator, which asks the evidence-driven
    circadian state machine what to do and acts (wake / consolidate / nothing).
    A beat that finds the mind already busy is skipped. ``request_wake`` pokes
    the loop so a just-arrived user message is processed on the next beat rather
    than waiting out the interval. One bad beat never kills the loop.

    This is the live counterpart to the deterministic accelerated
    simulation: same orchestrator, real time instead of a virtual clock.
    """

    def __init__(
        self,
        orchestrator,  # CircadianOrchestrator (duck-typed to avoid a circular import)
        *,
        beat_interval: float = 60.0,
        jitter: float = 6.0,
        now_fn=None,
        rng: random.Random | None = None,
    ):
        self.orchestrator = orchestrator
        self.beat_interval = float(beat_interval)
        self.jitter = float(jitter)
        self._now_fn = now_fn  # injectable clock (tests); default wall clock
        self.rng = rng or random.Random()
        self._task: asyncio.Task | None = None
        self._loop_ref: asyncio.AbstractEventLoop | None = None
        self._poke = asyncio.Event()
        self._stopping = False
        self._in_flight = False
        self.beats = 0
        self.errors = 0

    # ------------------------------------------------------------- lifecycle

    def _now_iso(self) -> str:
        if self._now_fn is not None:
            return self._now_fn().isoformat()
        return datetime.now(timezone.utc).isoformat()

    def next_delay(self) -> float:
        return max(0.1, self.beat_interval + (self.rng.random() * 2 - 1) * self.jitter)

    async def start(self) -> None:
        """Start the beat loop. Idempotent."""
        if self.running:
            return
        self._stopping = False
        self._loop_ref = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._loop(), name="resident-circadian")

    async def stop(self) -> None:
        """Cancel the loop and wait for it (and any in-flight beat) to end."""
        if self._task is None:
            return
        self._stopping = True
        self._poke.set()
        task, self._task = self._task, None
        self._loop_ref = None
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.cancel()
            await task
        self._poke.clear()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def request_wake(self, trigger: str = "event") -> None:
        """Ask for an out-of-band beat (thread-safe): poke the loop so a fresh
        user message is handled on the next beat rather than waiting out the
        interval. (The beat itself re-derives state from the store.)"""

        def _set() -> None:
            self._poke.set()

        if self._loop_ref is not None and self._loop_ref.is_running():
            self._loop_ref.call_soon_threadsafe(_set)
        else:
            _set()

    # -------------------------------------------------------------- stepping

    async def tick(self) -> dict:
        """Run ONE guarded beat. If a beat is in flight, skip it."""
        if self._in_flight:
            return {"skipped": True}
        self._in_flight = True
        try:
            await asyncio.sleep(0)  # yield so a concurrent tick sees the flag
            result = await self.orchestrator.beat(self._now_iso())
            self.beats += 1
            return result
        finally:
            self._in_flight = False

    # ------------------------------------------------------------------ loop

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.wait_for(self._poke.wait(), timeout=self.next_delay())
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                if self._stopping:
                    return
                raise
            self._poke.clear()
            try:
                await self.tick()
            except Exception:
                # a bad beat must never kill the loop
                self.errors += 1
