"""Small OpenRouter chat client (OpenAI-compatible ``POST /chat/completions``), standard library only.

Used by the validator's judge and by the reference miner.

Settings:
- ``OPENROUTER_API_KEY``: the API key. It is read once when the client is built, sent only in the
  ``Authorization`` header, and never logged, put in an exception, a ``repr`` or a record.
- ``JM_LLM_BASE_URL``: overrides the base URL (default ``https://openrouter.ai/api/v1``); tests point it at
  a local fake server.

Every request carries ``HTTP-Referer: https://jmecury.org`` and ``X-Title: J Mercury subnet`` (OpenRouter's
app attribution headers). 429 and 5xx answers and network errors are retried a bounded number of times with
exponential backoff (``Retry-After`` is honoured up to a cap); other 4xx answers fail at once.

A transport is any callable ``(url, headers, body, timeout) -> (status, headers, body)``; ``FakeTransport``
gives scripted answers for tests, so no test ever needs the network or a key.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence, Union

log = logging.getLogger("jmercury.llm")

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
ENV_KEY = "OPENROUTER_API_KEY"
ENV_BASE_URL = "JM_LLM_BASE_URL"
APP_REFERER = "https://jmecury.org"
APP_TITLE = "J Mercury subnet"
RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529})
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# (status, headers, body); status 0 is never returned: network failures raise TransportError.
TransportResult = tuple[int, Mapping[str, str], bytes]
Transport = Callable[[str, Mapping[str, str], bytes, float], TransportResult]


class LLMError(RuntimeError):
    """A chat call failed. ``kind`` is machine-readable; the message never contains the API key."""

    def __init__(self, kind: str, message: str, *, status: Optional[int] = None, retryable: bool = False):
        self.kind = kind
        self.status = status
        self.retryable = retryable
        super().__init__(message)


class LLMConfigError(LLMError):
    def __init__(self, message: str):
        super().__init__("config", message)


class TransportError(Exception):
    """Network-level failure (connection refused, timeout, reset)."""


def urllib_transport(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> TransportResult:
    req = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https or a test URL
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        data = b""
        try:
            data = exc.read(64 * 1024)
        except Exception:  # pragma: no cover - best effort
            pass
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, data
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TransportError(type(reason).__name__ if not isinstance(reason, str) else reason) from None


@dataclass
class FakeTransport:
    """Scripted transport for tests.

    ``responses`` items are ``(status, body)`` pairs (body: dict -> JSON, str or bytes), a callable
    ``(request_dict) -> (status, body)``, or an Exception instance to raise as a network failure.
    Every request is recorded in ``requests`` as ``{"url", "headers", "json"}``.
    """

    responses: list = field(default_factory=list)
    requests: list = field(default_factory=list)

    def __call__(self, url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> TransportResult:
        req = {"url": url, "headers": dict(headers), "json": json.loads(body.decode("utf-8")), "timeout": timeout}
        self.requests.append(req)
        if not self.responses:
            raise AssertionError("FakeTransport ran out of scripted responses")
        item = self.responses.pop(0)
        if callable(item) and not isinstance(item, Exception):
            item = item(req)
        if isinstance(item, Exception):
            raise TransportError(type(item).__name__)
        status, payload = item
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return status, {"content-type": "application/json"}, payload


def completion(text: str, *, model: str = "fake/model", provider: str = "FakeProvider",
               prompt_tokens: int = 100, completion_tokens: int = 50) -> dict:
    """An OpenRouter-shaped chat completion body (helper for tests and fakes)."""
    return {
        "id": "gen-test",
        "object": "chat.completion",
        "model": model,
        "provider": provider,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }


# --------------------------------------------------------------------------------------
# Message helpers
# --------------------------------------------------------------------------------------


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def image_part(data: bytes, mime: str = "image/png") -> dict:
    """An image input as a data URL (OpenAI-compatible ``image_url`` content part)."""
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")}}


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: Optional[float] = None  # OpenRouter reports it when usage accounting is on; not used for anything

    def add(self, other: "Usage") -> "Usage":
        cost = None if self.cost is None and other.cost is None else (self.cost or 0.0) + (other.cost or 0.0)
        return Usage(self.prompt_tokens + other.prompt_tokens, self.completion_tokens + other.completion_tokens,
                     self.total_tokens + other.total_tokens, cost)

    def as_dict(self) -> dict:
        d = {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
             "total_tokens": self.total_tokens}
        if self.cost is not None:
            d["cost"] = self.cost
        return d


@dataclass
class ChatResult:
    text: str
    usage: Usage
    model: Optional[str]  # the model OpenRouter says answered (may differ from the requested slug)
    provider: Optional[str]  # the upstream provider OpenRouter routed to
    requested_model: str
    finish_reason: Optional[str] = None
    generation_id: Optional[str] = None
    attempts: int = 1

    def as_record(self) -> dict:
        return {"requested_model": self.requested_model, "model": self.model, "provider": self.provider,
                "finish_reason": self.finish_reason, "generation_id": self.generation_id,
                "attempts": self.attempts, "usage": self.usage.as_dict()}


# --------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------


class LLMClient:
    """OpenRouter chat client. ``api_key`` defaults to ``$OPENROUTER_API_KEY``."""

    def __init__(self, *, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 transport: Optional[Transport] = None, timeout: float = 120.0, max_retries: int = 3,
                 backoff_base: float = 1.0, backoff_max: float = 20.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        key = api_key if api_key is not None else os.environ.get(ENV_KEY, "")
        if not key or not key.strip():
            raise LLMConfigError(f"{ENV_KEY} is not set: an OpenRouter API key is needed for LLM calls")
        self.__key = key.strip()
        self.base_url = (base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        self.transport: Transport = transport or urllib_transport
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._sleep = sleep
        self._clock = clock

    def __repr__(self) -> str:  # never show the key
        return f"LLMClient(base_url={self.base_url!r})"

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + self.__key,
            "HTTP-Referer": APP_REFERER,
            "X-Title": APP_TITLE,
        }

    def _scrub(self, text: str) -> str:
        """Defence in depth: never let the key reach an exception or a log line, even if echoed back."""
        return text.replace(self.__key, "[redacted]") if self.__key else text

    def chat(self, model: str, messages: Sequence[Mapping[str, Any]], *, temperature: Optional[float] = None,
             seed: Optional[int] = None, max_tokens: Optional[int] = None,
             response_format: Optional[Union[str, Mapping[str, Any]]] = None, timeout: Optional[float] = None,
             deadline: Optional[float] = None, extra: Optional[Mapping[str, Any]] = None) -> ChatResult:
        """One chat completion. ``response_format="json"`` asks for a JSON object.

        ``deadline`` is a ``time.monotonic()`` instant: no attempt starts, and no wait runs, past it.
        """
        if not model:
            raise LLMConfigError("no model given")
        payload: dict[str, Any] = {"model": model, "messages": list(messages)}
        if temperature is not None:
            payload["temperature"] = temperature
        if seed is not None:
            payload["seed"] = seed
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        elif isinstance(response_format, Mapping):
            payload["response_format"] = dict(response_format)
        if extra:
            payload.update(extra)
        body = json.dumps(payload).encode("utf-8")
        url = self.base_url + "/chat/completions"

        last: Optional[LLMError] = None
        for attempt in range(1, self.max_retries + 2):
            per_call = timeout or self.timeout
            if deadline is not None:
                left = deadline - self._clock()
                if left <= 1.0:
                    raise last or LLMError("deadline", "no time left for an LLM call", retryable=False)
                per_call = min(per_call, left)
            try:
                status, headers, data = self.transport(url, self._headers(), body, per_call)
            except TransportError as exc:
                last = LLMError("network", self._scrub(f"network error talking to the LLM endpoint: {exc}"),
                                retryable=True)
                headers, status, data = {}, None, b""
            else:
                result, last = self._interpret(model, status, data)
                if result is not None:
                    result.attempts = attempt
                    log.info("llm call ok model=%s provider=%s attempt=%d prompt_tokens=%d completion_tokens=%d",
                             result.model, result.provider, attempt, result.usage.prompt_tokens,
                             result.usage.completion_tokens)
                    return result
            assert last is not None
            log.warning("llm call failed kind=%s status=%s attempt=%d retryable=%s", last.kind, last.status,
                        attempt, last.retryable)
            if not last.retryable or attempt > self.max_retries:
                raise last
            delay = self._backoff(attempt, headers)
            if deadline is not None and self._clock() + delay > deadline - 1.0:
                raise last
            self._sleep(delay)
        raise last  # pragma: no cover - loop always returns or raises

    def _backoff(self, attempt: int, headers: Mapping[str, str]) -> float:
        retry_after = {k.lower(): v for k, v in headers.items()}.get("retry-after")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), self.backoff_max))
            except ValueError:
                pass
        base = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_max)
        return base * (0.5 + random.random() / 2)

    def _interpret(self, model: str, status: int, data: bytes) -> tuple[Optional[ChatResult], Optional[LLMError]]:
        if len(data) > MAX_RESPONSE_BYTES:
            return None, LLMError("bad_response", "LLM response too large", status=status)
        try:
            obj = json.loads(data.decode("utf-8")) if data else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            obj = None
        if status != 200:
            msg = _error_message(obj)
            return None, LLMError("http", self._scrub(f"LLM endpoint returned HTTP {status}" + (f": {msg}" if msg else "")),
                                  status=status, retryable=status in RETRY_STATUSES)
        if not isinstance(obj, dict):
            return None, LLMError("bad_response", "LLM endpoint returned a body that is not a JSON object",
                                  status=status, retryable=True)
        if "error" in obj and not obj.get("choices"):
            # OpenRouter can answer 200 with an error object (for example a provider failure mid-stream).
            err = obj.get("error") or {}
            code = err.get("code") if isinstance(err, dict) else None
            retryable = isinstance(code, int) and code in RETRY_STATUSES
            return None, LLMError("provider", self._scrub(f"LLM provider error {code}: {_error_message(obj)}"),
                                  status=code if isinstance(code, int) else status, retryable=retryable)
        try:
            choice = obj["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None, LLMError("bad_response", "LLM reply has no choices[0].message.content", status=status,
                                  retryable=True)
        if isinstance(content, list):  # content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str):
            content = ""
        u = obj.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(u.get("prompt_tokens") or 0),
            completion_tokens=int(u.get("completion_tokens") or 0),
            total_tokens=int(u.get("total_tokens") or 0),
            cost=float(u["cost"]) if isinstance(u.get("cost"), (int, float)) else None,
        )
        return ChatResult(text=content, usage=usage, model=obj.get("model"), provider=obj.get("provider"),
                          requested_model=model, finish_reason=choice.get("finish_reason"),
                          generation_id=obj.get("id")), None


def _error_message(obj: Any) -> str:
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"][:300]
        if isinstance(err, str):
            return err[:300]
    return ""
