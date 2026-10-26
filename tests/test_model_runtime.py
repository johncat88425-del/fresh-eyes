"""Paid activation uses mocked metadata and synthetic keys only; no HTTP call."""
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from fresh_eyes.model_runtime import OwnerModelFactory, CATALOG
from fresh_eyes.models import ModelError
from test_models import spec, SyntheticTransport


class OwnerActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.env=self.root/'synthetic.env';self.env.write_text('NEBIUS_API_KEY=synthetic-test-key-only\n')
        self.specs={'nano':spec(),'ultra':spec('ultra-test','1','3')}
        self.models=[{'id':s.model_id,'context_length':s.context_window,'pricing':
            {'prompt':str(float(s.input_per_million)/1_000_000),'completion':str(float(s.output_per_million)/1_000_000),
             'request':'0','input_cache_read':None}} for s in self.specs.values()]

    def tearDown(self):self.temp.cleanup()

    def opener(self):
        opener=Mock();opener.open.return_value=io.BytesIO(json.dumps({'data':self.models}).encode());return opener

    def test_disabled_activation_reads_no_key_and_makes_no_metadata_request(self):
        for value in (False,'true'):
            opened=self.opener()
            with patch('fresh_eyes.model_runtime.SandboxConfig.load') as loaded, patch('fresh_eyes.model_runtime.NemotronPromptBound') as bounded:
                with self.assertRaisesRegex(ModelError,'explicit'):
                    OwnerModelFactory(self.specs,self.env,self.root,enabled=value,opener=opened)
                loaded.assert_not_called();bounded.assert_not_called();opened.open.assert_not_called()

    def test_verified_catalog_builds_expected_route_with_fresh_private_ledger(self):
        opened=self.opener()
        with patch('fresh_eyes.model_runtime.NemotronPromptBound',return_value=lambda s,m:(100,'synthetic-reference')):
            factory=OwnerModelFactory(self.specs,self.env,self.root,enabled=True,opener=opened)
        request=opened.open.call_args.args[0]
        self.assertEqual(request.full_url,CATALOG)
        self.assertEqual(request.get_method(),'GET')
        self.assertNotIn('synthetic-test-key-only',json.dumps(factory.preflight))
        location=self.root/'slot';location.mkdir()
        with patch('fresh_eyes.model_runtime.NebiusTransport',return_value=SyntheticTransport([])) as transport:
            planner=factory(None,location,group='D',max_usd='0.02',deadline=time.monotonic()+60,run_id='synthetic-job')
        self.assertEqual(planner.route,'all_ultra')
        self.assertEqual(planner.caller.ledger.summary()['run_id'],'synthetic-job')
        self.assertEqual(planner.caller.ledger.summary()['calls'],[])
        self.assertTrue(planner.caller.ledger.path.is_relative_to(location))
        self.assertTrue(transport.call_args.kwargs['enabled'])

    def test_changed_rate_or_context_stops_before_any_inference_transport(self):
        for field in ('price','context'):
            original=json.loads(json.dumps(self.models))
            if field=='price':self.models[0]['pricing']['prompt']='0.5'
            else:self.models[0]['context_length']=123
            with patch('fresh_eyes.model_runtime.NemotronPromptBound'), patch('fresh_eyes.model_runtime.NebiusTransport') as transport:
                with self.assertRaisesRegex(ModelError,'could not be matched'):
                    OwnerModelFactory(self.specs,self.env,self.root,enabled=True,opener=self.opener())
                transport.assert_not_called()
            self.models=original

    def test_research_key_is_loaded_only_for_an_explicit_research_plan(self):
        from fresh_eyes.web_research import WebResearcher
        from test_web_research import SyntheticTavily, RESPONSE
        for enabled in (False, True):
            with self.subTest(enabled=enabled), patch('fresh_eyes.model_runtime.NemotronPromptBound',return_value=lambda s,m:(100,'synthetic-reference')):
                factory=OwnerModelFactory(self.specs,self.env,self.root,enabled=True,opener=self.opener(),web_research_enabled=enabled)
            location=self.root/str(enabled);location.mkdir()
            fake=SyntheticTavily(RESPONSE)
            with patch('fresh_eyes.web_research.load_transport',return_value=fake) as loaded, patch('fresh_eyes.model_runtime.NebiusTransport',return_value=SyntheticTransport([])):
                planner=factory(None,location,group='C',max_usd='0.02',deadline=time.monotonic()+60,run_id='synthetic-job')
            self.assertEqual(isinstance(planner.researcher,WebResearcher),enabled)
            self.assertEqual(loaded.call_count,int(enabled))
            self.assertEqual(fake.payloads,[])

    def test_metadata_http_error_is_closed_and_does_not_expose_key_or_body(self):
        body=io.BytesIO(b'synthetic-test-key-only')
        opened=Mock();opened.open.side_effect=HTTPError(CATALOG,401,'synthetic-test-key-only',{},body)
        with patch('fresh_eyes.model_runtime.NemotronPromptBound'):
            with self.assertRaises(ModelError) as caught:
                OwnerModelFactory(self.specs,self.env,self.root,enabled=True,opener=opened)
        self.assertNotIn('synthetic-test-key-only',str(caught.exception))
        self.assertTrue(body.closed)


if __name__=='__main__':unittest.main()
