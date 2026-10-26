"""README start-up check of a local checkout (B, 2026-10-25), for maintainers running Fresh Eyes in their own CI.

Same path as the public GitHub check: the README plan from startup.documented_plan, then startup.run_startup in a fresh
non-root container that can reach only PyPI. The only difference is where the source comes from: a copy of the
maintainer's own checkout, under the same limits as a GitHub archive (no links, no VCS metadata, size caps).
No model is called and no key is read.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re

from .contracts import digest
from .github_source import MAX_EXPANDED, MAX_FILE, MAX_FILES, SourceError
from .startup import documented_plan, run_startup

# Local credential/runtime state is never part of the execution snapshot.
SKIPPED = {".git", ".hg", ".svn", ".fresh-eyes", ".venv", "venv", "node_modules",
           "__pycache__", ".pytest_cache", ".ssh", ".aws", ".config", ".pypirc", ".npmrc"}


def excluded(relative):
    return any(part in SKIPPED or part == ".env" or part.startswith(".env.")
               or part in {"id_rsa", "id_ed25519"} or part.endswith((".pem", ".key"))
               for part in relative.parts)


def copy_checkout(path, destination, *, repo_url=None, commit_sha=None):
    """Copy the regular files of a checkout, refusing links and anything over the archive limits."""
    root, destination = Path(path).resolve(), Path(destination)
    if not root.is_dir():
        raise SourceError("unsupported_source", "Give the path of a checked-out repository.")
    if destination.exists():
        raise SourceError("source_exists", "Use a new source directory; an older copy is never overwritten.")
    files, expanded = {}, 0
    for item in sorted(root.rglob("*")):
        relative = item.relative_to(root)
        if excluded(relative):
            continue
        if item.is_symlink():
            raise SourceError("unsafe_source", f"Links are unsupported: {relative.as_posix()}")
        if not item.is_file():
            continue
        size = item.stat().st_size
        expanded += size
        if size > MAX_FILE or expanded > MAX_EXPANDED or len(files) >= MAX_FILES:
            raise SourceError("source_too_large", "Checkout exceeds the 2 MiB file, 32 MiB total or 2000-file limit.")
        files[relative.as_posix()] = item.read_bytes()
    if not files:
        raise SourceError("unsupported_source", "Checkout has no regular files.")
    destination.mkdir(parents=True)
    for name, content in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o644)
    valid_url = repo_url if isinstance(repo_url, str) and re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_url) else None
    valid_sha = commit_sha if isinstance(commit_sha, str) and re.fullmatch(r"[a-f0-9]{40}", commit_sha) else None
    return {"schema_version": 1, "origin": "local_checkout", "repo_url": valid_url, "commit_sha": valid_sha,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "source_files": {name: hashlib.sha256(content).hexdigest() for name, content in sorted(files.items())},
            "file_count": len(files), "expanded_bytes": expanded,
            "executed": False, "model_calls": 0, "credentials_used": False,
            "limitations": ["A copy of the local checkout; VCS metadata and known local credential/runtime files are excluded.",
                            "Links and large repositories are unsupported, as for GitHub archives."]}


def check_checkout(path, work, *, runner, repo_url=None, commit_sha=None):
    """Plan the README and run the documented install and help/version command. Returns the start-up report."""
    work = Path(work)
    source = work / "source"
    receipt = copy_checkout(path, source, repo_url=repo_url, commit_sha=commit_sha)
    plan = documented_plan(source)
    inspection = {"schema_version": 1, "source": receipt, "plan": plan, "plan_sha256": digest(plan),
                  "executed": False, "model_calls": 0, "data_origin": "local_checkout_inspection"}
    return run_startup(inspection, source, work / "report", runner=runner)
