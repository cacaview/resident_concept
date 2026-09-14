"""The WorldSource seam (v0.2 Step 1, ADR-0009): the World Window's *visual
organ*, split from its *mind*.

The sealed Phase-6 engine (:mod:`resident.world`) never learns what kind of
world it is looking at — fixture, replay, or live web. It only sees the small
read surface below (the shape :class:`~resident.world_corpus.WorldCorpus`
already had), plus one new call, :meth:`WorldSource.observe`, which
*materializes* a candidate into real content:

- **FixtureWorldSource** — the sealed, in-code corpus; ``observe`` is the
  identity. Byte-preserving: the sealed A/B/C regression must not move.
- **ReplayWorldSource** — serves previously *captured* snapshots (immutable
  JSON, content-hash addressed) fully offline and deterministically; a URL
  missing from the recording is a deterministic ``not_in_snapshot`` failure,
  never a live request.
- **LiveWebWorldSource** — fetches the real web through the SSRF-guarded
  :class:`~resident.web_fetch.SafeFetcher`, writes an immutable snapshot, and
  derives accident candidates from the page's *real* outbound links (a URL the
  resident never observed cannot pose as "accidentally encountered").

Capture + Replay (the v0.2 brief): real internet is not reproducible, so every
live fetch is persisted as an **immutable snapshot** (requested/final URL,
status, content type, title, normalized text, content hash, redirect chain,
sizes, timings, outbound links, and the window-event context that requested
it). Snapshots are addressed by a stable content-derived id, so identical
content refetches dedupe and a replay reconstructs *exactly* what was seen.
Simulations stay deterministic: run them on the fixture or on the replay of a
capture — never on the live network.

Provenance: the *event log* remains the provenance record of record (every
observation carries ``snapshot_id``; the dangling-link invariant still makes a
 fabricated observation impossible). The snapshot additionally embeds the
requesting event's id, so snapshot → event → thought is traceable both ways.
"""
from __future__ import annotations

import hashlib
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import urlsplit

from .web_fetch import FetchError, SafeFetcher
from .world_corpus import WorldCorpus, WorldItem

__all__ = [
    "WorldSource", "ObservedMaterial", "SeedEntry",
    "FixtureWorldSource", "WorldSnapshotStore", "ReplayWorldSource",
    "LiveWebWorldSource", "snapshot_id_for",
]

#: placeholder summary for a link discovered on a page but not yet opened
_UNOPENED_TMPL = "一个由「{parent}」链接而来、还未打开的页面"


def snapshot_id_for(*, final_url: str, content_hash: str, title: str, source_type: str) -> str:
    """Stable, content-derived snapshot id: the same document fetched twice
    yields the same id (dedupe), and a replay recomputes it identically."""
    basis = json.dumps(
        {"final_url": final_url, "content_hash": content_hash,
         "title": title, "source_type": source_type},
        ensure_ascii=False, sort_keys=True)
    return "snap_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def url_item_id(url: str) -> str:
    """Stable item id for a URL (seeds and discovered links share the scheme,
    so a discovered link that equals a seed is the same item)."""
    return "u_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SeedEntry:
    """A configured, bounded place the live/replay source may look. The
    declared topic drives frontier selection until real content arrives (the
    v0.2 lexical classifier may re-tag it at observation time — recorded
    honestly in the snapshot). This is a *fixed frontier*, not a search
    engine: nothing here crawls or expands on its own."""

    url: str
    source: str            # declared publisher/site label
    topic: str             # declared topic (frontier hint)
    title: str = ""
    summary: str = ""

    def item(self) -> WorldItem:
        title = self.title or urlsplit(self.url).netloc
        summary = self.summary or "一个配置好的观察入口（还未打开）。"
        return WorldItem(id=url_item_id(self.url), source=self.source,
                         topic=self.topic, title=title, summary=summary, links=())


@dataclass(frozen=True)
class ObservedMaterial:
    """What ``observe`` returns: the materialized item + the fetch/snapshot
    metadata (``None`` for the fixture — the sealed path stays byte-identical)."""

    item: WorldItem
    fetch: dict | None = None


class WorldSource(ABC):
    """The read surface the World Window is allowed to know about. Everything
    here is exactly the shape :class:`WorldCorpus` already exposed, plus
    ``observe`` (materialize) and ``topic_adjacency`` (the cross-topic frontier,
    which for live sources grows as real pages are observed)."""

    kind: str = "fixture"

    @abstractmethod
    def items(self) -> tuple[WorldItem, ...]:
        """The enumerable candidate frontier (deterministic order)."""

    @abstractmethod
    def get(self, item_id: str) -> WorldItem | None: ...

    @abstractmethod
    def outgoing_links(self, item: WorldItem) -> list[WorldItem]: ...

    @abstractmethod
    def topic_adjacency(self) -> dict[str, set[str]]:
        """Symmetric cross-topic adjacency derived from the source's own links."""

    @abstractmethod
    def observe(self, item: WorldItem, *, now_iso: str,
                context: dict | None = None) -> ObservedMaterial:
        """Materialize a candidate into real content. ``context`` (the requesting
        window event id / entry mode) is embedded into the snapshot's provenance
        so snapshot → event → thought is traceable. Fixture: identity.
        Live: fetch + snapshot (raises a typed :class:`FetchError` on failure —
        the engine turns that into an audit event and the window no-ops)."""


class FixtureWorldSource(WorldSource):
    """The sealed hermetic corpus, unchanged. ``observe`` is the identity —
    this is what keeps the v0.1/Phase-6 behavior byte-for-byte."""

    kind = "fixture"

    def __init__(self, corpus: WorldCorpus | None = None):
        self.corpus = corpus

    def items(self) -> tuple[WorldItem, ...]:
        return self.corpus.items

    def get(self, item_id: str) -> WorldItem | None:
        return self.corpus.get(item_id)

    def outgoing_links(self, item: WorldItem) -> list[WorldItem]:
        return self.corpus.outgoing_links(item)

    def topic_adjacency(self) -> dict[str, set[str]]:
        adj: dict[str, set[str]] = {t: set() for t in self.corpus.topics()}
        for it in self.corpus.items:
            for target in self.corpus.outgoing_links(it):
                if target.topic != it.topic:
                    adj[it.topic].add(target.topic)
                    adj[target.topic].add(it.topic)
        return adj

    def observe(self, item: WorldItem, *, now_iso: str,
                context: dict | None = None) -> ObservedMaterial:
        return ObservedMaterial(item=item, fetch=None)


# ------------------------------------------------------------------ snapshots


class WorldSnapshotStore:
    """Immutable, content-addressed snapshot files (``<id>.json``), written
    atomically once. A repeated save of the same id is a no-op that returns
    the existing record's id — snapshots are never rewritten."""

    def __init__(self, directory: str | os.PathLike):
        self.directory = str(directory)
        os.makedirs(self.directory, exist_ok=True)
        self._seq = max((self._seq_of(n) for n in os.listdir(self.directory)
                         if n.endswith(".json")), default=0)

    def _seq_of(self, filename: str) -> int:
        try:
            with open(os.path.join(self.directory, filename), encoding="utf-8") as fh:
                return int(json.load(fh).get("capture_seq", 0))
        except Exception:
            return 0

    def save(self, record: dict) -> str:
        sid = record["snapshot_id"]
        path = os.path.join(self.directory, f"{sid}.json")
        if not os.path.exists(path):  # immutability: first write wins
            if "capture_seq" not in record:
                self._seq += 1
                record["capture_seq"] = self._seq
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, path)
        return sid

    def load(self, snapshot_id: str) -> dict | None:
        path = os.path.join(self.directory, f"{snapshot_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def has(self, snapshot_id: str) -> bool:
        return os.path.exists(os.path.join(self.directory, f"{snapshot_id}.json"))

    def count(self) -> int:
        return sum(1 for name in os.listdir(self.directory) if name.endswith(".json"))

    def all_ids(self) -> list[str]:
        return sorted(n[:-5] for n in os.listdir(self.directory) if n.endswith(".json"))


def _item_from_snapshot(rec: dict) -> WorldItem:
    return WorldItem(
        id=rec["context"]["item_id"],
        source=rec["declared"]["source"],
        topic=rec["content"].get("topic") or rec["declared"]["topic"],
        title=rec.get("title") or rec["content"].get("title") or "(untitled)",
        summary=rec["content"]["summary"],
        links=(),
    )


class _SnapshotBackedBase(WorldSource):
    """Shared machinery of the replay + live sources: a bounded seed frontier +
    an id-keyed view built from the immutable snapshot store (rebuildable — the
    snapshots, not memory, are the record)."""

    def __init__(self, seeds: list[SeedEntry], snapshots: WorldSnapshotStore):
        self.seeds = list(seeds)
        self.snapshots = snapshots
        self._by_id: dict[str, WorldItem] = {}
        self._links_by_id: dict[str, list[str]] = {}   # item id -> absolute URLs
        self._url_by_id: dict[str, str] = {}           # item id -> fetchable URL
        self._rec_by_id: dict[str, dict] = {}          # item id -> latest record
        for seed in self.seeds:
            it = seed.item()
            self._by_id[it.id] = it
            self._url_by_id[it.id] = seed.url
        # rebuild the observed view from the snapshots (replayability + restart);
        # for an item captured more than once, the LATEST capture wins
        # (deterministically by capture_seq, not by filename order)
        for sid in self.snapshots.all_ids():
            rec = self.snapshots.load(sid) or {}
            self._absorb(rec)

    # -- view maintenance --------------------------------------------------

    def _absorb(self, rec: dict) -> None:
        """Fold one snapshot into the view: the real item supersedes the seed
        placeholder, and its real outbound links become discovered candidates."""
        if not rec or "context" not in rec:
            return
        item_id = rec["context"].get("item_id")
        prev = self._rec_by_id.get(item_id)
        if prev is not None and prev.get("capture_seq", 0) > rec.get("capture_seq", 0):
            return  # an older capture of an item we already hold a newer one for
        self._rec_by_id[item_id] = rec
        item = _item_from_snapshot(rec)
        self._by_id[item.id] = item
        urls = rec["content"].get("outbound_links") or []
        self._links_by_id[item.id] = list(urls)
        parent_title = item.title
        parent_topic = item.topic
        parent_source = item.source
        for url in urls:
            uid = url_item_id(url)
            self._url_by_id.setdefault(uid, url)
            if uid not in self._by_id:
                tail = urlsplit(url).path.rsplit("/", 1)[-1] or urlsplit(url).netloc
                self._by_id[uid] = WorldItem(
                    id=uid, source=parent_source, topic=parent_topic,
                    title=tail or "(未命名链接)",
                    summary=_UNOPENED_TMPL.format(parent=parent_title), links=())

    # -- WorldSource reads -------------------------------------------------

    def items(self) -> tuple[WorldItem, ...]:
        return tuple(self._by_id.values())

    def get(self, item_id: str) -> WorldItem | None:
        return self._by_id.get(item_id)

    def outgoing_links(self, item: WorldItem) -> list[WorldItem]:
        out = []
        for url in self._links_by_id.get(item.id, []):
            target = self._by_id.get(url_item_id(url))
            if target is not None:
                out.append(target)
        return out

    def topic_adjacency(self) -> dict[str, set[str]]:
        adj: dict[str, set[str]] = {}
        for it in self._by_id.values():
            adj.setdefault(it.topic, set())
        for it in self._by_id.values():
            for target in self.outgoing_links(it):
                if target.topic != it.topic:
                    adj.setdefault(it.topic, set()).add(target.topic)
                    adj.setdefault(target.topic, set()).add(it.topic)
        return adj


class ReplayWorldSource(_SnapshotBackedBase):
    """Fully offline, deterministic: serves exactly what was captured. A URL
    outside the recording fails *deterministically* (``not_in_snapshot``) —
    replay never touches the network, so a regression run on a recording is
    byte-reproducible."""

    kind = "replay"

    def __init__(self, seeds: list[SeedEntry], snapshots: WorldSnapshotStore):
        super().__init__(seeds, snapshots)

    def observe(self, item: WorldItem, *, now_iso: str,
                context: dict | None = None) -> ObservedMaterial:
        rec = self._record_for(item)
        if rec is None:
            raise FetchError(
                f"replay recording has no snapshot for {item.id}",
                detail={"cause": "not_in_snapshot", "item_id": item.id})
        material = _item_from_snapshot(rec)
        fetch = {
            "source_kind": "replay", "snapshot_id": rec["snapshot_id"],
            "requested_url": rec["requested_url"], "final_url": rec["final_url"],
            "status": rec["status"], "content_type": rec["content_type"],
            "source_type": rec["source_type"], "byte_size": rec["byte_size"],
            "redirect_count": len(rec.get("redirect_chain") or []),
            "redirect_chain": list(rec.get("redirect_chain") or []),
            "fetched_at": rec["fetched_at"], "duration_ms": rec.get("duration_ms", 0),
            "replay": True, "declared_topic": rec["declared"]["topic"],
        }
        return ObservedMaterial(item=material, fetch=fetch)

    def _record_for(self, item: WorldItem) -> dict | None:
        return self._rec_by_id.get(item.id)


class LiveWebWorldSource(_SnapshotBackedBase):
    """The real visual organ: fetch through the SSRF-guarded
    :class:`SafeFetcher`, persist an immutable snapshot, derive accident
    candidates from the page's real outbound links. A v0.2 lexical classifier
    (same hashing embedder family as the focus matcher) may re-tag the topic
    from the *content*; both the declared and the classified topic are recorded
    — the honest provenance of the tag. The seed list is the *entire* frontier:
    nothing searches, crawls, or expands on its own."""

    kind = "live"

    def __init__(self, seeds: list[SeedEntry], snapshots: WorldSnapshotStore,
                 fetcher: SafeFetcher | None = None, *,
                 classify_threshold: float = 0.06):
        super().__init__(seeds, snapshots)
        self.fetcher = fetcher or SafeFetcher()
        self._classify_threshold = classify_threshold
        # topic prototypes from the declared seeds (first per topic, file order)
        self._protos: dict[str, list[float]] = {}
        self._embedder = None
        seen: set[str] = set()
        for seed in self.seeds:
            if seed.topic in seen or not seed.summary:
                continue
            seen.add(seed.topic)
            vec = self._embed(seed.summary)
            if any(vec):
                self._protos[seed.topic] = vec

    # -- live observation --------------------------------------------------

    def observe(self, item: WorldItem, *, now_iso: str,
                context: dict | None = None) -> ObservedMaterial:
        url = self._url_of(item)
        if url is None:
            raise FetchError(f"item {item.id} has no fetchable URL",
                             detail={"cause": "no_url", "item_id": item.id})
        result = self.fetcher.fetch(url)
        declared = self._declared_of(item)
        topic = declared["topic"]
        classified = self._classify(result.text)
        if classified is not None:
            topic = classified
        material = WorldItem(
            id=item.id, source=declared["source"], topic=topic,
            title=result.title, summary=result.text[:300], links=())
        record = {
            "snapshot_id": snapshot_id_for(
                final_url=result.final_url, content_hash=result.content_hash,
                title=result.title, source_type=result.source_type),
            "requested_url": result.requested_url,
            "final_url": result.final_url,
            "fetched_at": result.fetched_at or now_iso,
            "status": result.status,
            "content_type": result.content_type,
            "source_type": result.source_type,
            "title": result.title,
            "text": result.text,
            "excerpt": result.excerpt,
            "content_hash": result.content_hash,
            "byte_size": result.byte_size,
            "duration_ms": result.duration_ms,
            "redirect_chain": list(result.redirect_chain),
            "source_kind": "live",
            "declared": dict(declared, url=url, declared_topic=declared["topic"]),
            "content": {
                "topic": topic,
                "summary": material.summary,
                "outbound_links": list(result.links),
            },
            "context": dict(context or {}, item_id=item.id),
        }
        self.snapshots.save(record)
        self._absorb(record)
        fetch = {
            "source_kind": "live", "snapshot_id": record["snapshot_id"],
            "requested_url": result.requested_url, "final_url": result.final_url,
            "status": result.status, "content_type": result.content_type,
            "source_type": result.source_type, "byte_size": result.byte_size,
            "redirect_count": len(result.redirect_chain),
            "redirect_chain": list(result.redirect_chain),
            "fetched_at": record["fetched_at"], "duration_ms": result.duration_ms,
            "replay": False, "declared_topic": declared["topic"],
            "classified_topic": classified,
        }
        return ObservedMaterial(item=material, fetch=fetch)

    # -- helpers -----------------------------------------------------------

    def _url_of(self, item: WorldItem) -> str | None:
        return self._url_by_id.get(item.id)

    def _declared_of(self, item: WorldItem) -> dict:
        for seed in self.seeds:
            if seed.item().id == item.id:
                return {"source": seed.source, "topic": seed.topic, "url": seed.url}
        # a discovered (accident) candidate: source/topic inherited at discovery
        return {"source": item.source, "topic": item.topic, "url": None}

    def _embed(self, text: str) -> list[float]:
        from .providers import HashingEmbeddingProvider

        if self._embedder is None:
            self._embedder = HashingEmbeddingProvider()
        return self._embedder.embed([text])[0]

    def _classify(self, text: str) -> str | None:
        """Nearest declared-topic prototype above the threshold, or None (keep
        the declared topic). Lexical v0.2 only — real embeddings are step 2."""
        if not self._protos or not text:
            return None
        qv = self._embed(text[:2000])
        best_topic, best = None, 0.0
        for topic, vec in self._protos.items():
            score = _cosine(qv, vec)
            if score > best:
                best, best_topic = score, topic
        return best_topic if best >= self._classify_threshold else None


def _cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return num / (na * nb)
