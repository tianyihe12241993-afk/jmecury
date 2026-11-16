"""Local scorer: gates + measured quality per site, pairwise judging and task scores per round.

    python -m jmercury.validator.score --brief brief.json --site ./my-site/            # gates + measured
    python -m jmercury.validator.score --brief brief.json --site ./a/ --vs ./b/ --judge # plus one pairwise judgement

Task score (DESIGN.md section 3.4):
    0                    if any gate fails, invented claims are confirmed in at least half of the site's pairs,
                         or the site duplicates an earlier one
    0.4 * M + 0.6 * J    otherwise
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import tempfile
import time
from itertools import combinations
from pathlib import Path
from typing import Any, Optional

from .. import __version__
from ..brief import Brief
from ..bundle import Bundle, BundleRejected, pack_dir, unpack, write_files
from .gates import GateResult, gate_facts, gate_no_reviews, gate_no_script, normalise_text
from .judge import FakeJudge, Judge, JudgeConfigError, OpenRouterJudge, SiteView, judge_pair  # noqa: F401 - re-export
from .measure import WEIGHTS, MeasureUnavailable, measure
from .render import render_origin, serve_dir

W_MEASURED = 0.4
W_JUDGED = 0.6
GATE_ORDER = ("bundle", "no_script", "facts", "no_fake_reviews")


def _not_run(name: str, why: str) -> dict:
    return {"gate": name, "passed": False, "reasons": [{"code": "not_run", "detail": why}]}


def score_site(brief: Brief, bundle_b64: str, *, out_dir: Optional[str | Path] = None, runs: int = 3,
               do_measure: bool = True) -> dict[str, Any]:
    """Gates 1-4 and measured quality M for one site. Screenshots are written under ``out_dir``."""
    t0 = time.monotonic()
    timing: dict[str, float] = {}
    out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="jm-site-"))
    out.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "bundle_sha256": None,
        "gates": {},
        "gates_passed": False,
        "render": None,
        "visible_text": None,
        "measured": None,
        "M": None,
        "artifacts_dir": str(out),
    }

    try:
        bundle: Bundle = unpack(bundle_b64)
    except BundleRejected as exc:
        try:
            result["bundle_sha256"] = hashlib.sha256(base64.b64decode("".join(bundle_b64.split()), validate=True)).hexdigest()
        except Exception:
            result["bundle_sha256"] = None
        result["gates"]["bundle"] = {"gate": "bundle", "passed": False, "reasons": [r.as_dict() for r in exc.reasons]}
        for g in GATE_ORDER[1:]:
            result["gates"][g] = _not_run(g, "bundle gate failed")
        timing["total"] = round(time.monotonic() - t0, 3)
        result["timing_s"] = timing
        return result

    result["bundle_sha256"] = bundle.sha256
    result["bundle_files"] = bundle.names
    result["gates"]["bundle"] = GateResult("bundle", True, []).as_dict()
    g2 = gate_no_script(bundle)
    timing["static_gates"] = round(time.monotonic() - t0, 3)

    with tempfile.TemporaryDirectory(prefix="jm-bundle-") as tmp:
        site_root = write_files(bundle.files, Path(tmp) / "site")
        with serve_dir(site_root) as origin:
            t1 = time.monotonic()
            try:
                rr = render_origin(origin, out, phone_digits=brief.phone_digits())
            except Exception as exc:  # a page that cannot be rendered fails the facts gate
                rr = None
                result["render"] = {"error": f"{type(exc).__name__}: {str(exc)[:500]}"}
            timing["render"] = round(time.monotonic() - t1, 3)

            if rr is not None:
                result["render"] = rr.as_dict(include_text=False)
                result["visible_text"] = rr.visible_text
                if rr.blocked_requests:
                    # Dynamic backstop for gate 2: the static checks missed something that fetched off-origin.
                    g2.passed = False
                    g2.reasons.append({"code": "external_request_at_render", "detail": ", ".join(rr.blocked_requests[:5])})
            result["gates"]["no_script"] = g2.as_dict()
            g3 = gate_facts(brief, bundle, rr.visible_text if rr is not None else None)
            g4 = gate_no_reviews(bundle)
            result["gates"]["facts"] = g3.as_dict()
            result["gates"]["no_fake_reviews"] = g4.as_dict()
            result["gates_passed"] = all(result["gates"][g]["passed"] for g in GATE_ORDER)

            # Lighthouse runs Chrome with JavaScript on, so it only ever sees gate-passing sites.
            if result["gates_passed"] and do_measure and rr is not None:
                t2 = time.monotonic()
                try:
                    m = measure(origin + "/index.html", no_hscroll=rr.no_hscroll, tel_visible=rr.tel_visible, runs=runs)
                    result["measured"] = m.as_dict()
                    result["M"] = m.M
                except MeasureUnavailable as exc:
                    result["measured"] = {"error": "unavailable", "detail": str(exc)}
                except Exception as exc:
                    result["measured"] = {"error": "lighthouse_failed", "detail": f"{type(exc).__name__}: {str(exc)[:800]}"}
                timing["measure"] = round(time.monotonic() - t2, 3)
            elif rr is not None:
                result["measured"] = {"skipped": "gates failed" if not result["gates_passed"] else "measurement disabled",
                                      "no_hscroll": rr.no_hscroll, "tel_visible": rr.tel_visible}

    timing["total"] = round(time.monotonic() - t0, 3)
    result["timing_s"] = timing
    return result


def _text_key(visible_text: Optional[str]) -> Optional[str]:
    if not visible_text:
        return None
    return hashlib.sha256(normalise_text(visible_text).encode("utf-8")).hexdigest()


def score_round(brief: Brief, responses: dict[str, tuple[str, float]], judge: Optional[Judge], *,
                out_dir: Optional[str | Path] = None, runs: int = 3, do_measure: bool = True,
                site_results: Optional[dict[str, dict]] = None) -> dict[str, Any]:
    """Score one round. ``responses`` maps uid -> (bundle_b64, response_time_seconds).

    ``site_results`` may supply precomputed ``score_site`` outputs (keyed by uid) to skip re-rendering.
    With ``judge=None`` no pairwise judging is done and J / task scores are left as None.
    """
    t0 = time.monotonic()
    out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="jm-round-"))
    out.mkdir(parents=True, exist_ok=True)
    brief_json = brief.to_json(indent=None)
    sites: dict[str, dict] = {}
    for uid in sorted(responses):
        b64, _ = responses[uid]
        if site_results and uid in site_results:
            sites[uid] = site_results[uid]
        else:
            sites[uid] = score_site(brief, b64, out_dir=out / f"uid_{uid}", runs=runs, do_measure=do_measure)

    # Duplicate rule: identical normalised visible text -> only the earliest response keeps its score.
    duplicate_of: dict[str, str] = {}
    groups: dict[str, list[str]] = {}
    for uid, s in sites.items():
        key = _text_key(s.get("visible_text"))
        s["visible_text_sha256"] = key
        if key:
            groups.setdefault(key, []).append(uid)
    for members in groups.values():
        members.sort(key=lambda u: (responses[u][1], u))
        for later in members[1:]:
            duplicate_of[later] = members[0]

    judged = [u for u in sorted(sites) if sites[u]["gates_passed"] and u not in duplicate_of]
    pairs = []
    wins = {u: 0.0 for u in judged}
    ties = {u: 0.0 for u in judged}
    comps = {u: 0 for u in judged}
    invented: dict[str, list[int]] = {u: [] for u in judged}
    if judge is not None and len(judged) >= 2:
        views = {u: SiteView.from_paths(sites[u]["visible_text"], sites[u]["render"]["screenshots"]) for u in judged}
        for i, (x, y) in enumerate(combinations(judged, 2)):
            pr = judge_pair(judge, brief_json, x, views[x], y, views[y])
            pairs.append(pr.as_dict())
            for u in (x, y):
                comps[u] += 1
                if pr.winner == u:
                    wins[u] += 1
                elif pr.winner is None:
                    ties[u] += 1
                if pr.invented_confirmed[u]:
                    invented[u].append(i)

    per_uid: dict[str, dict] = {}
    for uid in sorted(sites):
        s = sites[uid]
        rec: dict[str, Any] = {
            "response_time_s": responses[uid][1],
            "bundle_sha256": s["bundle_sha256"],
            "gates": s["gates"],
            "gates_passed": s["gates_passed"],
            "render": s.get("render"),
            "measured": s.get("measured"),
            "M": s.get("M"),
            "visible_text_sha256": s.get("visible_text_sha256"),
            "visible_text": s.get("visible_text"),
            "duplicate_of": duplicate_of.get(uid),
            "J": None,
            "comparisons": comps.get(uid, 0),
            "invented_claims_confirmed_in_pairs": invented.get(uid, []),
            "task_score": None,
            "zero_reason": None,
        }
        if not s["gates_passed"]:
            rec["task_score"], rec["zero_reason"] = 0.0, "gate_failed"
        elif uid in duplicate_of:
            rec["task_score"], rec["zero_reason"] = 0.0, "duplicate"
        elif _invented_confirmed(len(invented.get(uid, [])), comps.get(uid, 0)):
            rec["task_score"], rec["zero_reason"] = 0.0, "invented_claims"
            rec["J"] = _j(wins[uid], ties[uid], comps[uid])
        elif judge is not None:
            rec["J"] = _j(wins[uid], ties[uid], comps[uid])
            if rec["M"] is not None:
                rec["task_score"] = round(W_MEASURED * rec["M"] + W_JUDGED * rec["J"], 6)
            else:
                rec["zero_reason"] = "measured_quality_unavailable"
        per_uid[uid] = rec

    return {
        "record_version": 1,
        "scorer_version": __version__,
        "brief": json.loads(brief_json),
        "weights": {"task": {"M": W_MEASURED, "J": W_JUDGED}, "measured": WEIGHTS},
        "judge": (judge.record() if hasattr(judge, "record") else repr(judge)) if judge is not None else None,
        "miners": per_uid,
        "pairs": pairs,
        "timing_s": round(time.monotonic() - t0, 3),
        "artifacts_dir": str(out),
    }


def _invented_confirmed(flagged_pairs: int, pairs: int) -> bool:
    # DESIGN.md section 3.3: a site is zeroed for invented claims only when they are confirmed (flagged in both orders)
    # in at least half of its pairs. One adverse pair out of seven is a single judge roll, not a finding.
    return pairs > 0 and 2 * flagged_pairs >= pairs


def _j(w: float, t: float, n: int) -> float:
    # A lone gate-passing site has nothing to be compared with: it gets the neutral 0.5.
    return 0.5 if n == 0 else round((w + 0.5 * t) / n, 6)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def load_site(path: str) -> str:
    p = Path(path)
    if p.is_dir():
        return pack_dir(p)
    if p.suffix.lower() == ".zip":
        return base64.b64encode(p.read_bytes()).decode("ascii")
    return p.read_text(encoding="ascii").strip()  # a .b64 file


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jmercury.validator.score", description="J Mercury local scorer.")
    ap.add_argument("--brief", required=True, help="brief JSON file (python -m jmercury.brief --seed N > brief.json)")
    ap.add_argument("--site", required=True, help="site directory, .zip file or .b64 file")
    ap.add_argument("--vs", help="a second site to compare against (same formats as --site)")
    ap.add_argument("--judge", action="store_true",
                    help="run one pairwise judgement through OpenRouter (needs OPENROUTER_API_KEY and JM_JUDGE_MODEL)")
    ap.add_argument("--runs", type=int, default=3, help="Lighthouse runs per site (median is used; default 3)")
    ap.add_argument("--no-measure", action="store_true", help="skip Lighthouse (gates and render checks only)")
    ap.add_argument("--out", help="directory for screenshots and the JSON record (default: a new temp directory)")
    ap.add_argument("--with-text", action="store_true", help="include the full visible text in the printed output")
    args = ap.parse_args(argv)

    judge: Optional[Judge] = None
    if args.judge:
        if not args.vs:
            print("error: --judge needs --vs (a pairwise judgement compares two sites)", file=sys.stderr)
            return 2
        try:
            judge = OpenRouterJudge()
        except JudgeConfigError as exc:
            print(f"error: {exc}.", file=sys.stderr)
            return 2

    brief = Brief.model_validate_json(Path(args.brief).read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="jm-score-"))
    out.mkdir(parents=True, exist_ok=True)

    if not args.vs:
        res = score_site(brief, load_site(args.site), out_dir=out, runs=args.runs, do_measure=not args.no_measure)
        if not args.with_text:
            text = res.pop("visible_text") or ""
            res["visible_text_chars"] = len(text)
        (out / "result.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
        print(json.dumps(res, indent=2))
        return 0

    responses = {"a": (load_site(args.site), 0.0), "b": (load_site(args.vs), 1.0)}
    record = score_round(brief, responses, judge, out_dir=out, runs=args.runs, do_measure=not args.no_measure)
    (out / "round.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    if not args.with_text:
        for m in record["miners"].values():
            m["visible_text_chars"] = len(m.pop("visible_text") or "")
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
