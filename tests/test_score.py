import json
import subprocess
import sys

import pytest

from jmercury.bundle import pack_dir
from jmercury.judge import FakeJudge, reply
from jmercury.score import score_round, score_site

from .conftest import EXAMPLE_DIR, REPO, facts_html, site_b64

pytestmark = pytest.mark.browser

VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1">'


def page(brief, marker):
    return facts_html(brief, body_extra=f"<p>{marker}</p>").replace("<head>", f"<head>{VIEWPORT}")


def ranking_judge():
    def rank(text):
        for i in range(1, 9):
            if f"RANK{i}" in text:
                return i
        return 99

    def fn(_brief, a, b):
        ra, rb = rank(a.visible_text), rank(b.visible_text)
        winner = "A" if ra < rb else "B" if rb < ra else "tie"
        return reply(winner,
                     invented_a=["award"] if "INVENTED" in a.visible_text else [],
                     invented_b=["award"] if "INVENTED" in b.visible_text else [])

    return FakeJudge(fn=fn)


def test_score_round_with_fake_judge(brief42, tmp_path):
    good_a = page(brief42, "RANK1 first build")
    responses = {
        "a": (site_b64(good_a), 1.0),
        "b": (site_b64(page(brief42, "RANK2 second build")), 2.0),
        "c": (site_b64(good_a.replace("<p>RANK1", "<p>  RANK1")), 0.5),  # same visible text, answered earlier
        "d": (site_b64(page(brief42, "RANK1 <script>alert(1)</script>")), 0.1),
        "e": (site_b64(page(brief42, "RANK3 INVENTED")), 3.0),
    }
    pre = {u: score_site(brief42, b64, out_dir=tmp_path / u, do_measure=False) for u, (b64, _) in responses.items()}
    for s in pre.values():
        if s["gates_passed"]:
            s["M"] = 0.8
    judge = ranking_judge()
    rec = score_round(brief42, responses, judge, out_dir=tmp_path / "round", site_results=pre)
    m = rec["miners"]

    assert m["d"]["zero_reason"] == "gate_failed" and m["d"]["task_score"] == 0
    assert m["a"]["duplicate_of"] == "c" and m["a"]["task_score"] == 0 and m["a"]["zero_reason"] == "duplicate"
    assert m["c"]["duplicate_of"] is None
    assert m["e"]["zero_reason"] == "invented_claims" and m["e"]["task_score"] == 0
    assert m["c"]["J"] == 1.0 and m["b"]["J"] == 0.5
    assert m["c"]["task_score"] == pytest.approx(0.4 * 0.8 + 0.6 * 1.0)
    assert m["b"]["task_score"] == pytest.approx(0.4 * 0.8 + 0.6 * 0.5)
    # only gate-passing, non-duplicate sites are judged: 3 pairs x 2 orders
    assert len(rec["pairs"]) == 3 and len(judge.calls) == 6
    assert {frozenset((p["x"], p["y"])) for p in rec["pairs"]} == {frozenset(s) for s in (("b", "c"), ("b", "e"), ("c", "e"))}
    json.dumps(rec)  # the round record is plain JSON
    assert rec["brief"]["task_id"] == brief42.task_id
    assert all(m[u]["bundle_sha256"] for u in m)


def test_duplicate_tie_on_time_goes_to_uid_order(brief42, tmp_path):
    b64 = site_b64(page(brief42, "same"))
    pre = {u: score_site(brief42, b64, out_dir=tmp_path / u, do_measure=False) for u in ("u2", "u1")}
    for s in pre.values():
        s["M"] = 0.5
    rec = score_round(brief42, {"u2": (b64, 1.0), "u1": (b64, 1.0)}, ranking_judge(), site_results=pre, out_dir=tmp_path / "r")
    assert rec["miners"]["u2"]["duplicate_of"] == "u1"
    assert rec["miners"]["u1"]["J"] == 0.5  # alone after the duplicate is removed: neutral J
    assert rec["miners"]["u1"]["comparisons"] == 0


def test_no_judge_leaves_scores_open(brief42, tmp_path):
    b64 = site_b64(page(brief42, "x"))
    rec = score_round(brief42, {"1": (b64, 1.0)}, None, out_dir=tmp_path, do_measure=False)
    assert rec["miners"]["1"]["J"] is None and rec["miners"]["1"]["task_score"] is None


def test_bundle_rejection_in_score_site(brief42, tmp_path):
    res = score_site(brief42, "not base64", out_dir=tmp_path, do_measure=False)
    assert not res["gates_passed"]
    assert res["gates"]["bundle"]["reasons"][0]["code"] == "not_base64"
    assert res["gates"]["facts"]["reasons"][0]["code"] == "not_run"


def test_render_backstop_fails_gate2(brief42, tmp_path):
    # the request blocked during rendering is recorded as a second, independent gate-2 failure
    html = page(brief42, '<img src="https://evil.example/x.png" alt="x">')
    res = score_site(brief42, site_b64(html), out_dir=tmp_path, do_measure=False)
    codes = {r["code"] for r in res["gates"]["no_script"]["reasons"]}
    assert {"external_resource", "external_request_at_render"} <= codes


def test_example_passes_all_gates(example_brief, tmp_path):
    res = score_site(example_brief, pack_dir(EXAMPLE_DIR / "site"), out_dir=tmp_path, do_measure=False)
    assert res["gates_passed"], res["gates"]
    assert res["render"]["no_hscroll"] and res["render"]["tel_visible"]


def test_cli_judge_without_env_is_clear(tmp_path):
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("JM_JUDGE_")}
    out = subprocess.run([sys.executable, "-m", "jmercury.score", "--brief", str(EXAMPLE_DIR / "brief.json"),
                          "--site", str(EXAMPLE_DIR / "site"), "--vs", str(EXAMPLE_DIR / "site"), "--judge"],
                         capture_output=True, text=True, cwd=REPO, env=env)
    assert out.returncode == 2
    assert "JM_JUDGE_BASE_URL" in out.stderr and "JM_JUDGE_API_KEY" in out.stderr


@pytest.mark.lighthouse
def test_cli_end_to_end_with_lighthouse(tmp_path):
    out = subprocess.run([sys.executable, "-m", "jmercury.score", "--brief", str(EXAMPLE_DIR / "brief.json"),
                          "--site", str(EXAMPLE_DIR / "site"), "--runs", "1", "--out", str(tmp_path)],
                         capture_output=True, text=True, cwd=REPO, timeout=600)
    assert out.returncode == 0, out.stderr[-2000:]
    res = json.loads(out.stdout)
    assert res["gates_passed"]
    assert res["M"] is not None and 0.0 <= res["M"] <= 1.0
    assert set(res["measured"]["lighthouse_median"]) == {"performance", "accessibility", "best-practices", "seo"}
    assert (tmp_path / "result.json").exists() and (tmp_path / "mobile_first.png").exists()


def test_invented_claims_need_half_of_the_pairs(brief42, tmp_path):
    # Each site carries IDx; a site is flagged against opponent y when its own text says FLAG-y.
    import re

    def ident(text):
        return re.search(r"ID([A-Z])", text).group(1)

    def fn(_brief, a, b):
        ra, rb = ident(a.visible_text), ident(b.visible_text)
        return reply("A" if ra < rb else "B",
                     invented_a=["award"] if f"FLAG-{rb}" in a.visible_text else [],
                     invented_b=["award"] if f"FLAG-{ra}" in b.visible_text else [])

    texts = {"p": "IDP plain", "q": "IDQ plain", "r": "IDR FLAG-Q", "s": "IDS FLAG-P FLAG-Q"}
    responses = {u: (site_b64(page(brief42, t)), 1.0 + i) for i, (u, t) in enumerate(texts.items())}
    pre = {u: score_site(brief42, b64, out_dir=tmp_path / u, do_measure=False) for u, (b64, _) in responses.items()}
    for s in pre.values():
        assert s["gates_passed"]
        s["M"] = 0.8
    m = score_round(brief42, responses, FakeJudge(fn=fn), site_results=pre, out_dir=tmp_path / "r")["miners"]
    assert len(m["r"]["invented_claims_confirmed_in_pairs"]) == 1 and m["r"]["zero_reason"] is None  # 1 of 3 pairs
    assert m["r"]["task_score"] > 0
    assert len(m["s"]["invented_claims_confirmed_in_pairs"]) == 2 and m["s"]["zero_reason"] == "invented_claims"  # 2 of 3
    assert m["s"]["task_score"] == 0
