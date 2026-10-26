"""Check a frozen holdout inventory without executing it or approving it."""

import hashlib
import json
from pathlib import Path

from .contracts import Contract, InvalidCase


def verify_holdout(manifest_path):
    manifest_path = Path(manifest_path)
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 65536:
        raise InvalidCase("Unsupported holdout manifest")
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("kind") != "holdout":
        raise InvalidCase("Expected a holdout freeze manifest")
    entries = manifest.get("cases")
    if not isinstance(entries, list) or not entries or len(entries) > 100 or any(not isinstance(e, dict) for e in entries):
        raise InvalidCase("Invalid frozen holdout inventory")
    root = manifest_path.parent
    names, case_ids, results = set(), set(), []
    for entry in entries:
        name = entry.get("file", "")
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".json") or name == manifest_path.name or name in names:
            raise InvalidCase("Frozen cases require unique JSON basenames")
        names.add(name)
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
            raise InvalidCase("A frozen case is missing or is not a regular bounded file")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry.get("file_sha256"):
            raise InvalidCase("Frozen case bytes changed: " + name)
        contract = Contract.from_dict(json.loads(raw))
        case_id = contract.data["case_id"]
        if case_id != entry.get("case_id") or case_id in case_ids or contract.sha256 != entry.get("contract_sha256") or contract.acceptance_sha256 != entry.get("acceptance_sha256"):
            raise InvalidCase("Frozen case identity or acceptance changed: " + name)
        case_ids.add(case_id)
        results.append({"case_id": case_id, "acceptance_sha256": contract.acceptance_sha256,
                        "review_status": contract.data.get("review", {}).get("status")})
    actual = {p.name for p in root.glob("*.json") if p.name != manifest_path.name}
    if actual != names:
        raise InvalidCase("The holdout directory differs from the frozen inventory")
    return {"kind": "holdout", "integrity_verified": True, "executed": False,
            "human_approval_granted": False, "case_count": len(results), "cases": results,
            "notice": "Integrity only. Human review and a deliberate evaluation runner are still required."}
