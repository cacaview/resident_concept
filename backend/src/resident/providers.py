"""Model provider interfaces.

The architecture must not depend on one model family (ARCHITECTURE.md,
"Models"). All model access goes through these provider interfaces:

- ``ModelProvider`` — text completion. The default implementation,
  ``DeterministicFakeProvider``, is a rule-based stand-in used in tests
  and local runs: same store state in, same output out. No network,
  no randomness.
- ``EmbeddingProvider`` — semantic vector hook. The default,
  ``HashingEmbeddingProvider``, is a deterministic lexical fallback
  (NOT true semantics); a real OpenAI-compatible embedding endpoint
  can be swapped in without touching callers.

MindLoop prompts and provider outputs are JSON strings so the contract
stays model-agnostic (any chat-completions API can satisfy it).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from typing import Any, Protocol


class ModelProvider(Protocol):
    model_id: str

    def complete(self, *, system: str, prompt: str) -> str:
        """Complete a JSON-in / JSON-out request. See _ROUTER_SYSTEM."""


class EmbeddingProvider(Protocol):
    model_id: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class ProviderError(RuntimeError):
    """A model provider call failed (network, HTTP status, or response parse).

    Providers never fabricate output: on any failure the error propagates so
    the caller (e.g. MindLoop) can decide how to degrade — for v0.1 a failed
    model call is simply "no model output this cycle".
    """


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


class HashingEmbeddingProvider:
    """Deterministic lexical embedding (token-hash buckets, L2 norm).

    This is a real, swappable embedding *interface* with a naive default:
    cosine similarity here reflects lexical overlap, not semantics.
    """

    model_id = "local/hash-lexical"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _tokens(self, text: str) -> list[str]:
        toks = re.findall(r"[a-z0-9_]+", text.lower())
        for ch in text:
            if "一" <= ch <= "鿿":
                toks.append(ch)
        return toks

    def _embed_one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in self._tokens(text):
            h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest(), 16)
            v[h % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v))
        if norm == 0.0:
            return v
        return [x / norm for x in v]

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))


class DeterministicSemanticEmbeddingProvider:
    """Deterministic *semantic-shaped* embedding: seeded signed random
    projections of character n-grams + tokens (a CountSketch family).

    v0.2 Step 2 (ADR-0010): this is deliberately a **different algorithm
    family** from :class:`HashingEmbeddingProvider` (which counts unigram
    tokens into buckets). Bigram/word sketches with random ±1 signs give
    similarity structure closer to a real multilingual embedder — adjacent
    characters and shared subwords matter, exact token identity matters less —
    while staying hermetic and byte-deterministic (sha256-derived, never
    Python's ``hash()``). Roles:

    - tests for the shadow/semantic machinery (no network, reproducible);
    - the shadow-mode experiment vector source when no real embedding
      endpoint is configured — honestly labelled ``local/sketch-semantic-v1``
      in every provenance record, never presented as a real model.
    """

    model_id = "local/sketch-semantic-v1"
    version = "v1"
    normalization = "l2"
    #: geometry-calibrated clustering threshold for THIS vector family (signed
    #: sketches are near-orthogonal: pairwise cosine p50 ≈ 0, max ≈ 0.41 on
    #: short texts). Calibrated to the geometry, never to a behavioral metric —
    #: a real embedder declares its own hint (or falls back to CLUSTER_TAU).
    cluster_tau_hint = 0.25

    def __init__(self, dim: int = 256, seed: int = 20260911):
        self.dim = dim
        self.seed = seed

    @property
    def identity(self) -> dict:
        return {"provider": "sketch", "model": "sketch-semantic", "version": self.version,
                "dim": self.dim, "normalization": self.normalization}

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _grams(self, text: str) -> list[str]:
        text = text.strip()
        toks = re.findall(r"[a-z0-9_]+", text.lower())
        cjk = "".join(ch for ch in text if "一" <= ch <= "鿿")
        grams = list(toks)
        grams += [cjk[i:i + 2] for i in range(len(cjk) - 1)]  # CJK bigrams
        grams += [f"^{t}" for t in toks[:8]]  # word-boundary markers (light)
        return grams

    def _embed_one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for gram in self._grams(text):
            digest = hashlib.sha256(f"{self.seed}:{gram}".encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            # weight: rarer shorter grams count a bit less than words
            w = 1.0 if len(gram) >= 2 else 0.5
            v[idx] += sign * w
        norm = math.sqrt(sum(x * x for x in v))
        if norm == 0.0:
            return v
        return [x / norm for x in v]

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))


class OpenAICompatibleEmbeddingProvider:
    """EmbeddingProvider backed by an OpenAI-compatible ``/v1/embeddings``
    endpoint.

    This is the *pluggable real* embedding hook the architecture asks for
    (ARCHITECTURE.md, "Memory"): a real semantic vector source that can be
    swapped in without touching callers. It is strictly **opt-in** —
    constructed only when explicitly configured (see ``main.py``) — so the
    hermetic default remains :class:`HashingEmbeddingProvider` and the test
    suite + the accelerated simulation never touch the network.

    Batch contract: :meth:`embed` sends all texts in one ``input`` array and
    returns vectors in input order (honouring an ``index`` field when the
    server provides one). Auth mirrors the Messages provider: Bearer first,
    one ``x-api-key`` fallback on 401/403, no retry storms.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout: float = 30.0,
    ):
        if not base_url:
            raise ValueError("OpenAICompatibleEmbeddingProvider requires a non-empty base_url")
        if not api_key:
            raise ValueError("OpenAICompatibleEmbeddingProvider requires a non-empty api_key")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = float(timeout)
        self.model_id = f"embed/{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        url = f"{self.base_url}/v1/embeddings"
        data = json.dumps({"model": self.model, "input": texts}, ensure_ascii=False).encode("utf-8")
        auth_attempts = (
            {"Authorization": f"Bearer {self.api_key}"},
            {"x-api-key": self.api_key},
        )
        for i, auth in enumerate(auth_attempts):
            headers = {"content-type": "application/json"}
            headers.update(auth)
            status, raw = self._post(url, data, headers)
            if status in (401, 403) and i == 0:
                continue
            if status != 200:
                raise ProviderError(f"HTTP {status} from {url}: {raw[:200]!r}")
            return self._parse(raw, len(texts))
        raise ProviderError(f"authentication rejected (Bearer, then x-api-key) by {url}")

    def _post(self, url: str, data: bytes, headers: dict[str, str]) -> tuple[int, str]:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return int(resp.status), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return int(e.code), e.read().decode("utf-8", "replace")
        except Exception as e:  # URLError, socket timeout, connection refused, ...
            raise ProviderError(f"network error calling {url}: {e}") from e

    @staticmethod
    def _parse(raw: str, expected: int) -> list[list[float]]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProviderError(f"malformed JSON in embedding response: {raw[:200]!r}") from e
        items = data.get("data")
        if not isinstance(items, list) or not items:
            raise ProviderError(f"unexpected embedding response shape: {raw[:200]!r}")
        # Honour an explicit index when present; otherwise assume input order.
        if all(isinstance(it, dict) and "index" in it for it in items):
            ordered = sorted(items, key=lambda it: it["index"])
        else:
            ordered = items
        vectors = []
        for it in ordered:
            v = it.get("embedding")
            if not isinstance(v, list) or not all(isinstance(x, (int, float)) for x in v):
                raise ProviderError(f"unexpected embedding response shape: {raw[:200]!r}")
            vectors.append([float(x) for x in v])
        if len(vectors) != expected:
            raise ProviderError(
                f"embedding count mismatch: asked {expected}, got {len(vectors)}"
            )
        return vectors


# ---------------------------------------------------------------------------
# Anthropic Messages provider (opt-in real model)
# ---------------------------------------------------------------------------


class AnthropicMessagesProvider:
    """ModelProvider backed by an Anthropic Messages endpoint (POST /v1/messages).

    ARCHITECTURE.md says "preferably OpenAI-compatible HTTP"; the v0.1 session
    endpoint speaks the Anthropic Messages format instead. The provider
    *interface* stays model-agnostic, so this is an acceptable v0.1 deviation
    (recorded in docs/TODO.md).

    Auth: primary ``Authorization: Bearer <key>`` (session endpoints are
    configured with an ANTHROPIC_AUTH_TOKEN-style token); if the server
    answers 401/403 we retry exactly once with the classic ``x-api-key``
    header. No other retries — no retry storms.

    Selection is explicit and env-driven (see main.py): it is never the
    default, so the test suite stays hermetic.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        max_tokens: int = 512,
        timeout: float = 30.0,
    ):
        if not base_url:
            raise ValueError("AnthropicMessagesProvider requires a non-empty base_url")
        if not api_key:
            raise ValueError("AnthropicMessagesProvider requires a non-empty api_key")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout)
        self.model_id = f"anthropic/{model}"

    def complete(self, *, system: str, prompt: str) -> str:
        """One completion. Returns the model's text (code fences stripped)."""
        url = f"{self.base_url}/v1/messages"
        data = json.dumps(
            {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        auth_attempts = (
            {"Authorization": f"Bearer {self.api_key}"},
            {"x-api-key": self.api_key},
        )
        for i, auth in enumerate(auth_attempts):
            headers = {
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
            }
            headers.update(auth)
            status, raw = self._post(url, data, headers)
            if status in (401, 403) and i == 0:
                continue  # exactly one fallback: Bearer -> x-api-key
            if status != 200:
                raise ProviderError(f"HTTP {status} from {url}: {raw[:200]!r}")
            return self._parse_text(raw)
        raise ProviderError(f"authentication rejected (Bearer, then x-api-key) by {url}")

    def _post(self, url: str, data: bytes, headers: dict[str, str]) -> tuple[int, str]:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return int(resp.status), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return int(e.code), e.read().decode("utf-8", "replace")
        except Exception as e:  # URLError, socket timeout, connection refused, ...
            raise ProviderError(f"network error calling {url}: {e}") from e

    @staticmethod
    def _parse_text(raw: str) -> str:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProviderError(f"malformed JSON in provider response: {raw[:200]!r}") from e
        content = data.get("content")
        if not isinstance(content, list) or not content:
            raise ProviderError(f"unexpected provider response shape: {raw[:200]!r}")
        # Responses may carry several blocks (e.g. a "thinking" block before
        # the final "text" block); collect the text blocks.
        texts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        if not texts:
            raise ProviderError(f"unexpected provider response shape (no text block): {raw[:200]!r}")
        non_empty = [t for t in texts if t.strip()]
        if not non_empty:
            raise ProviderError(f"empty text block in provider response: {raw[:200]!r}")
        # The last non-empty text block is the final answer.
        return _strip_code_fences(non_empty[-1])


_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+-]*[ \t]*\n?(.*?)\n?[ \t]*```[ \t]*$", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """Models sometimes wrap JSON output in markdown fences; the contract
    is raw JSON-in / JSON-out, so strip a single surrounding fence pair."""
    m = _CODE_FENCE_RE.match(text.strip())
    if m:
        return m.group(1).strip()
    return text


# ---------------------------------------------------------------------------
# Deterministic fake model
# ---------------------------------------------------------------------------

_ROUTER_SYSTEM = (
    "You are the mind of a persistent digital resident. "
    "Input and output are strict JSON. Never invent experiences that "
    "have no supporting event. A wake may legitimately do nothing."
)


def _slug(text: str, n: int = 12) -> str:
    t = re.sub(r"\s+", "", text)[:n]
    return t or "…"


class DeterministicFakeProvider:
    """Rule-based, deterministic stand-in for a small/deep model.

    Route selection policy (first match wins):
      0. scripted routes (test override, consumed FIFO)
      1. nothing new since the last wake          -> rest
      2. an active thread exists                  -> continuity
      3. a dormant thread exists                  -> revisit
      4. a user message is among recent events    -> personal
      5. one topic dominates recent life          -> distant
      6. recent exploration events exist          -> serendipity
      7. history is still very short              -> self
      8. otherwise                                -> rest

    The `world` route is intentionally never auto-selected in v0.1:
    external exploration is unbound, so a world wake is a legitimate
    no-op (see docs/ADR-0005).
    """

    model_id = "fake/deterministic-v0"

    def __init__(self, scripted_routes: list[str] | None = None):
        self._scripted = list(scripted_routes or [])

    # -- public interface ---------------------------------------------------

    def complete(self, *, system: str, prompt: str) -> str:
        req = json.loads(prompt)
        task = req.get("task")
        if task == "route":
            return json.dumps(self._route(req), ensure_ascii=False)
        if task == "thought":
            return json.dumps(self._thought(req), ensure_ascii=False)
        if task == "chat":
            return json.dumps(self._chat(req), ensure_ascii=False)
        raise ValueError(f"unknown task: {task!r}")

    # -- route ----------------------------------------------------------------

    def _route(self, req: dict[str, Any]) -> dict[str, Any]:
        if self._scripted:
            route = self._scripted.pop(0)
            return {"route": route, "decision": self._decision_for(route, req), "reason": "scripted route (test)"}
        new_since = int(req.get("new_since_last_wake", 0))
        if new_since == 0:
            return {"route": "rest", "decision": "noop", "reason": "nothing new since last wake; resting is legitimate"}
        threads = req.get("active_threads") or []
        if threads:
            return {"route": "continuity", "decision": "persist", "reason": "an active thread pulls"}
        dormant = req.get("dormant_threads") or []
        if dormant:
            return {"route": "revisit", "decision": "persist", "reason": "old unfinished material resurfaced"}
        if req.get("has_recent_user_message"):
            return {"route": "personal", "decision": "persist", "reason": "the user's timeline just moved"}
        if float(req.get("topic_concentration", 0.0)) >= 0.7:
            return {"route": "distant", "decision": "persist", "reason": "one topic dominates; sampling away from it"}
        if req.get("has_exploration_recently"):
            return {"route": "serendipity", "decision": "persist", "reason": "an exploration boundary to examine"}
        if int(req.get("total_events", 0)) < 8:
            return {"route": "self", "decision": "persist", "reason": "history is short; observing my own runtime"}
        return {"route": "rest", "decision": "noop", "reason": "nothing worth persisting this cycle"}

    @staticmethod
    def _decision_for(route: str, req: dict[str, Any]) -> str:
        return "noop" if route in ("rest", "world") else "persist"

    # -- thought ---------------------------------------------------------------

    def _thought(self, req: dict[str, Any]) -> dict[str, Any]:
        route = req.get("route")
        recent: list[dict] = req.get("recent") or []
        if route == "rest" or req.get("decision") == "noop":
            return {"decision": "noop", "text": "", "links": {}}

        thread = req.get("thread") or {}
        dormant = req.get("dormant_thread") or {}
        user_msg = req.get("user_message") or {}
        stats = req.get("stats") or {}

        if route == "continuity":
            last = (thread.get("last_events") or [{}])[-1]
            text = (
                f"[continuity] 继续线程「{thread.get('title', '…')}」：上次停在这里——"
                f"「{_slug(last.get('text', ''), 40)}」。我仍想弄清它和最近经历（"
                f"{_slug(self._pick_text(recent, exclude=thread.get('thread_id')))}）之间的差别。"
            )
            links = {
                "related_to": self._thread_link_ids(thread),
                "thread_id": thread.get("thread_id"),
            }
        elif route == "revisit":
            last = (dormant.get("last_events") or [{}])[-1]
            text = (
                f"[revisit] 重新翻出旧线程「{dormant.get('title', '…')}」："
                f"「{_slug(last.get('text', ''), 40)}」。现在回头看，它和最近的"
                f"{_slug(self._pick_text(recent, exclude=dormant.get('thread_id')))}有了联系。"
            )
            links = {
                "related_to": self._thread_link_ids(dormant),
                "thread_id": dormant.get("thread_id"),
            }
        elif route == "personal":
            text = (
                f"[personal] 你刚说：「{_slug(user_msg.get('text', ''), 40)}」。"
                f"在我听到这句话之前，我的状态是「{req.get('mind_brief', '…')}」——"
                "这次对话没有抹掉它。"
            )
            links = {"related_to": [user_msg["id"]] if user_msg.get("id") else []}
        elif route == "distant":
            a, b = self._two_divergent(recent)
            text = (
                f"[distant] 把两件离得较远的事放在一起：「{_slug(a.get('text', ''))}」"
                f"与「{_slug(b.get('text', ''))}」。如果它们同构，同构点在哪里？"
            )
            links = {"related_to": [x["id"] for x in (a, b) if x.get("id")]}
        elif route == "serendipity":
            x = next((r for r in reversed(recent) if r.get("type", "").startswith("exploration.")), recent[-1] if recent else {})
            text = (
                f"[serendipity] 边界采样命中：「{_slug(x.get('text', ''), 40)}」。"
                "它不属于当前任何线程，但值得记下来。"
            )
            links = {"related_to": [x["id"]] if x.get("id") else []}
        elif route == "self":
            dist = ", ".join(f"{k}×{v}" for k, v in sorted((stats.get("route_dist") or {}).items())) or "（还没有醒来过）"
            text = (
                f"[self] 观察自己的运行：共醒来 {stats.get('wakes', 0)} 次，"
                f"其中 {stats.get('noops', 0)} 次是 no-op。路线分布：{dist}。"
                "no-op 是合法的一整天，不是故障。"
            )
            links = {"related_to": [r["id"] for r in recent if r.get("type", "").startswith("wake.")][:2]}
        elif route == "world":
            return {"decision": "noop", "text": "", "links": {}, "reason": "world route unbound in v0.1: no external access"}
        else:
            return {"decision": "noop", "text": "", "links": {}, "reason": f"no handler for route {route!r}"}

        visibility = "shareable" if route in ("personal", "continuity") else "private"
        return {"decision": "persist", "text": text, "links": links, "visibility": visibility}

    @staticmethod
    def _thread_link_ids(thread: dict) -> list[str]:
        return [e["id"] for e in (thread.get("last_events") or []) if e.get("id")][-3:]

    @staticmethod
    def _pick_text(recent: list[dict], exclude: str | None = None) -> str:
        for r in reversed(recent):
            if r.get("type", "").startswith(("conversation.", "wake.", "system")):
                continue
            if exclude and r.get("thread_id") == exclude:
                continue
            if r.get("text"):
                return r["text"]
        return "近期经历"

    @staticmethod
    def _two_divergent(recent: list[dict]) -> tuple[dict, dict]:
        """Two most-recent events from different families; best effort."""
        by_family: dict[str, dict] = {}
        for r in reversed(recent):
            fam = r.get("type", "x").split(".")[0]
            if fam not in by_family and r.get("text"):
                by_family[fam] = r
            if len(by_family) >= 2:
                break
        vals = list(by_family.values())
        if len(vals) < 2 and recent:
            vals = [recent[-1]] + ([recent[0]] if len(recent) > 1 else [])
        return (vals[0], vals[1]) if len(vals) >= 2 else (vals[0], {}) if vals else ({}, {})

    # -- chat ------------------------------------------------------------------

    def _chat(self, req: dict[str, Any]) -> dict[str, Any]:
        user = (req.get("user_message") or "")[:200]
        brief = req.get("mind_brief") or "（还没有什么值得说的）"
        life = req.get("recent_life") or []
        last = next((e for e in reversed(life) if e.get("text")), None)
        tail = (
            f" 在你说这句话之前，我最近在想：「{last['text'][:80]}」（{last['id']}）。"
            if last
            else ""
        )
        reply = (
            f"我听到了：{user}。"
            f"此刻我脑子里已有的状态是：{brief}。这次对话没有抹掉它。{tail}"
        )
        return {"reply": reply}
