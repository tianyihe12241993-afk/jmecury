"""Pairwise LLM judge (DESIGN.md section 3.3, RUBRIC.md).

The judge prompt embeds ``docs/RUBRIC.md`` verbatim, loaded from the file at run time, so
the published rubric and the prompt cannot drift apart.

Each pair is judged twice with the order swapped. A win counts only if both orders agree;
anything else is a tie. A site is flagged for invented claims only if its invented-claims
list is non-empty in both orders. A reply that does not match the output format counts as a
tie with no flags. The reply may be the bare JSON object or that object inside exactly one
fenced ```json block; anything else around it is a format failure.

``OpenRouterJudge`` calls the model named by ``JM_JUDGE_MODEL`` through ``jmercury.llm``
(OpenRouter, key in ``OPENROUTER_API_KEY``) at temperature 0. There is deliberately no default
model: it will be chosen on testnet by measured self-agreement. ``FakeJudge`` returns scripted
replies for tests.

Images per call: for each site, the phone first screen (390 x 844), the phone full page in up to
three parts of at most 390 x 1560 (``render.FULL_PART_HEIGHT``; the page is capped at 4000 px) and
the desktop first screen (1280 x 800): 6 to 10 images per call.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

from ..llm import LLMClient, LLMConfigError, Usage, image_part, text_part

RUBRIC_PATH = Path(__file__).resolve().parents[2] / "docs" / "RUBRIC.md"
CRITERIA = ("clarity", "facts", "fit", "phone", "professional")
CHOICES = ("A", "B", "tie")
MAX_TEXT_CHARS = 12000
ENV_MODEL = "JM_JUDGE_MODEL"
JUDGE_MAX_TOKENS = 1500


def load_rubric() -> str:
    return RUBRIC_PATH.read_text(encoding="utf-8")


SYSTEM_TEMPLATE = """You are the judge for the J Mercury subnet. You compare two websites, Site A and Site B, built for \
the same local-business brief. Apply the rubric below exactly. Reply with ONLY the JSON object described in its \
"Output format" section: no text before or after it.

--- RUBRIC (docs/RUBRIC.md, verbatim) ---
{rubric}
--- END RUBRIC ---"""


@dataclass
class SiteView:
    """What the judge sees of one site."""

    visible_text: str
    mobile_first_png: bytes
    mobile_full_png: bytes
    desktop_first_png: bytes
    # The full phone page in parts of at most 390 x 1560; empty means: send ``mobile_full_png`` whole.
    mobile_full_parts: list[bytes] = field(default_factory=list)

    @classmethod
    def from_paths(cls, visible_text: str, screenshots: dict[str, str]) -> "SiteView":
        part_keys = sorted((k for k in screenshots if re.fullmatch(r"mobile_full_\d+", k)), key=lambda k: int(k.rsplit("_", 1)[1]))
        return cls(
            visible_text=visible_text,
            mobile_first_png=Path(screenshots["mobile_first"]).read_bytes(),
            mobile_full_png=Path(screenshots["mobile_full"]).read_bytes(),
            desktop_first_png=Path(screenshots["desktop_first"]).read_bytes(),
            mobile_full_parts=[Path(screenshots[k]).read_bytes() for k in part_keys],
        )

    def full_page_images(self) -> list[bytes]:
        return list(self.mobile_full_parts) or [self.mobile_full_png]


def build_messages(brief_json: str, a: SiteView, b: SiteView, rubric: Optional[str] = None) -> list[dict]:
    rubric = rubric if rubric is not None else load_rubric()
    content: list[dict] = [text_part("BRIEF (JSON):\n" + brief_json)]
    for label, site in (("A", a), ("B", b)):
        text = site.visible_text
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "\n[... visible text truncated ...]"
        content += [text_part(f"SITE {label}: phone first screen (390 x 844)"), image_part(site.mobile_first_png)]
        parts = site.full_page_images()
        for i, png in enumerate(parts, start=1):
            suffix = f", part {i} of {len(parts)}, top to bottom" if len(parts) > 1 else ""
            content += [text_part(f"SITE {label}: phone full page{suffix}"), image_part(png)]
        content += [
            text_part(f"SITE {label}: desktop first screen (1280 x 800)"),
            image_part(site.desktop_first_png),
            text_part(f"SITE {label}: visible text\n<<<\n{text}\n>>>"),
        ]
    content.append(text_part("Compare Site A and Site B now. Reply with the JSON object only."))
    return [
        {"role": "system", "content": SYSTEM_TEMPLATE.format(rubric=rubric)},
        {"role": "user", "content": content},
    ]


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


@dataclass
class Verdict:
    """One judging order, in A/B terms."""

    valid: bool
    winner: str = "tie"
    criteria: dict[str, str] = field(default_factory=dict)
    invented: dict[str, list[str]] = field(default_factory=lambda: {"A": [], "B": []})
    reason: str = ""
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {"valid": self.valid, "winner": self.winner, "criteria": self.criteria,
                "invented_claims": self.invented, "reason": self.reason, "error": self.error}


_FENCED = re.compile(r"\A\s*```json[ \t]*\r?\n(.*?)\r?\n[ \t]*```\s*\Z", re.S)


def unwrap_reply(raw: str) -> str:
    """The bare JSON text: the reply itself, or the inside of exactly one ```json fence that is the whole reply."""
    m = _FENCED.match(raw)
    if m and "```" not in m.group(1):
        return m.group(1)
    return raw


def parse_reply(raw: Optional[str]) -> Verdict:
    """Strict: the reply must be one JSON object of exactly the rubric's shape, bare or in one ```json fence."""

    def bad(msg: str) -> Verdict:
        return Verdict(valid=False, error=msg)

    if not isinstance(raw, str):
        return bad("no reply text")
    try:
        obj = json.loads(unwrap_reply(raw).strip())
    except (json.JSONDecodeError, ValueError) as exc:
        return bad(f"not JSON: {exc}")
    if not isinstance(obj, dict):
        return bad("reply is not a JSON object")
    if set(obj) != {"criteria", "invented_claims", "winner", "reason"}:
        return bad(f"wrong top-level keys {sorted(obj)}")
    crit = obj["criteria"]
    if not isinstance(crit, dict) or set(crit) != set(CRITERIA):
        return bad("criteria must have exactly clarity, facts, fit, phone, professional")
    if any(v not in CHOICES for v in crit.values()):
        return bad("criteria values must be A, B or tie")
    inv = obj["invented_claims"]
    if not isinstance(inv, dict) or set(inv) != {"A", "B"}:
        return bad("invented_claims must have exactly keys A and B")
    for k in ("A", "B"):
        if not isinstance(inv[k], list) or not all(isinstance(x, str) for x in inv[k]):
            return bad(f"invented_claims.{k} must be a list of strings")
    if obj["winner"] not in CHOICES:
        return bad("winner must be A, B or tie")
    if not isinstance(obj["reason"], str):
        return bad("reason must be a string")
    invented = {k: [x for x in inv[k] if x.strip()] for k in ("A", "B")}
    return Verdict(valid=True, winner=obj["winner"], criteria=dict(crit), invented=invented, reason=obj["reason"])


# --------------------------------------------------------------------------------------
# Judges
# --------------------------------------------------------------------------------------


class Judge(Protocol):
    def compare(self, brief_json: str, a: SiteView, b: SiteView) -> str:
        """Return the raw reply text for one order (A = first site shown)."""


class JudgeConfigError(RuntimeError):
    pass


MISSING_MODEL_MESSAGE = (
    f"{ENV_MODEL} is not set. There is no default judge model yet: it will be chosen on testnet by measuring how "
    "often a model agrees with itself on repeated, identical comparisons (docs/DESIGN.md section 3.3). Set "
    f"{ENV_MODEL} to an OpenRouter model slug to run the judge"
)


class OpenRouterJudge:
    """The validator's judge: one OpenRouter chat call per order, temperature 0."""

    def __init__(self, client: Optional[LLMClient] = None, model: Optional[str] = None, *, timeout: float = 180.0,
                 seed: Optional[int] = 0, max_tokens: int = JUDGE_MAX_TOKENS):
        self.model = model or os.environ.get(ENV_MODEL, "").strip()
        if not self.model:
            raise JudgeConfigError(MISSING_MODEL_MESSAGE)
        try:
            self.client = client or LLMClient(timeout=timeout)
        except LLMConfigError as exc:
            raise JudgeConfigError(str(exc)) from None
        self.timeout = timeout
        self.seed = seed
        self.max_tokens = max_tokens
        self.usage = Usage()
        self.calls: list[dict] = []  # per call: requested/answering model, provider, usage (no content)

    def __repr__(self) -> str:  # never show the key
        return f"OpenRouterJudge(model={self.model!r})"

    def compare(self, brief_json: str, a: SiteView, b: SiteView) -> str:
        res = self.client.chat(self.model, build_messages(brief_json, a, b), temperature=0.0, seed=self.seed,
                               max_tokens=self.max_tokens, timeout=self.timeout)
        self.usage = self.usage.add(res.usage)
        self.calls.append(res.as_record())
        return res.text

    def record(self) -> dict:
        return {"model": self.model, "calls": len(self.calls), "usage": self.usage.as_dict(),
                "answered_by": sorted({f"{c['model']} via {c['provider']}" for c in self.calls})}


class FakeJudge:
    """Scripted judge for tests: ``fn(brief_json, a, b) -> raw reply`` or a list of replies in call order."""

    def __init__(self, replies: Optional[list[str]] = None, fn: Optional[Callable[[str, SiteView, SiteView], str]] = None):
        self.replies = list(replies or [])
        self.fn = fn
        self.calls: list[tuple[str, str]] = []  # (first line of A's text, first line of B's text)

    def compare(self, brief_json: str, a: SiteView, b: SiteView) -> str:
        self.calls.append((a.visible_text[:40], b.visible_text[:40]))
        if self.fn is not None:
            return self.fn(brief_json, a, b)
        if not self.replies:
            raise RuntimeError("FakeJudge ran out of scripted replies")
        return self.replies.pop(0)


def reply(winner: str = "tie", invented_a: Optional[list[str]] = None, invented_b: Optional[list[str]] = None,
          criteria: Optional[dict[str, str]] = None, reason: str = "test") -> str:
    """Build a well-formed reply string (helper for tests and fakes)."""
    return json.dumps({
        "criteria": criteria or {c: winner for c in CRITERIA},
        "invented_claims": {"A": invented_a or [], "B": invented_b or []},
        "winner": winner,
        "reason": reason,
    })


# --------------------------------------------------------------------------------------
# Both orders
# --------------------------------------------------------------------------------------


@dataclass
class PairResult:
    x: str
    y: str
    order1: Verdict  # x shown as A
    order2: Verdict  # y shown as A
    winner: Optional[str]  # x, y, or None for a tie
    invented_confirmed: dict[str, bool]
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "x": self.x, "y": self.y,
            "order1_x_is_A": self.order1.as_dict(),
            "order2_y_is_A": self.order2.as_dict(),
            "winner": self.winner,
            "invented_confirmed": self.invented_confirmed,
            "errors": self.errors,
        }


def _call(judge: Judge, brief_json: str, a: SiteView, b: SiteView, errors: list[str]) -> Verdict:
    try:
        raw = judge.compare(brief_json, a, b)
    except Exception as exc:  # a failed call is treated like a malformed reply: a tie
        errors.append(f"{type(exc).__name__}: {exc}")
        return Verdict(valid=False, error="call failed")
    return parse_reply(raw)


def judge_pair(judge: Judge, brief_json: str, x_id: str, x: SiteView, y_id: str, y: SiteView) -> PairResult:
    errors: list[str] = []
    v1 = _call(judge, brief_json, x, y, errors)  # x = A, y = B
    v2 = _call(judge, brief_json, y, x, errors)  # y = A, x = B
    w1 = {"A": x_id, "B": y_id}.get(v1.winner)
    w2 = {"A": y_id, "B": x_id}.get(v2.winner)
    winner = w1 if (w1 is not None and w1 == w2) else None
    invented = {
        x_id: bool(v1.invented["A"]) and bool(v2.invented["B"]),
        y_id: bool(v1.invented["B"]) and bool(v2.invented["A"]),
    }
    return PairResult(x=x_id, y=y_id, order1=v1, order2=v2, winner=winner, invented_confirmed=invented, errors=errors)
