"""End to end on 127.0.0.1: validator client -> three reference miner servers (fake LLM) -> bundles ->
score_site (all gates) -> score_round with a judge. No network beyond loopback, no API key, no real LLM."""

import json
import struct

import pytest

from jmercury.bundle import unpack
from jmercury.llm import FakeTransport, LLMClient, completion
from jmercury.miner.generate import generate_site
from jmercury.miner.server import MinerServer
from jmercury.protocol import hotkey_ss58, keypair_from_uri
from jmercury.validator.client import query_miners
from jmercury.validator.judge import FakeJudge, OpenRouterJudge, reply
from jmercury.validator.score import score_round, score_site

from ..conftest import EXAMPLE_DIR

pytestmark = [pytest.mark.browser, pytest.mark.e2e]

KEY = "sk-or-test-not-a-real-key"
VALIDATOR = keypair_from_uri("//Alice")
MINERS = {"1": keypair_from_uri("//Bob"), "2": keypair_from_uri("//Charlie"), "3": keypair_from_uri("//Dave")}


def answer(html: str) -> str:
    return f"<<<FILE index.html>>>\n{html}\n<<<END FILE>>>"


def variant(rank: int) -> str:
    html = (EXAMPLE_DIR / "site" / "index.html").read_text(encoding="utf-8")
    return html.replace("</main>", f"<p>Build marker RANK{rank}.</p></main>", 1)


def rank_of(text: str) -> int:
    for i in range(1, 4):
        if f"RANK{i}" in text:
            return i
    return 99


def miner_gen(texts: list[str], log: list):
    transport = FakeTransport([(200, completion(t, prompt_tokens=2500, completion_tokens=6000)) for t in texts])
    client = LLMClient(api_key=KEY, transport=transport, sleep=lambda s: None)

    def run(brief, deadline):
        res = generate_site(brief, client=client, model="fake/miner", check="validator", deadline=deadline)
        log.append(res)
        return res.bundle_b64

    return run


@pytest.fixture(scope="module")
def round_responses(example_brief):
    logs = {uid: [] for uid in MINERS}
    bad_first = variant(3).replace("</head>", '<link rel="stylesheet" href="https://fonts.example.com/x.css"></head>')
    scripts = {"1": [answer(variant(1))], "2": [answer(variant(2))], "3": [answer(bad_first), answer(variant(3))]}
    allow = [hotkey_ss58(VALIDATOR)]
    servers = {uid: MinerServer(kp, allow, miner_gen(scripts[uid], logs[uid])).start() for uid, kp in MINERS.items()}
    try:
        targets = [(servers[uid].url, hotkey_ss58(MINERS[uid])) for uid in sorted(MINERS)]
        results = query_miners(VALIDATOR, targets, example_brief)
    finally:
        for s in servers.values():
            s.close()
    return results, logs


def test_miners_answer_with_signed_bundles(round_responses):
    results, logs = round_responses
    assert [r.ok for r in results] == [True, True, True], [r.as_dict() for r in results]
    assert all(r.miner_version.startswith("jmercury-ref-miner/") for r in results)
    # miner 3 failed its own validator-gate check once (external font) and fixed it on the retry
    assert len(logs["3"][0].attempts) == 2 and len(logs["1"][0].attempts) == 1
    assert logs["3"][0].usage.total_tokens == 2 * 8500
    for r in results:
        assert "fonts.example.com" not in unpack(r.bundle_b64).text("index.html")


def test_bundles_pass_every_gate_and_the_round_scores(example_brief, round_responses, tmp_path):
    results, _ = round_responses
    responses = {uid: (r.bundle_b64, r.response_time) for uid, r in zip(sorted(MINERS), results)}
    pre = {}
    for uid, (b64, _) in responses.items():
        s = score_site(example_brief, b64, out_dir=tmp_path / uid, do_measure=False)
        assert s["gates_passed"], {g: v["reasons"] for g, v in s["gates"].items() if not v["passed"]}
        assert s["render"]["no_hscroll"] and s["render"]["tel_visible"]
        s["M"] = 0.8  # Lighthouse is exercised in tests/validator/test_measure.py
        pre[uid] = s

    def fn(_brief, a, b):
        ra, rb = rank_of(a.visible_text), rank_of(b.visible_text)
        return reply("A" if ra < rb else "B" if rb < ra else "tie")

    judge = FakeJudge(fn=fn)
    rec = score_round(example_brief, responses, judge, out_dir=tmp_path / "round", site_results=pre)
    m = rec["miners"]
    assert [m[u]["J"] for u in ("1", "2", "3")] == [1.0, 0.5, 0.0]
    assert m["1"]["task_score"] == pytest.approx(0.4 * 0.8 + 0.6 * 1.0)
    assert m["3"]["task_score"] == pytest.approx(0.4 * 0.8)
    assert len(rec["pairs"]) == 3 and len(judge.calls) == 6
    json.dumps(rec)


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_round_with_the_openrouter_judge_on_a_fake_transport(example_brief, round_responses, tmp_path):
    """The real judge code path (messages, images, parsing) with a scripted OpenRouter answer."""
    import base64

    results, _ = round_responses
    responses = {uid: (r.bundle_b64, r.response_time) for uid, r in zip(sorted(MINERS), results)}
    pre = {uid: score_site(example_brief, b64, out_dir=tmp_path / uid, do_measure=False)
           for uid, (b64, _) in responses.items()}
    for s in pre.values():
        s["M"] = 0.8
    calls = []

    def scripted(req):
        content = req["json"]["messages"][1]["content"]
        texts = {p["text"].split("\n", 1)[0]: p["text"] for p in content if p["type"] == "text"}
        ra = rank_of(texts["SITE A: visible text"])
        rb = rank_of(texts["SITE B: visible text"])
        images = [p["image_url"]["url"] for p in content if p["type"] == "image_url"]
        sizes = [_png_size(base64.b64decode(u.split(",", 1)[1])) for u in images]
        calls.append({"temperature": req["json"]["temperature"], "sizes": sizes})
        body = "```json\n" + reply("A" if ra < rb else "B") + "\n```"  # one fenced block is accepted
        return 200, completion(body, prompt_tokens=9000, completion_tokens=300)

    judge = OpenRouterJudge(LLMClient(api_key=KEY, transport=FakeTransport([scripted] * 6)), model="fake/judge")
    rec = score_round(example_brief, responses, judge, out_dir=tmp_path / "round", site_results=pre)
    assert [rec["miners"][u]["J"] for u in ("1", "2", "3")] == [1.0, 0.5, 0.0]
    assert len(calls) == 6 and all(c["temperature"] == 0.0 for c in calls)
    for c in calls:
        assert 6 <= len(c["sizes"]) <= 10
        assert all(max(w, h) <= 1568 for w, h in c["sizes"]), c["sizes"]
    assert rec["judge"]["usage"]["total_tokens"] == 6 * 9300
    assert KEY not in json.dumps(rec)
