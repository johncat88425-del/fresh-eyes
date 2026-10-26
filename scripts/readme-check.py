"""Check your own README: install and start your Python CLI exactly as the README says, in a fresh container.

For maintainers, locally or in CI (see action.yml). Needs Docker; reads no keys and calls no model.

  python3 scripts/readme-check.py PATH [--out DIR] [--summary FILE] [--fail-on unresolved,needs_input]

Exit code: 1 when the result is one of --fail-on (default: unresolved), 2 when the check itself could not run,
otherwise 0. A start-up check never says that the program's output is correct.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("github_check", Path(__file__).with_name("github-check.py"))
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)
STATUSES = {"startup_only", "unresolved", "needs_input", "unsupported", "infrastructure_error"}


def main(argv=None, environ=os.environ, runner=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--out", help="New directory for report.json/report.html (default: a temporary one)")
    parser.add_argument("--summary", help="Append the Markdown result to this file (e.g. $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--fail-on", default="unresolved")
    args = parser.parse_args(argv)
    fail_on = {s.strip() for s in args.fail_on.split(",") if s.strip()}
    if fail_on - STATUSES:
        parser.error("--fail-on takes: " + ", ".join(sorted(STATUSES)))
    from fresh_eyes.github_source import SourceError
    from fresh_eyes.local_check import check_checkout
    from fresh_eyes.runner import DockerRunner
    machine = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower(), "amd64")
    pull_error = None
    if runner is None:
        # The documented base image for this machine's architecture; pulling it is the only download besides PyPI.
        # No Docker, or a pull that hangs, is reported below as "the check could not complete", not as a crash.
        try:
            pulled = subprocess.run(["docker", "pull", "--platform", f"linux/{machine}", "python:3.12-slim"], capture_output=True, timeout=600)
            if pulled.returncode:
                pull_error = "Docker could not prepare the Python image; no repository code was run."
        except (OSError, subprocess.TimeoutExpired):
            pull_error = "Docker is unavailable or image preparation timed out; no repository code was run."
        runner = DockerRunner(image="python:3.12-slim", platform=f"linux/{machine}", dependency_mode="pypi_live")
    repo = f"https://github.com/{environ['GITHUB_REPOSITORY']}" if environ.get("GITHUB_REPOSITORY") else None
    work = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="fresh-eyes-readme-")) / "check"
    existing_output = work.exists()
    try:
        if existing_output:
            raise FileExistsError("Use a new output directory; earlier evidence is never overwritten.")
        if pull_error:
            raise RuntimeError(pull_error)
        report = check_checkout(args.path, work, runner=runner, repo_url=repo, commit_sha=environ.get("GITHUB_SHA"))
        result = {**render.summarise(report), "repo_url": repo}
    except SourceError as exc:
        result = {"status": "unsupported", "reason": f"{exc.code}: {exc}", "executed": False}
    except Exception as exc:  # report, don't hide: the check itself failed
        result = {"status": "infrastructure_error", "reason": pull_error or f"The check stopped: {type(exc).__name__}", "executed": False}
    result["model_calls"] = 0
    if not existing_output:
        report_dir = work / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        if not (report_dir / "report.json").exists():
            # A command/preflight failure is evidence of non-execution, not an engine run.
            body = {**result, "schema_version": 1, "data_origin": "local_check_command", "verification_level": "startup"}
            (report_dir / "report.json").write_text(json.dumps(body, indent=2) + "\n")
        (work / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    where = "on this CI runner" if environ.get("CI") else "on this machine"
    text = render.comment(result, where=where)
    sys.stdout.write(text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as stream:
            stream.write(text)
    print(json.dumps({"status": result["status"], "report": str(work / "report")}), file=sys.stderr)
    if result["status"] == "infrastructure_error":
        return 2
    return 1 if result["status"] in fail_on else 0


if __name__ == "__main__":
    sys.exit(main())
