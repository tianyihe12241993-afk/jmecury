import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jmercury import llm
from jmercury.llm import (
    APP_REFERER,
    APP_TITLE,
    FakeTransport,
    LLMClient,
    LLMConfigError,
    LLMError,
    completion,
    image_part,
    text_part,
)

KEY = "sk-or-test-not-a-real-key-0123456789"


def client(responses, **kw):
    t = FakeTransport(list(responses))
    sleeps = []
    c = LLMClient(api_key=KEY, transport=t, sleep=sleeps.append, **kw)
    return c, t, sleeps


def test_missing_key_is_a_config_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(LLMConfigError, match="OPENROUTER_API_KEY is not set"):
        LLMClient(transport=FakeTransport())


def test_key_from_environment_and_base_url_override(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("JM_LLM_BASE_URL", "http://127.0.0.1:9/v1/")
    t = FakeTransport([(200, completion("hi"))])
    c = LLMClient(transport=t)
    assert c.chat("m/x", [{"role": "user", "content": "q"}]).text == "hi"
    assert t.requests[0]["url"] == "http://127.0.0.1:9/v1/chat/completions"
    assert t.requests[0]["headers"]["Authorization"] == "Bearer " + KEY


def test_default_base_url_is_openrouter(monkeypatch):
    monkeypatch.delenv("JM_LLM_BASE_URL", raising=False)
    c, t, _ = client([(200, completion("ok"))])
    c.chat("m/x", [])
    assert t.requests[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"


def test_headers_and_payload():
    c, t, _ = client([(200, completion("ok", model="vendor/model-1", provider="SomeHost",
                                       prompt_tokens=11, completion_tokens=7))])
    msgs = [{"role": "user", "content": [text_part("look"), image_part(b"\x89PNG..", "image/png")]}]
    res = c.chat("vendor/model", msgs, temperature=0.0, seed=7, max_tokens=99, response_format="json")
    req = t.requests[0]
    h = req["headers"]
    assert h["HTTP-Referer"] == APP_REFERER == "https://jmecury.org"
    assert h["X-Title"] == APP_TITLE == "J Mercury subnet"
    assert h["Content-Type"] == "application/json"
    p = req["json"]
    assert p["model"] == "vendor/model" and p["temperature"] == 0.0 and p["seed"] == 7 and p["max_tokens"] == 99
    assert p["response_format"] == {"type": "json_object"}
    assert p["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert res.text == "ok" and res.model == "vendor/model-1" and res.provider == "SomeHost"
    assert res.requested_model == "vendor/model"
    assert res.usage.as_dict() == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert KEY not in json.dumps(res.as_record())


def test_unset_options_are_not_sent():
    c, t, _ = client([(200, completion("ok"))])
    c.chat("m/x", [])
    assert set(t.requests[0]["json"]) == {"model", "messages"}


def test_retries_429_and_5xx_then_succeeds():
    c, t, sleeps = client([(429, {"error": {"message": "slow down"}}), (502, "bad gateway"),
                           (200, completion("done"))], max_retries=3, backoff_base=1.0)
    res = c.chat("m/x", [])
    assert res.text == "done" and res.attempts == 3
    assert len(t.requests) == 3 and len(sleeps) == 2
    assert 0.5 <= sleeps[0] <= 1.0 and 1.0 <= sleeps[1] <= 2.0  # exponential with jitter


def test_retry_after_is_honoured_and_capped():
    answers = iter([(429, {"retry-after": "3"}, b"{}"), (429, {"Retry-After": "999"}, b"{}"),
                    (200, {}, json.dumps(completion("x")).encode())])
    sleeps = []
    c = LLMClient(api_key=KEY, transport=lambda url, h, b, to: next(answers), sleep=sleeps.append, backoff_max=20.0)
    assert c.chat("m/x", []).text == "x"
    assert sleeps == [3.0, 20.0]


def test_retries_are_bounded():
    c, t, sleeps = client([(503, "down")] * 10, max_retries=2)
    with pytest.raises(LLMError) as ei:
        c.chat("m/x", [])
    assert ei.value.kind == "http" and ei.value.status == 503 and ei.value.retryable
    assert len(t.requests) == 3 and len(sleeps) == 2


def test_network_errors_are_retried():
    c, t, _ = client([ConnectionResetError("boom"), (200, completion("ok"))])
    assert c.chat("m/x", []).text == "ok"


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
def test_client_errors_are_not_retried(status):
    c, t, sleeps = client([(status, {"error": {"message": "nope"}}), (200, completion("never"))])
    with pytest.raises(LLMError) as ei:
        c.chat("m/x", [])
    assert ei.value.status == status and not ei.value.retryable
    assert len(t.requests) == 1 and sleeps == []


def test_200_with_error_object_is_a_provider_error():
    c, t, _ = client([(200, {"error": {"code": 502, "message": "upstream died"}}), (200, completion("ok"))])
    assert c.chat("m/x", []).text == "ok"
    c, t, _ = client([(200, {"error": {"code": 400, "message": "bad input"}})])
    with pytest.raises(LLMError, match="bad input"):
        c.chat("m/x", [])


def test_bad_bodies_are_errors_not_crashes():
    c, t, _ = client([(200, "not json")] * 2 + [(200, {"choices": []})] * 2, max_retries=1)
    with pytest.raises(LLMError) as ei:
        c.chat("m/x", [])
    assert ei.value.kind == "bad_response"
    with pytest.raises(LLMError):
        c.chat("m/x", [])


def test_content_parts_are_joined():
    body = completion("")
    body["choices"][0]["message"]["content"] = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    c, _, _ = client([(200, body)])
    assert c.chat("m/x", []).text == "ab"


def test_deadline_stops_retries():
    now = [100.0]
    t = FakeTransport([(503, "x")] * 5)
    c = LLMClient(api_key=KEY, transport=t, sleep=lambda s: now.__setitem__(0, now[0] + s),
                  clock=lambda: now[0], max_retries=5, backoff_base=4.0)
    with pytest.raises(LLMError):
        c.chat("m/x", [], deadline=106.0)
    assert len(t.requests) <= 2
    assert all(r["timeout"] <= 6.0 for r in t.requests)
    with pytest.raises(LLMError, match="no time left"):
        c.chat("m/x", [], deadline=now[0] + 0.5)


def test_key_never_leaks_into_errors_or_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="jmercury.llm")
    echo = {"error": {"message": f"invalid key {KEY} (Bearer {KEY})"}}
    c, t, _ = client([(401, echo)])
    with pytest.raises(LLMError) as ei:
        c.chat("m/x", [])
    assert KEY not in str(ei.value) and "[redacted]" in str(ei.value)
    assert KEY not in repr(ei.value) and KEY not in repr(c)
    c, t, _ = client([(503, echo)] * 2 + [ConnectionError(KEY)] * 2, max_retries=3)
    with pytest.raises(LLMError) as ei:
        c.chat("m/x", [])
    assert KEY not in str(ei.value)
    assert KEY not in caplog.text
    assert "Authorization" not in caplog.text


def test_no_model_is_a_config_error():
    c, _, _ = client([])
    with pytest.raises(LLMConfigError):
        c.chat("", [])


class _Echo(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        body = self.rfile.read(n)
        _Echo.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, json.loads(body)))
        out = json.dumps(completion("over the wire")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def test_urllib_transport_against_a_local_server(monkeypatch):
    """The real transport, pointed at 127.0.0.1 through JM_LLM_BASE_URL (no outside network)."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        monkeypatch.setenv("JM_LLM_BASE_URL", f"http://127.0.0.1:{srv.server_address[1]}/api/v1")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        c = LLMClient(api_key=KEY)
        assert c.transport is llm.urllib_transport
        res = c.chat("m/x", [{"role": "user", "content": "hi"}], temperature=0)
        assert res.text == "over the wire"
        path, headers, payload = _Echo.seen[-1]
        assert path == "/api/v1/chat/completions"
        assert headers["http-referer"] == "https://jmecury.org" and headers["x-title"] == "J Mercury subnet"
        assert payload["temperature"] == 0
    finally:
        srv.shutdown()
        srv.server_close()


def test_urllib_transport_network_error_is_retryable():
    c = LLMClient(api_key=KEY, base_url="http://127.0.0.1:9", max_retries=1, sleep=lambda s: None, timeout=2)
    with pytest.raises(LLMError) as ei:
        c.chat("m/x", [])
    assert ei.value.kind == "network" and ei.value.retryable
