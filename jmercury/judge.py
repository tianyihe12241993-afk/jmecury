"""Pairwise LLM judge (DESIGN.md section 3.3, RUBRIC.md).

The judge prompt embeds ``docs/RUBRIC.md`` verbatim, loaded from the file at run time, so
the published rubric and the prompt cannot drift apart.

Each pair is judged twice with the order swapped. A win counts only if both orders agree;
anything else is a tie. A site is flagged for invented claims only if its invented-claims
list is non-empty in both orders. A reply that does not match the output format exactly
counts as a tie with no flags.

``OpenAIJudge`` talks to any OpenAI-compatible ``/chat/completions`` endpoint configured by
JM_JUDGE_BASE_URL, JM_JUDGE_MODEL and JM_JUDGE_API_KEY. The key is never logged or stored.
``FakeJudge`` returns scripted replies for tests.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

RUBRIC_PATH = Path(__file__).resolve().parent.parent / "docs" / "RUBRIC.md"
CRITERIA = ("clarity", "facts", "fit", "phone", "professional")
CHOICES = ("A", "B", "tie")
MAX_TEXT_CHARS = 12000
ENV_VARS = ("JM_JUDGE_BASE_URL", "JM_JUDGE_MODEL", "JM_JUDGE_API_KEY")


def load_rubric() -> str:
    return RUBRIC_PATH.read_text(encoding="utf-8")


SYSTEM_TEMPLATE = """You are the judge for the J Mercury subnet. You compare two websites, Site A and Site B, built for \
the same local-business brief. Apply the rubric below exactly. Reply with ONLY the JSON object described in its \
"Output format" section: no code fences, no text before or after it.

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

    @classmethod
    def from_paths(cls, visible_text: str, screenshots: dict[str, str]) -> "SiteView":
        return cls(
            visible_text=visible_text,
            mobile_first_png=Path(screenshots["mobile_first"]).read_bytes(),
            mobile_full_png=Path(screenshots["mobile_full"]).read_bytes(),
            desktop_first_png=Path(screenshots["desktop_first"]).read_bytes(),
        )


def _img(png: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}}


def build_messages(brief_json: str, a: SiteView, b: SiteView, rubric: Optional[str] = None) -> list[dict]:
    rubric = rubric if rubric is not None else load_rubric()
    content: list[dict] = [{"type": "text", "text": "BRIEF (JSON):\n" + brief_json}]
    for label, site in (("A", a), ("B", b)):
        text = site.visible_text
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "\n[... visible text truncated ...]"
        content += [
            {"type": "text", "text": f"SITE {label}: phone first screen (390 x 844)"},
            _img(site.mobile_first_png),
            {"type": "text", "text": f"SITE {label}: phone full page"},
            _img(site.mobile_full_png),
            {"type": "text", "text": f"SITE {label}: desktop first screen (1280 x 800)"},
            _img(site.desktop_first_png),
            {"type": "text", "text": f"SITE {label}: visible text\n<<<\n{text}\n>>>"},
        ]
    content.append({"type": "text", "text": "Compare Site A and Site B now. Reply with the JSON object only."})
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


def parse_reply(raw: Optional[str]) -> Verdict:
    """Strict: the whole reply must be one JSON object of exactly the rubric's shape."""

    def bad(msg: str) -> Verdict:
        return Verdict(valid=False, error=msg)

    if not isinstance(raw, str):
        return bad("no reply text")
    try:
        obj = json.loads(raw.strip())
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


class OpenAIJudge:
    """OpenAI-compatible /chat/completions client. Reads its settings from the environment."""

    def __init__(self, timeout: float = 180.0, temperature: float = 0.0):
        missing = [v for v in ENV_VARS if not os.environ.get(v)]
        if missing:
            raise JudgeConfigError("the judge needs these environment variables: " + ", ".join(missing))
        self.base_url = os.environ["JM_JUDGE_BASE_URL"].rstrip("/")
        self.model = os.environ["JM_JUDGE_MODEL"]
        self._key = os.environ["JM_JUDGE_API_KEY"]
        self.timeout = timeout
        self.temperature = temperature

    def __repr__(self) -> str:  # never show the key
        return f"OpenAIJudge(base_url={self.base_url!r}, model={self.model!r})"

    def compare(self, brief_json: str, a: SiteView, b: SiteView) -> str:
        body = json.dumps({
            "model": self.model,
            "temperature": self.temperature,
            "messages": build_messages(brief_json, a, b),
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"judge endpoint returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"judge endpoint unreachable: {type(exc).__name__}") from None
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("judge endpoint reply has no choices[0].message.content") from None


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
