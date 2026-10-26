"""A maintainer's own checkout goes through the same README plan and start-up path; synthetic runner, no Docker."""
import os
from pathlib import Path
import tempfile
import unittest

from fresh_eyes.github_source import SourceError
from fresh_eyes.local_check import check_checkout, copy_checkout
from test_github_startup import README, Runner


class LocalCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "checkout"
        (self.repo / ".git" / "objects").mkdir(parents=True)
        (self.repo / ".git" / "config").write_text("[core]\n")
        (self.repo / "README.md").write_text(README)
        (self.repo / "pyproject.toml").write_text("[project]\nname='unit'\n")

    def test_checkout_is_copied_without_vcs_metadata_and_recorded_by_hash(self):
        receipt = copy_checkout(self.repo, Path(self.temp.name) / "copy", repo_url="https://github.com/o/r", commit_sha="a" * 40)
        self.assertEqual(sorted(receipt["source_files"]), ["README.md", "pyproject.toml"])
        self.assertFalse((Path(self.temp.name) / "copy/.git").exists())
        self.assertEqual((receipt["repo_url"], receipt["commit_sha"], receipt["origin"]), ("https://github.com/o/r", "a" * 40, "local_checkout"))
        bad = copy_checkout(self.repo, Path(self.temp.name) / "copy2", repo_url="javascript:x", commit_sha="nope")
        self.assertEqual((bad["repo_url"], bad["commit_sha"]), (None, None))

    def test_local_credentials_and_runtime_state_never_reach_the_snapshot(self):
        for name in (".env", ".env.local", ".aws/credentials", ".ssh/id_rsa", ".fresh-eyes/spending-policy.json", "private.key"):
            f = self.repo / name; f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("synthetic-private-value-never-copy")
        target = Path(self.temp.name) / "safe"
        receipt = copy_checkout(self.repo, target)
        self.assertEqual(sorted(receipt["source_files"]), ["README.md", "pyproject.toml"])
        self.assertFalse(any(b"synthetic-private-value-never-copy" in f.read_bytes() for f in target.rglob("*") if f.is_file()))

    def test_links_and_oversized_files_are_refused(self):
        os.symlink("/etc/passwd", self.repo / "link")
        with self.assertRaises(SourceError):
            copy_checkout(self.repo, Path(self.temp.name) / "copy")
        (self.repo / "link").unlink()
        (self.repo / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        with self.assertRaises(SourceError):
            copy_checkout(self.repo, Path(self.temp.name) / "copy3")

    def test_ready_readme_runs_once_and_only_reports_start_up(self):
        runner = Runner()
        report = check_checkout(self.repo, Path(self.temp.name) / "work", runner=runner)
        self.assertEqual((report["status"], report["model_calls"], runner.calls), ("startup_only", 0, 1))
        self.assertEqual(runner.payload["workflow"]["argv"], ["unit", "--help"])
        self.assertTrue((Path(self.temp.name) / "work/report/report.json").is_file())

    def test_failing_start_up_is_unresolved_and_unclear_readme_runs_nothing(self):
        self.assertEqual(check_checkout(self.repo, Path(self.temp.name) / "w1", runner=Runner(False))["status"], "unresolved")
        (self.repo / "README.md").write_text("No commands here.\n")
        runner = Runner()
        report = check_checkout(self.repo, Path(self.temp.name) / "w2", runner=runner)
        self.assertEqual((report["status"], runner.calls), ("needs_input", 0))


if __name__ == "__main__":
    unittest.main()
