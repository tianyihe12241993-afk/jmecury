"""Wire protocol between validator and miner.

bittensor 11.x has no Synapse/Axon/Dendrite stack (``bittensor.Synapse`` raises an
AttributeError that points to ``bittensor.http_auth``). So ``SiteTask`` is a plain pydantic
model sent as a JSON body over HTTP, and requests are authenticated with the ``btauth/1``
headers from ``bittensor.http_auth``:

    validator: body = SiteTask(brief=...).to_body(); headers = sign_request(wallet, body, miner_hotkey)
               POST {miner_endpoint}{SITE_TASK_PATH} with body + headers
    miner:     caller = verify_request(headers, body, my_hotkey, expected_crypto_type=...)
               reply = SiteTask(..., bundle_b64=..., miner_version=...).to_body()
               reply headers = sign_response(wallet, reply, caller.hotkey_ss58, request_nonce=caller.nonce_ns)
    validator: verify_response(reply_headers, reply, my_hotkey, miner_hotkey, request_nonce=...)

The answer is signed too, so the validator can check which hotkey answered. A response signature uses the
pseudo-method ``RESPONSE`` and the target ``{SITE_TASK_PATH}#<request nonce>``: it cannot be mistaken for a
request, and it is bound to the one request it answers, so an old answer cannot be replayed to a new request.

This is the only module that imports bittensor.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

import bittensor as bt
from pydantic import BaseModel, ConfigDict

SITE_TASK_PATH = "/v1/site-task"
PROTOCOL_VERSION = 1
RESPONSE_METHOD = "RESPONSE"
CRYPTO_SR25519 = bt.CRYPTO_SR25519
HEADER_NONCE = bt.http_auth.HEADER_NONCE
HEADER_HOTKEY = bt.http_auth.HEADER_HOTKEY
AuthError = bt.http_auth.AuthError
MalformedAuth = bt.http_auth.MalformedAuth
BadSignature = bt.http_auth.BadSignature
WrongReceiver = bt.http_auth.WrongReceiver
StaleRequest = bt.http_auth.StaleRequest
ReplayedRequest = bt.http_auth.ReplayedRequest
InMemoryNonceStore = bt.http_auth.InMemoryNonceStore


class SiteTask(BaseModel):
    """Brief in (validator -> miner); bundle out (miner -> validator)."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: int = PROTOCOL_VERSION
    brief: dict[str, Any]
    bundle_b64: Optional[str] = None
    miner_version: Optional[str] = None

    def to_body(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_body(cls, body: bytes) -> "SiteTask":
        return cls.model_validate_json(body)


def sign_request(wallet: Any, body: bytes, receiver_ss58: str, *, path: str = SITE_TASK_PATH,
                 nonce_ns: Optional[int] = None) -> dict[str, str]:
    """btauth/1 headers for a POST of ``body`` to ``path``, bound to the receiving hotkey."""
    return bt.http_auth.sign(wallet, method="POST", path=path, body=body, receiver_ss58=receiver_ss58, nonce_ns=nonce_ns)


def verify_request(headers: Mapping[str, str], body: bytes, self_hotkey_ss58: str, *, path: str = SITE_TASK_PATH,
                   expected_crypto_type: Optional[int] = None, **kwargs: Any):
    """Authenticate a received request; returns ``bittensor.http_auth.Caller`` or raises ``AuthError``."""
    return bt.http_auth.verify(headers, body, method="POST", path=path, self_hotkey_ss58=self_hotkey_ss58,
                               expected_crypto_type=expected_crypto_type, **kwargs)


def hotkey_ss58(wallet: Any) -> str:
    """SS58 address of the hotkey that ``wallet`` (a ``bittensor.Wallet`` or a keypair) signs with."""
    return bt.resolve_signer(wallet, role="hotkey").ss58_address


def load_wallet(name: str, hotkey: str, path: Optional[str] = None) -> Any:
    """A ``bittensor.Wallet``; ``path`` defaults to bittensor's own default wallet directory."""
    return bt.Wallet(name=name, hotkey=hotkey, path=path) if path else bt.Wallet(name=name, hotkey=hotkey)


def keypair_from_uri(uri: str) -> Any:
    """A keypair from a derivation URI such as ``//Alice`` (public development keys; tests and local runs only)."""
    return bt.sp_core.Keypair.create_from_uri(uri)


def _response_path(request_nonce: int) -> str:
    return f"{SITE_TASK_PATH}#{int(request_nonce)}"


def sign_response(wallet: Any, body: bytes, receiver_ss58: str, *, request_nonce: int,
                  nonce_ns: Optional[int] = None) -> dict[str, str]:
    """btauth/1 headers for a miner's answer ``body``, bound to the validator hotkey and to the request nonce."""
    return bt.http_auth.sign(wallet, method=RESPONSE_METHOD, path=_response_path(request_nonce), body=body,
                             receiver_ss58=receiver_ss58, nonce_ns=nonce_ns)


def verify_response(headers: Mapping[str, str], body: bytes, self_hotkey_ss58: str, expected_sender_ss58: str, *,
                    request_nonce: int, expected_crypto_type: int = CRYPTO_SR25519, **kwargs: Any):
    """Check that ``body`` was signed by ``expected_sender_ss58`` for this validator and this request.

    Raises ``AuthError`` (``BadSignature`` when a different hotkey signed it).
    """
    caller = bt.http_auth.verify(headers, body, method=RESPONSE_METHOD, path=_response_path(request_nonce),
                                 self_hotkey_ss58=self_hotkey_ss58, expected_crypto_type=expected_crypto_type,
                                 **kwargs)
    if caller.hotkey_ss58 != expected_sender_ss58:
        raise BadSignature("the response was signed by a different hotkey than the miner that was asked")
    return caller
