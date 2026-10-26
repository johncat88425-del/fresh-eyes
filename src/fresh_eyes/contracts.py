"""Contracts stay outside the execution container; expected results never enter it."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class InvalidCase(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def relative_path(value: str) -> str:
    p = PurePosixPath(value)
    if not value or str(p) == "." or p.is_absolute() or ".." in p.parts or str(p) != value or "\\" in value or "\x00" in value:
        raise InvalidCase(f"Unsafe relative path: {value!r}")
    return value


def validate_steps(steps: list[dict]) -> None:
    if not steps or len(steps) > 8:
        raise InvalidCase("Expected 1–8 documented setup steps")
    for step in steps:
        argv = step.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or "\x00" in x for x in argv):
            raise InvalidCase("Each setup step needs a non-empty argv array")
        if not step.get("source"):
            raise InvalidCase("Every setup step needs a document source")
        if argv[0] not in {"pip", "pip3", "python", "python3"}:
            raise InvalidCase("Prototype setup only supports documented pip/Python installation commands")
        install_args = argv[1:] if argv[0].startswith("pip") else argv[1:]
        if argv[0].startswith("python"):
            if install_args[:2] != ["-m", "pip"]:
                raise InvalidCase("Setup Python commands must invoke pip")
            install_args = install_args[2:]
        if not install_args or install_args[0] != "install":
            raise InvalidCase("Only pip install is supported in setup")
        if any(x.startswith(("http:", "https:", "git+", "--index", "--extra-index", "--trusted-host")) for x in install_args):
            raise InvalidCase("Install steps cannot override the offline package source")


@dataclass(frozen=True)
class Contract:
    _serialized: str

    @classmethod
    def load(cls, path: Path) -> "Contract":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, data: dict) -> "Contract":
        if data.get("schema_version") != 1:
            raise InvalidCase("Unsupported schema_version")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,70}", data.get("case_id", "")):
            raise InvalidCase("Invalid case_id")
        if not re.fullmatch(r"[a-f0-9]{40}", data.get("repo", {}).get("commit", "")):
            raise InvalidCase("A full 40-character commit SHA is required")
        if data.get("environment", {}).get("platform") not in {"linux/arm64", "linux/amd64"}:
            raise InvalidCase("Unsupported platform")
        if not data.get("goal") or not data.get("readme_source"):
            raise InvalidCase("Goal and README source are required")
        validate_steps(data.get("setup", []))
        argv = data.get("workflow", {}).get("argv", [])
        if not argv or any(not isinstance(x, str) or "\x00" in x for x in argv):
            raise InvalidCase("A structured CLI workflow is required")
        if "/" in argv[0] or argv[0] in {"sh", "bash", "zsh", "env", "sudo"}:
            raise InvalidCase("A workflow must call a CLI directly")
        if argv[0].startswith("python") and any(x in {"-c", "-"} for x in argv[1:]):
            raise InvalidCase("Inline Python is not a supported CLI workflow")
        output = data.get("workflow", {}).get("output", "stdout")
        if output != "stdout":
            relative_path(output)
        fixtures = data.get("fixtures", [])
        if len(fixtures) < 2 or len(fixtures) > 10:
            raise InvalidCase("Provide 2–10 functional fixtures")
        names = set()
        for f in fixtures:
            relative_path(f["name"])
            if f["name"] in names:
                raise InvalidCase("Fixture names must be unique")
            names.add(f["name"])
            if not isinstance(f.get("input"), str) or len(f["input"].encode()) > 65536:
                raise InvalidCase("Fixture input must be text smaller than 64 KiB")
            if f.get("validator") not in {"text", "text_lines", "json", "html_elements"}:
                raise InvalidCase("Unsupported deterministic validator")
            if "expected" not in f:
                raise InvalidCase("Each fixture needs an expected result")
            if f["validator"] in {"text", "text_lines"} and not isinstance(f["expected"], str):
                raise InvalidCase("Text expectations must be strings")
            if f["validator"] == "html_elements" and (
                not isinstance(f["expected"], list) or not f["expected"] or
                any(not isinstance(e, list) or len(e) != 2 or not all(isinstance(x, str) for x in e) for e in f["expected"])
            ):
                raise InvalidCase("HTML expectations require non-empty [tag, text] pairs")
        return cls(canonical(data))

    @property
    def data(self) -> dict:
        # Return a copy so diagnostic/model code cannot mutate the accepted contract.
        return json.loads(self._serialized)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self._serialized.encode()).hexdigest()

    @property
    def acceptance_sha256(self) -> str:
        d = self.data
        return digest({k: d[k] for k in ("goal", "fixtures", "repo", "environment")})

    def execution_payload(self, setup: list[dict] | None = None) -> dict:
        d = self.data
        return {
            "setup": setup if setup is not None else d["setup"],
            "workflow": d["workflow"],
            "fixtures": [{"name": f["name"], "input": f["input"]} for f in d["fixtures"]],
        }

    def check_review(self, development: bool = False) -> None:
        status = self.data.get("review", {}).get("status")
        if status == "approved":
            return
        if status == "development_reviewed" and development:
            return
        raise InvalidCase("Case needs review; development-reviewed cases require --development")
