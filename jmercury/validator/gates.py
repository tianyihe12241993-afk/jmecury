"""Gates 2-4 (DESIGN.md section 3.1). Gate 1 is ``bundle.unpack``.

Every gate returns a ``GateResult``: ``passed`` plus machine-readable ``reasons``
(each ``{"code", "detail"}``). Gate 2 is static. Gate 3 needs the rendered visible
text from ``render.py`` for the name, phone and hours tokens; its tel: link and
JSON-LD checks are static.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Optional
from xml.etree import ElementTree as ET

import html5lib

from ..brief import DAYS, PHONE_TOKEN, Brief, normalise_text, phone_digits, time_pattern  # noqa: F401 - re-exported
from ..bundle import Bundle

# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------


@dataclass
class GateResult:
    name: str
    passed: bool
    reasons: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"gate": self.name, "passed": self.passed, "reasons": self.reasons}


class _Collector:
    def __init__(self) -> None:
        self.reasons: list[dict] = []

    def add(self, code: str, detail: str, where: str = "") -> None:
        item = {"code": code, "detail": detail[:300]}
        if where:
            item["file"] = where
        if item not in self.reasons:
            self.reasons.append(item)


# --------------------------------------------------------------------------------------
# Helpers shared by the gates
# --------------------------------------------------------------------------------------

_WS_CTRL = re.compile(r"[\x00-\x20\x7f]+")
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
ALLOWED_LINK_SCHEMES = ("tel", "mailto", "sms", "https")

# Elements that are never allowed (lower-case local names; SVG names are case-folded too).
FORBIDDEN_ELEMENTS = {
    "iframe", "frame", "frameset", "object", "embed", "applet", "base", "portal", "fencedframe",
    "foreignobject", "handler", "listener",
}
# Attributes that load a resource (must be a local relative path).
RESOURCE_ATTRS = {"src", "srcset", "imagesrcset", "poster", "background", "data", "lowsrc", "dynsrc", "codebase", "manifest", "icon", "archive", "classid"}
# Attributes that are navigation targets (allowed schemes apply).
NAV_ATTRS = {"href", "action", "formaction"}
FORBIDDEN_ATTRS = {"srcdoc", "ping"}
SVG_ANIMATION = {"set", "animate", "animatemotion", "animatetransform", "animatecolor", "discard"}
JSONLD_TYPE = "application/ld+json"


def _local(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _url_core(value: str) -> str:
    """Normalise a URL the way a browser's URL parser would before scheme detection."""
    v = value.strip("\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\x0c\r\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f ")
    return v.replace("\t", "").replace("\n", "").replace("\r", "")


def is_local_ref(value: str) -> bool:
    """True if ``value`` is a same-origin relative reference (path, ?query or #fragment)."""
    v = _url_core(value)
    if v == "":
        return True
    if ":" in v.split("#", 1)[0].split("?", 1)[0]:
        return False  # any scheme, including data:, blob:, javascript:
    if ":" in v:
        return False
    if v[:2] in ("//", "\\\\", "/\\", "\\/"):
        return False
    return True


def link_scheme_ok(value: str) -> bool:
    v = _url_core(value)
    if is_local_ref(v):
        return True
    m = _SCHEME.match(v)
    if not m:
        return False
    scheme = m.group(1).lower()
    if scheme == "https":
        return v[6:8] == "//" or v.lower().startswith("https://")
    return scheme in ALLOWED_LINK_SCHEMES


def _has_js_url(value: str) -> bool:
    v = _WS_CTRL.sub("", value).lower()
    return "javascript:" in v or "vbscript:" in v or "livescript:" in v


def _srcset_urls(value: str) -> list[str]:
    urls = []
    for cand in value.split(","):
        parts = cand.strip().split()
        if parts:
            urls.append(parts[0])
    return urls


# ---- CSS -----------------------------------------------------------------------------

_CSS_COMMENT = re.compile(r"/\*.*?(\*/|$)", re.S)
_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})[ \t\n\r\f]?|\\(.)", re.S)
_CSS_URL = re.compile(r"url\(\s*(\"[^\"]*\"|'[^']*'|[^)]*?)\s*\)", re.I | re.S)
_CSS_STRING = re.compile(r"\"((?:[^\"\\\n]|\\.)*)\"|'((?:[^'\\\n]|\\.)*)'", re.S)
_CSS_IMPORT = re.compile(r"@import\s+(\"[^\"]*\"|'[^']*'|url\([^)]*\)|[^;\s]+)", re.I)
_ABS_LIKE = re.compile(r"^\s*(?:(?:https?|data|javascript|vbscript|blob|file|ftp|wss?|filesystem):|//|\\\\)", re.I)


def _css_unescape(css: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group(1):
            cp = int(m.group(1), 16)
            if cp == 0 or cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
                return "�"
            return chr(cp)
        ch = m.group(2)
        return "" if ch == "\n" else ch

    return _CSS_ESCAPE.sub(repl, css)


def check_css(css: str, col: _Collector, where: str) -> None:
    text = _css_unescape(_CSS_COMMENT.sub(" ", css))
    for m in _CSS_IMPORT.finditer(text):
        target = m.group(1)
        inner = target
        um = _CSS_URL.match(target)
        if um:
            inner = um.group(1)
        inner = inner.strip().strip("\"'")
        if not is_local_ref(inner):
            col.add("css_external_import", target, where)
    for m in _CSS_URL.finditer(text):
        inner = m.group(1).strip().strip("\"'")
        if not is_local_ref(inner):
            col.add("css_external_url", inner[:120], where)
    for m in _CSS_STRING.finditer(text):
        s = m.group(1) if m.group(1) is not None else m.group(2)
        if _ABS_LIKE.match(s or ""):
            col.add("css_external_string", s[:120], where)
    low = text.lower()
    if _has_js_url(text):
        col.add("css_javascript_url", "javascript: in CSS", where)
    for bad in ("expression(", "-moz-binding", "behavior:"):
        if bad in _WS_CTRL.sub("", low):
            col.add("css_script_feature", bad, where)


# ---- HTML / SVG element walk ---------------------------------------------------------


def _check_element_attrs(tag: str, attrs: dict[str, str], col: _Collector, where: str) -> None:
    for raw_name, value in attrs.items():
        aname = _local(raw_name)
        value = value or ""
        if aname.startswith("on"):
            col.add("event_handler_attribute", f"<{tag} {aname}>", where)
        if aname in FORBIDDEN_ATTRS:
            col.add("forbidden_attribute", f"<{tag} {aname}>", where)
        if _has_js_url(value):
            col.add("javascript_url", f"<{tag} {aname}>", where)
        if aname == "style":
            check_css(value, col, where)
        if aname in ("srcset", "imagesrcset"):
            for u in _srcset_urls(value):
                if not is_local_ref(u):
                    col.add("external_resource", f"<{tag} {aname}> {u[:120]}", where)
        elif aname in RESOURCE_ATTRS:
            if not is_local_ref(value):
                col.add("external_resource", f"<{tag} {aname}> {value[:120]}", where)
        elif aname in NAV_ATTRS:
            if tag in ("a", "area") and aname == "href":
                if not link_scheme_ok(value):
                    col.add("disallowed_link_scheme", f"<{tag} href> {value[:120]}", where)
            elif aname in ("action", "formaction"):
                if not link_scheme_ok(value):
                    col.add("disallowed_link_scheme", f"<{tag} {aname}> {value[:120]}", where)
            else:
                # href on link/use/image/feImage/... loads a resource
                if not is_local_ref(value):
                    col.add("external_resource", f"<{tag} href> {value[:120]}", where)


def _walk_dom(root: Any, col: _Collector, where: str, *, svg_file: bool = False) -> list[str]:
    """Walk an ElementTree (html5lib or SVG). Returns the texts of JSON-LD blocks."""
    jsonld: list[str] = []
    for el in root.iter():
        tag = _local(el.tag)
        if not tag:
            continue  # comments, processing instructions
        attrs = {k: (v if isinstance(v, str) else "") for k, v in el.attrib.items()}
        lattrs = {_local(k): v for k, v in attrs.items()}
        if tag == "script":
            stype = (lattrs.get("type") or "").strip().lower()
            if svg_file or stype != JSONLD_TYPE or "src" in lattrs or set(lattrs) - {"type", "id"}:
                col.add("script_element", f"<script type={stype!r}>", where)
            else:
                jsonld.append("".join(el.itertext()))
            continue
        if tag in FORBIDDEN_ELEMENTS:
            col.add("forbidden_element", f"<{tag}>", where)
        if tag == "meta" and (lattrs.get("http-equiv") or "").strip().lower() == "refresh":
            col.add("meta_refresh", "<meta http-equiv=refresh>", where)
        if tag == "link":
            rel = (lattrs.get("rel") or "").lower().split()
            if "import" in rel:
                col.add("forbidden_element", "<link rel=import>", where)
        if tag in SVG_ANIMATION:
            target = _local(lattrs.get("attributename") or "")
            if target in ("href", "src") or target.startswith("on"):
                col.add("svg_animated_reference", f"<{tag} attributeName={target}>", where)
        if tag == "style":
            check_css("".join(el.itertext()), col, where)
        _check_element_attrs(tag, attrs, col, where)
    return jsonld


_RAW_FORBIDDEN_TAG = re.compile(r"<\s*/?\s*(?:[a-z0-9_-]+:)?(iframe|frame|object|embed|applet|base|foreignobject|portal)\b", re.I)
_RAW_SCRIPT_TAG = re.compile(r"<\s*(?:[a-z0-9_-]+:)?script\b([^>]*)>", re.I)
_RAW_TAG = re.compile(r"<[a-zA-Z][^>]*>", re.S)
_RAW_ATTR = re.compile(r"([^\s=/\"'>]+)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+))?")
_RAW_ON_ATTR = re.compile(r"[\s/\"']on[a-z]+\s*=", re.I)


def _raw_backstop(text: str, col: _Collector, where: str) -> None:
    """Defence in depth: pattern checks on the raw source, independent of the parser."""
    m = _RAW_FORBIDDEN_TAG.search(text)
    if m:
        col.add("forbidden_element", f"raw <{m.group(1).lower()}>", where)
    for m in _RAW_SCRIPT_TAG.finditer(text):
        attrs = {k.lower(): v.strip("\"'").strip().lower() for k, v in _RAW_ATTR.findall(m.group(1))}
        if attrs.get("type") != JSONLD_TYPE or set(attrs) - {"type", "id"}:
            col.add("script_element", "raw <script> that is not a plain JSON-LD block", where)
    for m in _RAW_TAG.finditer(text):
        tag_text = m.group(0)
        if _RAW_ON_ATTR.search(tag_text):
            col.add("event_handler_attribute", f"raw {tag_text[:80]}", where)


def parse_html(text: str) -> Any:
    return html5lib.parse(text, treebuilder="etree", namespaceHTMLElements=False)


def _check_svg_file(text: str, col: _Collector, where: str) -> None:
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
        col.add("svg_doctype", "DOCTYPE/ENTITY declarations are not allowed in SVG files", where)
        return
    if re.search(r"<\?xml-stylesheet", text, re.I):
        col.add("svg_stylesheet_pi", "xml-stylesheet processing instruction", where)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        col.add("svg_parse_error", str(exc), where)
        return
    _walk_dom(root, col, where, svg_file=True)
    _raw_backstop(text, col, where)


# --------------------------------------------------------------------------------------
# Gate 2: no script, no external resources
# --------------------------------------------------------------------------------------


def gate_no_script(bundle: Bundle) -> GateResult:
    col = _Collector()
    for name in bundle.names:
        ext = PurePosixPath(name).suffix.lower()
        if ext == ".html":
            text = bundle.text(name)
            _walk_dom(parse_html(text), col, name)
            _raw_backstop(text, col, name)
        elif ext == ".css":
            check_css(bundle.text(name), col, name)
        elif ext == ".svg":
            _check_svg_file(bundle.text(name), col, name)
    return GateResult("no_script", not col.reasons, col.reasons)


# --------------------------------------------------------------------------------------
# JSON-LD
# --------------------------------------------------------------------------------------


def jsonld_blocks(index_html: str) -> list[str]:
    col = _Collector()
    return _walk_dom(parse_html(index_html), col, "index.html")


def _iter_nodes(obj: Any) -> Iterable[dict]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_nodes(v)


def _type_names(node: dict) -> set[str]:
    t = node.get("@type")
    vals = t if isinstance(t, list) else [t]
    out = set()
    for v in vals:
        if isinstance(v, str):
            out.add(re.split(r"[/#:]", v.strip())[-1])
    return out


ALLOWED_TYPES: dict[str, set[str]] = {
    "lawn": {"HomeAndConstructionBusiness"},
    "roofing": {"RoofingContractor", "HomeAndConstructionBusiness", "GeneralContractor"},
    "hvac": {"HVACBusiness", "HomeAndConstructionBusiness"},
    "plumbing": {"Plumber", "HomeAndConstructionBusiness"},
    "dental": {"Dentist", "MedicalBusiness", "MedicalClinic"},
    "remodeling": {"GeneralContractor", "HomeAndConstructionBusiness"},
    "commercial": {"GeneralContractor", "HomeAndConstructionBusiness"},
    "aesthetics": {"HealthAndBeautyBusiness", "DaySpa", "BeautySalon", "MedicalBusiness"},
}
for _v in ALLOWED_TYPES.values():
    _v.add("LocalBusiness")

_DAY_NAMES = {"Monday": "mon", "Tuesday": "tue", "Wednesday": "wed", "Thursday": "thu", "Friday": "fri", "Saturday": "sat", "Sunday": "sun"}
_DAY_PREFIXES = ("https://schema.org/", "http://schema.org/", "schema:")


def _norm_ws(s: str) -> str:
    return " ".join(s.split())


def _norm_time(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    m = re.fullmatch(r"\s*([01]\d|2[0-3]):([0-5]\d)(?::00)?\s*", v)
    return f"{m.group(1)}:{m.group(2)}" if m else None


def _norm_day(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    s = v.strip()
    for p in _DAY_PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
            break
    return _DAY_NAMES.get(s)


def ohs_set(spec: Any) -> tuple[set[tuple[str, str, str]], list[str]]:
    """Normalise openingHoursSpecification to {(day, opens, closes)}; returns (set, problems)."""
    problems: list[str] = []
    out: set[tuple[str, str, str]] = set()
    specs = spec if isinstance(spec, list) else [spec]
    for s in specs:
        if not isinstance(s, dict):
            problems.append("specification entry is not an object")
            continue
        days = s.get("dayOfWeek")
        days = days if isinstance(days, list) else [days]
        opens, closes = _norm_time(s.get("opens")), _norm_time(s.get("closes"))
        if opens is None or closes is None:
            problems.append(f"bad opens/closes {s.get('opens')!r}/{s.get('closes')!r}")
            continue
        for d in days:
            nd = _norm_day(d)
            if nd is None:
                problems.append(f"bad dayOfWeek {d!r}")
                continue
            out.add((nd, opens, closes))
    return out, problems


def brief_hours_set(brief: Brief) -> set[tuple[str, str, str]]:
    return {(d, span[0], span[1]) for d in DAYS if (span := brief.hours[d]) is not None}


def _parse_jsonld(index_html: str, col: _Collector) -> list[Any]:
    docs = []
    for i, block in enumerate(jsonld_blocks(index_html)):
        try:
            docs.append(json.loads(block))
        except (json.JSONDecodeError, ValueError) as exc:
            col.add("jsonld_parse_error", f"block {i}: {exc}")
    return docs


def check_business_node(brief: Brief, node: dict) -> list[str]:
    problems = []
    name = node.get("name")
    if not isinstance(name, str) or _norm_ws(name) != _norm_ws(brief.name):
        problems.append(f"name {name!r} != {brief.name!r}")
    tel = node.get("telephone")
    if not isinstance(tel, str) or phone_digits(tel) != brief.phone_digits():
        problems.append(f"telephone {tel!r} != {brief.phone!r}")
    if "openingHoursSpecification" not in node:
        problems.append("openingHoursSpecification missing")
    else:
        got, probs = ohs_set(node["openingHoursSpecification"])
        problems += probs
        want = brief_hours_set(brief)
        if got != want:
            missing = sorted(want - got)
            extra = sorted(got - want)
            problems.append(f"openingHoursSpecification differs: missing {missing}, extra {extra}")
    return problems


# --------------------------------------------------------------------------------------
# Gate 3: facts are exact
# --------------------------------------------------------------------------------------

def tel_links(index_html: str) -> list[str]:
    root = parse_html(index_html)
    out = []
    for el in root.iter():
        if _local(el.tag) in ("a", "area"):
            for k, v in el.attrib.items():
                if _local(k) == "href" and isinstance(v, str) and _url_core(v).lower().startswith("tel:"):
                    out.append(_url_core(v))
    return out


def gate_facts(brief: Brief, bundle: Bundle, visible_text: Optional[str]) -> GateResult:
    col = _Collector()
    index_html = bundle.text("index.html")

    if visible_text is None:
        col.add("no_visible_text", "the page was not rendered; visible-text checks cannot run")
    else:
        vt = normalise_text(visible_text)
        if normalise_text(brief.name) not in vt:
            col.add("name_not_visible", brief.name)
        want = brief.phone_digits()
        if not any(phone_digits(m.group(0)) == want for m in PHONE_TOKEN.finditer(visible_text)):
            col.add("phone_not_visible", brief.phone)
        times = sorted({t for span in brief.hours.values() if span for t in span})
        for t in times:
            if not time_pattern(t).search(visible_text):
                col.add("hours_time_not_visible", t)

    tels = tel_links(index_html)
    if not any(phone_digits(t[4:].split("?")[0]) == brief.phone_digits() for t in tels):
        col.add("no_matching_tel_link", f"found {tels[:5]}")

    docs = _parse_jsonld(index_html, col)
    allowed = ALLOWED_TYPES[brief.trade]
    business = [n for d in docs for n in _iter_nodes(d) if _type_names(n) & allowed]
    if not business:
        found = sorted({t for d in docs for n in _iter_nodes(d) for t in _type_names(n)})
        col.add("jsonld_no_business_node", f"need one of {sorted(allowed)}; found {found}")
    else:
        attempts = [check_business_node(brief, n) for n in business]
        if not any(not p for p in attempts):
            best = min(attempts, key=len)
            for p in best:
                col.add("jsonld_mismatch", p)
    return GateResult("facts", not col.reasons, col.reasons)


# --------------------------------------------------------------------------------------
# Gate 4: no fake social proof in structured data
# --------------------------------------------------------------------------------------

_REVIEW_TYPES = {"AggregateRating", "Review", "EmployerAggregateRating", "CriticReview", "UserReview"}
_REVIEW_KEYS = {"aggregateRating", "review", "reviews"}


def gate_no_reviews(bundle: Bundle) -> GateResult:
    col = _Collector()
    docs = _parse_jsonld(bundle.text("index.html"), col)
    for d in docs:
        for n in _iter_nodes(d):
            hit = _type_names(n) & _REVIEW_TYPES
            if hit:
                col.add("jsonld_review_type", ",".join(sorted(hit)))
            keys = set(n.keys()) & _REVIEW_KEYS
            if keys:
                col.add("jsonld_review_property", ",".join(sorted(keys)))
    return GateResult("no_fake_reviews", not col.reasons, col.reasons)
