import json
import re
import subprocess
import sys

import pytest
from pydantic import ValidationError

from jmercury.brief import CITIES, DAYS, SECTIONS, TRADES, Brief, generate

from ..conftest import EXAMPLE_DIR, REPO

AREA = {(c, s): a for c, s, a, _ in CITIES}


def test_same_seed_identical_json():
    for seed in (0, 1, 42, 999, 2**40):
        assert generate(seed).to_json() == generate(seed).to_json()


def test_different_seeds_differ():
    assert len({generate(s).to_json() for s in range(50)}) == 50


def test_example_brief_is_seed_42():
    assert (EXAMPLE_DIR / "brief.json").read_text(encoding="utf-8").strip() == generate(42).to_json()


def test_cli_matches_generate():
    out = subprocess.run([sys.executable, "-m", "jmercury.brief", "--seed", "7"], capture_output=True, text=True, check=True, cwd=REPO)
    assert out.stdout.strip() == generate(7).to_json()


@pytest.mark.parametrize("seed", range(400))
def test_schema_and_content(seed):
    b = generate(seed)
    again = Brief.model_validate_json(b.to_json())
    assert again.to_json() == b.to_json()
    d = json.loads(b.to_json())
    assert list(d) == ["task_id", "trade", "name", "city", "state", "service_area", "phone", "email", "address",
                       "hours", "services", "facts", "owner_notes", "required"]
    assert b.trade in TRADES
    assert re.fullmatch(r"\(\d{3}\) 555-01\d\d", b.phone)
    assert b.phone[1:4] == AREA[(b.city, b.state)]
    assert b.email.endswith(".example")
    assert 3 <= len(b.services) <= 8 and len({s.name for s in b.services}) == len(b.services)
    assert list(b.hours) == list(DAYS)
    assert any(v is not None for v in b.hours.values())
    assert set(d["facts"]) <= {"founded", "license", "emergency", "insured", "languages"}
    assert None not in d["facts"].values()
    assert "call" in b.required and set(b.required) <= set(SECTIONS)
    assert b.city not in b.service_area and len(b.service_area) >= 4
    assert 1 <= b.owner_notes.count(".") <= 3


def test_variety_over_seeds():
    briefs = [generate(s) for s in range(400)]
    assert {b.trade for b in briefs} == set(TRADES)
    assert len({b.city for b in briefs}) >= 35
    assert any(b.address is None for b in briefs) and any(b.address for b in briefs)
    assert any(b.hours["sun"] is None for b in briefs) and any(b.hours["sat"] for b in briefs)
    assert any(b.facts.emergency for b in briefs if b.trade in ("hvac", "plumbing"))
    for key in ("founded", "license", "insured", "languages"):
        present = sum(getattr(b.facts, key) is not None for b in briefs)
        assert 0 < present < len(briefs), key


def test_rejects_extra_and_bad_fields():
    d = json.loads(generate(1).to_json())
    with pytest.raises(ValidationError):
        Brief.model_validate({**d, "rating": 5})
    with pytest.raises(ValidationError):
        Brief.model_validate({**d, "phone": "(937) 555-1234"})
    with pytest.raises(ValidationError):
        Brief.model_validate({**d, "facts": {"awards": ["best"]}})
    bad_hours = dict(d["hours"])
    bad_hours["mon"] = ["17:00", "08:00"]
    with pytest.raises(ValidationError):
        Brief.model_validate({**d, "hours": bad_hours})
