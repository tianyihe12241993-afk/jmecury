import pytest

from jmercury.bundle import unpack
from jmercury.gates import gate_no_script

from .conftest import zip_b64


def g2(index_html: str, extra=()):
    return gate_no_script(unpack(zip_b64([("index.html", index_html), *extra])))


def page(body: str, head: str = "") -> str:
    return f"<!DOCTYPE html><html lang=en><head><meta charset=utf-8><title>t</title>{head}</head><body>{body}</body></html>"


BAD_BODY = {
    # scripts
    "script": "<script>alert(1)</script>",
    "script_upper": "<SCRIPT>alert(1)</SCRIPT>",
    "script_mixed_src": "<ScRiPt src=x.js></ScRiPt>",
    "script_typed_js": '<script type="text/javascript">1</script>',
    "script_module": '<script type="module">1</script>',
    "jsonld_with_src": '<script type="application/ld+json" src="x.json"></script>',
    "jsonld_extra_attr": '<script type="application/ld+json" nonce=x>{}</script>',
    "script_in_template": "<template><script>alert(1)</script></template>",
    "script_in_comment_raw": "<!-- <script>alert(1)</script> -->",
    "script_svg": "<svg><script>alert(1)</script></svg>",
    "script_svg_ns": "<svg><svg:script>alert(1)</svg:script></svg>",
    # event handlers
    "onerror": "<img src=a.png onerror=alert(1) alt=x>",
    "onload_upper": "<body ONLOAD=alert(1)>",
    "onclick_space": '<div onclick ="x">x</div>',
    "svg_onload": "<svg onload=alert(1)></svg>",
    "onfocus_autofocus": "<input autofocus onfocus=alert(1)>",
    "on_slash": "<img/onerror=alert(1) src=a.png>",
    # javascript: URLs in any attribute
    "js_href": '<a href="javascript:alert(1)">x</a>',
    "js_href_case": '<a href="JaVaScRiPt:alert(1)">x</a>',
    "js_entity_dec": '<a href="&#106;avascript:alert(1)">x</a>',
    "js_entity_hex": '<a href="&#x6A;avascript:alert(1)">x</a>',
    "js_entity_named_tab": '<a href="java&Tab;script:alert(1)">x</a>',
    "js_tab_raw": '<a href="java\tscript:alert(1)">x</a>',
    "js_newline": '<a href="java\nscript:alert(1)">x</a>',
    "js_leading_space": '<a href="  javascript:alert(1)">x</a>',
    "js_leading_ctrl": '<a href="\x01javascript:alert(1)">x</a>',
    "js_formaction": '<form><button formaction="javascript:alert(1)">x</button></form>',
    "js_action": '<form action="javascript:alert(1)"><button>x</button></form>',
    "vbscript": '<a href="vbscript:msgbox(1)">x</a>',
    "js_srcset": '<img srcset="javascript:alert(1) 1x" alt=x>',
    "js_xlink": '<svg><a xlink:href="javascript:alert(1)"><text>x</text></a></svg>',
    "js_math": '<math><mtext><a href="javascript:alert(1)">x</a></mtext></math>',
    "js_svg_animate_values": '<svg><a><animate attributeName="href" values="javascript:alert(1)"/><text>x</text></a></svg>',
    # forbidden elements
    "iframe": '<iframe src="a.html"></iframe>',
    "iframe_srcdoc": '<iframe srcdoc="<p>x</p>"></iframe>',
    "object": '<object data="a.svg"></object>',
    "embed": '<embed src="a.svg">',
    "base": '<base href="https://evil.example/">',
    "frameset_raw": "<frameset><frame src=a.html></frameset>",
    "foreignobject": "<svg><foreignObject><div>x</div></foreignObject></svg>",
    "link_import": '<link rel="import" href="a.html">',
    "portal": '<portal src="a.html"></portal>',
    # disallowed link schemes
    "http_link": '<a href="http://example.com/">x</a>',
    "ftp_link": '<a href="ftp://example.com/">x</a>',
    "data_link": '<a href="data:text/html,hi">x</a>',
    "file_link": '<a href="file:///etc/passwd">x</a>',
    "ping": '<a href="#a" ping="https://evil.example/p">x</a>',
    # external resources
    "img_https": '<img src="https://evil.example/x.png" alt=x>',
    "img_http": '<img src="http://evil.example/x.png" alt=x>',
    "img_protocol_relative": '<img src="//evil.example/x.png" alt=x>',
    "img_backslashes": '<img src="\\\\evil.example\\x.png" alt=x>',
    "img_slash_backslash": '<img src="/\\evil.example/x.png" alt=x>',
    "img_data": '<img src="data:image/png;base64,AAAA" alt=x>',
    "img_blob": '<img src="blob:https://x/1" alt=x>',
    "img_srcset_external": '<img src="a.png" srcset="a.png 1x, https://evil.example/b.png 2x" alt=x>',
    "picture_source": '<picture><source srcset="https://evil.example/x.webp"><img src=a.png alt=x></picture>',
    "video_poster": '<video poster="https://evil.example/p.png"></video>',
    "video_source": '<video><source src="https://evil.example/v.mp4"></video>',
    "audio": '<audio src="https://evil.example/a.mp3"></audio>',
    "track": '<video><track src="https://evil.example/t.vtt"></video>',
    "input_image": '<input type=image src="https://evil.example/x.png" alt=x>',
    "body_background": '<table background="https://evil.example/bg.png"><tr><td>x</td></tr></table>',
    "noscript_img": '<noscript><img src="https://evil.example/x.png" alt=x></noscript>',
    # SVG references and animation
    "svg_use_href": '<svg><use href="https://evil.example/s.svg#a"/></svg>',
    "svg_use_xlink": '<svg><use xlink:href="https://evil.example/s.svg#a"/></svg>',
    "svg_image": '<svg><image href="https://evil.example/x.png"/></svg>',
    "svg_feimage": '<svg><filter><feImage href="https://evil.example/x.png"/></filter></svg>',
    "svg_set_href": '<svg><use href="#a"><set attributeName="href" to="https://evil.example/x.svg#a"/></use></svg>',
    "svg_style_import": "<svg><style>@import url(https://evil.example/x.css);</style></svg>",
    # CSS in style elements and attributes
    "style_import_url": "<style>@import url(https://evil.example/x.css);</style>",
    "style_import_string": '<style>@import "https://evil.example/x.css";</style>',
    "style_import_upper": '<style>@IMPORT "//evil.example/x.css";</style>',
    "style_bg_url": "<style>body{background:url(https://evil.example/x.png)}</style>",
    "style_bg_url_quoted_pr": '<style>body{background:url("//evil.example/x.png")}</style>',
    "style_data_url": "<style>body{background:url(data:image/png;base64,AAAA)}</style>",
    "style_font_face": "<style>@font-face{font-family:x;src:url(https://fonts.example/x.woff2)}</style>",
    "style_escape_url": "<style>body{background:\\75 rl(https://evil.example/x.png)}</style>",
    "style_escape_mid": "<style>body{background:u\\rl(https://evil.example/x.png)}</style>",
    "style_escape_in_url": "<style>body{background:url(ht\\74ps://evil.example/x.png)}</style>",
    "style_image_set": '<style>body{background-image:image-set("https://evil.example/x.png" 1x)}</style>',
    "style_comment_split": "<style>body{background:/**/url(https://evil.example/x.png)}</style>",
    "style_attr_url": '<div style="background:url(https://evil.example/x.png)">x</div>',
    "style_attr_import": '<div style="@import url(https://evil.example/x.css)">x</div>',
    "style_attr_js": "<p style=\"background:url('javascript:alert(1)')\">x</p>",
    "style_attr_entity": '<div style="background:url(&#104;ttps://evil.example/x.png)">x</div>',
    "style_binding": "<style>body{-moz-binding:url(a.xml#x)}</style>",
    "style_expression": "<style>body{width:expression(alert(1))}</style>",
    "meta_refresh": '<meta http-equiv="refresh" content="0;url=https://evil.example/">',
}

BAD_HEAD = {
    "link_stylesheet": '<link rel="stylesheet" href="https://evil.example/x.css">',
    "link_preconnect": '<link rel="preconnect" href="https://fonts.gstatic.example">',
    "link_icon": '<link rel="icon" href="https://evil.example/f.ico">',
    "link_preload_srcset": '<link rel="preload" as="image" imagesrcset="https://evil.example/x.png 1x">',
    "meta_refresh_head": '<meta http-equiv="Refresh" content="5">',
    "base_head": '<base href="/x/">',
}


@pytest.mark.parametrize("case", sorted(BAD_BODY))
def test_bad_body(case):
    r = g2(page(BAD_BODY[case]))
    assert not r.passed, case
    assert r.reasons


@pytest.mark.parametrize("case", sorted(BAD_HEAD))
def test_bad_head(case):
    assert not g2(page("<p>x</p>", head=BAD_HEAD[case])).passed


BAD_FILES = {
    "css_external_url": ("site.css", "body{background:url(https://evil.example/a.png)}"),
    "css_import": ("site.css", '@import "https://evil.example/x.css";'),
    "css_import_local_then_ext": ("site.css", '@import "a.css"; .x{background:url(//evil.example/a.png)}'),
    "css_data_font": ("site.css", "@font-face{font-family:f;src:url(data:font/woff2;base64,AAAA)}"),
    "css_escape": ("site.css", "body{background:\\000075rl(https://evil.example/a.png)}"),
    "svg_script": ("i.svg", '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
    "svg_onload": ("i.svg", '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'),
    "svg_external_use": ("i.svg", '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><use xlink:href="https://evil.example/a.svg#x"/></svg>'),
    "svg_external_image": ("i.svg", '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://evil.example/a.png"/></svg>'),
    "svg_foreign_object": ("i.svg", '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><p xmlns="http://www.w3.org/1999/xhtml">x</p></foreignObject></svg>'),
    "svg_entity": ("i.svg", '<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]><svg xmlns="http://www.w3.org/2000/svg">&a;</svg>'),
    "svg_stylesheet_pi": ("i.svg", '<?xml-stylesheet href="https://evil.example/x.css"?><svg xmlns="http://www.w3.org/2000/svg"/>'),
    "svg_style_url": ("i.svg", '<svg xmlns="http://www.w3.org/2000/svg"><style>rect{fill:url(https://evil.example/a.svg#g)}</style></svg>'),
    "svg_js_link": ("i.svg", '<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)"><text>x</text></a></svg>'),
    "svg_not_xml": ("i.svg", "<svg><g></svg>"),
    "other_html_script": ("about.html", "<p>x</p><script>alert(1)</script>"),
}


@pytest.mark.parametrize("case", sorted(BAD_FILES))
def test_bad_files(case):
    name, content = BAD_FILES[case]
    assert not g2(page('<link rel="stylesheet" href="site.css"><p>x</p>'), extra=[(name, content)]).passed


GOOD_BODY = [
    '<a href="tel:+19375550142">Call</a> <a href="mailto:office@x.example">Mail</a> <a href="sms:+19375550142">Text</a>',
    '<a href="https://maps.example.com/?q=Dayton">Map</a> <a href="#services">Services</a> <a href="/">Home</a>',
    '<img src="img/a.png" srcset="img/a.png 1x, img/a@2x.png 2x" alt="Truck">',
    "<style>body{background:url(img/bg.webp)} .x::before{content:\"Call: now\"} @import 'site.css';</style>",
    '<svg><defs><symbol id="i"><path d="M0 0h1"/></symbol></defs><use href="#i"/></svg>',
    '<script type="application/ld+json">{"@context":"https://schema.org","@type":"LocalBusiness","name":"x"}</script>',
    '<script type="application/ld+json" id="biz">{}</script>',
    '<p>Turn on the AC; we can help. Call 9am-5pm. The onboarding is simple.</p>',
    '<p style="color:#333;background:linear-gradient(#fff,#eee)">x</p>',
    '<form action="https://forms.example.com/x" method="post"><button>Send</button></form>',
]


@pytest.mark.parametrize("i", range(len(GOOD_BODY)))
def test_good_body(i):
    r = g2(page(GOOD_BODY[i]), extra=[("site.css", "body{color:#111}"), ("img/a.png", b"x")])
    assert r.passed, r.reasons


def test_good_svg_file_and_css():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4"><style>.a{fill:#000}</style><rect class="a" width="4" height="4"/></svg>'
    css = '@font-face{font-family:B;src:url("fonts/b.woff2") format("woff2")} body{background:url(\'img/bg.png\')}'
    r = g2(page('<img src="i.svg" alt="logo">', head='<link rel="stylesheet" href="site.css">'),
           extra=[("i.svg", svg), ("site.css", css)])
    assert r.passed, r.reasons


def test_example_site_passes(example_html):
    from pathlib import Path

    from jmercury.bundle import pack_dir

    from .conftest import EXAMPLE_DIR

    r = gate_no_script(unpack(pack_dir(Path(EXAMPLE_DIR) / "site")))
    assert r.passed, r.reasons
