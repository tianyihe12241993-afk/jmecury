"""Render an unpacked bundle in headless Chromium with the network off and JavaScript disabled.

The bundle is served from a directory by a static server on 127.0.0.1 (random port).
Every request that is not to that origin is aborted and recorded.

Outputs (``RenderResult``):
- ``visible_text``: the page's innerText at 390 x 844 after removing text that CSS hides
  (display/visibility, zero opacity, transparent colour, tiny font, zero-size or clipped
  boxes, off-page positioning);
- ``no_hscroll``: the page does not scroll horizontally at 390 px;
- ``tel_visible``: a ``tel:`` link with the brief's digits is visible and on top inside the
  first 390 x 844 screen;
- three screenshots: 390 x 844 first screen, full mobile page (height capped at 4000 px), 1280 x 800;
  the full mobile page is also written as vertical parts of at most 390 x 1560 (``mobile_full_1`` ...,
  at most 3), which is what the judge is shown: no part is tall enough for a vision model to shrink it.

Environment:
- ``JM_EXTRA_LIB_DIR``: extra shared-library directory for Chromium on hosts without its
  system libraries (prepended to LD_LIBRARY_PATH of the browser process only);
- ``JM_CHROMIUM``: optional explicit Chromium executable.
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

MOBILE = {"width": 390, "height": 844}
DESKTOP = {"width": 1280, "height": 800}
FULL_PAGE_CAP = 4000
# Judge image cap: 1560 px stays under the 1568 px long edge that vision APIs commonly downscale beyond,
# so the text in each part stays legible. 4000 px of page -> at most 3 parts (1560 + 1560 + 880).
FULL_PART_HEIGHT = 1560
NAV_TIMEOUT_MS = 20000


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".webp": "image/webp",
        ".avif": "image/avif",
        ".woff2": "font/woff2",
        ".ico": "image/x-icon",
        ".txt": "text/plain; charset=utf-8",
        ".xml": "application/xml",
    }

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


@contextlib.contextmanager
def serve_dir(root: str | os.PathLike) -> Iterator[str]:
    """Serve ``root`` on 127.0.0.1 with a random free port; yields the origin URL."""
    handler = functools.partial(_QuietHandler, directory=str(root))
    httpd = _Server(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def browser_env() -> dict[str, str]:
    env = dict(os.environ)
    extra = os.environ.get("JM_EXTRA_LIB_DIR")
    if extra:
        env["LD_LIBRARY_PATH"] = extra + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    return env


def chromium_executable() -> str:
    """Path of the full Chromium build Playwright uses (also given to Lighthouse)."""
    if os.environ.get("JM_CHROMIUM"):
        return os.environ["JM_CHROMIUM"]
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        return p.chromium.executable_path


@dataclass
class RenderResult:
    visible_text: str = ""
    no_hscroll: bool = False
    scroll_width: int = 0
    tel_visible: bool = False
    tel_links_checked: list[dict] = field(default_factory=list)
    screenshots: dict[str, str] = field(default_factory=dict)
    blocked_requests: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    page_height: int = 0
    error: Optional[str] = None

    def as_dict(self, include_text: bool = True) -> dict:
        d = {
            "no_hscroll": self.no_hscroll,
            "scroll_width": self.scroll_width,
            "tel_visible": self.tel_visible,
            "tel_links_checked": self.tel_links_checked,
            "screenshots": self.screenshots,
            "blocked_requests": self.blocked_requests,
            "failed_requests": self.failed_requests,
            "page_height": self.page_height,
            "error": self.error,
        }
        if include_text:
            d["visible_text"] = self.visible_text
        return d


# Runs in the page (Playwright evaluates it even with page JavaScript disabled).
# Marks every text node that CSS hides, blanks those nodes, and returns innerText.
_JS_HIDE_AND_READ = r"""
() => {
  const docW = Math.max(document.documentElement.scrollWidth, window.innerWidth);
  const docH = Math.max(document.documentElement.scrollHeight, window.innerHeight);
  const sx = window.scrollX, sy = window.scrollY;
  const alpha = (c) => {
    if (!c || c === 'transparent') return 0;
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return 1;
    const parts = m[1].split(/[\s,\/]+/).filter(Boolean);
    if (parts.length >= 4) { const a = parts[3]; return a.endsWith('%') ? parseFloat(a) / 100 : parseFloat(a); }
    return 1;
  };
  const clipRect = (el) => {
    // Intersection of the clipping boxes of all ancestors (overflow != visible, clip, clip-path inset).
    let r = {l: -Infinity, t: -Infinity, r: Infinity, b: Infinity};
    for (let a = el; a && a.nodeType === 1; a = a.parentElement) {
      const cs = getComputedStyle(a);
      const clips = cs.overflowX !== 'visible' || cs.overflowY !== 'visible' ||
                    (cs.clip && cs.clip !== 'auto') ||
                    (cs.clipPath && /inset\(\s*(50|100)%/.test(cs.clipPath));
      if (cs.clip && /rect\(\s*0(px)?[\s,]+0(px)?[\s,]+0(px)?[\s,]+0(px)?\s*\)/.test(cs.clip)) return {l: 0, t: 0, r: 0, b: 0};
      if (a === document.documentElement || a === document.body) continue;
      if (clips) {
        const br = a.getBoundingClientRect();
        if (cs.clipPath && /inset\(\s*(50|100)%/.test(cs.clipPath)) return {l: 0, t: 0, r: 0, b: 0};
        r = {l: Math.max(r.l, br.left), t: Math.max(r.t, br.top), r: Math.min(r.r, br.right), b: Math.min(r.b, br.bottom)};
      }
    }
    return r;
  };
  const styleHidden = (el) => {
    for (let a = el; a && a.nodeType === 1; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (parseFloat(cs.opacity) < 0.1) return true;
      if (cs.contentVisibility === 'hidden') return true;
    }
    const cs = getComputedStyle(el);
    if (cs.visibility !== 'visible') return true;
    if (parseFloat(cs.fontSize) < 6) return true;
    const fill = cs.webkitTextFillColor || cs.color;
    if (alpha(fill) < 0.1 && cs.webkitBackgroundClip !== 'text' && cs.backgroundClip !== 'text') return true;
    return false;
  };
  const hidden = [];
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    if (!n.nodeValue || !n.nodeValue.trim()) continue;
    const el = n.parentElement;
    if (!el) continue;
    let visible = false;
    if (!styleHidden(el)) {
      const range = document.createRange();
      range.selectNodeContents(n);
      const clip = clipRect(el);
      for (const rc of range.getClientRects()) {
        const l = Math.max(rc.left, clip.l), t = Math.max(rc.top, clip.t);
        const r = Math.min(rc.right, clip.r), b = Math.min(rc.bottom, clip.b);
        // page coordinates: must lie on the page, not off to the left/top or beyond its end
        const pl = l + sx, pt = t + sy, pr = r + sx, pb = b + sy;
        if (r - l >= 2 && b - t >= 2 && pr > 0 && pb > 0 && pl < docW && pt < docH) { visible = true; break; }
      }
    }
    if (!visible) hidden.push(n);
  }
  for (const h of hidden) h.nodeValue = '';
  return {text: (document.body ? document.body.innerText : ''), hidden: hidden.length};
}
"""

_JS_LAYOUT = r"""
() => ({
  scrollWidth: Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0),
  innerWidth: window.innerWidth,
  scrollHeight: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0),
})
"""

_JS_TEL = r"""
(vw_vh) => {
  const [vw, vh] = vw_vh;
  const out = [];
  for (const a of document.querySelectorAll('a[href], area[href]')) {
    const href = (a.getAttribute('href') || '').trim();
    if (!/^tel:/i.test(href)) continue;
    const r = a.getBoundingClientRect();
    let ok = r.width >= 1 && r.height >= 1;
    let why = ok ? '' : 'zero size';
    for (let e = a; ok && e && e.nodeType === 1; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.visibility !== 'visible' && e === a) { ok = false; why = 'visibility'; }
      if (parseFloat(cs.opacity) < 0.1) { ok = false; why = 'opacity'; }
      if (cs.display === 'none') { ok = false; why = 'display'; }
    }
    // centre of the visible part inside the first screen
    const l = Math.max(r.left, 0), t = Math.max(r.top, 0), rr = Math.min(r.right, vw), b = Math.min(r.bottom, vh);
    if (ok && (rr - l < 1 || b - t < 1)) { ok = false; why = 'outside first screen'; }
    if (ok) {
      const cx = (l + rr) / 2, cy = (t + b) / 2;
      const top = document.elementFromPoint(cx, cy);
      if (!top || !(top === a || a.contains(top))) { ok = false; why = 'covered or clipped'; }
    }
    out.push({href, visible: ok, why, rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)]});
  }
  return out;
}
"""


def _digits(s: str) -> str:
    import re

    d = re.sub(r"\D", "", s)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def render_site(site_dir: str | os.PathLike, out_dir: str | os.PathLike, phone_digits: Optional[str] = None,
                full_page_cap: int = FULL_PAGE_CAP) -> RenderResult:
    """Serve ``site_dir`` and render its index.html; screenshots go to ``out_dir``."""
    with serve_dir(site_dir) as origin:
        return render_origin(origin, out_dir, phone_digits, full_page_cap)


def render_origin(origin: str, out_dir: str | os.PathLike, phone_digits: Optional[str] = None,
                  full_page_cap: int = FULL_PAGE_CAP) -> RenderResult:
    """Render ``origin + '/index.html'`` (an already running local server)."""
    from playwright.sync_api import sync_playwright

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res = RenderResult()
    with sync_playwright() as p:
        launch_kwargs = {"env": browser_env(), "headless": True}
        if os.environ.get("JM_CHROMIUM"):
            launch_kwargs["executable_path"] = os.environ["JM_CHROMIUM"]
        browser = p.chromium.launch(**launch_kwargs)
        try:
            def make_context(viewport: dict, mobile: bool):
                ctx = browser.new_context(
                    viewport=viewport,
                    screen=viewport,
                    device_scale_factor=1,
                    is_mobile=mobile,
                    has_touch=mobile,
                    java_script_enabled=False,
                    reduced_motion="reduce",
                    service_workers="block",
                    locale="en-US",
                )

                def route(r):
                    url = r.request.url
                    if url == origin or url.startswith(origin + "/"):
                        r.continue_()
                    else:
                        if url not in res.blocked_requests:
                            res.blocked_requests.append(url[:300])
                        r.abort()

                ctx.route("**/*", route)
                return ctx

            # ---- mobile ------------------------------------------------------------
            ctx = make_context(MOBILE, True)
            page = ctx.new_page()
            page.on("requestfailed", lambda rq: res.failed_requests.append(rq.url[:300]) if rq.url.startswith(origin) else None)
            page.goto(origin + "/index.html", wait_until="load", timeout=NAV_TIMEOUT_MS)
            with contextlib.suppress(Exception):
                page.evaluate("() => document.fonts ? document.fonts.ready.then(() => true) : true")
            layout = page.evaluate(_JS_LAYOUT)
            res.scroll_width = int(layout["scrollWidth"])
            res.page_height = int(layout["scrollHeight"])
            res.no_hscroll = res.scroll_width <= MOBILE["width"]

            tels = page.evaluate(_JS_TEL, [MOBILE["width"], MOBILE["height"]])
            for t in tels:
                t["digits_match"] = phone_digits is None or _digits(t["href"][4:].split("?")[0]) == phone_digits
            res.tel_links_checked = tels
            res.tel_visible = any(t["visible"] and t["digits_match"] for t in tels)

            first = out / "mobile_first.png"
            page.screenshot(path=str(first), full_page=False)
            full = out / "mobile_full.png"
            h = min(res.page_height or MOBILE["height"], full_page_cap)
            page.screenshot(path=str(full), full_page=True, clip={"x": 0, "y": 0, "width": MOBILE["width"], "height": h})
            res.screenshots["mobile_first"] = str(first)
            res.screenshots["mobile_full"] = str(full)
            for i, y in enumerate(range(0, h, FULL_PART_HEIGHT), start=1):
                part = out / f"mobile_full_{i}.png"
                page.screenshot(path=str(part), full_page=True,
                                clip={"x": 0, "y": y, "width": MOBILE["width"], "height": min(FULL_PART_HEIGHT, h - y)})
                res.screenshots[f"mobile_full_{i}"] = str(part)

            vis = page.evaluate(_JS_HIDE_AND_READ)
            res.visible_text = vis["text"]
            ctx.close()

            # ---- desktop -----------------------------------------------------------
            ctx = make_context(DESKTOP, False)
            page = ctx.new_page()
            page.goto(origin + "/index.html", wait_until="load", timeout=NAV_TIMEOUT_MS)
            with contextlib.suppress(Exception):
                page.evaluate("() => document.fonts ? document.fonts.ready.then(() => true) : true")
            desk = out / "desktop_first.png"
            page.screenshot(path=str(desk), full_page=False)
            res.screenshots["desktop_first"] = str(desk)
            ctx.close()
        finally:
            browser.close()
    return res
