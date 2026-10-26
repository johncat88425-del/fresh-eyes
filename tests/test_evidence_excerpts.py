"""Cited installation lines survive bounded model context; no model calls."""
import tempfile
from pathlib import Path
import unittest

from fresh_eyes.contracts import Contract
from fresh_eyes.diagnostics import cited_ranges, context, file_evidence
from test_core import case_data


class EvidenceExcerpts(unittest.TestCase):
    def test_cited_middle_installation_and_usage_survive_without_changing_text(self):
        lines = ["Unrelated README paragraph " + "x" * 90 + "\n"] * 220
        lines[79:84] = ["### Installation\n", "\n", "```bash\n", "pip install -r requirements.txt\n", "```\n"]
        lines[139:142] = ["### Usage\n", "unit-tool input.json\n", "\n"]
        source = "".join(lines)
        data = case_data()
        data["readme_source"] = "README.md:L80-L84;L140-L142"
        data["setup"][0]["source"] = "README.md:L80-L84"
        result = file_evidence(source, data, "README.md")
        install = "".join(lines[79:84])
        self.assertIn(install, result["text"])
        self.assertEqual(source.count(install), 1)
        self.assertIn("unit-tool input.json", result["text"])
        self.assertEqual(result["missing_cited_ranges"], [])
        self.assertLessEqual(len(result["text"].encode()), 4096)

    def test_cited_lines_carry_real_numbers_without_changing_the_excerpt(self):
        lines = ["Unrelated README paragraph " + "x" * 90 + "\n"] * 220
        lines[79:84] = ["### Installation\n", "\n", "```bash\n", "pip install -r requirements.txt\n", "```\n"]
        data = case_data()
        data["readme_source"] = "README.md:L80-L84"
        data["setup"][0]["source"] = "README.md:L80-L84"
        result = file_evidence("".join(lines), data, "README.md")
        self.assertIn([83, "pip install -r requirements.txt"], result["cited_lines"])
        self.assertIn("```bash\npip install -r requirements.txt\n```", result["text"])  # the excerpt itself stays verbatim
        small = file_evidence("# Unit\npip install old\n", data, "README.md")
        self.assertEqual(small["cited_lines"], [])  # L80-L84 is past the end: nothing is numbered, it stays missing
        self.assertEqual(small["missing_cited_ranges"], [[80, 84]])
        data["readme_source"] = data["setup"][0]["source"] = "README.md:L2"
        self.assertEqual(file_evidence("# Unit\npip install old\n", data, "README.md")["cited_lines"], [[2, "pip install old"]])

    def test_semicolon_ranges_inherit_filename_and_do_not_use_other_files(self):
        data = case_data()
        data["readme_source"] = "README.md:L36-L45;L144-L153;pyproject.toml:L2-L4"
        self.assertEqual(cited_ranges(data, "README.md"), [(1, 1), (36, 45), (144, 153)])
        self.assertEqual(cited_ranges(data, "pyproject.toml"), [(2, 4)])

    def test_oversized_or_unread_citations_are_marked_missing(self):
        data = case_data()
        data["readme_source"] = "README.md:L1-L90;L300-L310"
        data["setup"][0]["source"] = "README.md:L1-L90"
        result = file_evidence("large line " * 50 + "\n" + ("x" * 100 + "\n") * 100, data, "README.md", complete=False)
        self.assertEqual(result["missing_cited_ranges"], [[1, 90], [300, 310]])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["text"].encode()), 4096)

    def test_small_file_is_verbatim_and_context_still_scrubs_control_secret(self):
        data = case_data()
        text = "pip install old\r\n"
        self.assertEqual(file_evidence(text, data, "README.md")["text"], text)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "README.md").write_text("SYNTHETIC-SECRET\npip install old\n")
            evidence = context(Contract.from_dict(data), {"result": {}}, repo, ("SYNTHETIC-SECRET",))
        self.assertNotIn("SYNTHETIC-SECRET", str(evidence))
        self.assertIn("[REDACTED]", evidence["files"]["README.md"]["text"])


if __name__ == "__main__":
    unittest.main()
