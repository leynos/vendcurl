from __future__ import annotations

import gzip
import io
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from vendcurl.cli import blob_key, resolve_output_path, url_manifest_key
from vendcurl.validators import ValidationError, detect_validator, is_unsafe_archive_name, validate_file


class ValidatorTests(unittest.TestCase):
    def test_detects_common_archive_extensions(self) -> None:
        self.assertEqual(detect_validator("https://example.test/pkg.tar.gz"), "tar")
        self.assertEqual(detect_validator("https://example.test/pkg.tgz"), "tar")
        self.assertEqual(detect_validator("https://example.test/pkg.zip"), "zip")
        self.assertEqual(detect_validator("https://example.test/pkg.whl"), "zip")
        self.assertEqual(detect_validator("https://example.test/pkg.gz"), "gzip")
        self.assertEqual(detect_validator("https://example.test/pkg.bin"), "none")

    def test_safe_and_unsafe_archive_names(self) -> None:
        self.assertFalse(is_unsafe_archive_name("foo/bar.txt"))
        self.assertTrue(is_unsafe_archive_name("../bar.txt"))
        self.assertTrue(is_unsafe_archive_name("/tmp/bar.txt"))
        self.assertTrue(is_unsafe_archive_name("C:/tmp/bar.txt"))
        self.assertTrue(is_unsafe_archive_name("foo\\..\\bar.txt"))

    def test_validates_zip_and_rejects_zip_slip_paths(self) -> None:
        with TemporaryDirectory() as tmpdir:
            safe = Path(tmpdir) / "safe.zip"
            with zipfile.ZipFile(safe, "w") as zf:
                zf.writestr("pkg/file.txt", "hello")
            validate_file(safe, "zip")

            unsafe = Path(tmpdir) / "unsafe.zip"
            with zipfile.ZipFile(unsafe, "w") as zf:
                zf.writestr("../evil.txt", "hello")
            with self.assertRaises(ValidationError):
                validate_file(unsafe, "zip")

    def test_validates_tar_gz(self) -> None:
        with TemporaryDirectory() as tmpdir:
            archive = Path(tmpdir) / "safe.tar.gz"
            payload = b"hello"
            info = tarfile.TarInfo("pkg/file.txt")
            info.size = len(payload)
            with tarfile.open(archive, "w:gz") as tf:
                tf.addfile(info, io.BytesIO(payload))
            validate_file(archive, "tar")

    def test_validates_gzip_stream(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "x.gz"
            with gzip.open(path, "wb") as fh:
                fh.write(b"hello")
            validate_file(path, "gzip")

            path.write_bytes(b"not gzip")
            with self.assertRaises(ValidationError):
                validate_file(path, "gzip")


class CliHelperTests(unittest.TestCase):
    def test_s3_keys_are_content_addressed(self) -> None:
        digest = "a" * 64
        self.assertEqual(blob_key("prefix", digest), f"prefix/blobs/sha256/aa/{digest}")
        key = url_manifest_key("prefix", "https://example.test/a.tar.gz")
        self.assertTrue(key.startswith("prefix/manifests/by-url/"))
        self.assertTrue(key.endswith(".json"))

    def test_output_path_defaults_to_url_basename(self) -> None:
        self.assertEqual(resolve_output_path(None, "https://example.test/path/pkg.tar.gz"), Path("pkg.tar.gz"))
        with TemporaryDirectory() as tmpdir:
            self.assertEqual(
                resolve_output_path(tmpdir + "/", "https://example.test/path/pkg.tar.gz"),
                Path(tmpdir) / "pkg.tar.gz",
            )


if __name__ == "__main__":
    unittest.main()
