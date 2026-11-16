"""Gate 1: safe unpacking of a miner's base64 zip bundle (DESIGN.md section 2.2).

``unpack(bundle_b64)`` never writes outside memory; it returns the files as a dict
name -> bytes, or raises ``BundleRejected`` with machine-readable reasons.
``write_files(files, dest)`` then materialises an accepted bundle in an empty directory.
``pack_dir(path)`` and ``pack_files(files)`` are the miner-side helpers that build a deterministic bundle.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import stat
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAX_ZIPPED = 2 * 1024 * 1024
MAX_UNZIPPED = 5 * 1024 * 1024
MAX_FILES = 40
MAX_DIR_ENTRIES = 40
ALLOWED_EXT = frozenset({".html", ".css", ".png", ".jpg", ".jpeg", ".webp", ".avif", ".svg", ".woff2", ".ico", ".txt", ".xml"})
UTF8_EXT = frozenset({".html", ".css", ".svg"})
# Zip-bomb guard while streaming: an entry may not expand past this ratio once it is larger than the floor.
BOMB_RATIO = 200
BOMB_FLOOR = 1024 * 1024
_CHUNK = 64 * 1024


@dataclass
class Reason:
    code: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail}


class BundleRejected(Exception):
    def __init__(self, reasons: list[Reason]):
        self.reasons = reasons
        super().__init__("; ".join(f"{r.code}: {r.detail}" for r in reasons))


@dataclass
class Bundle:
    files: dict[str, bytes]
    sha256: str
    zipped_bytes: int
    unzipped_bytes: int
    names: list[str] = field(default_factory=list)

    def text(self, name: str) -> str:
        return self.files[name].decode("utf-8")


def _check_name(raw: str) -> tuple[str | None, str | None]:
    """Return (normalised posix name, None) or (None, error code)."""
    if not raw or "\x00" in raw:
        return None, "bad_name"
    if "\\" in raw:
        return None, "backslash_in_name"
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        return None, "absolute_path"
    if ":" in raw:
        return None, "colon_in_name"
    is_dir = raw.endswith("/")
    body = raw[:-1] if is_dir else raw
    parts = body.split("/")
    if any(p == ".." for p in parts):
        return None, "path_traversal"
    if any(p in ("", ".") for p in parts):
        return None, "non_canonical_path"
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        return None, "control_char_in_name"
    return "/".join(parts) + ("/" if is_dir else ""), None


def _entry_kind(info: zipfile.ZipInfo) -> str:
    """'file', 'dir' or 'special' (symlink, device, fifo, socket ...)."""
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode:
        fmt = stat.S_IFMT(mode)
        if fmt == stat.S_IFDIR:
            return "dir"
        if fmt == stat.S_IFREG:
            return "dir" if info.filename.endswith("/") else "file"
        if fmt == 0:
            pass  # permission bits only: fall through to name-based detection
        else:
            return "special"
    # MS-DOS directory attribute
    if info.external_attr & 0x10:
        return "dir"
    return "dir" if info.filename.endswith("/") else "file"


def decode_b64(bundle_b64: str) -> bytes:
    if not isinstance(bundle_b64, str):
        raise BundleRejected([Reason("not_base64", "bundle must be a base64 string")])
    # 4/3 expansion plus a little slack for whitespace; refuse before decoding anything huge.
    if len(bundle_b64) > (MAX_ZIPPED * 4) // 3 + 1024:
        raise BundleRejected([Reason("zipped_too_large", f"base64 length {len(bundle_b64)}")])
    compact = "".join(bundle_b64.split())
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BundleRejected([Reason("not_base64", str(exc))]) from None
    if len(raw) > MAX_ZIPPED:
        raise BundleRejected([Reason("zipped_too_large", f"{len(raw)} bytes > {MAX_ZIPPED}")])
    return raw


def unpack(bundle_b64: str) -> Bundle:
    """Validate and read a bundle fully into memory. Raises BundleRejected."""
    raw = decode_b64(bundle_b64)
    sha = hashlib.sha256(raw).hexdigest()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        infos = zf.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError, NotImplementedError) as exc:
        raise BundleRejected([Reason("bad_zip", str(exc))]) from None

    reasons: list[Reason] = []
    files_meta: list[tuple[str, zipfile.ZipInfo]] = []
    dirs: set[str] = set()
    seen: set[str] = set()
    n_files = n_dirs = 0
    declared_total = 0

    for info in infos:
        name, err = _check_name(info.filename)
        if err:
            reasons.append(Reason(err, repr(info.filename)))
            continue
        kind = _entry_kind(info)
        if kind == "special":
            reasons.append(Reason("special_entry", f"{name} (symlink or other non-regular entry)"))
            continue
        if info.flag_bits & 0x1:
            reasons.append(Reason("encrypted_entry", name))
            continue
        key = name.rstrip("/").casefold()
        if key in seen:
            reasons.append(Reason("duplicate_name", name))
            continue
        seen.add(key)
        if kind == "dir":
            n_dirs += 1
            dirs.add(name.rstrip("/").casefold())
            continue
        n_files += 1
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            reasons.append(Reason("unsupported_compression", f"{name} method {info.compress_type}"))
            continue
        ext = PurePosixPath(name).suffix.lower()
        if ext not in ALLOWED_EXT:
            reasons.append(Reason("disallowed_type", name))
            continue
        declared_total += info.file_size
        files_meta.append((name, info))

    if n_files > MAX_FILES:
        reasons.append(Reason("too_many_files", f"{n_files} > {MAX_FILES}"))
    if n_dirs > MAX_DIR_ENTRIES:
        reasons.append(Reason("too_many_dirs", f"{n_dirs} > {MAX_DIR_ENTRIES}"))
    if declared_total > MAX_UNZIPPED:
        reasons.append(Reason("unzipped_too_large", f"declared {declared_total} bytes > {MAX_UNZIPPED}"))
    # A file and a directory may not share a path ("a" and "a/b").
    file_keys = {n.casefold() for n, _ in files_meta}
    for n, _ in files_meta:
        parts = n.casefold().split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in file_keys:
                reasons.append(Reason("file_dir_conflict", n))
    if "index.html" not in {n for n, _ in files_meta}:
        reasons.append(Reason("missing_index_html", "index.html must be at the root of the zip"))
    if reasons:
        raise BundleRejected(reasons)

    files: dict[str, bytes] = {}
    total = 0
    for name, info in files_meta:
        out = bytearray()
        try:
            with zf.open(info) as fh:
                while True:
                    chunk = fh.read(_CHUNK)
                    if not chunk:
                        break
                    out += chunk
                    total += len(chunk)
                    if total > MAX_UNZIPPED:
                        raise BundleRejected([Reason("unzipped_too_large", f"more than {MAX_UNZIPPED} bytes after decompression")])
                    if len(out) > info.file_size:
                        raise BundleRejected([Reason("size_mismatch", f"{name} expands past its declared size")])
                    if len(out) > BOMB_FLOOR and len(out) > BOMB_RATIO * max(info.compress_size, 1):
                        raise BundleRejected([Reason("zip_bomb", f"{name} compression ratio above {BOMB_RATIO}")])
        except BundleRejected:
            raise
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError, NotImplementedError) as exc:
            raise BundleRejected([Reason("bad_zip", f"{name}: {exc}")]) from None
        if len(out) != info.file_size:
            raise BundleRejected([Reason("size_mismatch", f"{name}: {len(out)} != declared {info.file_size}")])
        files[name] = bytes(out)

    for name, data in files.items():
        if PurePosixPath(name).suffix.lower() in UTF8_EXT:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                reasons.append(Reason("not_utf8", f"{name}: {exc.reason} at byte {exc.start}"))
    if reasons:
        raise BundleRejected(reasons)

    return Bundle(files=files, sha256=sha, zipped_bytes=len(raw), unzipped_bytes=total, names=sorted(files))


def write_files(files: dict[str, bytes], dest: str | os.PathLike) -> Path:
    """Write an accepted bundle into ``dest`` (must be empty or absent). Refuses to follow links."""
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError(f"{root} is not empty")
    root_real = root.resolve()
    for name, data in files.items():
        _, err = _check_name(name)
        if err:
            raise ValueError(f"refusing to write {name!r}: {err}")
        target = root_real.joinpath(*name.split("/"))
        if root_real not in target.parents:
            raise ValueError(f"refusing to write outside the bundle root: {name!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    return root_real


def pack_dir(path: str | os.PathLike) -> str:
    """Zip a site directory deterministically and return it as base64 (miner helper).

    Symlinks are refused rather than followed. File order and timestamps are fixed, so the
    same directory always gives the same bundle and the same hash.
    """
    root = Path(path)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    entries: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for d in dirnames:
            if (Path(dirpath) / d).is_symlink():
                raise ValueError(f"symlink not allowed in a bundle: {Path(dirpath) / d}")
        for f in filenames:
            p = Path(dirpath) / f
            if p.is_symlink() or not p.is_file():
                raise ValueError(f"only regular files are allowed in a bundle: {p}")
            entries.append((p.relative_to(root).as_posix(), p))
    entries.sort()
    return pack_files({name: p.read_bytes() for name, p in entries})


def pack_files(files: dict[str, bytes]) -> str:
    """Zip in-memory files (name -> bytes) deterministically and return base64 (miner helper)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in sorted(files):
            _, err = _check_name(name)
            if err or name.endswith("/"):
                raise ValueError(f"bad file name for a bundle: {name!r}")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.create_system = 3
            zf.writestr(info, files[name])
    return base64.b64encode(buf.getvalue()).decode("ascii")
