"""Global funding limits use attributed synthetic policy data only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from fresh_eyes.spending import SpendGuard, SpendError


def verified_guard(root, *, provider='nebius', cap='1', balance='1'):
    root=Path(root); root.mkdir(parents=True,exist_ok=True)
    now=datetime.now(timezone.utc)
    policy={'allow_overage':False,'allow_auto_top_up':False,'providers':{provider:{
        'enabled':True,'funding_mode':'prepaid','max_total':cap,'available_prepaid':balance,
        'overage_disabled_verified':True,'verified_at':now.isoformat(),
        'expires_at':(now+timedelta(minutes=15)).isoformat(),'verification_source':'synthetic engineering fixture'}}}
    (root/'spending-policy.json').write_text(json.dumps(policy))
    return SpendGuard(root)


class SpendingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()

    def test_missing_or_disabled_policy_cannot_send_a_request(self):
        guard=SpendGuard(self.root)
        with self.assertRaises(SpendError): guard.reserve('nebius','.01','test')
        self.assertFalse(guard.db_path.exists())

    def test_new_run_or_process_cannot_reset_project_total(self):
        guard=verified_guard(self.root,cap='.02')
        token=guard.reserve('nebius','.02','first-run'); guard.verify_reservation(token,'nebius'); guard.finish(token,'.02')
        with self.assertRaises(SpendError): SpendGuard(self.root).reserve('nebius','.001','new-run')

    def test_atomic_reservations_never_exceed_the_balance(self):
        guard=verified_guard(self.root,cap='10',balance='1')
        def reserve(_):
            try: guard.reserve('nebius','1','concurrent'); return True
            except SpendError: return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(reserve,range(4))),1)

    def test_unknown_usage_locks_further_calls_without_automatic_retry(self):
        guard=verified_guard(self.root)
        token=guard.reserve('nebius','.01','timeout'); guard.verify_reservation(token,'nebius'); guard.finish(token,None)
        with self.assertRaises(SpendError) as error: guard.reserve('nebius','.01','another')
        self.assertEqual(error.exception.code,'provider_usage_unknown')

    def test_one_reservation_cannot_be_dispatched_twice(self):
        guard=verified_guard(self.root)
        token=guard.reserve('nebius','.01','once'); guard.verify_reservation(token,'nebius')
        with self.assertRaises(SpendError): guard.verify_reservation(token,'nebius')
        guard.finish(token,'.001')
        with self.assertRaises(SpendError): guard.finish(token,'.001')

    def test_exhausted_unknown_stale_or_overage_funding_fails_closed(self):
        for change in ({'available_prepaid':None},{'available_prepaid':'0'},{'max_total':'0'},
                       {'overage_disabled_verified':False},{'funding_mode':'pay_as_you_go'},
                       {'expires_at':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()}):
            guard=verified_guard(self.root)
            policy=json.loads(guard.policy_path.read_text()); policy['providers']['nebius'].update(change)
            guard.policy_path.write_text(json.dumps(policy))
            with self.subTest(change=change),self.assertRaises(SpendError): guard.check('nebius')

    def test_tavily_credit_and_nebius_usd_allowances_are_independent(self):
        guard=verified_guard(self.root,provider='tavily',cap='2',balance='1000')
        for i in range(2):
            token=guard.reserve('tavily','1','basic-search');guard.verify_reservation(token,'tavily');guard.finish(token,'1')
        with self.assertRaises(SpendError):guard.reserve('tavily','1','third-search')
        with self.assertRaises(SpendError):guard.reserve('nebius','.001','not-authorized')

    def test_not_dispatched_reservation_can_release_only_its_own_hold(self):
        guard=verified_guard(self.root,cap='1',balance='1')
        token=guard.reserve('nebius','1','preflight');guard.finish(token,dispatched=False)
        self.assertTrue(guard.reserve('nebius','1','new-explicit-attempt'))

    def test_reported_charge_above_reservation_blocks_every_later_request(self):
        guard=verified_guard(self.root,cap='10',balance='10')
        token=guard.reserve('nebius','1','bad-provider');guard.verify_reservation(token,'nebius');guard.finish(token,'2')
        with self.assertRaises(SpendError):guard.reserve('nebius','.001','blocked')
        self.assertEqual(guard.summary()['providers']['nebius']['unresolved'],1)

    def bounded_trial(self, **changes):
        guard=verified_guard(self.root,cap='.03',balance='.90')
        policy=json.loads(guard.policy_path.read_text())
        policy['providers']['nebius'].update(funding_mode='bounded_prepaid_trial',overage_disabled_verified=False,
            positive_balance_verified=True,automatic_charge_trigger='non_positive_balance',authorization_budget_usd='0.03',
            billing_reference='https://docs.tokenfactory.nebius.com/other-capabilities/billing-new')
        policy['providers']['nebius'].update(changes)
        guard.policy_path.write_text(json.dumps(policy))
        return guard

    def test_bounded_prepaid_trial_reserves_inside_credit_without_claiming_provider_off(self):
        guard=self.bounded_trial()
        self.assertFalse(guard.check('nebius')['overage_disabled_verified'])
        token=guard.reserve('nebius','.02','authorized-single-trial')
        guard.verify_reservation(token,'nebius');guard.finish(token,'.02')
        with self.assertRaises(SpendError):SpendGuard(self.root).reserve('nebius','.011','over-total')

    def test_bounded_trial_rejects_larger_caps_insufficient_margin_and_stale_balance(self):
        for changes in ({'max_total':'.031'},{'available_prepaid':'.52'},
                        {'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=16)).isoformat()},
                        {'automatic_charge_trigger':'unknown'},{'positive_balance_verified':False}):
            with self.subTest(changes=changes),self.assertRaises(SpendError):self.bounded_trial(**changes).check('nebius')

    def test_bounded_trial_atomic_reservations_cannot_spend_the_margin(self):
        guard=self.bounded_trial()
        def reserve(_):
            try:guard.reserve('nebius','.02','concurrent');return True
            except SpendError:return False
        with ThreadPoolExecutor(max_workers=4) as pool:self.assertEqual(sum(pool.map(reserve,range(4))),1)

    def test_bounded_trial_does_not_unlock_other_providers(self):
        guard=self.bounded_trial()
        with self.assertRaises(SpendError):guard.reserve('tavily','1','not-authorized')

    def scoped_guard(self, scope='notes'):
        guard=self.bounded_trial()
        policy=json.loads(guard.policy_path.read_text());entry=policy['providers']['nebius']
        entry.update(funding_mode='bounded_prepaid_authorization',authorization_id='win18-20-20261024',
            authorization_budget_usd='1.70',historical_allowance_usd='0',max_total='1.70',available_prepaid='5',
            active_scope=scope,scopes={'notes':'.02','win4':'.24','drafts':'.24','formal':'1.20'})
        guard.policy_path.write_text(json.dumps(policy));return guard

    def test_each_authorized_scope_has_a_persistent_ceiling(self):
        guard=self.scoped_guard();token=guard.reserve('nebius','.019','model:maintainer_note')
        guard.verify_reservation(token,'nebius');guard.finish(token,'.019')
        with self.assertRaises(SpendError):SpendGuard(self.root).reserve('nebius','.002','model:maintainer_note')
        policy=json.loads(guard.policy_path.read_text());policy['providers']['nebius']['active_scope']='win4'
        guard.policy_path.write_text(json.dumps(policy));self.assertTrue(guard.reserve('nebius','.02','model:classify'))
        self.assertEqual(guard.summary()['scopes']['win18-20-20261024:notes']['held'],'0.019')

    def test_scope_cannot_charge_another_purpose_or_increase_a_ceiling(self):
        guard=self.scoped_guard()
        with self.assertRaises(SpendError):guard.reserve('nebius','.001','model:diagnose')
        policy=json.loads(guard.policy_path.read_text());policy['providers']['nebius']['scopes']['notes']='.03'
        guard.policy_path.write_text(json.dumps(policy))
        with self.assertRaises(SpendError):guard.check('nebius')

    def test_switching_scope_cannot_dispatch_an_old_reservation(self):
        guard=self.scoped_guard();token=guard.reserve('nebius','.001','model:maintainer_note')
        policy=json.loads(guard.policy_path.read_text());policy['providers']['nebius']['active_scope']='win4'
        guard.policy_path.write_text(json.dumps(policy))
        with self.assertRaises(SpendError):guard.verify_reservation(token,'nebius')

    def test_atomic_scope_holds_cannot_use_the_other_scopes_money(self):
        guard=self.scoped_guard()
        def reserve(_):
            try:guard.reserve('nebius','.015','model:maintainer_note');return True
            except SpendError:return False
        with ThreadPoolExecutor(max_workers=4) as pool:self.assertEqual(sum(pool.map(reserve,range(4))),1)

    def test_existing_unscoped_charge_is_preserved_when_schema_migrates(self):
        import sqlite3
        guard=self.scoped_guard()
        with sqlite3.connect(guard.db_path) as db:
            db.execute('CREATE TABLE reservations (id TEXT PRIMARY KEY,provider TEXT,held TEXT,status TEXT,purpose TEXT)')
            db.execute('INSERT INTO reservations VALUES (?,?,?,?,?)',('old','nebius','.01407140','spent','model:diagnose'))
        summary=guard.summary();self.assertEqual(summary['providers']['nebius']['held'],'0.01407140')
        self.assertEqual(summary['scopes'],{})


    def followup_guard(self):
        guard=verified_guard(self.root,cap='1.95407140',balance='10')
        # This synthetic older call has the exact hold and scope acknowledged by
        # a separate explicit follow-up authorization; no old row is rewritten.
        token='a'*32
        with guard.connection() as db:
            db.execute('INSERT INTO reservations VALUES (?,?,?,?,?,?)',
                       (token,'nebius','.029654','unknown','model:diagnose','win18-20-20261024:win4'))
        policy=json.loads(guard.policy_path.read_text())
        policy['providers']['nebius'].update(funding_mode='bounded_prepaid_followup',
            positive_balance_verified=True,automatic_charge_trigger='non_positive_balance',
            billing_reference='https://docs.tokenfactory.nebius.com/other-capabilities/billing-new',
            authorization_id='win4-md2html-followup-20261024',authorization_budget_usd='.24',
            historical_allowance_usd='1.71407140',active_scope='win4',max_per_attempt_usd='.08',
            max_attempts=3,stop_on_success=True,unknown_charge_continuation_confirmed=True,
            acknowledged_unknown_reservations={token:'.029654'})
        guard.policy_path.write_text(json.dumps(policy))
        return guard,token

    def test_explicit_followup_keeps_unknown_hold_and_independent_scope(self):
        guard,old=self.followup_guard()
        token=guard.reserve('nebius','.01','model:classify')
        guard.verify_reservation(token,'nebius');guard.finish(token,'.002')
        summary=guard.summary()
        self.assertEqual(summary['providers']['nebius'],{'held':'0.031654','requests':2,'unresolved':1})
        self.assertEqual(summary['scopes']['win4-md2html-followup-20261024:win4']['held'],'0.002')
        with guard.connection() as db:
            self.assertEqual(db.execute('SELECT held,status FROM reservations WHERE id=?',(old,)).fetchone(),('.029654','unknown'))

    def test_followup_cannot_borrow_unused_old_authorization(self):
        guard,_=self.followup_guard()
        for _ in range(3):
            token=guard.reserve('nebius','.08','model:diagnose')
            guard.verify_reservation(token,'nebius');guard.finish(token,'.08')
        with self.assertRaises(SpendError) as error:guard.reserve('nebius','.001','model:classify')
        self.assertEqual(error.exception.code,'scope_budget_exhausted')

    def test_followup_requires_exact_owner_acknowledged_hold(self):
        guard,old=self.followup_guard()
        with guard.connection() as db:db.execute('UPDATE reservations SET held=? WHERE id=?',('.03',old))
        with self.assertRaises(SpendError) as error:guard.reserve('nebius','.01','model:classify')
        self.assertEqual(error.exception.code,'provider_usage_unknown')

    def test_new_unknown_relocks_followup_even_with_unused_budget(self):
        guard,_=self.followup_guard()
        token=guard.reserve('nebius','.01','model:diagnose')
        guard.verify_reservation(token,'nebius');guard.finish(token,None)
        with self.assertRaises(SpendError) as error:guard.reserve('nebius','.01','model:classify_r2')
        self.assertEqual(error.exception.code,'provider_usage_unknown')
        self.assertEqual(guard.summary()['providers']['nebius']['unresolved'],2)

    def test_new_unknown_blocks_an_already_reserved_followup_before_dispatch(self):
        guard,_=self.followup_guard()
        first=guard.reserve('nebius','.01','model:diagnose')
        second=guard.reserve('nebius','.01','model:diagnose_r2')
        guard.verify_reservation(first,'nebius');guard.finish(first,None)
        with self.assertRaises(SpendError):guard.verify_reservation(second,'nebius')

    def test_followup_cannot_expand_scope_or_skip_explicit_confirmation(self):
        guard,_=self.followup_guard();original=json.loads(guard.policy_path.read_text())
        for changes in ({'unknown_charge_continuation_confirmed':False},{'authorization_budget_usd':'.25'},
                        {'max_total':'2'},{'active_scope':'formal'},{'max_attempts':4},
                        {'acknowledged_unknown_reservations':{}},{'max_per_attempt_usd':'.09'}):
            policy=json.loads(json.dumps(original));policy['providers']['nebius'].update(changes)
            guard.policy_path.write_text(json.dumps(policy))
            with self.subTest(changes=changes),self.assertRaises(SpendError):guard.reserve('nebius','.01','model:classify')
        guard.policy_path.write_text(json.dumps(original))
        with self.assertRaises(SpendError):guard.reserve('nebius','.01','model:acceptance_draft')
        with self.assertRaises(SpendError):guard.reserve('nebius','.081','model:diagnose')

    def test_old_overrun_is_never_covered_by_followup_acknowledgement(self):
        guard,old=self.followup_guard()
        with guard.connection() as db:db.execute('UPDATE reservations SET status=? WHERE id=?',('overrun',old))
        with self.assertRaises(SpendError):guard.reserve('nebius','.01','model:classify')


if __name__=='__main__':unittest.main()
