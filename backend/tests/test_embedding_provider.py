"""Hermetic tests for the pluggable OpenAI-compatible embedding provider.

A stub HTTP server on 127.0.0.1:0 stands in for a real /v1/embeddings
endpoint — no network is touched. This is the opt-in real embedding hook;
the hermetic default (HashingEmbeddingProvider) is what tests + the
accelerated simulation actually run on.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from resident.providers import OpenAICompatibleEmbeddingProvider, ProviderError


class _StubHandler(BaseHTTPRequestHandler):
    behavior: str = "ok"
    vectors: list[list[float]] = [[0.1, 0.2], [0.3, 0.4]]
    with_index: bool = False
    requests: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        _StubHandler.requests.append(
            {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
        )
        b = self.behavior
        if b == "always401":
            self._send(401, b"unauthorized")
        elif b == "bearer_only_401" and "authorization" in self.headers:
            self._send(401, b"unauthorized")
        elif b == "malformed":
            self._send(200, b"not json")
        elif b == "no_data":
            self._send(200, json.dumps({"data": []}).encode())
        elif b == "mismatch":
            self._send(200, json.dumps({"data": [{"embedding": [0.1, 0.2]}]}).encode())  # 1 for 2
        else:
            data = [{"embedding": v} for v in self.vectors]
            if self.with_index:
                data = [{"index": i, "embedding": v} for i, v in enumerate(self.vectors)]
            self._send(200, json.dumps({"data": data, "model": "stub"}).encode())

    def _send(self, code: int, payload: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


@pytest.fixture()
def stub():
    _StubHandler.behavior = "ok"
    _StubHandler.vectors = [[0.1, 0.2], [0.3, 0.4]]
    _StubHandler.with_index = False
    _StubHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()
    server.server_close()


def _make(stub, **kw):
    port = stub.server_address[1]
    return OpenAICompatibleEmbeddingProvider(
        base_url=f"http://127.0.0.1:{port}", model="stub-embed", api_key="k", **kw
    )


def test_embed_happy_path_batch_and_order(stub):
    p = _make(stub)
    out = p.embed(["a", "b"])
    assert out == [[0.1, 0.2], [0.3, 0.4]]
    req = _StubHandler.requests[0]
    assert req["path"] == "/v1/embeddings"
    assert req["headers"]["authorization"] == "Bearer k"
    assert req["body"]["model"] == "stub-embed"
    assert req["body"]["input"] == ["a", "b"]


def test_embed_honours_index_field(stub):
    _StubHandler.with_index = True
    _StubHandler.vectors = [[1.0], [2.0]]
    p = _make(stub)
    out = p.embed(["a", "b"])
    assert out == [[1.0], [2.0]]


def test_embed_empty_input_returns_empty(stub):
    p = _make(stub)
    assert p.embed([]) == []
    assert _StubHandler.requests == []  # no call made


def test_bearer_401_falls_back_to_x_api_key_once(stub):
    _StubHandler.behavior = "bearer_only_401"
    p = _make(stub)
    out = p.embed(["a", "b"])
    assert out[0] == [0.1, 0.2]
    assert len(_StubHandler.requests) == 2
    assert _StubHandler.requests[1]["headers"].get("x-api-key") == "k"


def test_both_auth_rejected_raises(stub):
    _StubHandler.behavior = "always401"
    p = _make(stub)
    with pytest.raises(ProviderError, match="HTTP 401"):
        p.embed(["a"])


def test_malformed_json_raises(stub):
    _StubHandler.behavior = "malformed"
    p = _make(stub)
    with pytest.raises(ProviderError, match="malformed JSON"):
        p.embed(["a", "b"])


def test_empty_data_raises(stub):
    _StubHandler.behavior = "no_data"
    p = _make(stub)
    with pytest.raises(ProviderError, match="shape"):
        p.embed(["a", "b"])


def test_count_mismatch_raises(stub):
    _StubHandler.behavior = "mismatch"
    p = _make(stub)
    with pytest.raises(ProviderError, match="count mismatch"):
        p.embed(["a", "b"])
