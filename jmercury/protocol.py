"""Wire protocol between validator and miner.

bittensor 11.x has no Synapse/Axon/Dendrite stack (``bittensor.Synapse`` raises an
AttributeError that points to ``bittensor.http_auth``). So ``SiteTask`` is a plain pydantic
model sent as a JSON body over HTTP, and requests are authenticated with the ``btauth/1``
headers from ``bittensor.http_auth``:

    validator: body = SiteTask(brief=...).to_body(); headers = sign_request(wallet, body, miner_hotkey)
               POST {miner_endpoint}{SITE_TASK_PATH} with body + headers
    miner:     caller = verify_request(headers, body, my_hotkey, expected_crypto_type=...)
               reply with SiteTask(..., bundle_b64=..., miner_version=...).to_body()

This is the only module that imports bittensor.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

import bittensor as bt
from pydantic import BaseModel, ConfigDict

SITE_TASK_PATH = "/v1/site-task"
PROTOCOL_VERSION = 1


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
