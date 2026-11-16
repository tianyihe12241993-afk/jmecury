import json

import pytest

from jmercury.judge import (CRITERIA, FakeJudge, JudgeConfigError, OpenAIJudge, SiteView, build_messages, judge_pair,
                            load_rubric, parse_reply, reply)

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
    "```json\n" + reply("A") + "\n```",
    "Here is my verdict: " + reply("A"),
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


def test_prompt_contains_rubric_and_six_images():
    msgs = build_messages('{"name":"b"}', X, Y)
    assert load_rubric() in msgs[0]["content"]
    parts = msgs[1]["content"]
    assert sum(p["type"] == "image_url" for p in parts) == 6
    texts = "\n".join(p.get("text", "") for p in parts)
    assert "site X text" in texts and "site Y text" in texts and texts.index("SITE A") < texts.index("SITE B")


def test_openai_judge_needs_env(monkeypatch):
    for v in ("JM_JUDGE_BASE_URL", "JM_JUDGE_MODEL", "JM_JUDGE_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(JudgeConfigError):
        OpenAIJudge()


def test_openai_judge_repr_hides_key(monkeypatch):
    monkeypatch.setenv("JM_JUDGE_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("JM_JUDGE_MODEL", "fake-model")
    monkeypatch.setenv("JM_JUDGE_API_KEY", "not-a-real-key-123")
    j = OpenAIJudge(timeout=1)
    assert "not-a-real-key-123" not in repr(j) and "not-a-real-key-123" not in str(vars(j).get("base_url"))
    # port 9 on loopback refuses: the error must not carry the key either
    with pytest.raises(RuntimeError) as ei:
        j.compare("{}", X, Y)
    assert "not-a-real-key-123" not in str(ei.value)
