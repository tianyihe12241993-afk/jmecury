"""Reference miner: turn a brief into a site with one OpenRouter LLM call (plus at most one retry).

    python -m jmercury.miner.generate --brief brief.json --out ./site/ [--b64 site.b64]

The LLM writes ``index.html`` (and optionally ``styles.css`` and ``favicon.svg``) following the hard rules of
docs/DESIGN.md section 2.2. The miner does not trust the LLM with the facts a gate checks exactly; after every
reply it deterministically:

- removes every ``<script>`` and inline event handler, and turns ``javascript:`` links into ``#``;
- builds the schema.org JSON-LD block itself from the brief (the trade's subtype, exact name, telephone and
  ``openingHoursSpecification``) and inserts it as the page's only script;
- makes sure a ``tel:`` link with the brief's digits exists (rewrites wrong ones, or adds a call bar);
- adds ``<meta name="viewport">``, ``<meta charset>``, ``lang``, ``<title>`` and a meta description when missing.

Then it checks the result. With the ``validator`` extra installed it runs the validator's own gates and the
390 px checks (no horizontal scrolling, call link visible on the first screen); otherwise it runs cheap static
checks. On a failure it asks the LLM once more, with the reasons. If the gates still fail it raises
``GenerationError``; a page that passes the gates but misses an advisory check is returned with warnings.

Environment: ``OPENROUTER_API_KEY`` (required), ``JM_MINER_MODEL`` (default ``anthropic/claude-haiku-5.5``, a
starting point only, not a recommendation), ``JM_LLM_BASE_URL`` (tests).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

from .. import __version__
from ..brief import DAYS, PHONE_TOKEN, Brief, normalise_text, phone_digits, time_pattern
from ..bundle import MAX_FILES, MAX_UNZIPPED, BundleRejected, pack_files, unpack
from ..llm import ChatResult, LLMClient, LLMConfigError, LLMError, Usage
from . import MINER_VERSION_PREFIX

ENV_MODEL = "JM_MINER_MODEL"
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"  # a starting point only
OUTPUT_FILES = ("index.html", "styles.css", "favicon.svg")
DEFAULT_MAX_TOKENS = 16000
DEFAULT_TEMPERATURE = 0.7
MIN_SECONDS_FOR_RETRY = 40.0
MINER_VERSION = f"{MINER_VERSION_PREFIX}/{__version__}"

DAY_FULL = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday",
            "sat": "Saturday", "sun": "Sunday"}
SCHEMA_TYPE = {
    "lawn": "HomeAndConstructionBusiness",
    "roofing": "RoofingContractor",
    "hvac": "HVACBusiness",
    "plumbing": "Plumber",
    "dental": "Dentist",
    "remodeling": "GeneralContractor",
    "aesthetics": "HealthAndBeautyBusiness",
    "commercial": "GeneralContractor",
}
TRADE_LABEL = {
    "lawn": "lawn care and landscaping",
    "roofing": "roofing",
    "hvac": "heating and air conditioning",
    "plumbing": "plumbing",
    "dental": "dental care",
    "remodeling": "home remodeling",
    "aesthetics": "medical aesthetics",
    "commercial": "commercial construction",
}


class GenerationError(RuntimeError):
    """The miner could not produce a gate-passing site. ``reasons`` are machine-readable."""

    def __init__(self, kind: str, message: str, *, reasons: Optional[list[dict]] = None,
                 usage: Optional[Usage] = None, attempts: Optional[list[dict]] = None):
        self.kind = kind
        self.reasons = reasons or []
        self.usage = usage or Usage()
        self.attempts = attempts or []
        super().__init__(message)

    def as_dict(self) -> dict:
        return {"error": self.kind, "message": str(self), "reasons": self.reasons, "usage": self.usage.as_dict(),
                "attempts": self.attempts}


# --------------------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------------------

SYSTEM_PROMPT = """You build one-page websites for local service businesses (the J Mercury subnet). A validator \
renders your page in headless Chromium on a 390 x 844 phone screen with the network off and JavaScript off, \
checks hard rules, measures it with Lighthouse, and has a vision model compare it with other pages built for the \
same brief.

HARD RULES (a page that breaks any of them scores 0):
1. Output only these files: index.html (required), styles.css (optional), favicon.svg (optional).
2. No JavaScript: no <script> elements at all (the structured data block is added for you), no on* event \
attributes, no javascript: URLs, no <iframe>, <object>, <embed> or <base>, no meta refresh, no scripts in SVG.
3. No external resources: no web fonts, stylesheets, images or icons loaded from other sites, no @import of URLs, \
no data: URLs. Use a system font stack (system-ui, -apple-system, "Segoe UI", Roboto, sans-serif). There are no \
photos: use CSS and inline SVG for any decoration. Links to tel:, mailto:, sms: and https: map links are fine.
4. The business name and the phone number, exactly as written in the brief, must be in the visible text, and a \
call link <a href="tel:+1XXXXXXXXXX"> with the brief's digits must be visible on the first phone screen.
5. Every opening and closing time in the brief must be in the visible text, written like "8:00 AM" or "8am", for \
every open day; say which days are closed.
6. Text hidden with CSS does not count as visible.

HONESTY RULE (a page that breaks it scores 0): state only facts that are in the brief. Never invent \
testimonials, customer quotes, star ratings, review counts, awards, rankings, "#1" or "best in" claims, prices, \
discounts, free offers, guarantees, warranties, financing, founding years or years in business, certifications, \
memberships, licenses, insurance, 24/7 or same-day promises, or staff names. The entries of "facts" may be stated \
exactly as given (for example the license number, the founding year, emergency service, insured, languages). \
General wording such as "friendly team" or "careful work" is fine.

WHAT WINS, in order of importance (the judge's rubric):
1. Five-second clarity: on the first phone screen a visitor sees what the business does, where it works and a \
big, obvious call button.
2. Facts and honesty: name, phone, hours, service area and services shown correctly and consistently.
3. Fit for the trade and its customers: urgency for a breakdown, reassurance for a nervous patient, scope and \
trust for a remodel. Use the owner's notes for tone and priorities; do not quote them as text.
4. Readable and usable on a phone: base font at least 16px, strong colour contrast, tap targets at least 48px \
tall, nothing cut off or overlapping, no horizontal scrolling. Include \
<meta name="viewport" content="width=device-width, initial-scale=1">.
5. Looks professional and trustworthy: clean, consistent, specific to this business, no filler or lorem ipsum.

Also: <html lang="en">, a <title>, a <meta name="description">, one <h1>, headings in order, header/main/footer \
landmarks, a sticky or prominent call button, compact CSS. Include every section listed in "required" (call, \
services, area, hours, about). The address, if the brief gives one, may be shown with an https: map link; if the \
address is null the business does not publish one: show the service area instead.

OUTPUT FORMAT: write each file as

<<<FILE index.html>>>
...file content...
<<<END FILE>>>

and nothing else before, between or after the files."""


def build_messages(brief: Brief) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "BRIEF (JSON):\n" + brief.to_json() + "\n\nBuild the site now."},
    ]


def feedback_message(report: "CheckReport", truncated: bool) -> str:
    lines = ["Your files failed these checks:"]
    for r in report.reasons + report.advisories:
        lines.append(f"- {r['code']}: {r.get('detail', '')}".rstrip(": "))
    if truncated:
        lines.append("- output_truncated: your answer was cut off at the token limit; write more compact HTML and CSS")
    lines.append("Fix every problem, keep everything else that was good, and output all files again in the same "
                 "format. Remember the hard rules and the honesty rule.")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Parsing the LLM output
# --------------------------------------------------------------------------------------

_FILE_BLOCK = re.compile(r"<<<FILE\s+([^>\s]+)\s*>>>\r?\n?(.*?)\r?\n?<<<END FILE>>>", re.S)
_FENCE_HTML = re.compile(r"```html[ \t]*\r?\n(.*?)\r?\n```", re.S | re.I)


class OutputFormatError(ValueError):
    pass


def parse_output(text: str) -> dict[str, str]:
    """Files from the LLM answer. Falls back to a single ```html fence or a bare HTML document."""
    files: dict[str, str] = {}
    for m in _FILE_BLOCK.finditer(text or ""):
        name = m.group(1).strip()
        if name not in OUTPUT_FILES:
            raise OutputFormatError(f"unexpected file {name!r}; allowed: {', '.join(OUTPUT_FILES)}")
        files[name] = m.group(2)
    if not files:
        fence = _FENCE_HTML.search(text or "")
        if fence:
            files["index.html"] = fence.group(1)
        elif re.match(r"\s*(<!doctype html|<html)", text or "", re.I):
            files["index.html"] = text.strip()
    if "index.html" not in files or not files["index.html"].strip():
        raise OutputFormatError("no index.html in the answer (expected <<<FILE index.html>>> ... <<<END FILE>>>)")
    return files


# --------------------------------------------------------------------------------------
# Deterministic post-processing (the miner owns these facts, not the LLM)
# --------------------------------------------------------------------------------------

_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.I | re.S)
_JSONLD_BLOCK = re.compile(r"""<script\b[^>]*\btype\s*=\s*["']?application/ld\+json["']?[^>]*>.*?</script\s*>""", re.I | re.S)
_SCRIPT_TAG = re.compile(r"</?script\b[^>]*>", re.I)
_TAG = re.compile(r"<[a-zA-Z][^<>]*>", re.S)
_ON_ATTR = re.compile(r"""\s+on[a-zA-Z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""")
_JS_HREF = re.compile(r"""((?:href|src|action|formaction|xlink:href)\s*=\s*)(["']?)\s*javascript:[^"'>\s]*\2""", re.I)
_TEL_HREF = re.compile(r"""(href\s*=\s*)(["']?)(tel:[^"'\s>]*)\2""", re.I)


def jsonld_for(brief: Brief) -> dict:
    """The LocalBusiness block, built only from the brief."""
    node: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": SCHEMA_TYPE[brief.trade],
        "name": brief.name,
        "telephone": "+1-" + "-".join((brief.phone_digits()[:3], brief.phone_digits()[3:6], brief.phone_digits()[6:])),
        "email": brief.email,
    }
    if brief.address:
        street = brief.address.split(",")[0].strip()
        node["address"] = {"@type": "PostalAddress", "streetAddress": street, "addressLocality": brief.city,
                           "addressRegion": brief.state, "addressCountry": "US"}
    node["areaServed"] = [{"@type": "City", "name": f"{town}, {brief.state}"} for town in [brief.city, *brief.service_area]]
    groups: dict[tuple[str, str], list[str]] = {}
    for d in DAYS:
        span = brief.hours[d]
        if span is not None:
            groups.setdefault((span[0], span[1]), []).append(DAY_FULL[d])
    node["openingHoursSpecification"] = [
        {"@type": "OpeningHoursSpecification", "dayOfWeek": days, "opens": o, "closes": c}
        for (o, c), days in groups.items()
    ]
    if brief.facts.founded is not None:
        node["foundingDate"] = str(brief.facts.founded)
    if brief.facts.languages:
        node["knowsLanguage"] = list(brief.facts.languages)
    return node


def jsonld_script(brief: Brief) -> str:
    text = json.dumps(jsonld_for(brief), ensure_ascii=False, separators=(",", ":"))
    text = text.replace("</", "<\\/")  # cannot close the script element early; still valid JSON
    return f'<script type="application/ld+json">{text}</script>'


def _strip_scripting(text: str) -> str:
    text = _SCRIPT_BLOCK.sub("", text)
    text = _SCRIPT_TAG.sub("", text)
    text = _TAG.sub(lambda m: _ON_ATTR.sub("", m.group(0)), text)
    return _JS_HREF.sub(lambda m: f"{m.group(1)}{m.group(2) or chr(34)}#{m.group(2) or chr(34)}", text)


def _ensure_skeleton(doc: str) -> str:
    if not re.search(r"<html\b", doc, re.I):
        doc = f"<html lang=\"en\"><head></head><body>{doc}</body></html>"
    if not re.search(r"<head\b", doc, re.I):
        doc = re.sub(r"(<html\b[^>]*>)", r"\1<head></head>", doc, count=1, flags=re.I)
    if not re.search(r"</head\s*>", doc, re.I):
        doc = re.sub(r"(<head\b[^>]*>)", r"\1</head>", doc, count=1, flags=re.I)
    if not re.search(r"<body\b", doc, re.I):
        doc = re.sub(r"(</head\s*>)", r"\1<body>", doc, count=1, flags=re.I)
    if not re.match(r"\s*<!doctype html", doc, re.I):
        doc = "<!DOCTYPE html>\n" + doc.lstrip()
    if not re.search(r"<html\b[^>]*\blang\s*=", doc, re.I):
        doc = re.sub(r"<html\b", '<html lang="en"', doc, count=1, flags=re.I)
    return doc


def _insert_after_head_open(doc: str, snippet: str) -> str:
    return re.sub(r"(<head\b[^>]*>)", lambda m: m.group(1) + snippet, doc, count=1, flags=re.I)


def _ensure_tel(brief: Brief, doc: str) -> tuple[str, list[str]]:
    want = brief.phone_digits()
    href = "tel:+1" + want
    notes: list[str] = []
    hrefs = [m.group(3) for m in _TEL_HREF.finditer(doc)]
    if any(phone_digits(h[4:].split("?")[0]) == want for h in hrefs):
        return doc, notes
    if hrefs:
        doc = _TEL_HREF.sub(lambda m: f'{m.group(1)}"{href}"', doc)
        notes.append("rewrote tel: links with the wrong digits")
        return doc, notes
    bar = (f'<a class="jm-call" href="{href}" style="display:block;padding:14px 16px;text-align:center;'
           f'font:700 18px/1.3 system-ui,sans-serif;background:#14532d;color:#fff;text-decoration:none">'
           f'Call {html.escape(brief.name)}: {html.escape(brief.phone)}</a>')
    doc = re.sub(r"(<body\b[^>]*>)", lambda m: m.group(1) + bar, doc, count=1, flags=re.I)
    notes.append("added a call bar with the brief's tel: link")
    return doc, notes


def postprocess(brief: Brief, files: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Apply the deterministic fixes. Returns the new files and a list of what was changed."""
    notes: list[str] = []
    out = dict(files)
    doc = out["index.html"]
    doc, n_ld = _JSONLD_BLOCK.subn("", doc)
    if n_ld:
        notes.append("replaced the page's JSON-LD with one built from the brief")
    before = doc
    doc = _strip_scripting(doc)
    if doc != before:
        notes.append("removed scripts, event handlers or javascript: links")
    doc = _ensure_skeleton(doc)
    head_bits = []
    if not re.search(r"<meta\b[^>]*\bcharset\s*=", doc, re.I):
        head_bits.append('<meta charset="utf-8">')
    if not re.search(r"<meta\b[^>]*\bname\s*=\s*[\"']?viewport", doc, re.I):
        head_bits.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
        notes.append("added the viewport meta tag")
    if not re.search(r"<title\b", doc, re.I):
        head_bits.append(f"<title>{html.escape(brief.name)} | {html.escape(brief.city)}, {brief.state}</title>")
    if not re.search(r"<meta\b[^>]*\bname\s*=\s*[\"']?description", doc, re.I):
        desc = f"{brief.name}: {TRADE_LABEL[brief.trade]} in {brief.city}, {brief.state}. Call {brief.phone}."
        head_bits.append(f'<meta name="description" content="{html.escape(desc)}">')
    if head_bits:
        doc = _insert_after_head_open(doc, "".join(head_bits))
    doc = re.sub(r"(</head\s*>)", lambda m: jsonld_script(brief) + m.group(1), doc, count=1, flags=re.I)
    doc, tel_notes = _ensure_tel(brief, doc)
    notes += tel_notes
    out["index.html"] = doc
    if "favicon.svg" in out:
        svg = _strip_scripting(out["favicon.svg"])
        if svg != out["favicon.svg"]:
            notes.append("removed scripting from favicon.svg")
        out["favicon.svg"] = svg
    return out, notes


# --------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------


@dataclass
class CheckReport:
    level: str  # "validator" (validator gates + 390 px render), "validator-static" (gates, no browser), "static"
    gates_passed: bool
    reasons: list[dict] = field(default_factory=list)  # gate failures: fatal after the retry
    advisories: list[dict] = field(default_factory=list)  # 390 px checks and the honesty lint: retry, not fatal
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"level": self.level, "gates_passed": self.gates_passed, "reasons": self.reasons,
                "advisories": self.advisories, "notes": self.notes}


class _TextExtractor(HTMLParser):
    """Approximate visible text without a browser (skips head, script, style, template and [hidden])."""

    SKIP = {"head", "script", "style", "template", "title"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = {k: (v or "") for k, v in attrs}
        hidden = "hidden" in a or re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", a.get("style", ""), re.I)
        if tag in self.VOID:
            if tag == "br":
                self.parts.append("\n")
            return
        if self.skip_stack or tag in self.SKIP or hidden:
            self.skip_stack.append(tag)
        elif tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "section", "tr", "dt", "dd", "header", "footer"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_stack and self.skip_stack[-1] == tag:
            self.skip_stack.pop()

    def handle_data(self, data: str) -> None:
        if not self.skip_stack:
            self.parts.append(data)


def static_visible_text(index_html: str) -> str:
    p = _TextExtractor()
    p.feed(index_html)
    p.close()
    return "".join(p.parts)


_LOCAL_SCHEMES_OK = ("tel:", "mailto:", "sms:", "https://")
_RESOURCE_ATTR = re.compile(r"""\s(src|srcset|poster|data|background)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_HREF_ATTR = re.compile(r"""\shref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_CSS_URL = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*?))\s*\)""", re.I)
_CSS_IMPORT = re.compile(r"""@import\s+(?:url\()?\s*["']?([^"')\s;]+)""", re.I)
_FORBIDDEN_TAG = re.compile(r"<\s*(iframe|frame|frameset|object|embed|applet|base|portal|foreignobject)\b", re.I)


def _is_local(ref: str) -> bool:
    v = ref.strip()
    if not v or v.startswith("#"):
        return True
    if v.startswith("//") or v.startswith("\\"):
        return False
    return ":" not in v.split("#", 1)[0].split("?", 1)[0]


def _css_refs(css: str, where: str, reasons: list[dict]) -> None:
    for m in _CSS_URL.finditer(css):
        ref = next(g for g in m.groups() if g is not None)
        if not _is_local(ref):
            reasons.append({"code": "css_external_url", "detail": f"{where}: {ref[:100]}"})
    for m in _CSS_IMPORT.finditer(css):
        if not _is_local(m.group(1)):
            reasons.append({"code": "css_external_import", "detail": f"{where}: {m.group(1)[:100]}"})


def static_gates(brief: Brief, files: dict[str, str], visible_text: str) -> list[dict]:
    """Cheap approximations of gates 1-4 that need neither html5lib nor a browser."""
    reasons: list[dict] = []
    doc = files["index.html"]
    total = sum(len(v.encode("utf-8")) for v in files.values())
    if total > MAX_UNZIPPED or len(files) > MAX_FILES:
        reasons.append({"code": "bundle_too_large", "detail": f"{total} bytes in {len(files)} files"})
    for name, text in files.items():
        if name.endswith(".css"):
            _css_refs(text, name, reasons)
            continue
        for m in re.finditer(r"<script\b([^>]*)>", text, re.I):
            if name != "index.html" or not re.fullmatch(r"""\s*type\s*=\s*["']?application/ld\+json["']?\s*""", m.group(1), re.I):
                reasons.append({"code": "script_element", "detail": f"{name}: <script{m.group(1)[:60]}>"})
        m = _FORBIDDEN_TAG.search(text)
        if m:
            reasons.append({"code": "forbidden_element", "detail": f"{name}: <{m.group(1).lower()}>"})
        if re.search(r"http-equiv\s*=\s*[\"']?refresh", text, re.I):
            reasons.append({"code": "meta_refresh", "detail": name})
        for tag in _TAG.finditer(text):
            t = tag.group(0)
            if _ON_ATTR.search(t):
                reasons.append({"code": "event_handler_attribute", "detail": f"{name}: {t[:80]}"})
            for rm in _RESOURCE_ATTR.finditer(t):
                val = next(g for g in rm.groups()[1:] if g is not None)
                urls = [u.strip().split()[0] for u in val.split(",") if u.strip()] if rm.group(1).lower() == "srcset" else [val]
                for u in urls:
                    if not _is_local(u):
                        reasons.append({"code": "external_resource", "detail": f"{name}: {u[:100]}"})
            for hm in _HREF_ATTR.finditer(t):
                val = next(g for g in hm.groups() if g is not None).strip()
                is_anchor = re.match(r"<\s*(a|area)\b", t, re.I) is not None
                if _is_local(val):
                    continue
                if is_anchor and val.lower().startswith(_LOCAL_SCHEMES_OK):
                    continue
                reasons.append({"code": "external_resource" if not is_anchor else "disallowed_link_scheme",
                                "detail": f"{name}: {val[:100]}"})
            sm = re.search(r"""\sstyle\s*=\s*(?:"([^"]*)"|'([^']*)')""", t, re.I)
            if sm:
                _css_refs(next(g for g in sm.groups() if g is not None), name, reasons)
        for sm in re.finditer(r"<style\b[^>]*>(.*?)</style\s*>", text, re.I | re.S):
            _css_refs(sm.group(1), name, reasons)
    # gate 3 approximations
    vt = normalise_text(visible_text)
    if normalise_text(brief.name) not in vt:
        reasons.append({"code": "name_not_visible", "detail": brief.name})
    if not any(phone_digits(m.group(0)) == brief.phone_digits() for m in PHONE_TOKEN.finditer(visible_text)):
        reasons.append({"code": "phone_not_visible", "detail": brief.phone})
    for t in sorted({t for span in brief.hours.values() if span for t in span}):
        if not time_pattern(t).search(visible_text):
            reasons.append({"code": "hours_time_not_visible", "detail": t})
    tels = [m.group(3) for m in _TEL_HREF.finditer(doc)]
    if not any(phone_digits(h[4:].split("?")[0]) == brief.phone_digits() for h in tels):
        reasons.append({"code": "no_matching_tel_link", "detail": f"found {tels[:5]}"})
    return _dedupe(reasons)


# Honesty lint: phrases that are usually invented. A phrase is ignored when the brief itself contains it.
_LINT = (
    ("rating", r"★|☆|\b[1-5](?:\.\d)?[- ]?stars?\b|\bstar[- ]rated\b|\brated\s+\d|\btop[- ]rated\b"),
    ("review_count", r"\b\d[\d,]*\+?\s+(?:\w+\s+)?(?:reviews|ratings)\b"),
    ("testimonial", r"\btestimonials?\b|\bwhat (?:our|my) (?:customers|clients|patients|neighbors|neighbours) say\b"),
    ("customer_quote", r"[\"“][^\"”\n]{25,}[\"”]\s*(?:[-—–]|<)\s*[A-Z][a-z]+"),
    ("award", r"\baward[- ]winning\b|\bawards?\b|#\s?1\b|\bnumber one\b|\bbest in\b|\bvoted\b"),
    ("price", r"\$\s?\d|\b\d+\s?% off\b|\bdiscounts?\b|\bcoupons?\b|\bfinancing\b"),
    ("free_offer", r"\bfree\s+(?:estimates?|quotes?|consultations?|inspections?|service calls?)\b"),
    ("guarantee", r"\bguarantee[ds]?\b|\bwarrant(?:y|ies)\b"),
    ("certification", r"\bcertified\b|\bcertifications?\b|\baccredited\b|\bBBB\b|\bmembers? of\b"),
    ("licensed", r"\blicensed\b"),
    ("insured", r"\binsured\b"),
    ("always_open", r"\b24/7\b|\b24 hours\b|\bsame[- ]day\b|\baround the clock\b"),
    ("emergency", r"\bemergenc(?:y|ies)\b"),
)
_YEAR_CLAIM = re.compile(r"\b(?:since|established|est\.?|founded(?: in)?|serving [^.]{0,40}? since)\s+((?:19|20)\d{2})\b", re.I)
_YEARS_IN_BUSINESS = re.compile(r"\b\d+\+?\s+years\b", re.I)


def honesty_lint(brief: Brief, visible_text: str) -> list[dict]:
    """Heuristic flags for claims the brief does not support. Advisory: triggers a retry, never fatal."""
    data = brief.model_dump(mode="json")
    data.pop("facts", None)  # facts are allowed by meaning below, not by wording ("insured": false is no licence to say insured)
    brief_text = json.dumps(data, ensure_ascii=False).lower()
    backed = {"licensed": bool(brief.facts.license), "insured": brief.facts.insured is True,
              "emergency": brief.facts.emergency is True}
    found: list[dict] = []
    for code, pattern in _LINT:
        if backed.get(code):
            continue
        for m in re.finditer(pattern, visible_text, re.I):
            phrase = m.group(0).strip()
            if phrase.lower() in brief_text:
                continue
            found.append({"code": f"possible_invented_claim:{code}", "detail": phrase[:80]})
            break
    for m in _YEAR_CLAIM.finditer(visible_text):
        if brief.facts.founded is None or int(m.group(1)) != brief.facts.founded:
            found.append({"code": "possible_invented_claim:year", "detail": m.group(0)[:80]})
            break
    if brief.facts.founded is None:
        m = _YEARS_IN_BUSINESS.search(visible_text)
        if m:
            found.append({"code": "possible_invented_claim:years_in_business", "detail": m.group(0)})
    return found


def _dedupe(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


def validator_available() -> bool:
    try:
        import html5lib  # noqa: F401
        import playwright  # noqa: F401

        from ..validator import gates  # noqa: F401
    except ImportError:
        return False
    return True


def check_files(brief: Brief, files: dict[str, str], level: str = "auto") -> CheckReport:
    """Check generated files: with the validator's gates when available (or forced), otherwise statically."""
    if level == "auto":
        level = "validator" if validator_available() else "static"
    if level == "static":
        text = static_visible_text(files["index.html"])
        reasons = static_gates(brief, files, text)
        return CheckReport("static", not reasons, reasons, honesty_lint(brief, text))
    if level != "validator":
        raise ValueError(f"unknown check level {level!r}")
    return _validator_check(brief, files)


def _validator_check(brief: Brief, files: dict[str, str]) -> CheckReport:
    from ..bundle import write_files
    from ..validator.gates import gate_facts, gate_no_reviews, gate_no_script
    from ..validator.render import render_site

    b64 = pack_files({k: v.encode("utf-8") for k, v in files.items()})
    try:
        bundle = unpack(b64)
    except BundleRejected as exc:
        return CheckReport("validator", False, [r.as_dict() for r in exc.reasons])
    reasons: list[dict] = []
    advisories: list[dict] = []
    notes: list[str] = []
    level = "validator"
    g2 = gate_no_script(bundle)
    rr = None
    with tempfile.TemporaryDirectory(prefix="jm-miner-check-") as tmp:
        site = write_files(bundle.files, Path(tmp) / "site")
        try:
            rr = render_site(site, Path(tmp) / "shots", phone_digits=brief.phone_digits())
        except Exception as exc:  # no browser here: keep the static parts of the gates
            level = "validator-static"
            notes.append(f"render unavailable ({type(exc).__name__}); visible text approximated without a browser")
    text = rr.visible_text if rr is not None else static_visible_text(files["index.html"])
    if rr is not None and rr.blocked_requests:
        g2.passed = False
        g2.reasons.append({"code": "external_request_at_render", "detail": ", ".join(rr.blocked_requests[:5])})
    for g in (g2, gate_facts(brief, bundle, text), gate_no_reviews(bundle)):
        if not g.passed:
            reasons += [{**r, "gate": g.name} for r in g.reasons]
    if rr is not None:
        if not rr.no_hscroll:
            advisories.append({"code": "horizontal_scroll_at_390px",
                               "detail": f"page is {rr.scroll_width}px wide on a 390px screen"})
        if not rr.tel_visible:
            why = "; ".join(f"{t['href']}: {t['why'] or 'wrong digits'}" for t in rr.tel_links_checked[:3]) or "no tel: link"
            advisories.append({"code": "call_link_not_on_first_screen", "detail": why})
    advisories += honesty_lint(brief, text)
    return CheckReport(level, not reasons, _dedupe(reasons), _dedupe(advisories), notes)


# --------------------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------------------


@dataclass
class GenerationResult:
    files: dict[str, str]
    bundle_b64: str
    bundle_sha256: str
    report: CheckReport
    usage: Usage
    attempts: list[dict]
    model: str
    miner_version: str = MINER_VERSION

    @property
    def warnings(self) -> list[dict]:
        return self.report.advisories

    def summary(self) -> dict:
        return {"ok": True, "miner_version": self.miner_version, "model": self.model,
                "bundle_sha256": self.bundle_sha256, "files": {k: len(v.encode("utf-8")) for k, v in self.files.items()},
                "check": self.report.as_dict(), "usage": self.usage.as_dict(), "attempts": self.attempts}


def resolve_model(model: Optional[str] = None) -> str:
    return model or os.environ.get(ENV_MODEL, "").strip() or DEFAULT_MODEL


def generate_site(brief: Brief, *, client: Optional[LLMClient] = None, model: Optional[str] = None,
                  check: str = "auto", max_attempts: int = 2, deadline: Optional[float] = None,
                  temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS,
                  seed: Optional[int] = None, clock=time.monotonic) -> GenerationResult:
    """Brief -> checked site. Raises ``GenerationError`` (or ``LLMConfigError`` without a key)."""
    client = client or LLMClient()
    model = resolve_model(model)
    messages = build_messages(brief)
    usage = Usage()
    attempts: list[dict] = []
    best: Optional[tuple[dict[str, str], CheckReport]] = None
    last_reasons: list[dict] = []

    for n in range(1, max_attempts + 1):
        try:
            res: ChatResult = client.chat(model, messages, temperature=temperature, seed=seed, max_tokens=max_tokens,
                                          deadline=deadline)
        except LLMError as exc:
            attempts.append({"attempt": n, "llm_error": exc.kind, "detail": str(exc)})
            if best is not None:
                break
            raise GenerationError("llm_failed", f"LLM call failed: {exc}", usage=usage, attempts=attempts) from None
        usage = usage.add(res.usage)
        rec: dict[str, Any] = {"attempt": n, "llm": res.as_record()}
        truncated = res.finish_reason == "length"
        try:
            raw_files = parse_output(res.text)
        except OutputFormatError as exc:
            report = CheckReport("format", False, [{"code": "bad_output_format", "detail": str(exc)}])
        else:
            files, notes = postprocess(brief, raw_files)
            report = check_files(brief, files, check)
            report.notes = notes + report.notes
            if report.gates_passed and (best is None or len(report.advisories) < len(best[1].advisories)):
                best = (files, report)
        rec["check"] = report.as_dict()
        attempts.append(rec)
        last_reasons = report.reasons
        if report.gates_passed and not report.advisories:
            break
        if n == max_attempts:
            break
        if deadline is not None and deadline - clock() < MIN_SECONDS_FOR_RETRY:
            attempts.append({"attempt": n + 1, "skipped": "not enough time left for a retry"})
            break
        messages = messages + [{"role": "assistant", "content": res.text},
                               {"role": "user", "content": feedback_message(report, truncated)}]

    if best is None:
        raise GenerationError("checks_failed",
                              f"no attempt passed the gates after {len([a for a in attempts if 'llm' in a])} LLM call(s): "
                              + "; ".join(f"{r['code']}: {r.get('detail', '')}" for r in last_reasons[:8]),
                              reasons=last_reasons, usage=usage, attempts=attempts)
    files, report = best
    b64 = pack_files({k: v.encode("utf-8") for k, v in files.items()})
    sha = hashlib.sha256(base64.b64decode(b64)).hexdigest()
    return GenerationResult(files=files, bundle_b64=b64, bundle_sha256=sha, report=report, usage=usage,
                            attempts=attempts, model=model)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def write_site(files: dict[str, str], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty")
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jmercury.miner.generate",
                                 description="Reference miner: build a site for a brief with an OpenRouter model.")
    ap.add_argument("--brief", required=True, help="brief JSON file (python -m jmercury.brief --seed N > brief.json)")
    ap.add_argument("--out", help="write the site files into this directory (must be empty or absent)")
    ap.add_argument("--b64", help="write the base64 zip bundle (what a miner sends) to this file")
    ap.add_argument("--model", help=f"OpenRouter model slug (default: ${ENV_MODEL} or {DEFAULT_MODEL})")
    ap.add_argument("--check", choices=("auto", "validator", "static"), default="auto",
                    help="self-check: validator gates + 390 px render (needs the validator extra), static, or auto")
    ap.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--seed", type=int, help="sampling seed passed to the model (not all providers honour it)")
    ap.add_argument("--timeout", type=float, default=170.0, help="overall time budget in seconds (default 170)")
    args = ap.parse_args(argv)
    if not args.out and not args.b64:
        ap.error("give --out DIR and/or --b64 FILE")

    brief = Brief.model_validate_json(Path(args.brief).read_text(encoding="utf-8"))
    try:
        result = generate_site(brief, model=args.model, check=args.check, temperature=args.temperature,
                               max_tokens=args.max_tokens, seed=args.seed, deadline=time.monotonic() + args.timeout)
    except LLMConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except GenerationError as exc:
        print(json.dumps({"ok": False, **exc.as_dict()}, indent=2))
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.out:
        write_site(result.files, Path(args.out))
    if args.b64:
        Path(args.b64).write_text(result.bundle_b64 + "\n", encoding="ascii")
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
