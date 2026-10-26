"""Persistent project-wide prepaid-only limits. Unknown funding always blocks."""

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import uuid

ROOT = Path(__file__).resolve().parents[2]/'.fresh-eyes'


class SpendError(ValueError):
    def __init__(self, code, message):
        self.code=code
        super().__init__(message)


def amount(value):
    try:
        result=Decimal(str(value))
    except (ValueError,InvalidOperation):
        raise SpendError('spending_policy_invalid','A finite non-negative funding amount is required.') from None
    if not result.is_finite() or result<0:
        raise SpendError('spending_policy_invalid','A finite non-negative funding amount is required.')
    return result


class SpendGuard:
    def __init__(self, root=None):
        self.root=Path(root) if root else ROOT
        self.policy_path=self.root/'spending-policy.json'
        self.db_path=self.root/'spending-ledger.sqlite3'

    def check(self, provider):
        try:
            if self.policy_path.is_symlink() or self.policy_path.stat().st_size>16384:
                raise ValueError()
            policy=json.loads(self.policy_path.read_text())
            entry=policy['providers'][provider]
            if policy.get('allow_overage') is not False or policy.get('allow_auto_top_up') is not False or entry.get('enabled') is not True:
                raise SpendError('api_spending_locked','Paid/credit API access is locked by the prepaid-only project policy.')
            bounded_trial = provider == 'nebius' and entry.get('funding_mode') == 'bounded_prepaid_trial'
            scoped = provider == 'nebius' and entry.get('funding_mode') == 'bounded_prepaid_authorization'
            followup = provider == 'nebius' and entry.get('funding_mode') == 'bounded_prepaid_followup'
            case_job = provider == 'nebius' and entry.get('funding_mode') == 'bounded_prepaid_case'
            if bounded_trial or scoped or followup or case_job:
                # Token Factory's current billing docs charge the card when the balance
                # becomes negative. This narrowly scoped trial must stay above a floor;
                # it does NOT assert that the provider disabled automatic charging.
                if (entry.get('automatic_charge_trigger') != 'non_positive_balance' or
                    entry.get('billing_reference') != 'https://docs.tokenfactory.nebius.com/other-capabilities/billing-new' or
                    entry.get('positive_balance_verified') is not True or
                    (bounded_trial and entry.get('authorization_budget_usd') != '0.03')):
                    raise SpendError('provider_funding_unverified','The bounded trial needs a fresh positive-balance check and the current billing conditions.')
            elif entry.get('funding_mode') not in {'free_only','prepaid'} or entry.get('overage_disabled_verified') is not True:
                raise SpendError('provider_funding_unverified','Provider overage/automatic charging has not been verified disabled; no request is allowed.')
            if entry.get('available_prepaid') is None:
                raise SpendError('provider_balance_unknown','Provider prepaid/free balance is unknown; no request is allowed.')
            balance=amount(entry['available_prepaid'])
            cap=amount(entry['max_total'])
            verified=datetime.fromisoformat(entry['verified_at'])
            expiry=datetime.fromisoformat(entry['expires_at'])
            now=datetime.now(timezone.utc)
            if not verified.tzinfo or not expiry.tzinfo or verified>now+timedelta(minutes=1) or not verified<=now<=expiry or expiry>verified+timedelta(hours=1):
                raise SpendError('provider_balance_stale','The funding verification expired; verify the provider balance/settings again.')
            if balance<=0 or cap<=0:
                raise SpendError('provider_budget_exhausted','No free/prepaid project allowance remains.')
            if bounded_trial and (cap>Decimal('0.03') or balance-cap<Decimal('0.50') or expiry>verified+timedelta(minutes=15)):
                raise SpendError('provider_budget_exhausted','The one-off prepaid trial must keep a $0.50 buffer, a $0.03 total cap and a 15-minute funding check.')
            if scoped:
                scopes=entry.get('scopes',{})
                expected={'notes':Decimal('.02'),'win4':Decimal('.24'),'drafts':Decimal('.24'),'formal':Decimal('1.20')}
                if (entry.get('authorization_id')!='win18-20-20261024' or
                    set(scopes)!=set(expected) or any(amount(scopes[k])!=v for k,v in expected.items()) or
                    amount(entry.get('authorization_budget_usd'))!=Decimal('1.70') or
                    cap>amount(entry.get('historical_allowance_usd'))+Decimal('1.70') or
                    entry.get('active_scope') not in scopes or balance-cap<Decimal('.50') or
                    expiry>verified+timedelta(minutes=15)):
                    raise SpendError('spending_policy_invalid','The authorized four scopes, total, positive balance margin or freshness changed.')
            if followup:
                acknowledged=entry.get('acknowledged_unknown_reservations',{})
                if (entry.get('authorization_id')!='win4-md2html-followup-20261024' or
                    amount(entry.get('authorization_budget_usd'))!=Decimal('.24') or
                    amount(entry.get('historical_allowance_usd'))!=Decimal('1.71407140') or
                    cap>Decimal('1.95407140') or
                    entry.get('active_scope')!='win4' or
                    amount(entry.get('max_per_attempt_usd'))!=Decimal('.08') or
                    entry.get('max_attempts')!=3 or entry.get('stop_on_success') is not True or
                    entry.get('unknown_charge_continuation_confirmed') is not True or
                    not isinstance(acknowledged,dict) or len(acknowledged)!=1 or
                    any(not isinstance(k,str) or len(k)!=32 or any(c not in '0123456789abcdef' for c in k)
                        or amount(v)!=Decimal('.029654') for k,v in acknowledged.items()) or
                    balance-cap<Decimal('.50') or expiry>verified+timedelta(minutes=15)):
                    raise SpendError('spending_policy_invalid','The separately authorized follow-up must retain the acknowledged unknown hold, its own $0.24 scope and fresh positive-balance margin.')
            if case_job:
                if (entry.get('authorization_id') != 'envdoctor-repair-20261026' or
                    entry.get('case_id') != 'envdoctor-check' or entry.get('active_scope') != 'repair' or
                    amount(entry.get('authorization_budget_usd')) != Decimal('1') or
                    entry.get('max_requests') != 4 or
                    cap > amount(entry.get('historical_allowance_usd')) + Decimal('1') or
                    balance-cap < Decimal('.50') or expiry > verified+timedelta(minutes=15)):
                    raise SpendError('spending_policy_invalid','The envdoctor grant needs its own persistent $1/four-request scope and a fresh prepaid balance with a $0.50 margin.')
            return entry
        except SpendError:
            raise
        except (OSError,ValueError,TypeError,KeyError):
            raise SpendError('api_spending_locked','No valid prepaid-only spending policy is configured; no request is allowed.') from None

    @contextmanager
    def connection(self):
        self.root.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(self.db_path,timeout=10)
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, provider TEXT, held TEXT, status TEXT, purpose TEXT)')
            if 'scope' not in {row[1] for row in db.execute('PRAGMA table_info(reservations)')}:
                db.execute('ALTER TABLE reservations ADD COLUMN scope TEXT')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def check_unresolved(self, rows, entry):
        # Default remains fail-closed. The owner explicitly authorized one new
        # scope while retaining this exact older reservation at its maximum.
        if any(status=='overrun' for _,_,status,_ in rows):
            raise SpendError('provider_usage_unknown','A previous provider charge is unresolved; further requests are blocked.')
        unknown={token:(amount(held),scope) for token,held,status,scope in rows if status=='unknown'}
        if not unknown:
            return
        if entry.get('funding_mode')!='bounded_prepaid_followup':
            raise SpendError('provider_usage_unknown','A previous provider charge is unresolved; further requests are blocked.')
        acknowledged=entry['acknowledged_unknown_reservations']
        expected={token:(amount(held),'win18-20-20261024:win4') for token,held in acknowledged.items()}
        if unknown!=expected:
            raise SpendError('provider_usage_unknown','A previous provider charge is unresolved; further requests are blocked.')

    def reserve(self, provider, requested, purpose):
        requested=amount(requested)
        if requested<=0:
            raise SpendError('spending_policy_invalid','A positive worst-case reservation is required.')
        entry=self.check(provider)
        with self.connection() as db:
            rows=db.execute('SELECT id,held,status,scope FROM reservations WHERE provider=?',(provider,)).fetchall()
            self.check_unresolved(rows,entry)
            used=sum((amount(held) for _,held,_,_ in rows),Decimal(0))
            if used+requested>min(amount(entry['max_total']),amount(entry['available_prepaid'])):
                raise SpendError('provider_budget_exhausted','The project-wide or verified prepaid allowance cannot cover this request.')
            scope=None
            if entry.get('funding_mode')=='bounded_prepaid_authorization':
                name=entry['active_scope'];scope=entry['authorization_id']+':'+name
                allowed={'notes':{'model:maintainer_note'},'drafts':{'model:acceptance_draft'},
                    'win4':{'model:classify','model:diagnose','model:classify_r2','model:diagnose_r2'},
                    'formal':{'model:classify','model:diagnose','model:classify_r2','model:diagnose_r2'}}
                scoped_used=sum((amount(held) for _,held,_,saved_scope in rows if saved_scope==scope),Decimal(0))
                if purpose not in allowed[name] or scoped_used+requested>amount(entry['scopes'][name]):
                    raise SpendError('scope_budget_exhausted','This request does not fit its authorized purpose and persistent scope ceiling.')
            elif entry.get('funding_mode')=='bounded_prepaid_followup':
                scope=entry['authorization_id']+':win4'
                allowed={'model:classify','model:diagnose','model:classify_r2','model:diagnose_r2'}
                scoped_used=sum((amount(held) for _,held,_,saved_scope in rows if saved_scope==scope),Decimal(0))
                if (purpose not in allowed or requested>Decimal('.08') or
                    scoped_used+requested>Decimal('.24')):
                    raise SpendError('scope_budget_exhausted','The follow-up cannot borrow another scope or exceed its persistent $0.24 ceiling.')
            elif entry.get('funding_mode')=='bounded_prepaid_case':
                scope=entry['authorization_id']+':repair'
                scoped=[amount(held) for _,held,_,saved_scope in rows if saved_scope==scope]
                if (purpose not in {'model:classify','model:diagnose','model:classify_r2','model:diagnose_r2'} or
                    len(scoped)>=4 or sum(scoped,Decimal(0))+requested>Decimal('1')):
                    raise SpendError('scope_budget_exhausted','The one envdoctor trial cannot exceed $1/four calls or fund another purpose.')
            token=uuid.uuid4().hex
            db.execute('INSERT INTO reservations (id,provider,held,status,purpose,scope) VALUES (?,?,?,?,?,?)',
                (token,provider,str(requested),'reserved',str(purpose)[:100],scope))
        return token

    def verify_reservation(self, token, provider):
        entry=self.check(provider)
        with self.connection() as db:
            row=db.execute('SELECT provider,status,scope FROM reservations WHERE id=?',(token,)).fetchone()
            expected_scope=(entry['authorization_id']+':'+entry['active_scope']) if entry.get('funding_mode') in {'bounded_prepaid_authorization','bounded_prepaid_followup','bounded_prepaid_case'} else None
            if row!=(provider,'reserved',expected_scope):
                raise SpendError('spending_reservation_missing','The provider request has no valid unconsumed global reservation.')
            self.check_unresolved(db.execute('SELECT id,held,status,scope FROM reservations WHERE provider=?',(provider,)).fetchall(),entry)
            # Consume before HTTP dispatch; the same reservation cannot be sent twice.
            db.execute('UPDATE reservations SET status=? WHERE id=?',('dispatching',token))

    def finish(self, token, actual=None, *, dispatched=True):
        with self.connection() as db:
            row=db.execute('SELECT held,status FROM reservations WHERE id=?',(token,)).fetchone()
            if not row or row[1] not in {'reserved','dispatching'}:
                raise SpendError('spending_reservation_finalized','Global reservation is missing or already finalized.')
            held=amount(row[0])
            if not dispatched:
                value,status=Decimal(0),'not_dispatched'
            elif actual is None:
                value,status=held,'unknown'
            else:
                value=amount(actual)
                status='overrun' if value>held else 'spent'
            db.execute('UPDATE reservations SET held=?,status=? WHERE id=?',(str(value),status,token))

    def summary(self):
        # A status read does not create a fresh policy or budget.
        if not self.db_path.is_file():
            return {'providers':{},'new_allowance_created':False}
        with self.connection() as db:
            rows=db.execute('SELECT provider,held,status,scope FROM reservations').fetchall()
        result={}
        scopes={}
        for provider,held,status,scope in rows:
            item=result.setdefault(provider,{'held':'0','requests':0,'unresolved':0})
            item['held']=str(amount(item['held'])+amount(held))
            item['requests']+=1
            item['unresolved']+=status in {'unknown','overrun','dispatching'}
            if scope:
                subtotal=scopes.setdefault(scope,{'held':'0','requests':0,'unresolved':0})
                subtotal['held']=str(amount(subtotal['held'])+amount(held));subtotal['requests']+=1
                subtotal['unresolved']+=status in {'unknown','overrun','dispatching'}
        return {'providers':result,'scopes':scopes,'new_allowance_created':False}
