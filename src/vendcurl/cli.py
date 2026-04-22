from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import __version__
from .storage import ObjectStoreError, S3Config, S3Store, join_key
from .validators import ValidationError, detect_validator, infer_filename_from_url, validate_file

_CHUNK_SIZE = 1024 * 1024
_RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class VendcurlError(RuntimeError):
    """Expected CLI failure."""


class CacheMiss(RuntimeError):
    """A manifest/blob existed but could not be used."""


@dataclass(frozen=True)
class DownloadResult:
    sha256: str
    size: int
    final_url: str
    content_type: str | None
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class InstallResult:
    status: str
    url: str
    output: str
    sha256: str
    size: int
    validator: str
    blob_key: str | None
    manifest_key: str | None
    final_url: str | None = None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = run(args, parser)
    except VendcurlError as exc:
        print(f"vendcurl: error: {exc}", file=sys.stderr)
        return 2
    except ObjectStoreError as exc:
        print(f"vendcurl: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("vendcurl: interrupted", file=sys.stderr)
        return 130

    if args.json_output:
        print(json.dumps(result.__dict__, sort_keys=True))
    elif not args.quiet:
        cache = "without object-store caching" if result.blob_key is None else f"via {result.blob_key}"
        print(
            f"vendcurl: {result.status}: {result.output} "
            f"sha256={result.sha256} validator={result.validator} {cache}",
            file=sys.stderr,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vendcurl",
        description="Download a URL, validate the artifact, vendor it into S3-compatible storage, and prefer that vendored copy next time.",
    )
    parser.add_argument("url", help="URL to fetch")
    parser.add_argument(
        "output",
        nargs="?",
        help="output file path; defaults to the basename of the URL path",
    )
    parser.add_argument("--version", action="version", version=f"vendcurl {__version__}")

    cache = parser.add_argument_group("object-store cache")
    cache.add_argument(
        "--bucket",
        default=os.getenv("VENDCURL_BUCKET") or os.getenv("S3_BUCKET"),
        help="S3 bucket name; env: VENDCURL_BUCKET or S3_BUCKET",
    )
    cache.add_argument(
        "--prefix",
        default=os.getenv("VENDCURL_PREFIX", "vendcurl"),
        help="key prefix inside the bucket; env: VENDCURL_PREFIX; default: vendcurl",
    )
    cache.add_argument(
        "--endpoint-url",
        default=(
            os.getenv("VENDCURL_ENDPOINT_URL")
            or os.getenv("AWS_ENDPOINT_URL_S3")
            or os.getenv("S3_ENDPOINT_URL")
        ),
        help="S3-compatible endpoint URL; env: VENDCURL_ENDPOINT_URL, AWS_ENDPOINT_URL_S3, or S3_ENDPOINT_URL",
    )
    cache.add_argument(
        "--region",
        default=os.getenv("VENDCURL_REGION") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
        help="AWS/S3 region; env: VENDCURL_REGION, AWS_REGION, or AWS_DEFAULT_REGION",
    )
    cache.add_argument(
        "--profile",
        default=os.getenv("VENDCURL_PROFILE") or os.getenv("AWS_PROFILE"),
        help="AWS profile name; env: VENDCURL_PROFILE or AWS_PROFILE",
    )
    cache.add_argument(
        "--refresh",
        action="store_true",
        help="ignore any URL manifest and fetch the origin URL, then update the vendored copy",
    )
    cache.add_argument(
        "--no-origin",
        action="store_true",
        help="do not fetch the origin URL; fail unless a valid vendored copy exists",
    )
    cache.add_argument(
        "--no-store",
        action="store_true",
        help="download and validate only; skip S3 lookup/upload entirely",
    )

    integrity = parser.add_argument_group("integrity")
    integrity.add_argument(
        "--sha256",
        "--expected-sha256",
        dest="expected_sha256",
        help="expected SHA-256 hex digest; strongly recommended for reproducible vendoring",
    )
    integrity.add_argument(
        "--validator",
        choices=["auto", "none", "tar", "zip", "gzip", "xz", "bz2"],
        default="auto",
        help="structural validator to run; default: auto from filename/content-type",
    )
    integrity.add_argument(
        "--allow-unsafe-archive-paths",
        action="store_true",
        help="allow archive members with absolute paths, drive letters, or parent traversal",
    )

    network = parser.add_argument_group("network")
    network.add_argument(
        "--retries",
        type=non_negative_int,
        default=non_negative_int(os.getenv("VENDCURL_RETRIES", "3")),
        help="additional origin-download attempts after the first; env: VENDCURL_RETRIES; default: 3",
    )
    network.add_argument(
        "--timeout",
        type=positive_float,
        default=positive_float(os.getenv("VENDCURL_TIMEOUT", "60")),
        help="socket timeout in seconds; env: VENDCURL_TIMEOUT; default: 60",
    )
    network.add_argument(
        "--user-agent",
        default=os.getenv("VENDCURL_USER_AGENT", f"vendcurl/{__version__}"),
        help="User-Agent for origin requests; env: VENDCURL_USER_AGENT",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--json", dest="json_output", action="store_true", help="print machine-readable result JSON")
    output.add_argument("--quiet", action="store_true", help="suppress progress and summary messages")
    return parser


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> InstallResult:
    expected_sha256 = normalize_sha256(args.expected_sha256) if args.expected_sha256 else None
    output = resolve_output_path(args.output, args.url)
    output.parent.mkdir(parents=True, exist_ok=True)

    reject_unsafe_paths = not args.allow_unsafe_archive_paths
    store: S3Store | None = None
    manifest_key: str | None = None
    blob_key_for_expected: str | None = None

    if args.no_origin and args.no_store:
        raise VendcurlError("--no-origin cannot be combined with --no-store")

    if not args.no_store:
        if not args.bucket:
            parser.error("--bucket is required unless --no-store is used")
        config = S3Config(
            bucket=args.bucket,
            prefix=args.prefix,
            endpoint_url=args.endpoint_url,
            region_name=args.region,
            profile_name=args.profile,
        )
        store = S3Store(config)
        manifest_key = url_manifest_key(args.prefix, args.url)
        if expected_sha256:
            blob_key_for_expected = blob_key(args.prefix, expected_sha256)

    if store is not None and manifest_key is not None and not args.refresh:
        with temp_path_for(output) as tmp:
            try:
                manifest = store.get_json(manifest_key)
                if manifest is None:
                    raise CacheMiss("URL manifest not found")
                result = try_install_from_manifest(
                    store,
                    manifest,
                    args.url,
                    output,
                    tmp,
                    args.validator,
                    expected_sha256,
                    reject_unsafe_paths,
                    manifest_key,
                    quiet=args.quiet,
                )
                return result
            except CacheMiss as exc:
                if args.no_origin:
                    raise VendcurlError(f"no usable vendored copy: {exc}") from exc
                log(args, f"cache miss: {exc}; fetching origin")
            except ObjectStoreError as exc:
                if args.no_origin:
                    raise VendcurlError(str(exc)) from exc
                log(args, f"object-store lookup failed: {exc}; fetching origin")

    if args.no_origin:
        # Optional escape hatch: if the caller knows the digest, we can try the
        # blob directly even when the URL manifest is missing/stale.
        if store is not None and expected_sha256 and blob_key_for_expected is not None:
            with temp_path_for(output) as tmp:
                if not store.download_file(blob_key_for_expected, tmp):
                    raise VendcurlError(f"no vendored blob found for sha256={expected_sha256}")
                actual = sha256_file(tmp)
                if actual != expected_sha256:
                    raise VendcurlError(
                        f"vendored blob checksum mismatch: expected {expected_sha256}, got {actual}"
                    )
                validator = choose_validator(args.validator, None, args.url, output, None)
                validate(tmp, validator, reject_unsafe_paths)
                size = tmp.stat().st_size
                os.replace(tmp, output)
                return InstallResult(
                    status="hit",
                    url=args.url,
                    output=str(output),
                    sha256=actual,
                    size=size,
                    validator=validator,
                    blob_key=blob_key_for_expected,
                    manifest_key=manifest_key,
                )
        raise VendcurlError("origin fetching disabled and no usable cache entry exists")

    with temp_path_for(output) as tmp:
        log(args, f"fetching origin: {args.url}")
        try:
            download = download_origin(
                args.url,
                tmp,
                timeout=args.timeout,
                retries=args.retries,
                user_agent=args.user_agent,
            )
        except Exception as exc:
            raise VendcurlError(f"origin download failed: {exc}") from exc

        if expected_sha256 and download.sha256 != expected_sha256:
            raise VendcurlError(
                f"checksum mismatch: expected {expected_sha256}, got {download.sha256}"
            )

        validator = choose_validator(args.validator, None, args.url, output, download.content_type)
        validate(tmp, validator, reject_unsafe_paths)

        actual_blob_key: str | None = None
        if store is not None and manifest_key is not None:
            actual_blob_key = blob_key(args.prefix, download.sha256)
            manifest = build_manifest(
                url=args.url,
                output=output,
                download=download,
                validator=validator,
                blob_key=actual_blob_key,
            )
            metadata = {
                "sha256": download.sha256,
                "vendcurl-url-sha256": sha256_text(args.url),
                "vendcurl-validator": validator,
            }
            store.upload_file_if_missing(tmp, actual_blob_key, metadata=metadata)
            store.put_json(manifest_key, manifest)
            log(args, f"vendored: s3://{args.bucket}/{actual_blob_key}")

        os.replace(tmp, output)
        return InstallResult(
            status="fetched",
            url=args.url,
            output=str(output),
            sha256=download.sha256,
            size=download.size,
            validator=validator,
            blob_key=actual_blob_key,
            manifest_key=manifest_key,
            final_url=download.final_url,
        )


def try_install_from_manifest(
    store: S3Store,
    manifest: dict[str, Any],
    url: str,
    output: Path,
    tmp: Path,
    requested_validator: str,
    expected_sha256: str | None,
    reject_unsafe_paths: bool,
    manifest_key: str,
    *,
    quiet: bool,
) -> InstallResult:
    manifest_sha = as_string(manifest.get("sha256"), "manifest.sha256")
    normalize_sha256(manifest_sha)
    if expected_sha256 and manifest_sha != expected_sha256:
        raise CacheMiss(
            f"manifest digest {manifest_sha} differs from requested digest {expected_sha256}"
        )

    key = manifest.get("blob_key")
    if not isinstance(key, str) or not key:
        key = blob_key(store.config.prefix, manifest_sha)

    if not store.download_file(key, tmp):
        raise CacheMiss(f"blob not found: {key}")

    actual = sha256_file(tmp)
    if actual != manifest_sha:
        raise CacheMiss(f"blob digest mismatch: manifest {manifest_sha}, downloaded {actual}")

    content_type = manifest.get("content_type") if isinstance(manifest.get("content_type"), str) else None
    manifest_validator = manifest.get("validator") if isinstance(manifest.get("validator"), str) else None
    validator = choose_validator(requested_validator, manifest_validator, url, output, content_type)
    validate(tmp, validator, reject_unsafe_paths)
    size = tmp.stat().st_size
    os.replace(tmp, output)
    return InstallResult(
        status="hit",
        url=url,
        output=str(output),
        sha256=actual,
        size=size,
        validator=validator,
        blob_key=key,
        manifest_key=manifest_key,
        final_url=manifest.get("final_url") if isinstance(manifest.get("final_url"), str) else None,
    )


def download_origin(
    url: str,
    target: Path,
    *,
    timeout: float,
    retries: int,
    user_agent: str,
) -> DownloadResult:
    attempts = retries + 1
    last_exc: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return _download_origin_once(url, target, timeout=timeout, user_agent=user_agent)
        except HTTPError as exc:
            last_exc = exc
            if exc.code not in _RETRYABLE_HTTP_STATUSES or attempt >= attempts:
                raise
        except (URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt >= attempts:
                raise

        sleep_for = min(2 ** (attempt - 1), 30)
        time.sleep(sleep_for)

    assert last_exc is not None
    raise last_exc


def _download_origin_once(url: str, target: Path, *, timeout: float, user_agent: str) -> DownloadResult:
    request = Request(url, headers={"User-Agent": user_agent})
    hasher = hashlib.sha256()
    size = 0

    with contextlib.closing(urlopen(request, timeout=timeout)) as response:
        final_url = response.geturl()
        content_type = response.headers.get("Content-Type")
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")

        with target.open("wb") as out:
            while True:
                chunk = response.read(_CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
                hasher.update(chunk)
                size += len(chunk)

    return DownloadResult(
        sha256=hasher.hexdigest(),
        size=size,
        final_url=final_url,
        content_type=content_type,
        etag=etag,
        last_modified=last_modified,
    )


def build_manifest(
    *,
    url: str,
    output: Path,
    download: DownloadResult,
    validator: str,
    blob_key: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "url": url,
        "url_sha256": sha256_text(url),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "final_url": download.final_url,
        "output_name": output.name,
        "sha256": download.sha256,
        "size": download.size,
        "validator": validator,
        "content_type": download.content_type,
        "etag": download.etag,
        "last_modified": download.last_modified,
        "blob_key": blob_key,
    }


def choose_validator(
    requested: str,
    manifest_validator: str | None,
    url: str,
    output: Path,
    content_type: str | None,
) -> str:
    if requested != "auto":
        return requested
    detected = detect_validator(url, output, content_type)
    if detected != "none":
        return detected
    if manifest_validator in {"none", "tar", "zip", "gzip", "xz", "bz2"}:
        return manifest_validator
    return "none"


def validate(path: Path, validator: str, reject_unsafe_paths: bool) -> None:
    try:
        validate_file(path, validator, reject_unsafe_paths=reject_unsafe_paths)
    except ValidationError as exc:
        raise VendcurlError(f"validation failed with {validator!r}: {exc}") from exc


def resolve_output_path(output_arg: str | None, url: str) -> Path:
    inferred = infer_filename_from_url(url)
    if output_arg is None:
        return Path(inferred)
    output = Path(output_arg)
    if output_arg.endswith((os.sep, "/")) or output.is_dir():
        return output / inferred
    return output


@contextlib.contextmanager
def temp_path_for(output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".vendcurl-tmp",
        dir=str(output.parent),
    )
    os.close(fd)
    tmp = Path(name)
    try:
        yield tmp
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(_CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def url_manifest_key(prefix: str, url: str) -> str:
    return join_key(prefix, "manifests", "by-url", f"{sha256_text(url)}.json")


def blob_key(prefix: str, sha256: str) -> str:
    return join_key(prefix, "blobs", "sha256", sha256[:2], sha256)


def normalize_sha256(value: str) -> str:
    digest = value.strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise VendcurlError(f"not a SHA-256 hex digest: {value!r}")
    return digest


def as_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CacheMiss(f"missing or invalid {name}")
    return value


def non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return parsed


def log(args: argparse.Namespace, message: str) -> None:
    if not args.quiet:
        print(f"vendcurl: {message}", file=sys.stderr)
