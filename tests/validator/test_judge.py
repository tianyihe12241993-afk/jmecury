import json

import pytest

from jmercury.llm import FakeTransport, LLMClient, completion
from jmercury.validator.judge import (CRITERIA, FakeJudge, JudgeConfigError, OpenRouterJudge, SiteView, build_messages,
                                      judge_pair, load_rubric, parse_reply, reply)

PNG = b"\x89PNG\r\n\x1a\nfake"


def view(text):
    return SiteView(visible_text=text, mobile_first_png=PNG, mobile_full_png=PNG, desktop_first_png=PNG)


X, Y = view("site X text"), view("site Y text")


def test_wellformed():
    v = parse_reply(reply("A", invented_b=["5-star rating"]))
    assert v.valid and v.winner == "A" and v.invented == {"A": [], "B": ["5-star rating"]}


GOOD = json.loads(reply("A"))


def mutate(**kw):
    d = json.loads(json.dumps(GOOD))
    for k, v in kw.items():
        if v is KeyError:
            d.pop(k)
        else:
            d[k] = v
    return json.dumps(d)


@pytest.mark.parametrize("raw", [
    None,
    "",
    "not json",
    "Here is my verdict: " + reply("A"),
    "```\n" + reply("A") + "\n```",  # a fence must say json
    "```python\n" + reply("A") + "\n```",
    "Verdict:\n```json\n" + reply("A") + "\n```",  # text before the fence
    "```json\n" + reply("A") + "\n```\nThanks!",  # text after the fence
    "```json\n" + reply("A") + "\n```\n```json\n" + reply("B") + "\n```",  # two fences
    "```json\n" + reply("A"),  # unclosed fence
    "```json " + reply("A") + "```",  # not a fenced block (no newlines)
    reply("A") + " trailing",
    "[1, 2]",
    mutate(winner="a"),
    mutate(winner="Site A"),
    mutate(winner=KeyError),
    mutate(reason=KeyError),
    mutate(reason=5),
    mutate(extra="x"),
    mutate(criteria={c: "A" for c in CRITERIA[:-1]}),
    mutate(criteria={**{c: "A" for c in CRITERIA}, "speed": "A"}),
    mutate(criteria={**{c: "A" for c in CRITERIA}, "fit": "both"}),
    mutate(invented_claims={"A": []}),
    mutate(invented_claims={"A": "none", "B": []}),
    mutate(invented_claims={"A": [1], "B": []}),
    mutate(invented_claims=[]),
])
def test_malformed_is_tie(raw):
    v = parse_reply(raw)
    assert not v.valid and v.winner == "tie" and v.invented == {"A": [], "B": []}


def test_whitespace_around_json_ok():
    assert parse_reply("\n  " + reply("B") + "\n").valid


def test_both_orders_agree_win():
    j = FakeJudge([reply("A"), reply("B")])  # order1: x=A wins; order2: y=A, x=B wins
    pr = judge_pair(j, "{}", "x", X, "y", Y)
    assert pr.winner == "x"
    assert j.calls == [("site X text", "site Y text"), ("site Y text", "site X text")]


def test_position_bias_is_tie():
    pr = judge_pair(FakeJudge([reply("A"), reply("A")]), "{}", "x", X, "y", Y)
    assert pr.winner is None


@pytest.mark.parametrize("r1,r2", [(reply("A"), reply("tie")), (reply("tie"), reply("tie")), (reply("A"), "garbage"),
                                   ("garbage", "garbage")])
def test_disagreement_or_malformed_is_tie(r1, r2):
    assert judge_pair(FakeJudge([r1, r2]), "{}", "x", X, "y", Y).winner is None


def test_invented_one_order_only_not_confirmed():
    pr = judge_pair(FakeJudge([reply("A", invented_a=["award"]), reply("B")]), "{}", "x", X, "y", Y)
    assert pr.invented_confirmed == {"x": False, "y": False}
    assert pr.winner == "x"


def test_invented_both_orders_confirmed():
    # x is A in order 1 and B in order 2
    pr = judge_pair(FakeJudge([reply("A", invented_a=["award"]), reply("B", invented_b=["since 1985"])]),
                    "{}", "x", X, "y", Y)
    assert pr.invented_confirmed == {"x": True, "y": False}


def test_invented_label_must_follow_the_site():
    # order 2 flags A, which is y, not x: x is not confirmed
    pr = judge_pair(FakeJudge([reply("A", invented_a=["award"]), reply("B", invented_a=["x"])]), "{}", "x", X, "y", Y)
    assert pr.invented_confirmed == {"x": False, "y": False}


def test_blank_claims_do_not_flag():
    pr = judge_pair(FakeJudge([reply("A", invented_a=["  "]), reply("B", invented_b=[""])]), "{}", "x", X, "y", Y)
    assert pr.invented_confirmed["x"] is False


def test_call_failure_is_tie():
    def boom(*_):
        raise RuntimeError("endpoint down")

    pr = judge_pair(FakeJudge(fn=boom), "{}", "x", X, "y", Y)
    assert pr.winner is None and len(pr.errors) == 2


def test_fenced_json_block_is_accepted():
    for raw in ("```json\n" + reply("B") + "\n```", "\n ```json\n" + reply("B") + "\n```\n", "```json\r\n" + reply("B") + "\r\n```"):
        v = parse_reply(raw)
        assert v.valid and v.winner == "B", raw


def test_prompt_contains_rubric_and_six_images():
    msgs = build_messages('{"name":"b"}', X, Y)
    assert load_rubric() in msgs[0]["content"]
    parts = msgs[1]["content"]
    assert sum(p["type"] == "image_url" for p in parts) == 6
    assert all(p["image_url"]["url"].startswith("data:image/png;base64,") for p in parts if p["type"] == "image_url")
    texts = "\n".join(p.get("text", "") for p in parts)
    assert "site X text" in texts and "site Y text" in texts and texts.index("SITE A") < texts.index("SITE B")


def test_full_page_parts_are_sent_in_order():
    a = SiteView("a", PNG, PNG + b"whole", PNG, mobile_full_parts=[PNG + b"1", PNG + b"2", PNG + b"3"])
    parts = build_messages("{}", a, Y)[1]["content"]
    assert sum(p["type"] == "image_url" for p in parts) == 8  # A: 1 + 3 parts + 1, B: 1 + 1 + 1
    texts = [p.get("text", "") for p in parts]
    assert "SITE A: phone full page, part 1 of 3, top to bottom" in texts
    assert "SITE A: phone full page, part 3 of 3, top to bottom" in texts
    assert "SITE B: phone full page" in texts


def judge_env(monkeypatch, model="judge/model-x"):
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key-123")
    if model is None:
        monkeypatch.delenv("JM_JUDGE_MODEL", raising=False)
    else:
        monkeypatch.setenv("JM_JUDGE_MODEL", model)


def test_judge_needs_model(monkeypatch):
    judge_env(monkeypatch, model=None)
    with pytest.raises(JudgeConfigError) as ei:
        OpenRouterJudge()
    assert "JM_JUDGE_MODEL is not set" in str(ei.value) and "no default judge model" in str(ei.value)


def test_judge_needs_key(monkeypatch):
    judge_env(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(JudgeConfigError) as ei:
        OpenRouterJudge()
    assert "OPENROUTER_API_KEY" in str(ei.value)


def test_judge_via_fake_transport(monkeypatch):
    judge_env(monkeypatch)
    t = FakeTransport([
        (200, completion("```json\n" + reply("A") + "\n```", model="judge/model-x-0501", provider="P1",
                         prompt_tokens=7000, completion_tokens=120)),
        (200, completion(reply("B"), model="judge/model-x-0501", provider="P1")),
    ])
    j = OpenRouterJudge(client=LLMClient(transport=t, sleep=lambda s: None))
    pr = judge_pair(j, "{}", "x", X, "y", Y)
    assert pr.winner == "x" and not pr.errors
    for r in t.requests:
        assert r["json"]["model"] == "judge/model-x" and r["json"]["temperature"] == 0.0
        assert r["headers"]["X-Title"] == "J Mercury subnet" and r["headers"]["HTTP-Referer"] == "https://jmecury.org"
    rec = j.record()
    assert rec["calls"] == 2 and rec["usage"]["prompt_tokens"] == 7100 and rec["answered_by"] == ["judge/model-x-0501 via P1"]
    assert "not-a-real-key-123" not in repr(j) + str(rec)


@pytest.mark.parametrize("junk", ["I prefer A.", "```json\n{\"winner\": \"A\"}\n```", "", "<json>" + reply("A") + "</json>"])
def test_judge_junk_reply_is_tie(monkeypatch, junk):
    judge_env(monkeypatch)
    t = FakeTransport([(200, completion(junk)), (200, completion(reply("B")))])
    pr = judge_pair(OpenRouterJudge(client=LLMClient(transport=t, sleep=lambda s: None)), "{}", "x", X, "y", Y)
    assert pr.winner is None and not pr.order1.valid and pr.order2.valid


def test_judge_http_failure_is_tie_and_hides_key(monkeypatch):
    judge_env(monkeypatch)
    t = FakeTransport([(401, {"error": {"message": "bad key not-a-real-key-123"}})] * 2)
    pr = judge_pair(OpenRouterJudge(client=LLMClient(transport=t, sleep=lambda s: None)), "{}", "x", X, "y", Y)
    assert pr.winner is None and len(pr.errors) == 2
    assert all("not-a-real-key-123" not in e for e in pr.errors) and "HTTP 401" in pr.errors[0]
