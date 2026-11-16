"""Measured quality M (DESIGN.md section 3.2).

Lighthouse runs from the repo-local ``node_modules`` (``npm ci``), mobile form factor,
categories performance / accessibility / best-practices / seo, with ``CHROME_PATH`` set to
the Playwright Chromium. Each site is measured ``runs`` times (default 3); the median per
category is used. The network is off for Lighthouse's Chrome too: every non-loopback
request goes to a dead proxy and every host name except 127.0.0.1 fails to resolve.

If ``node`` or the local Lighthouse install is missing, ``MeasureUnavailable`` is raised;
nothing is ever faked.
"""

from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .render import browser_env, chromium_executable

REPO_ROOT = Path(__file__).resolve().parent.parent
CATEGORIES = ("performance", "accessibility", "best-practices", "seo")
WEIGHTS = {
    "performance": 0.20,
    "accessibility": 0.25,
    "best-practices": 0.10,
    "seo": 0.15,
    "no_hscroll": 0.10,
    "tel_visible": 0.20,
}
LIGHTHOUSE_TIMEOUT_S = 180
CHROME_FLAGS = (
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--proxy-server=http://127.0.0.1:9",
    "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
    "--no-first-run",
    "--disable-extensions",
)


class MeasureUnavailable(RuntimeError):
    pass


def lighthouse_bin() -> Path:
    return REPO_ROOT / "node_modules" / ".bin" / "lighthouse"


def check_available() -> tuple[str, Path]:
    node = shutil.which("node")
    if not node:
        raise MeasureUnavailable("node is not on PATH: install Node.js 22.19+ to run Lighthouse (measured quality is skipped, not faked)")
    try:
        ver = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip().lstrip("v")
        major, minor = (int(x) for x in ver.split(".")[:2])
    except (OSError, ValueError, subprocess.SubprocessError):
        raise MeasureUnavailable("could not read `node --version`") from None
    if (major, minor) < (22, 19):
        raise MeasureUnavailable(f"Lighthouse 13 needs Node.js 22.19+, found {ver}")
    lh = lighthouse_bin()
    if not lh.exists():
        raise MeasureUnavailable(f"Lighthouse is not installed: run `npm ci --ignore-scripts` in {REPO_ROOT}")
    return node, lh


@dataclass
class LighthouseRun:
    scores: dict[str, Optional[float]]
    runtime_error: Optional[str] = None
    failed_audits: dict[str, list[str]] = field(default_factory=dict)


def run_lighthouse(url: str, *, chrome_path: Optional[str] = None, timeout: int = LIGHTHOUSE_TIMEOUT_S) -> LighthouseRun:
    _, lh = check_available()
    env = browser_env()
    env["CHROME_PATH"] = chrome_path or chromium_executable()
    with tempfile.TemporaryDirectory(prefix="jm-lh-") as tmp:
        out_json = Path(tmp) / "report.json"
        cmd = [
            str(lh), url,
            "--output=json", f"--output-path={out_json}",
            "--quiet",
            "--form-factor=mobile",
            "--only-categories=" + ",".join(CATEGORIES),
            "--chrome-flags=" + " ".join(CHROME_FLAGS),
            "--max-wait-for-load=30000",
        ]
        env["TMPDIR"] = tmp
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, cwd=tmp)
        if proc.returncode != 0 or not out_json.exists():
            tail = (proc.stderr or proc.stdout or "")[-800:]
            raise RuntimeError(f"lighthouse exited {proc.returncode}: {tail}")
        report = json.loads(out_json.read_text(encoding="utf-8"))
    scores: dict[str, Optional[float]] = {}
    failed: dict[str, list[str]] = {}
    for cat in CATEGORIES:
        c = report.get("categories", {}).get(cat)
        scores[cat] = None if c is None or c.get("score") is None else float(c["score"])
        if c:
            bad = []
            for ref in c.get("auditRefs", []):
                a = report.get("audits", {}).get(ref.get("id"), {})
                if ref.get("weight", 0) > 0 and a.get("score") is not None and a.get("score") < 1:
                    bad.append(ref["id"])
            failed[cat] = bad
    rte = report.get("runtimeError", {}).get("message") if report.get("runtimeError") else None
    return LighthouseRun(scores=scores, runtime_error=rte, failed_audits=failed)


@dataclass
class Measured:
    runs: list[dict]
    medians: dict[str, float]
    spread: dict[str, list[float]]
    parts: dict[str, float]
    M: float

    def as_dict(self) -> dict:
        return {"lighthouse_runs": self.runs, "lighthouse_median": self.medians, "lighthouse_min_max": self.spread,
                "parts": self.parts, "weights": WEIGHTS, "M": self.M}


def compute_m(medians: dict[str, float], no_hscroll: bool, tel_visible: bool) -> tuple[float, dict[str, float]]:
    parts = {cat: float(medians.get(cat) or 0.0) for cat in CATEGORIES}
    parts["no_hscroll"] = 1.0 if no_hscroll else 0.0
    parts["tel_visible"] = 1.0 if tel_visible else 0.0
    m = sum(WEIGHTS[k] * v for k, v in parts.items())
    return round(m, 6), parts


def measure(url: str, *, no_hscroll: bool, tel_visible: bool, runs: int = 3, chrome_path: Optional[str] = None) -> Measured:
    check_available()
    chrome = chrome_path or chromium_executable()
    results: list[LighthouseRun] = [run_lighthouse(url, chrome_path=chrome) for _ in range(runs)]
    medians: dict[str, float] = {}
    spread: dict[str, list[float]] = {}
    for cat in CATEGORIES:
        vals = [r.scores.get(cat) or 0.0 for r in results]  # a missing score counts as 0
        medians[cat] = float(statistics.median(vals))
        spread[cat] = [min(vals), max(vals)]
    m, parts = compute_m(medians, no_hscroll, tel_visible)
    runs_out = [{"scores": r.scores, "runtime_error": r.runtime_error, "failed_audits": r.failed_audits} for r in results]
    return Measured(runs=runs_out, medians=medians, spread=spread, parts=parts, M=m)
