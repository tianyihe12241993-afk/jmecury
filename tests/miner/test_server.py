import http.client
import json
import socket
import threading
import time

import pytest

from jmercury.brief import generate
from jmercury.miner import server as srvmod
from jmercury.miner.generate import GenerationError
from jmercury.miner.server import MinerServer
from jmercury.protocol import (
    SITE_TASK_PATH,
    BadSignature,
    SiteTask,
    hotkey_ss58,
    keypair_from_uri,
    sign_request,
    verify_response,
)

ALICE = keypair_from_uri("//Alice")  # validator
BOB = keypair_from_uri("//Bob")  # miner
CHARLIE = keypair_from_uri("//Charlie")  # not allow-listed
DAVE = keypair_from_uri("//Dave")  # second validator
BUNDLE = "UEsFBgAAAAAAAAAAAAAAAAAAAAAAAA=="  # an empty zip: the server does not look inside


@pytest.fixture(scope="module")
def brief():
    return generate(7)


def task_body(brief) -> bytes:
    return SiteTask(brief=brief.model_dump(mode="json")).to_body()


def post(url: str, body: bytes, headers: dict, path: str = SITE_TASK_PATH):
    host, port = url.split("//")[1].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        conn.request("POST", path, body=body, headers=headers)
        r = conn.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        conn.close()


def signed(body: bytes, wallet=ALICE, receiver=None, nonce_ns=None):
    return sign_request(wallet, body, receiver or hotkey_ss58(BOB), nonce_ns=nonce_ns)


@pytest.fixture
def miner():
    calls = []

    def gen(brief, deadline):
        calls.append((brief.task_id, deadline - time.monotonic()))
        return BUNDLE

    with MinerServer(BOB, [hotkey_ss58(ALICE), hotkey_ss58(DAVE)], gen) as srv:
        srv.calls = calls
        yield srv


def test_signed_request_gets_a_signed_bundle(miner, brief):
    body = task_body(brief)
    h = signed(body)
    status, headers, reply = post(miner.url, body, h)
    assert status == 200, reply
    caller = verify_response(headers, reply, hotkey_ss58(ALICE), hotkey_ss58(BOB),
                             request_nonce=int(h["X-Bittensor-Nonce"]))
    assert caller.hotkey_ss58 == hotkey_ss58(BOB)
    answer = SiteTask.from_body(reply)
    assert answer.bundle_b64 == BUNDLE and answer.brief == brief.model_dump(mode="json")
    assert answer.miner_version.startswith("jmercury-ref-miner/")
    assert miner.calls[0][0] == brief.task_id and 140 < miner.calls[0][1] <= 150


def test_response_signature_is_bound_to_the_request(miner, brief):
    body = task_body(brief)
    h = signed(body)
    status, headers, reply = post(miner.url, body, h)
    assert status == 200
    with pytest.raises(Exception):
        verify_response(headers, reply, hotkey_ss58(ALICE), hotkey_ss58(BOB),
                        request_nonce=int(h["X-Bittensor-Nonce"]) + 1)
    with pytest.raises(BadSignature):
        verify_response(headers, reply, hotkey_ss58(ALICE), hotkey_ss58(CHARLIE),
                        request_nonce=int(h["X-Bittensor-Nonce"]))


def test_unsigned_request_is_401(miner, brief):
    status, _, reply = post(miner.url, task_body(brief), {"Content-Type": "application/json"})
    assert status == 401 and json.loads(reply)["error"] == "unauthenticated"
    assert miner.calls == []


def test_tampered_body_is_401(miner, brief):
    body = task_body(brief)
    h = signed(body)
    status, _, _ = post(miner.url, body.replace(b'"protocol_version":1', b'"protocol_version":1 '), h)
    assert status == 401


def test_wrong_receiver_is_403(miner, brief):
    body = task_body(brief)
    status, _, reply = post(miner.url, body, signed(body, receiver=hotkey_ss58(CHARLIE)))
    assert status == 403 and json.loads(reply)["error"] == "WrongReceiver"


def test_replay_is_403(miner, brief):
    body = task_body(brief)
    h = signed(body)
    assert post(miner.url, body, h)[0] == 200
    status, _, reply = post(miner.url, body, h)
    assert status == 403 and json.loads(reply)["error"] == "ReplayedRequest"
    assert len(miner.calls) == 1


def test_stale_request_is_403(miner, brief):
    body = task_body(brief)
    status, _, reply = post(miner.url, body, signed(body, nonce_ns=time.time_ns() - 60 * 10**9))
    assert status == 403 and json.loads(reply)["error"] == "StaleRequest"


def test_not_allow_listed_is_403(miner, brief):
    body = task_body(brief)
    status, _, reply = post(miner.url, body, signed(body, wallet=CHARLIE))
    assert status == 403 and json.loads(reply)["error"] == "not_allowed"
    assert miner.calls == []


def test_oversize_body_is_413(miner):
    body = b"x" * (srvmod.MAX_REQUEST_BYTES + 1)
    status, _, reply = post(miner.url, body, signed(body))
    assert status == 413 and json.loads(reply)["error"] == "body_too_large"


def test_oversize_is_refused_before_the_body_is_read(miner):
    with socket.create_connection(("127.0.0.1", miner.port), timeout=5) as s:
        s.sendall(f"POST {SITE_TASK_PATH} HTTP/1.1\r\nHost: x\r\nContent-Length: 999999999\r\n\r\n".encode())
        assert s.recv(200).startswith(b"HTTP/1.1 413")


def test_missing_content_length_is_411(miner):
    with socket.create_connection(("127.0.0.1", miner.port), timeout=5) as s:
        s.sendall(f"POST {SITE_TASK_PATH} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        assert s.recv(200).startswith(b"HTTP/1.1 411")


def test_invalid_brief_is_400(miner, brief):
    bad = brief.model_dump(mode="json")
    bad["phone"] = "call us"
    body = SiteTask(brief=bad).to_body()
    status, _, reply = post(miner.url, body, signed(body))
    assert status == 400 and json.loads(reply)["error"] == "bad_brief"


def test_unknown_path_is_404_and_health_works(miner, brief):
    body = task_body(brief)
    assert post(miner.url, body, signed(body), path="/v1/other")[0] == 404
    conn = http.client.HTTPConnection("127.0.0.1", miner.port, timeout=5)
    conn.request("GET", "/health")
    r = conn.getresponse()
    assert r.status == 200 and json.loads(r.read())["hotkey"] == hotkey_ss58(BOB)
    conn.close()


def test_per_caller_concurrency_limit_is_429(brief):
    release = threading.Event()
    started = threading.Event()

    def slow(b, deadline):
        started.set()
        release.wait(10)
        return BUNDLE

    with MinerServer(BOB, [hotkey_ss58(ALICE), hotkey_ss58(DAVE)], slow, per_caller=1) as srv:
        results = {}
        body = task_body(brief)
        th = threading.Thread(target=lambda: results.setdefault("first", post(srv.url, body, signed(body))))
        th.start()
        assert started.wait(5)
        status, _, reply = post(srv.url, body, signed(body))
        assert status == 429 and json.loads(reply)["error"] == "too_many_tasks"
        other = threading.Thread(target=lambda: results.setdefault("dave", post(srv.url, body, signed(body, wallet=DAVE))))
        other.start()
        time.sleep(0.3)
        release.set()
        th.join(10)
        other.join(10)
        assert results["first"][0] == 200 and results["dave"][0] == 200


def test_global_limit_is_503(brief):
    release = threading.Event()
    started = threading.Event()

    def slow(b, deadline):
        started.set()
        release.wait(10)
        return BUNDLE

    with MinerServer(BOB, [hotkey_ss58(ALICE), hotkey_ss58(DAVE)], slow, max_concurrent=1) as srv:
        body = task_body(brief)
        th = threading.Thread(target=lambda: post(srv.url, body, signed(body)))
        th.start()
        assert started.wait(5)
        assert post(srv.url, body, signed(body, wallet=DAVE))[0] == 503
        release.set()
        th.join(10)


def test_generation_timeout_is_504(brief):
    def slow(b, deadline):
        time.sleep(1.5)
        return BUNDLE

    with MinerServer(BOB, [hotkey_ss58(ALICE)], slow, timeout=0.3) as srv:
        body = task_body(brief)
        t0 = time.monotonic()
        status, _, reply = post(srv.url, body, signed(body))
        assert status == 504 and time.monotonic() - t0 < 1.4


def test_generation_failure_is_500_and_the_server_survives(brief):
    n = []

    def failing(b, deadline):
        n.append(1)
        if len(n) == 1:
            raise GenerationError("checks_failed", "no attempt passed the gates")
        if len(n) == 2:
            raise ValueError("bug")
        return BUNDLE

    with MinerServer(BOB, [hotkey_ss58(ALICE)], failing) as srv:
        for want in (500, 500, 200):
            body = task_body(brief)
            assert post(srv.url, body, signed(body))[0] == want


def test_permit_check_hook(brief):
    with MinerServer(BOB, [hotkey_ss58(ALICE)], lambda b, d: BUNDLE, permit_check=lambda hk: False) as srv:
        body = task_body(brief)
        status, _, reply = post(srv.url, body, signed(body))
        assert status == 403 and json.loads(reply)["error"] == "no_permit"


def test_cli_needs_a_key(monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    rc = srvmod.main(["--dev-hotkey-uri", "//Bob", "--allow-hotkey", hotkey_ss58(ALICE), "--port", "0"])
    assert rc == 2 and "OPENROUTER_API_KEY is not set" in capsys.readouterr().err


def test_cli_needs_an_allow_list_and_a_short_timeout():
    with pytest.raises(SystemExit):
        srvmod.main(["--dev-hotkey-uri", "//Bob"])
    with pytest.raises(SystemExit):
        srvmod.main(["--dev-hotkey-uri", "//Bob", "--allow-hotkey", hotkey_ss58(ALICE), "--timeout", "180"])
