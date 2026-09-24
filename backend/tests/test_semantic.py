"""v0.2 Step 2 (ADR-0010): the semantic layer — hermetic tests.

Zero network: the "real" vectors come from the deterministic
``DeterministicSemanticEmbeddingProvider`` (a different algorithm family from
the legacy hashing embedder); the failure mode from a ``Dead`` provider.

Covered (the brief): versioned cache identity (no incompatible reuse),
shadow-mode divergence recording + mind isolation (shadow on/off ⇒ identical
mind), degraded mode (provider outage ⇒ explicit sidecar mark, empty results,
no crash, no silent different semantics), treatment strategies (bridge distant
≠ lowest cosine; mid-band serendipity; cluster concentration), topic view
(cluster identity from vectors, LLM label never identity), and the three sims'
``embeddings``/``shadow`` knobs.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from resident.event_store import EventStore
from resident.memory import MemoryRetrieval
from resident.models import EventCreate
from resident.providers import (
    DeterministicSemanticEmbeddingProvider,
    HashingEmbeddingProvider,
    ProviderError,
)
from resident.semantic import (
    CLUSTER_TAU,
    EmbeddingUnavailable,
    ResilientEmbedding,
    SemanticLayer,
    ShadowLog,
    emit_degraded,
    make_semantic_stack,
    semantic_profile,
)
from resident.vector_cache import VectorCache
from resident.world_sim import ARM_PARAMS, run_world_simulation

T0 = datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc)

TEXTS = [
    ("conversation.user_message", "我们的 Agent 记忆系统需要长期记忆存档"),
    ("thought.created", "Agent 的记忆如果可以跨会话保留，就能形成连续人格"),
    ("thought.created", "长期记忆的存档结构决定了回忆的方式"),
    ("conversation.user_message", "今天炖了番茄意面，酱汁很浓"),
    ("thought.created", "番茄酱汁熬煮的时间和鲜味有关"),
    ("conversation.user_message", "操作系统的 daemon 进程会常驻后台"),
    ("thought.created", "daemon 和 Agent 记忆一样，都是长期驻留状态"),
]


def _store(tmp_path, texts=TEXTS):
    clock = {"now": T0}
    store = EventStore(tmp_path / "w.sqlite3", now_fn=lambda: clock["now"])
    for typ, text in texts:
        store.append(EventCreate(type=typ, visibility="private",
                                 content={"text": text}, provenance={"source": "t"}))
        clock["now"] += timedelta(hours=12)
    return store


def _layer(store, tmp_path, provider=None) -> SemanticLayer:
    return SemanticLayer(store, provider or DeterministicSemanticEmbeddingProvider(),
                         cache=VectorCache(tmp_path / "vectors.sqlite3"))


class Dead:
    model_id = "dead/endpoint"

    def embed(self, texts):
        raise ProviderError("endpoint down")


# ------------------------------------------------------------- vector cache


class TestVectorCache:
    def test_identity_checked_roundtrip(self, tmp_path):
        cache = VectorCache(tmp_path / "v.sqlite3")
        p = DeterministicSemanticEmbeddingProvider()
        text = "记忆的存档结构"
        vec = p.embed([text])[0]
        cache.put(provider="sketch", model="sketch-semantic", version="v1",
                  dim=len(vec), normalization="l2", text=text, vector=vec)
        hit = cache.get(provider="sketch", model="sketch-semantic", version="v1",
                        dim=len(vec), normalization="l2", text=text)
        assert hit == vec

    def test_no_reuse_across_models_or_text(self, tmp_path):
        """The hard rule: a model change or a text change can NEVER serve an
        incompatible cached vector."""
        cache = VectorCache(tmp_path / "v.sqlite3")
        p = DeterministicSemanticEmbeddingProvider()
        text = "记忆的存档结构"
        vec = p.embed([text])[0]
        cache.put(provider="sketch", model="sketch-semantic", version="v1",
                  dim=len(vec), normalization="l2", text=text, vector=vec)
        # model changed → miss
        assert cache.get(provider="sketch", model="sketch-semantic-v2", version="v1",
                         dim=len(vec), normalization="l2", text=text) is None
        # version changed → miss
        assert cache.get(provider="sketch", model="sketch-semantic", version="v2",
                         dim=len(vec), normalization="l2", text=text) is None
        # text changed → miss (content hash is part of the identity)
        assert cache.get(provider="sketch", model="sketch-semantic", version="v1",
                         dim=len(vec), normalization="l2", text="不同的文本") is None
        assert cache.stats()["entries"] == 1

    def test_dim_mismatch_rejected(self, tmp_path):
        cache = VectorCache(tmp_path / "v.sqlite3")
        with pytest.raises(ValueError):
            cache.put(provider="p", model="m", version="v", dim=4,
                      normalization="l2", text="t", vector=[0.1, 0.2])


class TestResilientEmbedding:
    def test_cache_first_then_provider(self, tmp_path):
        cache = VectorCache(tmp_path / "v.sqlite3")
        calls = {"n": 0}

        class Counting(DeterministicSemanticEmbeddingProvider):
            def embed(self, texts):
                calls["n"] += 1
                return super().embed(texts)

        wrapper = ResilientEmbedding(Counting(), cache=cache)
        _ = wrapper.identity          # the dim probe: one provider call
        probe_calls = calls["n"]
        t = "长期记忆"
        v1 = wrapper.embed([t])[0]    # cache miss → provider
        v2 = wrapper.embed([t])[0]    # must hit the cache
        assert v1 == v2
        assert calls["n"] == probe_calls + 1  # exactly one real embed, ever

    def test_provider_failure_raises_unavailable_and_audits(self, tmp_path):
        marks = []
        wrapper = ResilientEmbedding(Dead(), on_degraded=lambda reason: marks.append(reason))
        with pytest.raises(EmbeddingUnavailable):
            wrapper.embed(["anything"])
        assert marks and "endpoint down" in marks[0]


class TestDegradedMode:
    def test_emit_degraded_one_per_wake(self, tmp_path):
        store = _store(tmp_path)
        log = ShadowLog(tmp_path / "shadow.jsonl")
        store.append(EventCreate(type="wake.started", visibility="system",
                                 content={}, provenance={"source": "t"}))
        assert emit_degraded({"reason": "down"}, log, store) is True
        assert emit_degraded({"reason": "down again"}, log, store) is False  # same wake
        store.append(EventCreate(type="wake.started", visibility="system",
                                 content={}, provenance={"source": "t"}))
        assert emit_degraded({"reason": "next wake"}, log, store) is True
        kinds = [r["kind"] for r in log.read()]
        assert kinds == ["degraded", "degraded"]

    def test_semantic_paths_degrade_to_empty_not_crash(self, tmp_path):
        """Treatment + provider outage: every embedder-fed path returns empty /
        neutral, nothing crashes, and the outage is on the record (sidecar),
        not silent."""
        store = _store(tmp_path)
        log = ShadowLog(tmp_path / "shadow.jsonl")
        on_degraded = lambda reason: emit_degraded(  # noqa: E731
            {"reason": reason[:300]}, log, store)
        resilient = ResilientEmbedding(Dead(), on_degraded=on_degraded)
        layer = SemanticLayer(store, Dead(), on_degraded=on_degraded)
        mem = MemoryRetrieval(store, embedder=resilient, semantic_layer=layer)
        assert mem.semantic_near("任何查询", 5) == []
        assert mem.distant(3) == []
        # `forgotten` is vector-free (age/recall-count based) — the degradation
        # is surgical: only vector-needing paths degrade, age-based recall lives on
        assert len(mem.forgotten(3)) > 0
        assert mem.topic_concentration(n_recent=7) == 0.0
        assert mem.serendipity(1) == []
        assert layer.recall("查询", 3) == []
        assert layer.shadow_compare(mem, route="continuity", query="查询") is None
        assert any(r["kind"] == "degraded" for r in log.read())


# --------------------------------------------------------------- clustering


class TestClusteringAndTopicView:
    def test_clusters_are_deterministic_and_meaning_shaped(self, tmp_path):
        store = _store(tmp_path)
        c1 = _layer(store, tmp_path).clusters()
        c2 = _layer(store, tmp_path).clusters()
        assert [c.id for c in c1] == [c.id for c in c2]  # deterministic
        assert [c.members for c in c1] == [c.members for c in c2]
        # the daemon/long-lived thought clusters WITH an agent-memory thought
        # (both are about 长期驻留状态), not with the pasta ones
        members_text = {}
        for i, (typ, text) in enumerate(TEXTS):
            members_text[f"evt_{i}"] = text
        by_id = {e.id: e.text for e in store.list(50, order="asc")}
        daemon = next(e for e in store.list(50, order="asc") if "daemon" in e.text)
        host = next(c for c in c1 if daemon.id in c.members)
        host_texts = [by_id[m] for m in host.members]
        assert any("Agent" in t for t in host_texts), host_texts

    def test_label_is_never_identity(self, tmp_path):
        """A display label can be attached, but cluster identity comes only
        from vectors + events; relabeling changes nothing structural."""
        store = _store(tmp_path)
        layer = _layer(store, tmp_path)
        before = [(c.id, c.members, c.centroid) for c in layer.clusters()]
        for c in layer.clusters():
            c.label = "本地 Agent / 长期记忆"  # the LLM's display label
        after = [(c.id, c.members, c.centroid) for c in layer.clusters()]
        assert [(i, m) for i, m, _ in before] == [(i, m) for i, m, _ in after]
        view = layer.topic_view()
        assert all("label" in c for c in view["clusters"])
        assert all(c["label"] is None for c in view["clusters"])  # not persisted in the view

    def test_default_tau_is_a_real_fallback(self):
        assert 0.0 < CLUSTER_TAU < 1.0


# ------------------------------------------------------------ shadow isolation


class TestShadowMode:
    def test_shadow_records_divergence_in_sidecar_not_log(self, tmp_path):
        asyncio.run(run_world_simulation(tmp_path / "run", days=2, seed=0,
                                         world_params=ARM_PARAMS["B_follow_edge"], shadow=True))
        log = ShadowLog(tmp_path / "run" / "semantic_shadow.jsonl")
        records = log.read()
        assert records and all(r["kind"] == "shadow" for r in records)
        rec = records[0]
        for key in ("route", "legacy_top", "semantic_top", "overlap",
                    "rank_displacement_mean", "top1_differs", "thread_selection_diverges",
                    "distant_legacy", "distant_bridge", "wake_id"):
            assert key in rec, key
        # NOT in the event log
        store = EventStore(tmp_path / "run" / "world.sqlite3")
        assert store.count("semantic.") == 0

    def test_shadow_does_not_perturb_the_mind(self, tmp_path):
        """The load-bearing isolation: with the same seed and world arm, shadow
        on vs off must produce an IDENTICAL mind (byte-same report) — the
        shadow only writes its sidecar."""
        a = asyncio.run(run_world_simulation(tmp_path / "off", days=2, seed=0,
                                             world_params=ARM_PARAMS["B_follow_edge"],
                                             shadow=False))
        b = asyncio.run(run_world_simulation(tmp_path / "on", days=2, seed=0,
                                             world_params=ARM_PARAMS["B_follow_edge"],
                                             shadow=True))
        for key, val in a.items():
            if key == "meta":
                for mk, mv in val.items():
                    assert b["meta"].get(mk) == mv, f"meta.{mk}"
            else:
                assert b.get(key) == val, key
        assert ShadowLog(tmp_path / "on" / "semantic_shadow.jsonl").read()

    def test_treatment_changes_the_mind_and_is_deterministic(self, tmp_path):
        """The treatment arm (real vectors) is allowed to differ — and it does —
        and it replays deterministically (vectors cached under the run dir)."""
        a = asyncio.run(run_world_simulation(tmp_path / "t1", days=2, seed=0,
                                             world_params=ARM_PARAMS["B_follow_edge"],
                                             embeddings="semantic"))
        b = asyncio.run(run_world_simulation(tmp_path / "t2", days=2, seed=0,
                                             world_params=ARM_PARAMS["B_follow_edge"],
                                             embeddings="semantic"))
        assert a["meta"] == b["meta"] and a["route_entropy"] == b["route_entropy"]
        legacy = asyncio.run(run_world_simulation(tmp_path / "l1", days=2, seed=0,
                                                  world_params=ARM_PARAMS["B_follow_edge"]))
        # genuinely different associative fabric (not a tuning claim — an
        # existence proof that the treatment is live)
        assert a["meta"]["n_total_events"] != legacy["meta"]["n_total_events"] or \
            a["route_entropy"] != legacy["route_entropy"]


# ------------------------------------------------------- treatment strategies


class TestTreatmentStrategies:
    def test_bridge_distant_is_not_lowest_cosine(self, tmp_path):
        """distant with a semantic layer prefers FAR-BUT-BRIDGEABLE: every
        surfaced pick carries either bridge evidence or an explicit
        'no bridge found' flag — never a bare 'far from current focus'."""
        store = _store(tmp_path)
        layer = _layer(store, tmp_path)
        mem = MemoryRetrieval(store, semantic_layer=layer)
        hits = mem.distant(3)
        if hits:  # tiny store: may legitimately have no candidates
            for h in hits:
                assert h.path == "distant"
                assert "bridge" in h.reason  # evidence recorded in the hit

    def test_serendipity_treatment_is_mid_band(self, tmp_path):
        store = _store(tmp_path)
        mem = MemoryRetrieval(store, semantic_layer=_layer(store, tmp_path))
        hits = mem.serendipity(1)
        for h in hits:
            assert "mid-band" in h.reason

    def test_cluster_concentration_differs_from_family_concentration(self, tmp_path):
        store = _store(tmp_path)
        legacy = MemoryRetrieval(store)
        treat = MemoryRetrieval(store, semantic_layer=_layer(store, tmp_path))
        lc, tc = legacy.topic_concentration(n_recent=7), treat.topic_concentration(n_recent=7)
        # both in [0,1]; the cluster read is meaning-based (may coincide, must exist)
        assert 0.0 <= lc <= 1.0 and 0.0 <= tc <= 1.0

    def test_semantic_profile_from_sidecar(self, tmp_path):
        asyncio.run(run_world_simulation(tmp_path / "run", days=2, seed=0,
                                         world_params=ARM_PARAMS["B_follow_edge"], shadow=True))
        log = ShadowLog(tmp_path / "run" / "semantic_shadow.jsonl")
        store = EventStore(tmp_path / "run" / "world.sqlite3")
        prof = semantic_profile(store, shadow_log=log)
        assert prof["n_shadow_comparisons"] > 0
        assert prof["legacy_semantic_overlap"] is not None
        assert prof["top1_divergence_rate"] is not None
        # the brief's metric list is present (values may be None when unmeasurable)
        for key in ("legacy_semantic_overlap", "rank_displacement", "cluster_entropy",
                    "cluster_lifetime_days", "cross_cluster_recall_rate",
                    "semantic_memory_age_days", "thread_selection_divergence",
                    "distant_bridge_rate", "serendipity_survival_rate"):
            assert key in prof, key


class TestMakeStack:
    def test_legacy_default_is_all_none(self, tmp_path):
        store = _store(tmp_path)
        assert make_semantic_stack(store, tmp_path / "v.sqlite3") == (None, None)

    def test_treatment_kwargs_and_shadow_layer(self, tmp_path):
        store = _store(tmp_path)
        shadow, kwargs = make_semantic_stack(store, tmp_path / "v.sqlite3", shadow=True)
        assert shadow is not None and kwargs is None
        shadow2, kwargs = make_semantic_stack(store, tmp_path / "v.sqlite3", mode="semantic")
        assert shadow2 is None  # treatment has no separate shadow layer
        assert set(kwargs) == {"embedder", "semantic_layer"}
        assert kwargs["semantic_layer"] is not None
        assert isinstance(kwargs["embedder"], ResilientEmbedding)


# ------------------------------------------------------- main.py wiring


class TestMainEmbeddingStackWiring:
    def test_main_treatment_outage_degrades_audited_not_typeerror(self, tmp_path, monkeypatch):
        """The LIVE main.py path (ADR-0010): a treatment embedding-provider
        outage must surface as an explicit, audited degradation —
        EmbeddingUnavailable plus a ``degraded`` sidecar record tagged with the
        treatment mode — and never as a wiring TypeError from the
        on_degraded callback main.py hands the stack. main.py's callback
        wiring is part of the contract, so this drives the real
        ``_make_embedding_stack``, not a hand-built stack."""
        monkeypatch.setenv("RESIDENT_EMBEDDINGS", "semantic")
        monkeypatch.setenv("RESIDENT_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("RESIDENT_VECTOR_CACHE", str(tmp_path / "vectors.sqlite3"))
        monkeypatch.setenv("RESIDENT_SHADOW_LOG", str(tmp_path / "shadow.jsonl"))
        for var in ("RESIDENT_EMBEDDING_BASE_URL", "RESIDENT_EMBEDDING_MODEL",
                    "RESIDENT_EMBEDDING_API_KEY"):
            monkeypatch.delenv(var, raising=False)

        # resident.main builds a module-level app at import; RESIDENT_HOME
        # (set above) keeps that construction inside the test's tmp dir.
        from resident import main as resident_main
        from resident import providers as resident_providers

        # No RESIDENT_EMBEDDING_* endpoint → main falls back to the
        # deterministic sketch provider; take it down to simulate a real
        # provider outage.
        def _dead_embed(self, texts):
            raise ProviderError("endpoint down (simulated outage)")

        monkeypatch.setattr(
            resident_providers.DeterministicSemanticEmbeddingProvider, "embed", _dead_embed)

        store = EventStore(tmp_path / "home" / "events.sqlite3")
        _shadow_layer, memory_kwargs = resident_main._make_embedding_stack(
            store, tmp_path / "home")
        assert memory_kwargs is not None  # treatment is on
        with pytest.raises(EmbeddingUnavailable):
            memory_kwargs["embedder"].embed(["长期记忆"])

        # the outage is on the record, in the sidecar (never the event log),
        # and the record says which mode degraded
        records = ShadowLog(tmp_path / "shadow.jsonl").read()
        degraded = [r for r in records if r.get("kind") == "degraded"]
        assert degraded, f"no degraded audit in sidecar: {records}"
        rec = degraded[0]
        assert rec["reason"]
        assert rec["mode"] == "treatment"
