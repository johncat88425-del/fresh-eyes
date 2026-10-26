#!/usr/bin/env python3
"""Validate an unchanged saved model candidate; never load keys or call a provider.

This creates a separate development verification, not a retry/rewrite of the
original model trial and never a formal evaluation result.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'src'))
from fresh_eyes.approval_io import contracts
from fresh_eyes.contracts import Contract, digest, InvalidCase
from fresh_eyes.engine import run_case, save_report
from fresh_eyes.runner import DockerRunner


def checked_candidate(project, trial):
    def read(name, maximum=4*1024*1024):
        path=trial/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size>maximum:
            raise InvalidCase('Saved candidate input must be a bounded regular file')
        raw=path.read_bytes();return json.loads(raw),hashlib.sha256(raw).hexdigest()
    report,report_sha=read('report.json');binding,_=read('trial-binding.json')
    ledger,ledger_sha=read('model-ledger.json');plan,_=read('trial-plan.json')
    calls=ledger.get('calls',[])
    if (binding.get('scope')!='development' or binding.get('evaluation_eligible') is not False or
        binding.get('report_sha256')!=report_sha or binding.get('ledger_sha256')!=ledger_sha or
        binding.get('run_id')!=report.get('run_id') or report.get('repair_author')!='model' or
        report.get('candidate_patch',{}).get('status')!='accepted_for_validation' or
        plan.get('payload_sha256')!=binding.get('plan_sha256') or
        digest({k:v for k,v in plan.items() if k!='payload_sha256'})!=plan.get('payload_sha256') or
        report.get('model_ledger')!=ledger or len(calls)!=2 or ledger.get('model_calls')!=2 or
        any(c.get('data_origin')!='nebius_api' or not c.get('dispatch_started_at') or c.get('status')!='completed' for c in calls)):
        raise InvalidCase('Saved model trial, real calls or evidence binding changed')
    data=contracts(project).get(report.get('case_id'))
    if data is None:raise InvalidCase('Only registered development cases can be revalidated')
    payload=data.data;payload['review']=plan['review'];contract=Contract.from_dict(payload);contract.check_review()
    if (contract.sha256!=report.get('contract_sha256') or contract.acceptance_sha256!=report.get('acceptance_sha256') or
        contract.sha256!=plan.get('execution_contract_sha256') or report.get('repo')!=contract.data['repo']):
        raise InvalidCase('Saved candidate acceptance, repo or human review changed')
    candidate=copy.deepcopy(report['candidate_patch']['patch'])
    provenance={'kind':'unchanged_saved_model_candidate','source_run_id':report['run_id'],
        'source_report_sha256':report_sha,'source_ledger_sha256':ledger_sha,
        'source_plan_sha256':plan['payload_sha256'],'source_model_calls':2,
        'source_call_ids':[c['call_id'] for c in calls],'source_calculated_usd':ledger['calculated_usd'],
        'candidate_sha256':digest(candidate),'source_patch_sha256':report['patch']['sha256'],
        'new_model_calls':0,'evaluation_eligible':False,
        'notice':'Original model trial remains unchanged. This separate verification reuses its exact candidate, without model dispatch or a new diagnosis.'}
    return contract,candidate,provenance,report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trial',type=Path,required=True);parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--docker',required=True)
    parser.add_argument('--execute',action='store_true');args=parser.parse_args()
    contract,candidate,provenance,original=checked_candidate(PROJECT,args.trial)
    if not args.execute:
        print(json.dumps({'action':'preview',**provenance},indent=2));return
    args.output.mkdir(parents=True,exist_ok=False)
    report=run_case(contract,args.source,None,args.output,patch=candidate,
        runner=DockerRunner(image=original['environment']['image_id'],platform=contract.data['environment']['platform'],
            executable=args.docker,dependency_mode='pypi_live'),temp_root=None,time_limit_s=600)
    if report.get('patch') and report['patch']['sha256']!=provenance['source_patch_sha256']:
        raise InvalidCase('Revalidation produced a different patch; do not attribute it to the original model')
    # run_case's direct-patch interface assumes a manual candidate. Only the
    # checked original model provenance above permits this precise attribution.
    if report.get('patch'):report['repair_author']='model'
    report.update(candidate_provenance=provenance,evaluation_eligible=False,
        record_scope='saved_model_candidate_validation',new_model_calls=0)
    save_report(report,args.output)
    record={'schema_version':1,'data_origin':'execution','record_source':'owner_saved_model_candidate_validation',
        'evaluation_eligible':False,'run_id':report['run_id'],'case_id':contract.data['case_id'],'case_available':True,
        'run_state':'completed','result_status':report['status'],'phase':'finished','reason':report.get('reason'),
        'recording_scope':'saved_model_candidate_validation','verification_level':'functional',
        'source_model_diagnosis':{k:v for k,v in original['diagnosis'].items() if k!='ledger'},
        'repair_mode':'model','repair_author':report.get('repair_author'),'patch_available':bool(report.get('patch')),
        'repo_url':report['repo']['url'],'commit_sha':report['repo']['commit'],'dependency_mode':'pypi_live',
        'acceptance_sha256':contract.acceptance_sha256,'review':report['review'],
        'created_at':report['started_at'],'started_at':report['started_at'],'finished_at':report['finished_at'],
        'evidence':report,'costs':{'currency':'USD','model_calls':[],'model_usd':0,'billed_usd':0,
            'runner_usd':None,'paid_calls_enabled':False,'billing_status':'local_compute_not_metered'}}
    (args.output/'run-record.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'run_id':report['run_id'],'status':report['status'],'repair_author':report.get('repair_author'),
        'candidate_unchanged':True,'model_calls':0,'source_model_run':provenance['source_run_id'],
        'attempts':[{'phase':a['phase'],'status':a['status']} for a in report['attempts']]},indent=2))


if __name__=='__main__':main()
