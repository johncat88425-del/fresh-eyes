"""Opt-in Tavily research (WIN-11): synthetic responses only, no network, no credits."""
import io
import json
import unittest
import tempfile
from pathlib import Path
from test_spending import verified_guard
from unittest.mock import patch
from urllib.error import HTTPError

from fresh_eyes.contracts import Contract
from fresh_eyes.diagnostics import RepairPlanner
from fresh_eyes.models import ModelCaller, ModelError, ModelLedger
from fresh_eyes.web_research import ENDPOINT, TavilyTransport, WebResearcher
import test_models as tm
from test_core import case_data


class SyntheticTavily:
    """Test double: records payloads and returns a fixed Tavily-shaped response, or raises."""
    data_origin = "engineering_test"
    api_key = "tvly-synthetic-research-key"

    def __init__(self, response=None, error=None):
        self.response, self.error, self.payloads = response, error, []

    def search(self, payload):
        self.payloads.append(payload)
        if self.error:
            raise self.error
        return self.response


RESPONSE = {"request_id": "synthetic-request", "usage": {"credits": 1}, "results": [
    {"title": "unit · PyPI", "url": "https://pypi.org/project/unit/", "content": "Install with pip install unit. " + "x" * 2000},
    {"title": "Docs", "url": "https://unit.readthedocs.io/en/latest/install.html", "content": "tvly-synthetic-research-key run unit"},
    {"title": "Elsewhere", "url": "https://blog.example.com/unit", "content": "off-allowlist"},
    {"title": "Plain http", "url": "http://pypi.org/project/unit/", "content": "not https"}]}


class WebResearcherTests(unittest.TestCase):
    def setUp(self):
        self.contract = Contract.from_dict(case_data())
        self.tmp=tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_transport_never_sends(self):
        transport = TavilyTransport("tvly-synthetic-research-key")
        with patch.object(transport.opener, "open") as opened, self.assertRaises(ModelError) as raised:
            transport.search({"query":"x","search_depth":"basic","auto_parameters":False,"include_answer":False,"include_raw_content":False})
        self.assertEqual(raised.exception.code, "research_disabled")
        opened.assert_not_called()

    def test_http_errors_are_closed_and_never_show_the_key(self):
        transport = TavilyTransport("tvly-synthetic-research-key", enabled=True,spend_guard=verified_guard(Path(self.tmp.name),provider="tavily"))
        body = io.BytesIO(b"tvly-synthetic-research-key echoed by server")
        error = HTTPError(ENDPOINT, 401, "tvly-synthetic-research-key", {}, body)
        with patch.object(transport.opener, "open", side_effect=error), self.assertRaises(ModelError) as raised:
            transport.search({"query":"x","search_depth":"basic","auto_parameters":False,"include_answer":False,"include_raw_content":False})
        self.assertNotIn("tvly-synthetic", str(raised.exception))
        self.assertTrue(body.closed)

    def test_lookup_sends_only_repo_identity_and_keeps_allowlisted_redacted_results(self):
        transport = SyntheticTavily(RESPONSE)
        record = WebResearcher(transport).lookup(self.contract)
        payload = transport.payloads[0]
        sent = json.dumps(payload)
        for private in ("expected", "fixtures", "input.json", "tvly-synthetic"):
            self.assertNotIn(private, sent)
        self.assertEqual(payload["search_depth"], "basic")
        self.assertFalse(payload["include_raw_content"])
        self.assertIn("pypi.org", payload["include_domains"])
        self.assertEqual([r["url"] for r in record["results"]],
                         ["https://pypi.org/project/unit/", "https://unit.readthedocs.io/en/latest/install.html"])
        self.assertLessEqual(len(record["results"][0]["content"].encode()), 800 + len("\n[TRUNCATED]\n"))
        self.assertNotIn("tvly-synthetic", json.dumps(record))
        self.assertEqual((record["status"], record["credits"], record["request_id"]), ("ok", 1, "synthetic-request"))

    def test_search_limit_counts_failed_searches_too(self):
        researcher = WebResearcher(SyntheticTavily(error=ModelError("research_request_error", "x")), max_searches=1)
        with self.assertRaises(ModelError):
            researcher.lookup(self.contract)
        self.assertEqual(researcher.lookup(self.contract), {"status": "skipped", "reason": "search_limit"})

    def test_limits_are_bounded(self):
        for kwargs in ({"max_results": 0}, {"max_results": 6}, {"max_searches": 3}, {"max_results": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ModelError):
                WebResearcher(SyntheticTavily(RESPONSE), **kwargs)

    def test_global_credit_cap_survives_a_new_researcher_and_prevents_http(self):
        guard=verified_guard(Path(self.tmp.name),provider='tavily',cap='1',balance='1000')
        transport=TavilyTransport('tvly-synthetic-research-key',enabled=True,spend_guard=guard)
        with patch.object(transport.opener,'open',return_value=io.BytesIO(json.dumps(RESPONSE).encode())) as opened:
            self.assertEqual(WebResearcher(transport).lookup(self.contract)['credits'],1)
            with self.assertRaises(ModelError):WebResearcher(transport).lookup(self.contract)
        self.assertEqual(opened.call_count,1)

    def test_unknown_credit_charge_locks_the_provider_and_does_not_retry(self):
        guard=verified_guard(Path(self.tmp.name),provider='tavily',cap='10',balance='1000')
        transport=TavilyTransport('tvly-synthetic-research-key',enabled=True,spend_guard=guard)
        with patch.object(transport.opener,'open',return_value=io.BytesIO(b'{"results":[]}')) as opened:
            with self.assertRaises(ModelError):WebResearcher(transport).lookup(self.contract)
            with self.assertRaises(ModelError):WebResearcher(transport).lookup(self.contract)
        self.assertEqual(opened.call_count,1)

    def test_credentials_queries_other_project_docs_and_other_github_repos_are_not_evidence(self):
        response={'usage':{'credits':1},'results':[{'title':'bad','content':'x','url':url} for url in (
            'https://secret@pypi.org/project/unit/','https://pypi.org/project/unit/?token=secret',
            'https://other.readthedocs.io/en/latest/','https://github.com/other/project/README.md')]}
        self.assertEqual(WebResearcher(SyntheticTavily(response)).lookup(self.contract)['results'],[])


class ResearchInDiagnosisTests(unittest.TestCase):
    """The same synthetic repository and scripted models as test_models, with a researcher attached."""

    def setUp(self):
        self.base = tm.DiagnosticEngineTests("test_all_ultra_uses_same_flow_with_both_roles_on_ultra")
        self.base.setUp()

    def tearDown(self):
        self.base.tearDown()

    def run_with(self, research_transport, answers=None, verdicts=(False, True, True)):
        b = self.base
        b.transport = tm.SyntheticTransport(b.answers if answers is None else answers)
        b.ledger = ModelLedger(b.root / "ledger.sqlite3", "run", "1")
        self.research = research_transport
        planner = RepairPlanner(ModelCaller(b.transport, b.ledger), b.nano, b.ultra, researcher=WebResearcher(research_transport))
        return tm.run_case(b.contract, b.source, b.wheels, b.root / "report", runner=tm.ScriptedRunner(list(verdicts)),
                           development=True, repairer=planner, max_patch_rounds=1)

    def diagnose_evidence(self):
        return json.loads(self.base.transport.payloads[1]["messages"][1]["content"])["evidence"]

    def test_results_reach_ultra_as_untrusted_cited_evidence_and_are_recorded(self):
        report = self.run_with(SyntheticTavily(RESPONSE))
        self.assertEqual(report["status"], "verified_fix")
        web = self.diagnose_evidence()["web_evidence"]
        self.assertIn("Untrusted", web["note"])
        self.assertEqual(web["results"][0]["url"], "https://pypi.org/project/unit/")
        self.assertEqual(report["diagnosis"]["web_research"][0]["credits"], 1)
        # Classification never sees web results; only the diagnosis does.
        self.assertNotIn("web_evidence", self.base.transport.payloads[0]["messages"][1]["content"])

    def test_research_failure_is_recorded_and_diagnosis_continues(self):
        report = self.run_with(SyntheticTavily(error=ModelError("research_request_error", "x")))
        self.assertEqual(report["status"], "verified_fix")
        self.assertEqual(report["diagnosis"]["web_research"][0]["status"], "unavailable")
        self.assertNotIn("web_evidence", self.diagnose_evidence())

    def test_no_search_when_classification_does_not_ask_for_a_patch(self):
        answers = [{"category": "infrastructure", "reason": "Synthetic runner problem"}]
        tavily = SyntheticTavily(RESPONSE)
        report = self.run_with(tavily, answers=answers, verdicts=(False,))
        self.assertEqual(tavily.payloads, [])
        self.assertNotIn("web_research", report["diagnosis"])


if __name__ == "__main__":
    unittest.main()
