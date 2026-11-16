import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jmercury.brief import generate
from jmercury.miner.server import MinerServer
from jmercury.protocol import HEADER_NONCE, SiteTask, hotkey_ss58, keypair_from_uri, sign_response
from jmercury.validator import client as vc
from jmercury.validator.client import MAX_RESPONSE_BYTES, query_miner, query_miners

ALICE = keypair_from_uri("//Alice")  # validator
BOB = keypair_from_uri("//Bob")  # miner
CHARLIE = keypair_from_uri("//Charlie")
BUNDLE = "UEsFBgAAAAAAAAAAAAAAAAAAAAAAAA=="


@pytest.fixture(scope="module")
def brief():
    return generate(11)


def test_success_against_the_reference_server(brief):
    with MinerServer(BOB, [hotkey_ss58(ALICE)], lambda b, d: BUNDLE) as srv:
        r = query_miner(ALICE, srv.url, hotkey_ss58(BOB), brief)
    assert r.ok and r.failure is None and r.bundle_b64 == BUNDLE and r.status == 200
    assert 0 <= r.response_time < 5 and r.miner_version.startswith("jmercury-ref-miner/")
    assert "bundle_b64" not in r.as_dict()


def test_asking_the_wrong_hotkey_fails_at_the_miner(brief):
    with MinerServer(BOB, [hotkey_ss58(ALICE)], lambda b, d: BUNDLE) as srv:
        r = query_miner(ALICE, srv.url, hotkey_ss58(CHARLIE), brief)
    assert not r.ok and r.failure == "http_status" and r.status == 403 and "WrongReceiver" in r.detail


class FakeMiner:
    """A miner that misbehaves on purpose. ``mode`` picks how."""

    def __init__(self, mode: str, signer=BOB):
        self.mode = mode
        self.signer = signer
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                task = json.loads(body)
                nonce = int(self.headers[HEADER_NONCE])
                validator = self.headers["X-Bittensor-Hotkey"]
                outer.handle(self, task, nonce, validator)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def handle(self, h, task, nonce, validator):
        mode = self.mode
        brief = task["brief"]
        if mode == "other_brief":
            brief = dict(brief, name="Someone Else")
        reply = SiteTask(brief=brief, bundle_b64=None if mode == "no_bundle" else BUNDLE, miner_version="fake/1")
        data = reply.to_body()
        if mode == "not_json":
            data = b"<html>hello</html>"
        if mode == "huge":
            data = b'{"x":"' + b"a" * (MAX_RESPONSE_BYTES + 10) + b'"}'
        headers = {} if mode == "unsigned" else sign_response(
            self.signer, data, validator, request_nonce=nonce + (1 if mode == "wrong_nonce" else 0))
        if mode == "slow":
            time.sleep(1.5)
        h.send_response(200)
        for k, v in headers.items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        if mode == "drip":
            for i in range(0, len(data), 8):
                h.wfile.write(data[i:i + 8])
                h.wfile.flush()
                time.sleep(0.1)
            return
        h.wfile.write(data)

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.mark.parametrize("mode,failure", [
    ("unsigned", "bad_signature"),
    ("wrong_nonce", "bad_signature"),
    ("not_json", "bad_body"),
    ("other_brief", "brief_mismatch"),
    ("no_bundle", "no_bundle"),
    ("huge", "response_too_large"),
])
def test_bad_answers_are_structured_failures(brief, mode, failure):
    with FakeMiner(mode) as m:
        r = query_miner(ALICE, m.url, hotkey_ss58(BOB), brief)
    assert not r.ok and r.failure == failure, r
    assert r.bundle_b64 is None


def test_answer_signed_by_another_hotkey_is_rejected(brief):
    with FakeMiner("ok", signer=CHARLIE) as m:
        r = query_miner(ALICE, m.url, hotkey_ss58(BOB), brief)
    assert not r.ok and r.failure == "bad_signature" and "BadSignature" in r.detail


def test_good_fake_miner_passes(brief):
    with FakeMiner("ok") as m:
        r = query_miner(ALICE, m.url, hotkey_ss58(BOB), brief)
    assert r.ok and r.miner_version == "fake/1"


def test_slow_miner_times_out(brief):
    with FakeMiner("slow") as m:
        t0 = time.monotonic()
        r = query_miner(ALICE, m.url, hotkey_ss58(BOB), brief, timeout=0.5)
    assert not r.ok and r.failure == "timeout" and time.monotonic() - t0 < 1.4


def test_dripping_miner_times_out_on_the_total_budget(brief):
    with FakeMiner("drip") as m:
        t0 = time.monotonic()
        r = query_miner(ALICE, m.url, hotkey_ss58(BOB), brief, timeout=0.6)
    assert not r.ok and r.failure == "timeout" and time.monotonic() - t0 < 1.5


def test_size_limit_is_configurable(brief):
    with FakeMiner("ok") as m:
        r = query_miner(ALICE, m.url, hotkey_ss58(BOB), brief, max_response_bytes=100)
    assert r.failure == "response_too_large"


def test_connection_refused_and_bad_url(brief):
    r = query_miner(ALICE, "http://127.0.0.1:9", hotkey_ss58(BOB), brief, timeout=3)
    assert r.failure == "connect_error"
    assert query_miner(ALICE, "ftp://127.0.0.1:9", hotkey_ss58(BOB), brief).failure == "bad_url"


def test_limits_match_the_design():
    assert vc.MINER_TIMEOUT == 180.0
    assert MAX_RESPONSE_BYTES > (2 * 1024 * 1024) * 4 // 3


def test_query_miners_keeps_order(brief):
    with MinerServer(BOB, [hotkey_ss58(ALICE)], lambda b, d: BUNDLE) as srv, FakeMiner("unsigned") as bad:
        rs = query_miners(ALICE, [(bad.url, hotkey_ss58(BOB)), (srv.url, hotkey_ss58(BOB))], brief)
    assert [r.ok for r in rs] == [False, True] and rs[0].url == bad.url
    assert query_miners(ALICE, [], brief) == []
