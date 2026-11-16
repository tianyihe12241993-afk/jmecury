import struct

import pytest

from jmercury.bundle import unpack
from jmercury.gates import gate_facts
from jmercury.render import render_site

from .conftest import default_jsonld, facts_html, site_b64

pytestmark = pytest.mark.browser

VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1">'


def png_size(path):
    with open(path, "rb") as fh:
        head = fh.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


def render_html(tmp_path, html, phone_digits=None, files=None):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(html, encoding="utf-8")
    for name, data in (files or {}).items():
        (site / name).write_bytes(data if isinstance(data, bytes) else data.encode())
    return render_site(site, tmp_path / "shots", phone_digits=phone_digits)


def doc(body, head=""):
    return f"<!DOCTYPE html><html lang=en><head><meta charset=utf-8>{VIEWPORT}<title>t</title>{head}</head><body style='margin:0'>{body}</body></html>"


def test_call_link_below_the_fold(tmp_path):
    html = doc('<div style="height:1200px">Intro</div><a href="tel:+16155550127">Call (615) 555-0127</a>')
    r = render_html(tmp_path, html, phone_digits="6155550127")
    assert r.no_hscroll
    assert not r.tel_visible
    assert r.tel_links_checked[0]["why"] == "outside first screen"
    w, h = png_size(r.screenshots["mobile_first"])
    assert (w, h) == (390, 844)
    assert png_size(r.screenshots["desktop_first"]) == (1280, 800)
    fw, fh = png_size(r.screenshots["mobile_full"])
    assert fw == 390 and fh > 844


def test_horizontal_overflow(tmp_path):
    html = doc('<a href="tel:6155550127">Call</a><div style="width:600px;background:#eee">wide</div>')
    r = render_html(tmp_path, html, phone_digits="6155550127")
    assert not r.no_hscroll and r.scroll_width >= 600
    assert r.tel_visible


def test_missing_viewport_meta_scrolls(tmp_path):
    html = "<!DOCTYPE html><html lang=en><head><title>t</title></head><body><a href='tel:6155550127'>Call</a></body></html>"
    r = render_html(tmp_path, html, phone_digits="6155550127")
    assert not r.no_hscroll  # laid out at 980 px on a phone


@pytest.mark.parametrize("variant,visible", [
    ("plain", True),
    ("covered", False),
    ("hidden", False),
    ("zero", False),
    ("wrong_number", False),
    ("fixed_bottom_bar", True),
])
def test_tel_visibility_variants(tmp_path, variant, visible):
    link = '<a href="tel:+16155550127" style="display:inline-block;padding:12px">Call</a>'
    body = {
        "plain": link,
        "covered": link + '<div style="position:fixed;inset:0;background:#fff"></div>',
        "hidden": '<a href="tel:+16155550127" style="visibility:hidden">Call</a>',
        "zero": '<a href="tel:+16155550127" style="display:block;width:0;height:0;overflow:hidden">Call</a>',
        "wrong_number": '<a href="tel:+16155550199">Call</a>',
        "fixed_bottom_bar": '<div style="height:3000px">x</div><a href="tel:6155550127" style="position:fixed;bottom:0;left:0;right:0;padding:16px">Call</a>',
    }[variant]
    r = render_html(tmp_path, doc(body), phone_digits="6155550127")
    assert r.tel_visible is visible, r.tel_links_checked


def test_network_is_off_and_js_disabled(tmp_path):
    html = doc('<p id=p>static</p><img src="https://evil.example/x.png" alt=x>'
               '<script>document.getElementById("p").textContent = "JS RAN"</script>')
    r = render_html(tmp_path, html)
    assert any("evil.example" in u for u in r.blocked_requests)
    assert "JS RAN" not in r.visible_text and "static" in r.visible_text


HIDE = {
    "display_none": "display:none",
    "visibility_hidden": "visibility:hidden",
    "font_size_zero": "font-size:0",
    "transparent": "color:transparent",
    "rgba_zero": "color:rgba(0,0,0,0)",
    "opacity_zero": "opacity:0",
    "offscreen_left": "position:absolute;left:-9999px",
    "offscreen_top": "position:absolute;top:-9999px",
    "text_indent": "text-indent:-9999px;overflow:hidden;white-space:nowrap",
    "sr_only": "position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0",
    "clip_path": "clip-path:inset(50%)",
    "zero_height": "height:0;overflow:hidden",
    "scale_zero": "transform:scale(0)",
}


@pytest.mark.parametrize("how", sorted(HIDE))
def test_css_hidden_name_and_phone_do_not_count(tmp_path, brief42, how):
    """Name and phone only inside CSS-hidden text: gate 3 must fail on visibility."""
    style = HIDE[how]
    jsonld = default_jsonld(brief42)
    hours = " ".join(f"{s[0]}-{s[1]}" for s in brief42.hours.values() if s)
    html = facts_html(brief42, jsonld=jsonld).replace(
        f"<h1>{brief42.name}</h1><a href=\"tel:{brief42.phone_digits()}\">{brief42.phone}</a>",
        f"<div style=\"{style}\"><h1>{brief42.name}</h1></div><div style=\"{style}\">{brief42.phone}</div>"
        f"<a href=\"tel:{brief42.phone_digits()}\">Call us</a><p>{hours}</p>",
    ).replace("<head>", f"<head>{VIEWPORT}")
    assert brief42.name in html
    r = render_html(tmp_path, html, phone_digits=brief42.phone_digits())
    g = gate_facts(brief42, unpack(site_b64(html)), r.visible_text)
    got = {x["code"] for x in g.reasons}
    assert "name_not_visible" in got and "phone_not_visible" in got, (how, r.visible_text)


def test_visible_name_and_phone_count(tmp_path, brief42):
    html = facts_html(brief42).replace("<head>", f"<head>{VIEWPORT}")
    r = render_html(tmp_path, html, phone_digits=brief42.phone_digits())
    g = gate_facts(brief42, unpack(site_b64(html)), r.visible_text)
    assert g.passed, (g.reasons, r.visible_text)


def test_text_transform_uppercase_still_counts(tmp_path, brief42):
    html = facts_html(brief42).replace("<h1>", "<h1 style='text-transform:uppercase'>").replace("<head>", f"<head>{VIEWPORT}")
    r = render_html(tmp_path, html, phone_digits=brief42.phone_digits())
    assert brief42.name.upper() in r.visible_text
    assert gate_facts(brief42, unpack(site_b64(html)), r.visible_text).passed
