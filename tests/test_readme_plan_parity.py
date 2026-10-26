"""The browser README reader (web/readme_plan.js) must reach the same plan as startup.documented_plan."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from fresh_eyes.startup import documented_plan

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
RUNNER = """
import { documentedPlan } from %s;
let input = ''; process.stdin.on('data', d => { input += d; });
process.stdin.on('end', () => {
  const cases = JSON.parse(input);
  process.stdout.write(JSON.stringify(cases.map(c => documentedPlan(c))));
});
"""
KEYS = ("status", "reason", "setup", "startup", "ignored_command_sources")

FENCE = "```"
CASES = {
    "ready": ("README.md", f"# Tool\n{FENCE}sh\npip install .\ntool --help\n{FENCE}\n", ["pyproject.toml"]),
    "dollar prompt and comment": ("README.md", f"{FENCE}\n$ pip install tool  # from PyPI\n$ tool --version\n{FENCE}\n", ["setup.py"]),
    "requirements file present": ("README.md", f"{FENCE}\npip install -r requirements.txt\npython -m tool -h\n{FENCE}\n", ["requirements.txt"]),
    "requirements file missing": ("README.md", f"{FENCE}\npip install -r requirements-dev.txt\ntool -h\n{FENCE}\n", ["pyproject.toml"]),
    "editable dev extra refused": ("README.md", f"{FENCE}\npip install -e \".[dev]\"\ntool --help\n{FENCE}\n", ["pyproject.toml"]),
    "two installs": ("README.md", f"{FENCE}\npip install .\npip install -U tool\ntool --help\n{FENCE}\n", ["pyproject.toml"]),
    "shell pipe ignored": ("README.md", f"{FENCE}\ncurl x | pip install -\ntool --help\npip install tool\n{FENCE}\n", ["pyproject.toml"]),
    "no help command": ("README.md", f"{FENCE}\npip install tool\ntool input.md\n{FENCE}\n", ["pyproject.toml"]),
    "outside fence ignored": ("README.md", "pip install tool\ntool --help\n", ["pyproject.toml"]),
    "tilde fence and python3 -m pip": ("README.md", "~~~\npython3 -m pip install tool==1.2,<2\ntool -V\n~~~\n", ["setup.cfg"]),
    "not python": ("README.md", f"{FENCE}\npip install tool\ntool --help\n{FENCE}\n", ["package.json"]),
    "url install refused": ("README.md", f"{FENCE}\npip install git+https://x/y\ntool --help\n{FENCE}\n", ["pyproject.toml"]),
    "rst literal block": ("README.rst", "Install::\n\n    pip install tool\n    tool --help\n\nDone.\n", ["setup.py"]),
    "rst code-block": ("README.rst", ".. code-block:: sh\n\n   pip install tool[cli]\n   python -m tool --help\n", ["pyproject.toml"]),
    "mid-word comment and quotes": ("README.md", f"{FENCE}\npip install 'tool'#pin\ntool --help#x\n{FENCE}\n", ["pyproject.toml"]),
    "unclosed quote skipped": ("README.md", f"{FENCE}\npip install \"tool\ntool --help\npip install tool\n{FENCE}\n", ["pyproject.toml"]),
    "sudo and git are not tools": ("README.md", f"{FENCE}\npip install tool\nsudo --help\ngit --version\ntool --help\n{FENCE}\n", ["pyproject.toml"]),
    "crlf and unicode line breaks": ("README.md", f"{FENCE}\r\npip install tool tool --help\r\n{FENCE}\r\n", ["pyproject.toml"]),
    "two readmes": ("README.md", f"{FENCE}\npip install tool\ntool --help\n{FENCE}\n", ["pyproject.toml", "readme.txt"]),
    "nested fence kinds": ("README.md", f"{FENCE}\n~~~\npip install tool\n{FENCE}\ntool --help\n", ["pyproject.toml"]),
}


@unittest.skipUnless(NODE, "node is required for the browser README reader")
class ReadmePlanParityTests(unittest.TestCase):
    def test_browser_reader_matches_the_backend_on_every_case(self):
        inputs, expected = [], []
        with tempfile.TemporaryDirectory() as tmp:
            for name, (readme, text, others) in CASES.items():
                root = Path(tmp) / str(len(inputs))
                root.mkdir()
                (root / readme).write_text(text, encoding="utf-8", newline="")
                for other in others:
                    (root / other).write_text("")
                plan = documented_plan(root)
                expected.append({k: plan.get(k) for k in KEYS if k in plan})
                raw = (root / readme).read_bytes()
                inputs.append({"rootFiles": sorted([readme, *others]), "readmeName": None,
                               "text": raw.decode("utf-8", errors="replace"), "size": len(raw)})
            runner = Path(tmp) / "runner.mjs"
            runner.write_text(RUNNER % json.dumps((ROOT / "web/readme_plan.js").as_uri()))
            out = subprocess.run([NODE, str(runner)], input=json.dumps(inputs), capture_output=True, text=True, check=True)
        actual = json.loads(out.stdout)
        for (name, _), want, got in zip(CASES.items(), expected, actual):
            with self.subTest(name):
                self.assertEqual({k: got.get(k) for k in want}, want)
                self.assertFalse(got.get("executed", False))

    def test_oversized_readme_is_unsupported_in_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "README.md").write_text("x" * 262145)
            (Path(tmp) / "pyproject.toml").write_text("")
            self.assertEqual(documented_plan(tmp)["status"], "unsupported")
            runner = Path(tmp) / "runner.mjs"
            runner.write_text(RUNNER % json.dumps((ROOT / "web/readme_plan.js").as_uri()))
            out = subprocess.run([NODE, str(runner)], input=json.dumps([{"rootFiles": ["README.md", "pyproject.toml"],
                                 "readmeName": None, "text": "x", "size": 262145}]), capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(out.stdout)[0]["status"], "unsupported")


if __name__ == "__main__":
    unittest.main()
