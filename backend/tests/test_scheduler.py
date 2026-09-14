"""WakeScheduler: jitter bounds, guarded tick, concurrency, lifecycle."""
import asyncio
import random

from resident.event_store import EventStore
from resident.mind_loop import MindLoop
from resident.scheduler import WakeScheduler


def make_mind(tmp_path):
    s = EventStore(tmp_path / "e.sqlite3")
    return s, MindLoop(s)


def test_next_delay_stays_in_jitter_bounds(tmp_path):
    _, m = make_mind(tmp_path)
    sched = WakeScheduler(m, interval=300.0, jitter=30.0, rng=random.Random(42))
    for _ in range(50):
        d = sched.next_delay()
        assert 270.0 <= d <= 330.0
        assert d >= 0.1


async def test_tick_runs_a_wake(tmp_path):
    s, m = make_mind(tmp_path)
    sched = WakeScheduler(m, interval=300.0)
    res = await sched.tick()
    assert res.get("skipped") is not True
    assert "run_id" in res
    assert s.count("wake.started") == 1
    assert s.count("wake.completed") == 1
    assert sched.wakes == 1


async def test_concurrent_ticks_exactly_one_wake(tmp_path):
    s, m = make_mind(tmp_path)
    sched = WakeScheduler(m, interval=300.0)
    a, b = await asyncio.gather(sched.tick(), sched.tick())
    skipped = [r for r in (a, b) if r.get("skipped") is True]
    ran = [r for r in (a, b) if "run_id" in r]
    assert len(skipped) == 1, "one tick must be skipped while the other runs"
    assert len(ran) == 1
    assert s.count("wake.started") == 1
    assert sched.skipped == 1


async def test_start_stop_lifecycle(tmp_path):
    s, m = make_mind(tmp_path)
    sched = WakeScheduler(m, interval=0.1, jitter=0.0, rng=random.Random(1))
    assert sched.running is False

    await sched.start()
    assert sched.running is True
    await sched.start()  # idempotent
    await asyncio.sleep(0.4)
    await sched.stop()
    assert sched.running is False

    wakes_after_stop = s.count("wake.started")
    assert wakes_after_stop >= 1
    assert sched.wakes >= 1

    await asyncio.sleep(0.3)  # nothing more happens after stop
    assert s.count("wake.started") == wakes_after_stop

    await sched.stop()  # idempotent
    await sched.start()
    assert sched.running is True
    await sched.stop()
    assert sched.running is False


async def test_bad_wake_does_not_kill_loop(tmp_path):
    """A wake that raises must not stop the scheduler."""

    class FlakyMind:
        def __init__(self, real):
            self.real = real
            self.calls = 0

        async def wake_once(self, trigger="manual"):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("model blew up")
            return await self.real.wake_once(trigger)

    s, real = make_mind(tmp_path)
    flaky = FlakyMind(real)
    sched = WakeScheduler(flaky, interval=0.1, jitter=0.0, rng=random.Random(2))
    await sched.start()
    await asyncio.sleep(0.35)
    await sched.stop()
    assert flaky.calls >= 2  # the loop survived the first failure
    assert sched.errors >= 1
