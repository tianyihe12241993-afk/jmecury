import bittensor as bt
import pytest
from bittensor.sp_core import Keypair

from jmercury.brief import generate
from jmercury.protocol import SITE_TASK_PATH, SiteTask, sign_request, verify_request


def test_bittensor_version_pinned():
    assert bt.__version__ == "11.3.2"


def test_sitetask_roundtrip():
    brief = generate(3).model_dump(mode="json")
    req = SiteTask(brief=brief)
    again = SiteTask.from_body(req.to_body())
    assert again == req and again.bundle_b64 is None
    resp = again.model_copy(update={"bundle_b64": "UEsFBgAAAAAAAAAAAAAAAAAAAAAAAA==", "miner_version": "ref-0.1"})
    assert SiteTask.from_body(resp.to_body()) == resp


def test_sitetask_rejects_unknown_fields():
    with pytest.raises(Exception):
        SiteTask.from_body(b'{"brief": {}, "evil": 1}')


def test_signed_request_roundtrip():
    # well-known public development keys (//Alice, //Bob), not secrets
    validator, miner = Keypair.create_from_uri("//Alice"), Keypair.create_from_uri("//Bob")
    body = SiteTask(brief=generate(5).model_dump(mode="json")).to_body()
    headers = sign_request(validator, body, miner.ss58_address)
    caller = verify_request(headers, body, miner.ss58_address, expected_crypto_type=bt.CRYPTO_SR25519,
                            nonce_store=bt.http_auth.InMemoryNonceStore())
    assert caller.hotkey_ss58 == validator.ss58_address
    assert SiteTask.from_body(body).brief["task_id"] == generate(5).task_id


def test_tampered_body_and_wrong_receiver_rejected():
    validator, miner, other = (Keypair.create_from_uri(u) for u in ("//Alice", "//Bob", "//Charlie"))
    body = SiteTask(brief={"x": 1}).to_body()
    headers = sign_request(validator, body, miner.ss58_address)
    with pytest.raises(bt.http_auth.BadSignature):
        verify_request(headers, body.replace(b"1", b"2"), miner.ss58_address, expected_crypto_type=bt.CRYPTO_SR25519,
                       nonce_store=bt.http_auth.InMemoryNonceStore())
    with pytest.raises(bt.http_auth.WrongReceiver):
        verify_request(headers, body, other.ss58_address, expected_crypto_type=bt.CRYPTO_SR25519,
                       nonce_store=bt.http_auth.InMemoryNonceStore())
    assert SITE_TASK_PATH.startswith("/")
