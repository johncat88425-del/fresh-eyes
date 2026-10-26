#!/usr/bin/env python3
"""Preview or execute one prepaid-only Tavily basic lookup, with no model call."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from fresh_eyes.contracts import Contract
from fresh_eyes.web_research import load_transport, WebResearcher

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--case',type=Path,default=ROOT/'cases/md2html.json')
parser.add_argument('--execute',action='store_true')
parser.add_argument('--env-file',type=Path,default=ROOT/'.env')
parser.add_argument('--max-credits',type=int,default=0,choices=[0,1])
parser.add_argument('--output',type=Path)
args=parser.parse_args()
try:
    contract=Contract.load(args.case)
    if not args.execute:
        print(json.dumps({'executed':False,'key_read':False,'credit_limit':args.max_credits,
            'search_depth':'basic','model_calls':0,'repo_url':contract.data['repo']['url']},indent=2))
        raise SystemExit(0)
    if args.max_credits!=1 or not args.output:
        parser.error('Real lookup requires --max-credits 1 and a new --output file.')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    # The existence claim precedes dispatch, so a restarted command cannot repeat it.
    with args.output.open('x') as stream:
        stream.write(json.dumps({'status':'claimed','model_calls':0,'research_calls_reserved':1})+'\n')
    record={'status':'not_dispatched','model_calls':0,'research_calls_reserved':1}
    try:
        transport=load_transport(args.env_file,enabled=True)
        record={'status':'usage_unknown','model_calls':0,'research_calls_reserved':1,
            'notice':'A request may have been dispatched. Check the persistent spending ledger before any further call.'}
        found=WebResearcher(transport,max_searches=1,max_results=3).lookup(contract)
        record={'format':'fresh-eyes-tavily-development-check-v1','data_origin':'tavily_api',
            'status':'completed','model_calls':0,'research_calls':1,'credit_cap':1,
            'research':found,'account_mode':'free_only','usd_billed':None,
            'notice':'Real Tavily component lookup only; no model diagnosis or repaired repository execution occurred.'}
    finally:
        args.output.write_text(json.dumps(record,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps({'status':record['status'],'research_calls':1,'reported_credits':found['credits'],
                     'sources':len(found['results']),'model_calls':0,'record':str(args.output)},indent=2))
except (ValueError,OSError,KeyError) as exc:
    print('Tavily check stopped: '+str(exc),file=sys.stderr)
    raise SystemExit(2)
