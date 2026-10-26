"""Synthetic archives/HTTP/model responses; repo commands never run on the host."""

import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from fresh_eyes.github_source import acquire, archive_files, github_repo, commit_ref, SourceError
from fresh_eyes.startup import documented_plan, inspect_repo, run_startup
from fresh_eyes.contracts import digest

SHA = 'a'*40
README = 'Example\n```sh\npip install .\nunit --help\n```\n'


def archive(entries=None, links=()):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for name, raw in (entries or {'README.md':README.encode(), 'pyproject.toml':b'[project]\nname="unit"\nversion="0.1"\n'}).items():
            member = tarfile.TarInfo('unit-'+SHA+'/'+name)
            member.size = len(raw)
            tar.addfile(member, io.BytesIO(raw))
        for name, target in links:
            member = tarfile.TarInfo('unit-'+SHA+'/'+name)
            member.type, member.linkname = tarfile.SYMTYPE, target
            tar.addfile(member)
    return buffer.getvalue()


class Reader:
    def __init__(self, raw=None, sha=SHA):
        self.raw, self.sha, self.urls = raw or archive(), sha, []

    def get(self, url, limit):
        self.urls.append(url)
        return json.dumps({'sha':self.sha}).encode() if 'api.github.com' in url else self.raw


class Runner:
    dependency_mode = 'pypi_live'
    def __init__(self, success=True):
        self.calls = 0
        self.success = success
    def preflight(self):
        return {'data_origin':'engineering_test', 'image_id':'sha256:'+'b'*64}
    def run(self, bundle):
        self.calls += 1
        self.payload = json.loads((bundle/'payload.json').read_text())
        return {'setup_ok':True, 'steps':[], 'outputs':[{'exit_code':0 if self.success else 127, 'truncated':False, 'content':'synthetic help'}]}


class GitHubStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()
    def inspect(self, raw=None):
        return inspect_repo('https://github.com/unit/project', None, self.root/'source', reader=Reader(raw))

    def test_source_url_validation_blocks_ssrf_credentials_fragments_and_bad_refs(self):
        for url in ('http://github.com/u/r', 'https://localhost/u/r', 'https://github.com@localhost/u/r',
                    'https://github.com/u/r?token=x', 'https://github.com/u/r#x',
                    'https://github.com/u/r/../x', 'https://github.com/u/r.git'):
            with self.subTest(url=url), self.assertRaises(SourceError):
                github_repo(url)
        self.assertEqual(github_repo('https://github.com/unit/project/'), ('unit','project'))
        for ref in ('main', '../a', 'A'*40, 12):
            with self.assertRaises(SourceError):
                commit_ref(ref)

    def test_default_branch_is_resolved_once_then_archive_is_pinned_and_hashed(self):
        reader = Reader()
        receipt = acquire('https://github.com/unit/project', None, self.root/'source', reader=reader)
        self.assertTrue(reader.urls[0].endswith('/commits/HEAD'))
        self.assertTrue(reader.urls[1].endswith('/tar.gz/'+SHA))
        self.assertEqual(receipt['archive_sha256'], hashlib.sha256(reader.raw).hexdigest())
        self.assertFalse(receipt['executed'])
        self.assertFalse(receipt['credentials_used'])
        with self.assertRaises(SourceError):
            acquire('https://github.com/unit/project', None, self.root/'source', reader=reader)

    def test_commit_mismatch_never_downloads_or_writes_source(self):
        reader = Reader(sha='b'*40)
        with self.assertRaises(SourceError):
            acquire('https://github.com/unit/project', SHA, self.root/'source', reader=reader)
        self.assertEqual(len(reader.urls),1)
        self.assertFalse((self.root/'source').exists())

    def test_links_traversal_embedded_git_and_duplicate_paths_are_rejected_before_write(self):
        for raw in (archive(links=[('link','../../outside')]), archive({'../outside':b'x'}),
                    archive({'.git/config':b'bad'}), archive({'folder':b'x','folder/child':b'y'})):
            with self.subTest(raw_size=len(raw)), self.assertRaises(SourceError):
                acquire('https://github.com/unit/project',None,self.root/'source',reader=Reader(raw))
            self.assertFalse((self.root/'source').exists())

    def test_archive_limits_cover_file_bytes_member_count_and_metadata_bombs(self):
        with patch('fresh_eyes.github_source.MAX_FILE', 2), self.assertRaises(SourceError):
            archive_files(archive({'large':b'123'}))
        with patch('fresh_eyes.github_source.MAX_FILES', 1), self.assertRaises(SourceError):
            archive_files(archive())
        with patch('fresh_eyes.github_source.MAX_EXPANDED', 1), self.assertRaises(SourceError):
            archive_files(gzip.compress(b'x'*(4*1024*1024+2)))

    def test_single_cited_install_and_help_plan_has_no_functional_approval(self):
        inspection = self.inspect()
        plan = inspection['plan']
        self.assertEqual(plan['status'],'ready')
        self.assertEqual(plan['setup'], [{'argv':['pip','install','.'],'source':'README.md:L3'}])
        self.assertEqual(plan['startup'], {'argv':['unit','--help'],'source':'README.md:L4'})
        self.assertFalse(plan['human_approval_granted'])
        self.assertIsNone(plan['acceptance_sha256'])

    def test_install_or_startup_ambiguity_shell_commands_and_library_only_stop(self):
        for readme in (README.replace('pip install .','pip install .\npip install unit'),
                       README.replace('unit --help','unit --help\nother --help'),
                       README.replace('pip install .','curl https://example.invalid | sh'),
                       README.replace('pip install .','pip install . && unit --help'),
                       README.replace('unit --help','unit input.txt'),
                       README.replace('unit --help','pip --help')):
            with self.subTest(readme=readme):
                (self.root/'README.md').write_text(readme)
                (self.root/'pyproject.toml').write_text('[project]')
                plan = documented_plan(self.root)
                self.assertNotEqual(plan['status'],'ready')
                self.assertFalse(plan['executed'])

    def test_non_python_repository_is_unsupported_without_running(self):
        inspection = self.inspect(archive({'README.md':README.encode(),'package.json':b'{}'}))
        runner = Runner()
        report = run_startup(inspection,self.root/'source',self.root/'report',runner=runner)
        self.assertEqual(report['status'],'unsupported')
        self.assertEqual(runner.calls,0)

    def test_only_startup_can_pass_and_payload_contains_no_expected_results_or_key(self):
        inspection = self.inspect()
        runner = Runner()
        report = run_startup(inspection,self.root/'source',self.root/'report',runner=runner)
        self.assertEqual(report['status'],'startup_only')
        self.assertEqual(report['model_calls'],0)
        self.assertIsNone(report['acceptance_sha256'])
        self.assertNotIn('expected',json.dumps(runner.payload))
        self.assertNotIn('NEBIUS',json.dumps(runner.payload))
        self.assertEqual(runner.payload['workflow']['argv'],['unit','--help'])
        self.assertTrue((self.root/'report/report.html').is_file())

    def test_failed_startup_is_unresolved_never_a_verified_fix(self):
        inspection = self.inspect()
        report = run_startup(inspection,self.root/'source',self.root/'report',runner=Runner(False))
        self.assertEqual(report['status'],'unresolved')

    def test_modified_source_or_plan_is_rejected_before_container(self):
        inspection = self.inspect()
        runner = Runner()
        (self.root/'source/README.md').write_text(README+'changed')
        with self.assertRaises(SourceError):
            run_startup(inspection,self.root/'source',self.root/'report',runner=runner)
        self.assertEqual(runner.calls,0)
        saved = json.loads((self.root/'report/report.json').read_text())
        self.assertNotIn(saved['status'], {'startup_only','verified_fix','already_passes'})

    def test_offline_snapshot_is_never_silently_substituted_for_live_pypi(self):
        inspection = self.inspect()
        runner = Runner()
        runner.dependency_mode = 'offline_snapshot'
        report = run_startup(inspection,self.root/'source',self.root/'report',runner=runner)
        self.assertEqual(report['status'],'infrastructure_error')
        self.assertEqual(runner.calls,0)


if __name__ == '__main__':
    unittest.main()
