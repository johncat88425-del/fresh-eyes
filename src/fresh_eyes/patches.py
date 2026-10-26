"""Exact or line-anchored replacements, structural TOML checks, no generic shell or source edits."""

import copy
import difflib
from pathlib import Path
import re
import tomllib

from .contracts import InvalidCase, relative_path, validate_steps


class PatchMismatch(InvalidCase):
    """An admissible target did not match; a later authorized round may revise it."""


def _without_dependencies(data):
    d = copy.deepcopy(data)
    project = d.get("project", {})
    project.pop("dependencies", None)
    project.pop("optional-dependencies", None)
    return d


def _normal(text):
    return " ".join(text.split())


def _line_edit(name, text, edit, secrets=()):
    """A line edit names 1-based lines of the original file and quotes what they say now (WIN-3).

    The quote is compared ignoring whitespace only, so a model that saw the cited lines can't miss on spacing,
    while a wrong line number is still refused and reported with what those lines really say."""
    start, end, old, new = edit.get("start"), edit.get("end"), edit.get("old"), edit.get("new")
    if not isinstance(old, str) or not old.strip() or not isinstance(new, str):
        raise InvalidCase("A line edit needs the current text of its lines and a replacement")
    lines = text.splitlines(keepends=True)
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines) or end - start >= 200:
        raise InvalidCase(f"Line range {start}-{end} is outside {name}")
    current = "".join(lines[start - 1:end])
    if _normal(current) != _normal(old):
        # The message reaches the saved report and the next round, so it gets the same redaction as model evidence.
        from .diagnostics import scrub
        raise PatchMismatch(f"Lines {start}-{end} of {name} don't match the quoted text; they read: {scrub(current, secrets)[:300]!r}")
    if new and current.endswith("\n") and not new.endswith("\n"):
        new += "\n"
    return start, end, new


def apply_patch(root: Path, bundle: dict, *, secrets=(), protected_paths=()) -> str:
    edits = bundle.get("edits", [])
    if not edits or len(edits) > 8:
        raise InvalidCase("Patch requires 1–8 exact replacements")
    diffs = []
    pending = {}
    by_file = {}
    for edit in edits:
        name = relative_path(edit["path"])
        if name in protected_paths:
            raise InvalidCase("Patch cannot modify a locked cwd_files input")
        path = root / name
        if any(p.is_symlink() for p in [path, *path.parents]) or not path.is_file():
            raise InvalidCase("Patch targets must be existing regular files")
        if name != "README.md" and name != "pyproject.toml" and not re.fullmatch(r"requirements[^/]*\.txt", name):
            raise InvalidCase(f"Patch is outside prototype scope: {name}")
        by_file.setdefault(name, []).append(edit)
    for name, file_edits in by_file.items():
        old_file = (root / name).read_text(encoding="utf-8")
        lined = ["start" in edit or "end" in edit for edit in file_edits]
        if any(lined) and not all(lined):
            raise InvalidCase(f"Edits to {name} must all quote exact text or all name lines")
        if all(lined):
            # Line numbers always refer to the original file; apply from the bottom so earlier ranges keep their numbers.
            spans = sorted((_line_edit(name, old_file, edit, secrets) for edit in file_edits), reverse=True)
            if any(later[0] <= earlier[1] for later, earlier in zip(spans, spans[1:])):
                raise InvalidCase(f"Line ranges in {name} overlap")
            lines = old_file.splitlines(keepends=True)
            for start, end, new in spans:
                lines[start - 1:end] = [new]
            new_file = "".join(lines)
        else:
            new_file = old_file
            for edit in file_edits:
                old, new = edit.get("old"), edit.get("new")
                if not isinstance(old, str) or not old or not isinstance(new, str):
                    raise InvalidCase("Each replacement must match exactly once")
                matches = new_file.count(old)
                if matches != 1:
                    raise PatchMismatch(f"Replacement in {name} matched {matches} times; expected exactly once")
                new_file = new_file.replace(old, new, 1)
        if len(new_file.encode()) > 262144:
            raise InvalidCase("Patched file is too large")
        if name == "pyproject.toml":
            try:
                before, after = tomllib.loads(old_file), tomllib.loads(new_file)
            except tomllib.TOMLDecodeError:
                raise InvalidCase("Candidate pyproject.toml must remain valid TOML") from None
            if _without_dependencies(before) != _without_dependencies(after):
                raise InvalidCase("Only project dependency fields may change")
        if name.startswith("requirements"):
            for line in new_file.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\[\],<>=!~; '\"()-]*", line):
                    raise InvalidCase("Requirement URLs, local paths and pip options are unsupported")
        diffs.extend(difflib.unified_diff(old_file.splitlines(True), new_file.splitlines(True), fromfile="a/" + name, tofile="b/" + name))
        pending[name] = new_file
    if "setup" in bundle:
        validate_steps(bundle["setup"])
        readme = pending.get("README.md", (root / "README.md").read_text(encoding="utf-8"))
        import shlex
        for step in bundle["setup"]:
            # The updated recipe must be visibly delivered in the patched README.
            if shlex.join(step["argv"]) not in readme and " ".join(step["argv"]) not in readme:
                from .diagnostics import scrub
                raise PatchMismatch("Patched setup command is absent from delivered README: " + scrub(shlex.join(step["argv"]), secrets))
    # Validate everything before mutating any target.
    for name, content in pending.items():
        (root / name).write_text(content, encoding="utf-8")
    return "".join(diffs)
