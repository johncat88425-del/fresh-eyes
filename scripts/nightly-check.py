"""One bounded README startup monitor run, with durable evidence and no model calls.

The same command runs in manual validation and the nightly workflow. Only the
actual GitHub event distinguishes a scheduled execution from a manual one.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

spec = importlib.util.spec_from_file_location("nightly_github_check", Path(__file__).with_name("github-check.py"))
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)


def prepare_image():
    result = subprocess.run(["docker", "pull", "--platform", gc.PLATFORM, gc.IMAGE],
                            capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError("The pinned Python image could not be prepared.")


def outcome(result):
    """A successful workflow requires evidence that the repository really ran."""
    if type(result.get("model_calls")) is not int or result["model_calls"] != 0:
        return "runner_error", 2
    status = result.get("status")
    if status == "startup_only" and result.get("executed") is True:
        return "passed", 0
    if status == "unresolved" and result.get("executed") is True:
        return "readme_failed", 1
    if status in {"needs_input", "unsupported"} and result.get("executed") is not True:
        return status, 1
    return "runner_error", 2


def monitor(repo, out, *, environ=os.environ, check=None, prepare=None):
    out = Path(out)
    if out.exists():
        raise FileExistsError("Use a new output directory; earlier monitoring evidence is never overwritten.")
    request = gc.parse_inputs(repo, "")  # refuse invalid URLs before pull or acquisition
    started = datetime.now(timezone.utc).isoformat()
    event = environ.get("GITHUB_EVENT_NAME", "local")
    trigger = {"schedule": "scheduled", "workflow_dispatch": "manual"}.get(event, "local")
    # Pass only the public provenance fields needed by the existing startup path.
    env = {k: str(environ[k]) for k in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID") if k in environ}
    env["REQUEST_REPO"] = request["repo_url"]
    try:
        (prepare or prepare_image)()
        result = (check or gc.check)(out, environ=env)
    except Exception as exc:
        out.mkdir(parents=True, exist_ok=True)
        result = {"schema_version": 1, "data_origin": "nightly_monitor_failure",
                  "status": "infrastructure_error", "reason": f"Monitor stopped: {type(exc).__name__}",
                  "repo_url": request["repo_url"], "executed": False, "model_calls": 0}
        # Preserve any original partial evidence, rather than replacing it.
        name = "result.json" if not (out / "result.json").exists() else "monitor-error.json"
        (out / name).write_text(json.dumps(result, indent=2) + "\n")
    verdict, code = outcome(result)
    commit = str(environ.get("GITHUB_SHA", ""))
    tool_commit = commit if gc.SHA.fullmatch(commit) else None
    attempt = str(environ.get("GITHUB_RUN_ATTEMPT", ""))
    candidate = f"https://github.com/{env.get('GITHUB_REPOSITORY', '')}/actions/runs/{env.get('GITHUB_RUN_ID', '')}"
    record = {"schema_version": 1, "data_origin": "nightly_startup_monitor",
              "trigger": trigger, "github_event": event if event in {"schedule", "workflow_dispatch"} else "local",
              "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
              "repo_url": request["repo_url"], "requested_ref": "default_branch_at_execution",
              "resolved_commit": result.get("commit_sha") if gc.SHA.fullmatch(str(result.get("commit_sha", ""))) else None,
              "tool_commit": tool_commit, "run_attempt": int(attempt) if attempt.isdigit() else None,
              "run_url": candidate if gc.RUN_URL.fullmatch(candidate) else None,
              "verdict": verdict, "result_status": result.get("status"),
              "repository_executed": result.get("executed") is True,
              "model_calls": result.get("model_calls"), "search_calls": 0,
              "verification_level": "startup", "functional_output_verified": False,
              "repair_attempted": False, "pinned_image": gc.IMAGE, "platform": gc.PLATFORM,
              "limitations": ["Installation and documented help/version command only; not functional validation.",
                              "Each run resolves the current default-branch README, then pins its commit.",
                              "PyPI dependencies are installed anew. A changed result alone does not prove dependency drift.",
                              "GitHub scheduling can be delayed or skipped; this is not an always-on service."]}
    (out / "monitor.json").write_text(json.dumps(record, indent=2) + "\n")
    summary = ("# Fresh Eyes nightly README monitor\n\n"
               f"**Trigger:** {trigger}. **Monitor outcome:** {verdict}.\n\n"
               "Manual validation is not evidence that the schedule has fired.\n\n" + gc.comment(result) +
               "\nThe artifact includes monitor.json (event and pinned versions), result.json and available full reports. "
               "The report's pip_freeze records installed versions when installation reaches that stage.\n")
    (out / "summary.md").write_text(summary)
    files = {}
    for path in sorted(out.rglob("*")):
        if path.is_symlink():
            raise ValueError("Monitoring evidence cannot contain symbolic links.")
        if path.is_file():
            files[path.relative_to(out).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    (out / "manifest.json").write_text(json.dumps({"schema_version": 1, "files": files}, indent=2) + "\n")
    return record, code, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--summary")
    args = parser.parse_args(argv)
    try:
        record, code, summary = monitor(args.repo_url, args.out)
    except (ValueError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.summary:
        with Path(args.summary).open("a") as stream:
            stream.write(summary)
    print(json.dumps(record))
    return code


if __name__ == "__main__":
    sys.exit(main())
