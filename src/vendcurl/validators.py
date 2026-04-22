from __future__ import annotations

import bz2
import gzip
import lzma
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse


class ValidationError(RuntimeError):
    """Raised when a downloaded artifact fails structural validation."""


VALIDATORS = {"auto", "none", "tar", "zip", "gzip", "xz", "bz2"}

_TAR_SUFFIXES = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.xz",
    ".txz",
    ".tar.bz2",
    ".tbz",
    ".tbz2",
)
_ZIP_SUFFIXES = (".zip", ".whl", ".jar")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_CHUNK_SIZE = 1024 * 1024


def infer_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = Path(unquote(parsed.path)).name
    return name or "download"


def detect_validator(url: str, output_path: Path | None = None, content_type: str | None = None) -> str:
    """Choose a validator from the URL, output path, or content type.

    The result is intentionally conservative. If in doubt, it returns ``none``
    rather than guessing an archive format and producing noisy false negatives.
    """

    candidates: list[str] = []
    if output_path is not None:
        candidates.append(output_path.name.lower())
    candidates.append(infer_filename_from_url(url).lower())

    for name in candidates:
        if name.endswith(_TAR_SUFFIXES):
            return "tar"
        if name.endswith(_ZIP_SUFFIXES):
            return "zip"
        if name.endswith(".gz"):
            return "gzip"
        if name.endswith(".xz"):
            return "xz"
        if name.endswith(".bz2"):
            return "bz2"

    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in {"application/zip", "application/x-zip-compressed"}:
        return "zip"
    if ct in {"application/gzip", "application/x-gzip"}:
        return "gzip"
    if ct in {"application/x-xz"}:
        return "xz"
    if ct in {"application/x-bzip2"}:
        return "bz2"
    if ct in {"application/x-tar"}:
        return "tar"
    return "none"


def validate_file(path: Path, validator: str, *, reject_unsafe_paths: bool = True) -> None:
    if validator not in VALIDATORS - {"auto"}:
        raise ValueError(f"unknown validator: {validator}")

    if validator == "none":
        if not path.is_file():
            raise ValidationError(f"not a regular file: {path}")
        return
    if validator == "tar":
        validate_tar(path, reject_unsafe_paths=reject_unsafe_paths)
        return
    if validator == "zip":
        validate_zip(path, reject_unsafe_paths=reject_unsafe_paths)
        return
    if validator == "gzip":
        _read_stream_to_eof(path, gzip.open, "gzip")
        return
    if validator == "xz":
        _read_stream_to_eof(path, lzma.open, "xz")
        return
    if validator == "bz2":
        _read_stream_to_eof(path, bz2.open, "bz2")
        return

    raise ValueError(f"validator must be resolved before validation: {validator}")


def validate_tar(path: Path, *, reject_unsafe_paths: bool = True) -> None:
    try:
        with tarfile.open(path, mode="r:*") as archive:
            for member in archive:
                if reject_unsafe_paths and is_unsafe_archive_name(member.name):
                    raise ValidationError(f"unsafe tar member path: {member.name!r}")
                if reject_unsafe_paths and (member.issym() or member.islnk()):
                    if member.linkname and is_unsafe_archive_name(member.linkname, allow_relative=True):
                        raise ValidationError(
                            f"unsafe tar link target for {member.name!r}: {member.linkname!r}"
                        )
    except ValidationError:
        raise
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ValidationError(f"invalid tar archive: {exc}") from exc


def validate_zip(path: Path, *, reject_unsafe_paths: bool = True) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if reject_unsafe_paths:
                for info in infos:
                    if is_unsafe_archive_name(info.filename):
                        raise ValidationError(f"unsafe zip member path: {info.filename!r}")
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ValidationError(f"zip member failed CRC check: {bad_member!r}")
    except ValidationError:
        raise
    except (zipfile.BadZipFile, RuntimeError, EOFError, OSError) as exc:
        raise ValidationError(f"invalid zip archive: {exc}") from exc


def is_unsafe_archive_name(name: str, *, allow_relative: bool = False) -> bool:
    """Reject archive names that are dangerous during later extraction.

    vendcurl never extracts archives, but catching zip-slip style paths here is
    cheap and tends to surface poisoned dependencies early.
    """

    if "\x00" in name:
        return True
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/") or normalized.startswith("//"):
        return True
    if _WINDOWS_DRIVE_RE.match(normalized):
        return True
    parts = PurePosixPath(normalized).parts
    if not allow_relative and any(part == ".." for part in parts):
        return True
    if allow_relative:
        # Link targets such as "libfoo.so.1" are fine; absolute paths, drive
        # letters, and traversal are not.
        depth = 0
        for part in parts:
            if part in {"", "."}:
                continue
            if part == "..":
                depth -= 1
            else:
                depth += 1
            if depth < 0:
                return True
    return False


def _read_stream_to_eof(path: Path, opener, label: str) -> None:
    try:
        with opener(path, "rb") as stream:
            for _ in iter(lambda: stream.read(_CHUNK_SIZE), b""):
                pass
    except (EOFError, OSError, lzma.LZMAError, gzip.BadGzipFile) as exc:
        raise ValidationError(f"invalid {label} stream: {exc}") from exc
