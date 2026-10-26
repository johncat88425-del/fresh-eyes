"""Public export keeps original result bytes and makes omissions explicit."""
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('public_source', Path(__file__).resolve().parents[1] / 'packaging/public_source.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PublicSource(unittest.TestCase):
    def fixture(self):
        local = '/' + '/'.join(['Users', 'synthetic-owner', 'private', 'project'])
        return local, {
            'README.md': b'# SYNTHETIC source\n[Metadata](reports/private.json)\n',
            'READ_ME_FIRST.txt': b'Synthetic fixture',
            '.gitignore': b'reports/\n.env\n.fresh-eyes/\n',
            'GUIDE.md': ('Local test path: ' + local).encode(),
            'OPEN_REPORTS.html': b'<a href="reports/private.json">metadata</a><a href="reports/report.json">report</a>',
            'reports/private.json': json.dumps({'source_root': local}).encode(),
            'reports/report.json': b'{"data_origin":"example","status":"needs_review"}',
            'reports/binding.json': b'{"scope":"SYNTHETIC, not an execution"}',
            'src/example.py': b'# Synthetic source; no execution\n',
        }

    def test_omissions_and_doc_redactions_do_not_rewrite_result_or_binding(self):
        local, original = self.fixture()
        before = dict(original)
        files, manifest = publisher.public_files(original)
        self.assertEqual(original, before)
        self.assertNotIn('reports/private.json', files)
        self.assertIn('reports/private.json', manifest['omitted'])
        self.assertNotIn(local.encode(), files['GUIDE.md'])
        self.assertIn(b'<local-path>', files['GUIDE.md'])
        self.assertEqual(files['reports/report.json'], original['reports/report.json'])
        self.assertEqual(files['reports/binding.json'], original['reports/binding.json'])
        self.assertIn(b'PUBLIC_EXPORT.md', files['OPEN_REPORTS.html'])
        self.assertIn(b'(PUBLIC_EXPORT.md)', files['README.md'])
        self.assertFalse(manifest['published'])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '.gitignore').write_bytes(files['.gitignore'])
            subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
            for name, ignored in [('reports/report.json', False), ('reports/private.json', True),
                                  ('reports/new-local-run/report.json', True), ('.env', True)]:
                result = subprocess.run(['git', '-C', str(root), 'check-ignore', '--no-index', name], capture_output=True)
                self.assertEqual(result.returncode, 0 if ignored else 1, name)

    def test_host_path_in_code_is_refused_instead_of_silently_removing_code(self):
        self.assertFalse(publisher.private_paths(r'/home/(?!default/projects/charset_normalizer/)'))
        local, files = self.fixture()
        files['src/example.py'] = ('file = ' + repr(local)).encode()
        with self.assertRaisesRegex(ValueError, 'Unexpected local path'):
            publisher.public_files(files)

    def test_input_tampering_and_private_state_are_rejected(self):
        _, files = self.fixture()
        for mode in ('tampered', 'private'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'handoff.zip'
                chosen = dict(files)
                if mode == 'private':
                    chosen['.env'] = b'SYNTHETIC forbidden private state'
                hashes = {n: publisher.sha(raw) for n, raw in chosen.items()}
                if mode == 'tampered':
                    hashes['README.md'] = '0' * 64
                with zipfile.ZipFile(path, 'w') as archive:
                    for name, raw in chosen.items():
                        archive.writestr('fresh-eyes/' + name, raw)
                    archive.writestr('fresh-eyes/handoff-manifest.json', json.dumps({'files': hashes}))
                with self.assertRaises(ValueError):
                    publisher.read_handoff(path)

    def test_export_never_overwrites_existing_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'existing.zip'
            output.write_bytes(b'SYNTHETIC original artifact')
            with self.assertRaisesRegex(ValueError, 'Preserve'):
                publisher.build(Path(temporary) / 'unused-input.zip', output)
            self.assertEqual(output.read_bytes(), b'SYNTHETIC original artifact')

    def test_internal_collaboration_and_account_drafts_are_not_published(self):
        _, original = self.fixture()
        for name in ('TEAM_TASKS.md', 'FEEDBACK.md', 'api/B26_PUBLIC_SOURCE_CHECK.md'):
            original[name] = b'SYNTHETIC internal account and collaboration notes'
        original['README.md'] += b'[Team](TEAM_TASKS.md)\n'
        files, manifest = publisher.public_files(original)
        for name in ('TEAM_TASKS.md', 'FEEDBACK.md', 'api/B26_PUBLIC_SOURCE_CHECK.md'):
            self.assertNotIn(name, files)
            self.assertIn(name, manifest['omitted'])
        self.assertIn(b'[Team](PUBLIC_EXPORT.md)', files['README.md'])
