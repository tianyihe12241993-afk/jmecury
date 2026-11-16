import json

import pytest

from jmercury.bundle import unpack
from jmercury.validator.gates import gate_facts, gate_no_reviews, time_pattern

from ..conftest import DAY_FULL, default_jsonld, facts_html, site_b64, visible_for


def g3(brief, html, visible=None, fmt="24h"):
    vt = visible if visible is not None else visible_for(brief, fmt)
    return gate_facts(brief, unpack(site_b64(html)), vt)


def codes(r):
    return {x["code"] for x in r.reasons}


# ---- visible-text tokens --------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["24h", "h:mm AM", "ham", "h a.m."])
def test_hours_all_four_formats(brief42, fmt):
    r = g3(brief42, facts_html(brief42), fmt=fmt)
    assert r.passed, r.reasons


@pytest.mark.parametrize("hhmm,text,ok", [
    ("08:00", "8:00 AM", True), ("08:00", "8am", True), ("08:00", "8 a.m.", True), ("08:00", "08:00", True),
    ("08:00", "8 AM", True), ("08:00", "8:00am", True), ("08:00", "8 am", True), ("08:00", "8:00", True),
    ("17:00", "5:00 PM", True), ("17:00", "5pm", True), ("17:00", "5 p.m.", True), ("17:00", "17:00", True),
    ("17:30", "5:30 PM", True), ("17:30", "17:30", True), ("12:00", "12pm", True), ("12:00", "12:00", True),
    ("17:00", "5:00", False), ("17:00", "5am", False), ("08:00", "8pm", False), ("08:00", "18:00", False),
    ("08:00", "8 amazing", False), ("17:30", "5pm", False), ("08:00", "08:00 PM", False), ("07:00", "17:00", False),
])
def test_time_tokens(hhmm, text, ok):
    assert bool(time_pattern(hhmm).search(f"Open {text} daily")) is ok


def test_wrong_hours_fail(brief42):
    vt = visible_for(brief42).replace("17:00", "18:00")
    r = g3(brief42, facts_html(brief42), visible=vt)
    assert "hours_time_not_visible" in codes(r)


def test_name_and_phone_must_be_visible(brief42):
    vt = visible_for(brief42)
    r = g3(brief42, facts_html(brief42), visible=vt.replace(brief42.name, "Our company"))
    assert "name_not_visible" in codes(r)
    r = g3(brief42, facts_html(brief42), visible=vt.replace(brief42.phone, "our number"))
    assert "phone_not_visible" in codes(r)


def test_name_case_and_spacing_tolerant(brief42):
    vt = visible_for(brief42).replace(brief42.name, "  " + brief42.name.upper().replace(" ", "\n "))
    assert g3(brief42, facts_html(brief42), visible=vt).passed


@pytest.mark.parametrize("phone", ["615-555-0127", "615.555.0127", "+1 615 555 0127", "1-615-555-0127", "6155550127"])
def test_phone_formats_visible(brief42, phone):
    vt = visible_for(brief42).replace(brief42.phone, phone)
    assert g3(brief42, facts_html(brief42), visible=vt).passed


def test_no_render_means_fail(brief42):
    r = gate_facts(brief42, unpack(site_b64(facts_html(brief42))), None)
    assert "no_visible_text" in codes(r)


# ---- tel: link -----------------------------------------------------------------------


@pytest.mark.parametrize("tel,ok", [
    ("tel:+16155550127", True), ("tel:615-555-0127", True), ("tel:(615) 555-0127", True), ("tel:+1-615-555-0127", True),
    ("tel:6155550128", False), ("tel:555-0127", False), ("tel:+446155550127", False),
])
def test_tel_link(brief42, tel, ok):
    r = g3(brief42, facts_html(brief42, tel=tel))
    assert r.passed is ok, r.reasons


# ---- JSON-LD -------------------------------------------------------------------------


def grouped(brief):
    by_span = {}
    for d, s in brief.hours.items():
        if s:
            by_span.setdefault(tuple(s), []).append(DAY_FULL[d])
    return [{"@type": "OpeningHoursSpecification", "dayOfWeek": days, "opens": o, "closes": c} for (o, c), days in by_span.items()]


def test_jsonld_grouped_days(brief42):
    assert g3(brief42, facts_html(brief42, jsonld=default_jsonld(brief42, openingHoursSpecification=grouped(brief42)))).passed


def test_jsonld_schema_url_days_and_seconds(brief42):
    specs = [{"dayOfWeek": [f"https://schema.org/{d}" for d in s["dayOfWeek"]], "opens": s["opens"] + ":00", "closes": s["closes"] + ":00"}
             for s in grouped(brief42)]
    assert g3(brief42, facts_html(brief42, jsonld=default_jsonld(brief42, openingHoursSpecification=specs))).passed


def test_jsonld_graph(brief42):
    node = default_jsonld(brief42, **{"@type": "HVACBusiness"})
    node.pop("@context")
    doc = {"@context": "https://schema.org", "@graph": [{"@type": "WebSite", "name": "x"}, node]}
    assert g3(brief42, facts_html(brief42, jsonld=doc)).passed


@pytest.mark.parametrize("t,ok", [
    ("HVACBusiness", True), ("LocalBusiness", True), ("HomeAndConstructionBusiness", True),
    ("https://schema.org/HVACBusiness", True), (["HVACBusiness", "LocalBusiness"], True),
    ("Plumber", False), ("Dentist", False), ("Organization", False), ("WebSite", False),
])
def test_jsonld_subtypes_for_hvac(brief42, t, ok):
    assert brief42.trade == "hvac"
    r = g3(brief42, facts_html(brief42, jsonld=default_jsonld(brief42, **{"@type": t})))
    assert r.passed is ok, r.reasons
    if not ok:
        assert "jsonld_no_business_node" in codes(r)


@pytest.mark.parametrize("tel,ok", [("+1 615 555 0127", True), ("+1-615-555-0127", True), ("615.555.0127", True),
                                    ("(615) 555-0128", False), ("", False)])
def test_jsonld_telephone(brief42, tel, ok):
    assert g3(brief42, facts_html(brief42, jsonld=default_jsonld(brief42, telephone=tel))).passed is ok


def test_jsonld_name(brief42):
    spaced = "  " + brief42.name.replace(" ", "   ") + " "
    assert g3(brief42, facts_html(brief42, jsonld=default_jsonld(brief42, name=spaced))).passed
    assert not g3(brief42, facts_html(brief42, jsonld=default_jsonld(brief42, name=brief42.name.upper()))).passed
    assert not g3(brief42, facts_html(brief42, jsonld=default_jsonld(brief42, name=brief42.name + " LLC"))).passed


def test_jsonld_hours_must_match_as_set(brief42):
    specs = grouped(brief42)
    # closed Sunday listed -> extra element -> fail
    extra = specs + [{"dayOfWeek": "Sunday", "opens": "00:00", "closes": "00:00"}]
    assert not g3(brief42, facts_html(brief42, jsonld=default_jsonld(brief42, openingHoursSpecification=extra))).passed
    # missing Saturday -> fail
    no_sat = [s for s in specs if "Saturday" not in s["dayOfWeek"]]
    r = g3(brief42, facts_html(brief42, jsonld=default_jsonld(brief42, openingHoursSpecification=no_sat)))
    assert "jsonld_mismatch" in codes(r)
    # wrong closing time -> fail
    wrong = json.loads(json.dumps(specs).replace('"17:00"', '"18:00"'))
    assert not g3(brief42, facts_html(brief42, jsonld=default_jsonld(brief42, openingHoursSpecification=wrong))).passed
    # duplicates are fine (it is a set)
    assert g3(brief42, facts_html(brief42, jsonld=default_jsonld(brief42, openingHoursSpecification=specs + specs))).passed


def test_jsonld_missing_or_malformed(brief42):
    assert "jsonld_no_business_node" in codes(g3(brief42, facts_html(brief42, jsonld=[])))
    r = g3(brief42, facts_html(brief42, jsonld=[default_jsonld(brief42), "{not json"]))
    assert "jsonld_parse_error" in codes(r)
    node = default_jsonld(brief42)
    node.pop("openingHoursSpecification")
    assert not g3(brief42, facts_html(brief42, jsonld=node)).passed


def test_jsonld_one_good_node_is_enough(brief42):
    bad = default_jsonld(brief42, telephone="(615) 555-0199")
    assert g3(brief42, facts_html(brief42, jsonld=[bad, default_jsonld(brief42)])).passed


def test_example_static_parts(example_brief, example_html):
    from ..conftest import EXAMPLE_DIR  # noqa: F401

    r = gate_facts(example_brief, unpack(site_b64(example_html)), visible_for(example_brief, "h:mm AM"))
    assert r.passed, r.reasons


# ---- gate 4 --------------------------------------------------------------------------


def g4(brief, jsonld):
    return gate_no_reviews(unpack(site_b64(facts_html(brief, jsonld=jsonld))))


def test_gate4_clean(brief42):
    assert g4(brief42, default_jsonld(brief42)).passed


@pytest.mark.parametrize("extra", [
    {"aggregateRating": {"@type": "AggregateRating", "ratingValue": 4.9, "reviewCount": 120}},
    {"aggregateRating": {"ratingValue": 4.9, "reviewCount": 120}},
    {"review": [{"@type": "Review", "author": "A", "reviewBody": "Great"}]},
    {"review": {"reviewBody": "Great"}},
    {"subjectOf": {"@type": "https://schema.org/Review", "reviewBody": "Great"}},
])
def test_gate4_rejects(brief42, extra):
    assert not g4(brief42, default_jsonld(brief42, **extra)).passed


def test_gate4_graph(brief42):
    doc = {"@context": "https://schema.org", "@graph": [default_jsonld(brief42), {"@type": "Review", "reviewBody": "x"}]}
    assert not g4(brief42, doc).passed


def test_gate4_separate_block(brief42):
    assert not g4(brief42, [default_jsonld(brief42), {"@type": "AggregateRating", "ratingValue": "5"}]).passed
