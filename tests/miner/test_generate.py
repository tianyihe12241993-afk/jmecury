import json
import subprocess
import sys
import textwrap
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jmercury.brief import TRADES, Brief, generate
from jmercury.bundle import pack_files, unpack
from jmercury.llm import FakeTransport, LLMClient, completion
from jmercury.miner import generate as gen
from jmercury.miner.generate import (
    SCHEMA_TYPE,
    GenerationError,
    check_files,
    generate_site,
    honesty_lint,
    parse_output,
    postprocess,
    static_visible_text,
)
from jmercury.validator.gates import ALLOWED_TYPES, gate_facts, gate_no_reviews, gate_no_script

from ..conftest import EXAMPLE_DIR, REPO

KEY = "sk-or-test-not-a-real-key"


def files_answer(index_html, **extra):
    out = f"<<<FILE index.html>>>\n{index_html}\n<<<END FILE>>>\n"
    for name, text in extra.items():
        out += f"<<<FILE {name.replace('_', '.')}>>>\n{text}\n<<<END FILE>>>\n"
    return out


def fake_client(*texts, finish_reason="stop"):
    responses = []
    for i, t in enumerate(texts):
        if isinstance(t, tuple):
            responses.append(t)
        else:
            body = completion(t, prompt_tokens=1000 + i, completion_tokens=500 + i)
            body["choices"][0]["finish_reason"] = finish_reason
            responses.append((200, body))
    transport = FakeTransport(responses)
    return LLMClient(api_key=KEY, transport=transport, sleep=lambda s: None, max_retries=1), transport


@pytest.fixture(scope="module")
def good_html():
    return (EXAMPLE_DIR / "site" / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def good_svg():
    return (EXAMPLE_DIR / "site" / "favicon.svg").read_text(encoding="utf-8")


def bad_page(brief: Brief) -> str:
    """Breaks rules the miner fixes itself, one it cannot fix (an external font) and the honesty rule."""
    hours = "".join(f"<li>{d}: {s[0]} to {s[1]}</li>" for d, s in brief.hours.items() if s)
    return (
        "<html><head><title>x</title>"
        '<link rel="stylesheet" href="https://fonts.example.com/css?family=Fancy">'
        "<script>document.title='hi'</script>"
        '<script type="application/ld+json">{"@type":"LocalBusiness","name":"Wrong"}</script>'
        "</head><body>"
        f'<h1 onclick="alert(1)">{brief.name}</h1><p>Call {brief.phone}</p>'
        '<a href="javascript:void(0)">menu</a>'
        f"<ul>{hours}</ul>"
        "<section><h2>What our customers say</h2>"
        "<blockquote>\"They did a wonderful job and showed up right on time.\" - Martha</blockquote>"
        "<p>Rated 4.9 stars from 212 reviews</p></section>"
        "<script src=\"https://cdn.example.com/x.js\"></script>"
        "</body></html>"
    )


# --------------------------------------------------------------------------------------
# Parsing and post-processing
# --------------------------------------------------------------------------------------


def test_parse_output_formats():
    assert parse_output(files_answer("<p>a</p>", styles_css="p{}")) == {"index.html": "<p>a</p>", "styles.css": "p{}"}
    assert parse_output("Here you go:\n```html\n<!DOCTYPE html><p>b</p>\n```\n") == {"index.html": "<!DOCTYPE html><p>b</p>"}
    assert parse_output("<!DOCTYPE html><p>c</p>") == {"index.html": "<!DOCTYPE html><p>c</p>"}
    with pytest.raises(gen.OutputFormatError):
        parse_output("I cannot help with that.")
    with pytest.raises(gen.OutputFormatError, match="unexpected file"):
        parse_output("<<<FILE app.js>>>\nx\n<<<END FILE>>>")


def test_postprocess_owns_scripts_jsonld_tel_and_viewport(brief42):
    files, notes = postprocess(brief42, {"index.html": bad_page(brief42), "favicon.svg": '<svg onload="x()"><script>y()</script></svg>'})
    doc = files["index.html"]
    assert "document.title" not in doc and "cdn.example.com" not in doc and "onclick" not in doc
    assert "javascript:" not in doc
    assert 'name="viewport"' in doc and "<!DOCTYPE html>" in doc and 'lang="en"' in doc
    assert f'href="tel:+1{brief42.phone_digits()}"' in doc
    assert "<script" not in files["favicon.svg"] and "onload" not in files["favicon.svg"]
    assert any("call bar" in n for n in notes) and any("JSON-LD" in n for n in notes)
    b = unpack(pack_files({k: v.encode() for k, v in files.items()}))
    left = gate_no_script(b).reasons  # only the external font remains: the miner does not own the design
    assert left and all("fonts.example.com" in r["detail"] for r in left), left
    g3 = gate_facts(brief42, b, static_visible_text(doc))
    assert g3.passed, g3.reasons
    assert gate_no_reviews(b).passed


def test_postprocess_rewrites_wrong_tel_links(brief42):
    html = f'<!DOCTYPE html><html lang="en"><head></head><body><a href="tel:5550000">{brief42.phone}</a></body></html>'
    files, notes = postprocess(brief42, {"index.html": html})
    assert f'href="tel:+1{brief42.phone_digits()}"' in files["index.html"] and "tel:5550000" not in files["index.html"]
    assert "rewrote tel: links with the wrong digits" in notes


@pytest.mark.parametrize("trade", TRADES)
def test_jsonld_passes_the_facts_gate_for_every_trade(trade):
    brief = next(b for b in (generate(s) for s in range(400)) if b.trade == trade)
    node = gen.jsonld_for(brief)
    assert node["@type"] == SCHEMA_TYPE[trade] and node["@type"] in ALLOWED_TYPES[trade]
    hours = "".join(f"<li>{d} {s[0]}-{s[1]}</li>" for d, s in brief.hours.items() if s)
    html = f"<html><head></head><body><h1>{brief.name}</h1><p>{brief.phone}</p><ul>{hours}</ul></body></html>"
    files, _ = postprocess(brief, {"index.html": html})
    b = unpack(pack_files({"index.html": files["index.html"].encode()}))
    res = gate_facts(brief, b, static_visible_text(files["index.html"]))
    assert res.passed, res.reasons
    assert gate_no_script(b).passed, gate_no_script(b).reasons
    assert check_files(brief, files, "static").gates_passed


def test_jsonld_cannot_break_out_of_its_script(brief42):
    b = brief42.model_copy(update={"name": "A </script><script>alert(1)</script> Co"})
    s = gen.jsonld_script(b)
    assert s.count("</script>") == 1 and json.loads(s[s.index(">") + 1:-len("</script>")])["name"] == b.name


def test_honesty_lint(brief42):
    text = "Rated 4.9 stars. 120 reviews. Award-winning! Free estimates. Since 1987. 25 years of experience."
    codes = {f["code"].split(":")[1] for f in honesty_lint(brief42, text)}
    assert {"rating", "review_count", "award", "free_offer", "year"} <= codes
    clean = f"{brief42.name}. Friendly, careful work across {brief42.city}."
    assert honesty_lint(brief42, clean) == []


def test_honesty_lint_allows_facts_from_the_brief():
    brief = next(b for b in (generate(s) for s in range(400)) if b.facts.founded and b.facts.insured)
    text = f"Since {brief.facts.founded}. Fully insured."
    assert not [f for f in honesty_lint(brief, text) if f["code"].endswith((":year", ":insured"))]
    no = brief.model_copy(update={"facts": brief.facts.model_copy(update={"insured": False, "license": None,
                                                                          "emergency": None})})
    codes = {f["code"] for f in honesty_lint(no, "Fully insured and licensed. Emergency calls welcome.")}
    assert {"possible_invented_claim:insured", "possible_invented_claim:licensed"} <= codes
    lic = brief.model_copy(update={"facts": brief.facts.model_copy(update={"license": "TN-12345"})})
    assert not [f for f in honesty_lint(lic, "Licensed and insured.") if f["code"].endswith(":licensed")]


def test_static_check_catches_what_gates_would(brief42, good_html):
    files, _ = postprocess(brief42, {"index.html": bad_page(brief42)})
    rep = check_files(brief42, files, "static")
    codes = {r["code"] for r in rep.reasons}
    assert not rep.gates_passed and "external_resource" in codes
    assert any(a["code"].startswith("possible_invented_claim") for a in rep.advisories)


# --------------------------------------------------------------------------------------
# generate_site with a fake LLM
# --------------------------------------------------------------------------------------


def test_good_page_one_call(example_brief, good_html, good_svg):
    client, t = fake_client(files_answer(good_html, favicon_svg=good_svg))
    res = generate_site(example_brief, client=client, model="vendor/miner-model", check="static")
    assert len(t.requests) == 1
    req = t.requests[0]["json"]
    assert req["model"] == "vendor/miner-model" and req["temperature"] == gen.DEFAULT_TEMPERATURE
    assert req["messages"][0]["content"] == gen.SYSTEM_PROMPT
    assert example_brief.task_id in req["messages"][1]["content"]
    assert res.report.gates_passed and res.report.advisories == []
    assert set(res.files) == {"index.html", "favicon.svg"}
    assert res.usage.as_dict() == {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
    assert unpack(res.bundle_b64).files.keys() == {"index.html", "favicon.svg"}
    s = res.summary()
    assert s["ok"] and s["usage"]["total_tokens"] == 1500 and s["miner_version"].startswith("jmercury-ref-miner/")


def test_default_model_and_env_override(monkeypatch, example_brief, good_html):
    monkeypatch.delenv("JM_MINER_MODEL", raising=False)
    client, t = fake_client(files_answer(good_html))
    generate_site(example_brief, client=client, check="static")
    assert t.requests[0]["json"]["model"] == "anthropic/claude-haiku-5.5"
    monkeypatch.setenv("JM_MINER_MODEL", "other/model")
    client, t = fake_client(files_answer(good_html))
    generate_site(example_brief, client=client, check="static")
    assert t.requests[0]["json"]["model"] == "other/model"


def test_bad_page_is_fixed_and_retried_once(brief42):
    hours = "".join(f"<li>{d}: {s[0]} to {s[1]}</li>" for d, s in brief42.hours.items() if s)
    fixed = (f"<!DOCTYPE html><html lang=\"en\"><head><title>{brief42.name}</title></head><body>"
             f"<h1>{brief42.name}</h1><a href=\"tel:+1{brief42.phone_digits()}\">Call {brief42.phone}</a>"
             f"<ul>{hours}</ul></body></html>")
    client, t = fake_client(files_answer(bad_page(brief42)), files_answer(fixed))
    res = generate_site(brief42, client=client, check="static")
    assert len(t.requests) == 2
    retry_msgs = t.requests[1]["json"]["messages"]
    assert [m["role"] for m in retry_msgs] == ["system", "user", "assistant", "user"]
    feedback = retry_msgs[-1]["content"]
    assert "external_resource" in feedback and "fonts.example.com" in feedback
    assert "possible_invented_claim" in feedback
    assert res.report.gates_passed and res.report.advisories == []
    assert res.usage.total_tokens == 1500 + 1502
    assert [a["attempt"] for a in res.attempts] == [1, 2]


def test_advisory_only_failure_returns_the_page_with_warnings(brief42):
    hours = "".join(f"<li>{d}: {s[0]} to {s[1]}</li>" for d, s in brief42.hours.items() if s)
    page = (f"<!DOCTYPE html><html lang=\"en\"><head></head><body><h1>{brief42.name}</h1>"
            f"<p>{brief42.phone}</p><ul>{hours}</ul><p>Award-winning service, free estimates!</p></body></html>")
    client, t = fake_client(files_answer(page), files_answer(page))
    res = generate_site(brief42, client=client, check="static")
    assert len(t.requests) == 2
    assert res.report.gates_passed and res.warnings
    assert {w["code"] for w in res.warnings} >= {"possible_invented_claim:award", "possible_invented_claim:free_offer"}


def test_llm_that_keeps_failing_gives_a_clear_error(brief42):
    client, t = fake_client(files_answer(bad_page(brief42)), "Sorry, I can't do that.")
    with pytest.raises(GenerationError) as ei:
        generate_site(brief42, client=client, check="static")
    err = ei.value
    assert err.kind == "checks_failed" and len(t.requests) == 2
    assert err.reasons and err.reasons[0]["code"] == "bad_output_format"
    assert err.usage.total_tokens == 1500 + 1502
    d = err.as_dict()
    assert d["error"] == "checks_failed" and len(d["attempts"]) == 2
    assert KEY not in json.dumps(d)


def test_llm_http_failure_gives_a_clear_error(brief42):
    client, t = fake_client((500, {"error": {"message": "upstream"}}), (500, {"error": {"message": "upstream"}}))
    with pytest.raises(GenerationError) as ei:
        generate_site(brief42, client=client, check="static")
    assert ei.value.kind == "llm_failed" and "HTTP 500" in str(ei.value)


def test_retry_is_skipped_when_time_is_short(brief42):
    client, t = fake_client(files_answer(bad_page(brief42)), files_answer(bad_page(brief42)))
    with pytest.raises(GenerationError) as ei:
        generate_site(brief42, client=client, check="static", deadline=time.monotonic() + 30.0)
    assert len(t.requests) == 1
    assert any(a.get("skipped") for a in ei.value.attempts)


def test_truncated_output_is_reported_in_the_feedback(brief42):
    client, t = fake_client(files_answer(bad_page(brief42)), files_answer(bad_page(brief42)), finish_reason="length")
    with pytest.raises(GenerationError):
        generate_site(brief42, client=client, check="static")
    assert "output_truncated" in t.requests[1]["json"]["messages"][-1]["content"]


@pytest.mark.browser
def test_validator_level_self_check(example_brief, good_html, good_svg):
    client, t = fake_client(files_answer(good_html, favicon_svg=good_svg))
    res = generate_site(example_brief, client=client, check="validator")
    assert res.report.level == "validator" and res.report.gates_passed and res.report.advisories == []


@pytest.mark.browser
def test_validator_level_retries_on_390px_failures(example_brief, good_html):
    wide = good_html.replace("</body>", '<div style="width:900px;height:10px;background:#000"></div></body>')
    client, t = fake_client(files_answer(wide), files_answer(good_html))
    res = generate_site(example_brief, client=client, check="validator")
    assert len(t.requests) == 2
    assert "horizontal_scroll_at_390px" in t.requests[1]["json"]["messages"][-1]["content"]
    assert res.report.advisories == []


# --------------------------------------------------------------------------------------
# The miner without the validator extra, and the CLI
# --------------------------------------------------------------------------------------

BLOCKER = textwrap.dedent("""
    import importlib.abc, sys
    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in ("playwright", "html5lib"):
                raise ImportError(f"{name} is blocked for this test")
            return None
    sys.meta_path.insert(0, Block())
""")


def test_miner_runs_without_playwright_or_html5lib(tmp_path, good_html):
    script = BLOCKER + textwrap.dedent(f"""
        import json, sys
        from jmercury.brief import Brief
        from jmercury.llm import FakeTransport, LLMClient, completion
        from jmercury.miner import generate, server
        from jmercury.miner.generate import generate_site, validator_available
        assert not validator_available()
        brief = Brief.model_validate_json(open({str(EXAMPLE_DIR / 'brief.json')!r}).read())
        page = open({str(EXAMPLE_DIR / 'site' / 'index.html')!r}).read()
        t = FakeTransport([(200, completion("<<<FILE index.html>>>\\n" + page + "\\n<<<END FILE>>>"))])
        res = generate_site(brief, client=LLMClient(api_key="x", transport=t), check="auto")
        heavy = sorted(m for m in sys.modules if m.split(".")[0] in ("playwright", "html5lib"))
        print(json.dumps({{"level": res.report.level, "passed": res.report.gates_passed, "heavy": heavy}}))
    """)
    p = subprocess.run([sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out == {"level": "static", "passed": True, "heavy": []}


class _FakeOpenRouter(BaseHTTPRequestHandler):
    answer = ""
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _FakeOpenRouter.seen.append(json.loads(body))
        out = json.dumps(completion(_FakeOpenRouter.answer, prompt_tokens=3000, completion_tokens=4000)).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def fake_openrouter(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenRouter)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    monkeypatch.setenv("JM_LLM_BASE_URL", f"http://127.0.0.1:{srv.server_address[1]}/api/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    yield _FakeOpenRouter
    srv.shutdown()
    srv.server_close()


def test_cli_writes_site_and_bundle(tmp_path, fake_openrouter, good_html, capsys):
    fake_openrouter.answer = files_answer(good_html)
    out, b64 = tmp_path / "site", tmp_path / "site.b64"
    rc = gen.main(["--brief", str(EXAMPLE_DIR / "brief.json"), "--out", str(out), "--b64", str(b64),
                   "--check", "static", "--model", "vendor/m"])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["usage"] == {"prompt_tokens": 3000, "completion_tokens": 4000, "total_tokens": 7000}
    assert (out / "index.html").exists() and unpack(b64.read_text().strip()).files.keys() == {"index.html"}
    assert fake_openrouter.seen[-1]["model"] == "vendor/m"


def test_cli_without_key_is_clear(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    rc = gen.main(["--brief", str(EXAMPLE_DIR / "brief.json"), "--out", str(tmp_path / "s")])
    assert rc == 2 and "OPENROUTER_API_KEY is not set" in capsys.readouterr().err


def test_cli_failure_exit_code(tmp_path, fake_openrouter, capsys):
    fake_openrouter.answer = "no files here"
    rc = gen.main(["--brief", str(EXAMPLE_DIR / "brief.json"), "--out", str(tmp_path / "s"), "--check", "static"])
    cap = capsys.readouterr()
    assert rc == 1 and json.loads(cap.out)["error"] == "checks_failed" and KEY not in cap.out + cap.err
    assert not (tmp_path / "s").exists()


def test_prompt_states_the_rules():
    p = gen.SYSTEM_PROMPT
    for phrase in ("No JavaScript", "No external resources", "tel:", "390 x 844", "HONESTY RULE", "testimonials",
                   "star ratings", "prices", "certifications", "<<<FILE index.html>>>", "viewport"):
        assert phrase in p, phrase
    assert all(ord(c) < 128 for c in p)


def test_documented_prompt_matches_the_code():
    doc = (REPO / "docs" / "MINER.md").read_text(encoding="utf-8")
    block = doc.split("### The generator prompt", 1)[1].split("```")[1]
    assert " ".join(block.split()) == " ".join(gen.SYSTEM_PROMPT.split())
