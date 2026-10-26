#!/usr/bin/env python3
"""Preview human page decisions; --apply writes new freeze-bound review files, never changes contracts."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from fresh_eyes.boundary import plan as boundary_plan
from fresh_eyes.evaluation import load_freeze
from fresh_eyes.review_decisions import prepare, read_json, merge_previous

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--decisions',type=Path,required=True)
parser.add_argument('--review-data',type=Path,required=True)
parser.add_argument('--freeze',type=Path,required=True)
parser.add_argument('--boundary-plan',type=Path,required=True)
parser.add_argument('--output',type=Path)
parser.add_argument('--apply',action='store_true')
parser.add_argument('--previous',type=Path,help='Carry existing decisions already bound to this same freeze/plan; old files remain unchanged')
args=parser.parse_args()
try:
    frozen=load_freeze(ROOT,ROOT/'cases/holdout/freeze.json',args.freeze,execution=True)
    saved=read_json(args.boundary_plan)
    if saved!=boundary_plan(ROOT,ROOT/'cases/boundary/expected.json',saved['runner_id']):
        raise ValueError('Boundary plan differs from current code or contracts')
    files=prepare(ROOT,args.decisions,args.review_data,frozen,saved)
    if args.previous:
        files=merge_previous(files,{name:read_json(args.previous/name) for name in ('holdout-reviews.json','boundary-reviews.json')})
    if args.apply:
        if not args.output:parser.error('--apply requires a new --output directory')
        args.output.mkdir(parents=True,exist_ok=False)
        for name,value in files.items():
            (args.output/name).write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps({'action':'imported' if args.apply else 'preview',**files['receipt.json']},indent=2,ensure_ascii=False))
except (ValueError,OSError,KeyError,TypeError) as error:
    print('Review import stopped: '+str(error),file=sys.stderr)
    raise SystemExit(2)
