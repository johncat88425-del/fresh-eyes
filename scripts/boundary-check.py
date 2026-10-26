#!/usr/bin/env python3
"""Plan a boundary baseline for human review; execution is a separate explicit action."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fresh_eyes.boundary import plan, review_template, run

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--manifest", type=Path, default=ROOT / "cases/boundary/expected.json")
parser.add_argument("--plan", type=Path, required=True)
parser.add_argument("--runner-id")
parser.add_argument("--review-template", type=Path)
parser.add_argument("--execute", action="store_true")
parser.add_argument("--reviews", type=Path)
parser.add_argument("--source-root", type=Path)
parser.add_argument("--output", type=Path)
parser.add_argument("--docker", default="docker")
args = parser.parse_args()
if args.execute:
    if not all((args.reviews, args.source_root, args.output)):
        parser.error("Execution needs reviews, pinned source-root and a new output directory")
    result = run(ROOT, args.manifest, args.plan, args.reviews, args.source_root, args.output, docker=args.docker)
else:
    if not args.runner_id or not args.review_template:
        parser.error("Planning needs runner-id and a new review-template")
    if args.plan.exists() or args.review_template.exists():
        parser.error("Preserve previous plans/reviews; choose a new version")
    result = plan(ROOT, args.manifest, args.runner_id)
    for path, value in ((args.plan, result), (args.review_template, review_template(result))):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x") as stream:
            stream.write(json.dumps(value, indent=2) + "\n")
print(json.dumps(result, indent=2))
