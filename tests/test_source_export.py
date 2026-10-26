"""Archive-only file link handling; no repository code is executed."""
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from fresh_eyes.contracts import InvalidCase
from fresh_eyes.engine import export_source


class SourceFileLinks(unittest.TestCase):
    def export(self, entries, destination):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as archive:
            for name, kind, content in entries:
                member = tarfile.TarInfo(name)
                if kind == "file":
                    data = content.encode()
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
                else:
                    member.type = tarfile.SYMTYPE if kind == "link" else tarfile.LNKTYPE
                    member.linkname = content
                    archive.addfile(member)
        with patch("fresh_eyes.engine.subprocess.check_output", side_effect=["a" * 40 + "\n", raw.getvalue()]):
            return export_source(Path("synthetic-repo"), "a" * 40, destination)

    def test_internal_relative_link_is_materialized_without_host_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "repo"
            result = self.export([("AUTHORS.rst", "file", "Synthetic authors\n"),
                                  ("docs/authors.md", "link", "../AUTHORS.rst")], output)
            self.assertEqual((output / "docs/authors.md").read_bytes(), (output / "AUTHORS.rst").read_bytes())
            self.assertFalse((output / "docs/authors.md").is_symlink())
            self.assertEqual(result["materialized_file_links"][0]["target"], "AUTHORS.rst")

    def test_link_chain_uses_only_archived_regular_file_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "repo"
            result = self.export([("a", "link", "b"), ("b", "link", "c"), ("c", "file", "safe")], output)
            self.assertEqual((output / "a").read_text(), "safe")
            self.assertEqual(len(result["materialized_file_links"]), 2)

    def test_external_missing_cyclic_and_hard_links_are_rejected_before_extraction(self):
        variants = [[("link", "link", "/etc/passwd")], [("link", "link", "../outside")],
                    [("link", "link", "missing")], [("a", "link", "b"), ("b", "link", "a")],
                    [("file", "file", "safe"), ("hard", "hard", "file")]]
        with tempfile.TemporaryDirectory() as tmp:
            for index, entries in enumerate(variants):
                output = Path(tmp) / str(index)
                with self.subTest(entries=entries), self.assertRaises(InvalidCase):
                    self.export(entries, output)
                self.assertFalse(output.exists())

    def test_link_cannot_be_used_as_archive_parent_even_if_target_exists(self):
        entries = [("safe", "file", "safe"), ("dir", "link", "safe"), ("dir/child", "file", "bad")]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(InvalidCase, "non-directory"):
                self.export(entries, Path(tmp) / "repo")


if __name__ == "__main__":
    unittest.main()
