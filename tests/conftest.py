import base64
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest

from jmercury.brief import Brief, generate

REPO = Path(__file__).resolve().parent.parent
EXAMPLE_DIR = REPO / "examples" / "seed42"


def zip_b64(entries, *, compression=zipfile.ZIP_DEFLATED) -> str:
    """entries: list of (name, bytes|str) or (ZipInfo, bytes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for name, data in entries:
            if isinstance(data, str):
                data = data.encode("utf-8")
            if isinstance(name, zipfile.ZipInfo):
                zf.writestr(name, data)
            else:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = compression
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.create_system = 3
                zf.writestr(info, data)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def site_b64(index_html: str, **files) -> str:
    entries = [("index.html", index_html)]
    entries += [(k.replace("__", "/").replace("_dot_", "."), v) for k, v in files.items()]
    return zip_b64(entries)


@pytest.fixture(scope="session")
def brief42() -> Brief:
    return generate(42)


@pytest.fixture(scope="session")
def example_brief() -> Brief:
    return Brief.model_validate_json((EXAMPLE_DIR / "brief.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def example_html() -> str:
    return (EXAMPLE_DIR / "site" / "index.html").read_text(encoding="utf-8")


def _browser_ok() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright

        from jmercury.render import browser_env

        with sync_playwright() as p:
            b = p.chromium.launch(env=browser_env())
            b.close()
        return True, ""
    except Exception as exc:  # pragma: no cover - environment dependent
        return False, f"Chromium cannot start here ({type(exc).__name__}); set JM_EXTRA_LIB_DIR / FONTCONFIG_FILE, see README"


_BROWSER = None


def pytest_collection_modifyitems(config, items):
    global _BROWSER
    needs_browser = [i for i in items if i.get_closest_marker("browser") or i.get_closest_marker("lighthouse")]
    if needs_browser and _BROWSER is None:
        _BROWSER = _browser_ok()
    for item in needs_browser:
        if not _BROWSER[0]:
            item.add_marker(pytest.mark.skip(reason=_BROWSER[1]))
    for item in items:
        if item.get_closest_marker("lighthouse"):
            from jmercury.measure import MeasureUnavailable, check_available

            try:
                check_available()
            except MeasureUnavailable as exc:
                item.add_marker(pytest.mark.skip(reason=str(exc)))


def facts_html(brief: Brief, *, jsonld=None, body_extra: str = "", tel: str | None = None) -> str:
    """A minimal page that satisfies gate 3 for ``brief`` unless overridden."""
    if jsonld is None:
        jsonld = default_jsonld(brief)
    tel = tel if tel is not None else "tel:" + brief.phone_digits()
    blocks = jsonld if isinstance(jsonld, list) else [jsonld]
    scripts = "".join(
        f'<script type="application/ld+json">{b if isinstance(b, str) else json.dumps(b)}</script>' for b in blocks
    )
    hours = "".join(
        f"<li>{d}: {s[0]} - {s[1]}</li>" for d, s in brief.hours.items() if s
    )
    return (
        f"<!DOCTYPE html><html lang=en><head><meta charset=utf-8><title>{brief.name}</title>{scripts}</head>"
        f"<body><h1>{brief.name}</h1><a href=\"{tel}\">{brief.phone}</a><ul>{hours}</ul>{body_extra}</body></html>"
    )


DAY_FULL = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}


def default_jsonld(brief: Brief, **over) -> dict:
    node = {
        "@context": "https://schema.org",
        "@type": "LocalBusiness",
        "name": brief.name,
        "telephone": brief.phone,
        "openingHoursSpecification": [
            {"@type": "OpeningHoursSpecification", "dayOfWeek": DAY_FULL[d], "opens": s[0], "closes": s[1]}
            for d, s in brief.hours.items() if s
        ],
    }
    node.update(over)
    return node


def visible_for(brief: Brief, fmt: str = "24h") -> str:
    def f(t: str) -> str:
        h, m = int(t[:2]), int(t[3:])
        h12 = h % 12 or 12
        ap = "AM" if h < 12 else "PM"
        if fmt == "24h":
            return t
        if fmt == "h:mm AM":
            return f"{h12}:{m:02d} {ap}"
        if fmt == "ham":
            return f"{h12}{ap.lower()}" if m == 0 else f"{h12}:{m:02d}{ap.lower()}"
        if fmt == "h a.m.":
            return (f"{h12} " if m == 0 else f"{h12}:{m:02d} ") + ("a.m." if h < 12 else "p.m.")
        raise ValueError(fmt)

    lines = [brief.name, f"Call {brief.phone}"]
    for d, s in brief.hours.items():
        lines.append(f"{DAY_FULL[d]}: " + (f"{f(s[0])} - {f(s[1])}" if s else "Closed"))
    return "\n".join(lines)

