"""Portable recorded demo export retains data and refuses changed inputs."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('demo_single_export', ROOT / 'web/demo/single_file.py')
single = importlib.util.module_from_spec(spec)
spec.loader.exec_module(single)
build_spec = importlib.util.spec_from_file_location('demo_export_builder', ROOT / 'web/demo/build.py')
builder = importlib.util.module_from_spec(build_spec)
build_spec.loader.exec_module(builder)


class SingleFileDemoTests(unittest.TestCase):
    def test_exports_recordings_without_network_fallback_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / 'demo', Path(tmp) / 'index.html'
            builder.build(ROOT, source)
            original = (source / 'index.html').read_bytes()
            result = single.build_single(source, output)
            text = output.read_text()
            self.assertIn('id="fresh-eyes-recordings"', text)
            self.assertIn('type="importmap"', text)
            self.assertNotIn('src="app.js"', text)
            self.assertGreater(result['embedded_recordings'], 20)
            self.assertEqual((source / 'index.html').read_bytes(), original)
            self.assertIn('new Response(\'Recording not found\', {status: 404})',
                          __import__('base64').b64decode(json.loads(text.split('<script type="importmap">')[1].split('</script>')[0])['imports']['fresh-eyes/recordings'].split(',', 1)[1]).decode())
            with self.assertRaises(ValueError):
                single.build_single(source, output)

    def test_tampered_recording_is_refused_before_creating_public_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / 'demo', Path(tmp) / 'index.html'
            builder.build(ROOT, source)
            (source / 'data/cases.json').write_text('[]')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                single.build_single(source, output)
            self.assertFalse(output.exists())
