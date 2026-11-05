import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import bcer_pilot as p


class FakeFTP:
    def __init__(self, data, declared=None):
        self.data = data
        self.declared = len(data) if declared is None else declared
        self.transfers = 0

    def voidcmd(self, command):
        pass

    def size(self, remote):
        return self.declared

    def retrbinary(self, command, callback, blocksize):
        self.transfers += 1
        for offset in range(0, len(self.data), 7):
            callback(self.data[offset:offset + 7])


class PilotTests(unittest.TestCase):
    def test_cloud_guard(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                p.require_cloud()

    def test_download_preserves_bytes_and_hashes(self):
        payload = b"%PDF-1.7\nsample raw bytes\n"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "raw.PDF"
            result = p.download(FakeFTP(payload), "/WellData/raw.PDF", path, 100)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(result["bytes"], len(payload))

    def test_oversize_rejected_before_download(self):
        ftp = FakeFTP(b"x" * 10)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                p.download(ftp, "/WellData/raw.LAS", Path(folder) / "raw.LAS", 5)
            self.assertEqual(ftp.transfers, 0)

    def test_truncated_download_not_promoted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "raw.LAS"
            with self.assertRaises(ValueError):
                p.download(FakeFTP(b"short", declared=20), "/WellData/raw.LAS", path, 100)
            self.assertFalse(path.exists())
            self.assertTrue(path.with_name("raw.LAS.part").exists())

    def test_html_not_accepted_as_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "raw.PDF"
            with self.assertRaises(ValueError):
                p.download(FakeFTP(b"<html>login</html>"), "/WellData/raw.PDF", path, 100)
            self.assertFalse(path.exists())

    def test_source_paths_and_duplicate_names(self):
        for text in ("remote_path\n/WellData/../secret\n",
                     "remote_path\n/WellData/a.LAS\n/WellData/sub/a.LAS\n"):
            with patch("builtins.open", return_value=io.StringIO(text)):
                with self.assertRaises(ValueError):
                    p.read_sources("sources.csv")

    def test_token_never_sent_to_other_host(self):
        with self.assertRaises(ValueError):
            p.Zenodo("test-token").call("GET", "https://example.com/api")

    def test_wrong_remote_checksum_retains_cloud_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "raw.LAS"
            path.write_bytes(b"original")
            z = p.Zenodo("test-token")
            z.draft = lambda _: {"submitted": False, "files": [
                {"filename": "raw.LAS", "filesize": 8, "checksum": "wrong"}]}
            with self.assertRaises(ValueError):
                z.upload(1, path)
            self.assertEqual(path.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
