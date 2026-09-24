"""Hermetic tests for the opt-in AnthropicMessagesProvider.

A stub HTTP server on 127.0.0.1:0 stands in for a real endpoint — these
tests never touch the network. Real-endpoint verification against the
owner-provided session config is a one-shot manual step, not part of the
automated suite (which must stay hermetic and reproducible).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from resident.event_store import EventStore
from resident.main import build_app
from resident.mind_loop import MindLoop
from resident.models import EventCreate
from resident.providers import AnthropicMessagesProvider, ProviderError

_STUB_TEXT = '{"route": "rest", "decision": "noop", "reason": "stub"}'


class _StubHandler(BaseHTTPRequestHandler):
    """Configurable stub: behavior/stub_text/requests are set per test."""

    behavior: str = "ok"
    stub_text: str = _STUB_TEXT
    requests: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        _StubHandler.requests.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        b = self.behavior
        if b == "always401":
            self._send(401, b"unauthorized")
        elif b == "bearer_only_401" and "authorization" in self.headers:
            self._send(401, b"unauthorized")
        elif b == "http500":
            self._send(500, b"boom")
        elif b == "malformed":
            self._send(200, b"this is not json")
        elif b == "no_text":
            self._send(200, json.dumps({"content": [{"type": "tool_use", "input": {}}]}).encode())
        elif b == "empty_text":
            self._send(200, json.dumps({"content": [{"type": "text", "text": "   "}]}).encode())
        else:
            self._send(200, json.dumps({"content": [{"type": "text", "text": self.stub_text}]}).encode())

    def _send(self, code: int, payload: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass  # keep test output quiet


@pytest.fixture()
def stub():
    _StubHandler.behavior = "ok"
    _StubHandler.stub_text = _STUB_TEXT
    _StubHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _make_provider(stub: HTTPServer, **kw) -> AnthropicMessagesProvider:
    port = stub.server_address[1]
    return AnthropicMessagesProvider(
        base_url=f"http://127.0.0.1:{port}", model="stub-model", api_key="stub-key", **kw
    )


# ------------------------------------------------------------------ complete


def test_complete_happy_path_sends_correct_request_and_parses_text(stub):
    p = _make_provider(stub)
    text = p.complete(system="SYS", prompt='{"task": "route"}')
    assert text == _STUB_TEXT
    assert len(_StubHandler.requests) == 1
    req = _StubHandler.requests[0]
    assert req["path"] == "/v1/messages"
    assert req["headers"]["content-type"] == "application/json"
    assert req["headers"]["anthropic-version"] == "2023-06-01"
    assert req["headers"]["authorization"] == "Bearer stub-key"
    assert "x-api-key" not in req["headers"]
    body = json.loads(req["body"])
    assert body["model"] == "stub-model"
    assert body["max_tokens"] == 512
    assert body["system"] == "SYS"
    assert body["messages"] == [{"role": "user", "content": '{"task": "route"}'}]


def test_bearer_401_falls_back_to_x_api_key_exactly_once(stub):
    _StubHandler.behavior = "bearer_only_401"
    p = _make_provider(stub)
    assert p.complete(system="s", prompt="p") == _STUB_TEXT
    assert len(_StubHandler.requests) == 2
    assert "authorization" in _StubHandler.requests[0]["headers"]
    second = _StubHandler.requests[1]["headers"]
    assert second.get("x-api-key") == "stub-key"
    assert "authorization" not in second


def test_auth_rejected_by_both_headers_raises(stub):
    _StubHandler.behavior = "always401"
    p = _make_provider(stub)
    with pytest.raises(ProviderError, match="HTTP 401"):
        p.complete(system="s", prompt="p")
    assert len(_StubHandler.requests) == 2  # Bearer, then x-api-key, then stop


def test_non_auth_http_error_raises_without_fallback(stub):
    _StubHandler.behavior = "http500"
    p = _make_provider(stub)
    with pytest.raises(ProviderError, match="HTTP 500"):
        p.complete(system="s", prompt="p")
    assert len(_StubHandler.requests) == 1  # no retry for non-auth errors


def test_malformed_json_response_raises(stub):
    _StubHandler.behavior = "malformed"
    p = _make_provider(stub)
    with pytest.raises(ProviderError, match="malformed JSON"):
        p.complete(system="s", prompt="p")


def test_missing_text_block_raises(stub):
    _StubHandler.behavior = "no_text"
    p = _make_provider(stub)
    with pytest.raises(ProviderError, match="unexpected provider response shape"):
        p.complete(system="s", prompt="p")


def test_empty_text_block_raises(stub):
    _StubHandler.behavior = "empty_text"
    p = _make_provider(stub)
    with pytest.raises(ProviderError, match="empty text block"):
        p.complete(system="s", prompt="p")


def test_markdown_code_fences_are_stripped(stub):
    _StubHandler.stub_text = "```json\n" + _STUB_TEXT + "\n```"
    p = _make_provider(stub)
    assert p.complete(system="s", prompt="p") == _STUB_TEXT


def test_network_error_raises_provider_error(monkeypatch):
    # Simulate a refused connection deterministically (the local sandbox may
    # proxy stray ports, so we do not rely on a real closed port).
    import urllib.error
    import urllib.request

    def _fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("resident.providers.urllib.request.urlopen", _fake_urlopen)
    p = AnthropicMessagesProvider(base_url="http://127.0.0.1:12345", model="m", api_key="k")
    with pytest.raises(ProviderError, match="network error"):
        p.complete(system="s", prompt="p")


# ------------------------------------------------- mind-loop degradation


def test_mind_loop_degrades_to_recorded_noop_when_provider_fails(tmp_path):
    """A persist-route wake whose model call raises must land on a recorded,
    provenance-complete no-op — never a fabricated thought."""
    class _FailingProvider:
        model_id = "test/failing"

        def complete(self, *, system: str, prompt: str) -> str:
            raise ProviderError("boom")

    store = EventStore(tmp_path / "events.sqlite3")
    # a user message so the state-driven policy picks a persist route and the
    # wake actually reaches the (failing) model call
    store.append(
        EventCreate(
            type="conversation.user_message", actor="user", visibility="user_visible",
            content={"text": "hi"}, provenance={"source": "test"},
        )
    )
    mind = MindLoop(store, provider=_FailingProvider())
    # wake_once is async; run it to completion
    import asyncio

    result = asyncio.run(mind.wake_once("test"))
    assert result["result"] == "noop"
    assert result["route"] not in ("rest", "world"), \
        "a persist route must be chosen so the failing model is actually reached"
    assert "provider unavailable" in result["reason"]
    noops = [e for e in store.list(20) if e.type == "wake.noop"]
    assert len(noops) == 1
    assert "provider unavailable" in noops[0].content["reason"]


# ------------------------------------------------- provider selection


def test_build_app_defaults_to_deterministic_fake(tmp_path, monkeypatch):
    for var in ("RESIDENT_MODEL_BASE_URL", "RESIDENT_MODEL_API_KEY", "RESIDENT_MODEL_NAME"):
        monkeypatch.delenv(var, raising=False)
    app = build_app(tmp_path)
    with TestClient(app) as client:
        body = client.get("/api/health").json()
    assert body["model"].startswith("fake/")


def test_build_app_selects_real_provider_when_env_set(tmp_path, monkeypatch, stub):
    port = stub.server_address[1]
    monkeypatch.setenv("RESIDENT_MODEL_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("RESIDENT_MODEL_API_KEY", "stub-key")
    monkeypatch.setenv("RESIDENT_MODEL_NAME", "stub-model")
    app = build_app(tmp_path)
    with TestClient(app) as client:
        body = client.get("/api/health").json()
    assert body["model"] == "anthropic/stub-model"
