"""Validator side of the wire: send a signed ``SiteTask`` to one miner and check the signed answer.

    res = query_miner(wallet, "http://1.2.3.4:8091", miner_hotkey_ss58, brief)
    if res.ok: res.bundle_b64, res.response_time

The whole exchange (connect, send, wait, read) must finish within ``MINER_TIMEOUT`` (180 s, docs/DESIGN.md
section 5); the answer may be at most ``MAX_RESPONSE_BYTES``; it must be signed by the miner hotkey that was asked,
for this validator's hotkey and this request (``protocol.verify_response``), and must echo the brief. Every failure
is returned as a ``MinerResult`` with ``ok=False`` and a machine-readable ``failure`` code, never raised, so a
round can treat it as "no site" (score 0) and keep going.

Where the miners come from (axon addresses and hotkeys from the subnet metagraph, validator permits) is not this
module's job: callers pass ``(url, hotkey)`` pairs. That is the hook for the chain code to come.
"""

from __future__ import annotations

import concurrent.futures
import http.client
import json
import socket
import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from pydantic import ValidationError

from ..brief import Brief
from ..bundle import MAX_ZIPPED
from ..protocol import (
    HEADER_NONCE,
    PROTOCOL_VERSION,
    SITE_TASK_PATH,
    AuthError,
    InMemoryNonceStore,
    SiteTask,
    hotkey_ss58,
    sign_request,
    verify_response,
)

MINER_TIMEOUT = 180.0
# A bundle is at most 2 MiB zipped, so at most ~2.67 MiB of base64; plus the echoed brief and JSON framing.
MAX_RESPONSE_BYTES = (MAX_ZIPPED * 4) // 3 + 4 + 256 * 1024
_CHUNK = 64 * 1024
# The miner signs its answer just before sending it; allow for the transfer and some clock difference.
RESPONSE_MAX_AGE = 30.0
_RESPONSE_NONCES = InMemoryNonceStore(retention=RESPONSE_MAX_AGE + 10.0)

FAILURES = ("bad_url", "connect_error", "timeout", "http_status", "response_too_large", "bad_signature",
            "bad_body", "protocol_version", "brief_mismatch", "no_bundle")


@dataclass
class MinerResult:
    hotkey: str
    url: str
    ok: bool
    bundle_b64: Optional[str] = None
    response_time: Optional[float] = None  # seconds from sending the request to the last byte of the answer
    miner_version: Optional[str] = None
    failure: Optional[str] = None  # one of FAILURES
    detail: str = ""
    status: Optional[int] = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("bundle_b64")
        return d


class _Fail(Exception):
    def __init__(self, failure: str, detail: str = "", status: Optional[int] = None):
        self.failure, self.detail, self.status = failure, detail, status
        super().__init__(f"{failure}: {detail}")


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _Fail("timeout", "no answer within the time limit")
    return left


def _connection(url: str, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise _Fail("bad_url", url)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    base = parts.path.rstrip("/")
    return cls(parts.hostname, parts.port, timeout=timeout), base + SITE_TASK_PATH


def _read_limited(resp: http.client.HTTPResponse, deadline: float, limit: int) -> bytes:
    declared = resp.getheader("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise _Fail("response_too_large", f"Content-Length {declared} > {limit}")
    buf = bytearray()
    while True:
        sock = getattr(resp, "fp", None) and getattr(resp.fp, "raw", None)
        if sock is not None and hasattr(sock, "_sock"):
            sock._sock.settimeout(_remaining(deadline))
        else:
            _remaining(deadline)
        chunk = resp.read1(min(_CHUNK, limit + 1 - len(buf)))  # one socket read: the deadline is checked per chunk
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > limit:
            raise _Fail("response_too_large", f"more than {limit} bytes")


def query_miner(wallet: Any, url: str, miner_hotkey: str, brief: Brief, *, timeout: float = MINER_TIMEOUT,
                max_response_bytes: int = MAX_RESPONSE_BYTES) -> MinerResult:
    """Send ``brief`` to the miner at ``url`` (``http://host:port``) whose hotkey is ``miner_hotkey``."""
    me = hotkey_ss58(wallet)
    brief_dict = brief.model_dump(mode="json")
    body = SiteTask(protocol_version=PROTOCOL_VERSION, brief=brief_dict).to_body()
    res = MinerResult(hotkey=miner_hotkey, url=url, ok=False)
    start = time.monotonic()
    deadline = start + timeout
    conn = None
    try:
        conn, path = _connection(url, timeout)
        headers = sign_request(wallet, body, miner_hotkey, path=SITE_TASK_PATH)
        request_nonce = int(headers[HEADER_NONCE])
        headers = {**headers, "Content-Type": "application/json", "Content-Length": str(len(body))}
        try:
            conn.request("POST", path, body=body, headers=headers)
            if conn.sock is not None:
                conn.sock.settimeout(_remaining(deadline))
            resp = conn.getresponse()
            res.status = resp.status
            reply = _read_limited(resp, deadline, max_response_bytes)
        except (socket.timeout, TimeoutError):
            raise _Fail("timeout", "no answer within the time limit") from None
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            raise _Fail("connect_error", type(exc).__name__) from None
        res.response_time = time.monotonic() - start
        if res.response_time > timeout:
            raise _Fail("timeout", f"{res.response_time:.1f} s")
        if resp.status != 200:
            detail = reply[:300].decode("utf-8", "replace")
            raise _Fail("http_status", detail, resp.status)
        try:
            verify_response(dict(resp.getheaders()), reply, me, miner_hotkey, request_nonce=request_nonce,
                            max_age=RESPONSE_MAX_AGE, nonce_store=_RESPONSE_NONCES)
        except AuthError as exc:
            raise _Fail("bad_signature", f"{type(exc).__name__}: {exc}") from None
        try:
            answer = SiteTask.from_body(reply)
        except (ValidationError, ValueError) as exc:
            raise _Fail("bad_body", str(exc)[:300]) from None
        if answer.protocol_version != PROTOCOL_VERSION:
            raise _Fail("protocol_version", str(answer.protocol_version))
        if answer.brief != brief_dict:
            raise _Fail("brief_mismatch", "the answer does not echo the brief that was sent")
        if not answer.bundle_b64:
            raise _Fail("no_bundle", "")
        res.ok = True
        res.bundle_b64 = answer.bundle_b64
        res.miner_version = answer.miner_version
    except _Fail as f:
        res.failure, res.detail = f.failure, f.detail
        if f.status is not None:
            res.status = f.status
    finally:
        if conn is not None:
            conn.close()
    return res


def query_miners(wallet: Any, miners: Iterable[tuple[str, str]], brief: Brief, *, timeout: float = MINER_TIMEOUT,
                 max_workers: int = 32, **kwargs: Any) -> list[MinerResult]:
    """Ask several miners ``[(url, hotkey), ...]`` in parallel; results are in the same order."""
    miners = list(miners)
    if not miners:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(miners))) as pool:
        futs = [pool.submit(query_miner, wallet, url, hk, brief, timeout=timeout, **kwargs) for url, hk in miners]
        return [f.result() for f in futs]


def summary(results: list[MinerResult]) -> str:
    return json.dumps([r.as_dict() for r in results], indent=2)
