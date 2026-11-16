"""Reference miner server: answers signed ``SiteTask`` requests with a signed site bundle.

    python -m jmercury.miner.server --wallet.name miner --wallet.hotkey default \\
        --allow-hotkey <validator ss58> [--allow-hotkey ...] [--host 127.0.0.1] [--port 8091]

One endpoint, ``POST /v1/site-task`` (``protocol.SITE_TASK_PATH``), plus ``GET /health``. A request must carry
btauth/1 headers signed by an allow-listed validator hotkey for this miner's hotkey (receiver binding), be fresh
(``--max-age`` seconds) and not replayed. The answer is a ``SiteTask`` with ``bundle_b64`` and ``miner_version``,
signed by the miner hotkey for the calling validator and bound to the request nonce (``protocol.sign_response``).

Status codes: 400 bad body or brief, 401 missing, malformed or invalid signature, 403 wrong receiver, stale,
replayed or not allow-listed, 404 unknown path, 411 no Content-Length, 413 body too large, 429 this caller already
has the maximum number of tasks running, 503 the miner is at its global limit, 500 generation failed, 504
generation did not finish in time. Error bodies are unsigned JSON ``{"error": code, "detail": text}``.

Validators wait at most 180 s (docs/DESIGN.md section 5); the default generation budget is 150 s.

The allow-list is static for now. ``MinerServer(permit_check=...)`` is the hook for a later chain check (for
example "the caller holds a validator permit on the subnet's metagraph"); no chain code lives here.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Optional

from pydantic import ValidationError

from ..brief import Brief
from ..protocol import (
    CRYPTO_SR25519,
    PROTOCOL_VERSION,
    SITE_TASK_PATH,
    AuthError,
    InMemoryNonceStore,
    MalformedAuth,
    SiteTask,
    BadSignature,
    hotkey_ss58,
    keypair_from_uri,
    load_wallet,
    sign_response,
    verify_request,
)
from .generate import MINER_VERSION, GenerationError

log = logging.getLogger("jmercury.miner.server")

MAX_REQUEST_BYTES = 64 * 1024  # a brief is about 2 KB
DEFAULT_TIMEOUT = 150.0  # under the validator's 180 s
DEFAULT_MAX_AGE = 10.0
DEFAULT_PER_CALLER = 1
DEFAULT_MAX_CONCURRENT = 4

# generate_fn(brief, deadline) -> bundle_b64. ``deadline`` is a time.monotonic() value.
GenerateFn = Callable[[Brief, float], str]
PermitCheck = Callable[[str], bool]


def llm_generate_fn(model: Optional[str] = None, check: str = "auto") -> GenerateFn:
    """The default generator: ``jmercury.miner.generate.generate_site`` with an OpenRouter client."""
    from ..llm import LLMClient
    from .generate import generate_site

    client = LLMClient()  # raises LLMConfigError now, at start-up, when OPENROUTER_API_KEY is missing

    def run(brief: Brief, deadline: float) -> str:
        result = generate_site(brief, client=client, model=model, check=check, deadline=deadline)
        log.info("generated task=%s attempts=%d usage=%s check=%s", brief.task_id, len(result.attempts),
                 result.usage.as_dict(), result.report.level)
        return result.bundle_b64

    return run


class HTTPError(Exception):
    def __init__(self, status: int, code: str, detail: str = ""):
        self.status, self.code, self.detail = status, code, detail
        super().__init__(f"{status} {code}: {detail}")


@dataclass
class _Slots:
    per_caller: int
    total: int
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: dict[str, int] = field(default_factory=dict)

    def acquire(self, caller: str) -> None:
        with self.lock:
            if self.running.get(caller, 0) >= self.per_caller:
                raise HTTPError(429, "too_many_tasks", f"at most {self.per_caller} task(s) at a time per validator")
            if sum(self.running.values()) >= self.total:
                raise HTTPError(503, "busy", "the miner is at its concurrency limit")
            self.running[caller] = self.running.get(caller, 0) + 1

    def release(self, caller: str) -> None:
        with self.lock:
            n = self.running.get(caller, 0) - 1
            if n > 0:
                self.running[caller] = n
            else:
                self.running.pop(caller, None)


class MinerServer:
    """The HTTP server. ``port=0`` picks a free port (see ``.port``)."""

    def __init__(self, wallet: Any, allow_hotkeys: Iterable[str], generate_fn: GenerateFn, *,
                 host: str = "127.0.0.1", port: int = 0, timeout: float = DEFAULT_TIMEOUT,
                 max_body: int = MAX_REQUEST_BYTES, per_caller: int = DEFAULT_PER_CALLER,
                 max_concurrent: int = DEFAULT_MAX_CONCURRENT, max_age: float = DEFAULT_MAX_AGE,
                 permit_check: Optional[PermitCheck] = None, miner_version: str = MINER_VERSION):
        self.wallet = wallet
        self.hotkey = hotkey_ss58(wallet)
        self.allow = frozenset(allow_hotkeys)
        self.generate_fn = generate_fn
        self.timeout = timeout
        self.max_body = max_body
        self.max_age = max_age
        self.permit_check = permit_check
        self.miner_version = miner_version
        self.nonces = InMemoryNonceStore(retention=max(60.0, 4 * max_age))
        self.slots = _Slots(per_caller, max_concurrent)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent, thread_name_prefix="jm-gen")
        self.httpd = ThreadingHTTPServer((host, port), _make_handler(self))
        self.httpd.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        host = self.httpd.server_address[0]
        return f"http://{host}:{self.port}"

    def start(self) -> "MinerServer":
        """Serve in a background thread (tests); use ``serve_forever`` in the foreground."""
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1},
                                        name="jm-miner-http", daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> "MinerServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ----------------------------------------------------------------------------------

    def authenticate(self, headers: Any, body: bytes) -> Any:
        try:
            caller = verify_request(headers, body, self.hotkey, expected_crypto_type=CRYPTO_SR25519,
                                    max_age=self.max_age, nonce_store=self.nonces)
        except (MalformedAuth, BadSignature) as exc:
            raise HTTPError(401, "unauthenticated", str(exc)) from None
        except AuthError as exc:  # WrongReceiver, StaleRequest, ReplayedRequest
            raise HTTPError(403, type(exc).__name__, str(exc)) from None
        if caller.hotkey_ss58 not in self.allow:
            raise HTTPError(403, "not_allowed", "this hotkey is not on the miner's allow-list")
        if self.permit_check is not None and not self.permit_check(caller.hotkey_ss58):
            raise HTTPError(403, "no_permit", "the caller failed the permit check")
        return caller

    def handle_task(self, headers: Any, body: bytes) -> tuple[bytes, dict[str, str]]:
        caller = self.authenticate(headers, body)
        try:
            task = SiteTask.from_body(body)
        except ValidationError as exc:
            raise HTTPError(400, "bad_body", str(exc.errors()[:3])) from None
        if task.protocol_version != PROTOCOL_VERSION:
            raise HTTPError(400, "protocol_version", f"this miner speaks version {PROTOCOL_VERSION}")
        try:
            brief = Brief.model_validate(task.brief)
        except ValidationError as exc:
            raise HTTPError(400, "bad_brief", str(exc.errors()[:3])) from None

        who = caller.hotkey_ss58
        self.slots.acquire(who)
        deadline = time.monotonic() + self.timeout
        try:
            fut = self.pool.submit(self.generate_fn, brief, deadline)
        except RuntimeError:
            self.slots.release(who)
            raise HTTPError(503, "busy", "the miner is shutting down") from None
        fut.add_done_callback(lambda _f: self.slots.release(who))  # the slot is held until generation really ends
        try:
            bundle_b64 = fut.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError:
            raise HTTPError(504, "timeout", f"generation took longer than {self.timeout:.0f} s") from None
        except GenerationError as exc:
            raise HTTPError(500, "generation_failed", str(exc)[:500]) from None
        except Exception as exc:  # noqa: BLE001 - never crash the server on one task
            log.exception("generation crashed for task %s", brief.task_id)
            raise HTTPError(500, "generation_failed", type(exc).__name__) from None

        reply = SiteTask(protocol_version=PROTOCOL_VERSION, brief=task.brief, bundle_b64=bundle_b64,
                         miner_version=self.miner_version).to_body()
        sig = sign_response(self.wallet, reply, who, request_nonce=caller.nonce_ns)
        return reply, sig


def _make_handler(server: MinerServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "jmercury-miner"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # headers (and signatures) are never logged
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, headers: Optional[dict[str, str]] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            if status >= 400:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def _error(self, err: HTTPError) -> None:
            self._send(err.status, json.dumps({"error": err.code, "detail": err.detail}).encode("utf-8"))

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, json.dumps({"ok": True, "hotkey": server.hotkey,
                                            "miner_version": server.miner_version}).encode("utf-8"))
            else:
                self._error(HTTPError(404, "not_found", self.path))

        def do_POST(self) -> None:
            try:
                if self.path != SITE_TASK_PATH:
                    raise HTTPError(404, "not_found", self.path)
                raw_len = self.headers.get("Content-Length")
                if raw_len is None:
                    raise HTTPError(411, "length_required", "send Content-Length")
                try:
                    length = int(raw_len)
                except ValueError:
                    raise HTTPError(400, "bad_length", "Content-Length is not an integer") from None
                if length < 0:
                    raise HTTPError(400, "bad_length", "negative Content-Length")
                if length > server.max_body:
                    raise HTTPError(413, "body_too_large", f"at most {server.max_body} bytes")
                body = self.rfile.read(length)
                reply, sig = server.handle_task(self.headers, body)
            except HTTPError as err:
                self._error(err)
                return
            self._send(200, reply, sig)

    return Handler


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jmercury.miner.server",
                                 description="Reference miner: serve SiteTask requests over btauth/1.")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--wallet.name", dest="wallet_name", help="bittensor wallet name")
    ap.add_argument("--wallet.hotkey", dest="wallet_hotkey", default="default", help="hotkey name (default: default)")
    ap.add_argument("--wallet.path", dest="wallet_path", help="wallet directory (default: bittensor's default)")
    ap.add_argument("--dev-hotkey-uri", help="use a public development key such as //Bob instead of a wallet "
                                             "(local testing only: anyone can sign as it)")
    ap.add_argument("--allow-hotkey", action="append", default=[], required=True,
                    help="validator hotkey (SS58) allowed to send tasks; repeat for several")
    ap.add_argument("--model", help="OpenRouter model slug (default: $JM_MINER_MODEL or the generator's default)")
    ap.add_argument("--check", choices=("auto", "validator", "static"), default="auto",
                    help="self-check level for generated sites (see python -m jmercury.miner.generate)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help=f"generation budget in seconds (default {DEFAULT_TIMEOUT:.0f}; validators wait 180)")
    ap.add_argument("--per-caller", type=int, default=DEFAULT_PER_CALLER, help="concurrent tasks per validator")
    ap.add_argument("--max-concurrent", type=int, default=DEFAULT_MAX_CONCURRENT, help="concurrent tasks in total")
    ap.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE, help="maximum request age in seconds")
    args = ap.parse_args(argv)

    if args.timeout >= 180:
        ap.error("--timeout must be under 180 s, the validator's limit")
    if bool(args.wallet_name) == bool(args.dev_hotkey_uri):
        ap.error("give either --wallet.name (with --wallet.hotkey) or --dev-hotkey-uri")
    wallet = (keypair_from_uri(args.dev_hotkey_uri) if args.dev_hotkey_uri
              else load_wallet(args.wallet_name, args.wallet_hotkey, args.wallet_path))
    logging.basicConfig(level=os.environ.get("JM_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    from ..llm import LLMConfigError

    try:
        gen = llm_generate_fn(model=args.model, check=args.check)
    except LLMConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    srv = MinerServer(wallet, args.allow_hotkey, gen, host=args.host, port=args.port, timeout=args.timeout,
                      per_caller=args.per_caller, max_concurrent=args.max_concurrent, max_age=args.max_age)
    log.info("miner %s listening on %s%s (allow-list: %d hotkeys)", srv.hotkey, srv.url, SITE_TASK_PATH,
             len(srv.allow))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
