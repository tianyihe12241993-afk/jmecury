import base64
import io
import os
import stat
import struct
import zipfile

import pytest

from jmercury.bundle import MAX_FILES, BundleRejected, pack_dir, unpack, write_files

from .conftest import EXAMPLE_DIR, zip_b64

OK_HTML = "<!DOCTYPE html><title>x</title><p>hi</p>"


def codes(b64: str) -> set[str]:
    with pytest.raises(BundleRejected) as ei:
        unpack(b64)
    return {r.code for r in ei.value.reasons}


def info(name, mode=stat.S_IFREG | 0o644, system=3):
    zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = mode << 16
    zi.create_system = system
    return zi


def test_minimal_ok():
    b = unpack(zip_b64([("index.html", OK_HTML), ("css/site.css", "body{}"), ("img/a.png", b"\x89PNG")]))
    assert b.names == ["css/site.css", "img/a.png", "index.html"]
    assert len(b.sha256) == 64


def test_directory_entries_ok():
    b = unpack(zip_b64([(info("css/", stat.S_IFDIR | 0o755), b""), ("index.html", OK_HTML), ("css/a.css", "")]))
    assert "css/a.css" in b.files


def test_not_base64():
    assert codes("this is not base64!!") == {"not_base64"}


def test_bad_zip():
    assert codes(base64.b64encode(b"PK\x03\x04garbage").decode()) == {"bad_zip"}


def test_zipped_too_large():
    assert "zipped_too_large" in codes(base64.b64encode(os.urandom(2 * 1024 * 1024 + 10)).decode())


@pytest.mark.parametrize("name,code", [
    ("../index.html", "path_traversal"),
    ("a/../../x.html", "path_traversal"),
    ("/etc/x.html", "absolute_path"),
    ("C:/x.html", "absolute_path"),
    ("a\\..\\x.html", "backslash_in_name"),
    ("a//b.html", "non_canonical_path"),
    ("./b.html", "non_canonical_path"),
    ("ab:c.html", "colon_in_name"),
    ("a\x01.html", "control_char_in_name"),
])
def test_bad_names(name, code):
    assert code in codes(zip_b64([("index.html", OK_HTML), (info(name), b"x")]))


def test_symlink_entry():
    assert "special_entry" in codes(zip_b64([("index.html", OK_HTML), (info("link.css", stat.S_IFLNK | 0o777), "/etc/passwd")]))


@pytest.mark.parametrize("mode", [stat.S_IFIFO | 0o644, stat.S_IFCHR | 0o644, stat.S_IFBLK | 0o644, stat.S_IFSOCK | 0o644])
def test_special_entries(mode):
    assert "special_entry" in codes(zip_b64([("index.html", OK_HTML), (info("x.css", mode), "")]))


def test_duplicate_names():
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert "duplicate_name" in codes(zip_b64([("index.html", OK_HTML), ("index.html", "<p>other</p>")]))


def test_case_insensitive_duplicate():
    assert "duplicate_name" in codes(zip_b64([("index.html", OK_HTML), ("a.css", ""), ("A.CSS", "")]))


def test_file_dir_conflict():
    assert "file_dir_conflict" in codes(zip_b64([("index.html", OK_HTML), ("a.css", ""), ("a.css/b.css", "")]))


def test_too_many_files():
    entries = [("index.html", OK_HTML)] + [(f"f{i}.css", "") for i in range(MAX_FILES)]
    assert "too_many_files" in codes(zip_b64(entries))


def test_exactly_max_files_ok():
    entries = [("index.html", OK_HTML)] + [(f"f{i}.css", "") for i in range(MAX_FILES - 1)]
    unpack(zip_b64(entries))


def test_unzipped_too_large():
    # random data so it does not trip the bomb check; 3 x 1.9 MB > 5 MB but each file compresses badly
    blobs = [("index.html", OK_HTML)] + [(f"img/{i}.png", os.urandom(600_000)) for i in range(9)]
    b64 = zip_b64(blobs, compression=zipfile.ZIP_STORED)
    assert "zipped_too_large" in codes(b64) or "unzipped_too_large" in codes(b64)


def test_unzipped_too_large_declared():
    # 6 MB of zeros compresses to a few KB: under the zipped limit, over the unzipped one
    assert "unzipped_too_large" in codes(zip_b64([("index.html", OK_HTML), ("big.txt", b"\0" * 6_000_000)]))


def test_zip_bomb_ratio():
    # 1.5 MB of zeros: inside the 5 MB limit but a ratio far above 200
    assert "zip_bomb" in codes(zip_b64([("index.html", OK_HTML), ("bomb.txt", b"\0" * 1_500_000)]))


def test_lying_size_header():
    raw = base64.b64decode(zip_b64([("index.html", OK_HTML), ("a.txt", b"A" * 5000)]))
    # shrink the declared uncompressed size of a.txt in the central directory to 10 bytes
    cd = raw.rfind(b"PK\x01\x02")
    buf = bytearray(raw)
    struct.pack_into("<I", buf, cd + 24, 10)
    got = codes(base64.b64encode(bytes(buf)).decode())
    assert got & {"size_mismatch", "bad_zip", "zip_bomb"}


@pytest.mark.parametrize("name", ["app.js", "x.mjs", "page.htm", "data.json", "font.woff", "noext", "x.php", "a.HTML.js"])
def test_disallowed_types(name):
    assert "disallowed_type" in codes(zip_b64([("index.html", OK_HTML), (name, "x")]))


def test_uppercase_allowed_ext_ok():
    unpack(zip_b64([("index.html", OK_HTML), ("A.PNG", b"x")]))


def test_missing_index():
    assert "missing_index_html" in codes(zip_b64([("home.html", OK_HTML)]))


def test_index_only_in_subdir():
    assert "missing_index_html" in codes(zip_b64([("site/index.html", OK_HTML)]))


@pytest.mark.parametrize("name", ["index.html", "a.css", "i.svg"])
def test_non_utf8_text(name):
    entries = [("index.html", OK_HTML)] if name != "index.html" else []
    entries.append((name, "caf\u00e9 <p>".encode("latin-1")))
    assert "not_utf8" in codes(zip_b64(entries))


def test_encrypted_flag():
    raw = bytearray(base64.b64decode(zip_b64([("index.html", OK_HTML), ("a.css", "body{}")])))
    cd = raw.rfind(b"PK\x01\x02")
    flags = struct.unpack_from("<H", raw, cd + 8)[0]
    struct.pack_into("<H", raw, cd + 8, flags | 1)
    assert "encrypted_entry" in codes(base64.b64encode(bytes(raw)).decode())


def test_pack_dir_roundtrip_and_deterministic(tmp_path):
    site = EXAMPLE_DIR / "site"
    a, b = pack_dir(site), pack_dir(site)
    assert a == b
    bundle = unpack(a)
    assert bundle.files["index.html"] == (site / "index.html").read_bytes()
    out = write_files(bundle.files, tmp_path / "out")
    assert (out / "index.html").read_bytes() == (site / "index.html").read_bytes()


def test_pack_dir_refuses_symlink(tmp_path):
    (tmp_path / "index.html").write_text(OK_HTML)
    os.symlink("/etc/hostname", tmp_path / "x.txt")
    with pytest.raises(ValueError):
        pack_dir(tmp_path)


def test_write_files_refuses_nonempty(tmp_path):
    (tmp_path / "junk").write_text("x")
    with pytest.raises(ValueError):
        write_files({"index.html": b"x"}, tmp_path)


def test_write_files_refuses_bad_names(tmp_path):
    with pytest.raises(ValueError):
        write_files({"../x.html": b"x"}, tmp_path / "o")


def test_zip_with_comment_and_stored_ok():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.comment = b"hello"
        zf.writestr("index.html", OK_HTML)
    unpack(base64.b64encode(buf.getvalue()).decode())
