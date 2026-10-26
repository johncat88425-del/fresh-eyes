#!/usr/bin/env python3
"""Run the prepared real-repository cases; no API key or simulated outcomes."""
import argparse
from pathlib import Path
import subprocess
import sys

project = Path(__file__).resolve().parents[1]
workspace = project.parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--case", choices=["mistletoe", "md2html"], default="mistletoe")
parser.add_argument("--candidate-patch", action="store_true", help="Try the unverified manual md2html candidate patch")
parser.add_argument("--dependency-mode", choices=["offline_snapshot", "pypi_live"], default="offline_snapshot")
args = parser.parse_args()
if args.candidate_patch and args.case != "md2html":
    parser.error("The prepared candidate patch only belongs to md2html")
cmd = [sys.executable, str(project / "scripts/fresh-eyes"), "run", str(project / f"cases/{args.case}.json"),
       "--source", str(workspace / f"work/candidates/{args.case}"), "--wheelhouse", str(workspace / "work/wheelhouse"),
       "--output", str(project / (f"reports/{args.case}" + ("-candidate" if args.candidate_patch else ""))),
       "--docker", str(project / "scripts/docker-local"), "--development", "--dependency-mode", args.dependency_mode]
if args.candidate_patch:
    cmd.extend(["--patch", str(project / "cases/md2html-candidate-patch.json")])
raise SystemExit(subprocess.call(cmd))
