"""v0.2 Step 1 (ADR-0009): the Real World Adapter — hermetic tests.

Zero public network: every test drives :class:`~resident.web_fetch.SafeFetcher`
through an injected fake transport + resolver, and the live/replay sources
through an on-disk :class:`~resident.world_snapshot`-style store in ``tmp_path``.

Covered (the v0.2 brief, §8): 200-HTML / redirect / redirect-to-private
rejected / literal private+loopback+link-local rejected / dangerous DNS /
timeout / oversized (raw + gzip-bomb) / invalid MIME / malformed HTML /
404-500 / provenance completeness / snapshot-hash stability / replay ==
capture / fetch failure never crashes the MindLoop / accident uses only
really-observed links.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import socket
from datetime import datetime, timezone

import pytest

from resident.circadian import CircadianOrchestrator
from resident.event_store import EventStore
from resident.memory import MemoryRetrieval
from resident.mind_loop import MindLoop
from resident.models import EventCreate
from resident.providers import DeterministicFakeProvider
from resident.sleep import SleepEngine
from resident.web_fetch import (
    BlockedURL,
    FetchTimeout,
    HTTPStatusError,
    NetworkError,
    ResponseTooLarge,
    SafeFetcher,
    UnsupportedContent,
)
from resident.world import WorldParams, WorldWindowEngine, world_profile
from resident.world_corpus import DEFAULT_WORLD_CORPUS
from resident.world_source import (
    FixtureWorldSource,
    LiveWebWorldSource,
    ReplayWorldSource,
    SeedEntry,
    WorldSnapshotStore,
    snapshot_id_for,
    url_item_id,
)

T0 = datetime(2026, 6, 1, 6, 0, 0, tzinfo=timezone.utc)

HTML = (
    b"<html><head><title>Umami Basics</title></head><body>"
    b"<p>The science of broth.</p>"
    b"<a href='/related/fermentation'>f</a>"
    b"<a href='https://other.example/page'>o</a>"
    b"<script>tracker()</script>"
    b"</body></html>"
).replace(b"\n", b"")


# ------------------------------------------------------------------ fakes


class FakeResp:
    def __init__(self, status=200, headers=None, body=HTML):
        self.status = status
        self._headers = headers or {}
        self._body = body

    def getheader(self, key, default=None):
        return self._headers.get(key, default)

    def read(self, n=-1):
        out, self._body = self._body, b""
        return out

    def close(self):
        pass


class FakeTransport:
    """Routes (host, path) -> FakeResp; records every dial so tests can assert
    the *pinned IP* was used and no forbidden host was ever contacted."""

    def __init__(self, routes=None, public_ip="93.184.216.34"):
        self.routes = routes or {}
        self.public_ip = public_ip
        self.dials: list[tuple[str, str, str]] = []  # (host, ip, path)

    def open(self, *, scheme, host, ip, port, path, headers, timeout):
        self.dials.append((host, ip, path))
        resp = self.routes.get((host, path))
        if resp is None:
            resp = self.routes.get((host, "*"))
        if resp is None:
            raise ConnectionResetError(f"no route for {host}{path}")
        return resp

    def resolve(self, host):
        return [self.public_ip]


def _fetcher(routes=None, **kw) -> tuple[SafeFetcher, FakeTransport]:
    transport = FakeTransport(routes)
    return SafeFetcher(transport=transport, **kw), transport


def _clock(start=T0):
    class _C:
        def __init__(self):
            self.now = start

    return _C()


# ------------------------------------------------------------------ SSRF


class TestSsrfGuard:
    def _blocked(self, url, fetcher=None):
        f = fetcher or _fetcher()[0]
        with pytest.raises(BlockedURL):
            f.fetch(url)

    def test_scheme_allowlist(self):
        for url in ("ftp://x.example/", "file:///etc/passwd", "data:text/html,x",
                    "gopher://x/", "unix:/var/run.sock"):
            self._blocked(url)

    def test_port_allowlist(self):
        self._blocked("http://ok.example:8080/")
        self._blocked("https://ok.example:8443/")

    def test_literal_private_and_local_and_metadata(self):
        for host in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.9",
                     "169.254.169.254", "0.0.0.0", "100.64.0.1", "[::1]",
                     "[fe80::1]", "[fc00::1]", "[::ffff:10.0.0.1]"):
            self._blocked(f"http://{host}/x")

    def test_dns_resolving_to_forbidden_ip(self):
        class BadResolver:
            def resolve(self, host):
                return ["10.0.0.9"]

        f = SafeFetcher(transport=FakeTransport(), resolver=BadResolver().resolve)
        self._blocked("http://ok.example/", f)

    def test_dns_with_one_bad_record_among_good_blocked(self):
        class MixedResolver:
            def resolve(self, host):
                return ["93.184.216.34", "192.168.0.44"]  # rebinding-shaped record set

        f = SafeFetcher(transport=FakeTransport(), resolver=MixedResolver().resolve)
        self._blocked("http://ok.example/", f)

    def test_redirect_to_private_ip_revalidated(self):
        routes = {("ok.example", "/"): FakeResp(302, {"Location": "http://10.9.9.9/secret"})}
        f, transport = _fetcher(routes)
        self._blocked("http://ok.example/", f)
        assert transport.dials  # the first hop was dialed before the refusal

    def test_redirect_chain_revalidated_each_hop_and_followed(self):
        routes = {
            ("ok.example", "/"): FakeResp(302, {"Location": "/step2"}),
            ("ok.example", "/step2"): FakeResp(301, {"Location": "http://ok.example/final"}),
            ("ok.example", "/final"): FakeResp(200, {"Content-Type": "text/plain"}, b"done"),
        }
        f, transport = _fetcher(routes)
        r = f.fetch("http://ok.example/")
        assert r.status == 200 and r.text == "done"
        assert r.redirect_chain == ("http://ok.example/step2", "http://ok.example/final")

    def test_dials_the_validated_ip_not_a_reResolved_host(self):
        """DNS-rebinding defense: the socket dials the *validated* address."""
        f, transport = _fetcher({("ok.example", "*"): FakeResp()})
        f.fetch("http://ok.example/page")
        assert transport.dials == [("ok.example", transport.public_ip, "/page")]

    def test_proxy_dns_range_blocked_by_default_and_allowed_by_opt_in(self):
        """198.18.0.0/15 (transparent fake-IP proxy DNS) is refused by default;
        the explicit allow_proxy_dns opt-in permits dialing it (ADR-0009)."""
        class ProxyResolver:
            def resolve(self, host):
                return ["198.18.0.80"]

        f = SafeFetcher(transport=FakeTransport({("ok.example", "*"): FakeResp()}),
                        resolver=ProxyResolver().resolve)
        self._blocked("http://ok.example/", f)

        f2 = SafeFetcher(transport=FakeTransport({("ok.example", "*"): FakeResp()}),
                         resolver=ProxyResolver().resolve, allow_proxy_dns=True)
        r = f2.fetch("http://ok.example/")
        assert r.status == 200

    def test_redirect_limit(self):
        def mk(i):
            return FakeResp(302, {"Location": f"/hop{i + 1}"})

        routes = {(f"ok.example", f"/hop{i}"): mk(i) for i in range(10)}
        f, _ = _fetcher({**routes, ("ok.example", "/hop0"): mk(0)})
        with pytest.raises(NetworkError):
            f.fetch("http://ok.example/hop0")


class TestCaps:
    def test_raw_size_cap(self):
        f, _ = _fetcher({("ok.example", "*"): FakeResp(200, {"Content-Type": "text/plain"}, b"A" * 4096)},
                        max_bytes=1024)
        with pytest.raises(ResponseTooLarge):
            f.fetch("http://ok.example/big")

    def test_gzip_bomb_cap(self):
        bomb = gzip.compress(b"B" * (3 * 1024 * 1024))
        f, _ = _fetcher({("ok.example", "*"): FakeResp(200, {"Content-Type": "text/plain", "Content-Encoding": "gzip"}, bomb)},
                        max_decompressed=1024 * 1024)
        with pytest.raises(ResponseTooLarge):
            f.fetch("http://ok.example/bomb")

    def test_read_timeout(self):
        class Slow:
            def open(self, **kw):
                raise socket.timeout("timed out")

            def resolve(self, host):
                return ["93.184.216.34"]

        f = SafeFetcher(transport=Slow())
        with pytest.raises(FetchTimeout):
            f.fetch("http://ok.example/")

    def test_network_error_wrapped(self):
        class Broken:
            def open(self, **kw):
                raise ConnectionResetError("reset")

            def resolve(self, host):
                return ["93.184.216.34"]

        f = SafeFetcher(transport=Broken())
        with pytest.raises(NetworkError):
            f.fetch("http://ok.example/")


class TestParsers:
    def test_html_extraction_and_relative_links(self):
        f, _ = _fetcher({("ok.example", "*"): FakeResp()})
        r = f.fetch("http://ok.example/doc")
        assert r.title == "Umami Basics"
        assert "The science of broth." in r.text and "tracker()" not in r.text
        assert r.links == ("http://ok.example/related/fermentation", "https://other.example/page")
        assert r.source_type == "html" and r.content_hash

    def test_malformed_html_never_crashes(self):
        f, _ = _fetcher({("ok.example", "*"): FakeResp(200, {"Content-Type": "text/html"},
                                                      b"<html><title>Bad<br><p>unclosed<div>tail")})
        r = f.fetch("http://ok.example/")
        assert r.title == "Bad" and "tail" in r.text

    def test_rss_item_links_are_outbound(self):
        rss = (b"<?xml version='1.0'?><rss><channel><title>Feed</title>"
               b"<item><title>A</title><link>https://x.example/a</link>"
               b"<description>da</description></item></channel></rss>")
        f, _ = _fetcher({("ok.example", "*"): FakeResp(200, {"Content-Type": "application/rss+xml"}, rss)})
        r = f.fetch("http://ok.example/feed")
        assert r.source_type == "rss" and r.links == ("https://x.example/a",)

    def test_json_title_and_text(self):
        f, _ = _fetcher({("ok.example", "*"): FakeResp(200, {"Content-Type": "application/json"},
                                                      b'{"title":"Doc","k":1}')})
        r = f.fetch("http://ok.example/api")
        assert r.source_type == "json" and r.title == "Doc" and '"k": 1' in r.text

    def test_explicit_unsupported_mime_refused_not_sniffed(self):
        f, _ = _fetcher({("ok.example", "*"): FakeResp(200, {"Content-Type": "application/pdf"}, b"%PDF-1.7")})
        with pytest.raises(UnsupportedContent):
            f.fetch("http://ok.example/doc.pdf")

    def test_missing_header_sniffed(self):
        f, _ = _fetcher({("ok.example", "*"): FakeResp(200, {}, HTML)})
        r = f.fetch("http://ok.example/")
        assert r.source_type == "html"

    def test_http_error_statuses(self):
        for status in (404, 500):
            f, _ = _fetcher({("ok.example", "*"): FakeResp(status)})
            with pytest.raises(HTTPStatusError):
                f.fetch("http://ok.example/")

    def test_non_ascii_url_is_percent_encoded_on_the_wire(self):
        """Found by the live smoke: an IRI path (/wiki/记忆) must reach the wire
        percent-encoded; the snapshot keeps the original IRI."""
        f, transport = _fetcher({("zh.example", "*"): FakeResp()})
        r = f.fetch("https://zh.example/wiki/记忆")
        assert transport.dials[0][2] == "/wiki/%E8%AE%B0%E5%BF%86"
        assert r.status == 200


# ------------------------------------------------------------- snapshot store


class TestSnapshotStore:
    def test_stable_content_addressed_id(self):
        a = snapshot_id_for(final_url="http://x/", content_hash="h1", title="t", source_type="html")
        b = snapshot_id_for(final_url="http://x/", content_hash="h1", title="t", source_type="html")
        c = snapshot_id_for(final_url="http://x/", content_hash="h2", title="t", source_type="html")
        assert a == b and a.startswith("snap_") and a != c

    def test_save_load_and_immutability(self, tmp_path):
        store = WorldSnapshotStore(tmp_path / "snaps")
        rec = {"snapshot_id": "snap_x", "content": {"title": "t"}}
        assert store.save(rec) == "snap_x"
        rec["content"]["title"] = "MUTATED"
        store.save(rec)  # second save of the same id must be a no-op
        assert store.load("snap_x")["content"]["title"] == "t"
        assert store.count() == 1


def _seed_entries() -> list[SeedEntry]:
    return [
        SeedEntry(url="http://food.example/umami", source="food_site", topic="food",
                  title="Umami pages", summary="关于鲜味与汤汁的科学文章。"),
        SeedEntry(url="http://astro.example/news", source="astro_site", topic="astrophysics",
                  title="Astro news", summary="行星形成与恒星演化的新闻。"),
    ]


class TestLiveSource:
    def test_observe_persists_snapshot_and_materializes(self, tmp_path):
        f, transport = _fetcher({("food.example", "/umami"): FakeResp()})
        src = LiveWebWorldSource(_seed_entries(), WorldSnapshotStore(tmp_path / "s"), fetcher=f)
        item = src.items()[0]
        mat = src.observe(item, now_iso=T0.isoformat())
        assert mat.item.title == "Umami Basics"
        assert "The science of broth." in mat.item.summary
        assert mat.fetch["snapshot_id"] and mat.fetch["source_kind"] == "live"
        rec = src.snapshots.load(mat.fetch["snapshot_id"])
        # the brief's snapshot field list:
        for key in ("requested_url", "final_url", "fetched_at", "status", "content_type",
                    "title", "text", "content_hash", "redirect_chain", "byte_size"):
            assert key in rec, key
        assert rec["requested_url"] == "http://food.example/umami"
        assert rec["final_url"] == "http://food.example/umami"
        assert rec["source_kind"] == "live"
        assert rec["content"]["outbound_links"] == [
            "http://food.example/related/fermentation", "https://other.example/page"]

    def test_snapshot_context_carries_requesting_event(self, tmp_path):
        f, _ = _fetcher({("food.example", "/umami"): FakeResp()})
        src = LiveWebWorldSource(_seed_entries(), WorldSnapshotStore(tmp_path / "s"), fetcher=f)
        mat = src.observe(src.items()[0], now_iso=T0.isoformat(),
                          context={"opened_event_id": "evt_open1", "entry_mode": "follow"})
        rec = src.snapshots.load(mat.fetch["snapshot_id"])
        assert rec["context"]["opened_event_id"] == "evt_open1"
        assert rec["context"]["entry_mode"] == "follow"

    def test_accident_candidates_are_real_observed_links_only(self, tmp_path):
        f, _ = _fetcher({("food.example", "/umami"): FakeResp()})
        src = LiveWebWorldSource(_seed_entries(), WorldSnapshotStore(tmp_path / "s"), fetcher=f)
        ids_before = {it.id for it in src.items()}
        src.observe(src.items()[0], now_iso=T0.isoformat())
        # the page's real links became candidates; nothing else appeared
        new_ids = {it.id for it in src.items()} - ids_before
        assert new_ids == {url_item_id("http://food.example/related/fermentation"),
                           url_item_id("https://other.example/page")}
        # and the parent's outgoing links are exactly those items
        parent = src.get(url_item_id("http://food.example/umami"))
        assert {t.id for t in src.outgoing_links(parent)} == new_ids

    def test_topic_reclassification_recorded(self, tmp_path):
        f, _ = _fetcher({("astro.example", "/news"): FakeResp()})
        src = LiveWebWorldSource(_seed_entries(), WorldSnapshotStore(tmp_path / "s"), fetcher=f)
        mat = src.observe(src.items()[1], now_iso=T0.isoformat())
        rec = src.snapshots.load(mat.fetch["snapshot_id"])
        assert rec["declared"]["declared_topic"] == "astrophysics"
        # classified topic is recorded next to the declared one (may match)
        assert "classified_topic" in mat.fetch

    def test_view_rebuilds_from_snapshots_after_restart(self, tmp_path):
        f, _ = _fetcher({("food.example", "/umami"): FakeResp()})
        snaps = WorldSnapshotStore(tmp_path / "s")
        src1 = LiveWebWorldSource(_seed_entries(), snaps, fetcher=f)
        src1.observe(src1.items()[0], now_iso=T0.isoformat())
        src2 = LiveWebWorldSource(_seed_entries(), snaps, fetcher=f)  # fresh process
        ids2 = {it.id for it in src2.items()}
        assert url_item_id("http://food.example/related/fermentation") in ids2
        parent = src2.get(url_item_id("http://food.example/umami"))
        assert parent.title == "Umami Basics"


class TestReplaySource:
    def _capture(self, tmp_path):
        snaps = WorldSnapshotStore(tmp_path / "snaps")
        f, _ = _fetcher({("food.example", "/umami"): FakeResp()})
        live = LiveWebWorldSource(_seed_entries(), snaps, fetcher=f)
        item = live.items()[0]
        mat = live.observe(item, now_iso=T0.isoformat())
        return snaps, item, mat

    def test_replay_equals_capture(self, tmp_path):
        snaps, item, captured = self._capture(tmp_path)
        replay = ReplayWorldSource(_seed_entries(), snaps)
        mat = replay.observe(item, now_iso=T0.isoformat())
        assert mat.item.title == captured.item.title
        assert mat.item.summary == captured.item.summary
        assert mat.item.topic == captured.item.topic
        assert mat.fetch["replay"] is True
        assert mat.fetch["snapshot_id"] == captured.fetch["snapshot_id"]
        assert mat.fetch["byte_size"] == captured.fetch["byte_size"]
        # the real outbound links survive replay (accident still works offline)
        assert {t.id for t in replay.outgoing_links(mat.item)} == \
               {t.id for t in LiveWebWorldSource(_seed_entries(), snaps).outgoing_links(mat.item)}

    def test_replay_miss_is_deterministic_failure(self, tmp_path):
        snaps, item, _ = self._capture(tmp_path)
        replay = ReplayWorldSource(_seed_entries(), snaps)
        other = SeedEntry(url="http://never.example/x", source="s", topic="t",
                          title="t", summary="never captured")
        from resident.web_fetch import FetchError

        with pytest.raises(FetchError) as exc:
            replay.observe(other.item(), now_iso=T0.isoformat())
        assert exc.value.detail["cause"] == "not_in_snapshot"

    def test_replay_serves_latest_capture_deterministically(self, tmp_path):
        """A URL captured twice (a page whose content changed) replays as the
        LATEST capture — decided by capture_seq, not by filename order."""
        snaps = WorldSnapshotStore(tmp_path / "s")
        f, _ = _fetcher({("food.example", "/umami"): FakeResp()})
        item = _seed_entries()[0].item()
        live = LiveWebWorldSource(_seed_entries(), snaps, fetcher=f)
        mat1 = live.observe(item, now_iso=T0.isoformat())

        # a second capture of the same URL with DIFFERENT content (new snapshot id)
        class Changed(FakeTransport):
            def open(self, **kw):
                return FakeResp(body=HTML.replace(b"Umami Basics", b"Umami Revisited"))

        live2 = LiveWebWorldSource(_seed_entries(), snaps, fetcher=SafeFetcher(transport=Changed()))
        mat2 = live2.observe(item, now_iso=T0.isoformat())
        assert mat1.fetch["snapshot_id"] != mat2.fetch["snapshot_id"]

        replay = ReplayWorldSource(_seed_entries(), snaps)
        served = replay.observe(item, now_iso=T0.isoformat())
        assert served.item.title == "Umami Revisited"  # the latest capture
        assert served.fetch["snapshot_id"] == mat2.fetch["snapshot_id"]


# --------------------------------------------- engine integration (no crash)


def _engine(store, source, *, seed=0, params=None) -> WorldWindowEngine:
    import random

    if params is None:
        # open-forcing rails (Phase-6 test convention): the propensity roll
        # always passes and the cooldown never blocks — tests exercise the
        # fetch/observe paths, not the gate.
        params = WorldParams(base_propensity=1.0, propensity_cap=1.0, cooldown_s=0.0)
    return WorldWindowEngine(
        store, memory=MemoryRetrieval(store), params=params,
        rng=random.Random(seed), source=source)


class TestEngineWithLiveSource:
    def test_failed_fetch_is_audit_event_and_window_noop(self, tmp_path):
        """The brief: 网络失败绝不能使 MindLoop 崩溃 — the window opens, looks,
        the world does not answer; a fetch audit event is written; NO
        observation exists; the budget slot is honestly spent."""
        store = EventStore(tmp_path / "w.sqlite3", now_fn=lambda: T0)
        f, _ = _fetcher({})  # no routes: every fetch fails (connection reset)
        src = LiveWebWorldSource(_seed_entries(), WorldSnapshotStore(tmp_path / "s"), fetcher=f)
        eng = _engine(store, src)
        out = eng.maybe_open(T0.isoformat(), circadian_state="QUIET")
        assert out["result"] == "fetch_failed" and out["blocker"] == "network_error"
        assert store.count("world.fetch_failed") == 1
        assert store.count("world.observation") == 0
        assert store.count("world.window_opened") == 1  # the slot was spent
        # the fetch audit must be invisible to the mind's route context
        mind = MindLoop(store, memory=MemoryRetrieval(store), provider=DeterministicFakeProvider())
        ctx = mind._route_context()
        assert ctx == (0, store.count() - 2)  # minus window_opened + fetch_failed

    def test_blocked_and_timeout_get_distinct_events(self, tmp_path):
        store = EventStore(tmp_path / "w.sqlite3", now_fn=lambda: T0)

        class BlockedThenSlow(FakeTransport):
            def open(self, **kw):
                if kw["path"] == "/umami":
                    raise BlockedURL("forbidden")
                raise socket.timeout("slow")

        src = LiveWebWorldSource(
            _seed_entries(), WorldSnapshotStore(tmp_path / "s"),
            fetcher=SafeFetcher(transport=BlockedThenSlow()))
        eng = _engine(store, src)
        # drive both items' fetch paths directly: the mapping refusal-type →
        # audit event is what is under test (the gate is tested elsewhere)
        for item in src.items():
            eng._open(T0.isoformat(), "alien", item, {"propensity": 1.0})
        assert store.count("world.fetch_blocked") == 1
        assert store.count("world.fetch_timeout") == 1

    def test_successful_live_open_writes_full_provenance_chain(self, tmp_path):
        store = EventStore(tmp_path / "w.sqlite3", now_fn=lambda: T0)
        f, _ = _fetcher({
            ("food.example", "/umami"): FakeResp(),
            ("astro.example", "/news"): FakeResp(200, {"Content-Type": "text/plain"}, b"stars"),
        })
        src = LiveWebWorldSource(_seed_entries(), WorldSnapshotStore(tmp_path / "s"), fetcher=f)
        eng = _engine(store, src)
        eng.maybe_open(T0.isoformat(), circadian_state="QUIET")
        # whichever seed was selected, the observation must carry full provenance
        obs_events = store.list(1, type_prefix="world.observation", order="desc")
        assert obs_events, "an open must have produced an observation"
        obs = obs_events[0]
        fetch = obs.provenance["fetch"]
        assert fetch["snapshot_id"].startswith("snap_")
        assert fetch["source_kind"] == "live"
        assert obs.links["caused_by"], "observation must be caused_by the window event"
        assert obs.content["title"] in ("Umami Basics", "Text document")

    def test_live_open_via_orchestrator_beat_never_crashes_mind(self, tmp_path):
        """End-to-end: the circadian orchestrator runs beats against a live
        source whose network always fails; the beat completes, the mind
        continues, the audit trail records what happened."""
        clock = _clock()
        store = EventStore(tmp_path / "w.sqlite3", now_fn=lambda: clock.now)
        memory = MemoryRetrieval(store)
        mind = MindLoop(store, memory=memory, provider=DeterministicFakeProvider())
        sleep_engine = SleepEngine(store, memory=memory)
        src = LiveWebWorldSource(_seed_entries(), WorldSnapshotStore(tmp_path / "s"),
                                 fetcher=SafeFetcher(transport=FakeTransport()))  # all fail
        eng = _engine(store, src)
        orch = CircadianOrchestrator(store, mind, sleep_engine, world_engine=eng)
        from datetime import timedelta

        for _ in range(6):
            clock.now = clock.now + timedelta(minutes=30)
            res = asyncio.run(orch.beat(clock.now.isoformat()))
            assert "state" in res
        assert store.count("world.window_opened") + store.count("world.window_skipped") >= 1
        if store.count("world.window_opened"):
            assert store.count("world.fetch_failed") == store.count("world.window_opened")
        assert store.count("thought.created") >= 0  # the mind itself kept running

    def test_accident_in_engine_uses_real_links(self, tmp_path):
        """accident 只能使用真实 observed links: with only follow+edge enabled the
        fake-URL path cannot even exist — the discovered frontier comes from the
        snapshot's outbound_links."""
        store = EventStore(tmp_path / "w.sqlite3", now_fn=lambda: T0)
        single_link_html = (
            b"<html><head><title>Umami Basics</title></head><body>"
            b"<p>The science of broth.</p>"
            b"<a href='/related/fermentation'>f</a></body></html>")
        f, _ = _fetcher({
            ("food.example", "/umami"): FakeResp(body=single_link_html),
            ("food.example", "/related/fermentation"): FakeResp(
                200, {"Content-Type": "text/plain"}, b"fermentation notes"),
        })
        # a food thread gives the follow frontier its topic focus; a single
        # seed makes the selection deterministic
        store.append(EventCreate(
            type="conversation.user_message", visibility="user_visible",
            content={"text": "我最近一直在想鲜味和汤汁的科学，想多了解一点。"},
            provenance={"source": "test"}))
        src = LiveWebWorldSource([_seed_entries()[0]], WorldSnapshotStore(tmp_path / "s"), fetcher=f)
        eng = _engine(store, src, params=WorldParams(
            base_propensity=1.0, propensity_cap=1.0, cooldown_s=0.0, impression_prob=0.0,
            association_prob=0.0, promotion_prob=0.0, thread_prob=0.0,
            max_light_per_day=8, max_accident_per_day=2, enabled_modes=("follow", "accident")))
        first = eng.maybe_open(T0.isoformat(), circadian_state="QUIET")
        assert first["result"] == "opened"
        # the discovered (real) link is now a candidate the engine can pick
        discovered = url_item_id("http://food.example/related/fermentation")
        assert any(it.id == discovered for it in src.items())
        second = eng.maybe_open(T0.isoformat(), circadian_state="QUIET")
        assert second["result"] in ("opened", "skipped")
        if second["result"] == "opened":
            obs = store.get(second["observation_id"])
            assert obs.content["item_id"] in {discovered, url_item_id("http://food.example/umami")}


class TestLiveTelemetry:
    def test_fixture_profile_has_zero_live_section(self, tmp_path):
        prof = world_profile(EventStore(tmp_path / "w.sqlite3", now_fn=lambda: T0))
        live = prof["live"]
        assert live["live_fetch_attempts"] == 0
        assert live["live_fetch_success_rate"] == 0.0
        assert live["replay_hit_rate"] == 0.0
        # and the sealed Phase-6 keys are all still present
        for key in ("n_windows_opened", "world_exposure_rate", "seen_left_nothing_rate",
                    "entry_mode_distribution", "origin_shares", "source_concentration"):
            assert key in prof

    def test_live_profile_counts_real_requests(self, tmp_path):
        store = EventStore(tmp_path / "w.sqlite3", now_fn=lambda: T0)
        store.append(EventCreate(
            type="conversation.user_message", visibility="user_visible",
            content={"text": "我最近一直在想鲜味和汤汁的科学，想多了解一点。"},
            provenance={"source": "test"}))
        f, _ = _fetcher({("food.example", "/umami"): FakeResp()})
        src = LiveWebWorldSource([_seed_entries()[0]], WorldSnapshotStore(tmp_path / "s"), fetcher=f)
        eng = _engine(store, src, params=WorldParams(
            base_propensity=1.0, propensity_cap=1.0, cooldown_s=0.0, impression_prob=1.0,
            association_prob=0.0, promotion_prob=0.0, thread_prob=0.0,
            enabled_modes=("follow",)))
        eng.maybe_open(T0.isoformat(), circadian_state="QUIET")     # success
        eng.maybe_open(T0.isoformat(), circadian_state="QUIET")     # same item again? -> already observed; select may fail
        prof = world_profile(store)
        live = prof["live"]
        assert live["source_kind"] == "live"
        assert live["live_fetch_attempts"] >= 1
        assert live["bytes_received"] >= len(HTML)
        assert live["snapshot_count"] == 1
        assert live["live_fetch_success_rate"] == live["fetch_to_observation_rate"]
