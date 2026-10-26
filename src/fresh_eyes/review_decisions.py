"""Bind supplied human decisions to unchanged review-page subjects and current plans.

An unsigned record preserves attribution; it does not authenticate the reviewer.
Rejections stay rejections, and a missing/rejected holdout blocks the whole batch.
"""
import hashlib
import json
import copy
from pathlib import Path

from .approval_io import timestamp
from .contracts import Contract, InvalidCase, digest

SUBJECT = ('goal', 'repo', 'readme_source', 'setup', 'workflow', 'fixtures')


def read_json(path, limit=262144):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise InvalidCase('Review input must be a bounded regular JSON file')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise InvalidCase('Duplicate review JSON field')
            value[key] = item
        return value
    return json.loads(path.read_text(), object_pairs_hook=unique)


def prepare(project, decisions_path, review_data_path, frozen, boundary_plan):
    project = Path(project)
    supplied, page = read_json(decisions_path), read_json(review_data_path)
    reviewer = supplied.get('reviewed_by')
    if not isinstance(reviewer, str) or reviewer != reviewer.strip() or not 1 <= len(reviewer) <= 100:
        raise InvalidCase('Original reviewer attribution is required')
    if set(supplied) != {'reviewed_by', 'account_id', 'approvals', 'not_accepted'}:
        raise InvalidCase('Unsupported human decision envelope')
    cases = {}
    for row in page.get('cases', []):
        case_id, name = row.get('case_id'), row.get('file')
        if case_id in cases or not isinstance(name, str):
            raise InvalidCase('Duplicate or missing review subject')
        path = Path(name)
        group = row.get('group')
        folder = {'holdout':'cases/holdout', 'boundary':'cases/boundary', 'development':'cases/win5'}.get(group)
        if not folder or path.parent.as_posix() != folder or path.suffix != '.json':
            raise InvalidCase('Review subject must name a supported case file')
        contract = Contract.load(project/path)
        if contract.data['case_id'] != case_id or contract.acceptance_sha256 != row.get('acceptance_sha256'):
            raise InvalidCase('Reviewed acceptance changed: '+str(case_id))
        if any(row.get(key) != contract.data[key] for key in SUBJECT):
            raise InvalidCase('Reviewed instructions or fixtures changed: '+str(case_id))
        cases[case_id] = (row, contract)
    decisions = {}
    for status, field in [('approved','approvals'), ('rejected','not_accepted')]:
        rows = supplied.get(field)
        if not isinstance(rows, list) or len(rows) > 100:
            raise InvalidCase('Human decision rows must be bounded lists')
        for row in rows:
            case_id = row.get('case_id')
            if case_id not in cases or case_id in decisions:
                raise InvalidCase('Unknown or duplicate human decision')
            if row.get('acceptance_sha256') != cases[case_id][1].acceptance_sha256:
                raise InvalidCase('Decision acceptance changed: '+case_id)
            date = row.get('approved_at' if status == 'approved' else 'decided_at')
            timestamp(date)
            if status == 'approved' and (row.get('status') != 'approved' or row.get('reviewed_by') != reviewer):
                raise InvalidCase('Decision reviewer or approval status changed')
            if status == 'rejected' and (not isinstance(row.get('reason'),str) or not row['reason'].strip()):
                raise InvalidCase('A rejection needs the original reason')
            if case_id == 'j2cli-render' and status == 'approved' and row.get('choice') != '留在邊界組（照 v1）':
                raise InvalidCase('Boundary grouping was not accepted; revise the plan first')
            decisions[case_id] = {**row, 'status':status, 'reviewed_by':reviewer,
                'approved_at':date if status == 'approved' else None, 'decided_at':date}
    def bind(entries, scope, binding, key):
        rows = []
        for case in entries:
            case_id = case['case_id']
            if case_id not in cases or case['acceptance_sha256'] != cases[case_id][1].acceptance_sha256:
                raise InvalidCase('Plan differs from the review subject')
            saved = decisions.get(case_id)
            rows.append({'case_id':case_id, 'acceptance_sha256':case['acceptance_sha256'],
                'status':saved['status'] if saved else 'draft',
                'reviewed_by':reviewer if saved else None, 'approved_at':saved.get('approved_at') if saved else None,
                **({'reason':saved['reason'], 'decided_at':saved['decided_at']} if saved and saved['status']=='rejected' else {})})
        return {'schema_version':1, 'scope':scope, key:binding, 'approvals':rows}
    holdout = bind(frozen['cases'], 'holdout_team_acceptance_review', frozen['payload_sha256'], 'freeze_sha256')
    boundary = bind(boundary_plan['cases'], 'boundary_baseline', boundary_plan['payload_sha256'], 'plan_sha256')
    boundary['format'] = 'fresh-eyes-boundary-human-reviews-v1'
    receipt = {'format':'fresh-eyes-human-decision-intake-v1', 'source':'human_supplied_record',
        'source_sha256':hashlib.sha256(Path(decisions_path).read_bytes()).hexdigest(),
        'review_page_sha256':hashlib.sha256(Path(review_data_path).read_bytes()).hexdigest(),
        'reviewer':reviewer, 'identity_verified':False, 'account_id_exported':False,
        'accepted':[key for key,row in decisions.items() if row['status']=='approved'],
        'rejected':{key:row['reason'] for key,row in decisions.items() if row['status']=='rejected'},
        'holdout_execution_ready':all(row['status']=='approved' for row in holdout['approvals']),
        'boundary_execution_ready':all(row['status']=='approved' for row in boundary['approvals']),
        'repository_executed':False, 'model_calls':0,
        'notice':'Attribution preserved from a human-supplied unsigned record. Rejected or changed contracts require new human review.'}
    return {'holdout-reviews.json':holdout, 'boundary-reviews.json':boundary, 'receipt.json':receipt}


def merge_previous(prepared, previous):
    """Carry unchanged attributed decisions into a new output, never modify a previous file."""
    result=copy.deepcopy(prepared)
    carried=[]
    for name,key in [('holdout-reviews.json','freeze_sha256'),('boundary-reviews.json','plan_sha256')]:
        current,old=result[name],previous[name]
        if current.get(key)!=old.get(key):
            raise InvalidCase('Previous reviews belong to another freeze/plan; explicitly rebind unchanged subjects first')
        old_rows=old.get('approvals',[])
        if len(old_rows)!=len(current['approvals']) or len({row['case_id'] for row in old_rows})!=len(old_rows):
            raise InvalidCase('Previous review inventory changed')
        indexed={row['case_id']:row for row in old_rows}
        for i,row in enumerate(current['approvals']):
            prior=indexed.get(row['case_id'])
            if not prior or prior.get('acceptance_sha256')!=row['acceptance_sha256']:
                raise InvalidCase('Previous acceptance changed')
            if row['status']=='draft' and prior.get('status') in {'approved','rejected'}:
                if not isinstance(prior.get('reviewed_by'),str) or not prior['reviewed_by'].strip():
                    raise InvalidCase('Previous reviewer attribution is missing')
                timestamp(prior.get('approved_at') if prior['status']=='approved' else prior.get('decided_at'))
                if prior['status']=='rejected' and not prior.get('reason'):
                    raise InvalidCase('Previous rejection reason is missing')
                current['approvals'][i]=copy.deepcopy(prior)
                if prior['status']=='approved':carried.append(prior['case_id'])
    receipt=result['receipt.json']
    receipt.update(carried_approvals=carried,previous_reviews_sha256=digest(previous),
        holdout_execution_ready=all(row['status']=='approved' for row in result['holdout-reviews.json']['approvals']),
        boundary_execution_ready=all(row['status']=='approved' for row in result['boundary-reviews.json']['approvals']))
    return result
