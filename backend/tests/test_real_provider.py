"""AnthropicMessagesProvider + explicit env-based provider selection.

All model traffic goes to an in-process stub HTTP server on 127.0.0.1 with an
ephemeral port — no external network, deterministic, no retries beyond the
single documented auth fallback.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from resident.event_store import EventStore
from resident.main import build_app
from resident.mind_loop import MindLoop
from resident.models import EventCreate
from resident.providers import AnthropicMessagesProvider, ProviderError

CANNED_TEXT = '{"route": "rest", "decision": "noop", "reason": "stub"}'


def _make_handler(state: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            body = self.rfile.read(n)
            state["requests"].append(
                {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(body),
                }
            )
            count = len(state["requests"])
            mode = state["mode"]
            if mode == "ok":
                self._reply(200, _envelope(state["text"]))
            elif mode == "fenced":
                self._reply(200, _envelope("```json\n" + state["text"] + "\n```"))
            elif mode == "bad_json":
                self._reply(200, "this is not json{")
            elif mode == "bad_shape":
                self._reply(200, '{"content": []}')
            elif mode == "http500":
                self._reply(500, '{"error": "boom"}')
            elif mode == "thinking":
                # realistic multi-block response: thinking block, then text
                body = json.dumps(
                    {
                        "id": "msg_stub",
                        "type": "message",
                        "role": "assistant",
                        "model": "haiku-test",
                        "content": [
                            {"type": "thinking", "thinking": "let me think..."},
                            {"type": "text", "text": state["text"]},
                        ],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    }
                )
                self._reply(200, body)
            elif mode == "auth401":
                # first attempt (Bearer) is rejected, the fallback is served
                if count == 1:
                    self._reply(401, '{"error": {"type": "unauthorized", "message": "unauthorized"}}')
                else:
                    self._reply(200, _envelope(state["text"]))
            else:
                self._reply(500, "unknown stub mode")

        def _reply(self, status: int, text: str):
            data = text.encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def _envelope(model_text: str) -> str:
    """Anthropic Messages-shaped success body around the model's text."""
    return json.dumps(
        {
            "id": "msg_stub",
            "type": "message",
            "role": "assistant",
            "model": "haiku-test",
            "content": [{"type": "text", "text": model_text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )


class StubMessagesServer:
    """Anthropic-Messages-shaped stub on 127.0.0.1 with an ephemeral port."""

    def __init__(self, mode: str = "ok", text: str = CANNED_TEXT):
        self.state = {"mode": mode, "text": text, "requests": []}
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def requests(self):
        return self.state["requests"]

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def make_provider(srv, **kw):
    return AnthropicMessagesProvider(
        base_url=srv.base_url, model="haiku-test", api_key="test-key", **kw
    )


# ---------------------------------------------------------------- provider


def test_request_shape_and_parse():
    srv = StubMessagesServer(mode="ok")
    try:
        p = make_provider(srv, max_tokens=64, timeout=5)
        out = p.complete(system="SYS", prompt='{"task": "route"}')
        assert p.model_id == "anthropic/haiku-test"
        assert json.loads(out) == json.loads(CANNED_TEXT)
        assert len(srv.requests) == 1
        req = srv.requests[0]
        assert req["path"] == "/v1/messages"
        assert req["headers"]["content-type"] == "application/json"
        assert req["headers"]["anthropic-version"] == "2023-06-01"
        assert req["headers"]["authorization"] == "Bearer test-key"
        assert "x-api-key" not in req["headers"]
        assert req["body"] == {
            "model": "haiku-test",
            "max_tokens": 64,
            "system": "SYS",
            "messages": [{"role": "user", "content": '{"task": "route"}'}],
        }
    finally:
        srv.stop()


def test_base_url_trailing_slash_is_normalized():
    srv = StubMessagesServer(mode="ok")
    try:
        p = AnthropicMessagesProvider(
            base_url=srv.base_url + "/", model="m", api_key="k"
        )
        p.complete(system="S", prompt="{}")
        assert srv.requests[0]["path"] == "/v1/messages"
    finally:
        srv.stop()


def test_bearer_falls_back_to_x_api_key_on_401():
    srv = StubMessagesServer(mode="auth401")
    try:
        p = make_provider(srv)
        out = p.complete(system="S", prompt="{}")
        assert json.loads(out) == json.loads(CANNED_TEXT)
        assert len(srv.requests) == 2, "exactly one fallback attempt"
        first, second = srv.requests
        assert first["headers"]["authorization"] == "Bearer test-key"
        assert "x-api-key" not in first["headers"]
        assert second["headers"]["x-api-key"] == "test-key"
        assert "authorization" not in second["headers"]
    finally:
        srv.stop()


@pytest.mark.parametrize("mode", ["bad_json", "bad_shape", "http500"])
def test_provider_error_on_bad_response(mode):
    srv = StubMessagesServer(mode=mode)
    try:
        p = make_provider(srv)
        with pytest.raises(ProviderError):
            p.complete(system="S", prompt="{}")
    finally:
        srv.stop()


def test_code_fences_are_stripped():
    srv = StubMessagesServer(mode="fenced")
    try:
        p = make_provider(srv)
        out = p.complete(system="S", prompt="{}")
        assert out == CANNED_TEXT
        json.loads(out)  # contract: raw JSON out
    finally:
        srv.stop()


def test_thinking_block_before_text_is_handled():
    """Real endpoints may return multi-block content (thinking -> text);
    the provider must surface the text block, not fail on the first one."""
    srv = StubMessagesServer(mode="thinking")
    try:
        p = make_provider(srv)
        out = p.complete(system="S", prompt="{}")
        assert out == CANNED_TEXT
    finally:
        srv.stop()


# ------------------------------------------------------------ mind loop


async def test_mind_loop_degrades_to_recorded_noop_on_invalid_real_output(tmp_path):
    """A real provider whose model output is not a usable thought (no `text`)
    must land on MindLoop's safety net: a recorded, provenance-complete no-op —
    never a fabricated thought. Routing is state-driven, so the provider only
    supplies the thought text; an unparseable/empty thought degrades safely."""
    text = '{"route": "banana", "decision": "persist", "reason": "weird"}'  # no "text" key
    srv = StubMessagesServer(mode="ok", text=text)
    try:
        s = EventStore(tmp_path / "e.sqlite3")
        # a user message so the (state-driven) wake reaches the model call
        s.append(
            EventCreate(
                type="conversation.user_message", actor="user", visibility="user_visible",
                content={"text": "hi"}, provenance={"source": "test"},
            )
        )
        m = MindLoop(s, provider=make_provider(srv))
        res = await m.wake_once("stub")
        assert res["result"] == "noop"
        assert res["route"] not in ("rest", "world"), \
            "a persist route must be chosen so the model is actually reached"
        types = [e.type for e in s.list(50, type_prefix="wake")]
        assert "wake.route_selected" in types
        assert "wake.noop" in types
        assert "wake.completed" in types
        assert s.count("thought.created") == 0  # nothing fabricated
    finally:
        srv.stop()


# ------------------------------------------------------- provider selection


def test_health_reports_fake_model_by_default(tmp_path, monkeypatch):
    for var in ("RESIDENT_MODEL_BASE_URL", "RESIDENT_MODEL_API_KEY", "RESIDENT_MODEL_NAME"):
        monkeypatch.delenv(var, raising=False)
    app = build_app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/api/health").json()
    assert body["model"].startswith("fake/")


def test_health_reports_real_model_when_explicitly_configured(tmp_path, monkeypatch):
    srv = StubMessagesServer(mode="ok")
    try:
        monkeypatch.setenv("RESIDENT_MODEL_BASE_URL", srv.base_url)
        monkeypatch.setenv("RESIDENT_MODEL_API_KEY", "test-key")
        monkeypatch.setenv("RESIDENT_MODEL_NAME", "haiku-test")
        app = build_app(tmp_path)
        with TestClient(app) as c:
            body = c.get("/api/health").json()
        assert body["model"] == "anthropic/haiku-test"
    finally:
        srv.stop()


def test_partial_env_config_keeps_fake(tmp_path, monkeypatch):
    """All three vars are required; a partial config must not enable the real one."""
    monkeypatch.delenv("RESIDENT_MODEL_BASE_URL", raising=False)
    monkeypatch.setenv("RESIDENT_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("RESIDENT_MODEL_NAME", "haiku-test")
    app = build_app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/api/health").json()
    assert body["model"].startswith("fake/")
