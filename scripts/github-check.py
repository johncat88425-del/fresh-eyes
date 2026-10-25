"""Free manual GitHub Actions README startup checks. No issue trigger or message.

The check job reads a public repository URL and optional commit from environment
variables, runs inspect_repo + run_startup in a non-root PyPI-only container, and
writes evidence. A separate read-only job renders the result in its job summary.
No provider key is loaded and no model or repair is invoked.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# The image digest of the local real startup run (reports/github-rich-cli-startup-20261024-v1), so both agree.
IMAGE = "python@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704"
PLATFORM = "linux/arm64"
RUN_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]{1,20}")
REPO_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?(?=[\s)>\]]|$)")
SHA = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
MAX_TEXT = 1500


class RequestError(ValueError):
    pass


def parse_request(body):
    """One repository URL and at most one commit; anything else is refused before any download."""
    body = str(body or "")[:20000]
    repos = {f"https://github.com/{owner}/{name}" for owner, name in REPO_URL.findall(body)}
    if len(repos) != 1:
        raise RequestError("Put exactly one public GitHub repository URL in the form.")
    commits = set(SHA.findall(body))
    if len(commits) > 1:
        raise RequestError("Give at most one full 40-character commit.")
    return {"repo_url": repos.pop(), "ref": commits.pop() if commits else None}


def parse_inputs(repo, commit):
    repo, commit = str(repo or "").strip(), str(commit or "").strip()
    if not REPO_URL.fullmatch(repo):
        raise RequestError("Give one complete public GitHub repository URL.")
    if commit and not SHA.fullmatch(commit):
        raise RequestError("The optional commit must be exactly 40 lowercase hexadecimal characters.")
    return parse_request(repo + (" " + commit if commit else ""))


def summarise(report):
    plan = report.get("readme_plan") or {}
    attempt = (report.get("attempts") or [{}])[0].get("result") or {}
    steps = [{"command": " ".join(s.get("argv") or []), "exit_code": s.get("exit_code")}
             for s in attempt.get("steps") or [] if s.get("argv") and not str(s["argv"][0]).startswith("/")]
    outputs = [{"command": " ".join((o.get("process") or {}).get("argv") or []), "exit_code": o.get("exit_code"),
                "stdout": str((o.get("process") or {}).get("stdout") or o.get("content") or "")[:MAX_TEXT],
                "stderr": str((o.get("process") or {}).get("stderr") or "")[:MAX_TEXT]}
               for o in attempt.get("outputs") or []]
    return {"status": report.get("status"), "reason": report.get("reason"), "executed": bool(report.get("executed")),
            "commit_sha": (report.get("repo") or {}).get("commit_sha"), "readme": plan.get("readme"),
            "setup": plan.get("setup") or [], "startup": plan.get("startup"), "steps": steps, "outputs": outputs,
            "environment": report.get("environment"), "model_calls": report.get("model_calls", 0)}


def check(out, environ=os.environ, inspect=None, start=None, runner=None):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    result = {"schema_version": 1, "data_origin": "github_actions_startup_check", "model_calls": 0,
              "checked_at": datetime.now(timezone.utc).isoformat(),
              "run_url": None}
    candidate = f"https://github.com/{environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{environ.get('GITHUB_RUN_ID', '')}"
    if RUN_URL.fullmatch(candidate):
        result["run_url"] = candidate
    try:
        request = (parse_inputs(environ.get("REQUEST_REPO"), environ.get("REQUEST_COMMIT"))
                   if "REQUEST_REPO" in environ else parse_request(environ.get("ISSUE_BODY")))
    except RequestError as exc:
        result.update(status="invalid_request", reason=str(exc))
    else:
        result.update(request)
        from fresh_eyes.github_source import SourceError
        from fresh_eyes.runner import DockerRunner
        from fresh_eyes.startup import inspect_repo, run_startup
        inspect, start = inspect or inspect_repo, start or run_startup
        try:
            with tempfile.TemporaryDirectory(prefix="fresh-eyes-actions-") as directory:
                source = Path(directory) / "repo"
                inspection = inspect(request["repo_url"], request["ref"], source)
                report = start(inspection, source, out / "report",
                               runner=runner or DockerRunner(image=IMAGE, platform=PLATFORM, dependency_mode="pypi_live"))
                result.update(summarise(report))
        except SourceError as exc:
            result.update(status="unsupported" if exc.code not in {"source_unavailable", "source_timeout", "source_http_error"}
                          else "infrastructure_error", reason=f"{exc.code}: {exc}")
        except Exception as exc:  # the comment job must still be able to say the check didn't complete
            result.update(status="infrastructure_error", reason=f"The check stopped: {type(exc).__name__}")
    (out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result


VERDICT = {
    "startup_only": ("✅", "Installed and started as the README says", "The documented install and help/version command both worked in a clean container. This checks start-up only, not that the output is correct."),
    "unresolved": ("❌", "The README's install or start-up command failed", "Following the README exactly in a clean container did not get the program started. The commands and logs are below."),
    "needs_input": ("❔", "A person needs to choose", "The README doesn't have exactly one pip install command and one help/version command in a code block, so nothing was run rather than guessing."),
    "unsupported": ("⛔", "Not supported", "This kind of project or request is outside the current scope (small Python CLIs installed with pip)."),
    "infrastructure_error": ("⚠️", "Our runner failed", "This says nothing about the README; the check could not complete."),
    "invalid_request": ("✋", "Couldn't read the request", ""),
}


def fence(text):
    """Untrusted text in a code block that it cannot close."""
    text = str(text or "").replace("\r", "")[:MAX_TEXT]
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}text\n{text}\n{ticks}"


def comment(result):
    symbol, title, explain = VERDICT.get(result.get("status"), ("❔", str(result.get("status")), ""))
    repo = result.get("repo_url") or ""
    if repo and not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        repo = ""
    sha = result.get("commit_sha") if SHA.fullmatch(str(result.get("commit_sha") or "")) else None
    lines = [f"## {symbol} {title}", ""]
    if repo:
        lines += [f"**Repository:** {repo}" + (f" at `{sha[:12]}`" if sha else ""), ""]
    if explain:
        lines.append(explain)
    if result.get("status") == "invalid_request":
        lines.append(fence(result.get("reason")))
    elif result.get("reason"):
        lines += ["", "**Reason:**", fence(result.get("reason"))]
    readme = result.get("readme") if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(result.get("readme") or "")) else "README"
    for label, steps in (("README install", result.get("setup") or []),
                         ("README start-up", [result["startup"]] if result.get("startup") else [])):
        for step in steps:
            line = re.search(r":L([0-9]+)$", str(step.get("source", "")))
            lines += ["", f"**{label}** ({readme}" + (f" line {line[1]}" if line else "") + "):", fence(" ".join(step.get("argv") or []))]
    for step in result.get("steps") or []:
        lines += ["", f"**Ran:** exit code {int(step.get('exit_code')) if isinstance(step.get('exit_code'), int) else 'unknown'}", fence(step.get("command"))]
    for out in result.get("outputs") or []:
        code = out.get("exit_code") if isinstance(out.get("exit_code"), int) else "unknown"
        lines += ["", f"**Start-up command** (exit code {code}):", fence(out.get("command"))]
        if out.get("stderr"):
            lines += ["stderr (first 1,500 characters):", fence(out["stderr"])]
    environment = result.get("environment") if isinstance(result.get("environment"), dict) else {}
    lines += ["", "---"]
    if result.get("executed") is True and environment.get("image_id"):
        # Only what the recorded environment shows: the container platform and its network policy.
        network = ("Only PyPI was reachable (pypi.org, files.pythonhosted.org)."
                   if environment.get("network") == "pypi_proxy_isolated" else "Network policy: not recorded.")
        platform = environment.get("platform") if re.fullmatch(r"linux/[a-z0-9]{1,16}", str(environment.get("platform") or "")) else "linux"
        lines.append(f"Ran by [Fresh Eyes](https://johncat88425-del.github.io/fresh-eyes/) in a fresh, non-root {platform} "
                     f"container on a GitHub-hosted runner. {network}")
    else:
        lines.append("Fresh Eyes did not run anything from this repository.")
    lines.append("No AI model was called and no fix was attempted. This is a start-up check only: it does not show that "
                 "the program's output is correct.")
    if RUN_URL.fullmatch(str(result.get("run_url") or "")):
        lines.append(f"[Workflow run]({result['run_url']})")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("check")
    run.add_argument("--out", required=True)
    show = sub.add_parser("comment")
    show.add_argument("result")
    args = parser.parse_args(argv)
    if args.command == "check":
        print(json.dumps({k: v for k, v in check(args.out).items() if k in ("status", "reason", "repo_url")}))
    else:
        sys.stdout.write(comment(json.loads(Path(args.result).read_text())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
