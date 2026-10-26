"""An ID in logs/module text must never make a dead public route look valid."""
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("submission_links", Path(__file__).resolve().parents[1] / "submission/check_links.py")
links = importlib.util.module_from_spec(spec); spec.loader.exec_module(links)


class PublicRouteInventory(unittest.TestCase):
    def html(self, include_detail=True):
        run, nested = "a" * 32, "b" * 32
        assets = {"data/runs.json": json.dumps({"items": [{"run_id": run}]}),
                  "data/cases.json": json.dumps({"items": [{"case_id": "real-case", "commit_sha": "c" * 40}]})}
        if include_detail: assets["data/runs/" + run + ".json"] = json.dumps({"run_id": run, "evidence": {"run_id": nested, "text": "not-a-case"}})
        return '<script id="fresh-eyes-recordings" type="application/json">' + json.dumps(assets) + '</script><script>/* ' + nested + ' */</script>'

    def test_nested_evidence_id_and_script_text_are_not_routes(self):
        index = links.build_index(self.html())
        self.assertTrue(links.check(links.PAGES + "#/runs/" + "a" * 32, index)[0])
        self.assertFalse(links.check(links.PAGES + "#/runs/" + "b" * 32, index)[0])
        self.assertFalse(links.check(links.PAGES + "#/answer/" + "b" * 32, index)[0])

    def test_case_routes_use_the_case_list(self):
        index = links.build_index(self.html())
        self.assertTrue(links.check(links.PAGES + "?lang=zh#/cases/real-case", index)[0])
        self.assertFalse(links.check(links.PAGES + "#/cases/not-a-case", index)[0])

    def test_run_index_without_detail_does_not_pass(self):
        self.assertFalse(links.check(links.PAGES + "#/runs/" + "a" * 32, links.build_index(self.html(False)))[0])

    def test_missing_inventory_is_a_failure_not_an_empty_success(self):
        with self.assertRaises(ValueError): links.build_index('<h1>No data</h1>')


if __name__ == '__main__': unittest.main()
